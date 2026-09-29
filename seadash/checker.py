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
from dataclasses import dataclass, field

from . import ast as A
from . import builtins
from .errors import CheckError, Loc
from .types import (
    BOOL, FLOAT, INT, NONE, PRIMITIVES, STR,
    DictType, Field, FuncInfo, FuncType, IterType, ListType, ModuleType, OptionalType, Param,
    SetType, StructType, TupleType, Type, Var,
    assignable, element_type, is_hashable, is_numeric, join, strip_optional,
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


@dataclass(frozen=True)
class MaybeUnbound:
    pass


Entry = Bound | Conflict | MaybeUnbound


@dataclass
class State:
    names: dict[str, Entry] = field(default_factory=dict)
    dead: bool = False  # after return/break/continue: unreachable

    def copy(self) -> State:
        return State(dict(self.names), self.dead)


def merge(states: list[State]) -> State:
    live = [s for s in states if not s.dead]
    if not live:
        # Keep the names so unreachable code after e.g. `return` still checks sensibly.
        return State(dict(states[0].names), dead=True)
    merged = State()
    for name in {n for s in live for n in s.names}:
        entries = [s.names.get(name) for s in live]
        merged.names[name] = merge_entries(entries)
    return merged


def merge_entries(entries: list[Entry | None]) -> Entry:
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
class ModuleInfo:
    """Everything codegen needs about a checked module."""

    structs: list[StructType]
    functions: list[FuncInfo]
    globals: list[Var]
    main_locals: list[Var]
    imports: list[builtins.Module]


def check(module: A.Module) -> ModuleInfo:
    return Checker().check_module(module)


class Checker:
    def __init__(self) -> None:
        self.structs: dict[str, StructType] = {}
        self.functions: dict[str, FuncInfo] = {}
        self.modules: dict[str, builtins.Module] = {}  # `import math` / `import math as m`
        self.imported: dict[str, tuple[builtins.Module, str]] = {}  # `from math import sqrt`
        self.globals: dict[str, Var] = {}
        self.module_assign_counts: dict[str, int] = {}
        self.scope: FunctionScope | None = None
        self.state = State()
        self.loops: list[LoopContext] = []
        self.handler_depth = 0  # inside an `except` block (bare `raise` allowed)
        self.finally_loops: list[int] = []  # loop depth on entering each enclosing `finally`
        self.lambda_depth = 0

    def error(self, message: str, node: A.Node | Loc) -> CheckError:
        return CheckError(message, node if isinstance(node, Loc) else node.loc)

    # =========================================================================
    # Module structure: declarations first, then bodies
    # =========================================================================

    def check_module(self, module: A.Module) -> ModuleInfo:
        top_level: list[A.Stmt] = []
        struct_nodes: list[A.ClassDef] = []
        func_nodes: list[A.FunctionDef] = []

        for stmt in module.body:
            match stmt:
                case A.ClassDef():
                    self.declare_struct(stmt)
                    struct_nodes.append(stmt)
                case A.FunctionDef():
                    if stmt.name in self.functions or stmt.name in self.structs:
                        raise self.error(f"'{stmt.name}' is already defined", stmt)
                    func_nodes.append(stmt)
                    self.functions[stmt.name] = None  # placeholder until signatures resolve
                case A.Import() | A.ImportFrom():
                    self.declare_import(stmt)
                case _:
                    top_level.append(stmt)

        for node in struct_nodes:
            self.resolve_base(node.sym)
        for node in struct_nodes:
            self.resolve_struct_members(node.sym)
        for node in func_nodes:
            self.functions[node.name] = self.resolve_signature(node, owner=None)
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

        return ModuleInfo(
            structs=list(self.structs.values()),
            functions=list(self.functions.values()),
            globals=list(self.globals.values()),
            main_locals=[v for v in main_scope.locals if v.kind != "global"],
            imports=list(self.modules.values()) + [m for m, _ in self.imported.values()],
        )

    def all_functions(self) -> list[FuncInfo]:
        infos = list(self.functions.values())
        for st in self.structs.values():
            infos += st.methods.values()
        return infos

    def declare_struct(self, node: A.ClassDef) -> None:
        if node.name in self.structs or node.name in self.functions:
            raise self.error(f"'{node.name}' is already defined", node)
        if node.name in PRIMITIVES or node.name in builtins.CONTAINER_TYPES or node.name in builtins.EXCEPTIONS:
            raise self.error(f"can't define a {node.kind} named '{node.name}'; that's a built-in type", node)
        if node.type_params:
            raise self.error(f"generic {node.kind}s are not supported yet", node)
        if len(node.bases) > 1:
            raise self.error("multiple inheritance is not supported", node.bases[1])
        st = StructType(node.name, node.kind, node)
        node.sym = st
        self.structs[node.name] = st

    def declare_import(self, node: A.Import | A.ImportFrom) -> None:
        if isinstance(node, A.Import):
            for alias in node.names:
                mod = self.find_module(alias.name, alias)
                self.modules[alias.asname or alias.name] = mod
            return
        mod = self.find_module(node.module, node)
        for alias in node.names:
            if alias.name not in mod.members:
                raise self.error(f"module '{mod.name}' has no member '{alias.name}'", alias)
            self.imported[alias.asname or alias.name] = (mod, alias.name)

    def find_module(self, name: str, node: A.Node) -> builtins.Module:
        mod = builtins.MODULES.get(name)
        if mod is None:
            raise self.error(f"no module named '{name}' (available: {', '.join(sorted(builtins.MODULES))})", node)
        return mod

    def lookup_struct(self, name: str) -> StructType | None:
        return self.structs.get(name) or builtins.EXCEPTIONS.get(name)

    def resolve_base(self, st: StructType) -> None:
        """Only exception classes can inherit (for now): `class NotFound(ValueError): ...`"""
        if not st.node.bases:
            return
        base_expr = st.node.bases[0]
        base = self.resolve_type(base_expr)
        if not (isinstance(base, StructType) and base.is_exception):
            raise self.error(
                f"inheritance is only supported for exception classes (for now), e.g. `class {st.name}(Exception):`",
                base_expr,
            )
        if st.kind != "class":
            raise self.error(f"exceptions must be classes, not structs: `class {st.name}({base.name}):`", st.node)
        if base.is_subclass_of(st):
            raise self.error(f"'{st.name}' can't inherit from itself", base_expr)
        st.base = base

    def resolve_struct_members(self, st: StructType) -> None:
        for stmt in st.node.body:
            match stmt:
                case A.AnnAssign(A.Name(name), annotation, default):
                    if name in st.fields or (st.base and st.base.find_field(name)):
                        raise self.error(f"field '{name}' is already defined", stmt)
                    st.fields[name] = Field(name, self.resolve_type(annotation), default, stmt.loc)
                case A.FunctionDef(name):
                    if name in st.methods or name in st.fields:
                        raise self.error(f"'{name}' is already defined in {st.name}", stmt)
                    st.methods[name] = self.resolve_signature(stmt, owner=st)
                case A.Pass() | A.ExprStmt(A.StrLit()):
                    pass  # `pass` or a docstring
                case _:
                    raise self.error(
                        f"a {st.kind} body can only contain fields (`x: int`) and methods (`def ...`)", stmt
                    )
        if st.init is not None and st.init.ret != NONE:
            raise self.error("__init__ must not return a value", st.init.node)

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
                    f"struct '{st.name}' can't contain itself (field '{f.name}'); "
                    f"make it a class, or use a list", f.loc,
                )

    def resolve_signature(self, node: A.FunctionDef, owner: StructType | None) -> FuncInfo:
        if node.type_params:
            raise self.error("generic functions are not supported yet", node)
        params = list(node.params)
        if owner is not None:
            if not params or params[0].name != "self":
                raise self.error(f"method '{node.name}' needs 'self' as its first parameter", node)
            first = params.pop(0)
            if first.annotation is not None:
                raise self.error("'self' doesn't need a type annotation", first)
        resolved: list[Param] = []
        for p in params:
            if p.annotation is None:
                raise self.error(
                    f"parameter '{p.name}' needs a type annotation, e.g. `{p.name}: int`", p
                )
            resolved.append(Param(p.name, self.resolve_type(p.annotation), p.default, p.loc))
        ret = self.resolve_type(node.returns) if node.returns else NONE
        info = FuncInfo(node.name, resolved, ret, node, owner)
        node.sym = info
        return info

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

    def check_function_body(self, info: FuncInfo) -> None:
        node: A.FunctionDef = info.node
        scope = FunctionScope(info, info.ret, assigned_names(node.body), info.locals)
        state = State()
        if info.owner is not None:
            self_var = Var("self", "self", info.owner, "param", node.params[0].loc)
            state.names["self"] = Bound(self_var, info.owner)
            node.params[0].sym = self_var
        param_nodes = node.params[1:] if info.owner is not None else node.params
        for p, pnode in zip(info.params, param_nodes):
            var = Var(p.name, p.name, p.type, "param", p.loc)
            scope.vars[(p.name, p.type)] = var
            scope.cpp_names.add(p.name)
            state.names[p.name] = Bound(var, p.type)
            pnode.sym = var
        self.enter(scope, state)
        self.check_block(node.body)
        if not self.state.dead and info.ret != NONE and not isinstance(info.ret, OptionalType):
            raise self.error(
                f"function '{info.name}' can reach its end without returning a value "
                f"(it's declared to return {info.ret})", node,
            )

    def enter(self, scope: FunctionScope, state: State) -> None:
        self.scope = scope
        self.state = state
        self.loops = []
        self.handler_depth = 0
        self.finally_loops = []
        self.lambda_depth = 0

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
            case A.UnionType():
                raise self.error("union types are not supported yet (T? for 'T or None' is)", t)
            case A.FuncTypeExpr(params, ret):
                return FuncType(tuple(self.resolve_type(p) for p in params), self.resolve_type(ret))
            case A.TypeName(name, args):
                return self.resolve_type_name(t, name, args)
        raise self.error("invalid type", t)

    def resolve_type_name(self, node: A.TypeName, name: str, args: list[A.TypeExpr]) -> Type:
        if name in PRIMITIVES or self.lookup_struct(name):
            if args:
                raise self.error(f"'{name}' doesn't take type arguments", node)
            return PRIMITIVES.get(name) or self.lookup_struct(name)
        if name in builtins.CONTAINER_TYPES:
            arity = builtins.CONTAINER_TYPES[name]
            example = {"list": "list[int]", "set": "set[int]", "dict": "dict[str, int]", "tuple": "tuple[int, str]"}[name]
            if not args or (arity is not None and len(args) != arity):
                raise self.error(f"'{name}' needs {arity or 'some'} type argument(s), e.g. {example}", node)
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
            raise self.error("types from modules are not supported yet", node)
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
            case A.Assert(test, msg):
                self.state, _ = self.check_condition(test)  # after `assert x`, x is known not None
                if msg is not None:
                    self.check_expr(msg)
            case A.Raise(exc, cause):
                self.check_raise(stmt, exc, cause)
            case A.Try():
                self.check_try(stmt)
            case A.If():
                self.check_if(stmt)
            case A.While():
                self.check_while(stmt)
            case A.For():
                self.check_for(stmt)
            case A.FunctionDef():
                raise self.error("functions can only be defined at the top level of a module (for now)", stmt)
            case A.ClassDef():
                raise self.error(f"{stmt.kind}s can only be defined at the top level of a module", stmt)
            case A.Import() | A.ImportFrom():
                raise self.error("imports must be at the top level of a module", stmt)
            case _:
                raise self.error(f"unsupported statement {type(stmt).__name__}", stmt)

    def expected_for_target(self, target: A.Expr) -> Type | None:
        """The type a target currently holds, as a hint for `xs = []` style values."""
        match target:
            case A.Name(name):
                entry = self.state.names.get(name)
                return entry.var.type if isinstance(entry, Bound) else None
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
        vt = self.check_expr(value)
        result = self.binop_type(op, current, vt, stmt)
        if isinstance(target, A.Name):
            self.bind(target, result, stmt)
        elif not assignable(result, current):
            raise self.error(f"'{op}=' would change this {current} into a {result}", stmt)
        # For codegen: what was read (and its type there) and the operation's
        # result type. target.sym is the variable written, which may differ.
        stmt.sym = (read_sym, current, result)

    def assign(self, target: A.Expr, t: Type, value: A.Expr) -> None:
        match target:
            case A.Name():
                self.bind(target, t, value)
            case A.Attribute(obj, attr):
                owner = self.check_expr(obj)
                if not isinstance(owner, StructType):
                    raise self.error(f"can't set attribute '{attr}' on {owner}", target)
                f = owner.find_field(attr)
                if f is None:
                    raise self.error(f"{owner.name} has no field '{attr}'", target)
                if not assignable(t, f.type):
                    raise self.error(f"field '{attr}' is {f.type}, can't assign {t}", value)
                target.ty = f.type
                target.sym = f
            case A.Index(container, index):
                ct = self.check_expr(container)
                match ct:
                    case ListType(elem):
                        if isinstance(index, A.Slice):
                            raise self.error("assigning to a slice is not supported yet", index)
                        self.expect_type(index, INT, "list index")
                        slot = elem
                    case DictType(key, val):
                        self.expect_type(index, key, "dict key")
                        slot = val
                    case _ if ct == STR or isinstance(ct, TupleType):
                        raise self.error(f"{ct} can't be changed in place (it's immutable)", target)
                    case _:
                        raise self.error(f"{ct} doesn't support item assignment", target)
                if not assignable(t, slot):
                    raise self.error(f"can't store {t} in a {ct}", value)
                target.ty = slot
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
        if not exact and isinstance(entry, Bound) and assignable(t, entry.var.type):
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
            var = self.variable(name.id, t, name.loc)
        view = var.type
        if isinstance(var.type, OptionalType) and t != NONE and not isinstance(t, OptionalType):
            view = var.type.inner  # just assigned a real value: known not None
        self.state.names[name.id] = Bound(var, view)
        name.sym = var
        name.ty = var.type

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
        var = Var(name, cpp_name, t, kind, loc)
        scope.vars[key] = var
        scope.cpp_names.add(cpp_name)
        scope.locals.append(var)
        if kind == "global":
            self.globals[name] = var
        return var

    def check_return(self, stmt: A.Return, value: A.Expr | None) -> None:
        scope = self.scope
        if scope.is_module:
            raise self.error("'return' outside a function", stmt)
        if self.finally_loops:
            raise self.error("'return' can't be used inside a 'finally' block", stmt)
        name = scope.info.name
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

    def check_handler_types(self, handler: A.ExceptHandler) -> StructType:
        """The classes an `except` clause catches; returns the type bound by `as e`."""
        if handler.type is None:
            handler.sym = []
            return builtins.EXCEPTIONS["Exception"]
        exprs = handler.type.elts if isinstance(handler.type, A.TupleLit) else [handler.type]
        classes: list[StructType] = []
        for e in exprs:
            st = self.lookup_struct(e.id) if isinstance(e, A.Name) else None
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
        elem = element_type(iter_type)
        if elem is None:
            raise self.error(f"can't loop over {iter_type}", stmt.iter)

        def iteration() -> State:
            exhausted = self.state.copy()
            self.assign(stmt.target, elem, stmt.iter)
            return exhausted

        self.check_loop(stmt, iteration)

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
            case A.Compare(subject, [op], [A.NoneLit()]) if op in ("is", "is not", "==", "!="):
                self.check_expr(e)
                narrowed = self.narrowed(subject)
                return (narrowed, self.state.copy()) if op in ("is not", "!=") else (self.state.copy(), narrowed)
        t = self.check_expr(e)
        self.check_truthy(t, e)
        if isinstance(t, OptionalType):
            return self.narrowed(e), self.state.copy()
        return self.state.copy(), self.state.copy()

    def narrowed(self, subject: A.Expr) -> State:
        """A copy of the state where `subject` (a name or walrus) is known not to be None."""
        state = self.state.copy()
        name = subject.target if isinstance(subject, A.NamedExpr) else subject
        if isinstance(name, A.Name):
            entry = state.names.get(name.id)
            if isinstance(entry, Bound) and isinstance(entry.ty, OptionalType):
                state.names[name.id] = Bound(entry.var, entry.ty.inner)
        return state

    def check_truthy(self, t: Type, e: A.Expr) -> None:
        ok = t in (INT, FLOAT, BOOL, STR) or isinstance(
            t, (ListType, DictType, SetType, TupleType, OptionalType)
        )
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
            case A.BoolLit():
                return BOOL
            case A.NoneLit():
                return NONE
            case A.FString(parts):
                for part in parts:
                    if isinstance(part, A.FormattedValue):
                        self.check_printable(self.check_expr(part.value), part.value)
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
                return TupleType(tuple(self.check_expr(x, h) for x, h in zip(elts, hints)))
            case A.ListComp(elt, gens):
                return ListType(self.check_comprehension(gens, lambda: self.check_expr(elt)))
            case A.SetComp(elt, gens):
                t = SetType(self.check_comprehension(gens, lambda: self.check_expr(elt)))
                self.check_hashable(t.elem, "set elements", elt)
                return t
            case A.GeneratorExp(elt, gens):
                return IterType(self.check_comprehension(gens, lambda: self.check_expr(elt)), "generator")
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
            return entry.ty
        if isinstance(entry, Conflict):
            versions = sorted(entry.sources, key=lambda s: (s[1].line, s[1].col))
            described = ", ".join(f"{t} (line {loc.line})" for t, loc in versions)
            raise self.error(
                f"'{name}' has different types depending on the path taken to get here: {described}. "
                f"Use one type on every path, or give it a new name", e,
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
        if self.lookup_struct(name) or name in builtins.FUNCTIONS:
            return self.callable_as_value(e, expected, name)
        if name in builtins.VALUES:
            e.sym = builtins.VALUES[name]
            return builtins.VALUES[name].type
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
        outer = self.state
        self.state = outer.copy()
        self.lambda_depth += 1
        for p, t in zip(e.params, param_types):
            var = Var(p.name, p.name, t, "lambda", p.loc)
            self.state.names[p.name] = Bound(var, t)
            p.sym = var
        hint = expected.ret if isinstance(expected, FuncType) else None
        body = self.check_expr(e.body, hint)
        self.lambda_depth -= 1
        self.state = outer
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
        if isinstance(t, (IterType, ModuleType)):
            raise self.error(f"{t} can't be converted to a string", e)

    def check_sequence_literal(self, e, elts, expected, ctor, word: str) -> Type:
        hint = expected.elem if isinstance(expected, ctor) else None
        if not elts:
            if hint is None:
                raise self.error(
                    f"can't tell what type of {word} this is; annotate the variable, "
                    f"e.g. `xs: {word}[int] = {'[]' if word == 'list' else 'set()'}`", e,
                )
            return expected
        types = [self.check_expr(x, hint) for x in elts]
        if hint is not None and all(assignable(t, hint) for t in types):
            return expected
        return ctor(self.join_all(types, elts, f"{word} items"))

    def check_dict_literal(self, e: A.DictLit, keys, values, expected) -> Type:
        if not keys:
            if isinstance(expected, (DictType, SetType)):
                return expected  # `s: set[int] = {}` is allowed and means an empty set
            raise self.error("can't tell what type of dict this is; annotate the variable, e.g. `d: dict[str, int] = {}`", e)
        khint, vhint = (expected.key, expected.value) if isinstance(expected, DictType) else (None, None)
        kts = [self.check_expr(k, khint) for k in keys]
        vts = [self.check_expr(v, vhint) for v in values]
        if khint is not None and all(assignable(t, khint) for t in kts) and all(assignable(t, vhint) for t in vts):
            return expected
        k = self.join_all(kts, keys, "dict keys")
        self.check_hashable(k, "dict keys", keys[0])
        return DictType(k, self.join_all(vts, values, "dict values"))

    def join_all(self, types: list[Type], nodes: list[A.Expr], what: str) -> Type:
        result = types[0]
        for t, node in zip(types[1:], nodes[1:]):
            joined = join(result, t)
            if joined is None:
                raise self.error(f"{what} have different types: {result} and {t}", node)
            result = joined
        if result == NONE:
            raise self.error(f"{what} can't all be None", nodes[0])
        return result

    def check_comprehension(self, gens: list[A.Comprehension], check_element):
        """Comprehension variables live in their own scope, like Python 3."""
        outer = self.state
        self.state = outer.copy()
        for gen in gens:
            iter_type = self.check_expr(gen.iter)
            elem = element_type(iter_type)
            if elem is None:
                raise self.error(f"can't loop over {iter_type}", gen.iter)
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
        if op in ("-", "+") and is_numeric(t):
            return t
        if op == "~" and t == INT:
            return INT
        raise self.error(f"bad operand type for unary {op}: {t}", e)

    def binop_type(self, op: str, l: Type, r: Type, e: A.Node) -> Type:
        numeric = is_numeric(l) and is_numeric(r)
        widened = FLOAT if FLOAT in (l, r) else INT
        match op:
            case "+":
                if numeric:
                    return widened
                if l == r and (l == STR or isinstance(l, ListType)):
                    return l
                if isinstance(l, TupleType) and isinstance(r, TupleType):
                    return TupleType(l.elts + r.elts)
            case "-":
                if numeric:
                    return widened
                if l == r and isinstance(l, SetType):
                    return l
            case "*":
                if numeric:
                    return widened
                if (l == STR or isinstance(l, ListType)) and r == INT:
                    return l
                if l == INT and (r == STR or isinstance(r, ListType)):
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
        hint = ""
        if op == "+" and STR in (l, r):
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
        for op, right in zip(e.ops, e.comparators):
            rt = self.check_expr(right)
            self.check_comparison(op, lt, rt, left, right, e)
            left, lt = right, rt
        return BOOL

    def check_comparison(self, op: str, lt: Type, rt: Type, left: A.Expr, right: A.Expr, e: A.Compare) -> None:
        if op in ("<", ">", "<=", ">="):
            ordered = (is_numeric(lt) and is_numeric(rt)) or (
                lt == rt and (lt == STR or isinstance(lt, (TupleType, ListType)))
            )
            if not ordered:
                raise self.error(f"'{op}' isn't supported between {lt} and {rt}", e)
        elif op in ("==", "!="):
            if not (is_numeric(lt) and is_numeric(rt)) and join(lt, rt) is None:
                raise self.error(f"comparing {lt} with {rt} using '{op}' is always {op == '!='}", e)
        elif op in ("in", "not in"):
            match rt:
                case ListType(elem) | SetType(elem) | IterType(elem, "range"):
                    ok = assignable(lt, elem)
                case DictType(key):
                    ok = assignable(lt, key)
                case TupleType(elts):
                    ok = any(join(lt, t) for t in elts)
                case _ if rt == STR:
                    ok = lt == STR
                case _:
                    raise self.error(f"'{op}' needs a list, set, dict, tuple or str on the right, not {rt}", right)
            if not ok:
                raise self.error(f"a {lt} can never be in a {rt}", e)
        elif op in ("is", "is not"):
            is_none_check = (isinstance(right, A.NoneLit) and (isinstance(lt, OptionalType) or lt == NONE)) or (
                isinstance(left, A.NoneLit) and isinstance(rt, OptionalType)
            )
            same_object = lt == rt and isinstance(lt, StructType) and lt.kind == "class"
            if not (is_none_check or same_object):
                if isinstance(right, A.NoneLit):
                    raise self.error(f"{lt} can never be None (only T? types can)", e)
                raise self.error(f"'{op}' is for None checks and class instances; use '==' to compare values", e)

    def check_attribute(self, e: A.Attribute, value: A.Expr, attr: str, expected: Type | None = None) -> Type:
        if (
            isinstance(value, A.Name)
            and value.id in ("str", "list", "dict", "set")
            and value.id not in self.state.names
            and value.id not in self.scope.assigned
        ):
            return self.callable_as_value(e, expected, f"{value.id}.{attr}")  # key=str.lower
        vt = self.check_expr(value)
        if isinstance(vt, ModuleType):
            if isinstance(value.sym.members.get(attr), builtins.Function):
                return self.callable_as_value(e, expected, f"{value.sym.name}.{attr}")
            return self.module_member(e, value.sym, attr)
        if isinstance(vt, StructType):
            if f := vt.find_field(attr):
                e.sym = f
                return f.type
            if (method := vt.find_method(attr)) and method.name != "__init__":
                e.sym = method  # a bound method: remembers its object
                return FuncType(tuple(p.type for p in method.params), method.ret)
            raise self.error(f"{vt.name} has no field '{attr}'", e)
        if isinstance(vt, OptionalType):
            raise self.error(
                f"{vt} might be None; check it first, e.g. `if {describe_short(value)} is not None:`", value
            )
        raise self.error(f"{vt} has no attribute '{attr}'", e)

    def module_member(self, e: A.Expr, mod: builtins.Module, member: str) -> Type:
        m = mod.members.get(member)
        if m is None:
            raise self.error(f"module '{mod.name}' has no member '{member}'", e)
        if isinstance(m, builtins.Function):
            raise self.error(f"'{mod.name}.{member}' can only be called here (functions aren't values yet)", e)
        e.sym = m
        return m.type

    def check_index(self, e: A.Index, value: A.Expr, index: A.Expr) -> Type:
        vt = self.check_expr(value)
        if isinstance(index, A.Slice):
            if not (isinstance(vt, ListType) or vt == STR):
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
                return self.check_module_call(e, mod, member)
            if name in builtins.FUNCTIONS and name not in self.module_assign_counts:
                ctx = builtins.CallContext(self, e, f"{name}()", expected)
                e.sym = CallTarget("builtin", name)
                return builtins.FUNCTIONS[name](ctx)
        if isinstance(func, A.Attribute):
            owner = self.check_expr(func.value)
            if isinstance(owner, ModuleType):
                return self.check_module_call(e, func.value.sym, func.attr)
            if isinstance(owner, StructType):
                method = owner.find_method(func.attr)
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
            return handler(ctx)
        return self.call_value(e, self.check_expr(func))

    def call_value(self, e: A.Call, t: Type) -> Type:
        """Calling a function *value*: a variable, parameter, field or expression of function type."""
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

    def check_module_call(self, e: A.Call, mod: builtins.Module, member: str) -> Type:
        f = mod.members.get(member)
        if f is None:
            raise self.error(f"module '{mod.name}' has no member '{member}'", e.func)
        if not isinstance(f, builtins.Function):
            raise self.error(f"'{mod.name}.{member}' is not a function", e.func)
        e.sym = CallTarget("module_func", (mod, member))
        return f.check(builtins.CallContext(self, e, f"{mod.name}.{member}()", None))

    def check_constructor(self, e: A.Call, st: StructType) -> Type:
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
        """Match positional and keyword arguments to parameters; returns one slot per parameter."""
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
        return slots


# ---- helpers ----------------------------------------------------------------


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
        if f.name in ("loc", "sym", "ty"):
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
        if not isinstance(node, A.Node) or isinstance(node, (A.FunctionDef, A.ClassDef) + COMPREHENSIONS):
            return
        match node:
            case A.Assign(targets):
                for t in targets:
                    names.extend(target_names(t))
            case A.AnnAssign(target) | A.AugAssign(target) | A.For(target) | A.NamedExpr(target):
                names.extend(target_names(target))
            case A.ExceptHandler(_, A.Name(name)):
                names.append(name)
        for f in dataclasses.fields(node):
            if f.name not in ("loc", "sym", "ty"):
                visit(getattr(node, f.name))

    visit(stmts)
    return names


def assigned_names(stmts: list[A.Stmt]) -> set[str]:
    return set(assigned_targets(stmts))


def count_assignments(stmts: list[A.Stmt]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in assigned_targets(stmts):
        counts[name] = counts.get(name, 0) + 1
    # Anything assigned inside a nested block (if/for/while) or by a loop can
    # run zero or many times, so it can't be a simple global.
    for stmt in stmts:
        if isinstance(stmt, (A.If, A.While, A.For, A.Try)):
            for name in assigned_targets([stmt]):
                counts[name] = counts.get(name, 0) + 1
    return counts


def constant_int(e: A.Expr) -> int | None:
    match e:
        case A.IntLit(value):
            return value
        case A.UnaryOp("-", A.IntLit(value)):
            return -value
    return None


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


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


def describe_short(e: A.Expr) -> str:
    match e:
        case A.Name(name):
            return name
        case A.Attribute(value, attr):
            return f"{describe_short(value)}.{attr}"
    return "x"
