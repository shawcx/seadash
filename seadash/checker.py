"""Type checker: annotates the syntax tree with types and resolved names.

After checking, every Expr has `.ty` (a types.Type) and names, calls,
attributes and definitions have `.sym` saying what they refer to, so code
generation never has to guess.

The interesting part is *flow-sensitive variables*. Like Python, a name can
be rebound to a value of a different type:

    a = 2            # a is Var(a: int)
    a = str(a)       # a is now Var(a_1: str): a new C++ variable

The checker walks each function in order, tracking a `State` that maps each
name to what it holds *at this point*:

    Bound(var, ty)   the name currently refers to `var`; `ty` may be narrower
                     than `var.type` (e.g. `int` inside `if x is not None:`
                     when var.type is `int?`)
    Conflict         different paths left different types here (if/else, loops)
    MaybeUnbound     assigned on some paths but not others

Where control flow joins (after if/else, around loops) states are merged.
A Conflict or MaybeUnbound is only an error if the name is *read* while in
that state; reassigning it first is fine.
"""

from __future__ import annotations

import dataclasses
import difflib
import re
import textwrap
from copy import deepcopy
from dataclasses import dataclass, field

from . import ast as A
from . import builtins, flow, threads
from .errors import CheckError, Loc
from .parser import parse
from .types import (
    BINARY_FILE, BOOL, BYTES, FLOAT, INT, JSON_VALUE, NONE, PATH, PRIMITIVES, SOCKET, STR, TEMPDIR, TEXT_FILE,
    DATE, DATETIME, DATETIME_TYPES, TIME, TIMEDELTA, UUID_T, SQLITE_CONNECTION, StructFormatType,
    SYNC_ARITY, ClassAttr, ClassRefType, CmpKeyType, ContextManagerType, EXIT_STACK, HTTPServerType, CounterType, FutureType, GeneratorType, MatchType, NamespaceType, PatternType, ProcessType,
    VarTupleType, DefaultDictType, DequeType, DictType, Field, FileType, FuncInfo, FuncType, IterType, ListType, ModuleType, OptionalType, Param, SyncType,
    SetType, StructType, TupleType, Type, Var, EnumInfo, EnumMember, enum_decays, enum_flag_op, enum_mixin,
    UNKNOWN, assignable, contains_unknown, element_type, is_hashable, is_numeric, join, strip_optional, widen,
)

INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


# ---- flow state -------------------------------------------------------------


@dataclass(frozen=True)
class Bound:
    var: Var
    ty: Type  # the type as seen at this point (narrowed), assignable to var.type


@dataclass(frozen=True)
class Conflict:
    sources: frozenset[tuple[Type, Loc]]  # (type, where that version was assigned)


LOCKED_VIEWS = ("mutex", "rw_read", "rw_write")  # `with m as data:`: data is only valid in the block


@dataclass(frozen=True)
class MaybeUnbound:
    pass


@dataclass(frozen=True)
class Moved(MaybeUnbound):
    """Handed to a seadash.Mutex, which now owns it: reading the name again is an error
    until it's given a new value."""

    mutex: str | None  # how the Mutex was written, for the message: "shared"
    loc: Loc
    var: Var  # (so `data = []` afterwards still knows it's a list[int])


Entry = Bound | Conflict | MaybeUnbound


@dataclass
class State:
    names: dict[str, Entry] = field(default_factory=dict)
    dead: bool = False  # after return/break/continue: unreachable
    # Attribute chains known not to be None here: ("u", "address") -> Address.
    # Codegen dereferences these with a runtime check (a call could have changed them).
    attrs: dict[tuple[str, ...], Type] = field(default_factory=dict)

    def copy(self) -> State:
        return State(dict(self.names), self.dead, dict(self.attrs))

    def forget_attrs(self, prefix: tuple[str, ...]) -> None:
        """Assigning to `u` or `u.address` invalidates what we knew about u.address.city etc."""
        for path in [p for p in self.attrs if p[: len(prefix)] == prefix]:
            del self.attrs[path]


def merge(states: list[State]) -> State:
    live = [s for s in states if not s.dead]
    if not live:
        # Keep the names so unreachable code after e.g. `return` still checks sensibly.
        return State(dict(states[0].names), dead=True, attrs=dict(states[0].attrs))
    merged = State()
    for name in {n for s in live for n in s.names}:
        entries = [s.names.get(name) for s in live]
        merged.names[name] = merge_entries(entries)
    # An attribute stays narrowed only if every path agrees.
    first = live[0].attrs
    merged.attrs = {p: t for p, t in first.items() if all(s.attrs.get(p) == t for s in live[1:])}
    return merged


def merge_entries(entries: list[Entry | None]) -> Entry:
    if moved := next((e for e in entries if isinstance(e, Moved)), None):
        return moved  # (moved on some path: say so, rather than "might not be assigned")
    if any(e is None or isinstance(e, MaybeUnbound) for e in entries):
        return MaybeUnbound()
    first = entries[0]
    if all(isinstance(e, Bound) and e.var is first.var for e in entries):
        same_view = all(e.ty == first.ty for e in entries)
        return first if same_view else Bound(first.var, first.var.type)
    sources: set[tuple[Type, Loc]] = set()
    for e in entries:
        if isinstance(e, Bound):
            sources.add((e.var.type, e.var.loc))
        else:
            sources |= e.sources
    return Conflict(frozenset(sources))


# ---- scopes -----------------------------------------------------------------


@dataclass
class FunctionScope:
    """Per-function bookkeeping. The module's top-level code is a scope too."""

    info: FuncInfo | None  # None for module-level code
    ret: Type | None  # None at module level ('return' not allowed)
    assigned: set[str]  # every name assigned anywhere in the body => local
    locals: list[Var]
    vars: dict[tuple[str, Type], Var] = field(default_factory=dict)
    cpp_names: set[str] = field(default_factory=set)
    nonlocals: set[str] = field(default_factory=set)  # `nonlocal x` in this (nested) function
    global_names: set[str] = field(default_factory=set)  # `global x` in this function
    frame: int = 0
    outer_assigned: set[str] = field(default_factory=set)  # locals of enclosing functions

    @property
    def is_module(self) -> bool:
        return self.info is None


@dataclass
class LoopContext:
    breaks: list[State] = field(default_factory=list)
    continues: list[State] = field(default_factory=list)


@dataclass
class CallTarget:
    """What a Call resolved to; stored in Call.sym for code generation.

    kind: 'func' (user function), 'method' (user method), 'ctor' (struct/class
    construction), 'builtin', 'builtin_method', 'module_func'.
    args: one entry per parameter, in declaration order, for user functions,
    methods and constructors (None = use that parameter's default). For the
    builtin kinds codegen reads call.args / call.keywords directly.
    """

    kind: str
    target: object
    args: list[A.Expr | None] | None = None
    params: list[Param] | None = None  # for 'ctor': the parameters `args` line up with


@dataclass
class WithInfo:
    """How a `with` item is entered and exited; stored in WithItem.sym for codegen."""

    kind: str  # 'file' (close() on exit) or 'object' (__enter__/__exit__)
    enter_type: Type
    exit: FuncInfo | None
    suppresses: bool  # __exit__ returns bool: it may swallow the exception


TYPING_ALIASES = {"List": "list", "Dict": "dict", "Set": "set", "Tuple": "tuple"}


@dataclass
class ModuleInfo:
    """Everything codegen needs about a checked module."""

    structs: list[StructType]
    functions: list[FuncInfo]
    globals: list[Var]
    main_locals: list[Var]
    imports: list[builtins.Module]
    generics: dict = field(default_factory=dict)  # name -> GenericDef, for importers
    spawns: list = field(default_factory=list)  # threading.Thread(...) calls, for threads.verify


def check(module: A.Module, name: str = "__main__", loader=None) -> ModuleInfo:
    return Checker(name, loader).check_module(module)


class ImportCycle(Exception):
    def __init__(self, chain: list[str]):
        super().__init__(" -> ".join(chain))
        self.chain = chain


def with_submodules(mods: list[builtins.Module]) -> list[builtins.Module]:
    """`import urllib.request` binds `urllib`; its submodules' headers and libraries are needed too."""
    out: list[builtins.Module] = []
    for m in mods:
        out.append(m)
        if not isinstance(m, builtins.UserModule):
            out += with_submodules([s for s in m.members.values() if isinstance(s, builtins.Module)])
    return out


def mixed_tuples(a: Type, b: Type) -> bool:
    """tuple(xs) < (1, 2): a tuple[T, ...] and a tuple of Ts compare item by item."""
    if isinstance(b, VarTupleType):
        a, b = b, a
    return isinstance(a, VarTupleType) and isinstance(b, TupleType) and all(t == a.elem for t in b.elts)


class Checker:
    def imported_modules(self) -> list[builtins.Module]:
        """The modules whose headers and libraries the program needs. seadash's thread types
        are in threading's header, so using them (or `import seadash`) brings it in."""
        mods = list(self.modules.values()) + [m for m, _ in self.imported.values()] + self.needed_modules
        seadash = builtins.MODULES["seadash"]
        if seadash in self.modules.values() or any(
                m is seadash and member in builtins.SEADASH_THREAD_TYPES for m, member in self.imported.values()):
            mods.append(builtins.MODULES["threading"])
        return mods

    def __init__(self, module_name: str = "__main__", loader=None) -> None:
        self.module_name = module_name
        # loader(dotted_name) -> builtins.UserModule | None: finds and checks another .sd file.
        self.loader = loader
        self.structs: dict[str, StructType] = {}
        self.functions: dict[str, FuncInfo] = {}
        self.modules: dict[str, builtins.Module] = {}  # `import math` / `import math as m`
        self.imported: dict[str, tuple[builtins.Module, str]] = {}  # `from math import sqrt`
        self.needed_modules: list[builtins.Module] = []  # used without an import: Path.glob needs fnmatch
        self.globals: dict[str, Var] = {}
        self.module_assign_counts: dict[str, int] = {}
        self.scope: FunctionScope | None = None
        self.state = State()
        self.loops: list[LoopContext] = []
        self.handler_depth = 0  # inside an `except` block (bare `raise` allowed)
        self.finally_loops: list[int] = []  # loop depth on entering each enclosing `finally`
        self.lambda_depth = 0
        # Generics (see "Generics" below)
        self.generics: dict[str, GenericDef] = {}
        self.instances: dict[tuple, FuncInfo | StructType] = {}
        self.type_env: dict[str, Type] = {}
        self.pending: list = []  # generic instance bodies waiting to be checked
        self.spawns: list = []  # threading.Thread(...) calls, verified once the program is checked
        self.with_contexts: set[int] = set()  # the context expressions of with statements (by id)
        self.decorated: dict[str, str] = {}  # decorated function name -> hidden name of the original
        self.argument_parsers: dict[int, list] = {}  # parser key -> [(dest, type)] from add_argument()
        self.subcommands: dict[int, dict] = {}  # parser key -> its add_subparsers() and add_parser()s
        self.module_checked = False
        self.out_structs: list[StructType] = []
        self.out_functions: list[FuncInfo] = []
        # Every function body and lambda gets a frame number; a variable read from
        # a different frame than it was created in is captured by a closure.
        self.frame = 0
        self.frame_count = 0

    def error(self, message: str, node: A.Node | Loc) -> CheckError:
        return CheckError(message, node if isinstance(node, Loc) else node.loc)

    # =========================================================================
    # Module structure: declarations first, then bodies
    # =========================================================================

    def check_module(self, module: A.Module) -> ModuleInfo:
        top_level: list[A.Stmt] = []
        struct_nodes: list[A.ClassDef] = []
        func_nodes: list[A.FunctionDef] = []

        for stmt in module.body:  # imports first: class and function decorators may come from them
            if isinstance(stmt, (A.Import, A.ImportFrom)):
                self.declare_import(stmt)
        for stmt in module.body:
            match stmt:
                case A.ClassDef() | A.FunctionDef() if stmt.type_params:
                    if stmt.decorators and isinstance(stmt, A.FunctionDef):
                        raise self.error("decorators on generic functions aren't supported yet", stmt.decorators[0])
                    self.declare_generic(stmt)
                case A.ClassDef():
                    self.declare_struct(stmt)
                    struct_nodes.append(stmt)
                case A.FunctionDef():
                    if stmt.name in self.functions or stmt.name in self.structs or stmt.name in self.generics:
                        raise self.error(f"'{stmt.name}' is already defined", stmt)
                    func_nodes.append(stmt)
                    if assignment := self.declare_decorated(stmt):
                        stmt.decorated = assignment  # codegen emits it where the def was
                        top_level.append(assignment)  # f = deco(<f>)
                    else:
                        self.functions[stmt.name] = None  # placeholder until signatures resolve
                case A.Import() | A.ImportFrom():
                    pass  # (declared above)
                case _:
                    top_level.append(stmt)

        for node in struct_nodes:
            self.resolve_base(node.sym)
        # Bases before subclasses, so a subclass can see what it inherits.
        for node in sorted(struct_nodes, key=lambda n: len(n.sym.ancestors())):
            self.resolve_struct_members(node.sym)
        for node in struct_nodes:
            self.check_overrides(node.sym)
        for node in func_nodes:
            key = self.decorated.get(node.name, node.name)
            info = self.resolve_signature(node, owner=None)
            if key != node.name:
                info.cpp_name = key  # the undecorated function; `f` itself is the decorated value
            info.cached = any(self.classify_decorator(d)[0] in ("cache", "lru_cache") for d in node.decorators)
            if info.cached:
                self.check_cacheable(info)
            self.functions[key] = info
        for node in struct_nodes:
            self.check_value_recursion(node.sym)
        for info in self.all_functions():
            self.check_param_defaults(info)
        for st in self.structs.values():
            self.check_field_defaults(st)

        # Module-level code runs first (it's `main`), so globals get their
        # types before function bodies that read them are checked.
        for name, count in count_assignments(top_level).items():
            self.module_assign_counts[name] = count
        main_scope = FunctionScope(None, None, assigned_names(top_level), [])
        self.enter(main_scope, State())
        self.check_block(top_level)

        for info in self.all_functions():
            self.check_function_body(info)
        self.check_frozen_changes(top_level, None)
        self.check_dropped_changes([], top_level, module=True)
        self.module_checked = True
        self.check_pending_instances()

        # Generic instances created later (by modules importing this one) are appended
        # to these same lists, so they're still generated with this module.
        self.out_structs[:0] = list(self.structs.values())
        self.out_functions[:0] = list(self.functions.values())
        return ModuleInfo(
            structs=self.out_structs,
            functions=self.out_functions,
            globals=list(self.globals.values()),
            main_locals=[v for v in main_scope.locals if v.kind != "global"],
            imports=with_submodules(self.imported_modules()),
            generics=self.generics,
            spawns=self.spawns,
        )

    def all_functions(self) -> list[FuncInfo]:
        infos = list(self.functions.values())
        for st in self.structs.values():
            infos += st.methods.values()
        return infos

    def declare_struct(self, node: A.ClassDef) -> None:
        if node.name in self.structs or node.name in self.functions or node.name in self.generics:
            raise self.error(f"'{node.name}' is already defined", node)
        if node.name in PRIMITIVES or node.name in builtins.CONTAINER_TYPES or node.name in builtins.EXCEPTIONS:
            raise self.error(f"can't define a class named '{node.name}'; that's a built-in type", node)
        if len(node.bases) > 1:
            raise self.error("multiple inheritance is not supported", node.bases[1])
        self.set_class_kind(node)
        st = StructType(node.name, node.kind, node, module=self.module_name)
        if (base := self.enum_base(node)) is not None:
            st.enum, st.frozen = EnumInfo(base, UNKNOWN), True
        node.sym = st
        self.structs[node.name] = st
        threads.ALL_CLASSES.append(st)

    def declare_import(self, node: A.Import | A.ImportFrom) -> None:
        if isinstance(node, A.Import):
            for alias in node.names:
                mod = self.find_module(alias.name, alias)
                if alias.asname or "." not in alias.name:
                    self.modules[alias.asname or alias.name] = mod
                elif isinstance(mod, builtins.UserModule):  # `import geometry.shapes` binds `geometry`
                    parts = alias.name.split(".")
                    pkg = self.modules.get(parts[0])
                    if not isinstance(pkg, builtins.UserModule):
                        pkg = builtins.UserModule(parts[0], {})
                        self.modules[parts[0]] = pkg
                    for part in parts[1:-1]:
                        pkg = pkg.members.setdefault(part, builtins.UserModule(part, {}))
                    pkg.members[parts[-1]] = mod
                else:  # `import os.path` binds `os`, like Python
                    top = alias.name.split(".")[0]
                    self.modules[top] = self.find_module(top, alias)
            return
        try:
            mod = self.find_module(node.module, node)
        except CheckError:
            # `from pkg import textutil`: pkg is a folder, and the names are its modules.
            subs = [self.loader(f"{node.module}.{a.name}") if self.loader else None for a in node.names]
            if not all(subs):
                raise
            for alias, sub in zip(node.names, subs):
                self.modules[alias.asname or alias.name] = sub
            return
        if [a.name for a in node.names] == ["*"]:  # every public name (not _private, not Class.member)
            for member in mod.members:
                if not member.startswith("_") and "." not in member:
                    self.imported[member] = (mod, member)
            return
        for alias in node.names:
            if alias.name not in mod.members and self.loader is not None and isinstance(mod, builtins.UserModule):
                sub = self.loader(f"{node.module}.{alias.name}")
                if sub is not None:
                    self.modules[alias.asname or alias.name] = sub
                    continue
            if alias.name not in mod.members:
                raise self.error(builtins.missing_member(mod, alias.name), alias)
            self.imported[alias.asname or alias.name] = (mod, alias.name)

    def find_module(self, name: str, node: A.Node) -> builtins.Module:
        if self.loader is not None:
            try:
                user = self.loader(name)
            except ImportCycle as cycle:
                raise self.error(f"circular import: {cycle}", node) from None
            if user is not None:  # a local .sd file wins, like Python's search path
                return user
        first, *rest = name.split(".")
        mod = builtins.MODULES.get(first)
        for part in rest:
            mod = mod.members.get(part) if mod is not None else None
            if not isinstance(mod, builtins.Module):
                mod = None
        if mod is None:
            local = f" (and no {name.replace('.', '/')}.sd next to this file)" if self.loader else ""
            close = difflib.get_close_matches(name, [m for m in builtins.MODULES if not m.startswith("_")], n=1)
            hint = f"; did you mean '{close[0]}'?" if close else ""
            raise self.error(f"no module named '{name}'{local}{hint}", node)
        return mod

    def lookup_struct(self, name: str) -> StructType | None:
        if name in self.imported:  # `from zlib import error`
            mod, member = self.imported[name]
            m = mod.members.get(member)
            return m if isinstance(m, StructType) else None
        return self.structs.get(name) or builtins.EXCEPTIONS.get(name)

    def module_struct(self, e: A.Expr) -> StructType | None:
        """`zlib.error`, `urllib.error.HTTPError`: a class defined by a module."""
        chain: list[str] = []
        while isinstance(e, A.Attribute):
            chain.insert(0, e.attr)
            e = e.value
        if not chain or not isinstance(e, A.Name) or e.id not in self.modules or e.id in self.state.names:
            return None
        m = self.modules[e.id]
        for attr in chain:
            if not isinstance(m, builtins.Module):
                return None
            m = m.members.get(attr)
        return m if isinstance(m, StructType) else None

    def resolve_base(self, st: StructType) -> None:
        """`class Dog(Animal):` Classes inherit from one class; structs (values) can't inherit."""
        if not st.node.bases or st.enum is not None:
            return
        base_expr = st.node.bases[0]
        base = self.resolve_type(base_expr)
        if isinstance(base, StructType) and base.enum is not None:
            raise self.error(f"an enum with members can't be inherited from: '{base.name}' is a fixed set of values",
                             base_expr)
        if not isinstance(base, StructType):
            raise self.error(f"a class can only inherit from another class, not {base}", base_expr)
        if st.kind != "class":
            what = ("an exception can't be a @value class" if base.is_exception
                    else "a @value class can't inherit (it's a value, not a shared object)")
            raise self.error(f"{what}: remove @value to make `class {st.name}({base.name}):` an ordinary class", st.node)
        if base.kind != "class":
            raise self.error(f"can't inherit from '{base.name}', a @value class; only ordinary classes can be inherited from",
                             base_expr)
        if base.is_subclass_of(st):
            raise self.error(f"'{st.name}' can't inherit from itself", base_expr)
        st.base = base

    def check_overrides(self, st: StructType) -> None:
        """An override must match the base method's signature (it becomes a C++ virtual override)."""
        if st.base is None:
            return
        for name, m in st.methods.items():
            if st.base.find_field(name):
                raise self.error(f"'{name}' is a field in {st.base.name}; a method can't reuse the name", m.node)
            base_m = st.base.find_method(name)
            if base_m is None or name == "__init__":
                continue
            if [p.type for p in m.params] != [p.type for p in base_m.params] or m.ret != base_m.ret:
                raise self.error(
                    f"{st.name}.{name}() overrides {base_m.owner.name}.{name}(), so it must have the same "
                    f"parameter and return types: {base_m}", m.node,
                )

    def resolve_struct_members(self, st: StructType) -> None:
        if st.enum is not None:
            self.resolve_enum_members(st)
            return
        for stmt in st.node.body:
            match stmt:
                case A.AnnAssign(A.Name(name), annotation, default):
                    if name in st.fields or (st.base and st.base.find_field(name)):
                        raise self.error(f"field '{name}' is already defined", stmt)
                    default = self.field_default(default)
                    stmt.value = default
                    st.fields[name] = Field(name, self.resolve_type(annotation), default, stmt.loc)
                case A.FunctionDef(name):
                    self.declare_method(st, stmt)
                case A.Assign([A.Name(name)], value):
                    self.declare_class_attr(st, name, value, stmt)
                case A.Pass() | A.ExprStmt(A.StrLit()):
                    pass  # `pass` or a docstring
                case _:
                    raise self.error(
                        f"a {st.kind} body can only contain fields (`x: int`), methods (`def ...`) and class "
                        f"attributes (`version = \"1.0\"`)", stmt
                    )
        for name, ca in st.class_attrs.items():
            if st.find_method(name) is not None:
                raise self.error(f"'{name}' is both a class attribute and a method of {st.name}", ca.loc)
        for d in reversed(st.node.decorators):  # (the one nearest the class applies first, as in Python)
            kind = self.classify_decorator(d)[0]
            if kind == "dataclass":
                self.apply_dataclass(st, d)
            elif kind == "total_ordering":
                self.apply_total_ordering(st, d)
        if any(t.frozen for t in st.ancestors()):
            for m in st.methods.values():
                if m.lazy:
                    raise self.error("@cached_property can't be used in a frozen class (it stores the value it computes)", m.node)
        self.check_dunder_signatures(st)
        if st.init is not None and st.init.ret != NONE:
            raise self.error("__init__ must not return a value", st.init.node)

    # ---- enums ----------------------------------------------------------------------

    def resolve_enum_members(self, st: StructType) -> None:
        """`class Color(Enum):` Each `NAME = value` in the body is a member. Values are
        constants, worked out here (auto() too), so aliases and @unique are settled now."""
        info = st.enum
        info.unique = any(self.classify_decorator(d)[0] == "unique" for d in st.node.decorators)
        values: dict[str, object] = {}  # every member so far, for `RW = R | W` and auto()
        for stmt in st.node.body:
            match stmt:
                case A.Assign([A.Name(name)], value):
                    if name.startswith("_"):
                        raise self.error(f"enum members can't start with '_' ('{name}'): those names are reserved", stmt)
                    if name in ("name", "value"):
                        raise self.error(f"an enum member can't be called '{name}': every member has .name and .value", stmt)
                    if name in values:
                        raise self.error(f"'{name}' already defined as {values[name]!r}", stmt)
                    v = self.enum_value(st, name, value, values)
                    values[name] = v
                    alias = next((m.name for m in info.members.values() if m.alias_of is None
                                  and type(m.value) is type(v) and m.value == v), None)
                    info.members[name] = EnumMember(name, v, stmt.loc, alias)
                case A.FunctionDef(name):
                    if name in ("__init__", "__new__", "__eq__", "__hash__"):
                        raise self.error(f"an enum can't define {name}() (its members are fixed values)", stmt)
                    if name in values:
                        raise self.error(f"'{name}' is already defined in {st.name}", stmt)
                    self.declare_method(st, stmt)
                case A.Pass() | A.ExprStmt(A.StrLit()):
                    pass
                case A.AnnAssign():
                    raise self.error("an enum's body has members (`RED = 1`) and methods, not fields", stmt)
                case _:
                    raise self.error("an enum's body can only contain members (`RED = 1`) and methods", stmt)
        for name in values:
            if name in st.methods:
                raise self.error(f"'{name}' is already defined in {st.name}", st.methods[name].node)
        for m in st.methods.values():
            if m.lazy:
                raise self.error("@cached_property can't be used in an enum (its members can't store anything); "
                                 "use @property", m.node)
        types = {name: constant_type(v) for name, v in values.items()}
        first = next(iter(types), None)
        for name, t in types.items():
            if t != types[first]:
                raise self.error(
                    f"an enum's values must all have the same type, but {first} is {types[first]} and {name} is {t}",
                    info.members[name].loc)
        info.value_type = types[first] if first is not None else (STR if info.base == "StrEnum" else INT)
        if info.flag:
            for m in info.members.values():
                if m.value < 0:
                    raise self.error(f"a flag's values can't be negative ({m.name} is {m.value})", m.loc)
        if info.unique and (aliases := [m for m in info.members.values() if m.alias_of is not None]):
            pairs = ", ".join(f"{m.name} -> {m.alias_of}" for m in aliases)
            raise self.error(f"duplicate values found in <enum '{st.name}'>: {pairs}", aliases[0].loc)
        self.check_dunder_signatures(st)

    def enum_value(self, st: StructType, name: str, e: A.Expr, earlier: dict[str, object]) -> object:
        """A member's value, worked out now. `auto()` gives the next number (the next bit in
        a Flag, the name in lowercase in a StrEnum)."""
        info = st.enum
        if isinstance(e, A.Call) and self.is_enum_auto(e.func):
            if e.args or e.keywords:
                raise self.error("auto() takes no arguments", e)
            if info.base == "StrEnum":
                return name.lower()
            ints = list(earlier.values())
            if any(type(v) is not int for v in ints):
                last = next(v for v in reversed(ints) if type(v) is not int)
                raise self.error(f"auto() can't follow a value that isn't an int: unable to increment {last!r}", e)
            if not ints:
                return 1
            if info.flag:
                return 2 ** max(ints).bit_length()
            return max(ints) + 1
        try:
            v = enum_constant(e, earlier)
        except ValueError:
            raise self.error(
                "an enum member's value must be a constant (a number, string, bytes, bool, or a tuple of those) "
                "or auto()", e) from None
        if info.base == "StrEnum" and type(v) is not str:
            raise self.error(f"{v!r} is not a string (a StrEnum's values are strs)", e)
        if info.base != "Enum" and type(v) is not int and info.base != "StrEnum":
            raise self.error(f"{info.base} members must be ints, not {constant_type(v)}", e)
        if type(v) is int and not INT64_MIN <= v <= INT64_MAX:
            raise self.error("integer value is too large for int (64-bit)", e)
        return v

    def is_enum_auto(self, func: A.Expr) -> bool:
        if isinstance(func, A.Name) and func.id in self.imported:
            mod, member = self.imported[func.id]
            return mod is builtins.MODULES["enum"] and member == "auto"
        return (isinstance(func, A.Attribute) and func.attr == "auto" and isinstance(func.value, A.Name)
                and self.modules.get(func.value.id) is builtins.MODULES["enum"])

    def declare_class_attr(self, st: StructType, name: str, value: A.Expr, stmt: A.Stmt) -> None:
        """`version = "1.0"`: a constant of the class, which a subclass may set to its own value."""
        if not is_constant(value):
            raise self.error(
                f"a class attribute must be a constant (a number, string, bytes, bool, None, or a tuple of those); "
                f"for anything else, use a field (`{name}: T = ...`) or a module-level variable", value,
            )
        if name in st.class_attrs:
            raise self.error(f"class attribute '{name}' is already defined", stmt)
        if name in st.fields or (st.base and st.base.find_field(name)):
            raise self.error(f"'{name}' is a field of {st.name}; a class attribute can't reuse the name", stmt)
        t = self.check_expr(value)
        if (inherited := st.base.find_class_attr(name) if st.base else None) is not None:
            if not assignable(t, inherited.type):
                raise self.error(f"{st.name}.{name} redefines {inherited.name} as {t}, but it's {inherited.type} in the "
                                 f"base class", value)
            t = inherited.type
        st.class_attrs[name] = ClassAttr(name, t, value, stmt.loc)

    def check_value_recursion(self, st: StructType) -> None:
        """A struct can't contain itself by value (it would be infinitely large)."""
        if st.kind != "struct":
            return

        def contains(t: Type) -> bool:
            match t:
                case StructType() if t is st:
                    return True
                case StructType(kind="struct"):
                    return any(contains(f.type) for f in t.fields.values())
                case OptionalType(inner):
                    return contains(inner)
                case TupleType(elts):
                    return any(contains(e) for e in elts)
            return False

        for f in st.fields.values():
            if contains(f.type):
                raise self.error(
                    f"@value class '{st.name}' can't contain itself (field '{f.name}'): it would be infinitely "
                    f"large. Remove @value to make it an ordinary class, or use a list", f.loc,
                )
            if reason := threads.not_a_value(f.type):
                fix = (f"store an id instead, remove @value to make {st.origin or st.name} an ordinary class, or make "
                       f"{f.type.name} a @value class" if isinstance(f.type, StructType)
                       else f"store something that can be copied instead (e.g. an int or a str), or remove @value to "
                            f"make {st.origin or st.name} an ordinary class")
                direct = threads.not_a_value(f.type) == reason and not isinstance(
                    f.type, (ListType, SetType, DictType, DequeType, TupleType, VarTupleType, OptionalType))
                raise self.error(
                    f"fields of a @value class must be values, but '{f.name}' {'is' if direct else 'holds'} {reason}. "
                    f"Copying {st.origin or st.name} would share it: {fix}", f.loc,
                )

    def resolve_signature(self, node: A.FunctionDef, owner: StructType | None, kind: str = "method") -> FuncInfo:
        if node.type_params and not self.type_env:
            raise self.error("generic methods are not supported yet (make the class generic instead)", node)
        params = list(node.params)
        if owner is not None and kind == "classmethod":
            if not params:
                raise self.error(f"classmethod '{node.name}' needs 'cls' as its first parameter", node)
            if params.pop(0).annotation is not None:
                raise self.error("'cls' doesn't need a type annotation", node)
        elif owner is not None and kind != "static":
            if not params or params[0].name != "self":
                raise self.error(f"method '{node.name}' needs 'self' as its first parameter", node)
            first = params.pop(0)
            if first.annotation is not None:
                raise self.error("'self' doesn't need a type annotation", first)
        resolved: list[Param] = []
        for p in params:
            if p.annotation is None:
                example = f"*{p.name}: str" if p.star else f"{p.name}: int"
                raise self.error(
                    f"parameter '{p.name}' needs a type annotation, e.g. `{example}`", p
                )
            t = self.resolve_type(p.annotation)
            resolved.append(Param(p.name, VarTupleType(t) if p.star else t, p.default, p.loc, p.star))
        ret = self.resolve_type(node.returns) if node.returns else NONE
        info = FuncInfo(node.name, resolved, ret, node, owner, module=self.module_name, kind=kind)
        if has_yield(node.body):
            if not isinstance(ret, GeneratorType):
                raise self.error(
                    f"'{node.name}' is a generator (it has 'yield'), so its return type is Iterator[T]: "
                    f"write `-> Iterator[int]` (with the type it yields)", node,
                )
            if owner is not None and threads.is_synchronized(owner):
                raise self.error("a Synchronized class's methods can't be generators (the lock can't be held across a yield)", node)
            info.generator = True
        if any(self.classify_decorator(d)[0] == "contextmanager" for d in node.decorators):
            self.check_context_generator(node, info)
        node.sym = info
        return info

    def check_context_generator(self, node: A.FunctionDef, info: FuncInfo) -> None:
        """@contextlib.contextmanager: a generator whose one `yield` gives the value for `with ... as`.
        Its call returns a ContextManager[T]; an exception in the with block is raised at the yield."""
        usage = f"`with {node.name}(...) as x:`"
        if len(node.decorators) > 1:
            raise self.error("@contextmanager can't be combined with other decorators yet", node.decorators[0])
        if not info.generator:
            raise self.error(
                f"'{node.name}' is a @contextmanager function, so it must `yield` (once) the value {usage} gives: "
                f"the code before the yield runs when the with block starts, the code after it when the block ends",
                node,
            )
        ContextYields(self).block(node.body, 0)
        info.ret = ContextManagerType(info.ret.elem)
        info.context_manager = True
        info.cm_suppresses = self.yield_may_be_caught(node.body, False)

    def yield_may_be_caught(self, body: list[A.Stmt], caught: bool) -> bool:
        """Could an exception raised at a yield in `body` be caught there (so a @contextmanager
        function swallows it)? Inside a try with an `except` that doesn't end in `raise`, or
        inside a `with` whose context manager may swallow it."""
        for s in body:
            match s:
                case A.Yield():
                    if caught:
                        return True
                case A.Try(body=b, handlers=handlers, orelse=orelse, finalbody=fin):
                    catches = caught or any(not (h.body and isinstance(h.body[-1], A.Raise)) for h in handlers)
                    if self.yield_may_be_caught(b, catches) or any(
                            self.yield_may_be_caught(x, caught) for x in [orelse, fin, *(h.body for h in handlers)]):
                        return True
                case A.With(items=items, body=b):
                    if self.yield_may_be_caught(b, caught or not all(self.never_swallows(i.context) for i in items)):
                        return True
                case A.If(body=b, orelse=orelse) | A.For(body=b, orelse=orelse) | A.While(body=b, orelse=orelse):
                    if self.yield_may_be_caught(b, caught) or self.yield_may_be_caught(orelse, caught):
                        return True
                case A.Match(cases=cases):
                    if any(self.yield_may_be_caught(c.body, caught) for c in cases):
                        return True
        return False

    def never_swallows(self, context: A.Expr) -> bool:
        """A with item that certainly lets exceptions through, judged from its syntax (function
        bodies aren't checked yet): open(), nullcontext(), closing(), or a @contextmanager
        function (defined earlier) that doesn't catch them."""
        if not isinstance(context, A.Call):
            return False
        f = context.func
        name = f.id if isinstance(f, A.Name) else f.attr if isinstance(f, A.Attribute) else None
        if name in ("open", "nullcontext", "closing"):
            return True
        info = self.functions.get(name) if isinstance(f, A.Name) else None
        return isinstance(info, FuncInfo) and info.context_manager and not info.cm_suppresses

    def check_param_defaults(self, info: FuncInfo) -> None:
        for p in info.params:
            if p.default is not None:
                self.check_constant_default(p.default, p.type, f"default for '{p.name}'")

    def check_field_defaults(self, st: StructType) -> None:
        for f in st.fields.values():
            if f.default is not None:
                self.check_constant_default(f.default, f.type, f"default for field '{f.name}'")

    def check_constant_default(self, expr: A.Expr, expected: Type, what: str) -> None:
        self.enter(FunctionScope(None, None, set(), []), State())
        t = self.check_expr(expr, expected)
        if not assignable(t, expected):
            raise self.error(f"{what} must be {expected}, not {t}", expr)

    def check_function_body(self, info: FuncInfo, outer: State | None = None) -> None:
        """Check a function's body. `outer` is the enclosing function's state for a nested def."""
        node: A.FunctionDef = info.node
        nonlocals, global_names = declared_names(node.body)
        if nonlocals and outer is None:
            raise self.error("'nonlocal' is only allowed in nested functions", next(iter(nonlocals.values())))
        declared = set(nonlocals) | set(global_names)
        self.frame_count += 1
        scope = FunctionScope(
            info, info.ret, assigned_names(node.body) - declared, info.locals,
            nonlocals=set(nonlocals), global_names=set(global_names), frame=self.frame_count,
        )
        state = State()
        param_names = {p.name for p in node.params}
        if outer is not None:
            scope.outer_assigned = self.scope.assigned | self.scope.outer_assigned
            for name, entry in outer.names.items():
                if name in scope.assigned or name in param_names:
                    continue
                # The closure may run after the variable changes, so narrowing doesn't carry in.
                state.names[name] = Bound(entry.var, entry.var.type) if isinstance(entry, Bound) else entry
        for name, stmt in nonlocals.items():
            entry = state.names.get(name)
            if not isinstance(entry, Bound) or entry.var.kind not in ("local", "param"):
                raise self.error(f"no variable '{name}' in an enclosing function for 'nonlocal' to use", stmt)
            entry.var.captured = True
        for name, stmt in global_names.items():
            var = self.globals.get(name)
            if var is None:
                if name in self.module_assign_counts:
                    raise self.error(
                        f"'global {name}' needs a module-level variable assigned exactly once; "
                        f"'{name}' is assigned {self.module_assign_counts[name]} times", stmt,
                    )
                raise self.error(f"no module-level variable '{name}' for 'global' to use", stmt)
            state.names[name] = Bound(var, var.type)
        has_receiver = info.owner is not None and info.kind != "static"
        if has_receiver and info.kind == "classmethod":
            first = node.params[0]
            cls_var = Var(first.name, first.name, ClassRefType(info.owner), "param", first.loc, frame=scope.frame)
            state.names[first.name] = Bound(cls_var, cls_var.type)
            first.sym = cls_var
        elif has_receiver:
            self_var = Var("self", "self", info.owner, "param", node.params[0].loc, frame=scope.frame)
            state.names["self"] = Bound(self_var, info.owner)
            node.params[0].sym = self_var
        param_nodes = node.params[1:] if has_receiver else node.params
        for p, pnode in zip(info.params, param_nodes):
            var = Var(p.name, p.name, p.type, "param", p.loc, frame=scope.frame)
            scope.vars[(p.name, p.type)] = var
            scope.cpp_names.add(p.name)
            state.names[p.name] = Bound(var, p.type)
            pnode.sym = var
        self.enter(scope, state)
        self.check_block(node.body)
        self.check_frozen_changes(node.body, info)
        self.check_dropped_changes(param_nodes, node.body)
        if not self.state.dead and info.ret != NONE and not isinstance(info.ret, OptionalType) and not info.generator:
            raise self.error(
                f"function '{info.name}' can reach its end without returning a value "
                f"(it's declared to return {info.ret})", node,
            )

    # =========================================================================
    # Generics: each distinct use is checked (and generated) separately, like a
    # C++ template, so a generic body can do anything its type arguments support.
    # =========================================================================

    def check_dropped_changes(self, params: list, body: list[A.Stmt], module: bool = False) -> None:
        """Changing a copy of a @value class (or of its list) and then never reading it is a
        mistake: the change is lost. (The module's own names are left alone if a function
        reads them.)"""
        for stmt, var, what in flow.dropped_changes(params, body):
            if module and any(flow.mentions(f.node.body, var) for f in self.all_functions()):
                continue
            if var.kind == "param":
                fix = f"Return it (and have the caller keep the result), or make {var.type.name} an ordinary class"
            elif what.startswith("a copy of an item"):
                source = what.split("a copy of an item of ", 1)[1].split(" (")[0]
                fix = f"Loop over the indexes and change {source}[i] instead, or write the copy back"
            else:
                source = what.split("a copy of ", 1)[1]
                fix = f"Write it back (`{source} = {var.name}`), or change {source} in place"
            raise self.error(f"this changes '{var.name}', {what}, and then never uses it. {fix}", stmt)

    def set_class_kind(self, node: A.ClassDef) -> None:
        """`@value class Point:` (from seadash import value) is a value type, kind 'struct';
        any other class is a shared reference."""
        kinds = [self.classify_decorator(d)[0] for d in node.decorators]
        enum_base = self.enum_base(node)
        for kind, d in zip(kinds, node.decorators):
            if enum_base is not None and kind != "unique":
                raise self.error(f"an enum can only be decorated with @enum.unique, not @{kind}", d)
            if kind == "unique" and enum_base is None:
                raise self.error("@unique is for enums (`class Color(Enum):`)", d)
            if kind not in ("dataclass", "value", "total_ordering", "unique"):
                raise self.error("only @dataclass, @value and @functools.total_ordering can decorate a class (for now)", d)
        # An enum's members are immutable values: a frozen value class, as far as copying goes.
        node.kind = "struct" if "value" in kinds or enum_base is not None else "class"

    def enum_base(self, node: A.ClassDef) -> str | None:
        """'Enum', 'IntEnum'... if the class inherits one of the enum module's classes."""
        if not node.bases or not isinstance(node.bases[0], A.TypeName) or node.bases[0].args:
            return None
        base = self.module_member_named(node.bases[0].name)
        return base.name if isinstance(base, builtins.EnumBase) else None

    def declare_generic(self, node: A.FunctionDef | A.ClassDef) -> None:
        if node.name in self.structs or node.name in self.functions or node.name in self.generics:
            raise self.error(f"'{node.name}' is already defined", node)
        if isinstance(node, A.ClassDef):
            self.set_class_kind(node)
        if len(set(node.type_params)) != len(node.type_params):
            raise self.error("duplicate type parameter", node)
        kind = "func" if isinstance(node, A.FunctionDef) else "class"
        self.generics[node.name] = GenericDef(kind, node.name, node, self)

    def generic_named(self, e: A.Expr) -> GenericDef | None:
        """The generic function/class a call's callee refers to, if any."""
        if isinstance(e, A.Name) and e.id not in self.state.names and e.id not in self.scope.assigned:
            if e.id in self.generics:
                return self.generics[e.id]
            if e.id in self.imported:
                mod, member = self.imported[e.id]
                m = mod.members.get(member)
                return m if isinstance(m, GenericDef) else None
        if isinstance(e, A.Attribute) and isinstance(e.value, A.Name) and e.value.id in self.modules:
            m = self.modules[e.value.id].members.get(e.attr)
            return m if isinstance(m, GenericDef) else None
        return None

    def call_generic(self, e: A.Call, gen: GenericDef, explicit: tuple | None, expected: Type | None) -> Type:
        """first(xs) / first[int](xs) / Stack[int]() / Box(5): infer or take the type
        arguments, instantiate, then check the call like any other."""
        owner = gen.checker
        tvars = gen.node.type_params
        if explicit is not None:
            if len(explicit) != len(tvars):
                raise self.error(f"{gen.name} takes {plural(len(tvars), 'type argument')}, not {len(explicit)}", e.func)
            type_args = explicit
        else:
            type_args = self.infer_type_args(e, gen, expected)
        if gen.kind == "func":
            info = owner.instantiate_function(gen, type_args, e)
            e.sym = CallTarget("func", info, self.match_args(e, info.params, f"{gen.name}()"))
            return info.ret
        return self.check_constructor(e, owner.instantiate_class(gen, type_args, e))

    def infer_type_args(self, e: A.Call, gen: GenericDef, expected: Type | None) -> tuple:
        owner = gen.checker
        tvars = set(gen.node.type_params)
        params = generic_params(gen)
        if expected is not None:
            want = strip_optional(expected)
            if gen.kind == "class" and isinstance(want, StructType) and want.origin == gen.name:
                return want.type_args  # s: Stack[int] = Stack()
        by_name: dict[str, A.Expr] = {}
        for i, arg in enumerate(e.args):
            if i < len(params):
                by_name[params[i][0]] = arg
        for kw in e.keywords:
            by_name[kw.name] = kw.value
        env: dict[str, Type] = {}
        later = []
        for pname, annotation in params:
            arg = by_name.get(pname)
            if arg is None or annotation is None:
                continue
            if mentions(annotation, tvars) and (isinstance(arg, A.Lambda) or needs_context(arg)):
                later.append((annotation, arg))  # needs T first: a lambda, or [] / None
                continue
            hint = None if mentions(annotation, tvars) else owner.resolve_in_env(annotation, {})
            owner.unify(annotation, self.check_expr(arg, hint), tvars, env)
        if expected is not None and gen.kind == "func" and gen.node.returns is not None:
            owner.unify(gen.node.returns, expected, tvars, env, only_missing=True)
        for annotation, arg in later:
            hint = owner.partial_type(annotation, env, tvars)
            owner.unify(annotation, self.check_expr(arg, hint), tvars, env)
        missing = [v for v in gen.node.type_params if v not in env]
        if missing:
            example = ", ".join("int" for _ in gen.node.type_params)
            raise self.error(
                f"can't tell what {' and '.join(missing)} should be for {gen.name}; "
                f"write the types, e.g. {gen.name}[{example}](...)", e,
            )
        return tuple(env[v] for v in gen.node.type_params)

    def unify(self, annotation: A.TypeExpr, actual: Type, tvars: set[str], env: dict, only_missing: bool = False) -> None:
        """Learn type variables by matching an annotation against an actual type."""
        match annotation:
            case A.TypeName(name, []) if name in tvars:
                if actual in (NONE, UNKNOWN) or contains_unknown(actual):
                    return
                if name not in env:
                    env[name] = actual
                elif not only_missing:
                    env[name] = join(env[name], actual) or env[name]  # a mismatch is reported by the call check
            case A.OptionalType(inner):
                if actual != NONE:
                    self.unify(inner, strip_optional(actual), tvars, env, only_missing)
            case A.FuncTypeExpr(params, ret) if isinstance(actual, FuncType):
                for p, t in zip(params, actual.params):
                    self.unify(p, t, tvars, env, only_missing)
                if actual.ret is not None:
                    self.unify(ret, actual.ret, tvars, env, only_missing)
            case A.TypeName(name, args) if args:
                inner: tuple = ()
                match actual:
                    case ListType(x) | SetType(x) if name in ("list", "set", "List", "Set"):
                        inner = (x,)
                    case DictType(k, v) if name in ("dict", "Dict"):
                        inner = (k, v)
                    case TupleType(xs) if name in ("tuple", "Tuple"):
                        inner = xs
                    case StructType() if actual.origin == name.rpartition(".")[2]:
                        inner = actual.type_args
                for a, t in zip(args, inner):
                    self.unify(a, t, tvars, env, only_missing)

    def resolve_in_env(self, annotation: A.TypeExpr, env: dict) -> Type:
        saved, self.type_env = self.type_env, env
        try:
            return self.resolve_type(annotation)
        finally:
            self.type_env = saved

    def partial_type(self, annotation: A.TypeExpr, env: dict, tvars: set[str]) -> Type | None:
        """A hint for a lambda before all type variables are known: `(T) -> U` with T known
        is the function type (T) -> ? (the lambda's body decides U)."""
        if not mentions(annotation, tvars - set(env)):
            return self.resolve_in_env(annotation, env)
        if isinstance(annotation, A.FuncTypeExpr):
            params = [self.partial_type(p, env, tvars) for p in annotation.params]
            if all(p is not None for p in params):
                return FuncType(tuple(params), self.partial_type(annotation.ret, env, tvars))
        return None

    def instantiate_function(self, gen: GenericDef, type_args: tuple, node: A.Node) -> FuncInfo:
        key = (gen.name, type_args)
        if key in self.instances:
            return self.instances[key]
        self.check_instantiable(gen, type_args, node)
        env = dict(zip(gen.node.type_params, type_args))
        copy = deepcopy(gen.node)
        info = self.resolve_in_context(env, lambda: self.resolve_signature(copy, owner=None))
        display = f"{gen.name}[{', '.join(map(str, type_args))}]"
        info.cpp_name = mangle(gen.name, type_args)
        self.instances[key] = info
        self.out_functions.append(info)
        self.resolve_in_context(env, lambda: self.check_param_defaults(info))
        self.pending.append((display, env, [info]))
        self.check_pending_instances()
        return info

    def instantiate_class(self, gen: GenericDef, type_args: tuple, node: A.Node) -> StructType:
        key = (gen.name, type_args)
        if key in self.instances:
            return self.instances[key]
        if len(type_args) != len(gen.node.type_params):
            raise self.error(f"{gen.name} takes {plural(len(gen.node.type_params), 'type argument')}", node)
        self.check_instantiable(gen, type_args, node)
        env = dict(zip(gen.node.type_params, type_args))
        copy = deepcopy(gen.node)
        display = f"{gen.name}[{', '.join(map(str, type_args))}]"
        st = StructType(display, copy.kind, copy, module=self.module_name,
                        mangled=mangle(gen.name, type_args), origin=gen.name, type_args=type_args)
        copy.sym = st
        self.instances[key] = st  # before resolving members: fields may mention Stack[T] itself
        self.out_structs.append(st)

        def resolve() -> None:
            self.resolve_base(st)
            self.resolve_struct_members(st)
            self.check_overrides(st)
            self.check_value_recursion(st)
            self.check_field_defaults(st)
            for m in st.methods.values():
                self.check_param_defaults(m)

        self.resolve_in_context(env, resolve)
        self.pending.append((display, env, list(st.methods.values())))
        self.check_pending_instances()
        return st

    def check_instantiable(self, gen: GenericDef, type_args: tuple, node: A.Node) -> None:
        """For now, a generic from another module can only be used with types that module can see."""
        allowed = self.visible_modules()
        for t in type_args:
            for st in structs_in(t):
                if not st.builtin and st.module not in allowed:
                    raise CheckError(
                        f"{gen.name} (from module '{self.module_name}') can't be used with {st.name} "
                        f"from module '{st.module}' yet", node.loc,
                    )

    def visible_modules(self) -> set[str]:
        seen = {self.module_name}
        todo = [m for m in [*self.modules.values(), *(m for m, _ in self.imported.values())]]
        while todo:
            m = todo.pop()
            if isinstance(m, builtins.UserModule) and m.name not in seen:
                seen.add(m.name)
                if m.info is not None:
                    todo.extend(m.info.imports)
                todo.extend(x for x in m.members.values() if isinstance(x, builtins.UserModule))
        return seen

    def resolve_in_context(self, env: dict, action):
        """Run `action` with type variables bound and the checker's own context saved."""
        saved = (self.scope, self.state, self.loops, self.handler_depth, self.finally_loops,
                 self.lambda_depth, self.frame, self.type_env)
        self.type_env = env
        try:
            return action()
        finally:
            (self.scope, self.state, self.loops, self.handler_depth, self.finally_loops,
             self.lambda_depth, self.frame, self.type_env) = saved

    def check_pending_instances(self) -> None:
        """Check generic instance bodies. They wait until the module's own code is checked
        (a body may use module globals, which get their types from the top-level code)."""
        if not self.module_checked:
            return
        while self.pending:
            display, env, functions = self.pending.pop(0)
            for info in functions:
                try:
                    self.resolve_in_context(env, lambda: self.check_function_body(info))
                except CheckError as err:
                    if not err.message.startswith("in "):
                        err.message = f"in {display}: {err.message}"
                    raise

    def check_nested_def(self, node: A.FunctionDef) -> None:
        """A def inside a function: a local variable holding a closure."""
        if has_yield(node.body):
            raise self.error("nested functions can't be generators yet; move it to the top level", node)
        if node.type_params:
            raise self.error("generic functions are not supported yet", node)
        for d in node.decorators:  # (only @functools.wraps(f), which copies f's name and docstring: nothing to do)
            if self.classify_decorator(d)[0] != "wraps":
                raise self.error("decorators on nested functions aren't supported yet (except @functools.wraps)", d)
            if not (isinstance(d, A.Call) and len(d.args) == 1 and not d.keywords):
                raise self.error("@wraps takes the function being wrapped: @wraps(f)", d)
            if not isinstance(self.check_expr(d.args[0]), FuncType):
                raise self.error(f"@wraps takes the function being wrapped, not {d.args[0].ty}", d.args[0])
        info = self.resolve_signature(node, owner=None)
        for p in info.params:
            if p.default is not None:
                raise self.error(f"default values aren't supported in nested functions yet ('{p.name}')", p.default)
        name = A.Name(node.name, loc=node.loc)
        self.bind(name, FuncType(tuple(p.type for p in info.params), info.ret), node)
        info.var = name.sym
        saved = (self.scope, self.state, self.loops, self.handler_depth, self.finally_loops, self.lambda_depth, self.frame)
        try:
            self.check_function_body(info, outer=self.state)
        finally:
            (self.scope, self.state, self.loops, self.handler_depth,
             self.finally_loops, self.lambda_depth, self.frame) = saved

    def note_capture(self, var: Var) -> None:
        """A variable used from a different function/lambda body is shared with it through a cell."""
        if var.kind in ("local", "param") and var.frame != self.frame and var.name != "self":
            var.captured = True

    def enter(self, scope: FunctionScope, state: State) -> None:
        self.scope = scope
        self.state = state
        self.loops = []
        self.handler_depth = 0
        self.finally_loops = []
        self.lambda_depth = 0
        self.frame = scope.frame

    # =========================================================================
    # Type annotations
    # =========================================================================

    def resolve_type(self, t: A.TypeExpr) -> Type:
        match t:
            case A.OptionalType(inner):
                resolved = self.resolve_type(inner)
                if resolved == NONE:
                    raise self.error("'None?' is not a meaningful type", t)
                return resolved if isinstance(resolved, OptionalType) else OptionalType(resolved)
            case A.UnionType(options) if len(options) == 2 and any(is_none_type(o) for o in options):
                other = next(o for o in options if not is_none_type(o))  # `T | None` is T?
                return self.resolve_type(A.OptionalType(other, loc=t.loc))
            case A.UnionType():
                raise self.error("union types are not supported yet (T? or `T | None` for 'T or None' is)", t)
            case A.FuncTypeExpr(params, ret):
                return FuncType(tuple(self.resolve_type(p) for p in params), self.resolve_type(ret))
            case A.TypeName(name, args):
                return self.resolve_type_name(t, name, args)
        raise self.error("invalid type", t)

    def resolve_type_name(self, node: A.TypeName, name: str, args: list[A.TypeExpr]) -> Type:
        if name in self.type_env and not args:  # T inside a generic
            return self.type_env[name]
        if (kind := self.sync_kind_named(name)) is not None:  # threading.Lock, Queue[int]
            if len(args) != SYNC_ARITY[kind]:
                want = f"{kind}[T]" if SYNC_ARITY[kind] else kind
                raise self.error(f"{kind} takes {plural(SYNC_ARITY[kind], 'type argument')}: write {want}", node)
            t = SyncType(kind, tuple(self.resolve_type(a) for a in args))
            self.check_sync_contents(t, node)
            return t
        if self.module_member_named(name) is builtins.FUTURE_MARKER:  # Future[int]
            if len(args) != 1:
                raise self.error("Future takes one type argument, e.g. Future[int]", node)
            return FutureType(self.resolve_type(args[0]))
        if (kind := self.collection_kind_named(name)) is not None:  # deque[int], defaultdict[str, int]
            return self.collection_type(kind, [self.resolve_type(a) for a in args], node)
        gen = self.generics.get(name)
        if gen is None and name in self.imported:
            mod, member = self.imported[name]
            gen = mod.members.get(member) if isinstance(mod.members.get(member), GenericDef) else None
        if gen is None and "." in name:
            mod_name, _, member = name.rpartition(".")
            mod = self.modules.get(mod_name)
            if mod is not None and isinstance(mod.members.get(member), GenericDef):
                gen = mod.members[member]
        if gen is not None:
            if gen.kind != "class":
                raise self.error(f"'{name}' is a generic function, not a type", node)
            if not args:
                params = ", ".join(gen.node.type_params)
                raise self.error(f"'{name}' needs type arguments: {name}[{params}]", node)
            return gen.checker.instantiate_class(gen, tuple(self.resolve_type(a) for a in args), node)
        if name in ("TextIO", "BinaryIO") and not args:
            return BINARY_FILE if name == "BinaryIO" else TEXT_FILE
        if name in ("Iterator", "Iterable", "Generator") and args:  # typing.Iterator[T] / Generator[T, None, None]
            if name != "Generator" and len(args) != 1 or name == "Generator" and (
                len(args) != 3 or any(self.resolve_type(a) != NONE for a in args[1:])
            ):
                raise self.error(f"write Iterator[T] (or Generator[T, None, None]: send() isn't supported)", node)
            return GeneratorType(self.resolve_type(args[0]))
        if name.rpartition(".")[2] in ("ContextManager", "AbstractContextManager"):  # contextlib's (not classes)
            if len(args) != 1:
                raise self.error(f"write {name}[T], with the type `with ... as x:` gives", node)
            return ContextManagerType(self.resolve_type(args[0]))
        if name == "Optional" and len(args) == 1:  # typing.Optional[T] is T?
            inner = self.resolve_type(args[0])
            return inner if isinstance(inner, OptionalType) else OptionalType(inner)
        if name in TYPING_ALIASES:  # typing.List[int] is list[int]
            name = TYPING_ALIASES[name]
        if name in self.imported and not args:  # `from json import Value`
            mod, member = self.imported[name]
            m = mod.members.get(member)
            if isinstance(m, builtins.NamedType):
                return m.type
            if isinstance(m, builtins.Function) and m.as_type is not None:  # `from socket import socket`
                return m.as_type
        if name in PRIMITIVES or self.lookup_struct(name):
            if args:
                raise self.error(f"'{name}' doesn't take type arguments", node)
            return PRIMITIVES.get(name) or self.lookup_struct(name)
        if name in builtins.CONTAINER_TYPES:
            arity = builtins.CONTAINER_TYPES[name]
            example = {"list": "list[int]", "set": "set[int]", "dict": "dict[str, int]", "tuple": "tuple[int, str]"}[name]
            if not args or (arity is not None and len(args) != arity):
                raise self.error(f"'{name}' needs {arity or 'some'} type argument(s), e.g. {example}", node)
            dots = [isinstance(a, A.TypeName) and a.name == "..." for a in args]
            if name == "tuple" and dots == [False, True]:  # tuple[int, ...]
                return VarTupleType(self.resolve_type(args[0]))
            if any(dots):
                raise self.error("'...' only goes in tuple[T, ...] (a tuple of any length)", node)
            resolved = [self.resolve_type(a) for a in args]
            match name:
                case "list":
                    return ListType(resolved[0])
                case "set":
                    self.check_hashable(resolved[0], "set elements", args[0])
                    return SetType(resolved[0])
                case "dict":
                    self.check_hashable(resolved[0], "dict keys", args[0])
                    return DictType(resolved[0], resolved[1])
                case "tuple":
                    return TupleType(tuple(resolved))
        if "." in name:
            m = self.module_member_named(name)  # (also http.client.HTTPResponse, from a submodule)
            if isinstance(m, StructType):
                return m
            if isinstance(m, builtins.NamedType):
                return m.type
            if isinstance(m, builtins.Function) and m.as_type is not None:  # socket.socket
                return m.as_type
            mod_name, _, member = name.rpartition(".")
            if (m is None and member in builtins.SEADASH_THREAD_TYPES and isinstance(mod := self.modules.get(mod_name), builtins.Module)
                    and mod.name == "threading"):
                raise self.error(builtins.missing_member(mod, member), node)  # threading.Mutex: it's seadash's
            raise self.error(f"unknown type '{name}'", node)
        raise self.error(f"unknown type '{name}'", node)

    def check_hashable(self, t: Type, what: str, node: A.Node) -> None:
        if not is_hashable(t):
            raise self.error(f"{what} must be int, float, str, bool, or a tuple of those; not {t}", node)

    # =========================================================================
    # Statements
    # =========================================================================

    def check_block(self, stmts: list[A.Stmt]) -> None:
        for stmt in stmts:
            self.check_stmt(stmt)

    def check_stmt(self, stmt: A.Stmt) -> None:
        match stmt:
            case A.ExprStmt(value):
                self.check_expr(value)
            case A.Assign(targets, value):
                expected = self.expected_for_target(targets[0])
                t = self.check_expr(value, expected)
                for target in targets:
                    self.assign(target, t, value)
                self.name_the_mutex(targets[0], value)
            case A.AnnAssign(target, annotation, value):
                self.check_ann_assign(stmt, target, annotation, value)
            case A.AugAssign(target, op, value):
                self.check_aug_assign(stmt, target, op, value)
            case A.Pass():
                pass
            case A.Break() | A.Continue():
                word = "break" if isinstance(stmt, A.Break) else "continue"
                if not self.loops:
                    raise self.error(f"'{word}' outside a loop", stmt)
                if self.finally_loops and self.finally_loops[-1] == len(self.loops):
                    raise self.error(f"'{word}' can't leave a 'finally' block", stmt)
                loop = self.loops[-1]
                (loop.breaks if isinstance(stmt, A.Break) else loop.continues).append(self.state.copy())
                self.state.dead = True
            case A.Return(value):
                self.check_return(stmt, value)
            case A.Yield(value, from_):
                self.check_yield(stmt, value, from_)
            case A.Assert(test, msg):
                self.state, _ = self.check_condition(test)  # after `assert x`, x is known not None
                if msg is not None:
                    self.check_expr(msg)
            case A.Raise(exc, cause):
                self.check_raise(stmt, exc, cause)
            case A.Try():
                self.check_try(stmt)
            case A.With():
                self.check_with(stmt)
            case A.If():
                self.check_if(stmt)
            case A.Match():
                self.check_match(stmt)
            case A.While():
                self.check_while(stmt)
            case A.For():
                self.check_for(stmt)
            case A.FunctionDef():
                self.check_nested_def(stmt)
            case A.Nonlocal():
                if self.scope.is_module or self.scope.info.var is None:
                    raise self.error("'nonlocal' is only allowed in nested functions", stmt)
            case A.Global():
                pass  # handled when the function body starts
            case A.ClassDef():
                raise self.error("a class can only be defined at the top level of a module", stmt)
            case A.Import() | A.ImportFrom():
                raise self.error("imports must be at the top level of a module", stmt)
            case _:
                raise self.error(f"unsupported statement {type(stmt).__name__}", stmt)

    def expected_for_target(self, target: A.Expr) -> Type | None:
        """The type a target currently holds, as a hint for `xs = []` style values."""
        match target:
            case A.Name(name):
                entry = self.state.names.get(name)
                return entry.var.type if isinstance(entry, (Bound, Moved)) else None
            case A.Attribute(value, attr):
                owner = self.peek_type(value)
                if isinstance(owner, StructType) and (f := owner.find_field(attr)):
                    return f.type
            case A.Index(value):
                match self.peek_type(value):
                    case DictType(_, val):
                        return val
                    case ListType(elem):
                        return elem
        return None

    def peek_type(self, e: A.Expr) -> Type | None:
        """The type of a simple name (no side effects, no errors)."""
        if isinstance(e, A.Name):
            entry = self.state.names.get(e.id)
            return entry.ty if isinstance(entry, Bound) else None
        return None

    def check_ann_assign(self, stmt: A.AnnAssign, target: A.Expr, annotation: A.TypeExpr, value: A.Expr | None) -> None:
        declared = self.resolve_type(annotation)
        if not isinstance(target, A.Name):
            raise self.error("only plain variables can have type annotations here", target)
        if value is None:
            raise self.error(
                f"variable '{target.id}' needs a value, e.g. `{target.id}: {declared} = ...`", stmt
            )
        t = self.check_expr(value, declared)
        if not assignable(t, declared):
            raise self.error(f"'{target.id}' is declared as {declared}, but the value is {t}", value)
        self.bind(target, declared, value, exact=True)

    def check_aug_assign(self, stmt: A.AugAssign, target: A.Expr, op: str, value: A.Expr) -> None:
        # `x += v` is checked as `x = x + v`, so for plain names it may rebind
        # (`n = 1; n += 0.5` makes n a float), like Python.
        current = self.check_expr(target)
        read_sym = target.sym
        if isinstance(target, A.Attribute) and isinstance(read_sym, tuple) and read_sym[0] in ("enum_attr", "enum_member"):
            self.assign(target, current, value)  # (the error for changing an enum)
        vt = self.check_expr(value)
        result = self.binop_type(op, current, vt, stmt)
        if isinstance(target, A.Name):
            self.bind(target, result, stmt)
        elif not assignable(result, current):
            raise self.error(f"'{op}=' would change this {current} into a {result}", stmt)
        elif isinstance(target, A.Index) and isinstance(target.dunder, Dunder):
            owner = target.value.ty
            if not self.dunder(owner, "__setitem__"):
                raise self.error(f"{owner.name} doesn't support item assignment (define __setitem__)", target)
        elif isinstance(target, A.Attribute) and isinstance(target.sym, Field):
            self.check_not_frozen(target.value.ty, target.attr, target)
            self.note_attr_assignment(target, target.sym.type, result)
        elif isinstance(target, A.Attribute) and isinstance(target.sym, tuple) and target.sym[0] == "property":
            self.property_setter(target.value.ty, target.attr, target)  # obj.count += 1 needs a setter
        # For codegen: what was read (and its type there) and the operation's
        # result type. target.sym is the variable written, which may differ.
        stmt.sym = (read_sym, current, result)

    def assign(self, target: A.Expr, t: Type, value: A.Expr) -> None:
        match target:
            case A.Name():
                self.bind(target, t, value)
            case A.Attribute(obj, attr):
                if (isinstance(obj, A.Name) and obj.id not in self.state.names and (cls := self.lookup_struct(obj.id))
                        and cls.find_class_attr(attr) is not None):
                    raise self.error(f"{cls.name}.{attr} is a class attribute, a constant: it can't be changed", target)
                owner = self.check_expr(obj)
                if isinstance(owner, ClassRefType) and owner.st.enum is not None:
                    what = "reassign member" if attr in owner.st.enum.members else "add a member"
                    raise self.error(f"cannot {what} '{attr}': an enum's members are fixed when it's defined", target)
                if isinstance(owner, StructType) and owner.enum is not None:
                    raise self.error(f"{owner.name} is an enum: its members can't be changed (cannot set attribute "
                                     f"'{attr}')", target)
                if not isinstance(owner, StructType):
                    raise self.error(f"can't set attribute '{attr}' on {owner}", target)
                if (getter := owner.find_method(attr)) and getter.lazy:  # obj.cached = v: replaces the kept value
                    if not assignable(t, getter.ret):
                        raise self.error(f"cached property '{attr}' is {getter.ret}, can't assign {t}", value)
                    target.sym = ("cached_set", getter)
                    target.ty = getter.ret
                    return
                if (getter := owner.find_method(attr)) and getter.kind == "getter":  # obj.area = v
                    setter = self.property_setter(owner, attr, target)
                    if not assignable(t, setter.params[0].type):
                        raise self.error(f"property '{attr}' takes {setter.params[0].type}, not {t}", value)
                    target.sym = ("property_set", setter)
                    target.ty = setter.params[0].type
                    return
                self.check_not_frozen(owner, attr, target)
                f = owner.find_field(attr)
                if f is None and owner.find_class_attr(attr) is not None:
                    raise self.error(
                        f"'{attr}' is a class attribute of {owner.name}, a constant, so it can't be set on an object "
                        f"(Python would give this object its own '{attr}'). Make it a field to change it per object: "
                        f"`{attr}: {owner.find_class_attr(attr).type} = ...`", target,
                    )
                if f is None:
                    raise self.error(f"{owner.name} has no field '{attr}'", target)
                self.check_synchronized_access(owner, obj, attr, target)
                if not assignable(t, f.type):
                    raise self.error(f"field '{attr}' is {f.type}, can't assign {t}", value)
                target.ty = f.type
                target.sym = f
                self.note_attr_assignment(target, f.type, t)
            case A.Index(container, index):
                ct = self.check_expr(container)
                if m := self.dunder(ct, "__setitem__"):  # obj[k] = v -> obj.__setitem__(k, v)
                    self.expect_type(index, m.params[0].type, f"{ct.name} index")
                    if not assignable(t, m.params[1].type):
                        raise self.error(f"{ct.name}.__setitem__ takes {m.params[1].type}, not {t}", value)
                    target.dunder = Dunder(m)
                    target.ty = m.params[1].type
                    return
                if isinstance(ct, StructType):
                    raise self.error(f"{ct.name} doesn't support item assignment (define __setitem__)", target)
                match ct:
                    case ListType(elem):
                        if isinstance(index, A.Slice):
                            raise self.error("assigning to a slice is not supported yet", index)
                        self.expect_type(index, INT, "list index")
                        slot = elem
                    case DequeType(elem):
                        self.expect_type(index, INT, "deque index")
                        slot = elem
                    case DictType(key, val):
                        self.expect_type(index, key, "dict key")
                        slot = val
                    case _ if ct in (STR, BYTES) or isinstance(ct, TupleType):
                        raise self.error(f"{ct} can't be changed in place (it's immutable)", target)
                    case _:
                        raise self.error(f"{ct} doesn't support item assignment", target)
                if not assignable(t, slot):
                    raise self.error(f"can't store {t} in a {ct}", value)
                target.ty = slot
            case A.TupleLit(elts) | A.ListLit(elts) if isinstance(t, VarTupleType):
                for elt in elts:  # checked when it runs: the lengths must match
                    self.assign(elt, t.elem, value)
                target.ty = t
            case A.TupleLit(elts) | A.ListLit(elts):
                if not isinstance(t, TupleType):
                    raise self.error(f"can only unpack a tuple here, not {t}", value)
                if len(t.elts) != len(elts):
                    raise self.error(f"can't unpack {len(t.elts)} values into {len(elts)} names", target)
                for elt, et in zip(elts, t.elts):
                    self.assign(elt, et, value)
                target.ty = t
            case _:
                raise self.error("can't assign to this", target)

    def bind(self, name: A.Name, t: Type, value: A.Node, exact: bool = False) -> None:
        """Point `name` at a variable holding type `t`, reusing the current one if `t` fits.

        With exact=True (annotated assignment) the variable's type is exactly `t`.
        """
        entry = self.state.names.get(name.id)
        if name.id in self.scope.nonlocals or name.id in self.scope.global_names:
            kind = "nonlocal" if name.id in self.scope.nonlocals else "global"
            if exact or not assignable(t, entry.var.type):
                raise self.error(f"can't change the type of {kind} '{name.id}' from {entry.var.type} to {t}", value)
            var = entry.var
            self.note_capture(var)
        elif not exact and isinstance(entry, Bound) and assignable(t, entry.var.type):
            var = entry.var
        else:
            if t == NONE:
                if isinstance(value, A.Call):
                    raise self.error("this call doesn't return a value", value)
                raise self.error(
                    f"can't tell what type '{name.id}' should be from None alone; "
                    f"annotate it, e.g. `{name.id}: int? = None`", value,
                )
            if isinstance(t, IterType):
                raise self.error(
                    f"can't store {t.kind}(...) in a variable; loop over it directly, "
                    f"or make a list with list(...)", value,
                )
            if isinstance(t, ClassRefType):
                raise self.error("a class can't be stored in a variable yet; call it, or call its class methods", value)
            if isinstance(t, ModuleType):
                raise self.error(f"a module can't be stored in a variable; use `import ... as name` to rename it", value)
            var = self.variable(name.id, t, name.loc)
        view = var.type
        if isinstance(var.type, OptionalType) and t != NONE and not isinstance(t, OptionalType):
            view = var.type.inner  # just assigned a real value: known not None
        self.state.names[name.id] = Bound(var, view)
        self.state.forget_attrs((name.id,))
        name.sym = var
        name.ty = var.type

    def note_attr_assignment(self, target: A.Attribute, field_type: Type, value_type: Type) -> None:
        """After `u.address = x`, forget narrowings under u.address; if x is definitely
        not None, u.address is now narrowed (like a plain variable)."""
        path = attr_path(target)
        if path is None:
            return
        self.state.forget_attrs(path)
        if isinstance(field_type, OptionalType) and value_type != NONE and not isinstance(value_type, OptionalType):
            self.state.attrs[path] = field_type.inner

    def variable(self, name: str, t: Type, loc: Loc) -> Var:
        scope = self.scope
        key = (name, t)
        if key in scope.vars:
            return scope.vars[key]
        cpp_name = name
        n = 0
        while cpp_name in scope.cpp_names:
            n += 1
            cpp_name = f"{name}_{n}"
        kind = "local"
        if scope.is_module and self.module_assign_counts.get(name) == 1:
            kind = "global"
        var = Var(name, cpp_name, t, kind, loc, frame=self.frame, module=self.module_name)
        scope.vars[key] = var
        scope.cpp_names.add(cpp_name)
        scope.locals.append(var)
        if kind == "global":
            self.globals[name] = var
        return var

    def check_yield(self, stmt: A.Yield, value: A.Expr | None, from_: bool) -> None:
        scope = self.scope
        if scope.is_module:
            raise self.error("'yield' outside a function", stmt)
        info = scope.info
        if not info.generator:  # a nested def or lambda
            raise self.error("only top-level functions and methods can be generators (for now)", stmt)
        elem = info.ret.elem
        if self.handler_depth or self.finally_loops:  # (C++ can't suspend a coroutine in a catch block or a lambda)
            where = "an 'except'" if self.handler_depth else "a 'finally'"
            raise self.error(f"a generator can't yield inside {where} block yet; set a flag there and yield after "
                             f"the try statement", stmt)
        if from_ and info.context_manager:
            raise self.error("a @contextmanager function yields once: `yield from` isn't supported there", stmt)
        if from_:
            got = self.loop_element(value, self.check_expr(value, ListType(elem)))
            if not assignable(got, elem):
                raise self.error(f"'{info.name}' yields {elem}, but this gives {got}", value)
            return
        if value is None:
            if not assignable(NONE, elem):
                raise self.error(f"a bare `yield` gives None, but '{info.name}' yields {elem}", stmt)
            return
        got = self.check_expr(value, elem)
        if not assignable(got, elem):
            raise self.error(f"'{info.name}' yields {elem}, not {got}", value)

    def check_return(self, stmt: A.Return, value: A.Expr | None) -> None:
        scope = self.scope
        if scope.is_module:
            raise self.error("'return' outside a function", stmt)
        if self.finally_loops:
            raise self.error("'return' can't be used inside a 'finally' block", stmt)
        name = scope.info.name
        if scope.info.generator:
            if value is not None:
                raise self.error("a generator can only `return` without a value (to finish early)", value)
            self.state.dead = True
            return
        if value is None:
            if scope.ret != NONE and not isinstance(scope.ret, OptionalType):
                raise self.error(f"'{name}' must return a {scope.ret}", stmt)
        else:
            t = self.check_expr(value, scope.ret)
            if scope.ret == NONE and t != NONE:
                raise self.error(
                    f"'{name}' returns a value but has no return type; add `-> {t}` to its definition", value
                )
            if not assignable(t, scope.ret):
                raise self.error(f"'{name}' should return {scope.ret}, not {t}", value)
        self.state.dead = True

    def check_raise(self, stmt: A.Raise, exc: A.Expr | None, cause: A.Expr | None) -> None:
        if exc is None:
            if not self.handler_depth:
                raise self.error("a bare 'raise' can only re-raise inside an 'except' block", stmt)
        elif isinstance(exc, A.Name) and exc.id not in self.state.names and (st := self.lookup_struct(exc.id)):
            # `raise ValueError` is short for `raise ValueError()`
            if not st.is_exception:
                raise self.error(f"can only raise exceptions, not {st.name}", exc)
            if any(p.default is None for p in self.constructor_params(st, exc)):
                raise self.error(f"{st.name} needs arguments: `raise {st.name}(...)`", exc)
            stmt.sym = st
        else:
            t = self.check_expr(exc)
            if not (isinstance(t, StructType) and t.is_exception):
                raise self.error(f"can only raise exceptions, not {t}", exc)
        if cause is not None:
            t = self.check_expr(cause)
            if not (isinstance(t, StructType) and t.is_exception):
                raise self.error(f"'raise ... from' needs an exception, not {t}", cause)
        self.state.dead = True

    def check_try(self, stmt: A.Try) -> None:
        """Handlers can start from any point in the try body (it may raise anywhere),
        so they see the merge of the states before and after each body statement."""
        snapshots = [self.state.copy()]
        for s in stmt.body:
            self.check_stmt(s)
            snapshots.append(self.state.copy())
        raised = merge(snapshots)
        raised.dead = False
        self.check_block(stmt.orelse)
        exits = [self.state]

        for handler in stmt.handlers:
            self.state = raised.copy()
            caught = self.check_handler_types(handler)
            if handler.name is not None:
                self.bind(handler.name, caught, handler)
            self.handler_depth += 1
            self.check_block(handler.body)
            self.handler_depth -= 1
            if handler.name is not None and not self.state.dead:
                self.state.names[handler.name.id] = MaybeUnbound()  # Python unbinds `e` after the block
            exits.append(self.state)

        if not stmt.finalbody:
            self.state = merge(exits)
            return
        # `finally` runs on every path. Check it once for the exception path
        # (errors only), then for the normal exits, which is what continues.
        self.finally_loops.append(len(self.loops))
        self.state = merge([raised, *exits])
        self.state.dead = False
        self.check_block(stmt.finalbody)
        self.state = merge(exits)
        dead = self.state.dead
        self.state.dead = False
        self.check_block(stmt.finalbody)
        self.state.dead = self.state.dead or dead
        self.finally_loops.pop()

    def check_with(self, stmt: A.With) -> None:
        """`with ctx as x:` calls __enter__ first and __exit__ on every way out.
        If an __exit__ may swallow exceptions (returns bool), code after the with
        block can be reached from any point in the body, as after a try/except."""
        can_suppress = False
        for item in stmt.items:
            self.with_contexts.add(id(item.context))  # (m.read() / m.write() are only allowed here)
            t = self.check_expr(item.context)
            info = self.context_manager(t, item.context)
            item.sym = info
            can_suppress = can_suppress or info.suppresses
            if item.target is not None:
                if info.enter_type == NONE:
                    if info.kind == "contextlib":
                        raise self.error(f"this {t} gives None (a bare `yield`, or nothing to enter), so there's "
                                         f"nothing to bind with 'as'", item.target)
                    raise self.error("__enter__ doesn't return anything, so there's nothing to bind with 'as'", item.target)
                self.assign(item.target, info.enter_type, item.context)
        for item in stmt.items:
            if item.sym.kind in LOCKED_VIEWS and not isinstance(item.target, (A.Name, type(None))):
                raise self.error("`with mutex as name:` needs a plain name (it's a reference to the protected value)", item.target)
        snapshots = [self.state.copy()]
        for s in stmt.body:
            self.check_stmt(s)
            snapshots.append(self.state.copy())
        _, modified, escaped = threads.uses(stmt.body) if any(i.sym.kind in LOCKED_VIEWS for i in stmt.items) else ({}, {}, {})
        for item in stmt.items:
            if item.sym.kind in LOCKED_VIEWS and isinstance(item.target, A.Name):
                var = item.target.sym
                if var.captured:
                    raise self.error(
                        f"'{item.target.id}' is only valid while the mutex is held, so a closure can't use it", item.target
                    )
                if id(var) in escaped:
                    name = item.target.id
                    elem = element_type(var.type)
                    if elem is not None and threads.holds_references(elem):
                        copy = f"copy each item too, e.g. [list(row) for row in {name}]"
                    else:
                        kind = {ListType: "list", DictType: "dict", SetType: "set"}.get(type(var.type), "list")
                        copy = f"take a copy: {kind}({name})"
                    raise CheckError(
                        f"'{name}' is only valid while the mutex is held, and this would let the data in it escape the "
                        f"lock (another thread could then change it). Work on it inside the with block, or {copy}",
                        escaped[id(var)],
                    )
                if item.sym.kind == "rw_read" and id(var) in modified:
                    m = describe_short(item.context.func.value) if isinstance(item.context, A.Call) else "the RWMutex"
                    raise CheckError(
                        f"{m}.read() gives read-only access (other threads may be reading too), but this changes "
                        f"'{item.target.id}'. Use `with {m}.write() as {item.target.id}:` to change it",
                        modified[id(var)],
                    )
                if not self.state.dead:
                    self.state.names[item.target.id] = MaybeUnbound()  # gone once the lock is released
        for item in stmt.items:
            if item.sym.kind == "exitstack":
                item.sym.suppresses = self.exit_stack_may_swallow(item, stmt.body)
        can_suppress = any(item.sym.suppresses for item in stmt.items)
        if can_suppress:
            raised = merge(snapshots)
            raised.dead = False
            self.state = merge([self.state, raised])

    def context_manager(self, t: Type, node: A.Expr) -> WithInfo:
        if isinstance(t, FileType):
            return WithInfo("file", t, None, False)
        if isinstance(t, SyncType) and t.kind in ("Lock", "RLock"):
            return WithInfo("lock", BOOL, None, False)
        if t == SOCKET:
            return WithInfo("socket", t, None, False)
        if t == builtins.HTTP_RESPONSE:  # `with urlopen(url) as r:` closes the connection at the end
            return WithInfo("response", t, None, False)
        if t == builtins.EXECUTOR:  # `with ThreadPoolExecutor() as pool:` waits for the work at the end
            return WithInfo("executor", t, None, False)
        if t == TEMPDIR:  # `with TemporaryDirectory() as tmp:` gives its name, removed at the end
            return WithInfo("tempdir", STR, None, False)
        if t == SQLITE_CONNECTION:  # `with conn:` commits, or rolls back if the block raised
            return WithInfo("connection", t, None, False)
        if isinstance(t, HTTPServerType):  # `with HTTPServer(...) as server:` closes it at the end
            return WithInfo("server", t, None, False)
        if isinstance(t, ProcessType) and t.kind == "Popen":  # waits for the child at the end
            return WithInfo("process", t, None, False)
        if isinstance(t, SyncType) and t.kind == "Mutex":
            return WithInfo("mutex", t.args[0], None, False)
        if isinstance(t, SyncType) and t.kind in ("RWRead", "RWWrite"):  # with m.read() as data:
            return WithInfo("rw_read" if t.kind == "RWRead" else "rw_write", t.args[0], None, False)
        if isinstance(t, SyncType) and t.kind == "RWMutex":
            m = describe_short(node)
            raise self.error(f"say which: `with {m}.read() as data:` (many readers at once) or "
                             f"`with {m}.write() as data:` (one writer)", node)
        if isinstance(t, ContextManagerType):  # contextlib's: a @contextmanager function's call, suppress()...
            return WithInfo("contextlib", t.elem, None, self.may_swallow(node))
        if t == EXIT_STACK:  # `with ExitStack() as stack:` unwinds what was pushed onto it
            return WithInfo("exitstack", t, None, True)
        if isinstance(t, StructType):
            enter = t.find_method("__enter__")
            exit_ = t.find_method("__exit__")
            if enter is None or exit_ is None:
                missing = "__enter__" if enter is None else "__exit__"
                raise self.error(f"{t.name} can't be used in a 'with' statement: it has no {missing} method", node)
            if enter.params:
                raise self.error("__enter__ can't take parameters (other than self)", enter.node)
            if len(exit_.params) > 1 or (
                exit_.params and not (
                    isinstance(exit_.params[0].type, OptionalType)
                    and isinstance(exit_.params[0].type.inner, StructType)
                    and exit_.params[0].type.inner.is_exception
                )
            ):
                raise self.error(
                    "__exit__ takes either no parameters, or one `exc: Exception?` "
                    "(None when the block finished normally)", exit_.node,
                )
            if exit_.ret not in (NONE, BOOL):
                raise self.error("__exit__ must return nothing, or a bool (True to swallow the exception)", exit_.node)
            return WithInfo("object", enter.ret, exit_, exit_.ret == BOOL)
        raise self.error(
            f"{t} can't be used in a 'with' statement (it needs __enter__ and __exit__ methods)", node
        )

    def exit_stack_may_swallow(self, item: A.WithItem, body: list[A.Stmt]) -> bool:
        """`with ExitStack() as stack:` swallows an exception only if something entered onto it
        does. Known when the block only uses `stack` for enter_context(), callback(), close()
        and pop_all() (checked by now); otherwise assume it can."""
        ctx = item.context
        name = ctx.func.id if isinstance(ctx, A.Call) and isinstance(ctx.func, A.Name) else \
            ctx.func.attr if isinstance(ctx, A.Call) and isinstance(ctx.func, A.Attribute) else None
        if name != "ExitStack":
            return True
        if item.target is None:
            return False
        if not isinstance(item.target, A.Name):
            return True
        var = item.target.sym
        uses = sum(1 for n in flow.walk(body) if isinstance(n, A.Name) and n.sym is var)
        expected = 0
        for n in flow.walk(body):
            if not (isinstance(n, A.Call) and isinstance(n.func, A.Attribute) and isinstance(n.func.value, A.Name)
                    and n.func.value.sym is var):
                continue
            if n.func.attr == "enter_context":
                if n.with_info.suppresses:
                    return True
                expected += 1
            elif n.func.attr in ("callback", "close", "pop_all"):
                expected += 1
        return uses != expected

    def may_swallow(self, node: A.Expr) -> bool:
        """Can this contextlib context manager swallow an exception from the with block?
        Known for a direct call (nullcontext() and closing() never do, a @contextmanager
        function does if it catches what's raised at its yield); otherwise assume it can."""
        if isinstance(node, A.Call):
            if getattr(node, "never_suppresses", False):
                return False
            target = node.sym.target if isinstance(node.sym, CallTarget) else None
            if isinstance(target, FuncInfo) and target.context_manager:
                return target.cm_suppresses
        return True

    def check_handler_types(self, handler: A.ExceptHandler) -> StructType:
        """The classes an `except` clause catches; returns the type bound by `as e`."""
        if handler.type is None:
            handler.sym = []
            return builtins.EXCEPTIONS["Exception"]
        exprs = handler.type.elts if isinstance(handler.type, A.TupleLit) else [handler.type]
        classes: list[StructType] = []
        for e in exprs:
            st = self.lookup_struct(e.id) if isinstance(e, A.Name) else self.module_struct(e)
            if st is None or not st.is_exception:
                raise self.error("'except' needs an exception class, like `except ValueError:`", e)
            classes.append(st)
        handler.sym = classes
        caught = classes[0]
        for st in classes[1:]:
            caught = join(caught, st)
        return caught

    def check_if(self, stmt: A.If) -> None:
        on_true, on_false = self.check_condition(stmt.test)
        self.state = on_true
        self.check_block(stmt.body)
        after_body = self.state
        self.state = on_false
        self.check_block(stmt.orelse)
        self.state = merge([after_body, self.state])

    # ---- match statements ----------------------------------------------------

    def check_match(self, stmt: A.Match) -> None:
        """Each case is a branch. `remaining` is what the subject may still be when a case is
        tried: after `case None:` an optional subject is known not to be None, and once a
        case matches everything, later cases can't run (and the match can't fall through)."""
        subject_t = self.check_expr(stmt.subject)
        remaining: Type | None = subject_t  # None once every value has been matched
        enum_t = strip_optional(subject_t)
        if not (isinstance(enum_t, StructType) and enum_t.enum is not None and not enum_t.enum.flag):
            enum_t = None
        covered: set[str] = set()  # the enum's members matched so far (by cases without a guard)
        fallthrough = self.state.copy()
        exits: list[State] = []
        for i, case in enumerate(stmt.cases):
            if remaining is None:
                raise self.error("this case can never run: the cases before it already match everything", case)
            self.state = fallthrough.copy()
            bindings = self.check_pattern(case.pattern, remaining)
            seen: set[str] = set()
            for name, t in bindings:
                if name.id in seen:
                    raise self.error(f"multiple assignments to name '{name.id}' in pattern", name)
                seen.add(name.id)
            for name, t in bindings:
                self.bind(name, t, case.pattern)
            self.narrow_subject(stmt.subject, self.pattern_narrows(case.pattern, remaining))
            if case.guard is not None:
                on_true, on_false = self.check_condition(case.guard)
                fallthrough = merge([fallthrough, on_false])
                self.state = on_true
            self.check_block(case.body)
            exits.append(self.state)
            if case.guard is None:
                if self.irrefutable(case.pattern, remaining):
                    if i < len(stmt.cases) - 1 and isinstance(case.pattern, A.MatchAs) and case.pattern.pattern is None:
                        what = f"name capture '{case.pattern.name.id}'" if case.pattern.name else "wildcard"
                        raise self.error(f"{what} makes remaining patterns unreachable", case.pattern)
                    remaining = None
                else:
                    remaining = self.unmatched(case.pattern, remaining)
                    if enum_t is not None and remaining is not None:  # case Color.RED: ... every member is everything
                        covered |= enum_members_matched(case.pattern)
                        if covered >= {m.name for m in enum_t.enum.distinct}:
                            remaining = NONE if isinstance(remaining, OptionalType) else None
            if remaining is not None and remaining is not subject_t:
                self.narrow_subject_in(fallthrough, stmt.subject, remaining)
        if remaining is not None:
            exits.append(fallthrough)
        self.state = merge(exits)
        stmt.never_completes = self.state.dead  # every case returns or raises, and one always runs

    def narrow_subject(self, subject: A.Expr, t: Type | None) -> None:
        if t is not None:
            self.narrow_subject_in(self.state, subject, t)

    def narrow_subject_in(self, state: State, subject: A.Expr, t: Type) -> None:
        """Inside `case Dog():` (or after `case None:`) a subject variable has the narrower type."""
        if isinstance(subject, A.Name) and isinstance(entry := state.names.get(subject.id), Bound):
            if t != entry.ty and assignable(t, entry.ty) and (
                isinstance(entry.ty, OptionalType) or isinstance(t, StructType)
            ):
                state.names[subject.id] = Bound(entry.var, t)

    def pattern_narrows(self, p: A.Pattern, t: Type) -> Type | None:
        """The subject's type when `p` matched, if narrower than `t`."""
        match p:
            case A.MatchAs(inner, _):
                return self.pattern_narrows(inner, t) if inner is not None else None
            case A.MatchOr(options):
                narrowed = [self.pattern_narrows(o, t) or t for o in options]
                out = narrowed[0]
                for n in narrowed[1:]:
                    out = join(out, n) or t
                return out if out != t else None
            case A.MatchValue(A.NoneLit()):
                return None
            case A.MatchClass() if isinstance(p.sym, StructType):
                return p.sym
        if isinstance(t, OptionalType) and not isinstance(p, A.MatchAs):
            return t.inner  # anything but None (or a capture) means there's a value
        return None

    def irrefutable(self, p: A.Pattern, t: Type) -> bool:
        """Does `p` match every value of type `t`?"""
        match p:
            case A.MatchAs(inner, _):
                return inner is None or self.irrefutable(inner, t)
            case A.MatchOr(options):
                if any(self.irrefutable(o, t) for o in options):
                    return True
                rest: Type | None = t
                for o in options:  # `None | int()` on an int? covers it
                    rest = self.unmatched(o, rest) if rest is not None else None
                return rest is None
            case A.MatchClass(_, args, _, kw) if not isinstance(t, OptionalType):
                covers = p.sym == t if isinstance(p.sym, StructType) else (p.sym is not None and p.sym[1] == t)
                if isinstance(p.sym, StructType) and isinstance(t, StructType):
                    covers = t.is_subclass_of(p.sym)
                return covers and all(self.irrefutable(a, a.ty) for a in (*args, *kw))
            case A.MatchSequence(items) if isinstance(t, TupleType):
                return all(self.irrefutable(item, item.ty) for item in items if not isinstance(item, A.MatchStar))
        return False

    def unmatched(self, p: A.Pattern, t: Type) -> Type | None:
        """What's left of `t` once values matching `p` are taken out (None: nothing)."""
        if self.irrefutable(p, t):
            return None
        if isinstance(t, OptionalType):
            options = p.patterns if isinstance(p, A.MatchOr) else [p]
            takes_none = any(isinstance(o, A.MatchValue) and isinstance(o.value, A.NoneLit) for o in options)
            takes_value = any(self.irrefutable(o, t.inner) for o in options if not isinstance(o, A.MatchValue))
            if takes_none and takes_value:
                return None
            if takes_none:
                return t.inner
            if takes_value:
                return NONE
        if t == NONE and isinstance(p, A.MatchValue) and isinstance(p.value, A.NoneLit):
            return None
        return t

    def check_pattern(self, p: A.Pattern, t: Type) -> list[tuple[A.Name, Type]]:
        """Check pattern `p` against a subject of type `t`; return the names it binds."""
        p.ty = t
        match p:
            case A.MatchAs(None, None):
                return []
            case A.MatchAs(None, name):
                return [(name, t)]
            case A.MatchAs(inner, name):
                bindings = self.check_pattern(inner, t)
                p.bound = self.pattern_narrows(inner, t) or t
                return bindings + [(name, p.bound)]
            case A.MatchOr(options):
                results = [self.check_pattern(o, t) for o in options]
                names = [sorted(n.id for n, _ in r) for r in results]
                if any(n != names[0] for n in names[1:]):
                    raise self.error("alternative patterns bind different names", p)
                merged: list[tuple[A.Name, Type]] = []
                for name, bt in results[0]:
                    joined = bt
                    for r in results[1:]:
                        other = next(t2 for n2, t2 in r if n2.id == name.id)
                        joined = join(joined, other)
                        if joined is None:
                            raise self.error(f"'{name.id}' would be {bt} or {other} depending on the alternative", p)
                    merged.append((name, joined))
                p.sym = [[n for n, _ in r] for r in results]  # every alternative's own Name nodes, for codegen
                return merged
            case A.MatchValue(value):
                self.check_value_pattern(p, value, t)
                return []
            case A.MatchSequence(items):
                return self.check_sequence_pattern(p, items, t)
            case A.MatchMapping(keys, patterns, rest):
                return self.check_mapping_pattern(p, keys, patterns, rest, t)
            case A.MatchClass():
                return self.check_class_pattern(p, t)
            case A.MatchStar():
                raise self.error("a starred pattern needs to be inside a sequence pattern", p)
        raise self.error("unsupported pattern", p)

    def never_matches(self, p: A.Node, t: Type, what: str) -> CheckError:
        return self.error(f"this pattern can never match: {what}", p)

    def check_value_pattern(self, p: A.MatchValue, value: A.Expr, t: Type) -> None:
        inner = strip_optional(t)
        if isinstance(value, A.NoneLit):
            value.ty = NONE
            if not isinstance(t, OptionalType) and t not in (NONE, JSON_VALUE):
                raise self.never_matches(p, t, f"{with_article(t)} is never None")
            return
        vt = self.check_expr(value)
        if isinstance(vt, StructType) and vt.enum is not None:  # case Color.RED:
            if vt != inner:
                raise self.never_matches(p, t, f"{with_article(t)} is never equal to a {vt} member")
            return
        if enum_mixin(inner) is not None and vt == enum_mixin(inner):
            return  # an IntEnum subject with `case 1:`
        if vt not in (INT, FLOAT, BOOL, STR, BYTES):
            raise self.error(f"a value pattern must be a number, string, bytes, True/False or None, not {vt}", value)
        if inner == JSON_VALUE:
            return
        if isinstance(value, A.BoolLit) and inner != BOOL:
            raise self.never_matches(
                p, t, f"`case {value.value}:` only matches a bool (it compares with `is`), not {with_article(t)}"
            )
        ok = vt == inner or (is_numeric(vt) and is_numeric(inner) and vt != BOOL)
        if not ok:
            raise self.never_matches(p, t, f"{with_article(t)} is never equal to {with_article(vt)}")

    def check_sequence_pattern(self, p: A.MatchSequence, items: list[A.Pattern], t: Type) -> list[tuple[A.Name, Type]]:
        inner = strip_optional(t)
        stars = [i for i, item in enumerate(items) if isinstance(item, A.MatchStar)]
        if len(stars) > 1:
            raise self.error("multiple starred names in sequence pattern", items[stars[1]])
        star = stars[0] if stars else None
        fixed = len(items) - (1 if stars else 0)
        bindings: list[tuple[A.Name, Type]] = []
        match inner:
            case ListType(elem) | VarTupleType(elem):
                elem_types = [elem] * len(items)
                star_type: Type = ListType(elem)
            case TupleType(elts):
                if star is None and len(elts) != len(items):
                    raise self.never_matches(p, t, f"{with_article(inner)} has {len(elts)} items, not {len(items)}")
                if star is not None and fixed > len(elts):
                    raise self.never_matches(p, t, f"{with_article(inner)} has {len(elts)} items, fewer than {fixed}")
                if star is None:
                    elem_types = list(elts)
                    star_type = NONE
                else:
                    after = len(items) - star - 1
                    middle = list(elts[star:len(elts) - after])
                    elem_types = list(elts[:star]) + [NONE] + list(elts[len(elts) - after:])
                    named = items[star].name is not None
                    if named and middle and any(m != middle[0] for m in middle):
                        raise self.error(f"*{items[star].name.id} would hold items of different types "
                                         f"({', '.join(map(str, middle))})", items[star])
                    star_type = ListType(middle[0]) if middle and named else None
                    if star_type is None and named:
                        raise self.error(f"*{items[star].name.id} is always empty here; leave it out", items[star])
            case _ if inner == JSON_VALUE:
                elem_types = [JSON_VALUE] * len(items)
                star_type = ListType(JSON_VALUE)
            case _ if inner in (STR, BYTES):
                raise self.never_matches(p, t, f"sequence patterns don't match {with_article(inner)} (as in Python); "
                                               f"compare it, or use a guard")
            case _:
                raise self.never_matches(p, t, f"{with_article(t)} isn't a list or tuple")
        for i, item in enumerate(items):
            if i == star:
                item.ty = star_type
                if item.name is not None:
                    bindings.append((item.name, star_type))
            else:
                bindings += self.check_pattern(item, elem_types[i])
        return bindings

    def check_mapping_pattern(self, p: A.MatchMapping, keys: list[A.Expr], patterns: list[A.Pattern],
                              rest: A.Name | None, t: Type) -> list[tuple[A.Name, Type]]:
        inner = strip_optional(t)
        match inner:
            case DictType(key, value):
                pass
            case _ if inner == JSON_VALUE:
                key, value = STR, JSON_VALUE
            case _:
                raise self.never_matches(p, t, f"{with_article(t)} isn't a dict")
        seen = set()
        bindings: list[tuple[A.Name, Type]] = []
        for k, sub in zip(keys, patterns):
            kt = self.check_expr(k)
            if not assignable(kt, key):
                raise self.never_matches(k, t, f"its keys are {key}, not {kt}")
            literal = getattr(k, "value", None) if not isinstance(k, A.Attribute) else None
            if literal is not None and (kt, literal) in seen:
                raise self.error(f"mapping pattern checks duplicate key ({describe_short(k)})", k)
            seen.add((kt, literal))
            bindings += self.check_pattern(sub, value)
        if rest is not None:
            bindings.append((rest, DictType(key, value)))
        return bindings

    BUILTIN_CLASS_PATTERNS = {"int": INT, "float": FLOAT, "str": STR, "bool": BOOL, "bytes": BYTES}

    def check_class_pattern(self, p: A.MatchClass, t: Type) -> list[tuple[A.Name, Type]]:
        inner = strip_optional(t)
        cls = p.cls
        name = cls.id if isinstance(cls, A.Name) else None
        builtin_name = name if name is not None and name not in self.state.names and self.lookup_struct(name) is None else None
        if builtin_name in (*self.BUILTIN_CLASS_PATTERNS, "list", "dict", "tuple"):
            if p.kwd_names:
                raise self.error(f"{builtin_name}() patterns don't take keyword sub-patterns", p)
            if len(p.patterns) > 1:
                raise self.error(f"{builtin_name}() accepts 1 positional sub-pattern ({len(p.patterns)} given)", p)
            matched = self.builtin_class_match(p, builtin_name, inner, t)
            p.sym = (builtin_name, matched)
            return self.check_pattern(p.patterns[0], matched) if p.patterns else []
        st = self.lookup_struct(name) if name is not None else self.module_struct(cls)
        if st is None:
            raise self.error(f"'{describe_short(cls)}' isn't a class that patterns can match", cls)
        if not isinstance(inner, StructType):
            raise self.never_matches(p, t, f"{with_article(t)} is never {with_article(st.name)}")
        if not (inner.is_subclass_of(st) or (st.is_subclass_of(inner) and inner.kind == "class")):
            raise self.never_matches(p, t, f"{with_article(inner.name)} is never {with_article(st.name)}")
        p.sym = st
        fields = list(st.all_fields())
        if p.patterns and not (st.kind == "struct" or (st.node is not None and st.node.decorators)):
            raise self.error(f"{st.name}() accepts no positional sub-patterns (it isn't a @dataclass); "
                             f"name the fields instead, like {st.name}({fields[0] if fields else 'x'}=...)", p)
        if len(p.patterns) > len(fields):
            raise self.error(f"{st.name}() accepts {len(fields)} positional sub-pattern"
                             f"{'' if len(fields) == 1 else 's'} ({len(p.patterns)} given)", p)
        names = fields[:len(p.patterns)] + p.kwd_names
        if len(set(names)) != len(names):
            dup = next(n for n in names if names.count(n) > 1)
            raise self.error(f"{st.name}() got multiple sub-patterns for attribute '{dup}'", p)
        p.fields = names
        bindings: list[tuple[A.Name, Type]] = []
        for field_name, sub in zip(names, (*p.patterns, *p.kwd_patterns)):
            f = st.find_field(field_name)
            if f is None:
                raise self.error(f"{st.name} has no field '{field_name}'", sub)
            bindings += self.check_pattern(sub, f.type)
        return bindings

    def builtin_class_match(self, p: A.MatchClass, name: str, inner: Type, t: Type) -> Type:
        """The type of the value when `int()`, `list()`... matches a subject of type `t`."""
        if inner == JSON_VALUE:  # json.Value: int(), str(), list()... check which kind of value it holds
            if name == "tuple" or name == "bytes":
                raise self.never_matches(p, t, f"JSON has no {name}s")
            return {"list": ListType(JSON_VALUE), "dict": DictType(STR, JSON_VALUE)}.get(name) or self.BUILTIN_CLASS_PATTERNS[name]
        want = self.BUILTIN_CLASS_PATTERNS.get(name)
        ok = (inner == want or (name == "int" and inner == BOOL) or (name == "list" and isinstance(inner, ListType))
              or (name == "dict" and isinstance(inner, DictType))
              or (name == "tuple" and isinstance(inner, (TupleType, VarTupleType))))
        if not ok:
            raise self.never_matches(p, t, f"{with_article(t)} is never {with_article(name)}")
        return inner

    def check_while(self, stmt: A.While) -> None:
        def iteration() -> State:
            on_true, on_false = self.check_condition(stmt.test)
            if isinstance(stmt.test, A.BoolLit) and stmt.test.value:
                on_false.dead = True  # `while True:` only exits via break
            self.state = on_true
            return on_false

        self.check_loop(stmt, iteration)

    def check_for(self, stmt: A.For) -> None:
        iter_type = self.check_expr(stmt.iter)
        elem = self.loop_element(stmt.iter, iter_type)

        def iteration() -> State:
            exhausted = self.state.copy()
            self.assign(stmt.target, elem, stmt.iter)
            return exhausted

        self.check_loop(stmt, iteration)

    def loop_element(self, node: A.Expr, t: Type) -> Type:
        elem = element_type(t)
        if elem is None:
            raise self.error(f"can't loop over {t}{builtins.mixed_tuple_hint(t)}", node)
        builtins.mark_tuple_iterable(node, t, elem)
        return elem

    def check_loop(self, stmt: A.While | A.For, begin_iteration) -> None:
        """Check a loop body until the variable state at the loop head stops changing.

        The body can run with the state from before the loop, or with the state
        left by a previous iteration (or a `continue`), so the head state is
        their merge. Re-checking the body with that wider state catches e.g. a
        variable that is an int on the first iteration but a str afterwards.
        """
        before = self.state
        head = before
        for _ in range(10):
            self.state = head.copy()
            exit_state = begin_iteration()
            loop = LoopContext()
            self.loops.append(loop)
            self.check_block(stmt.body)
            self.loops.pop()
            new_head = merge([before, self.state, *loop.continues])
            if new_head == head:
                break
            head = new_head
        else:
            raise self.error("internal error: loop types did not settle", stmt)
        if stmt.orelse:
            self.state = exit_state
            self.check_block(stmt.orelse)
            exit_state = self.state
        self.state = merge([exit_state, *loop.breaks])

    # =========================================================================
    # Conditions and narrowing
    # =========================================================================

    def check_condition(self, e: A.Expr) -> tuple[State, State]:
        """Check `e` used as a condition. Returns the states where it's true / false.

        This is where `T?` narrowing happens: in `if x is not None:` the true
        state sees x as T.
        """
        match e:
            case A.UnaryOp("not", operand):
                on_true, on_false = self.check_condition(operand)
                e.ty = BOOL
                return on_false, on_true
            case A.BoolOp("and", left, right):
                left_true, left_false = self.check_condition(left)
                self.state = left_true
                right_true, right_false = self.check_condition(right)
                e.ty = BOOL
                return right_true, merge([left_false, right_false])
            case A.BoolOp("or", left, right):
                left_true, left_false = self.check_condition(left)
                self.state = left_false
                right_true, right_false = self.check_condition(right)
                e.ty = BOOL
                return merge([left_true, right_true]), right_false
            case A.Call(A.Name("isinstance")) if self.is_builtin_name(e.func, "isinstance"):
                self.check_expr(e)
                return self.isinstance_narrowing(e), self.state.copy()
            case A.Compare(A.Attribute(obj, attr) as subject, ["==" | "!=" as op], [A.StrLit(value)]) if (
                isinstance(self.check_expr(obj), NamespaceType) and any(d == attr for d, _, _ in obj.ty.commands)
                and attr_path(subject) is not None
            ):  # if args.command == "add": that subcommand's arguments have their own types
                self.check_expr(e)
                chosen = self.state.copy()
                base = attr_path(subject)
                for dest, names, fields in obj.ty.commands:
                    if dest == attr and value in names:
                        for name, t in fields:
                            chosen.attrs[base[:-1] + (name,)] = t
                return (chosen, self.state.copy()) if op == "==" else (self.state.copy(), chosen)
            case A.Compare(subject, [op], [A.NoneLit()]) if op in ("is", "is not", "==", "!="):
                self.check_expr(e)
                narrowed = self.narrowed(subject)
                return (narrowed, self.state.copy()) if op in ("is not", "!=") else (self.state.copy(), narrowed)
            case A.Compare(subject, ["==" | "!=" | "is" | "is not" as op], [other]):
                self.check_expr(e)  # if e.errno == errno.ENOENT: e.errno isn't None there (or `c is Color.RED`)
                if isinstance(subject.ty, OptionalType) and other.ty not in (None, NONE) and not isinstance(other.ty, OptionalType):
                    narrowed = self.narrowed(subject)
                    return (narrowed, self.state.copy()) if op in ("==", "is") else (self.state.copy(), narrowed)
                return self.state.copy(), self.state.copy()
        t = self.check_expr(e)
        self.check_truthy(t, e)
        if isinstance(t, OptionalType):
            return self.narrowed(e), self.state.copy()
        return self.state.copy(), self.state.copy()

    def narrowed(self, subject: A.Expr) -> State:
        """A copy of the state where `subject` (a name, walrus, or attribute chain
        like self.head) is known not to be None."""
        state = self.state.copy()
        name = subject.target if isinstance(subject, A.NamedExpr) else subject
        if isinstance(name, A.Name):
            entry = state.names.get(name.id)
            if isinstance(entry, Bound) and isinstance(entry.ty, OptionalType):
                state.names[name.id] = Bound(entry.var, entry.ty.inner)
        elif (path := attr_path(subject)) and isinstance(subject.ty, OptionalType):
            state.attrs[path] = subject.ty.inner
        return state

    def check_truthy(self, t: Type, e: A.Expr) -> None:
        ok = t in (INT, FLOAT, BOOL, STR, BYTES, JSON_VALUE, PATH, *DATETIME_TYPES) or isinstance(
            t, (ListType, DictType, SetType, TupleType, OptionalType, FileType, DequeType, MatchType, VarTupleType,
                GeneratorType)
        ) or bool(self.dunder(t, "__bool__") or self.dunder(t, "__len__")) or (
            isinstance(t, StructType) and t.enum is not None)  # (a member is true; an IntEnum's or flag's if not 0)
        if not ok:
            raise self.error(f"{t} can't be used as a condition", e)

    # =========================================================================
    # Expressions
    # =========================================================================

    def check_expr(self, e: A.Expr, expected: Type | None = None) -> Type:
        """Type-check `e`, record the result in e.ty, and return it.

        `expected` is only a hint (for `[]`, `{}` and friends); callers still
        check assignability themselves.
        """
        t = self.infer(e, expected)
        e.ty = t
        return t

    def infer(self, e: A.Expr, expected: Type | None) -> Type:
        match e:
            case A.IntLit(value):
                if not INT64_MIN <= value <= INT64_MAX:
                    raise self.error("integer literal is too large for int (64-bit)", e)
                return INT
            case A.FloatLit():
                return FLOAT
            case A.StrLit():
                return STR
            case A.BytesLit():
                return BYTES
            case A.BoolLit():
                return BOOL
            case A.NoneLit():
                return NONE
            case A.FormatArg():
                return e.ty  # set when the format string was compiled (builtins.str_format)
            case A.FString(parts):
                for part in parts:
                    if isinstance(part, A.FormattedValue):
                        t = self.check_expr(part.value)
                        self.check_printable(t, part.value)
                        self.check_format_spec(part, t)
                return STR
            case A.Name():
                return self.check_name(e, expected)
            case A.Lambda():
                return self.check_lambda(e, expected)
            case A.ListLit(elts):
                return self.check_sequence_literal(e, elts, expected, ListType, "list")
            case A.SetLit(elts):
                t = self.check_sequence_literal(e, elts, expected, SetType, "set")
                self.check_hashable(t.elem, "set elements", e)
                return t
            case A.DictLit(keys, values):
                return self.check_dict_literal(e, keys, values, expected)
            case A.TupleLit(elts):
                hints = expected.elts if isinstance(expected, TupleType) and len(expected.elts) == len(elts) else [None] * len(elts)
                if isinstance(expected, VarTupleType):
                    hints = [expected.elem] * len(elts)
                return TupleType(tuple(self.check_expr(x, h) for x, h in zip(elts, hints)))
            case A.ListComp(elt, gens):
                return ListType(self.check_comprehension(gens, lambda: self.check_expr(elt)))
            case A.SetComp(elt, gens):
                t = SetType(self.check_comprehension(gens, lambda: self.check_expr(elt)))
                self.check_hashable(t.elem, "set elements", elt)
                return t
            case A.GeneratorExp(elt, gens):
                return GeneratorType(self.check_comprehension(gens, lambda: self.check_expr(elt)))  # lazy
            case A.DictComp(key, value, gens):
                k, v = self.check_comprehension(gens, lambda: (self.check_expr(key), self.check_expr(value)))
                self.check_hashable(k, "dict keys", key)
                return DictType(k, v)
            case A.UnaryOp(op, operand):
                return self.check_unary(e, op, operand)
            case A.BinOp(op, left, right):
                return self.binop_type(op, self.check_expr(left), self.check_expr(right), e)
            case A.BoolOp():
                return self.check_boolop(e)
            case A.Compare():
                return self.check_compare(e)
            case A.IfExp(test, body, orelse):
                on_true, on_false = self.check_condition(test)
                self.state = on_true
                bt = self.check_expr(body, expected)
                after_true = self.state
                self.state = on_false
                ot = self.check_expr(orelse, expected)
                self.state = merge([after_true, self.state])
                joined = join(bt, ot)
                if joined is None:
                    raise self.error(f"the two branches have different types: {bt} and {ot}", e)
                return joined
            case A.NamedExpr(target, value):
                if self.lambda_depth:
                    raise self.error("':=' can't be used inside a lambda", e)
                t = self.check_expr(value)
                self.bind(target, t, value)
                return self.state.names[target.id].ty
            case A.Call():
                return self.check_call(e, expected)
            case A.Attribute(value, attr):
                return self.check_attribute(e, value, attr, expected)
            case A.Index(value, index):
                return self.check_index(e, value, index)
            case A.Slice():
                raise self.error("a slice can only be used inside [...]", e)
        raise self.error(f"unsupported expression {type(e).__name__}", e)

    def check_name(self, e: A.Name, expected: Type | None = None) -> Type:
        name = e.id
        entry = self.state.names.get(name)
        if isinstance(entry, Bound):
            e.sym = entry.var
            self.note_capture(entry.var)
            return entry.ty
        if isinstance(entry, Conflict):
            versions = sorted(entry.sources, key=lambda s: (s[1].line, s[1].col))
            described = ", ".join(f"{t} (line {loc.line})" for t, loc in versions)
            raise self.error(
                f"'{name}' has different types depending on the path taken to get here: {described}. "
                f"Use one type on every path, or give it a new name", e,
            )
        if isinstance(entry, Moved):
            how = f"`with {entry.mutex} as {name}:`" if entry.mutex else "a `with` block"
            raise self.error(
                f"'{name}' was moved into a Mutex (line {entry.loc.line}), which owns it now: use it through the "
                f"Mutex ({how}), or give '{name}' a new value first", e,
            )
        if isinstance(entry, MaybeUnbound) or name in self.scope.assigned:
            what = "might not be assigned yet" if isinstance(entry, MaybeUnbound) else "is used before it's assigned"
            raise self.error(f"'{name}' {what}", e)
        if not self.scope.is_module and name in self.module_assign_counts:
            var = self.globals.get(name)
            if var is None:
                raise self.error(
                    f"functions can only use module-level variables that are assigned exactly once; "
                    f"'{name}' is assigned {self.module_assign_counts[name]} times", e,
                )
            e.sym = var
            return var.type
        if name in self.modules:
            e.sym = self.modules[name]
            return ModuleType(self.modules[name].name)
        if name in self.imported:
            mod, member = self.imported[name]
            if isinstance(mod.members.get(member), builtins.Function):
                return self.callable_as_value(e, expected, name)
            return self.module_member(e, mod, member)
        if name in self.functions:
            info = self.functions[name]
            e.sym = info
            return FuncType(tuple(p.type for p in info.params), info.ret)
        if ((st := self.lookup_struct(name)) is not None and st.enum is not None
                and not isinstance(strip_optional(expected) if expected else None, FuncType)):
            e.sym = st  # an enum class: `for c in Color`, `len(Color)`, `Color["RED"]`
            return ClassRefType(st)
        if self.lookup_struct(name) or name in builtins.FUNCTIONS:
            return self.callable_as_value(e, expected, name)
        if name in builtins.VALUES:
            e.sym = builtins.VALUES[name]
            return builtins.VALUES[name].type
        if name in self.scope.outer_assigned:
            raise self.error(
                f"'{name}' isn't assigned yet where this nested function is defined; "
                f"assign it before the def", e,
            )
        raise self.error(f"name '{name}' is not defined", e)

    def check_lambda(self, e: A.Lambda, expected: Type | None) -> Type:
        """Parameter types come from the expected function type; the result type from the body."""
        expected = strip_optional(expected) if expected is not None else None  # `f: ((int) -> int)? = lambda ...`
        n = len(e.params)
        if isinstance(expected, FuncType) and len(expected.params) == n:
            param_types = expected.params
        elif n == 0:
            param_types = ()
        elif isinstance(expected, FuncType):
            raise self.error(f"this lambda takes {plural(n, 'parameter')}, but {expected} is expected here", e)
        else:
            example = ", ".join("int" for _ in e.params)
            raise self.error(
                f"can't tell the types of this lambda's parameters from here; give it a type, "
                f"e.g. `f: ({example}) -> int = lambda ...`", e,
            )
        outer, outer_frame = self.state, self.frame
        # The lambda may run after captured variables change, so narrowing doesn't carry in.
        self.state = State(
            {n: Bound(en.var, en.var.type) if isinstance(en, Bound) else en for n, en in outer.names.items()},
            outer.dead,
        )
        self.lambda_depth += 1
        self.frame_count += 1
        self.frame = self.frame_count
        for p, t in zip(e.params, param_types):
            var = Var(p.name, p.name, t, "lambda", p.loc, frame=self.frame)
            self.state.names[p.name] = Bound(var, t)
            p.sym = var
        hint = expected.ret if isinstance(expected, FuncType) else None
        try:
            body = self.check_expr(e.body, hint)
        finally:
            self.lambda_depth -= 1
            self.state, self.frame = outer, outer_frame
        if isinstance(body, IterType):
            raise self.error(f"a lambda can't return {body.kind}(...); make a list with list(...)", e.body)
        ret = hint if hint is not None and hint != NONE and assignable(body, hint) else body
        return FuncType(param_types, ret)

    def callable_as_value(self, e: A.Name | A.Attribute, expected: Type | None, name: str) -> Type:
        """Built-ins, constructors and module functions aren't values themselves (they're
        overloaded or generic), but where a function type is expected we can wrap them:
        `key=len` becomes `key=lambda p: len(p)`, `key=str.lower` becomes `lambda p: p.lower()`."""
        expected = strip_optional(expected) if expected is not None else None
        if not isinstance(expected, FuncType):
            raise self.error(f"'{name}' can only be used as a value where a function type is expected; "
                             f"otherwise wrap it in a lambda", e)
        params = [A.Param(f"sd_p{i}", loc=e.loc) for i in range(len(expected.params))]
        args = [A.Name(p.name, loc=e.loc) for p in params]
        if isinstance(e, A.Attribute) and isinstance(e.value, A.Name) and e.value.id in ("str", "list", "dict", "set"):
            if not args:
                raise self.error(f"'{name}' needs an argument to call it on", e)
            body = A.Call(A.Attribute(args[0], e.attr, loc=e.loc), args[1:], loc=e.loc)
        elif isinstance(e, A.Attribute):
            body = A.Call(A.Attribute(e.value, e.attr, loc=e.loc), args, loc=e.loc)
        else:
            body = A.Call(A.Name(e.id, loc=e.loc), args, loc=e.loc)
        wrapper = A.Lambda(params, body, loc=e.loc)
        t = self.check_expr(wrapper, expected)
        e.sym = wrapper
        return t

    def check_printable(self, t: Type, e: A.Expr) -> None:
        if isinstance(t, (IterType, ClassRefType)):
            raise self.error(f"{t} can't be converted to a string", e)

    def name_the_mutex(self, target: A.Expr, value: A.Expr) -> None:
        """`shared = seadash.Mutex(data)`: say `with shared as data:` in the moved-from error."""
        if isinstance(target, A.Name) and isinstance(value, A.Call) and getattr(value.sym, "kind", None) == "sync_new":
            moved = value.sym.target[1].get("value")
            if isinstance(moved, A.Name) and isinstance(entry := self.state.names.get(moved.id), Moved):
                self.state.names[moved.id] = Moved(target.id, entry.loc, entry.var)

    def move_into_mutex(self, value: A.Expr | None, mutex: str | None) -> None:
        """seadash.Mutex(data) / m.set(data): the Mutex owns data now. A plain local name is
        moved (codegen), and reading it afterwards is an error; anything else is copied."""
        if not isinstance(value, A.Name) or not isinstance(value.sym, Var):
            return
        var = value.sym
        if not threads.holds_references(var.type):
            return  # (nothing that could be shared)
        if var.kind in ("local", "param") and not var.captured:
            value.moved_into_mutex = True  # (a closure could still read a captured one: that's copied)
        elif not (var.kind == "global" and self.scope.is_module):
            return
        # A module-level name is copied (functions may read it), but the module's own code
        # reading it again is the same mistake.
        self.state.names[value.id] = Moved(mutex, value.loc, var)

    def check_format_spec(self, part: A.FormattedValue, t: Type) -> None:
        """f"{x:spec}": numbers, strings and dates take a spec. A constant spec is checked
        here by Python's own rules; one with nested fields ({x:{width}}) at run time."""
        spec = part.spec
        if isinstance(spec, A.FString):
            self.check_expr(spec)
        elif not spec:
            return  # {x:} is str(x)
        if part.conversion:
            t = STR
        if isinstance(t, OptionalType) and builtins.format_spec_error(strip_optional(t), None) is None:
            raise self.error(
                f"{t} might be None; check it first, e.g. `if {describe_short(part.value)} is not None:`", part.value
            )
        if message := builtins.format_spec_error(t, spec if isinstance(spec, str) else None):
            raise self.error(message, part.value if message.startswith("a format spec") else part)

    def check_sequence_literal(self, e, elts, expected, ctor, word: str) -> Type:
        hint = expected.elem if isinstance(expected, ctor) else None
        if not elts:
            if hint is None:
                raise self.error(
                    f"can't tell what type of {word} this is; annotate the variable, "
                    f"e.g. `xs: {word}[int] = {'[]' if word == 'list' else 'set()'}`", e,
                )
            return expected
        if hint is None:
            return ctor(self.join_items(elts, f"{word} items"))
        types = [self.check_expr(x, hint) for x in elts]
        if all(assignable(t, hint) for t in types):
            return expected
        return ctor(self.join_all(types, elts, f"{word} items"))

    def check_dict_literal(self, e: A.DictLit, keys, values, expected) -> Type:
        if not keys:
            if isinstance(expected, (DictType, SetType)):
                return expected  # `s: set[int] = {}` is allowed and means an empty set
            raise self.error("can't tell what type of dict this is; annotate the variable, e.g. `d: dict[str, int] = {}`", e)
        if not isinstance(expected, DictType):
            k = self.join_items(keys, "dict keys")
            self.check_hashable(k, "dict keys", keys[0])
            return DictType(k, self.join_items(values, "dict values"))
        kts = [self.check_expr(k, expected.key) for k in keys]
        vts = [self.check_expr(v, expected.value) for v in values]
        if all(assignable(t, expected.key) for t in kts) and all(assignable(t, expected.value) for t in vts):
            return expected
        k = self.join_all(kts, keys, "dict keys")
        self.check_hashable(k, "dict keys", keys[0])
        return DictType(k, self.join_all(vts, values, "dict values"))

    def join_items(self, nodes: list[A.Expr], what: str) -> Type:
        """The common type of a literal's items, with no annotation to go on. Items like `[]`
        or `[None]` get their type from their siblings: {"a": [1.5], "b": [None]} is
        dict[str, list[float?]]."""
        if not any(needs_context(n) for n in nodes) or all(needs_context(n) for n in nodes):
            return self.join_all([self.check_expr(n) for n in nodes], nodes, what)
        result = None
        for n in nodes:
            t = self.literal_shape(n) if needs_context(n) else self.check_expr(n)
            joined = t if result is None else widen(result, t)
            if joined is None:
                raise self.error(f"{what} have different types: {result} and {t}", n)
            result = joined
        if contains_unknown(result) or result == NONE:
            raise self.error(f"can't tell the type of these {what}; annotate the variable", nodes[0])
        for n in nodes:
            # Re-check anything not already exactly `result`, now that the siblings have decided it:
            # [None] and [] adopt it, and [1.5] becomes a list[float?] rather than needing a conversion.
            if needs_context(n) or (n.ty != result and not is_scalar(n.ty)):
                t = self.check_expr(n, result)
                if not assignable(t, result):
                    raise self.error(f"{what} have different types: {t} and {result}", n)
        return result

    def literal_shape(self, e: A.Expr) -> Type:
        """What a context-dependent literal can tell us: [None] is a list of None, [] a list of UNKNOWN."""
        match e:
            case A.NoneLit():
                return NONE
            case A.ListLit(elts) | A.SetLit(elts):
                inner = UNKNOWN
                for x in elts:
                    inner = widen(inner, self.literal_shape(x)) or inner
                return ListType(inner) if isinstance(e, A.ListLit) else SetType(inner)
            case A.DictLit(keys, values):
                key = self.join_all([self.check_expr(k) for k in keys], keys, "dict keys") if keys else UNKNOWN
                value = UNKNOWN
                for v in values:
                    value = widen(value, self.literal_shape(v)) or value
                return DictType(key, value)
        return UNKNOWN

    def join_all(self, types: list[Type], nodes: list[A.Expr], what: str) -> Type:
        """The common type of a literal's items. Nested container literals may be widened
        to fit each other, e.g. [[1.0, None], [3.0]] is a list[list[float?]]."""
        result = types[0]
        for t, node in zip(types[1:], nodes[1:]):
            joined = join(result, t) or widen(result, t)
            if joined is None:
                raise self.error(f"{what} have different types: {result} and {t}", node)
            result = joined
        if result == NONE:
            raise self.error(f"{what} can't all be None", nodes[0])
        for t, node in zip(types, nodes):
            if not assignable(t, result):
                rechecked = self.check_expr(node, result)  # a nested literal adopts the wider type
                if not assignable(rechecked, result):
                    raise self.error(f"{what} have different types: {rechecked} and {result}", node)
        return result

    def check_comprehension(self, gens: list[A.Comprehension], check_element):
        """Comprehension variables live in their own scope, like Python 3."""
        outer = self.state
        self.state = outer.copy()
        for gen in gens:
            iter_type = self.check_expr(gen.iter)
            elem = self.loop_element(gen.iter, iter_type)
            self.bind_comprehension_target(gen.target, elem, gen.iter)
            for cond in gen.ifs:
                self.state, _ = self.check_condition(cond)
        result = check_element()
        self.state = outer
        return result

    def bind_comprehension_target(self, target: A.Expr, t: Type, value: A.Expr) -> None:
        match target:
            case A.Name(name):
                var = Var(name, name, t, "comp", target.loc)
                self.state.names[name] = Bound(var, t)
                target.sym = var
                target.ty = t
            case A.TupleLit(elts) if isinstance(t, TupleType) and len(t.elts) == len(elts):
                for elt, et in zip(elts, t.elts):
                    self.bind_comprehension_target(elt, et, value)
                target.ty = t
            case _:
                raise self.error(f"can't unpack {t} into this", target)

    def check_unary(self, e: A.UnaryOp, op: str, operand: A.Expr) -> Type:
        if op == "not":
            self.check_condition(operand)
            return BOOL
        t = self.check_expr(operand)
        if op == "~" and isinstance(t, StructType) and t.enum is not None and t.enum.flag:
            return t  # ~P.R: the other members
        t = enum_mixin(t) or t  # -N.ONE: an IntEnum member is an int
        if op in ("-", "+") and (is_numeric(t) or t in (TIMEDELTA, builtins.NORMAL_DIST)):
            return t
        if op == "~" and t == INT:
            return INT
        if m := self.dunder(t, UNARY_DUNDERS[op]):  # -vec -> vec.__neg__()
            e.dunder = Dunder(m)
            return m.ret
        raise self.error(f"bad operand type for unary {op}: {t}{self.dunder_hint(t, UNARY_DUNDERS[op])}", e)

    def binop_type(self, op: str, l: Type, r: Type, e: A.Node) -> Type:
        if (flag := enum_flag_op(op, l, r)) is not None:
            return flag  # P.R | P.W: a flag; IntFlag.R | 8 too
        if enum_mixin(l) or enum_mixin(r):  # N.ONE + 1: an IntEnum member is an int
            return self.binop_type(op, enum_mixin(l) or l, enum_mixin(r) or r, e)
        name = BINARY_DUNDERS.get(op)
        if name and (isinstance(l, StructType) or isinstance(r, StructType)):
            if (m := self.dunder(l, f"__{name}__")) and accepts(m, r):  # a + b -> a.__add__(b)
                e.dunder = Dunder(m)
                return m.ret
            if (m := self.dunder(r, f"__r{name}__")) and accepts(m, l):  # 2 * v -> v.__rmul__(2)
                e.dunder = Dunder(m, reflected=True)
                return m.ret
            owner = l if isinstance(l, StructType) else r
            raise self.error(f"unsupported operand types for {op}: {l} and {r}{self.dunder_hint(owner, f'__{name}__')}", e)
        numeric = is_numeric(l) and is_numeric(r)
        widened = FLOAT if FLOAT in (l, r) else INT
        if op in ("+", "-", "|", "&") and l == r and isinstance(l, CounterType):
            return l  # Counter arithmetic keeps positive counts
        if op == "/" and PATH in (l, r) and {l, r} <= {PATH, STR}:
            return PATH  # Path("docs") / "logo.svg"
        if l in DATETIME_TYPES or r in DATETIME_TYPES:
            if (result := datetime_arithmetic(op, l, r)) is not None:
                return result
            raise self.error(f"unsupported operand types for {op}: {l} and {r}", e)
        if builtins.NORMAL_DIST in (l, r):
            if normal_dist_arithmetic(op, l, r):
                return builtins.NORMAL_DIST
            raise self.error(f"unsupported operand types for {op}: {l} and {r}", e)
        match op:
            case "+":
                if numeric:
                    return widened
                if l == r and (l in (STR, BYTES) or isinstance(l, ListType)):
                    return l
                if isinstance(l, TupleType) and isinstance(r, TupleType):
                    return TupleType(l.elts + r.elts)
                if isinstance(l, VarTupleType) and l == r:
                    return l
            case "-":
                if numeric:
                    return widened
                if l == r and isinstance(l, SetType):
                    return l
            case "*":
                if numeric:
                    return widened
                if (l in (STR, BYTES) or isinstance(l, ListType)) and r == INT:
                    return l
                if l == INT and (r in (STR, BYTES) or isinstance(r, ListType)):
                    return r
            case "/":
                if numeric:
                    return FLOAT
            case "//" | "%":
                if numeric:
                    return widened
                if op == "%" and l == STR:
                    raise self.error("'%' formatting isn't supported; use an f-string: f\"{x}\"", e)
            case "**":
                if numeric:
                    return widened
            case "<<" | ">>":
                if l == INT and r == INT:
                    return INT
            case "&" | "|" | "^":
                if l == INT and r == INT:
                    return INT
                if l == BOOL and r == BOOL:
                    return BOOL
                if l == r and isinstance(l, SetType):
                    return l
                if op == "|" and l == r and type(l) is DictType:
                    return l  # the items of both, the right one's winning
        if isinstance(l, OptionalType) or isinstance(r, OptionalType):
            maybe = l if isinstance(l, OptionalType) else r
            try:  # would it work once None is ruled out?
                self.binop_type(op, strip_optional(l), strip_optional(r), A.BinOp(op, None, None, loc=e.loc))
            except CheckError:
                pass
            else:
                operand = e.left if maybe is l else e.right
                raise self.error(
                    f"{maybe} might be None; check it first, e.g. `if {describe_short(operand)} is not None:`", operand
                )
        hint = ""
        if {l, r} == {STR, BYTES}:
            hint = " (convert with s.encode() or b.decode())"
        elif op == "+" and STR in (l, r):
            hint = " (convert with str(...), or use an f-string)"
        raise self.error(f"unsupported operand types for {op}: {l} and {r}{hint}", e)

    def check_boolop(self, e: A.BoolOp) -> Type:
        """`a and b` / `a or b` as values. As in Python they return an operand,
        so both sides must have compatible types. `x or default` with x: T? gives T."""
        left_true, left_false = self.check_condition(e.left)
        lt = e.left.ty
        self.state = left_true if e.op == "and" else left_false
        rt = self.check_expr(e.right)
        self.check_truthy(rt, e.right)
        self.state = merge([left_false if e.op == "and" else left_true, self.state])
        if lt == BOOL and rt == BOOL:
            return BOOL
        if e.op == "or" and isinstance(lt, OptionalType) and assignable(rt, lt.inner):
            return lt.inner
        joined = join(lt, rt)
        if joined is None:
            raise self.error(f"both sides of '{e.op}' should have the same type, not {lt} and {rt}", e)
        return joined

    def check_compare(self, e: A.Compare) -> Type:
        left = e.left
        lt = self.check_expr(left)
        e.dunder = []
        for op, right in zip(e.ops, e.comparators):
            rt = self.check_expr(right)
            # N.ONE < 2: an IntEnum member compares as its value (codegen decays it the same way)
            lv = enum_mixin(lt) if enum_decays(op, lt, rt) else lt
            rv = enum_mixin(rt) if enum_decays(op, rt, lt, right=True) else rt
            e.dunder.append(self.comparison_dunder(op, lv, rv) or self.check_comparison(op, lv, rv, left, right, e))
            left, lt = right, rt
        return BOOL

    def comparison_dunder(self, op: str, lt: Type, rt: Type) -> Dunder | None:
        """a < b -> a.__lt__(b), else the reflection b.__gt__(a); a != b -> not a.__eq__(b)
        without __ne__; x in c -> c.__contains__(x). None: use the built-in rules."""
        if op in ("in", "not in"):
            if (m := self.dunder(rt, "__contains__")) and accepts(m, lt):
                return Dunder(m, reflected=True, negate=op == "not in")
            return None
        if op not in COMPARE_DUNDERS:
            return None
        name, reflected_name = COMPARE_DUNDERS[op]
        if (m := self.dunder(lt, name)) and accepts(m, rt):
            return Dunder(m)
        if (m := self.dunder(rt, reflected_name)) and accepts(m, lt):
            return Dunder(m, reflected=True)
        if op == "!=":
            if (m := self.dunder(lt, "__eq__")) and accepts(m, rt):
                return Dunder(m, negate=True)
            if (m := self.dunder(rt, "__eq__")) and accepts(m, lt):
                return Dunder(m, reflected=True, negate=True)
        return None

    def check_comparison(self, op: str, lt: Type, rt: Type, left: A.Expr, right: A.Expr, e: A.Compare) -> None:
        if op in ("<", ">", "<=", ">="):
            ordered = (is_numeric(lt) and is_numeric(rt)) or (
                lt == rt and (lt in (STR, BYTES, PATH, DATE, TIME, DATETIME, TIMEDELTA, UUID_T)
                              or isinstance(lt, (TupleType, ListType, VarTupleType, SetType, CmpKeyType)))  # (sets: subset and superset)
            ) or mixed_tuples(lt, rt)
            if not ordered:
                owner = lt if isinstance(lt, StructType) else rt
                hint = self.dunder_hint(owner, COMPARE_DUNDERS[op][0])
                raise self.error(f"'{op}' isn't supported between {lt} and {rt}{hint}", e)
        elif op in ("==", "!="):
            if not (is_numeric(lt) and is_numeric(rt)) and join(lt, rt) is None:
                raise self.error(f"comparing {lt} with {rt} using '{op}' is always {op == '!='}", e)
        elif op in ("in", "not in"):
            match rt:
                case _ if rt == builtins.HTTP_HEADERS:  # "Content-Type" in headers
                    ok = lt == STR
                case ListType(elem) | SetType(elem) | DequeType(elem) | VarTupleType(elem) | GeneratorType(elem) | IterType(
                    elem, "range" | "keys" | "values" | "items"
                ):
                    ok = assignable(lt, elem)
                case DictType(key):
                    ok = assignable(lt, key)
                case TupleType(elts):
                    ok = any(join(lt, t) for t in elts)
                case _ if rt == STR:
                    ok = lt == STR
                case _ if rt == BYTES:
                    ok = lt in (BYTES, INT)
                case _ if rt == JSON_VALUE:
                    ok = lt == STR  # a key of a JSON object
                case ClassRefType(st) if st.enum is not None:
                    ok = lt == st  # Color.RED in Color
                case StructType() if rt.enum is not None and rt.enum.flag:
                    ok = lt == rt  # P.R in perms
                case _:
                    raise self.error(f"'{op}' needs a list, set, dict, tuple or str on the right, not {rt}", right)
            if not ok:
                raise self.error(f"a {lt} can never be in a {rt}", e)
        elif op in ("is", "is not"):
            is_none_check = (isinstance(right, A.NoneLit) and (isinstance(lt, OptionalType) or lt == NONE)) or (
                isinstance(left, A.NoneLit) and isinstance(rt, OptionalType)
            )
            enum_t = strip_optional(lt) if isinstance(strip_optional(lt), StructType) else strip_optional(rt)
            lo, ro = strip_optional(lt), strip_optional(rt)  # node.parent is root: either may be None
            same_object = (isinstance(enum_t, StructType) and enum_t.enum is not None and join(lt, rt) is not None) or lo == ro and (
                (isinstance(lo, StructType) and lo.kind == "class")
                or isinstance(lo, (ListType, DictType, SetType, DequeType, CounterType, DefaultDictType))
            )
            if not (is_none_check or same_object):
                if isinstance(right, A.NoneLit):
                    raise self.error(f"{lt} can never be None (only T? types can)", e)
                raise self.error(
                    f"'{op}' is for None checks, class instances, lists, dicts and sets; use '==' to compare values", e
                )

    def check_attribute(self, e: A.Attribute, value: A.Expr, attr: str, expected: Type | None = None) -> Type:
        if (member := self.class_member(e)) is not None:  # timezone.utc
            if isinstance(member, builtins.Function):
                raise self.error(f"{member.name}() can only be called here (functions aren't values yet)", e)
            e.sym = member
            return member.type
        if (
            isinstance(value, A.Name)
            and value.id in ("str", "list", "dict", "set")
            and value.id not in self.state.names
            and value.id not in self.scope.assigned
        ):
            return self.callable_as_value(e, expected, f"{value.id}.{attr}")  # key=str.lower
        if (isinstance(value, A.Name) and value.id not in self.state.names and value.id not in self.scope.assigned
                and (cls := self.lookup_struct(value.id)) is not None and (ca := cls.find_class_attr(attr)) is not None):
            value.ty, value.sym = ClassRefType(cls), cls
            e.sym = ("class_attr_of", cls, ca)  # Handler.version
            return ca.type
        vt = self.check_expr(value)
        if isinstance(vt, ModuleType):
            if isinstance(value.sym.members.get(attr), builtins.Function):
                return self.callable_as_value(e, expected, f"{value.sym.name}.{attr}")
            return self.module_member(e, value.sym, attr)
        if isinstance(vt, ClassRefType) and vt.st.enum is not None:  # Color.RED
            members = vt.st.enum.members
            if attr not in members:
                close = difflib.get_close_matches(attr, list(members), n=1)
                hint = f"; did you mean '{close[0]}'?" if close else ""
                raise self.error(f"type object '{vt.st.name}' has no attribute '{attr}'{hint}", e)
            e.sym = ("enum_member", vt.st, members[attr])
            return vt.st
        if isinstance(vt, StructType) and vt.enum is not None and attr in ("name", "value", "_name_", "_value_"):
            e.sym = ("enum_attr", attr.strip("_"))  # Color.RED.name, c.value
            if attr.strip("_") == "value":
                return vt.enum.value_type
            if vt.enum.flag and (path := attr_path(e)) in self.state.attrs:
                return self.state.attrs[path]  # narrowed: `if m.name is not None:`
            return OptionalType(STR) if vt.enum.flag else STR  # (a flag of no members has no name)
        if isinstance(vt, StructType):
            if (getter := vt.find_method(attr)) and getter.kind == "getter":  # obj.area -> obj.area()
                e.sym = ("property", getter)
                return getter.ret
            if f := vt.find_field(attr):
                self.check_synchronized_access(vt, value, attr, e)
                e.sym = f
                if (path := attr_path(e)) in self.state.attrs:
                    return self.state.attrs[path]  # narrowed: `if u.address is not None:`, isinstance()
                return f.type
            if (method := vt.find_method(attr)) and method.name != "__init__":
                e.sym = method  # a bound method: remembers its object
                return FuncType(tuple(p.type for p in method.params), method.ret)
            if (ca := vt.find_class_attr(attr)) is not None:  # self.version: the object's class's value
                e.sym = ("class_attr", ca)
                return ca.type
            if vt.enum is not None:
                raise self.error(f"'{vt.name}' object has no attribute '{attr}' (an enum member has .name, .value "
                                 f"and its class's methods)", e)
            raise self.error(f"{vt.name} has no field '{attr}'", e)
        if isinstance(vt, ClassRefType) and (ca := vt.st.find_class_attr(attr)) is not None:  # Handler.version
            e.sym = ("class_attr_of", vt.st, ca)
            return ca.type
        if isinstance(vt, HTTPServerType) and attr in ("serve_forever", "handle_request", "shutdown"):
            e.sym = ("server_method", attr)  # target=server.serve_forever
            return FuncType((), NONE)
        if isinstance(vt, SyncType) and vt.kind == "Thread" and attr in builtins.THREAD_ATTRIBUTES:
            e.sym = ("thread_attr", attr)
            return builtins.THREAD_ATTRIBUTES[attr]
        if (attrs := builtins.type_attributes(vt)) is not None:  # m.string, pattern.groups
            if attr not in attrs and isinstance(vt, NamespaceType):
                known = ", ".join(name for name, _ in vt.fields) or "none"
                raise self.error(f"the parsed arguments have no '{attr}' (the parser's arguments are: {known})", e)
            if attr not in attrs:
                raise self.error(f"{vt} has no attribute '{attr}'", e)
            try:
                t = attrs[attr](vt)
            except builtins.AttributeUnavailable as problem:
                raise self.error(f"{vt}.{attr}: {problem}", e) from None
            e.sym = ("builtin_attr", attr)
            if (path := attr_path(e)) in self.state.attrs:
                return self.state.attrs[path]  # narrowed: `if args.command == "add":`
            return t
        if isinstance(vt, OptionalType):
            raise self.error(
                f"{vt} might be None; check it first, e.g. `if {describe_short(value)} is not None:`", value
            )
        raise self.error(f"{vt} has no attribute '{attr}'", e)

    def check_synchronized_access(self, owner: StructType, obj: A.Expr, attr: str, node: A.Node) -> None:
        """A Synchronized object's fields are only reachable inside its own methods (which hold
        its lock), through `self`: `other.balance` could race with another thread."""
        if not threads.is_synchronized(owner):
            return
        if isinstance(obj, A.Name) and isinstance(obj.sym, Var) and obj.sym.name == "self" and obj.sym.kind == "param":
            return
        raise self.error(
            f"{owner.name} is Synchronized, so its fields can only be used inside its methods (through self); "
            f"add a method that reads or updates '{attr}'", node,
        )

    def module_member(self, e: A.Expr, mod: builtins.Module, member: str) -> Type:
        m = mod.members.get(member)
        if m is None:
            raise self.error(builtins.missing_member(mod, member), e)
        if isinstance(m, builtins.Function):
            raise self.error(f"'{mod.name}.{member}' can only be called here (functions aren't values yet)", e)
        if isinstance(m, StructType) and m.enum is not None:
            e.sym = m  # an enum class, from another module
            return ClassRefType(m)
        if isinstance(m, StructType):
            raise self.error(f"'{mod.name}.{member}' is a class; it can be called, raised or caught", e)
        if isinstance(m, builtins.Module):
            e.sym = m
            return ModuleType(m.name)
        if isinstance(m, FuncInfo):  # utils.helper as a value
            e.sym = m
            return FuncType(tuple(p.type for p in m.params), m.ret)
        if isinstance(m, Var):  # utils.LIMIT
            e.sym = m
            return m.type
        if isinstance(m, (builtins.TypeAlias, builtins.NamedType)):
            raise self.error(f"'{member}' is a type; it can only be used in annotations", e)
        e.sym = m
        return m.type

    def check_index(self, e: A.Index, value: A.Expr, index: A.Expr) -> Type:
        vt = self.check_expr(value)
        if isinstance(vt, ClassRefType) and vt.st.enum is not None:  # Color["RED"]: by name
            self.expect_type(index, STR, f"{vt.st.name}[...] (a member's name)")
            e.sym = ("enum_name", vt.st)
            return vt.st
        if m := self.dunder(vt, "__getitem__"):  # obj[k] -> obj.__getitem__(k)
            self.expect_type(index, m.params[0].type, f"{vt.name} index")
            e.dunder = Dunder(m)
            return m.ret
        if isinstance(index, A.Slice):
            if not (isinstance(vt, (ListType, VarTupleType)) or vt in (STR, BYTES)):
                raise self.error(f"{vt} can't be sliced", e)
            for part in (index.lower, index.upper, index.step):
                if part is not None:
                    self.expect_type(part, INT, "slice index")
            index.ty = vt
            return vt
        match vt:
            case ListType(elem):
                self.expect_type(index, INT, "list index")
                return elem
            case DequeType(elem):
                self.expect_type(index, INT, "deque index")
                return elem
            case VarTupleType(elem):
                self.expect_type(index, INT, "tuple index")
                return elem
            case _ if vt == builtins.HTTP_HEADERS:  # headers["Content-Type"]: None if missing
                self.expect_type(index, STR, "header name")
                return OptionalType(STR)
            case MatchType():  # m[1] is m.group(1)
                ctx = builtins.CallContext(self, A.Call(value, [index], loc=e.loc), "re.Match[...]", None, vt)
                return builtins.match_group_arg(ctx, index)
            case DictType(key, val):
                self.expect_type(index, key, "dict key")
                return val
            case TupleType(elts):
                i = constant_int(index)
                if i is None:
                    self.expect_type(index, INT, "tuple index")
                    if len(set(elts)) == 1:
                        return elts[0]
                    raise self.error("a tuple with mixed types can only be indexed by a constant", index)
                if not -len(elts) <= i < len(elts):
                    raise self.error(f"tuple index {i} is out of range for {vt}", index)
                self.check_expr(index)
                return elts[i]
            case _ if vt == STR:
                self.expect_type(index, INT, "string index")
                return STR
            case _ if vt == BYTES:
                self.expect_type(index, INT, "bytes index")
                return INT
            case _ if vt == JSON_VALUE:
                it = self.check_expr(index)
                if it not in (INT, STR):
                    raise self.error(f"a json.Value is indexed by int (arrays) or str (objects), not {it}", index)
                return JSON_VALUE
            case OptionalType():
                raise self.error(f"{vt} might be None; check it before indexing", value)
        raise self.error(f"{vt} can't be indexed", e)

    def expect_type(self, e: A.Expr, t: Type, what: str) -> None:
        actual = self.check_expr(e, t)
        if not assignable(actual, t):
            raise self.error(f"{what} must be {t}, not {actual}", e)

    # =========================================================================
    # Calls
    # =========================================================================

    def check_call(self, e: A.Call, expected: Type | None) -> Type:
        func = e.func
        if isinstance(func, A.Index) and (kind := self.sync_kind_of(func.value)) is not None:
            explicit = tuple(self.resolve_type(expr_to_type(x)) for x in type_arg_exprs(func.index))
            return self.construct_sync(e, kind, explicit, expected)  # queue.Queue[int]()
        if isinstance(func, A.Index) and (kind := self.collection_kind_of(func.value)) is not None:
            args = [self.resolve_type(expr_to_type(x)) for x in type_arg_exprs(func.index)]
            return self.construct_collection(e, self.collection_type(kind, args, func), expected)  # deque[int]()
        if (gen := self.generic_named(func)) is not None:
            return self.call_generic(e, gen, None, expected)
        if isinstance(func, A.Index) and (gen := self.generic_named(func.value)) is not None:
            explicit = tuple(self.resolve_type(expr_to_type(x)) for x in type_arg_exprs(func.index))
            return self.call_generic(e, gen, explicit, expected)
        if self.is_builtin_name(func, "isinstance"):
            return self.check_isinstance(e)
        if (isinstance(func, A.Attribute) and isinstance(func.value, A.Name)
                and (func.value.id, func.attr) in builtins.TYPE_FUNCTIONS and self.is_builtin_name(func.value, func.value.id)):
            # str.maketrans(...), bytes.fromhex(...): called on the type itself
            ctx = builtins.CallContext(self, e, f"{func.value.id}.{func.attr}()", expected)
            e.sym = CallTarget("builtin", f"{func.value.id}.{func.attr}")
            return builtins.TYPE_FUNCTIONS[(func.value.id, func.attr)](ctx)
        if isinstance(func, A.Attribute) and isinstance(func.value, A.Call) and self.is_builtin_name(func.value.func, "super"):
            return self.check_super_call(e, func.value, func.attr)
        own = self.scope.info.var if self.scope.info is not None else None
        if own is not None and isinstance(func, A.Name) and (entry := self.state.names.get(func.id)):
            if isinstance(entry, Bound) and entry.var is own:
                # A nested function calling itself: compiled without capturing itself
                # (which would make a reference cycle, and leak).
                func.sym, func.ty = own, entry.ty
                ret = self.call_value(e, entry.ty)
                e.sym = CallTarget("self_call", self.scope.info)
                return ret
        if isinstance(func, A.Name) and func.id not in self.state.names and func.id not in self.scope.assigned:
            name = func.id
            if name in self.functions:
                info = self.functions[name]
                e.sym = CallTarget("func", info, self.match_args(e, info.params, f"{name}()"))
                return info.ret
            if st := self.lookup_struct(name):
                return self.check_constructor(e, st)
            if name in self.imported and name not in self.module_assign_counts:
                mod, member = self.imported[name]
                return self.check_module_call(e, mod, member, expected)
            if name in builtins.FUNCTIONS and name not in self.module_assign_counts:
                ctx = builtins.CallContext(self, e, f"{name}()", expected)
                e.sym = CallTarget("builtin", name)
                return builtins.FUNCTIONS[name](ctx)
        if isinstance(func, A.Attribute) and func.attr == "from_iterable" and self.is_itertools_chain(func.value):
            mod = builtins.MODULES["itertools"]  # itertools.chain.from_iterable(xss)
            e.sym = CallTarget("module_func", (mod, "chain.from_iterable"))
            return mod.members["chain.from_iterable"].check(builtins.CallContext(self, e, "chain.from_iterable()", expected))
        if isinstance(func, A.Attribute) and (member := self.class_member(func)) is not None:
            if not isinstance(member, builtins.Function):
                raise self.error(f"{member.name} isn't a function", func)
            e.sym = CallTarget("class_func", member)  # Path.cwd(), datetime.now(), date.fromisoformat(s)
            return member.check(builtins.CallContext(self, e, f"{member.name}()", expected))
        if isinstance(func, A.Attribute):
            if (
                isinstance(func.value, A.Name) and func.value.id not in self.state.names
                and func.value.id not in self.scope.assigned and (cls := self.lookup_struct(func.value.id))
            ):
                owner = ClassRefType(cls)  # Point.origin()
                func.value.ty, func.value.sym = owner, cls
            else:
                owner = self.check_expr(func.value)
            if isinstance(owner, ClassRefType) or (
                isinstance(owner, StructType) and (m := owner.find_method(func.attr)) and m.kind in ("static", "classmethod")
            ):
                st = owner.st if isinstance(owner, ClassRefType) else owner
                method = st.find_method(func.attr)
                if method is None or method.kind not in ("static", "classmethod"):
                    raise self.error(
                        f"{st.name}.{func.attr}() needs an instance: only @staticmethod and @classmethod "
                        f"methods can be called on the class", func,
                    )
                e.sym = CallTarget("static_method", method, self.match_args(e, method.params, f"{func.attr}()"))
                return method.ret
            if isinstance(owner, ModuleType):
                return self.check_module_call(e, func.value.sym, func.attr, expected)
            if isinstance(owner, StructType):
                method = owner.find_method(func.attr)
                if method is None and enum_mixin(owner) is not None:  # S.A.upper(): a StrEnum member is a str
                    func.value = A.Attribute(func.value, "value", loc=func.value.loc)
                    return self.check_call(e, expected)
                if method is None and (handler := builtins.method_for(owner, func.attr)) is not None:
                    ctx = builtins.CallContext(self, e, f"{owner.name}.{func.attr}()", expected, receiver=owner)
                    e.sym = CallTarget("builtin_method", (owner, func.attr))  # e.g. HTTPError.read()
                    return handler(ctx)
                if method is None or method.name == "__init__":
                    field = owner.find_field(func.attr)
                    if field is None:
                        raise self.error(f"{owner.name} has no method '{func.attr}'", func)
                    if not isinstance(strip_optional(field.type), FuncType):
                        raise self.error(f"'{func.attr}' is a field, not a method", func)
                    return self.call_value(e, self.check_expr(func))  # a field holding a function
                e.sym = CallTarget("method", method, self.match_args(e, method.params, f"{func.attr}()"))
                return method.ret
            handler = builtins.method_for(owner, func.attr)
            if handler is None:
                if isinstance(owner, OptionalType):
                    raise self.error(
                        f"{owner} might be None; check it first, e.g. "
                        f"`if {describe_short(func.value)} is not None:`", func.value,
                    )
                raise self.error(f"{owner} has no method '{func.attr}'", func)
            ctx = builtins.CallContext(self, e, f"{type_family(owner)}.{func.attr}()", expected, receiver=owner)
            e.sym = CallTarget("builtin_method", (owner, func.attr))
            result = handler(ctx)
            if isinstance(result, SyncType) and result.kind in ("RWRead", "RWWrite") and id(e) not in self.with_contexts:
                raise self.error(f"{func.attr}() gives a view of the data that's only valid while locked: "
                                 f"use it in a with statement, `with {describe_short(func.value)}.{func.attr}() as data:`", e)
            if isinstance(owner, SyncType) and owner.kind in ("Mutex", "RWMutex") and func.attr == "set":
                value = e.args[0] if e.args else next((k.value for k in e.keywords if k.name == "value"), None)
                self.move_into_mutex(value, describe_short(func.value))
            return result
        return self.call_value(e, self.check_expr(func))

    # ---- threading / queue ----------------------------------------------------

    def sync_kind_named(self, name: str) -> str | None:
        """`Lock` (imported from threading) or `queue.Queue` as a type name."""
        if "." in name:
            mod_name, _, member = name.rpartition(".")
            mod = self.modules.get(mod_name)
            m = mod.members.get(member) if mod is not None else None
        elif name in self.imported:
            mod, member = self.imported[name]
            m = mod.members.get(member)
        else:
            return None
        return m.kind if isinstance(m, builtins.SyncTypeDef) else None

    def sync_kind_of(self, e: A.Expr) -> str | None:
        if isinstance(e, A.Name) and e.id not in self.state.names:
            return self.sync_kind_named(e.id)
        if isinstance(e, A.Attribute) and (path := attr_path(e)):
            return self.sync_kind_named(".".join(path))
        return None

    def builtin_class(self, e: A.Expr) -> Type | None:
        """The built-in type a name refers to as a class: `Path`, `datetime.date`, `timezone`."""
        f = None
        if isinstance(e, A.Name) and e.id not in self.state.names and e.id in self.imported:
            mod, member = self.imported[e.id]
            f = mod.members.get(member)
        elif isinstance(e, A.Attribute) and isinstance(e.value, A.Name) and e.value.id in self.modules:
            f = self.modules[e.value.id].members.get(e.attr)
        if isinstance(f, builtins.Function) and f.as_type in builtins.CLASS_MEMBERS:
            return f.as_type
        return None

    def is_itertools_chain(self, e: A.Expr) -> bool:
        chain = builtins.MODULES["itertools"].members["chain"]
        if isinstance(e, A.Name) and e.id not in self.state.names and e.id in self.imported:
            mod, member = self.imported[e.id]
            return mod.members.get(member) is chain
        if isinstance(e, A.Attribute) and isinstance(e.value, A.Name) and e.value.id in self.modules:
            return self.modules[e.value.id].members.get(e.attr) is chain
        return False

    def class_member(self, e: A.Attribute):
        """A member of a built-in class itself (`datetime.now`, `timezone.utc`), or None."""
        cls = self.builtin_class(e.value)
        if cls is None:
            return None
        member = builtins.CLASS_MEMBERS[cls].get(e.attr)
        if member is None:
            raise self.error(f"type object '{cls}' has no attribute '{e.attr}'", e)
        return member

    # ---- collections ----------------------------------------------------------------

    def module_member_named(self, name: str):
        """What `name` (imported, or `module.member`) refers to in a module, or None."""
        if "." in name:
            mod_name, _, member = name.rpartition(".")
            mod = self.modules.get(mod_name)
            if mod is None and "." in mod_name:  # concurrent.futures.Future after `import concurrent.futures`
                head, *rest = mod_name.split(".")
                mod = self.modules.get(head)
                for part in rest:
                    mod = mod.members.get(part) if mod is not None else None
            return mod.members.get(member) if isinstance(mod, builtins.Module) else None
        if name in self.imported:
            mod, member = self.imported[name]
            return mod.members.get(member)
        return None

    def collection_kind_named(self, name: str) -> str | None:
        """`deque` (imported from collections) or `collections.deque` as a name."""
        if "." in name:
            mod_name, _, member = name.rpartition(".")
            mod = self.modules.get(mod_name)
            m = mod.members.get(member) if mod is not None else None
        elif name in self.imported:
            mod, member = self.imported[name]
            m = mod.members.get(member)
        else:
            return None
        return m.kind if isinstance(m, builtins.CollectionTypeDef) else None

    def collection_kind_of(self, e: A.Expr) -> str | None:
        if isinstance(e, A.Name) and e.id not in self.state.names:
            return self.collection_kind_named(e.id)
        if isinstance(e, A.Attribute) and (path := attr_path(e)):
            return self.collection_kind_named(".".join(path))
        return None

    def collection_type(self, kind: str, args: list[Type], node: A.Node) -> Type:
        arity = {"defaultdict": 2, "Counter": 1, "deque": 1}[kind]
        example = {"defaultdict": "defaultdict[str, list[int]]", "Counter": "Counter[str]", "deque": "deque[int]"}[kind]
        if len(args) != arity:
            raise self.error(f"{kind} takes {plural(arity, 'type argument')}, e.g. {example}", node)
        if kind == "deque":
            return DequeType(args[0])
        self.check_hashable(args[0], f"{kind} keys", node)
        return DefaultDictType(args[0], args[1]) if kind == "defaultdict" else CounterType(args[0], INT)

    def construct_collection(self, e: A.Call, kind_or_type: str | Type, expected: Type | None) -> Type:
        """defaultdict(list), Counter(words), deque(items, maxlen=3) -- and their [T] forms.
        The type comes from explicit type arguments, the expected type, or the contents."""
        if isinstance(kind_or_type, str):
            kind, t = kind_or_type, None
        else:
            t = kind_or_type
            kind = {DefaultDictType: "defaultdict", CounterType: "Counter", DequeType: "deque"}[type(t)]
        want = strip_optional(expected) if expected is not None else None
        if t is None:  # from the variable's annotation (a plain dict annotation also works)
            match kind, want:
                case "defaultdict", DictType(k, v) if not isinstance(want, CounterType):
                    t = DefaultDictType(k, v)
                case "Counter", DictType(k, v) if v == INT:
                    t = CounterType(k, INT)
                case "deque", DequeType():
                    t = want
        kw = {k.name: k.value for k in e.keywords}
        extra: dict = {}
        if kind == "defaultdict":
            if kw:
                raise self.error("defaultdict() takes positional arguments: defaultdict(factory[, initial dict])", e)
            if len(e.args) > 2:
                raise self.error(f"defaultdict() takes at most 2 arguments ({len(e.args)} given)", e)
            if t is None:
                raise self.error(
                    "a defaultdict needs its key and value types: annotate the variable, "
                    "e.g. `d: defaultdict[str, list[int]] = defaultdict(list)`", e,
                )
            if e.args:
                extra.update(self.check_default_factory(e.args[0], t.value))
            if len(e.args) == 2:
                self.expect_type(e.args[1], DictType(t.key, t.value), "defaultdict's initial contents")
        elif kind == "Counter":
            if kw:
                raise self.error("Counter(a=1) isn't supported; use Counter({'a': 1})", e)
            if len(e.args) > 1:
                raise self.error(f"Counter() takes at most 1 argument ({len(e.args)} given)", e)
            if e.args:
                arg = e.args[0]
                at = self.check_expr(arg, DictType(t.key, INT) if t is not None else None)
                if isinstance(at, DictType) and at.value == INT and not isinstance(at, CounterType) or (
                    isinstance(at, CounterType)
                ):
                    extra["counts"] = True  # Counter({'a': 2}): copy the counts
                    key = at.key
                else:
                    key = self.loop_element(arg, at)
                    self.check_hashable(key, "Counter keys", arg)
                if t is None:
                    t = CounterType(key, INT)
                elif not assignable(key, t.key):
                    raise self.error(f"this Counter counts {t.key}, not {key}", arg)
            if t is None:
                raise self.error(
                    "a Counter needs to know what it counts: `Counter(words)`, or annotate the variable "
                    "(`c: Counter[str] = Counter()`)", e,
                )
        else:  # deque
            for name in kw:
                if name not in ("maxlen",):
                    raise self.error(f"deque() got an unexpected keyword argument '{name}'", e)
            if len(e.args) > 2:
                raise self.error(f"deque() takes at most 2 arguments ({len(e.args)} given)", e)
            items = e.args[0] if e.args else kw.get("iterable")
            maxlen = e.args[1] if len(e.args) > 1 else kw.get("maxlen")
            if items is not None:
                it = self.check_expr(items, ListType(t.elem) if t is not None else None)
                elem = self.loop_element(items, it)
                if t is None:
                    t = DequeType(elem)
                elif not assignable(elem, t.elem):
                    raise self.error(f"this deque holds {t.elem}, not {elem}", items)
            if maxlen is not None and not isinstance(maxlen, A.NoneLit):
                self.expect_type(maxlen, INT, "deque maxlen")
            extra["items"], extra["maxlen"] = items, maxlen
            if t is None:
                raise self.error(
                    "a deque needs to know what it holds: `deque(items)`, `deque[int]()`, or annotate the "
                    "variable (`d: deque[int] = deque()`)", e,
                )
        e.sym = CallTarget("collection_new", (t, extra))
        return t

    def check_default_factory(self, node: A.Expr, value: Type) -> dict:
        """defaultdict(list) / (int) / (Point) / (lambda: ...): something making a `value`."""
        if isinstance(node, A.Name) and node.id in ("list", "dict", "set", "int", "float", "str", "bool") and (
            node.id not in self.state.names and node.id not in self.scope.assigned
        ):
            fits = {"list": ListType, "dict": DictType, "set": SetType}.get(node.id)
            if fits is not None and not isinstance(value, fits) or fits is None and node.id not in (
                "float" if value == FLOAT else str(value),
            ):
                raise self.error(f"this defaultdict holds {value}, but {node.id}() doesn't make one", node)
            made = self.check_expr(A.Call(A.Name(node.id, loc=node.loc), [], loc=node.loc), value)
            if not assignable(made, value):
                raise self.error(f"{node.id}() makes {made}, but this defaultdict holds {value}", node)
            return {"factory": None, "factory_repr": f"<class '{node.id}'>"}  # the value type's default
        ft = self.check_expr(node, FuncType((), value))
        if not isinstance(ft, FuncType) or ft.params or not assignable(ft.ret, value):
            raise self.error(f"a defaultdict factory must take no arguments and return {value}, not {ft}", node)
        if isinstance(node, A.Name) and (st := self.lookup_struct(node.id)):
            shown = f"<class '{self.module_name}.{st.name}'>"
        elif isinstance(node, A.Name):
            shown = f"<function {node.id}>"
        else:
            shown = "<function <lambda>>"
        return {"factory": node, "factory_repr": shown}

    def construct_sync(self, e: A.Call, kind: str, explicit: tuple | None, expected: Type | None) -> Type:
        """Lock(), Atomic(0), Mutex(value), Queue[int](maxsize=10), Thread(target=f, args=(...))."""
        want = strip_optional(expected) if expected is not None else None
        type_args = explicit
        if type_args is None and isinstance(want, SyncType) and want.kind == kind:
            type_args = want.args
        if type_args is not None and len(type_args) != SYNC_ARITY[kind]:
            raise self.error(f"{kind} takes {plural(SYNC_ARITY[kind], 'type argument')}", e.func)
        kw = {k.name: k.value for k in e.keywords}
        extra: dict = {}

        def take(position: int, name: str) -> A.Expr | None:
            return e.args[position] if position < len(e.args) else kw.get(name)

        def only(allowed: tuple[str, ...], positional: int) -> None:
            if len(e.args) > positional:
                raise self.error(f"{kind}() takes at most {plural(positional, 'positional argument')}", e)
            for name in kw:
                if name not in allowed:
                    raise self.error(f"{kind}() got an unexpected keyword argument '{name}'", e)

        if kind in ("Lock", "RLock", "Event"):
            only((), 0)
            t = SyncType(kind)
        elif kind == "Atomic":
            only(("value",), 1)
            if (v := take(0, "value")) is not None:
                self.expect_type(v, INT, "Atomic value")
            extra["value"] = v
            t = SyncType(kind)
        elif kind in ("Mutex", "RWMutex"):
            only(("value",), 1)
            v = take(0, "value")
            if v is None and type_args is None:
                raise self.error(f"{kind} needs a starting value, or a type: `{kind}[list[int]]()`", e)
            if v is not None:
                vt = self.check_expr(v, type_args[0] if type_args else None)
                if type_args is None:
                    type_args = (vt,)
                elif not assignable(vt, type_args[0]):
                    raise self.error(f"this {kind} holds {type_args[0]}, not {vt}", v)
            extra["value"] = v
            t = SyncType(kind, type_args)
            self.move_into_mutex(v, None)
        elif kind == "Queue":
            only(("maxsize",), 1)
            if type_args is None:
                raise self.error(
                    "a Queue needs to know what it holds: `queue.Queue[int]()`, or annotate the variable "
                    "(`q: Queue[int] = Queue()`)", e,
                )
            if (m := take(0, "maxsize")) is not None:
                self.expect_type(m, INT, "Queue maxsize")
            extra["maxsize"] = m
            t = SyncType(kind, type_args)
        else:  # Thread
            if e.args:
                raise self.error("pass the function as a keyword: threading.Thread(target=worker, args=(...))", e.args[0])
            only(("target", "args", "name", "daemon"), 0)
            target = kw.get("target")
            if target is None:
                raise self.error("threading.Thread() needs target=<function to run>", e)
            args_node = kw.get("args")
            arg_types: tuple = ()
            if args_node is not None:
                at = self.check_expr(args_node)
                if not isinstance(at, TupleType):
                    raise self.error(f"args must be a tuple, like args=(x,) or args=(a, b), not {at}", args_node)
                arg_types = at.elts
            tt = self.check_expr(target, FuncType(arg_types, None))
            if not isinstance(tt, FuncType):
                raise self.error(f"target must be a function, not {tt}", target)
            if len(tt.params) != len(arg_types) or not all(assignable(a, p) for a, p in zip(arg_types, tt.params)):
                params = ", ".join(map(str, tt.params)) or "no arguments"
                given = ", ".join(map(str, arg_types)) or "none"
                raise self.error(f"target takes ({params}), but args gives ({given})", args_node or target)
            if (name := kw.get("name")) is not None:
                self.expect_type(name, STR, "thread name")
            if (daemon := kw.get("daemon")) is not None:
                self.expect_type(daemon, BOOL, "daemon")
            target_name = target.id if isinstance(target, A.Name) else target.attr if isinstance(target, A.Attribute) else "<lambda>"
            extra = {"target": target, "args": args_node, "name": name, "daemon": daemon,
                     "target_name": target_name, "target_type": tt}
            t = SyncType(kind)
            self.spawns.append((e, self.scope, self.module_name))
        self.check_sync_contents(t, e)
        e.sym = CallTarget("sync_new", (t, extra))
        return t

    def check_sync_contents(self, t: SyncType, node: A.Node) -> None:
        for arg in t.args:
            if reason := threads.unsendable(arg):
                raise self.error(f"a {t.kind} can only hold values that can be copied between threads: {reason}", node)

    def is_builtin_name(self, e: A.Expr, name: str) -> bool:
        return (
            isinstance(e, A.Name) and e.id == name and name not in self.state.names
            and name not in self.scope.assigned and name not in self.functions
        )

    # ---- dunder methods -------------------------------------------------------

    def dunder(self, t: Type, name: str) -> FuncInfo | None:
        """A user type's __name__ method (built-in types' behaviour isn't dunder-based)."""
        if isinstance(t, StructType) and not t.builtin:
            return t.find_method(name)
        return None

    def dunder_hint(self, t: Type, name: str) -> str:
        return f" (define {name} on {t.name})" if isinstance(t, StructType) and not t.builtin else ""

    def check_dunder_signatures(self, st: StructType) -> None:
        for name, m in st.methods.items():
            spec = DUNDER_SIGNATURES.get(name)
            if spec is None:
                continue
            count, ret = spec
            if len(m.params) != count:
                params = ", ".join(["self", *(["other"] if count == 1 else ["key", "value"] if count == 2 else [])])
                raise self.error(f"{name} takes ({params})", m.node)
            if ret is not None and m.ret != ret:
                raise self.error(f"{name} must return {ret}, not {m.ret}", m.node)
            if name == "__iter__" and element_type(m.ret) is None:
                raise self.error(f"__iter__ must return something iterable (like a list), not {m.ret}", m.node)
        if "__hash__" in st.methods and not st.find_method("__eq__") and st.kind != "struct":  # (values compare fields)
            raise self.error("a class with __hash__ also needs __eq__ (equal objects must hash the same)", st.methods["__hash__"].node)

    # ---- decorators --------------------------------------------------------------

    def classify_decorator(self, d: A.Expr) -> tuple[str, A.Expr]:
        """'staticmethod', 'classmethod', 'property', 'setter', 'dataclass', 'cache', 'lru_cache', or 'user'."""
        base = d.func if isinstance(d, A.Call) else d
        if isinstance(base, A.Name) and base.id in ("staticmethod", "classmethod", "property"):
            return base.id, d
        marker = None
        if isinstance(base, A.Name) and base.id in self.imported:
            mod, member = self.imported[base.id]
            marker = mod.members.get(member)
        elif isinstance(base, A.Attribute) and isinstance(base.value, A.Name) and base.value.id in self.modules:
            marker = self.modules[base.value.id].members.get(base.attr)
        if isinstance(marker, builtins.DecoratorName):
            return marker.name, d
        if isinstance(base, A.Attribute) and base.attr == "setter" and isinstance(base.value, A.Name):
            return "setter", d
        return "user", d

    def declare_decorated(self, node: A.FunctionDef) -> A.Assign | None:
        """A top-level def with user decorators is `f = d1(d2(<undecorated f>))`, run where the
        def is. Returns that assignment (the undecorated function gets a hidden name)."""
        kinds = [self.classify_decorator(d)[0] for d in node.decorators]
        for kind, d in zip(kinds, node.decorators):
            if kind in ("staticmethod", "classmethod", "property", "setter", "dataclass"):
                raise self.error(f"@{kind} only makes sense on a method inside a class", d)
        if node.type_params and node.decorators:
            raise self.error("decorators on generic functions aren't supported yet", node.decorators[0])
        if "user" not in kinds:
            return None
        if any(k in ("cache", "lru_cache") for k in kinds):
            raise self.error("functools.cache can't be combined with other decorators yet", node.decorators[0])
        hidden = f"sd_undecorated_{node.name}"
        self.decorated[node.name] = hidden
        self.functions[hidden] = None
        value: A.Expr = A.Name(hidden, loc=node.loc)
        for d in reversed(node.decorators):
            value = A.Call(d, [value], loc=d.loc)
        return A.Assign([A.Name(node.name, loc=node.loc)], value, loc=node.loc)

    def check_cacheable(self, info: FuncInfo) -> None:
        for p in info.params:
            if not is_hashable(p.type):
                raise self.error(f"functools.cache needs hashable arguments; '{p.name}' is a {p.type}", p.loc)
        if info.ret == NONE:
            raise self.error("functools.cache is for functions that return a value", info.node)

    def declare_method(self, st: StructType, node: A.FunctionDef) -> None:
        name = node.name
        if len(node.decorators) > 1:
            raise self.error("a method can have one decorator (for now)", node.decorators[1])
        kind = self.classify_decorator(node.decorators[0])[0] if node.decorators else "method"
        if kind in ("cache", "lru_cache", "user", "dataclass", "total_ordering", "wraps"):
            raise self.error(
                "methods only support @staticmethod, @classmethod, @property, @<name>.setter and "
                "@functools.cached_property (for now)", node.decorators[0],
            )
        if kind == "setter":
            getter = st.methods.get(name)
            base = node.decorators[0].value
            if base.id != name or getter is None or getter.kind != "getter":
                raise self.error(f"@{base.id}.setter must follow a @property named '{base.id}' and share its name", node)
            info = self.resolve_signature(node, owner=st)
            if len(info.params) != 1 or info.ret != NONE:
                raise self.error(f"a property setter takes (self, value) and returns nothing", node)
            info.kind, info.cpp_name = "setter", f"sd_set_{name}"
            st.methods[f"{name}.setter"] = info
            return
        if name in st.methods or name in st.fields:
            raise self.error(f"'{name}' is already defined in {st.name}", node)
        mapped = {"staticmethod": "static", "classmethod": "classmethod", "property": "getter",
                  "cached_property": "getter"}.get(kind, "method")
        info = self.resolve_signature(node, owner=st, kind=mapped)
        if mapped == "getter":
            what = "@cached_property" if kind == "cached_property" else "@property"
            if info.params or info.ret == NONE:
                raise self.error(f"a {what} takes only self and returns a value", node)
            info.cpp_name = f"sd_get_{name}"
            if kind == "cached_property":  # (computed by sd_compute_<name>, kept in sd_cache_<name>)
                info.lazy, info.cpp_name = True, f"sd_compute_{name}"
        st.methods[name] = info

    def property_setter(self, owner: StructType, attr: str, node: A.Node) -> FuncInfo:
        setter = owner.find_method(f"{attr}.setter")
        if setter is None:
            raise self.error(f"property '{attr}' of {owner.name} is read-only (add an @{attr}.setter)", node)
        return setter

    def check_not_frozen(self, owner: Type, attr: str, node: A.Node) -> None:
        if isinstance(owner, StructType) and any(t.frozen for t in owner.ancestors()):
            if self.in_own_init(node):
                return  # (the object isn't finished yet: its __init__ sets its fields)
            raise self.error(
                f"{owner.name} is a frozen dataclass; its field '{attr}' can't be changed "
                f"(make a changed copy: dataclasses.replace(obj, {attr}=...))", node,
            )

    def in_own_init(self, target: A.Node) -> bool:
        """`self.x = ...` (or self.items.append) inside the class's own __init__."""
        info = self.scope.info if self.scope is not None else None
        return info is not None and info.name == "__init__" and threads.base_name(target) == "self"

    def check_frozen_changes(self, body: list, fn: FuncInfo | None) -> None:
        """A frozen @value class is frozen all the way down: nothing inside it can change,
        directly or through something reached from it (`for row in t.grid: row.append(0)`)."""
        changed = threads.direct_changes(body)
        if not changed:
            return
        seen: set[int] = set()
        for n in flow.walk(body):
            if not (isinstance(n, A.Name) and isinstance(n.sym, Var)) or id(n.sym) in seen:
                continue
            var = n.sym
            seen.add(id(var))
            t = strip_optional(var.type)
            if id(var) not in changed or not (isinstance(t, StructType) and t.kind == "struct" and t.frozen and t.enum is None):
                continue
            if fn is not None and fn.name == "__init__" and var.name == "self":
                continue
            raise CheckError(
                f"{t.name} is a frozen @value class, so nothing in '{var.name}' can change, lists included "
                f"(they're part of its value). Make a changed copy with dataclasses.replace({var.name}, ...), "
                f"or copy the list first", changed[id(var)],
            )

    def field_default(self, default: A.Expr | None) -> A.Expr | None:
        """dataclasses.field(default=v) / field(default_factory=f) as a field's default."""
        if not (isinstance(default, A.Call) and self.classify_decorator(default.func)[0] == "field"):
            return default
        if default.args:
            raise self.error("field() takes keyword arguments: field(default=...) or field(default_factory=...)", default)
        kw = {k.name: k.value for k in default.keywords}
        for name in kw:
            if name not in ("default", "default_factory"):
                raise self.error(f"field({name}=...) isn't supported yet", default)
        if "default_factory" in kw:
            return A.Call(kw["default_factory"], [], loc=default.loc)
        return kw.get("default")

    def apply_total_ordering(self, st: StructType, d: A.Expr) -> None:
        """@functools.total_ordering: the comparisons a class doesn't define, from the one it does
        (Python's choice of which, and its formulas)."""
        derive = {
            "__lt__": {"__gt__": "not r and self != other", "__le__": "r or self == other", "__ge__": "not r"},
            "__le__": {"__ge__": "not r or self == other", "__lt__": "r and self != other", "__gt__": "not r"},
            "__gt__": {"__lt__": "not r and self != other", "__ge__": "r or self == other", "__le__": "not r"},
            "__ge__": {"__le__": "not r or self == other", "__gt__": "r and self != other", "__lt__": "not r"},
        }
        roots = [name for name in derive if st.find_method(name) is not None]
        if not roots:
            raise self.error("must define at least one ordering operation: < > <= >=", d)
        root = max(roots)  # (as Python picks: __lt__ first, then __le__, __gt__, __ge__)
        method = st.find_method(root)
        if len(method.params) != 1 or method.ret != BOOL:
            raise self.error(f"@total_ordering needs {root}(self, other) -> bool", method.node)
        op = {"__lt__": "<", "__le__": "<=", "__gt__": ">", "__ge__": ">="}[root]
        methods = [f"def {name}(self, other: {method.params[0].type}) -> bool:\n    r = self {op} other\n    return {expr}"
                   for name, expr in derive[root].items() if name not in roots]
        self.synthesize_methods(st, methods, d)

    def synthesize_methods(self, st: StructType, methods: list[str], d: A.Expr) -> None:
        """Add methods written as source (by @dataclass, @total_ordering) to a class."""
        if not methods:
            return
        source = "class Synthesized:\n" + "\n".join(textwrap.indent(m, "    ") for m in methods) + "\n"
        for fn in parse(source).body[0].body:
            for n in walk(fn):
                n.loc = d.loc  # errors in generated methods point at the decorator
            st.node.body.append(fn)
            st.methods[fn.name] = self.resolve_signature(fn, owner=st)

    def apply_dataclass(self, st: StructType, d: A.Expr) -> None:
        """@dataclass(eq=True, order=False, frozen=False, unsafe_hash=False): generate the
        methods Python's dataclass would, as ordinary dunders."""
        options = {"eq": True, "order": False, "frozen": False, "unsafe_hash": False}
        for kw in d.keywords if isinstance(d, A.Call) else []:
            if kw.name not in options:
                raise self.error(f"@dataclass({kw.name}=...) isn't supported", kw)
            if not isinstance(kw.value, A.BoolLit):
                raise self.error(f"@dataclass({kw.name}=...) must be True or False", kw.value)
            options[kw.name] = kw.value.value
        st.frozen = options["frozen"]
        fields = list(st.all_fields())
        mine = "(" + "".join(f"self.{f}, " for f in fields) + ")"
        theirs = "(" + "".join(f"other.{f}, " for f in fields) + ")"
        def other(name: str) -> str:
            # Like Python's, the generated method only compares objects of exactly this class;
            # it takes what the method it overrides takes (a subclass's __eq__ takes a Base).
            overridden = st.base.find_method(name) if st.base is not None else None
            return f"other: {overridden.params[0].type if overridden is not None else st.name}"

        same = f"isinstance(other, {st.name}) and __same_class__(self, other)" if st.kind == "class" else "True"
        methods = []
        if options["eq"] and st.kind == "class" and "__eq__" not in st.methods:  # structs already compare fields
            methods.append(f"def __eq__(self, {other('__eq__')}) -> bool:\n"
                           f"    if {same}:\n        return {mine} == {theirs}\n    return False")
        if options["order"]:
            for op, name in (("<", "__lt__"), ("<=", "__le__"), (">", "__gt__"), (">=", "__ge__")):
                if name not in st.methods:
                    fail = (f"    raise TypeError(f\"'{op}' not supported between instances of "
                            f"'{{__class_name__(self)}}' and '{{__class_name__(other)}}'\")")
                    methods.append(f"def {name}(self, {other(name)}) -> bool:\n"
                                   f"    if {same}:\n        return {mine} {op} {theirs}\n{fail}"
                                   if st.kind == "class" else
                                   f"def {name}(self, other: {st.name}) -> bool:\n    return {mine} {op} {theirs}")
        hashable = all(is_hashable(f.type) for f in st.all_fields().values())  # (a list field: like Python, it can't be hashed)
        if (options["unsafe_hash"] or (options["eq"] and options["frozen"])) and "__hash__" not in st.methods and hashable:
            methods.append(f"def __hash__(self) -> int:\n    return hash({mine})")
        self.synthesize_methods(st, methods, d)

    def check_isinstance(self, e: A.Call) -> Type:
        """isinstance(x, Dog) or isinstance(x, (Dog, Cat)), for class hierarchies."""
        if len(e.args) != 2 or e.keywords:
            raise self.error("isinstance() takes exactly 2 arguments: a value and a class (or a tuple of classes)", e)
        subject, spec = e.args
        t = self.check_expr(subject)
        base = strip_optional(t)
        if not isinstance(base, StructType) or base.kind != "class":
            raise self.error(
                f"isinstance() only works on class instances; a {t} always has the type {t}", subject
            )
        exprs = spec.elts if isinstance(spec, A.TupleLit) else [spec]
        classes: list[StructType] = []
        for x in exprs:
            st = (self.lookup_struct(x.id) if isinstance(x, A.Name) else self.module_struct(x))
            if st is None:
                raise self.error("isinstance() needs a class (or a tuple of classes) as its second argument", x)
            if not (st.is_subclass_of(base) or base.is_subclass_of(st)):
                raise self.error(f"a {base.name} can never be a {st.name} (they're unrelated classes)", x)
            classes.append(st)
        e.sym = CallTarget("isinstance", classes)
        return BOOL

    def isinstance_narrowing(self, e: A.Call) -> State:
        """The state where isinstance(subject, classes) is true: subject is the narrower class."""
        subject = e.args[0]
        classes: list[StructType] = e.sym.target
        declared = strip_optional(subject.ty)
        narrow = classes[0]
        for c in classes[1:]:
            narrow = join(narrow, c)
        if not narrow.is_subclass_of(declared):
            narrow = declared  # isinstance(dog, Animal): nothing new to learn beyond "not None"
        state = self.state.copy()
        if isinstance(subject, A.Name):
            entry = state.names.get(subject.id)
            if isinstance(entry, Bound):
                state.names[subject.id] = Bound(entry.var, narrow)
        elif path := attr_path(subject):
            state.attrs[path] = narrow
        return state

    def check_super_call(self, e: A.Call, sup: A.Call, method_name: str) -> Type:
        """super().method(...) calls the base class's version; super().__init__(...) its initializer."""
        info = self.scope.info
        if sup.args or sup.keywords or info is None or info.owner is None or self.lambda_depth:
            raise self.error("super() only works as `super().method(...)` directly inside a method", sup)
        base = info.owner.base
        if base is None or base.builtin:
            raise self.error(f"{info.owner.name} has no base class to call with super()", sup)
        if method_name == "__init__":
            params = self.constructor_params(base, e)
            owner = base.init.owner if base.init is not None else base
            e.sym = CallTarget("super_init", owner, self.match_args(e, params, f"{base.name}.__init__()"), params)
            return NONE
        method = base.find_method(method_name)
        if method is None:
            raise self.error(f"{base.name} has no method '{method_name}'", e.func)
        e.sym = CallTarget("super_method", method, self.match_args(e, method.params, f"{method_name}()"))
        return method.ret

    def call_value(self, e: A.Call, t: Type) -> Type:
        """Calling a function *value*: a variable, parameter, field or expression of function type."""
        if isinstance(t, ClassRefType):  # cls(...) in a classmethod
            return self.check_constructor(e, t.st)
        if m := self.dunder(t, "__call__"):  # obj(args) -> obj.__call__(args)
            e.sym = CallTarget("call_dunder", m, self.match_args(e, m.params, f"{t.name}()"))
            return m.ret
        if isinstance(t, OptionalType) and isinstance(t.inner, FuncType):
            raise self.error(f"{t} might be None; check it first", e.func)
        if not isinstance(t, FuncType):
            raise self.error(f"{t} is not callable", e.func)
        if e.keywords:
            raise self.error("keyword arguments can't be used when calling a function value", e.keywords[0])
        if len(e.args) != len(t.params):
            raise self.error(f"this function takes {plural(len(t.params), 'argument')} but {len(e.args)} were given", e)
        for i, (arg, pt) in enumerate(zip(e.args, t.params)):
            at = self.check_expr(arg, pt)
            if not assignable(at, pt):
                raise self.error(f"argument {i + 1} must be {pt}, not {at}", arg)
        e.sym = CallTarget("value", t)
        return t.ret

    def check_module_call(self, e: A.Call, mod: builtins.Module, member: str, expected: Type | None = None) -> Type:
        f = mod.members.get(member)
        if f is None:
            raise self.error(builtins.missing_member(mod, member), e.func)
        if isinstance(f, StructType):
            return self.check_constructor(e, f)  # utils.Point(...), raise zlib.error("...")
        if isinstance(f, builtins.SyncTypeDef):  # threading.Lock(), queue.Queue(...)
            return self.construct_sync(e, f.kind, None, expected)
        if isinstance(f, builtins.CollectionTypeDef):  # collections.deque(...), Counter(...)
            return self.construct_collection(e, f.kind, expected)
        if isinstance(f, GenericDef):  # utils.first(xs) / utils.Stack()
            return self.call_generic(e, f, None, expected)
        if isinstance(f, Var) and isinstance(f.type, FuncType):  # a decorated function from another module
            return self.call_value(e, self.module_member(e.func, mod, member))
        if isinstance(f, FuncInfo):  # a function from another .sd module
            e.sym = CallTarget("func", f, self.match_args(e, f.params, f"{mod.name}.{member}()"))
            return f.ret
        if not isinstance(f, builtins.Function):
            raise self.error(f"'{mod.name}.{member}' is not a function", e.func)
        e.sym = CallTarget("module_func", (mod, member))
        return f.check(builtins.CallContext(self, e, f"{mod.name}.{member}()", expected))

    def check_constructor(self, e: A.Call, st: StructType) -> Type:
        if st.enum is not None:  # Color(1): the member with that value (codegen: sd_lookup)
            if e.keywords or len(e.args) != 1:
                raise self.error(f"{st.name}() takes one argument, a member's value: {st.name}(value)", e)
            t = self.check_expr(e.args[0], st.enum.value_type)
            if t != st and not assignable(t, st.enum.value_type):
                raise self.error(f"{st.name}() looks a member up by its value, {with_article(st.enum.value_type)}, "
                                 f"not {with_article(t)}", e.args[0])
            param = Param("value", t, None, e.loc)
            e.sym = CallTarget("ctor", st, [e.args[0]], [param])
            return st
        params = self.constructor_params(st, e)
        e.sym = CallTarget("ctor", st, self.match_args(e, params, f"{st.name}()"), params)
        return st

    def constructor_params(self, st: StructType, node: A.Node) -> list[Param]:
        if st.init is not None:
            return st.init.params
        params = [Param(f.name, f.type, f.default, f.loc) for f in st.all_fields().values()]
        if st.is_exception and len(params) > 1 and any(p.default is None for p in params[1:]):
            # HttpError("not found", 404): the message becomes required when later fields are.
            params[0] = Param(params[0].name, params[0].type, None, params[0].loc)
        # Like a dataclass: fields without defaults are required, but a
        # field with a default can't come before one without.
        seen_default = False
        for p in params:
            if p.default is not None:
                seen_default = True
            elif seen_default:
                raise self.error(f"{st.name}(): field '{p.name}' has no default but comes after a field that does", node)
        return params

    def match_args(self, e: A.Call, params: list[Param], what: str) -> list[A.Expr | None]:
        """Match positional and keyword arguments to parameters; returns one slot per parameter.
        A `*args` parameter's slot is a tuple of the remaining positional arguments."""
        rest = None
        if params and params[-1].star:
            star, params = params[-1], params[:-1]
            rest = A.TupleLit(list(e.args[len(params):]), loc=e.loc)
            rest.ty = star.type
            for arg in rest.elts:
                t = self.check_expr(arg, star.type.elem)
                if not assignable(t, star.type.elem):
                    raise self.error(f"*{star.name} of {what} takes {star.type.elem} arguments, not {t}", arg)
            e = A.Call(e.func, e.args[:len(params)], e.keywords, loc=e.loc)
        if len(e.args) > len(params):
            raise self.error(f"{what} takes {plural(len(params), 'argument')} but {len(e.args)} were given", e)
        slots: list[A.Expr | None] = list(e.args) + [None] * (len(params) - len(e.args))
        index = {p.name: i for i, p in enumerate(params)}
        for kw in e.keywords:
            if kw.name not in index:
                raise self.error(f"{what} got an unexpected keyword argument '{kw.name}'", kw)
            if slots[index[kw.name]] is not None:
                raise self.error(f"{what} got multiple values for argument '{kw.name}'", kw)
            slots[index[kw.name]] = kw.value
        for p, arg in zip(params, slots):
            if arg is None:
                if p.default is None:
                    raise self.error(f"{what} is missing argument '{p.name}'", e)
                continue
            t = self.check_expr(arg, p.type)
            if not assignable(t, p.type):
                raise self.error(f"argument '{p.name}' of {what} must be {p.type}, not {t}", arg)
        return slots if rest is None else [*slots, rest]


# ---- helpers ----------------------------------------------------------------


def enum_members_matched(p: A.Pattern) -> set[str]:
    """The enum members a pattern matches by value (`Color.RED | Color.GREEN`), aliases as their member."""
    match p:
        case A.MatchValue(value) if isinstance(value.sym, tuple) and value.sym[0] == "enum_member":
            member = value.sym[2]
            return {member.alias_of or member.name}
        case A.MatchOr(options):
            return set().union(*(enum_members_matched(o) for o in options))
        case A.MatchAs(inner, _) if inner is not None:
            return enum_members_matched(inner)
    return set()


def walk(node):
    """Yield every syntax node under `node` (a node or list of nodes), depth first."""
    if isinstance(node, list):
        for item in node:
            yield from walk(item)
        return
    if not isinstance(node, A.Node):
        return
    yield node
    for f in dataclasses.fields(node):
        if f.name in ("loc", "sym", "ty", "dunder"):
            continue
        value = getattr(node, f.name)
        if isinstance(value, (A.Node, list)):
            yield from walk(value)


def target_names(target: A.Expr) -> list[str]:
    match target:
        case A.Name(name):
            return [name]
        case A.TupleLit(elts) | A.ListLit(elts):
            return [n for elt in elts for n in target_names(elt)]
    return []


COMPREHENSIONS = (A.ListComp, A.SetComp, A.DictComp, A.GeneratorExp)


def assigned_targets(stmts: list[A.Stmt]) -> list[str]:
    """Every name assigned in a function body (outside comprehensions and nested defs)."""
    names: list[str] = []

    def visit(node) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if isinstance(node, A.FunctionDef):
            names.append(node.name)  # a nested def assigns its name; its body is its own scope
            return
        if not isinstance(node, A.Node) or isinstance(node, (A.ClassDef, A.Lambda) + COMPREHENSIONS):
            return
        match node:
            case A.Assign(targets):
                for t in targets:
                    names.extend(target_names(t))
            case A.AnnAssign(target) | A.AugAssign(target) | A.For(target) | A.NamedExpr(target):
                names.extend(target_names(target))
            case A.ExceptHandler(_, A.Name(name)):
                names.append(name)
            case A.MatchAs(_, A.Name(name)) | A.MatchStar(A.Name(name)):
                names.append(name)
            case A.MatchMapping(_, _, A.Name(name)):
                names.append(name)
        for f in dataclasses.fields(node):
            if f.name not in ("loc", "sym", "ty", "dunder"):
                visit(getattr(node, f.name))

    visit(stmts)
    return names


def declared_names(stmts: list[A.Stmt]) -> tuple[dict[str, A.Stmt], dict[str, A.Stmt]]:
    """`nonlocal` and `global` declarations in a function body (not in nested functions)."""
    nonlocals: dict[str, A.Stmt] = {}
    global_names: dict[str, A.Stmt] = {}

    def visit(node) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, A.Node) or isinstance(node, (A.FunctionDef, A.ClassDef)):
            return
        if isinstance(node, A.Nonlocal):
            nonlocals.update(dict.fromkeys(node.names, node))
        elif isinstance(node, A.Global):
            global_names.update(dict.fromkeys(node.names, node))
        for f in dataclasses.fields(node):
            if f.name not in ("loc", "sym", "ty", "dunder"):
                visit(getattr(node, f.name))

    visit(stmts)
    return nonlocals, global_names


def assigned_names(stmts: list[A.Stmt]) -> set[str]:
    return set(assigned_targets(stmts))


def count_assignments(stmts: list[A.Stmt]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in assigned_targets(stmts):
        counts[name] = counts.get(name, 0) + 1
    # Anything assigned inside a nested block (if/for/while) or by a loop can
    # run zero or many times, so it can't be a simple global.
    for stmt in stmts:
        if isinstance(stmt, (A.If, A.While, A.For, A.Try, A.Match)):
            for name in assigned_targets([stmt]):
                counts[name] = counts.get(name, 0) + 1
    return counts


def is_scalar(t: Type) -> bool:
    """Values C++ converts implicitly where needed (int -> float); containers need re-checking."""
    return t in (INT, FLOAT, BOOL, STR, BYTES)


def attr_path(e: A.Expr) -> tuple[str, ...] | None:
    """`u.address.city` -> ("u", "address", "city"); None unless it's a chain rooted at a name."""
    match e:
        case A.Attribute(A.Name(name), attr):
            return (name, attr)
        case A.Attribute(value, attr):
            base = attr_path(value)
            return base + (attr,) if base else None
    return None


@dataclass(frozen=True)
class Dunder:
    """An operator or protocol resolved to a user type's dunder method (stored in Node.dunder)."""

    method: FuncInfo
    reflected: bool = False  # called on the right operand: 2 * v -> v.__rmul__(2), x in c -> c.__contains__(x)
    negate: bool = False  # a != b as `not a.__eq__(b)`; `not in`


BINARY_DUNDERS = {
    "+": "add", "-": "sub", "*": "mul", "/": "truediv", "//": "floordiv", "%": "mod", "**": "pow",
    "@": "matmul", "&": "and", "|": "or", "^": "xor", "<<": "lshift", ">>": "rshift",
}
UNARY_DUNDERS = {"-": "__neg__", "+": "__pos__", "~": "__invert__"}
# op -> (method on the left operand, reflected method on the right operand)
COMPARE_DUNDERS = {
    "==": ("__eq__", "__eq__"), "!=": ("__ne__", "__ne__"),
    "<": ("__lt__", "__gt__"), ">": ("__gt__", "__lt__"), "<=": ("__le__", "__ge__"), ">=": ("__ge__", "__le__"),
}
# name -> (number of parameters besides self, required return type or None)
DUNDER_SIGNATURES = {
    "__repr__": (0, STR), "__str__": (0, STR), "__len__": (0, INT), "__bool__": (0, BOOL), "__hash__": (0, INT),
    "__iter__": (0, None), "__neg__": (0, None), "__pos__": (0, None), "__invert__": (0, None), "__abs__": (0, None),
    **{name: (1, BOOL) for name in ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__contains__")},
    "__getitem__": (1, None), "__setitem__": (2, NONE),
    **{f"__{n}__": (1, None) for n in BINARY_DUNDERS.values()},
    **{f"__r{n}__": (1, None) for n in BINARY_DUNDERS.values()},
}


def accepts(m: FuncInfo, t: Type) -> bool:
    return len(m.params) == 1 and assignable(t, m.params[0].type)


@dataclass
class GenericDef:
    """A generic function or class; `checker` is the module that defines it."""

    kind: str  # 'func' or 'class'
    name: str
    node: A.FunctionDef | A.ClassDef
    checker: Checker


def generic_params(gen: GenericDef) -> list[tuple[str, A.TypeExpr | None]]:
    """The parameters inference matches arguments against: a function's parameters, or a
    class's constructor (its __init__, or its fields in order)."""
    if gen.kind == "func":
        return [(p.name, p.annotation) for p in gen.node.params]
    for stmt in gen.node.body:
        if isinstance(stmt, A.FunctionDef) and stmt.name == "__init__":
            return [(p.name, p.annotation) for p in stmt.params[1:]]
    return [(s.target.id, s.annotation) for s in gen.node.body
            if isinstance(s, A.AnnAssign) and isinstance(s.target, A.Name)]


def mentions(annotation: A.TypeExpr | None, names: set[str]) -> bool:
    match annotation:
        case A.TypeName(name, args):
            return name in names or any(mentions(a, names) for a in args)
        case A.OptionalType(inner):
            return mentions(inner, names)
        case A.UnionType(options):
            return any(mentions(o, names) for o in options)
        case A.FuncTypeExpr(params, ret):
            return any(mentions(p, names) for p in params) or mentions(ret, names)
    return False


def expr_to_type(e: A.Expr) -> A.TypeExpr:
    """The type written as an expression in `first[int](...)` or `Stack[list[str]]()`."""
    match e:
        case A.Name(name):
            return A.TypeName(name, loc=e.loc)
        case A.NoneLit():
            return A.TypeName("None", loc=e.loc)
        case A.Attribute():
            return A.TypeName(".".join(attr_path(e) or ("?",)), loc=e.loc)
        case A.Index(value, index):
            base = expr_to_type(value)
            return A.TypeName(base.name, [expr_to_type(x) for x in type_arg_exprs(index)], loc=e.loc)
    raise CheckError("expected a type here (write complex types like int? in an annotation instead)", e.loc)


def type_arg_exprs(index: A.Expr) -> list[A.Expr]:
    return index.elts if isinstance(index, A.TupleLit) else [index]


def structs_in(t: Type) -> list[StructType]:
    match t:
        case StructType():
            return [t, *(s for a in t.type_args for s in structs_in(a))]
        case ListType(x) | SetType(x) | OptionalType(x):
            return structs_in(x)
        case DictType(k, v):
            return structs_in(k) + structs_in(v)
        case TupleType(xs):
            return [s for x in xs for s in structs_in(x)]
        case FuncType(params, ret):
            return [s for x in (*params, ret) if x is not None for s in structs_in(x)]
    return []


def mangle(name: str, type_args: tuple) -> str:
    """first[int] -> first_of_int, Stack[str?] -> Stack_of_strQ, Pair[list[int], str] ->
    Pair_of_list_int_and_str. (No double underscores: C++ reserves those names.)"""
    parts = []
    for t in type_args:
        text = str(t).replace("?", "Q")
        parts.append(re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_"))
    return f"{name}_of_{'_and_'.join(parts)}"


def is_none_type(t: A.TypeExpr) -> bool:
    return isinstance(t, A.TypeName) and t.name == "None" and not t.args


def needs_context(e: A.Expr) -> bool:
    """A literal whose type can only come from its surroundings: [], {}, None, [None, None]..."""
    match e:
        case A.NoneLit():
            return True
        case A.ListLit(elts) | A.SetLit(elts):
            return all(needs_context(x) for x in elts)
        case A.DictLit(keys, values):
            return all(needs_context(v) for v in values)
    return False


def constant_int(e: A.Expr) -> int | None:
    match e:
        case A.IntLit(value):
            return value
        case A.UnaryOp("-", A.IntLit(value)):
            return -value
    return None


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def is_constant(e: A.Expr) -> bool:
    """A literal number, string, bytes, bool or None (or a negated number), or a tuple of those."""
    match e:
        case A.IntLit() | A.FloatLit() | A.StrLit() | A.BytesLit() | A.BoolLit() | A.NoneLit():
            return True
        case A.UnaryOp("-" | "+", A.IntLit() | A.FloatLit()):
            return True
        case A.TupleLit(elts):
            return all(is_constant(x) for x in elts)
    return False


ENUM_OPERATORS = {
    "+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b, "<<": lambda a, b: a << b,
    ">>": lambda a, b: a >> b, "|": lambda a, b: a | b, "&": lambda a, b: a & b, "^": lambda a, b: a ^ b,
}


def enum_constant(e: A.Expr, earlier: dict[str, object]) -> object:
    """The value of an enum member's constant expression (ValueError if it isn't one): a
    literal, a tuple of them, or ints combined with operators, using earlier members by name."""
    match e:
        case A.IntLit(v) | A.FloatLit(v) | A.StrLit(v) | A.BytesLit(v) | A.BoolLit(v):
            return v
        case A.TupleLit(elts):
            return tuple(enum_constant(x, earlier) for x in elts)
        case A.Name(name) if name in earlier:
            return earlier[name]
        case A.UnaryOp("-" | "+" | "~" as op, operand):
            v = enum_constant(operand, earlier)
            if type(v) not in (int, float) or (op == "~" and type(v) is not int):
                raise ValueError
            return -v if op == "-" else ~v if op == "~" else v
        case A.BinOp(op, left, right) if op in ENUM_OPERATORS:
            a, b = enum_constant(left, earlier), enum_constant(right, earlier)
            if type(a) is not int or type(b) is not int or (op in ("<<", ">>") and not 0 <= b < 64):
                raise ValueError
            return ENUM_OPERATORS[op](a, b)
    raise ValueError


def constant_type(v: object) -> Type:
    """The type of a constant worked out when compiling (an enum member's value)."""
    if isinstance(v, tuple):
        return TupleType(tuple(constant_type(x) for x in v))
    return {bool: BOOL, int: INT, float: FLOAT, str: STR, bytes: BYTES}[type(v)]


def type_family(t: Type) -> str:
    match t:
        case ListType():
            return "list"
        case DictType():
            return "dict"
        case SetType():
            return "set"
        case TupleType():
            return "tuple"
    return str(t)


def with_article(thing) -> str:
    """'an int', 'a str', 'an Animal'."""
    text = str(thing)
    return ("an " if text[:1].lower() in "aeiou" else "a ") + text


def describe_short(e: A.Expr) -> str:
    match e:
        case A.Name(name):
            return name
        case A.Attribute(value, attr):
            return f"{describe_short(value)}.{attr}"
    return "x"


def datetime_arithmetic(op: str, l: Type, r: Type) -> Type | None:
    """The datetime module's operators, as Python defines them."""
    numeric = r in (INT, FLOAT)
    match op, l, r:
        case "-", _, _ if l == r and l in (DATE, DATETIME):
            return TIMEDELTA  # date - date, datetime - datetime
        case "+" | "-", _, _ if l in (DATE, DATETIME) and r == TIMEDELTA:
            return l
        case "+", _, _ if l == TIMEDELTA and r in (DATE, DATETIME):
            return r
        case "+" | "-" | "%", _, _ if l == r == TIMEDELTA:
            return TIMEDELTA
        case "*", _, _ if (l == TIMEDELTA and numeric) or (r == TIMEDELTA and l in (INT, FLOAT)):
            return TIMEDELTA
        case "/", _, _ if l == TIMEDELTA:
            return FLOAT if r == TIMEDELTA else TIMEDELTA if numeric else None
        case "//", _, _ if l == TIMEDELTA:
            return INT if r == TIMEDELTA else TIMEDELTA if r == INT else None
    return None


def normal_dist_arithmetic(op: str, l: Type, r: Type) -> bool:
    """statistics.NormalDist's operators: +/- another NormalDist or a number, * and / by a number."""
    nd = builtins.NORMAL_DIST
    if op in ("+", "-"):
        return (l == nd and (r == nd or is_numeric(r))) or (r == nd and is_numeric(l))
    if op == "*":
        return (l == nd and is_numeric(r)) or (r == nd and is_numeric(l))
    return op == "/" and l == nd and is_numeric(r)


def has_yield(body: list[A.Stmt]) -> bool:
    """Is this a generator's body? (A nested def's yields are its own.)"""
    for stmt in body:
        match stmt:
            case A.Yield():
                return True
            case A.FunctionDef() | A.ClassDef():
                continue
        for field in ("body", "orelse", "finalbody"):
            if has_yield(getattr(stmt, field, None) or []):
                return True
        for handler in getattr(stmt, "handlers", None) or getattr(stmt, "cases", None) or []:
            if has_yield(handler.body):
                return True
    return False


class ContextYields:
    """A @contextmanager function must yield exactly once: the with block runs at the yield, and
    when it ends the generator must finish. Flags a yield that runs after another one on every
    path (Python's "generator didn't stop"); yields in different branches are fine. States: 0
    (no yield yet on some path), 1 (a yield on every path), None (the path has ended)."""

    def __init__(self, checker: "Checker"):
        self.checker = checker

    @staticmethod
    def lo(*states: int | None) -> int | None:
        live = [s for s in states if s is not None]
        return min(live) if live else None

    def block(self, stmts: list[A.Stmt], state: int | None) -> int | None:
        for s in stmts:
            if state is None:
                return None
            state = self.stmt(s, state)
        return state

    def stmt(self, s: A.Stmt, state: int) -> int | None:
        match s:
            case A.Yield(from_=from_):
                if from_:
                    raise self.checker.error("a @contextmanager function yields once: `yield from` isn't supported there", s)
                if state == 1:
                    raise self.checker.error(
                        "this yield always runs after another one, but a @contextmanager function must yield exactly "
                        "once (a second yield is Python's \"generator didn't stop\" error)", s,
                    )
                return 1
            case A.Return() | A.Raise() | A.Break() | A.Continue():
                return None
            case A.If(body=body, orelse=orelse):
                return self.lo(self.block(body, state), self.block(orelse, state))
            case A.For(body=body, orelse=orelse) | A.While(body=body, orelse=orelse):
                self.block(body, state)  # (may run any number of times, so nothing is certain after it)
                self.block(orelse, state)
                return state
            case A.With(body=body):
                return self.lo(state, self.block(body, state))
            case A.Match(cases=cases):
                return self.lo(state, *(self.block(c.body, state) for c in cases))
            case A.Try(body=body, handlers=handlers, orelse=orelse, finalbody=finalbody):
                after = self.lo(self.block(body + orelse, state), *(self.block(h.body, state) for h in handlers))
                if finalbody:
                    fin = self.block(finalbody, state)
                    if fin is None:
                        return None
                    if fin == 1 and after is not None:
                        after = 1
                return after
        return state
