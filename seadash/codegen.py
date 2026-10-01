"""Code generation: checked syntax tree -> C++23 source.

The checker has already done the thinking: every expression has a type
(`.ty`) and every name/call knows what it refers to (`.sym`). This module
turns that into C++, leaning on the runtime header (seadash.hpp) for anything
with Python-specific behaviour.

Layout of a generated file:

    #include "seadash.hpp"
    namespace prog {
    struct Point;                        // forward declarations
    double dist(const Point& p);         // function prototypes
    struct Point { ... };                // structs/classes (value deps first)
    std::int64_t LIMIT{};                // module-level globals
    double dist(const Point& p) { ... }  // method and function bodies
    void module_main() { ... }           // the module's top-level code
    }
    int main(int argc, char** argv) { return sd::run_main(argc, argv, prog::module_main); }

Conventions:
  * All locals are declared ("hoisted") at the top of their function, which
    maps cleanly onto Python's function-wide scoping and the checker's
    one-C++-variable-per-(name, type) rebinding.
  * Every user identifier goes through `ident()`, which avoids C++ keywords;
    compiler temporaries start with `sd_`, which user names never do.
  * Parameters a function never modifies are passed by const reference.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from . import ast as A
from . import builtins
from .checker import CallTarget, Dunder, ModuleInfo
from .flow import last_use, loop_by_reference, mark_copy_outs, sub_blocks
from .flow import walk as walk_nodes
from .threads import MUTATING_METHODS  # (a parameter changed by one of these is passed by value)
from .types import (
    SYNC_CPP, BOOL, BYTES, FLOAT, INT, JSON_VALUE, NONE, PATH, SOCKET, STR, SyncType, DATETIME_TYPES, DATETIME,
    BuiltinClass, STR_TEMPLATE, HASH, HMAC_T, EXECUTOR, FutureType, LOGGER, LOG_HANDLER, UUID_T, SQLITE_CONNECTION, SQLITE_CURSOR, StructFormatType,
    CSV_WRITER, CSV_DICT_READER, CSV_DICT_WRITER, HTTP_CONNECTION,
    PARSER, ParserType, SubParsersType, HTTPServerType, CmpKeyType, ContextManagerType, EXIT_STACK,
    CounterType, DefaultDictType, DequeType, DictType, GeneratorType, MatchType, NamespaceType, PatternType, ProcessType,
    VarTupleType, FileType, FuncInfo, FuncType, IterType, ListType, OptionalType, SetType, StructType, strip_optional,
    TupleType, Type, Var, element_type, is_numeric, user_dunder, ClassRefType, enum_decays, enum_flag_op, enum_mixin,
)

CPP_KEYWORDS = frozenset(
    """
    alignas alignof and and_eq asm auto bitand bitor bool break case catch char char8_t
    char16_t char32_t class compl concept const consteval constexpr constinit const_cast
    continue co_await co_return co_yield decltype default delete do double dynamic_cast
    else enum explicit export extern false float for friend goto if inline int long
    mutable namespace new noexcept not not_eq nullptr operator or or_eq private protected
    public register reinterpret_cast requires return short signed sizeof static
    static_assert static_cast struct switch template this thread_local throw true try
    typedef typeid typename union unsigned using virtual void volatile wchar_t while xor
    xor_eq main std sd prog module_main NULL
    stdin stdout stderr errno st_atime st_mtime st_ctime fileno
    """.split()  # (the last line: C library names that are macros on some systems)
)


MATH_FLOAT_1 = {"sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "exp", "log2", "log10", "fabs"}
MATH_VALUES = {
    "pi": "std::numbers::pi",
    "e": "std::numbers::e",
    "tau": "(2 * std::numbers::pi)",
    "inf": "std::numeric_limits<double>::infinity()",
    "nan": "std::numeric_limits<double>::quiet_NaN()",
}


@dataclass
class ModuleUnit:
    """One .sd module to generate: its tree, check results, and C++ namespace."""

    module: A.Module
    info: ModuleInfo
    name: str = "__main__"
    namespace: str = "prog"
    path: str = "<string>"  # the .sd file, for logging's %(pathname)s


# Which module is being generated, and every module's namespace: references to
# another module's functions/classes/globals are written with its namespace.
# Thread-local, since several programs may be compiled at once (the test suite does).
_QUALIFY = threading.local()


def qualified(name: str, module: str) -> str:
    current = getattr(_QUALIFY, "current", "__main__")
    namespaces = getattr(_QUALIFY, "namespaces", {})
    if module != current and module in namespaces:
        return f"{namespaces[module]}::{name}"
    return name


def generate(module: A.Module, info: ModuleInfo) -> str:
    return generate_program([ModuleUnit(module, info)])


def generate_program(units: list[ModuleUnit]) -> str:
    """The whole program as one C++ file: every module's namespace, dependencies first,
    then main(), which runs each module's top-level code in that same order."""
    _QUALIFY.namespaces = {u.name: u.namespace for u in units}
    imports = [m for u in units for m in u.info.imports]
    uses_json = any(m.name == "json" for m in imports)
    uses_copy = any(m.name == "copy" for m in imports)
    out = ["// Generated by the seadash compiler. Do not edit.", '#include "seadash.hpp"']
    out += [f'#include "{h}"' for h in dict.fromkeys(m.header for m in imports if m.header)]
    out.append("")
    for u in units:
        _QUALIFY.current = u.name
        gen = CodeGen(u.module, u.info, u.name, u.namespace, uses_json, uses_copy)
        gen.source_path = u.path
        out.append(gen.generate_namespace())
    inits = " ".join(f"{u.namespace}::module_main();" for u in units)
    out.append(f"int main(int argc, char** argv) {{ return sd::run_main(argc, argv, [] {{ {inits} }}); }}")
    return "\n".join(out) + "\n"


def ident(name: str) -> str:
    """A user identifier, made safe for C++."""
    if name == "_":
        return "_d"  # not "__": C++ reserves names with a double underscore (and a user's _d becomes _d_)
    if len(name) > 4 and name.startswith("__") and name.endswith("__"):
        return f"sd_op_{name[2:-2]}"  # __add__ -> sd_op_add (user names never start with sd_)
    if "__" in name:
        name = name.replace("__", "_u_")
    if name in CPP_KEYWORDS or name.startswith("sd_") or name.startswith("_"):
        return name + "_"
    return name


def local_name(st: StructType) -> str:
    """A struct/class's C++ name inside its own namespace (generic instances are mangled)."""
    return ident(st.mangled or st.name)


def fn_name(fn: FuncInfo) -> str:
    return ident(fn.cpp_name or fn.name)


def class_name(st: StructType) -> str:
    """The C++ name of a struct/class; built-in exceptions live in the runtime."""
    if st.cpp_name:
        return st.cpp_name
    return f"sd::{st.name}" if st.builtin else qualified(local_name(st), st.module)


def cpp_string(s: str) -> str:
    """A C++ std::string literal ("..."s) holding the UTF-8 bytes of `s`."""
    return cpp_bytes(s.encode("utf-8"))


def cpp_bytes(data: bytes) -> str:
    """A C++ std::string literal ("..."s) holding exactly these bytes (NULs included)."""
    out = []
    for b in data:
        c = chr(b)
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif 0x20 <= b < 0x7F:
            out.append(c)
        else:
            out.append(f"\\{b:03o}")  # octal escapes can't swallow following digits beyond 3
    return '"' + "".join(out) + '"s'


def float_literal(v: float) -> str:
    if v != v:
        return "std::numeric_limits<double>::quiet_NaN()"
    if v in (float("inf"), float("-inf")):
        return ("-" if v < 0 else "") + "std::numeric_limits<double>::infinity()"
    text = repr(v)
    return text if any(c in text for c in ".e") else text + ".0"


class CodeGen:
    def __init__(self, module: A.Module, info: ModuleInfo, name: str = "__main__",
                 namespace: str = "prog", uses_json: bool | None = None, uses_copy: bool | None = None):
        self.module = module
        self.info = info
        self.module_name = name
        self.namespace = namespace
        self.lines: list[str] = []
        self.depth = 0
        self.counter = 0
        self.func: FuncInfo | None = None
        # For `break` in loops with an else: [goto label, whether a break used it]
        self.loop_labels: list[list] = []
        # Inside a lambda that uses `self`, self is a captured copy named sd_self.
        self.lambda_self = 0
        self.source_path = "<string>"
        # Structs/classes get JSON conversion hooks when the program imports json (anywhere).
        self.uses_json = uses_json if uses_json is not None else any(m.name == "json" for m in info.imports)
        # Classes get copy.copy / copy.deepcopy hooks when the program imports copy (anywhere).
        self.uses_copy = uses_copy if uses_copy is not None else any(m.name == "copy" for m in info.imports)
        # Nested functions that call themselves: (FuncInfo, name of the self parameter).
        self.recursion: list[tuple[FuncInfo, str]] = []
        # Expressions already evaluated into temporaries (see `in_order`).
        self.precomputed: dict[int, str] = {}
        self.format_args: list[str] = []  # str.format() arguments, while building its f-string
        self.module_values = {
            id(v): (mod.name, name)
            for mod in builtins.MODULES.values()
            for name, v in mod.members.items()
            if isinstance(v, builtins.Value)
        }

    # ---- output helpers ----------------------------------------------------

    def line(self, text: str = "") -> None:
        self.lines.append(("    " * self.depth + text) if text else "")

    def open(self, header: str) -> None:
        self.line(f"{header} {{" if header else "{")
        self.depth += 1

    def close(self, trailer: str = "") -> None:
        self.depth -= 1
        self.line("}" + trailer)

    def fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"sd_{prefix}{self.counter}"

    # ---- types ---------------------------------------------------------------

    def cpp_type(self, t: Type) -> str:
        match t:
            case _ if t == INT:
                return "std::int64_t"
            case _ if t == FLOAT:
                return "double"
            case _ if t == BOOL:
                return "bool"
            case _ if t == STR:
                return "std::string"
            case _ if t == BYTES:
                return "sd::bytes"
            case _ if t == JSON_VALUE:
                return "sd::json::Value"
            case BuiltinClass():
                return t.cpp
            case HTTPServerType():
                return "sd::httpserver::HTTPServer"
            case CmpKeyType(elem):
                return f"sd::CmpKey<{self.cpp_type(elem)}>"
            case StructFormatType():
                return "sd::structmod::Struct"
            case FutureType(elem):
                return f"sd::futures::Future<{self.cpp_type(elem)}>"
            case ParserType() | SubParsersType():
                return "sd::argparse::ArgumentParser"
            case NamespaceType():
                return "sd::argparse::Namespace"
            case _ if t == NONE:
                return "void"
            case ListType(elem):
                return f"sd::list<{self.cpp_type(elem)}>"
            case SetType(elem):
                return f"sd::set<{self.cpp_type(elem)}>"
            case DequeType(elem):
                return f"sd::deque<{self.cpp_type(elem)}>"
            case VarTupleType(elem):
                return f"sd::vtuple<{self.cpp_type(elem)}>"
            case GeneratorType(elem):
                return f"sd::Generator<{self.cpp_type(elem)}>"
            case ContextManagerType(elem):
                return f"sd::contextlib::ContextManager<{self.cpp_type(elem)}>"
            case ProcessType(kind):
                return f"sd::subprocess::{kind}"
            case PatternType():
                return "sd::re::Pattern"
            case MatchType():
                return "sd::re::Match"
            case CounterType(key):
                return f"sd::Counter<{self.cpp_type(key)}>"
            case DefaultDictType(key, value):
                return f"sd::defaultdict<{self.cpp_type(key)}, {self.cpp_type(value)}>"
            case DictType(key, value):
                return f"sd::dict<{self.cpp_type(key)}, {self.cpp_type(value)}>"
            case TupleType(elts):  # (a None item is a std::nullopt_t, which shows as None)
                return f"std::tuple<{', '.join('std::nullopt_t' if e == NONE else self.cpp_type(e) for e in elts)}>"
            case OptionalType(inner):
                return f"std::optional<{self.cpp_type(inner)}>"
            case StructType(kind="class"):
                return f"std::shared_ptr<{class_name(t)}>"
            case StructType():
                return class_name(t)
            case IterType(elem):
                return f"sd::list<{self.cpp_type(elem)}>"
            case FuncType(params, ret):
                return f"std::function<{self.cpp_type(ret)}({', '.join(self.cpp_type(p) for p in params)})>"
            case SyncType(kind, args):
                inner = f"<{', '.join(self.cpp_type(a) for a in args)}>" if args else ""
                return SYNC_CPP[kind] + inner
            case FileType(binary):
                return f"std::shared_ptr<sd::{'BinaryFile' if binary else 'TextFile'}>"
        raise NotImplementedError(f"no C++ type for {t}")

    def coerce(self, code: str, src: Type, dst: Type) -> str:
        """Convert `code` (of type src) to dst: int -> float, T/None -> T?, tuples elementwise."""
        if src == dst or dst is None or isinstance(src, IterType):
            return code
        if isinstance(dst, GeneratorType) and not isinstance(src, GeneratorType):  # a list where an iterator is wanted
            return f"sd::iterate<{self.cpp_type(dst.elem)}>(sd::to_list({code}))"
        if isinstance(dst, VarTupleType) and isinstance(src, TupleType):  # (1, 2) as a tuple[int, ...]
            items = ", ".join(self.coerce(f"std::get<{i}>(sd_t)", t, dst.elem) for i, t in enumerate(src.elts))
            param = "sd_t" if src.elts else ""
            return f"[&](auto&& {param}) {{ return {self.cpp_type(dst)}{{{items}}}; }}({code})"
        if src == NONE:
            return f"{self.cpp_type(dst)}{{}}"
        return f"static_cast<{self.cpp_type(dst)}>({code})"

    def expr_as(self, e: A.Expr, dst: Type) -> str:
        return self.coerce(self.expr(e), e.ty, dst)

    # =========================================================================
    # Program structure
    # =========================================================================

    def generate_namespace(self) -> str:
        self.line(f"namespace {self.namespace} {{  // module {self.module_name}")
        self.line()

        structs = self.ordered_structs()
        if structs:
            for st in structs:
                self.line(f"struct {local_name(st)};")
            self.line()
        if self.info.functions:
            for fn in self.info.functions:
                self.line(self.signature(fn) + ";")
            self.line()
        for st in structs:
            self.struct_definition(st)
        hashed = [st for st in structs if st.find_method("__hash__") or st.enum is not None]
        if hashed:  # std::hash specializations must be declared in namespace std
            self.line(f"}}  // namespace {self.namespace}")
            for st in [st for st in hashed if st.enum is not None]:  # an enum member hashes as its index or bits
                full = f"{self.namespace}::{local_name(st)}"
                field = "sd_bits" if st.enum.flag else "sd_index"
                self.line(f"template <> struct std::hash<{full}> {{ std::size_t operator()(const {full}& x) const "
                          f"{{ return std::hash<std::int64_t>{{}}(x.{field}); }} }};")
            hashed = [st for st in hashed if st.enum is None]
            for st in hashed:
                full = f"{self.namespace}::{local_name(st)}"
                key = f"std::shared_ptr<{full}>" if st.kind == "class" else full
                call = "x->sd_op_hash()" if st.kind == "class" else f"const_cast<{full}&>(x).sd_op_hash()"
                self.line(f"template <> struct std::hash<{key}> {{ std::size_t operator()(const {key}& x) const "
                          f"{{ return static_cast<std::size_t>({call}); }} }};")
            self.line(f"namespace {self.namespace} {{")
            self.line()
        if self.info.globals:
            for var in self.info.globals:
                self.line(f"{self.cpp_type(var.type)} {ident(var.cpp_name)}{self.unset_init(var.type)};")
            self.line()
        for st in structs:
            self.struct_members(st)
        for fn in self.info.functions:
            self.function(fn)

        top_level = [
            getattr(s, "decorated", s) for s in self.module.body  # a decorated def: `f = deco(<f>)`
            if not isinstance(s, (A.FunctionDef, A.ClassDef, A.Import, A.ImportFrom)) or hasattr(s, "decorated")
        ]
        self.func = None
        mark_copy_outs(top_level)
        self.open("void module_main()")
        self.hoist(self.info.main_locals)
        self.block(top_level)
        self.close()
        self.line()
        self.line(f"}}  // namespace {self.namespace}")
        self.line()
        return "\n".join(self.lines)

    def ordered_structs(self) -> list[StructType]:
        """Structs in an order where anything held *by value* is defined first."""
        done: list[StructType] = []

        def value_deps(t: Type) -> list[StructType]:
            match t:
                case StructType(kind="struct"):
                    return [t]
                case OptionalType(inner):
                    return value_deps(inner)
                case TupleType(elts):
                    return [d for e in elts for d in value_deps(e)]
                case SetType(elem):
                    return value_deps(elem)
                case DictType(key, value):
                    return value_deps(key) + value_deps(value)
            return []  # lists (std::vector) and classes (shared_ptr) allow incomplete types

        def visit(st: StructType) -> None:
            if st in done:
                return
            if st.base is not None and not st.base.builtin:
                visit(st.base)  # a C++ base class must be defined first
            for f in st.fields.values():
                for dep in value_deps(f.type):
                    if dep is not st:
                        visit(dep)
            done.append(st)

        for st in self.info.structs:
            visit(st)
        return done

    def struct_definition(self, st: StructType) -> None:
        if st.enum is not None:
            self.enum_definition(st)
            return
        if st.is_exception:
            self.exception_definition(st)
            return
        if st.kind == "class":
            self.class_definition(st)
            return
        name = local_name(st)
        base = f" : std::enable_shared_from_this<{name}>" if st.kind == "class" else ""
        self.open(f"struct {name}{base}")
        for f in st.fields.values():
            init = f" = {self.expr_as(f.default, f.type)}" if f.default is not None else "{}"
            self.line(f"{self.cpp_type(f.type)} {ident(f.name)}{init};")
        if st.fields:
            self.line()
        self.line(f"{name}() = default;")
        if st.init is not None:
            params = ", ".join(["sd::init_t", *self.params(st.init)])
            self.line(f"{name}({params});")
        elif st.fields:
            params = ", ".join(f"{self.cpp_type(f.type)} sd_{f.name}" for f in st.fields.values())
            inits = ", ".join(
                f"{ident(f.name)}(sd::send(std::move(sd_{f.name})))" if holds_references(f.type) else f"{ident(f.name)}(std::move(sd_{f.name}))"
                for f in st.fields.values()
            )
            self.line(f"{name}({params}) : {inits} {{}}")
        if any(holds_references(f.type) for f in st.fields.values()) and not st.frozen:
            # A value class is a value all the way down: copying one copies its lists, dicts and
            # sets. (A frozen one's can't change, so its copies share them: copying is cheap.)
            copies = [(ident(f.name), f"sd::value_copy(o.{ident(f.name)})" if holds_references(f.type) else f"o.{ident(f.name)}")
                      for f in st.fields.values()]
            self.line(f"{name}(const {name}& o) : {', '.join(f'{n}({c})' for n, c in copies)} {{}}")
            self.line(f"{name}({name}&&) = default;")
            assigns = " ".join(f"{n} = {c};" for n, c in copies)
            self.line(f"{name}& operator=(const {name}& o) {{ if (this != &o) {{ {assigns} }} return *this; }}")
            self.line(f"{name}& operator=({name}&&) = default;")
        for m in st.methods.values():
            if m.name != "__init__":
                static = "static " if is_static(m) else ""
                self.line(f"{static}{self.cpp_type(m.ret)} {fn_name(m)}({', '.join(self.params(m))});")
                self.generator_declaration(st, m)
        self.line("std::string sd_repr() const;")
        self.protocol_members(st, name)
        self.class_attr_members(st)
        self.lazy_members(st)
        if st.kind == "struct":
            if st.find_method("__eq__"):
                self.line(f"bool operator==(const {name}& o) const {{ return const_cast<{name}*>(this)->sd_op_eq(o); }}")
            else:
                self.line(f"bool operator==(const {name}&) const = default;")
            if st.find_method("__lt__"):
                self.line(f"bool operator<(const {name}& o) const {{ return const_cast<{name}*>(this)->sd_op_lt(o); }}")
        if self.json_hooks(st):
            self.line("sd::json::Value sd_to_json() const;")
            self.line(f"static {self.cpp_type(st)} sd_from_json(const sd::json::Value& v, const std::string& path);")
        self.close(";")
        self.line()

    def enum_definition(self, st: StructType) -> None:
        """An enum: a member is an index into the class's table of members (sd_index), or for a
        flag the bits themselves (sd_bits). The table (modules/enum.hpp) has the names and values."""
        info, name = st.enum, local_name(st)
        vt = self.cpp_type(info.value_type)
        table = f"sd::enums::Table<{vt}>"
        field = "sd_bits" if info.flag else "sd_index"
        distinct = info.distinct
        index = {m.name: i for i, m in enumerate(distinct)}
        names = ", ".join(cpp_string(m.name) for m in distinct)
        values = ", ".join(self.enum_literal(m.value, info.value_type) for m in distinct)
        by_name = ", ".join(f"{{{cpp_string(m.name)}, {index[m.alias_of or m.name]}}}" for m in info.members.values())
        canonical = ", ".join(str(index[m.name]) for m in info.canonical) if info.flag else ""
        mask = 0  # a flag's bits: every member's
        for m in distinct if info.flag else []:
            mask |= m.value
        keep = "true" if info.base == "IntFlag" else "false"
        self.open(f"struct {name}")
        self.line(f"std::int64_t {field} = 0;")
        self.line()
        self.line(f"static const {table}& sd_table() {{")
        self.line(f"    static const {table} t{{{cpp_string(st.name)}, {{{names}}}, {{{values}}}, {{{by_name}}}, "
                  f"{{{canonical}}}, {mask}, {keep}}};")
        self.line("    return t;")
        self.line("}")
        at = "sd_table().values[i]" if info.flag else "i"
        self.line(f"static {name} sd_at(std::int64_t i) {{ {name} x; x.{field} = {at}; return x; }}")
        self.line(f"static {name} sd_by_name(const std::string& n) {{ return sd_at(sd::enums::lookup_name(sd_table(), n)); }}")
        if info.flag:
            self.line(f"static {name} sd_from_bits(std::int64_t b) {{ {name} x; x.sd_bits = b; return x; }}")
            self.line("std::optional<std::string> sd_name() const { return sd::enums::flag_name(sd_table(), sd_bits); }")
            self.line("const std::int64_t& sd_value() const { return sd_bits; }")
            self.line(f"static {name} sd_lookup(std::int64_t v) {{ return sd_from_bits(sd::enums::flag_value(sd_table(), v)); }}")
            self.line(f"static sd::list<{name}> sd_members() {{ sd::list<{name}> out; for (std::int64_t i : sd_table().canonical) "
                      f"out.push_back(sd_at(i)); return out; }}")
            self.line(f"sd::list<{name}> sd_iter() const {{ sd::list<{name}> out; for (std::int64_t i : "
                      f"sd::enums::flag_members(sd_table(), sd_bits)) out.push_back(sd_at(i)); return out; }}")
            self.line("std::size_t size() const { return sd::enums::flag_members(sd_table(), sd_bits).size(); }")
            for op in ("|", "&", "^"):
                self.line(f"friend {name} operator{op}({name} a, {name} b) {{ return sd_from_bits(a.sd_bits {op} b.sd_bits); }}")
                if info.base == "IntFlag":
                    self.line(f"friend {name} operator{op}({name} a, std::int64_t b) {{ return sd_from_bits(a.sd_bits {op} b); }}")
                    self.line(f"friend {name} operator{op}(std::int64_t a, {name} b) {{ return sd_from_bits(a {op} b.sd_bits); }}")
            self.line(f"{name} operator~() const {{ return sd_from_bits(sd::enums::flag_invert(sd_table(), sd_bits)); }}")
        else:
            self.line("const std::string& sd_name() const { return sd_table().names[sd_index]; }")
            self.line(f"const {vt}& sd_value() const {{ return sd_table().values[sd_index]; }}")
            self.line(f"static {name} sd_lookup(const {vt}& v) {{ return sd_at(sd::enums::lookup(sd_table(), v)); }}")
            self.line(f"static sd::list<{name}> sd_members() {{ sd::list<{name}> out; for (std::int64_t i = 0; i < "
                      f"{len(distinct)}; ++i) out.push_back(sd_at(i)); return out; }}")
        self_ = f"const_cast<{name}*>(this)"
        if "__repr__" in st.methods:
            repr_ = f"{self_}->sd_op_repr()"
        else:
            repr_ = "sd::enums::flag_repr(sd_table(), sd_bits)" if info.flag else "sd::enums::repr(sd_table(), sd_index)"
        if "__str__" in st.methods:
            str_ = f"{self_}->sd_op_str()"
        elif info.flag:
            str_ = "sd::enums::flag_str(sd_table(), sd_bits)"
        elif info.mixin is not None:
            str_ = "sd::str(sd_value())"  # IntEnum, StrEnum: the value
        else:
            str_ = f"{cpp_string(st.name + '.')} + sd_name()"
        self.line(f"std::string sd_repr() const {{ return {repr_}; }}")
        self.line(f"std::string sd_str() const {{ return {str_}; }}")
        if "__bool__" in st.methods:
            truthy = f"{self_}->sd_op_bool()"
        elif "__len__" in st.methods:
            truthy = f"{self_}->sd_op_len() != 0"
        elif info.flag:
            truthy = "sd_bits != 0"
        elif info.mixin is not None:
            truthy = "sd::truthy(sd_value())"
        else:
            truthy = "true"
        self.line(f"bool sd_truthy() const {{ return {truthy}; }}")
        # format(member, spec): its str(), or an IntEnum's / StrEnum's value (found by argument-dependent lookup)
        formatted = "x.sd_value()" if info.mixin is not None and "__str__" not in st.methods else "x.sd_str()"
        self.line(f"friend std::string format_value(const {name}& x, std::string_view spec) "
                  f"{{ return sd::format_any({formatted}, spec); }}")
        self.line(f"bool operator==(const {name}&) const = default;")
        if info.mixin is not None:  # an IntEnum member is an int wherever one is expected
            self.line(f"operator const {vt}&() const {{ return sd_value(); }}")
        if st.find_method("__lt__"):
            self.line(f"bool operator<(const {name}& o) const {{ return {self_}->sd_op_lt(o); }}")
        elif info.mixin is not None:
            self.line(f"bool operator<(const {name}& o) const {{ return sd_value() < o.sd_value(); }}")
        if "__iter__" in st.methods:
            self.line(f"auto sd_iter() const {{ return sd::iter({self_}->sd_op_iter()); }}")
        for m in st.methods.values():
            static = "static " if is_static(m) else ""
            self.line(f"{static}{self.cpp_type(m.ret)} {fn_name(m)}({', '.join(self.params(m))});")
            self.generator_declaration(st, m)
        self.close(";")
        self.line()

    def enum_literal(self, v: object, t: Type) -> str:
        """An enum member's value (worked out by the checker) as a C++ expression of type t."""
        if isinstance(v, tuple):
            return f"{self.cpp_type(t)}{{{', '.join(self.enum_literal(x, et) for x, et in zip(v, t.elts))}}}"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return "(-9223372036854775807_i - 1)" if v == -(2**63) else f"{v}_i"
        if isinstance(v, float):
            return float_literal(v)
        if isinstance(v, bytes):
            return f"sd::bytes({cpp_bytes(v)})"
        return cpp_string(v)

    def generator_declaration(self, st: StructType, m: FuncInfo) -> None:
        if m.generator and not is_static(m):
            params = ", ".join([f"{self.self_type(st)} sd_self", *self.params(m)])
            self.line(f"static {self.cpp_type(m.ret)} sd_gen_{fn_name(m)}({params});")

    def ctor_params(self, st: StructType) -> list[Type]:
        if st.init is not None:
            return [p.type for p in st.init.params]
        return [f.type for f in st.all_fields().values()]

    def own_init(self, st: StructType) -> bool:
        """Does this class define sd_init itself (its __init__, or generated field assignments)?"""
        return "__init__" in st.methods or st.init is None

    def class_definition(self, st: StructType) -> None:
        """A class: shared (std::shared_ptr) and polymorphic. Methods are virtual so calls
        through a base class reach overrides; construction runs sd_init (the __init__ body,
        or field assignments), which super().__init__(...) can call directly."""
        name = local_name(st)
        base = f" : public {class_name(st.base)}" if st.base else f" : public std::enable_shared_from_this<{name}>"
        self.open(f"struct {name}{base}")
        for f in st.fields.values():
            init = f" = {self.expr_as(f.default, f.type)}" if f.default is not None else "{}"
            self.line(f"{self.cpp_type(f.type)} {ident(f.name)}{init};")
        if st.fields:
            self.line()
        self.line(f"{name}() = default;")
        if st.base is None:
            self.line(f"virtual ~{name}() = default;")
        types = self.ctor_params(st)
        names = [f"sd_a{i}" for i in range(len(types))]
        owner = st if self.own_init(st) else st.init.owner
        params = ", ".join(["sd::init_t", *(f"{self.cpp_type(t)} {n}" for t, n in zip(types, names))])
        forward = ", ".join(f"std::move({n})" for n in names)
        self.line(f"{name}({params}) {{ this->{class_name(owner)}::sd_init({forward}); }}")
        if self.own_init(st):
            if "__init__" in st.methods:
                self.line(f"void sd_init({', '.join(self.params(st.methods['__init__']))});")
            else:
                self.line(f"void sd_init({', '.join(f'{self.cpp_type(t)} {n}' for t, n in zip(types, names))});")
        for key, m in st.methods.items():
            if m.name == "__init__":
                continue
            decl = f"{self.cpp_type(m.ret)} {fn_name(m)}({', '.join(self.params(m))})"
            self.generator_declaration(st, m)
            if is_static(m):
                self.line(f"static {decl};")
                continue
            overrides = st.base is not None and st.base.find_method(key) is not None
            self.line(f"{decl} override;" if overrides else f"virtual {decl};")
        self.line("std::string sd_repr() const override;" if st.base else "virtual std::string sd_repr() const;")
        virtual, override = ("", " override") if st.base else ("virtual ", "")  # (the runtime class, for dataclass __eq__)
        self.line(f'{virtual}std::string sd_class_name() const{override} {{ return {cpp_string(st.origin or st.name)}; }}')
        self.protocol_members(st, name)
        self.class_attr_members(st)
        self.lazy_members(st)
        self.handler_dispatch(st)
        if self.copy_hooks(st):
            root = class_name(st.ancestors()[-1])
            virtual, override = ("", " override") if st.base else ("virtual ", "")
            self.line(f"{virtual}std::shared_ptr<{root}> sd_copy() const{override};")
            self.line(f"{virtual}std::shared_ptr<{root}> sd_deep_copy(sd::CopyMemo& memo) const{override};")
        if self.json_hooks(st):
            virtual = "" if st.base and self.json_hooks(st.base) else "virtual "
            suffix = " override" if st.base and self.json_hooks(st.base) else ""
            self.line(f"{virtual}sd::json::Value sd_to_json() const{suffix};")
            self.line(f"static {self.cpp_type(st)} sd_from_json(const sd::json::Value& v, const std::string& path);")
        self.close(";")
        # Containers and algorithms (std::find, std::sort, dict keys) compare through these.
        ptr = f"std::shared_ptr<{name}>"
        if "__eq__" in st.methods:
            self.line(f"inline bool operator==(const {ptr}& a, const {ptr}& b) {{ return a->sd_op_eq(b); }}")
        if "__lt__" in st.methods:
            self.line(f"inline bool operator<(const {ptr}& a, const {ptr}& b) {{ return a->sd_op_lt(b); }}")
        self.line()

    def handler_dispatch(self, st: StructType) -> None:
        """A BaseHTTPRequestHandler subclass: the request's method calls its do_<METHOD>()."""
        if not st.is_subclass_of(builtins.HANDLER):
            return
        handlers = [m for key, m in st.methods.items() if key.startswith("do_")]
        if not handlers:
            return
        tests = " ".join(f"if (sd_command == {cpp_string(m.name[3:])}) {{ this->{fn_name(m)}(); return true; }}" for m in handlers)
        self.line(f"bool sd_dispatch(const std::string& sd_command) override {{ {tests} "
                  f"return {class_name(st.base)}::sd_dispatch(sd_command); }}")

    def class_attr_members(self, st: StructType) -> None:
        """`version = "1.0"`: Cls.version is sd_class_version(); obj.version is sd_attr_version(),
        virtual in a class, so a base class's method sees a subclass's value."""
        for ca in st.class_attrs.values():
            t = self.cpp_type(ca.type)
            self.line(f"static {t} sd_class_{ca.name}() {{ return {self.expr_as(ca.value, ca.type)}; }}")
            if st.kind != "class":
                self.line(f"{t} sd_attr_{ca.name}() const {{ return sd_class_{ca.name}(); }}")
            elif st.base is not None and st.base.find_class_attr(ca.name) is not None:
                self.line(f"{t} sd_attr_{ca.name}() const override {{ return sd_class_{ca.name}(); }}")
            else:
                self.line(f"virtual {t} sd_attr_{ca.name}() const {{ return sd_class_{ca.name}(); }}")

    def protocol_members(self, st: StructType, name: str) -> None:
        """Hooks the runtime uses for str(), truthiness and iteration, from __str__,
        __bool__/__len__ and __iter__. Classes make them virtual, so a subclass's
        dunder methods are found through a base-class reference."""
        self_ = f"const_cast<{name}*>(this)"
        is_class = st.kind == "class"
        root = is_class and st.base is None
        if "__str__" in st.methods or root:
            body = f"{self_}->sd_op_str()" if "__str__" in st.methods else "sd_repr()"
            prefix = "virtual " if root else ""
            suffix = " override" if is_class and not root else ""
            self.line(f"{prefix}std::string sd_str() const{suffix} {{ return {body}; }}")
        if "__bool__" in st.methods or "__len__" in st.methods or root:
            if "__bool__" in st.methods:
                body = f"{self_}->sd_op_bool()"
            elif "__len__" in st.methods:
                body = f"{self_}->sd_op_len() != 0"
            else:
                body = "true"
            prefix = "virtual " if root else ""
            suffix = " override" if is_class and not root else ""
            self.line(f"{prefix}bool sd_truthy() const{suffix} {{ return {body}; }}")
        if "__iter__" in st.methods:
            self.line(f"auto sd_iter() const {{ return sd::iter({self_}->sd_op_iter()); }}")

    def exception_definition(self, st: StructType) -> None:
        """`class NotFound(ValueError)` derives from the runtime's sd::ValueError."""
        name = local_name(st)
        self.open(f"struct {name} : {class_name(st.base)}")
        for f in st.fields.values():
            init = f" = {self.expr_as(f.default, f.type)}" if f.default is not None else "{}"
            self.line(f"{self.cpp_type(f.type)} {ident(f.name)}{init};")
        self.line(f"{name}() = default;")
        if st.init is not None:
            self.line(f"{name}({', '.join(['sd::init_t', *self.params(st.init)])});")
        else:
            fields = list(st.all_fields().values())
            params = ", ".join(f"{self.cpp_type(f.type)} sd_{f.name}" for f in fields)
            sets = " ".join(f"this->{ident(f.name)} = std::move(sd_{f.name});" for f in fields)
            self.line(f"{name}({params}) {{ {sets} }}")
        for m in st.methods.values():
            if m.name != "__init__":
                self.line(f"{self.cpp_type(m.ret)} {ident(m.name)}({', '.join(self.params(m))});")
        self.line(f'std::string sd_type() const override {{ return "{st.name}"; }}')
        self.close(";")
        self.line()

    def struct_members(self, st: StructType) -> None:
        name = local_name(st)
        if st.is_exception:
            for m in st.methods.values():
                if m.name == "__init__":
                    self.function_body(m, f"{name}::{name}({', '.join(['sd::init_t', *self.params(m)])})")
                else:
                    self.function_body(m, f"{self.cpp_type(m.ret)} {name}::{ident(m.name)}({', '.join(self.params(m))})")
            return
        if st.enum is not None:  # (its repr and str are in its definition)
            for m in st.methods.values():
                if m.generator and not is_static(m):
                    self.generator_method(m, name)
                else:
                    self.function_body(m, f"{self.cpp_type(m.ret)} {name}::{fn_name(m)}({', '.join(self.params(m))})")
            return
        fields = list(st.all_fields().values())
        parts = [cpp_string(f"{st.origin or st.name}(")]
        for i, f in enumerate(fields):
            parts.append(cpp_string(("" if i == 0 else ", ") + f"{f.name}="))
            parts.append(f"sd::repr({ident(f.name)})")
        parts.append(cpp_string(")"))
        lock = "std::lock_guard<std::recursive_mutex> sd_lock(this->sd_mutex); " if is_synchronized(st) else ""
        body = " + ".join(parts)
        if st.find_method("__repr__"):  # the user's own repr
            body = f"const_cast<{name}*>(this)->sd_op_repr()"
        self.line(f"std::string {name}::sd_repr() const {{ {lock}return {body}; }}")
        self.line()
        if self.json_hooks(st):
            self.json_members(st)
        if self.copy_hooks(st):
            self.copy_members(st)
        if st.kind == "class" and self.own_init(st) and "__init__" not in st.methods:
            fields_all = list(st.all_fields().values())
            params = ", ".join(f"{self.cpp_type(f.type)} sd_a{i}" for i, f in enumerate(fields_all))
            sets = " ".join(
                f"this->{ident(f.name)} = sd::send(std::move(sd_a{i}));" if is_synchronized(st) and holds_references(f.type)
                else f"this->{ident(f.name)} = std::move(sd_a{i});"
                for i, f in enumerate(fields_all)
            )
            self.line(f"void {name}::sd_init({params}) {{ {sets} }}")
            self.line()
        for m in st.methods.values():
            if m.name == "__init__" and st.kind == "class":
                self.function_body(m, f"void {name}::sd_init({', '.join(self.params(m))})")
            elif m.name == "__init__":
                params = ", ".join(["sd::init_t", *self.params(m)])
                self.function_body(m, f"{name}::{name}({params})")
            elif m.generator and not is_static(m):
                self.generator_method(m, name)
            else:
                self.function_body(m, f"{self.cpp_type(m.ret)} {name}::{fn_name(m)}({', '.join(self.params(m))})")

    def copy_hooks(self, st: StructType) -> bool:
        """A class (not a @value class, an exception, or one built on a runtime class such as
        Synchronized) gets virtual hooks for copy.copy and copy.deepcopy, which copy the object's
        runtime class."""
        return self.uses_copy and st.kind == "class" and not any(a.builtin for a in st.ancestors())

    def copy_members(self, st: StructType) -> None:
        name = local_name(st)
        root = class_name(st.ancestors()[-1])
        if (m := st.find_method("__copy__")) is not None:  # the class's own way to copy itself
            body = f"return const_cast<{name}*>(this)->{fn_name(m)}();"
        else:
            body = f"return std::make_shared<{name}>(*this);"
        self.line(f"std::shared_ptr<{root}> {name}::sd_copy() const {{ {body} }}")
        deep = " ".join(f"sd_out->{ident(f.name)} = sd::copymod::deep_copy(sd_out->{ident(f.name)}, memo);"
                        for f in st.all_fields().values())
        self.line(f"std::shared_ptr<{root}> {name}::sd_deep_copy(sd::CopyMemo& memo) const {{ "
                  f"auto sd_out = std::make_shared<{name}>(*this); sd::copymod::note_copy<{root}>(this, sd_out, memo); "
                  f"{deep} return sd_out; }}")
        self.line()

    def json_hooks(self, st: StructType) -> bool:
        return self.uses_json and not st.is_exception and (
            builtins.json_problem(st, decoding=False) is None or builtins.json_problem(st, decoding=True) is None
        )

    def json_members(self, st: StructType) -> None:
        """A struct/class as a JSON object: fields in declaration order, matched by name."""
        name = local_name(st)
        fields = list(st.all_fields().values())
        if builtins.json_problem(st, decoding=False) is None:
            self.open(f"sd::json::Value {name}::sd_to_json() const")
            self.line("sd::json::Value out = sd::json::Value::object();")
            for f in fields:
                self.line(f"out.set({cpp_string(f.name)}, sd::json::to_value({ident(f.name)}));")
            self.line("return out;")
            self.close()
            self.line()
        if builtins.json_problem(st, decoding=True) is None:
            self.open(f"{self.cpp_type(st)} {name}::sd_from_json(const sd::json::Value& v, const std::string& path)")
            if st.kind == "class":
                self.line(f"auto out = std::make_shared<{name}>();")
                target = "out->"
            else:
                self.line(f"{name} out;")
                target = "out."
            self.line("sd::json::expect_object(v, path);")
            for f in fields:
                self.line(f"if (const sd::json::Value* sd_f = v.find({cpp_string(f.name)})) {{")
                self.line(f"    {target}{ident(f.name)} = sd::json::decode<{self.cpp_type(f.type)}>"
                          f"(*sd_f, path + {cpp_string('.' + f.name)});")
                if f.default is None and not isinstance(f.type, OptionalType):
                    self.line(f"}} else {{ sd::json::missing_field({cpp_string(f.name)}, path); }}")
                else:
                    self.line("}")
            self.line("return out;")
            self.close()
            self.line()

    def signature(self, fn: FuncInfo) -> str:
        return f"{self.cpp_type(fn.ret)} {fn_name(fn)}({', '.join(self.params(fn))})"

    def params(self, fn: FuncInfo) -> list[str]:
        """C++ parameters. The signature depends only on the types (so an override always
        matches its base method): small values by value, the rest by const reference.
        A body that modifies or captures a const& parameter works on its own copy
        (see local_params)."""
        out = []
        mentioned = {n.id for n in walk_nodes(fn.node.body) if isinstance(n, A.Name)}
        for p, var in zip(fn.params, self.param_vars(fn)):
            t = self.cpp_type(p.type)
            unused = "" if p.name in mentioned else "[[maybe_unused]] "  # (an override that ignores one: log_message)
            if by_value(p.type) or fn.generator:  # (a generator keeps running after the call: it needs its own copy)
                name = f"sd_arg_{var.cpp_name}" if var.captured else ident(p.name)
                out.append(f"{unused}{t} {name}")
            else:
                own_copy = var.captured or p.name in modified_names(fn.node.body) or synchronized_copy(fn, p.type)
                out.append(f"{unused}const {t}& {f'sd_arg_{var.cpp_name}' if own_copy else ident(p.name)}")
        return out

    def function(self, fn: FuncInfo) -> None:
        if not fn.cached:
            self.function_body(fn, self.signature(fn))
            return
        # @functools.cache: the real body under another name, and a memoizing wrapper. The lock
        # isn't held while computing, so recursive calls (and other threads) can use the cache.
        uncached = f"sd_uncached_{fn_name(fn)}"
        self.function_body(fn, f"{self.cpp_type(fn.ret)} {uncached}({', '.join(self.params(fn))})")
        types = [self.cpp_type(p.type) for p in fn.params]
        names = [f"sd_p{i}" for i in range(len(types))]
        ret = self.cpp_type(fn.ret)
        self.open(f"{ret} {fn_name(fn)}({', '.join(f'{t} {n}' for t, n in zip(types, names))})")
        self.line("static std::mutex sd_mu;")
        self.line(f"static sd::dict<std::tuple<{', '.join(types)}>, {ret}> sd_cache;")
        self.line(f"std::tuple<{', '.join(types)}> sd_key{{{', '.join(names)}}};")
        self.open("")
        self.line("std::lock_guard sd_lock(sd_mu);")
        self.line("if (const auto* sd_hit = sd_cache.find(sd_key)) return *sd_hit;")
        self.close()
        self.line(f"{ret} sd_result = {uncached}({', '.join(names)});")
        self.line("std::lock_guard sd_lock(sd_mu);")
        self.line("sd_cache[sd_key] = sd_result;")
        self.line("return sd_result;")
        self.close()
        self.line()

    def generator_method(self, fn: FuncInfo, owner: str) -> None:
        """A generator method is a coroutine, and its body only runs when the first value
        is wanted -- the object may be gone by then. So the method just calls a static
        coroutine, passing (at the call) a reference to the object (a class) or a copy
        of it (a struct value)."""
        params = self.params(fn)
        names = [p.rsplit(" ", 1)[1] for p in params]
        ret = self.cpp_type(fn.ret)
        self_var = fn.node.params[0].sym
        self.open(f"{ret} {owner}::{fn_name(fn)}({', '.join(params)})")
        self.line(f"return sd_gen_{fn_name(fn)}({', '.join([self.var_code(self_var, self_var.type), *names])});")
        self.close()
        self.line()
        header = f"{ret} {owner}::sd_gen_{fn_name(fn)}({', '.join([f'{self.self_type(fn.owner)} sd_self', *params])})"
        self.function_body(fn, header, coroutine_self=True)

    def self_type(self, st: StructType) -> str:
        return self.cpp_type(st) if st.kind == "class" else local_name(st)

    def function_body(self, fn: FuncInfo, header: str, coroutine_self: bool = False) -> None:
        self.func = fn
        self.open(header)
        if fn.owner is not None and fn.name != "__init__" and not is_static(fn) and is_synchronized(fn.owner):
            self.line("std::lock_guard<std::recursive_mutex> sd_lock(this->sd_mutex);  // Synchronized")
        if coroutine_self:  # a generator method's body reaches its object through sd_self
            self.lambda_self += 1
        self.hoist(fn.locals)
        self.hoist_makers(fn.node.body)
        mark_copy_outs(fn.node.body)
        self.cell_params(fn)
        self.block(fn.node.body)
        if coroutine_self:
            self.lambda_self -= 1
        if isinstance(fn.ret, OptionalType) and not ends_with_return(fn.node.body):
            self.line("return std::nullopt;")
        self.close()
        self.line()

    def hoist(self, variables: list[Var]) -> None:
        for var in variables:
            if var.kind == "global":
                continue
            t = self.cpp_type(var.type)
            unset = "sd::unset" if self.unset_init(var.type) != "{}" else ""
            if var.captured:  # shared with a closure: a cell both sides point to
                self.line(f"std::shared_ptr<{t}> {ident(var.cpp_name)} = std::make_shared<{t}>({unset});")
            else:
                # Python doesn't mind a variable that's assigned but never read (`for _ in ...`, `a, b = t`)
                self.line(f"[[maybe_unused]] {t} {ident(var.cpp_name)}{self.unset_init(var.type)};")

    def unset_init(self, t: Type) -> str:
        """How a variable is declared before its first assignment: a list, dict or set with no
        storage yet (the checker makes sure it's assigned before it's read)."""
        if isinstance(t, (ListType, SetType, DictType, DequeType, CounterType, DefaultDictType)):
            return "{sd::unset}"
        return "{}"

    def param_vars(self, fn: FuncInfo) -> list[Var]:
        nodes = fn.node.params[1:] if fn.owner is not None and fn.kind != "static" else fn.node.params
        return [p.sym for p in nodes]

    def cell_params(self, fn: FuncInfo) -> None:
        """Parameters captured by a closure are copied into cells on entry; parameters the
        body modifies get a local variable (a struct is copied, so the caller's is untouched;
        a list is a shared handle, so changes reach the caller as in Python)."""
        modified = modified_names(fn.node.body)
        for var in self.param_vars(fn):
            t = self.cpp_type(var.type)
            arg = f"sd_arg_{var.cpp_name}"
            if synchronized_copy(fn, var.type):  # a Synchronized object keeps nothing its caller can reach
                arg = f"sd::value_copy({arg})"
            if var.captured:
                self.line(f"std::shared_ptr<{t}> {ident(var.cpp_name)} = std::make_shared<{t}>({arg});")
            elif not fn.generator and ((var.name in modified and not by_value(var.type)) or synchronized_copy(fn, var.type)):
                # (a generator's parameters are already its own: see params())
                self.line(f"{t} {ident(var.cpp_name)} = {arg};")

    # =========================================================================
    # Statements
    # =========================================================================

    def block(self, stmts: list[A.Stmt]) -> None:
        for s in stmts:
            self.stmt(s)

    def stmt(self, s: A.Stmt) -> None:
        match s:
            case A.ExprStmt(A.StrLit()):
                pass  # docstring
            case A.ExprStmt(value):
                self.line(f"{self.expr(value)};")
            case A.Assign(targets, value):
                code = self.expr(value)
                if len(targets) == 1:
                    self.assign(targets[0], code, value.ty)
                else:
                    tmp = self.fresh("v")
                    self.open("")
                    self.line(f"auto {tmp} = {code};")
                    for t in targets:
                        self.assign(t, tmp, value.ty)
                    self.close()
            case A.AnnAssign(target, _, value):
                self.assign(target, self.expr(value), value.ty)
            case A.AugAssign(target, op, value):
                self.aug_assign(s, target, op, value)
            case A.Pass():
                pass
            case A.Break():
                label = self.loop_labels[-1]
                if label[0]:
                    label[1] = True
                    self.line(f"goto {label[0]};")
                else:
                    self.line("break;")
            case A.Continue():
                self.line("continue;")
            case A.Return(value):
                self.return_stmt(value)
            case A.Yield(value, from_):
                self.yield_stmt(value, from_)
            case A.Assert(test, msg):
                message = f"sd::str({self.expr(msg)})" if msg is not None else '""s'
                self.line(f'if (!{self.cond(test)}) sd::raise("AssertionError", {message});')
            case A.FunctionDef():
                self.nested_def(s)
            case A.Nonlocal() | A.Global():
                pass
            case A.Raise(exc):
                if exc is None:
                    self.line("throw;")
                elif isinstance(s.sym, StructType):  # `raise ValueError`
                    self.line(f"throw sd::Thrown{{std::make_shared<{class_name(s.sym)}>()}};")
                else:
                    self.line(f"throw sd::Thrown{{{self.expr(exc)}}};")
            case A.Try():
                self.try_stmt(s)
            case A.With(items, body):
                self.with_stmt(items, body)
            case A.If():
                self.if_stmt(s)
            case A.Match():
                self.match_stmt(s)
            case A.While(test, body, orelse):
                self.loop(f"while ({unwrapped(self.cond(test))})", body, orelse)
            case A.For(target, it, body, orelse):
                v = self.fresh("v")
                elem = element_type(it.ty)
                if self.func is not None and loop_by_reference(self.func.node.body, s):
                    # Nothing can tell the item from a copy: refer to it (no copy per iteration).
                    prologue = lambda: self.line(f"[[maybe_unused]] auto& {ident(target.sym.cpp_name)} = {v};")
                else:
                    prologue = lambda: self.assign(target, v, elem)
                self.loop(f"for (auto&& {v} : sd::iter({self.expr(it)}))", body, orelse, prologue=prologue)
            case _:
                raise NotImplementedError(f"codegen for {type(s).__name__}")

    def maker_signature(self, s: A.FunctionDef) -> tuple[str, str]:
        """A nested def that a thread runs is built by a maker from the variables it captures,
        so the thread's copy can be built from copies of them (threads.Spawn.check_captures)."""
        info: FuncInfo = s.sym
        cells = ", ".join(f"std::shared_ptr<{self.cpp_type(v.type)}>" for v in s.snapshot)
        return f"sd_mk_{ident(info.var.cpp_name)}", f"std::function<{self.cpp_type(info.var.type)}({cells})>"

    def hoist_makers(self, body: list[A.Stmt]) -> None:
        todo = list(body)
        while todo:
            stmt = todo.pop()
            if isinstance(stmt, A.FunctionDef) and getattr(stmt, "snapshot", None) is not None:
                name, t = self.maker_signature(stmt)
                self.line(f"{t} {name};")
            for sub in sub_blocks(stmt):
                todo.extend(sub)

    def snapshot_call(self, s: A.FunctionDef) -> str:
        """The thread's own copy of a nested def: rebuilt around copies of what it captures."""
        name, _ = self.maker_signature(s)
        cells = ", ".join(f"std::make_shared<{self.cpp_type(v.type)}>(sd::value_copy({self.var_ref(v)}))" for v in s.snapshot)
        return f"{name}({cells})"

    def nested_def(self, s: A.FunctionDef) -> None:
        """A nested def becomes a C++ lambda stored in its (std::function) variable.

        Closures capture by value ([=]); variables they share with the enclosing
        function are cells (shared_ptr), so copying the pointer shares the variable.
        A function that calls itself takes itself as an extra parameter instead of
        capturing its own cell, which would be a reference cycle.
        """
        from .checker import walk

        info: FuncInfo = s.sym
        ret = self.cpp_type(info.ret)
        params = self.params(info)
        capture = "[=]"
        self_names = [n for n in walk(s.body) if isinstance(n, A.Name) and is_self(n)]
        if self_names:
            capture = f"[=, sd_self = {self.var_code(self_names[0].sym, self_names[0].sym.type)}]"
        recursive = any(isinstance(n, A.Call) and isinstance(n.sym, CallTarget) and n.sym.kind == "self_call"
                        and n.sym.target is info for n in walk(s.body))
        snapshot = getattr(s, "snapshot", None)
        if recursive:
            fn, rec = self.fresh("fn"), self.fresh("rec")
            self.recursion.append((info, rec))
            header = f"auto {fn} = {capture}(auto& {rec}{''.join(', ' + p for p in params)}) mutable -> {ret}"
        elif snapshot is not None:  # built by a maker, so a thread can have one built from copies
            maker, _ = self.maker_signature(s)
            cells = ", ".join(f"std::shared_ptr<{self.cpp_type(v.type)}> {ident(v.cpp_name)}" for v in snapshot)
            header = (f"{maker} = {capture}({cells}) -> {self.cpp_type(info.var.type)} {{ return "
                      f"[=]({', '.join(params)}) mutable -> {ret}")
        else:
            header = f"{self.var_ref(info.var)} = {capture}({', '.join(params)}) mutable -> {ret}"
        saved = (self.func, self.loop_labels, self.lambda_self)
        self.func, self.loop_labels = info, []
        if self_names:
            self.lambda_self += 1
        self.open(header)
        self.hoist(info.locals)
        self.hoist_makers(s.body)
        self.cell_params(info)
        self.block(s.body)
        if isinstance(info.ret, OptionalType) and not ends_with_return(s.body):
            self.line("return std::nullopt;")
        self.close("; };" if snapshot is not None and not recursive else ";")
        self.func, self.loop_labels, self.lambda_self = saved
        if snapshot is not None and not recursive:
            maker, _ = self.maker_signature(s)
            self.line(f"{self.var_ref(info.var)} = {maker}({', '.join(ident(v.cpp_name) for v in snapshot)});")
        if recursive:
            self.recursion.pop()
            names = [self.fresh("p") for _ in info.params]
            typed = ", ".join(f"{self.cpp_type(p.type)} {n}" for p, n in zip(info.params, names))
            call = f"{fn}({', '.join([fn, *names])})"
            self.line(f"{self.var_ref(info.var)} = [{fn}]({typed}) mutable -> {ret} {{ return {call}; }};")

    def yield_stmt(self, value: A.Expr | None, from_: bool) -> None:
        elem = self.func.ret.elem
        if self.func.context_manager and elem == NONE:  # (a @contextmanager's bare `yield`)
            if value is not None:
                self.line(f"{self.expr(value)};")
            self.line("co_yield std::monostate{};")
            return
        if from_:
            v = self.fresh("y")
            source = element_type(value.ty) if not hasattr(value, "tuple_elem") else value.tuple_elem
            self.line(f"for (auto&& {v} : sd::iter({self.expr(value)})) co_yield {self.coerce(v, source, elem)};")
        elif value is None:
            self.line(f"co_yield {self.cpp_type(elem)}{{}};")
        else:
            self.line(f"co_yield {self.expr_as(value, elem)};")

    def return_stmt(self, value: A.Expr | None) -> None:
        if self.func is not None and self.func.generator:
            self.line("co_return;")
            return
        ret = self.func.ret if self.func else NONE
        if self.func is not None and self.func.name == "__init__":
            self.line("return;")
        elif value is None:
            self.line("return std::nullopt;" if isinstance(ret, OptionalType) else "return;")
        elif synchronized_copy(self.func, ret):
            self.line(f"return sd::value_copy({self.expr_as(value, ret)});")  # nothing escapes the lock shared
        else:
            self.line(f"return {self.expr_as(value, ret)};")

    def try_stmt(self, s: A.Try) -> None:
        if not s.finalbody:
            self.try_except(s)
            return
        # The Finally guard runs the block on normal exits (and return/break/
        # continue); the catch-all runs it on the exception path and rethrows.
        fin = self.fresh("finally")
        self.open("")
        self.line(f"sd::Finally {fin}([&] {{")
        self.depth += 1
        self.block(s.finalbody)
        self.depth -= 1
        self.line("});")
        self.open("try")
        self.try_except(s)
        self.depth -= 1
        self.line("} catch (...) {")
        self.depth += 1
        self.line(f"{fin}.run_now();")
        self.line("throw;")
        self.close()
        self.close()

    def with_stmt(self, items: list[A.WithItem], body: list[A.Stmt]) -> None:
        """Each item: evaluate, __enter__, then guarantee __exit__ (or close()) on every way out.

        Like try/finally, a Finally guard handles normal exits and return/break;
        exceptions are caught to call __exit__ outside of stack unwinding, which
        also lets an __exit__ that takes the exception decide to swallow it.
        """
        if not items:
            self.block(body)
            return
        item = items[0]
        info = item.sym
        ctx = self.fresh("ctx")
        if info.kind in ("lock", "mutex", "rw_read", "rw_write"):
            self.open("")
            if info.kind == "lock":  # held for the block, released on every way out (RAII)
                self.line(f"sd::threading::Held<{self.cpp_type(item.context.ty)}> {ctx}({self.expr(item.context)});")
                if item.target is not None:
                    self.assign(item.target, "true", BOOL)
            else:  # `with m as data:`: data is a reference into the protected value while locked
                lock = "" if info.kind != "mutex" else ".lock()"  # (m.read() / m.write() are the guard already)
                self.line(f"auto {ctx} = {self.expr(item.context)}{lock};")
                if item.target is not None:
                    var: Var = item.target.sym
                    const = "const " if info.kind == "rw_read" else ""
                    self.line(f"{const}{self.cpp_type(var.type)}& {ident(var.cpp_name)} = {ctx}.value();")
            self.with_stmt(items[1:], body)
            self.close()
            return
        self.open("")
        self.line(f"auto {ctx} = {self.expr(item.context)};")
        exit_param = None
        if info.kind in ("contextlib", "exitstack"):
            self.contextlib_with(item, ctx, items[1:], body)
            self.close()
            return
        if info.kind == "file":
            enter, exit_call = ctx, f"{ctx}->close()"
        elif info.kind == "socket":
            enter, exit_call = ctx, f"{ctx}.close()"
        elif info.kind == "process":
            enter, exit_call = ctx, f"{ctx}.sd_exit()"
        elif info.kind == "tempdir":
            enter, exit_call = f"{ctx}.name()", f"{ctx}.cleanup()"
        elif info.kind == "executor":
            enter, exit_call = ctx, f"{ctx}.shutdown(true, false)"
        elif info.kind == "response":
            enter, exit_call = ctx, f"{ctx}.close()"
        elif info.kind == "connection":
            enter, exit_call = ctx, f"{ctx}.sd_exit(false)"  # commit; the catch below rolls back
        elif info.kind == "server":
            enter, exit_call = ctx, f"{ctx}.server_close()"
        else:
            st: StructType = item.context.ty
            arrow = "->" if st.kind == "class" else "."
            enter = f"{ctx}{arrow}{ident('__enter__')}()"
            exit_fn = f"{ctx}{arrow}{ident('__exit__')}"
            if info.exit.params:
                exit_param = info.exit.params[0].type  # an optional exception class
                exit_call = f"{exit_fn}({self.cpp_type(exit_param)}{{}})"
            else:
                exit_call = f"{exit_fn}()"
        if item.target is not None:
            self.assign(item.target, enter, info.enter_type)
        elif info.kind not in ("file", "socket", "process", "tempdir", "executor", "response", "connection", "server"):  # still call __enter__ for its effects
            self.line(f"{enter};")
        guard = self.fresh("with")
        self.line(f"sd::Finally {guard}([&] {{ {exit_call}; }});")
        self.open("try")
        self.with_stmt(items[1:], body)
        self.depth -= 1
        if exit_param is not None:
            caught = self.fresh("exc")
            exc_class = class_name(exit_param.inner)
            self.line(f"}} catch (const sd::Thrown& {caught}) {{")
            self.depth += 1
            self.line(f"{guard}.disarm();")
            passed = f"{self.cpp_type(exit_param)}(std::dynamic_pointer_cast<{exc_class}>({caught}.exc))"
            if info.suppresses:
                self.line(f"if (!{exit_fn}({passed})) throw;  // __exit__ returned True: swallow it")
            else:
                self.line(f"{exit_fn}({passed});")
                self.line("throw;")
            self.depth -= 1
        self.line("} catch (...) {")
        self.depth += 1
        if info.kind == "connection":
            self.line(f"{guard}.disarm();")
            self.line(f"{ctx}.sd_exit(true);")
        else:
            self.line(f"{guard}.run_now();")
        self.line("throw;")
        self.close()
        self.close()

    def contextlib_with(self, item: A.WithItem, ctx: str, rest: list[A.WithItem], body: list[A.Stmt]) -> None:
        """contextlib's context managers and ExitStack: sd_exit(exception) runs the exit (it
        may swallow the exception, or raise another)."""
        info = item.sym
        enter = ctx if info.kind == "exitstack" else f"{ctx}.enter()"
        if item.target is not None:
            self.assign(item.target, enter, info.enter_type)
        elif info.kind != "exitstack":
            self.line(f"{enter};")
        guard = self.fresh("with")
        self.line(f"sd::Finally {guard}([&] {{ {ctx}.sd_exit(nullptr); }});")
        self.open("try")
        self.with_stmt(rest, body)
        self.depth -= 1
        self.line("} catch (...) {")
        self.depth += 1
        self.line(f"{guard}.disarm();")
        if info.suppresses:
            self.line(f"if (!{ctx}.sd_exit(std::current_exception())) throw;  // (true: swallowed)")
        else:
            self.line(f"{ctx}.sd_exit(std::current_exception());")
            self.line("throw;")
        self.close()

    def enter_context(self, stack: str, e: A.Call) -> str:
        """stack.enter_context(cm): enter it now, and push its exit (taking the exception in
        flight, returning true to swallow it) onto the stack."""
        info = e.with_info
        arg = e.args[0]
        if info.kind in ("contextlib", "exitstack"):
            return f"sd::contextlib::enter_context({stack}, {self.expr(arg)})"
        t = arg.ty
        c, ex = "sd_c", "sd_e"
        make = self.expr(arg)
        if isinstance(t, StructType) and t.kind != "class":  # a @value class: entered and exited in one place
            make = f"std::make_shared<{class_name(t)}>({make})"
        exits = {
            "file": f"{c}->close();", "socket": f"{c}.close();", "process": f"{c}.sd_exit();",
            "tempdir": f"{c}.cleanup();", "executor": f"{c}.shutdown(true, false);", "response": f"{c}.close();",
            "connection": f"{c}.sd_exit({ex} != nullptr);", "server": f"{c}.server_close();",
        }
        if info.kind in exits:
            enter = f"{c}.name()" if info.kind == "tempdir" else c
            exit_code = f"{exits[info.kind]} return false;"
        else:  # a class with __enter__ and __exit__
            enter = f"{c}->{ident('__enter__')}()"
            exit_fn = f"{c}->{ident('__exit__')}"
            if info.exit.params:
                opt = info.exit.params[0].type
                passed = (f"[&] {{ auto sd_x = sd::contextlib::thrown_as<{class_name(opt.inner)}>({ex}); "
                          f"return sd_x ? {self.cpp_type(opt)}(sd_x) : {self.cpp_type(opt)}(); }}()")
                exit_code = (f"bool sd_r = {exit_fn}({passed}); return {ex} && sd_r;" if info.suppresses
                             else f"{exit_fn}({passed}); return false;")
            else:
                exit_code = f"{exit_fn}(); return false;"
        push = f"{stack}.push_exit([{c}]([[maybe_unused]] std::exception_ptr {ex}) mutable -> bool {{ {exit_code} }});"
        if info.enter_type == NONE:
            return f"[&] {{ auto {c} = {make}; {enter}; {push} }}()"
        return f"[&] {{ auto {c} = {make}; auto sd_v = {enter}; {push} return sd_v; }}()"

    def contextlib_call(self, name: str, e: A.Call) -> str:
        if name == "nullcontext":
            node = e.args[0] if e.args else self.keyword(e, "enter_result")
            if e.ty.elem == NONE:
                if node is None or isinstance(node, A.NoneLit):
                    return "sd::contextlib::nullcontext()"
                return f"(static_cast<void>({self.expr(node)}), sd::contextlib::nullcontext())"
            return f"sd::contextlib::nullcontext<{self.cpp_type(e.ty.elem)}>({self.expr_as(node, e.ty.elem)})"
        if name == "closing":
            t = e.ty.elem
            arrow = "->" if isinstance(t, FileType) or (isinstance(t, StructType) and t.kind == "class") else "."
            close = "close" if not isinstance(t, StructType) else fn_name(t.find_method("close"))
            return (f"sd::contextlib::closing<{self.cpp_type(t)}>({self.expr(e.args[0])}, "
                    f"[](auto& sd_x) {{ sd_x{arrow}{close}(); }})")
        if name == "suppress":
            test = " || ".join(f"sd::isinstance<{class_name(c)}>(sd_t)" for c in e.suppressed) or "false"
            return f"sd::contextlib::suppress([](const sd::Thrown& {'sd_t' if e.suppressed else ''}) {{ return {test}; }})"
        if name == "ExitStack":
            return "sd::contextlib::ExitStack()"
        raise NotImplementedError(f"codegen for contextlib.{name}()")

    def try_except(self, s: A.Try) -> None:
        if not s.handlers:
            self.block(s.body)
            return
        ok = self.fresh("ok") if s.orelse else None
        if ok:
            self.line(f"bool {ok} = false;")
        caught = self.fresh("exc")
        self.open("try")
        self.block(s.body)
        if ok:
            self.line(f"{ok} = true;")
        self.depth -= 1
        self.line(f"}} catch (const sd::Thrown& {caught}) {{")
        self.depth += 1
        for i, h in enumerate(s.handlers):
            classes: list[StructType] = h.sym
            test = " || ".join(f"sd::isinstance<{class_name(c)}>({caught})" for c in classes) or "true"
            if i == 0:
                self.open(f"if ({test})")
            else:
                self.depth -= 1
                self.line(f"}} else if ({test}) {{")
                self.depth += 1
            if h.name is not None:
                var: Var = h.name.sym
                self.line(f"{self.var_ref(var)} = std::dynamic_pointer_cast<{class_name(var.type)}>({caught}.exc);")
            self.block(h.body)
        self.depth -= 1
        self.line("} else {")
        self.depth += 1
        self.line("throw;")
        self.close()
        self.close()
        if ok:
            self.open(f"if ({ok})")
            self.block(s.orelse)
            self.close()

    def if_stmt(self, s: A.If) -> None:
        self.open(f"if ({unwrapped(self.cond(s.test))})")
        self.block(s.body)
        orelse = s.orelse
        while len(orelse) == 1 and isinstance(orelse[0], A.If):
            self.depth -= 1
            self.line(f"}} else if ({unwrapped(self.cond(orelse[0].test))}) {{")
            self.depth += 1
            self.block(orelse[0].body)
            orelse = orelse[0].orelse
        if orelse:
            self.depth -= 1
            self.line("} else {")
            self.depth += 1
            self.block(orelse)
        self.close()

    # ---- match statements ------------------------------------------------------

    def match_stmt(self, s: A.Match) -> None:
        """Cases in order, each `if (tests) { bind names; if (guard) { body } }`; a flag
        stops later cases once one has run. The subject is evaluated once."""
        subject = self.fresh("m")
        self.open("")
        self.line(f"auto&& {subject} = {self.expr(s.subject)};")
        done = self.fresh("matched") if len(s.cases) > 1 else None
        if done:
            self.line(f"bool {done} = false;")
        for i, case in enumerate(s.cases):
            path = subject
            if isinstance(s.subject.ty, OptionalType) and not isinstance(case.pattern.ty, OptionalType) and (
                case.pattern.ty != NONE
            ):
                path = f"(*{subject})"  # an earlier `case None:` took the Nones
            alternatives: list[str] = []
            tests, binds = self.pattern_code(case.pattern, path, alternatives)
            for v in alternatives:
                self.line(f"int {v} = 0;")
            if done and i > 0:
                tests = [f"!{done}", *tests]
            self.open(f"if ({unwrapped(tests[0]) if len(tests) == 1 else ' && '.join(tests) or 'true'})")
            for name, code, ty, when in binds:
                if when:
                    self.open(f"if ({when})")
                self.assign(name, code, ty)
                if when:
                    self.close()
            if case.guard is not None:
                self.open(f"if ({unwrapped(self.cond(case.guard))})")
            if done and i < len(s.cases) - 1:
                self.line(f"{done} = true;")
            self.block(case.body)
            if case.guard is not None:
                self.close()
            self.close()
        self.close()
        if s.never_completes:  # (C++ can't tell that a case always runs)
            self.line('sd::raise("SystemError", "no case matched");')

    def pattern_code(self, p: A.Pattern, path: str, alternatives: list[str]) -> tuple[list[str], list[tuple]]:
        """C++ tests for pattern `p` against the value at `path`, and its bindings:
        (Name, C++ value, type, condition or None)."""
        t = p.ty
        match p:
            case A.MatchAs(None, None):
                return [], []
            case A.MatchAs(None, name):
                return [], [(name, path, t, None)]
            case A.MatchAs(inner, name):
                tests, binds = self.pattern_code(inner, path, alternatives)
                return tests, binds + [(name, self.narrowed_path(path, t, p.bound), p.bound, None)]
            case A.MatchOr(options):
                results = [self.pattern_code(o, path, alternatives) for o in options]
                if not any(binds for _, binds in results):
                    return [f"({' || '.join(paren_all(tests) for tests, _ in results)})"], []
                which = self.fresh("alt")
                alternatives.append(which)
                tests = [f"({' || '.join(f'({paren_all(ts)} && (({which} = {i + 1}), true))' for i, (ts, _) in enumerate(results))})"]
                first = {n.id: n for n in p.sym[0]}
                binds = []
                for i, (_, alt_binds) in enumerate(results):
                    for name, code, ty, when in alt_binds:
                        cond = f"{which} == {i + 1}" + (f" && {when}" if when else "")
                        binds.append((first[name.id], code, ty, cond))
                return tests, binds
            case A.MatchValue(value):
                inner = strip_optional(t)
                if isinstance(value, A.NoneLit):
                    if inner == JSON_VALUE and not isinstance(t, OptionalType):
                        return [f"{path}.is_null()"], []
                    return (["true"] if t == NONE else [f"!{path}.has_value()"]), []
                if inner == JSON_VALUE:
                    tests, path = self.optional_test(path, t)
                    return tests + [f"sd::json::equals_literal({path}, {self.expr(value)})"], []
                return [f"({path} == {self.expr(value)})"], []
        tests, path = self.optional_test(path, t)
        t = strip_optional(t)
        match p:
            case A.MatchSequence(items):
                return self.sequence_pattern_code(items, path, t, tests, alternatives)
            case A.MatchMapping(keys, patterns, rest):
                binds: list[tuple] = []
                key_type = t.key if isinstance(t, DictType) else STR
                if t == JSON_VALUE:
                    tests.append(f"{path}.is_dict()")
                key_codes = [self.expr_as(k, key_type) for k in keys]
                for k, sub in zip(key_codes, patterns):
                    tests.append(f"({path}.find({k}) != nullptr)")
                    sub_tests, sub_binds = self.pattern_code(sub, f"(*{path}.find({k}))", alternatives)
                    tests += sub_tests
                    binds += sub_binds
                if rest is not None:
                    source = f"{path}.as_dict()" if t == JSON_VALUE else path
                    kt = self.cpp_type(key_type)
                    binds.append((rest, f"sd::dict_without({source}, std::vector<{kt}>{{{', '.join(key_codes)}}})",
                                  DictType(key_type, t.value if isinstance(t, DictType) else JSON_VALUE), None))
                return tests, binds
            case A.MatchClass() if isinstance(p.sym, tuple):  # int(x), list(items)...
                name, matched = p.sym
                if t == JSON_VALUE:
                    kind = {"int": "int", "float": "float", "str": "str", "bool": "bool", "list": "list", "dict": "dict"}[name]
                    tests.append(f"{path}.is_{kind}()")
                    path = f"{path}.as_{kind}()"
                if p.patterns:
                    sub_tests, binds = self.pattern_code(p.patterns[0], path, alternatives)
                    return tests + sub_tests, binds
                return tests, []
            case A.MatchClass():
                st: StructType = p.sym
                obj = path
                if st.kind == "class" and not (isinstance(t, StructType) and t.is_subclass_of(st)):
                    tests.append(f"sd::isinstance_of<{class_name(st)}>({path})")
                    obj = f"std::static_pointer_cast<{class_name(st)}>({path})"
                arrow = "->" if st.kind == "class" else "."
                binds = []
                for field_name, sub in zip(p.fields, (*p.patterns, *p.kwd_patterns)):
                    sub_tests, sub_binds = self.pattern_code(sub, f"{obj}{arrow}{ident(field_name)}", alternatives)
                    tests += sub_tests
                    binds += sub_binds
                return tests, binds
        raise NotImplementedError(type(p).__name__)

    def optional_test(self, path: str, t: Type) -> tuple[list[str], str]:
        """A pattern that needs a value first checks an optional has one."""
        if isinstance(t, OptionalType):
            return [f"{path}.has_value()"], f"(*{path})"
        return [], path

    def narrowed_path(self, path: str, t: Type, bound: Type) -> str:
        """`case Dog() as d:` binds the subject as a Dog; `case int() as n` on an int? unwraps it."""
        if isinstance(t, OptionalType) and not isinstance(bound, OptionalType):
            path, t = f"(*{path})", t.inner
        if isinstance(bound, StructType) and isinstance(t, StructType) and bound is not t:
            path = f"std::static_pointer_cast<{class_name(bound)}>({path})"
        return path

    def sequence_pattern_code(self, items: list[A.Pattern], path: str, t: Type, tests: list[str],
                              alternatives: list[str]) -> tuple[list[str], list[tuple]]:
        star = next((i for i, item in enumerate(items) if isinstance(item, A.MatchStar)), None)
        n = len(items)
        after = n - star - 1 if star is not None else 0
        binds: list[tuple] = []
        if isinstance(t, TupleType):
            size = len(t.elts)
            for i, item in enumerate(items):
                if i == star:
                    if item.name is not None:
                        middle = ", ".join(f"std::get<{j}>({path})" for j in range(star, size - after))
                        binds.append((item.name, f"{self.cpp_type(item.ty)}{{{middle}}}", item.ty, None))
                    continue
                index = i if star is None or i < star else size - (n - i)
                sub_tests, sub_binds = self.pattern_code(item, f"std::get<{index}>({path})", alternatives)
                tests += sub_tests
                binds += sub_binds
            return tests, binds
        if t == JSON_VALUE:
            tests.append(f"{path}.is_list()")
            seq = f"(*{path}.arr_)"
        elif isinstance(t, VarTupleType):
            seq = f"{path}.items"
        else:
            seq = path
        if star is None:
            tests.append(f"({seq}.size() == {n})")
        elif n > 1:
            tests.append(f"({seq}.size() >= {n - 1})")
        for i, item in enumerate(items):
            if i == star:
                if item.name is not None:
                    code = f"{self.cpp_type(item.ty)}({seq}.begin() + {star}, {seq}.end() - {after})"
                    binds.append((item.name, code, item.ty, None))
                continue
            element = f"{seq}[{i}]" if star is None or i < star else f"{seq}[{seq}.size() - {n - i}]"
            sub_tests, sub_binds = self.pattern_code(item, element, alternatives)
            tests += sub_tests
            binds += sub_binds
        return tests, binds

    def loop(self, header: str, body: list[A.Stmt], orelse: list[A.Stmt], prologue=None) -> None:
        """Python's loop `else:` runs unless the loop was left by `break`, so
        in loops with an else, `break` jumps past the else block."""
        label = [self.fresh("break") if orelse else None, False]
        self.loop_labels.append(label)
        self.open(header)
        if prologue:
            prologue()
        self.block(body)
        self.close()
        self.loop_labels.pop()
        if orelse:
            self.block(orelse)
            if label[1]:
                self.line(f"{label[0]}:;")

    def assign(self, target: A.Expr, code: str, ty: Type) -> None:
        match target:
            case A.Name():
                var: Var = target.sym
                self.line(f"{self.var_ref(var)} = {self.coerce(code, ty, var.type)};")
            case A.Attribute() if isinstance(target.sym, tuple) and target.sym[0] == "cached_set":
                getter: FuncInfo = target.sym[1]  # obj.cached = v replaces the kept value
                prefix = self.self_prefix(target.value.sym.type) if is_self(target.value) else (
                    f"{self.expr(target.value)}{'->' if getter.owner.kind == 'class' else '.'}")
                self.line(f"{prefix}sd_cache_{getter.name} = {self.coerce(code, ty, getter.ret)};")
            case A.Attribute() if isinstance(target.sym, tuple) and target.sym[0] == "property_set":
                setter: FuncInfo = target.sym[1]
                self.line(f"{self.member(target.value, setter)}({self.coerce(code, ty, setter.params[0].type)});")
            case A.Attribute():
                value = self.coerce(code, ty, target.ty)
                if isinstance(target.value.ty, StructType) and target.value.ty.kind == "struct" and holds_references(target.ty):
                    value = f"sd::value_copy({value})"  # a struct is a value all the way down
                self.line(f"{self.attribute(target)} = {value};")
            case A.Index(container, index):
                c = self.expr(container)
                if target.dunder is not None:  # obj[k] = v -> obj.__setitem__(k, v)
                    m = target.dunder.method
                    args = [self.expr_as(index, m.params[0].type), self.coerce(code, ty, m.params[1].type)]
                    self.line(f"{self.dunder_call(target.dunder, c, container.ty, args)};")
                    return
                if isinstance(container.ty, DictType):
                    key = self.expr_as(index, container.ty.key)
                    self.line(f"{c}[{key}] = {self.coerce(code, ty, target.ty)};")
                else:
                    self.line(f"sd::index({c}, {self.expr(index)}) = {self.coerce(code, ty, target.ty)};")
            case A.TupleLit(elts) | A.ListLit(elts) if isinstance(ty, VarTupleType):
                tmp = self.fresh("t")
                self.open("")
                self.line(f"auto {tmp} = {code};")
                self.line(f"sd::check_unpack({tmp}.size(), {len(elts)});")
                for i, elt in enumerate(elts):
                    self.assign(elt, f"{tmp}.items[{i}]", ty.elem)
                self.close()
            case A.TupleLit(elts) | A.ListLit(elts):
                tmp = self.fresh("t")
                self.open("")
                self.line(f"auto {tmp} = {code};")
                for i, elt in enumerate(elts):
                    self.assign(elt, f"std::get<{i}>({tmp})", ty.elts[i])
                self.close()

    def aug_assign(self, s: A.AugAssign, target: A.Expr, op: str, value: A.Expr) -> None:
        read_var, read_type, result = s.sym
        if isinstance(target, A.Index) and target.dunder is not None:
            # obj[k] += v -> obj.__setitem__(k, obj.__getitem__(k) + v), evaluating obj and k once
            owner = target.value.ty
            getter = target.dunder
            setter = Dunder(owner.find_method("__setitem__"))
            obj, key = self.fresh("obj"), self.fresh("key")
            self.open("")
            self.line(f"auto&& {obj} = {self.expr(target.value)};")
            self.line(f"auto {key} = {self.expr_as(target.index, getter.method.params[0].type)};")
            current = self.dunder_call(getter, obj, owner, [key])
            new = self.binop_code(op, current, read_type, self.expr(value), value.ty, result, s.dunder)
            self.line(f"{self.dunder_call(setter, obj, owner, [key, self.coerce(new, result, setter.method.params[1].type)])};")
            self.close()
            return
        if isinstance(target, A.Name):
            write: Var = target.sym
            if op == "+" and isinstance(read_type, ListType) and write is read_var:
                self.line(f"sd::list_extend({self.var_ref(write)}, {self.expr(value)});")
                return
            if op == "|" and type(read_type) is DictType and write is read_var:
                self.line(f"sd::dict_update({self.var_code(read_var, read_type)}, {self.expr(value)});")  # d |= other, in place
                return
            if isinstance(read_type, (ListType, SetType)) and write is read_var and result == read_type and s.dunder is None:
                # xs *= 2, s |= t: in place, so other references to the list or set see it
                current = self.var_code(read_var, read_type)
                new = self.binop_code(op, current, read_type, self.expr(value), value.ty, result, s.dunder)
                self.line(f"sd::assign_contents({current}, {new});")
                return
            current = self.var_code(read_var, read_type)
            if write is read_var and read_type == result == write.type == value.ty and (
                (op in ("+", "-", "*") and is_numeric(result)) or (op == "+" and result == STR)
            ):
                self.line(f"{self.var_ref(write)} {op}= {self.expr(value)};")  # the readable form
                return
            new = self.binop_code(op, current, read_type, self.expr(value), value.ty, result, s.dunder)
            self.line(f"{self.var_ref(write)} = {self.coerce(new, result, write.type)};")
            return
        if isinstance(target, A.Attribute) and isinstance(target.sym, tuple) and target.sym[0] == "property":
            # obj.count += 1 -> obj.sd_set_count(obj.sd_get_count() + 1), evaluating obj once
            getter: FuncInfo = target.sym[1]
            setter = target.value.ty.find_method(f"{target.attr}.setter")
            obj = self.fresh("obj")
            self.open("")
            self.line(f"auto&& {obj} = {self.expr(target.value)};")
            arrow = "->" if getter.owner.kind == "class" else "."
            new = self.binop_code(op, f"{obj}{arrow}{fn_name(getter)}()", read_type, self.expr(value), value.ty, result, s.dunder)
            self.line(f"{obj}{arrow}{fn_name(setter)}({self.coerce(new, result, setter.params[0].type)});")
            self.close()
            return
        ref = self.fresh("ref")
        if isinstance(target, A.Index) and isinstance(target.value.ty, (CounterType, DefaultDictType)):
            # c[k] += 1 starts from 0 (Counter) or factory() (defaultdict) for a new key
            lvalue = f"{self.expr(target.value)}[{self.expr_as(target.index, target.value.ty.key)}]"
        else:
            lvalue = self.attribute(target) if isinstance(target, A.Attribute) else self.index(target)
        self.open("")
        self.line(f"auto& {ref} = {lvalue};")
        new = self.binop_code(op, ref, read_type, self.expr(value), value.ty, result, s.dunder)
        if isinstance(read_type, ListType) and op == "+" and s.dunder is None:
            self.line(f"sd::list_extend({ref}, {self.expr(value)});")  # obj.xs += ys extends it in place
        elif op == "|" and type(read_type) is DictType:
            self.line(f"sd::dict_update({ref}, {self.expr(value)});")
        elif isinstance(read_type, (ListType, SetType)) and result == read_type and s.dunder is None:
            self.line(f"sd::assign_contents({ref}, {new});")
        else:
            self.line(f"{ref} = {self.coerce(new, result, read_type)};")
        self.close()

    # =========================================================================
    # Expressions
    # =========================================================================

    def cond(self, e: A.Expr) -> str:
        """`e` as a C++ bool, applying Python truthiness."""
        code = self.expr(e)
        return code if e.ty == BOOL else f"sd::truthy({code})"

    def in_order(self, operands: list[A.Expr], build, keep_refs: bool = False) -> str:
        """Evaluate operands left to right, as Python does.

        C++ leaves the order of function arguments and of most binary
        operands unspecified, so `print(f(), g())` might call g first. When at
        least two operands could have side effects, evaluate them into
        temporaries in source order inside a lambda, then build the expression
        from those. `build` is called with no arguments and uses self.expr as
        usual; the temporaries are substituted transparently.
        """
        # One operand with side effects is enough if another operand could observe
        # them: `print(f(), log[-1])` must call f() before reading log.
        if not any(has_call(x) for x in operands) or sum(not is_literal(x) for x in operands) < 2:
            return build()
        decls = []
        for x in operands:
            tmp = self.fresh("a")
            # A value, not auto&&: the expression may return a reference into a temporary.
            decls.append(f"[[maybe_unused]] auto {tmp} = {self.expr(x)};")  # (str.format may not use it)
            self.precomputed[id(x)] = tmp
        try:
            inner = build()
        finally:
            for x in operands:
                del self.precomputed[id(x)]
        # keep_refs: a call like d.setdefault(k, []) returns a reference into its receiver
        # (never into the temporaries here), and `.append()` on it must reach the dict.
        ret = " -> decltype(auto)" if keep_refs else ""
        return f"[&](){ret} {{ {' '.join(decls)} return {inner}; }}()"

    def expr(self, e: A.Expr) -> str:
        if id(e) in self.precomputed:
            return self.precomputed[id(e)]  # already converted, if it's a tuple loop
        if (elem := getattr(e, "tuple_elem", None)) is not None:
            if isinstance(e, A.TupleLit):  # `for x in (a, b, c)`: build the list directly
                return f"{self.cpp_type(ListType(elem))}{{{', '.join(self.expr_as(x, elem) for x in e.elts)}}}"
            return self.tuple_as_list(self.expr_code(e), e.ty, elem)
        if getattr(e, "copy_out", False):  # a @value class's list used as a value: a copy of it
            return f"sd::value_copy({self.expr_code(e)})"
        return self.expr_code(e)

    def tuple_as_list(self, code: str, t: TupleType, elem: Type) -> str:
        """`for x in (a, b)`: the tuple's items as a list of their common type."""
        items = ", ".join(self.coerce(f"std::get<{i}>(sd_tup)", et, elem) for i, et in enumerate(t.elts))
        return f"[&](auto&& sd_tup) {{ return {self.cpp_type(ListType(elem))}{{{items}}}; }}({code})"

    def expr_code(self, e: A.Expr) -> str:
        match e:
            case A.IntLit(v):
                return f"{v}_i"
            case A.FloatLit(v):
                return float_literal(v)
            case A.StrLit(v):
                return cpp_string(v)
            case A.BytesLit(v):
                return f"sd::bytes({cpp_bytes(v)})"
            case A.BoolLit(v):
                return "true" if v else "false"
            case A.NoneLit():
                return "std::nullopt"
            case A.FormatArg(index):
                return self.format_args[index]
            case A.FString(parts):
                values = fstring_values(parts)
                return self.in_order(values, lambda: self.fstring(parts))
            case A.Name():
                return self.name(e)
            case A.ListLit(elts) | A.SetLit(elts):
                return f"{self.cpp_type(e.ty)}{{{', '.join(self.expr_as(x, e.ty.elem) for x in elts)}}}"
            case A.DictLit(keys, values):
                if isinstance(e.ty, SetType):
                    return f"{self.cpp_type(e.ty)}{{}}"  # `s: set[int] = {}`
                pairs = ", ".join(
                    f"{{{self.expr_as(k, e.ty.key)}, {self.expr_as(v, e.ty.value)}}}" for k, v in zip(keys, values)
                )
                return f"{self.cpp_type(e.ty)}{{{pairs}}}"
            case A.TupleLit(elts) if isinstance(e.ty, VarTupleType):  # the arguments packed into *args
                return f"{self.cpp_type(e.ty)}{{{', '.join(self.expr_as(x, e.ty.elem) for x in elts)}}}"
            case A.TupleLit(elts):
                args = ", ".join(self.expr_as(x, t) for x, t in zip(elts, e.ty.elts))
                return f"{self.cpp_type(e.ty)}{{{args}}}"  # braces: evaluated left to right
            case A.GeneratorExp():
                return self.generator_expression(e)
            case A.ListComp() | A.SetComp() | A.DictComp() | A.GeneratorExp():
                return self.comprehension(e)
            case A.UnaryOp("not", operand):
                return f"(!{self.cond(operand)})"
            case A.UnaryOp(op, operand) if e.dunder is not None:  # -vec -> vec.__neg__()
                return self.dunder_call(e.dunder, self.expr(operand), operand.ty, [])
            case A.UnaryOp(op, operand):
                return f"({op}{self.expr(operand)})"
            case A.BinOp(op, left, right):
                return self.in_order(
                    [left, right],
                    lambda: self.binop_code(op, self.expr(left), left.ty, self.expr(right), right.ty, e.ty, e.dunder),
                )
            case A.BoolOp():
                return self.boolop(e)
            case A.Compare():
                return self.compare(e)
            case A.IfExp(test, body, orelse):
                return f"({self.cond(test)} ? {self.expr_as(body, e.ty)} : {self.expr_as(orelse, e.ty)})"
            case A.Lambda():
                return self.lambda_code(e)
            case A.NamedExpr(target, value):
                var: Var = target.sym
                code = f"({self.var_ref(var)} = {self.expr_as(value, var.type)})"
                return self.view(code, var.type, e.ty)
            case A.Call():
                return self.call(e)
            case A.Attribute():
                return self.attribute(e)
            case A.Index():
                return self.index(e)
        raise NotImplementedError(f"codegen for {type(e).__name__}")

    def view(self, code: str, declared: Type, seen: Type) -> str:
        """A variable as the checker currently sees it: unwrap a narrowed optional, and
        cast to a subclass after isinstance()."""
        if isinstance(declared, OptionalType) and not isinstance(seen, OptionalType) and seen != NONE:
            code, declared = f"(*{code})", declared.inner
        if isinstance(seen, StructType) and isinstance(declared, StructType) and seen is not declared:
            return f"std::static_pointer_cast<{class_name(seen)}>({code})"
        return code

    def var_ref(self, var: Var) -> str:
        """The C++ lvalue for a variable. Variables shared with closures live in a cell."""
        name = ident(var.cpp_name)
        if var.kind == "global":
            name = qualified(name, var.module)
        return f"(*{name})" if var.captured else name

    def var_code(self, var: Var, seen: Type) -> str:
        if var.name == "self" and var.kind == "param":
            if self.lambda_self:
                return "sd_self"
            if var.type.kind == "struct":
                return "(*this)"
            if var.type.base is not None:  # shared_from_this() gives the root class's pointer
                return f"std::static_pointer_cast<{class_name(var.type)}>(this->shared_from_this())"
            return "this->shared_from_this()"
        if isinstance(var.type, ClassRefType) and var.type.st.enum is not None:
            return f"{class_name(var.type.st)}::sd_members()"  # `for m in cls` in an enum's classmethod
        return self.view(self.var_ref(var), var.type, seen)

    def name(self, e: A.Name) -> str:
        sym = e.sym
        if (fn_def := getattr(e, "snapshot_of", None)) is not None and getattr(fn_def, "snapshot", None) is not None:
            return self.snapshot_call(fn_def)  # a thread's target: built from copies of what it captures
        if isinstance(sym, Var):
            return self.var_code(sym, e.ty)
        if isinstance(sym, FuncInfo):
            return qualified(fn_name(sym), sym.module)  # a function used as a value
        if isinstance(sym, A.Lambda):
            return self.expr(sym)  # `key=len` was wrapped as `lambda p: len(p)`
        if isinstance(sym, builtins.Module):  # print(math)
            return f"sd::ModuleRef{{{cpp_string(builtins.module_repr(sym))}}}"
        if isinstance(sym, StructType) and sym.enum is not None:  # for c in Color: its members
            return f"{class_name(sym)}::sd_members()"
        if isinstance(sym, builtins.Value):
            if sym.name == "__name__":
                return cpp_string(self.module_name)
            return self.module_value(sym)
        raise NotImplementedError(f"codegen for name {e.id}")

    def module_value(self, v: builtins.Value) -> str:
        if v.cpp is not None:
            return v.cpp
        mod, name = self.module_values[id(v)]
        if mod == "math":
            return MATH_VALUES[name]
        if mod == "sys" and name == "argv":
            return "sd::argv()"
        raise NotImplementedError(f"codegen for {mod}.{name}")

    def self_prefix(self, st: StructType) -> str:
        """How to reach a member of `self`: this->x, or sd_self.x inside a lambda."""
        if self.lambda_self:
            return "sd_self." if st.kind == "struct" else "sd_self->"
        return "this->"

    def lambda_code(self, e: A.Lambda) -> str:
        """Captures by value ([=]), so the lambda is safe to return or store. `self` is
        captured as a copy (struct) or shared pointer (class) instead of the raw `this`."""
        t: FuncType = e.ty
        used = {id(n.sym) for n in walk_expr(e.body) if isinstance(n, A.Name)}
        params = ", ".join(f"{'' if id(p.sym) in used else '[[maybe_unused]] '}{self.cpp_type(pt)} {ident(p.sym.cpp_name)}"
                           for p, pt in zip(e.params, t.params))
        captures = ["="]
        uses_self = any(isinstance(n, A.Name) and is_self(n) for n in walk_expr(e.body))
        if uses_self:
            self_var = next(n.sym for n in walk_expr(e.body) if isinstance(n, A.Name) and is_self(n))
            captures.append(f"sd_self = {self.var_code(self_var, self_var.type)}")
            self.lambda_self += 1
        for v in getattr(e, "snapshot", None) or []:  # run on another thread: its own copies of what it uses
            captures.append(f"{ident(v.cpp_name)} = std::make_shared<{self.cpp_type(v.type)}>(sd::value_copy({self.var_ref(v)}))")
        capture = f"[{', '.join(captures)}]"
        try:
            body = self.expr_as(e.body, t.ret)
        finally:
            if uses_self:
                self.lambda_self -= 1
        if t.ret == NONE:  # `lambda: print(x)` or `lambda s: None`: evaluate for effect only
            statement = "" if isinstance(e.body, A.NoneLit) else f" {body};"
            return f"{capture}({params}) mutable -> void {{{statement} }}"
        return f"{capture}({params}) mutable -> {self.cpp_type(t.ret)} {{ return {body}; }}"

    def member(self, obj: A.Expr, m: FuncInfo) -> str:
        """`obj.m` / `this->m`, ready to be called."""
        if is_self(obj):
            return f"{self.self_prefix(obj.sym.type)}{fn_name(m)}"
        arrow = "->" if m.owner.kind == "class" else "."
        return f"{self.expr(obj)}{arrow}{fn_name(m)}"

    def partial_code(self, e: A.Call) -> str:
        """functools.partial: a lambda holding the function and the bound arguments (evaluated
        now; copied over if it's for another thread), filling in the rest, and defaults."""
        info: builtins.PartialInfo = e.partial
        captures, args = [], []
        params = [f"{self.cpp_type(info.params[i].type)} sd_p{i}" for i in info.taken]
        for i, (p, arg) in enumerate(zip(info.params, info.bound)):
            if arg is not None:
                code = self.expr_as(arg, p.type)
                captures.append(f"sd_b{i} = {'sd::send(' + code + ')' if info.sent else code}")
                args.append(f"sd_b{i}")
            elif i in info.taken:
                args.append(f"sd_p{i}")
            else:
                args.append(self.expr_as(p.default, p.type))
        if isinstance(info.target, StructType):
            st = info.target
            if st.init is not None or (st.kind == "class" and not st.is_exception):
                args = ["sd::init", *args]
            call = (f"std::make_shared<{class_name(st)}>({', '.join(args)})" if st.kind == "class"
                    else f"{class_name(st)}({', '.join(args)})")
        else:
            captures.insert(0, f"sd_f = {self.expr(e.args[0])}")
            call = f"sd_f({', '.join(args)})"
        ret = self.cpp_type(e.ty.ret)
        body = f"{call};" if e.ty.ret == NONE else f"return {call};"
        return f"[{', '.join(captures)}]({', '.join(params)}) mutable -> {ret} {{ {body} }}"

    def getter(self, obj: A.Expr, getter: FuncInfo) -> str:
        """`obj.area` -> obj.sd_get_area, ready to be called (a cached_property's keeps its value)."""
        if not getter.lazy:
            return self.member(obj, getter)
        if is_self(obj):
            return f"{self.self_prefix(obj.sym.type)}sd_get_{getter.name}"
        return f"{self.expr(obj)}{'->' if getter.owner.kind == 'class' else '.'}sd_get_{getter.name}"

    def lazy_members(self, st: StructType) -> None:
        """@cached_property: the kept value, and a getter that computes it the first time."""
        for m in st.methods.values():
            if not m.lazy:
                continue
            t = self.cpp_type(m.ret)
            lock = "std::lock_guard<std::recursive_mutex> sd_lock(this->sd_mutex); " if is_synchronized(st) else ""
            self.line(f"std::optional<{t}> sd_cache_{m.name};")
            self.line(f"{t} sd_get_{m.name}() {{ {lock}if (!sd_cache_{m.name}) sd_cache_{m.name} = {fn_name(m)}(); "
                      f"return *sd_cache_{m.name}; }}")

    def bound_method(self, obj: A.Expr, m: FuncInfo) -> str:
        """`counter.tick` as a value: a lambda holding (a copy of / reference to) the object."""
        names = [self.fresh("p") for _ in m.params]
        params = ", ".join(f"{self.cpp_type(p.type)} {n}" for p, n in zip(m.params, names))
        arrow = "->" if m.owner.kind == "class" else "."
        call = f"sd_o{arrow}{fn_name(m)}({', '.join(names)})"
        return f"[sd_o = {self.expr(obj)}]({params}) mutable -> {self.cpp_type(m.ret)} {{ return {call}; }}"

    def fstring(self, parts: list) -> str:
        """f"w{n}!" -> sd::fstr("w"sv, n, "!"sv): pieces appended into one string."""
        pieces = []
        for p in parts:
            if isinstance(p, str):
                pieces.append(cpp_string(p)[:-1] + "sv")  # a string_view literal (keeps NULs)
            else:
                value = self.expr(p.value)
                if p.conversion == "r":
                    value = f"sd::repr({value})"
                elif p.conversion == "a":
                    value = f"sd::ascii(sd::repr({value}))"
                elif p.conversion == "s":
                    value = f"sd::str({value})"
                if isinstance(p.spec, A.FString):  # {x:{width}}: the spec is built at run time
                    value = f"sd::format_any({value}, {self.fstring(p.spec.parts)})"
                elif p.spec:
                    value = f"sd::format_any({value}, {cpp_string(p.spec)[:-1]}sv)"
                pieces.append(value)
        return f"sd::fstr({', '.join(pieces)})"

    def attribute(self, e: A.Attribute) -> str:
        if isinstance(e.sym, builtins.Value):
            return self.module_value(e.sym)
        if isinstance(e.sym, builtins.Module):  # print(os.path)
            return f"sd::ModuleRef{{{cpp_string(builtins.module_repr(e.sym))}}}"
        if isinstance(e.sym, A.Lambda):
            return self.expr(e.sym)  # `key=str.lower`
        if isinstance(e.sym, tuple) and e.sym[0] == "thread_attr":
            return f"{self.expr(e.value)}.{e.sym[1]}()"
        if isinstance(e.sym, tuple) and e.sym[0] == "server_method":  # target=server.serve_forever
            return f"[sd_o = {self.expr(e.value)}]() mutable {{ sd_o.{e.sym[1]}(); }}"
        if isinstance(e.sym, tuple) and e.sym[0] == "class_attr":  # self.version
            getter = f"sd_attr_{e.sym[1].name}()"
            if is_self(e.value):
                return f"{self.self_prefix(e.value.sym.type)}{getter}"
            return f"{self.expr(e.value)}{'->' if e.value.ty.kind == 'class' else '.'}{getter}"
        if isinstance(e.sym, tuple) and e.sym[0] == "enum_member":  # Color.RED
            st, member = e.sym[1], e.sym[2]
            return f"{class_name(st)}::sd_at({[m.name for m in st.enum.distinct].index(member.alias_of or member.name)})"
        if isinstance(e.sym, StructType) and e.sym.enum is not None:  # for c in colors.Color: its members
            return f"{class_name(e.sym)}::sd_members()"
        if isinstance(e.sym, tuple) and e.sym[0] == "enum_attr":  # c.name, c.value
            code = f"{self.expr(e.value)}.sd_{e.sym[1]}()"
            if e.sym[1] == "name" and e.value.ty.enum.flag and not isinstance(e.ty, OptionalType):
                return f"sd::unwrap({code}, {cpp_string('.'.join(attr_chain(e)))[:-1]})"  # narrowed: checked
            return code
        if isinstance(e.sym, tuple) and e.sym[0] == "class_attr_of":  # Handler.version
            return f"{class_name(e.sym[1])}::sd_class_{e.sym[2].name}()"
        if isinstance(e.sym, tuple) and e.sym[0] == "builtin_attr":
            obj, vt = self.expr(e.value), e.value.ty
            declared = builtins.type_attributes(vt)[e.sym[1]](vt)
            if isinstance(declared, OptionalType) and e.ty is not None and not isinstance(e.ty, OptionalType):
                inner = A.Attribute(e.value, e.sym[1], loc=e.loc)  # narrowed (`if db.parent:`): checked unwrap
                inner.sym, inner.ty = e.sym, declared
                return f"sd::unwrap({self.attribute(inner)}, {cpp_string('.'.join(attr_chain(e)))[:-1]})"
            if isinstance(vt, ProcessType):
                return self.process_attribute(obj, vt, e.sym[1], e.ty)
            if vt == STR_TEMPLATE:  # t.template
                return f"{obj}.get_template()"
            if vt == UUID_T and e.sym[1] == "bytes":
                return f"{obj}.get_bytes()"
            if isinstance(vt, FileType):  # f.closed, f.name, f.mode
                return f"{obj}->{'is_closed' if e.sym[1] == 'closed' else 'get_' + e.sym[1]}()"
            if isinstance(vt, NamespaceType):  # args.count
                return f"{obj}.get<{self.cpp_type(e.ty)}>({cpp_string(e.sym[1])})"
            return f"{obj}.{e.sym[1]}()"  # m.string(), pattern.groups()
        if isinstance(e.sym, tuple) and e.sym[0] == "property":  # obj.area -> obj.sd_get_area()
            return f"{self.getter(e.value, e.sym[1])}()"
        if isinstance(e.sym, FuncInfo):
            if e.sym.owner is None:  # textutil.shout: a module's function, as a value
                return qualified(fn_name(e.sym), e.sym.module)
            return self.bound_method(e.value, e.sym)
        if isinstance(e.sym, Var):  # geo.ORIGIN: another module's global
            return self.var_ref(e.sym)
        obj = e.value
        field = ident(e.attr)
        if is_self(obj):
            code = f"{self.self_prefix(obj.sym.type)}{field}"
        else:
            arrow = "->" if isinstance(obj.ty, StructType) and obj.ty.kind == "class" else "."
            code = f"{self.expr(obj)}{arrow}{field}"
        declared = e.sym.type if hasattr(e.sym, "type") else e.ty
        path = cpp_string(".".join(attr_chain(e)))[:-1]
        if isinstance(declared, OptionalType) and e.ty is not None and not isinstance(e.ty, OptionalType) and e.ty != NONE:
            code, declared = f"sd::unwrap({code}, {path})", declared.inner  # narrowed, but checked
        if isinstance(e.ty, StructType) and isinstance(declared, StructType) and e.ty is not declared:
            code = f"sd::downcast<{class_name(e.ty)}>({code}, {path})"  # after isinstance(); checked too
        return code

    def index(self, e: A.Index) -> str:
        if isinstance(e.sym, tuple) and e.sym[0] == "enum_name":  # Color["RED"]
            return f"{class_name(e.sym[1])}::sd_by_name({self.expr(e.index)})"
        v = self.expr(e.value)
        if e.dunder is not None:  # obj[k] -> obj.__getitem__(k)
            return self.dunder_call(e.dunder, v, e.value.ty, [self.expr_as(e.index, e.dunder.method.params[0].type)])
        idx = e.index
        if isinstance(idx, A.Slice):
            parts = [self.expr(p) if p is not None else "std::nullopt" for p in (idx.lower, idx.upper, idx.step)]
            return f"sd::slice({v}, {', '.join(parts)})"
        vt = e.value.ty
        if isinstance(vt, TupleType):
            i = constant_int(idx)
            if i is not None:
                return f"std::get<{i % len(vt.elts)}>({v})"
            return f"sd::tuple_index({v}, {self.expr(idx)})"
        if isinstance(vt, DictType):
            return f"sd::index({v}, {self.expr_as(idx, vt.key)})"
        if isinstance(vt, MatchType):  # m[1] is m.group(1)
            return self.match_group(v, idx, e.ty)
        return f"sd::index({v}, {self.expr(idx)})"

    def dunder_call(self, d, recv: str, recv_type: Type, args: list[str]) -> str:
        """A call to a user type's dunder method; `not` it for != / not in derived from == / in."""
        arrow = "->" if isinstance(recv_type, StructType) and recv_type.kind == "class" else "."
        call = f"{recv}{arrow}{ident(d.method.name)}({', '.join(args)})"
        return f"(!{call})" if d.negate else call

    def binop_code(self, op: str, lc: str, lt: Type, rc: str, rt: Type, t: Type, dunder=None) -> str:
        if enum_flag_op(op, lt, rt) is not None:
            return f"({lc} {op} {rc})"  # the flag's own operators
        if enum_mixin(lt) is not None:  # N.ONE + 1: the member's value
            lc, lt = f"{lc}.sd_value()", enum_mixin(lt)
        if enum_mixin(rt) is not None:
            rc, rt = f"{rc}.sd_value()", enum_mixin(rt)
        if lt in DATETIME_TYPES or rt in DATETIME_TYPES:  # C++ operators on the datetime values
            if op in ("//", "%"):
                return f"sd::datetime::{'floordiv' if op == '//' else 'mod'}({lc}, {rc})"
            return f"({lc} {op} {rc})"
        if builtins.NORMAL_DIST in (lt, rt):  # C++ operators on statistics.NormalDist
            lc, rc = (self.coerce(code, ct, FLOAT) if ct == INT else code for code, ct in ((lc, lt), (rc, rt)))
            return f"({lc} {op} {rc})"
        if dunder is not None:  # a + b -> a.__add__(b), or 2 * v -> v.__rmul__(2)
            param = dunder.method.params[0].type
            if dunder.reflected:
                return self.dunder_call(dunder, rc, rt, [self.coerce(lc, lt, param)])
            return self.dunder_call(dunder, lc, lt, [self.coerce(rc, rt, param)])
        if is_numeric(lt) and is_numeric(rt):
            if op == "/":
                return f"sd::truediv({self.coerce(lc, lt, FLOAT)}, {self.coerce(rc, rt, FLOAT)})"
            lc, rc = self.coerce(lc, lt, t), self.coerce(rc, rt, t)
            match op:
                case "//":
                    return f"sd::floordiv({lc}, {rc})"
                case "%":
                    return f"sd::mod({lc}, {rc})"
                case "**":
                    return f"sd::pow({lc}, {rc})"
            return f"({lc} {op} {rc})"
        match op:
            case "+" if isinstance(lt, ListType):
                return f"sd::concat({lc}, {rc})"
            case "+" if isinstance(lt, TupleType):
                return f"std::tuple_cat({lc}, {rc})"
            case "*" if lt == INT:
                return f"sd::repeat({rc}, {lc})"
            case "*":
                return f"sd::repeat({lc}, {rc})"
            case "-" | "&" | "|" | "^" if isinstance(lt, SetType):
                fn = {"-": "sub", "&": "and", "|": "or", "^": "xor"}[op]
                return f"sd::set_{fn}({lc}, {rc})"
            case "|" if type(lt) is DictType:
                return f"sd::dict_or({lc}, {rc})"
        return f"({lc} {op} {rc})"

    def boolop(self, e: A.BoolOp) -> str:
        if e.ty == BOOL:
            joiner = " && " if e.op == "and" else " || "
            return f"({self.cond(e.left)}{joiner}{self.cond(e.right)})"
        # As values, `and`/`or` return one of their operands, like Python.
        tmp = self.fresh("l")
        t = e.ty
        left = self.coerce(tmp, e.left.ty, t)
        if e.op == "or" and isinstance(e.left.ty, OptionalType) and not isinstance(t, OptionalType):
            left = self.coerce(f"(*{tmp})", e.left.ty.inner, t)
        right = self.expr_as(e.right, t)
        picked = f"{left} : {right}" if e.op == "or" else f"{right} : {left}"
        return f"[&]() -> {self.cpp_type(t)} {{ auto {tmp} = {self.expr(e.left)}; return sd::truthy({tmp}) ? {picked}; }}()"

    def compare(self, e: A.Compare) -> str:
        operands = [e.left, *e.comparators]
        dunders = e.dunder or [None] * len(e.ops)
        if len(e.ops) == 1:
            return self.in_order(operands, lambda: self.comparison(e.ops[0], self.expr(e.left), e.left, self.expr(e.comparators[0]), e.comparators[0], dunders[0]))
        if all(is_simple(x) for x in operands[1:-1]):
            parts = [
                self.comparison(op, self.expr(l), l, self.expr(r), r, d)
                for op, l, r, d in zip(e.ops, operands, operands[1:], dunders)
            ]
            return parts[0] if len(parts) == 1 else "(" + " && ".join(parts) + ")"
        # a < f() < c: evaluate each middle operand once, and stop early like Python.
        temps = [self.fresh("c") for _ in operands]
        body = [f"auto {temps[0]} = {self.expr(operands[0])};"]
        for i, op in enumerate(e.ops):
            body.append(f"auto {temps[i + 1]} = {self.expr(operands[i + 1])};")
            test = self.comparison(op, temps[i], operands[i], temps[i + 1], operands[i + 1], dunders[i])
            body.append(f"if (!{test}) return false;")
        return "[&]() -> bool { " + " ".join(body) + " return true; }()"

    def comparison(self, op: str, lc: str, left: A.Expr, rc: str, right: A.Expr, dunder=None) -> str:
        if dunder is not None:  # a < b -> a.__lt__(b); x in c -> c.__contains__(x)
            param = dunder.method.params[0].type
            if dunder.reflected:
                return self.dunder_call(dunder, rc, right.ty, [self.coerce(lc, left.ty, param)])
            return self.dunder_call(dunder, lc, left.ty, [self.coerce(rc, right.ty, param)])
        lt, rt = left.ty, right.ty
        if enum_decays(op, lt, rt):  # N.ONE < 2: compared as its value
            lc = f"{lc}.sd_value()"
        if enum_decays(op, rt, lt, right=True):
            rc = f"{rc}.sd_value()"
        enum_t = strip_optional(lt) if isinstance(strip_optional(lt), StructType) else strip_optional(rt)
        if isinstance(enum_t, StructType) and enum_t.enum is not None and op in ("is", "is not") and not (
                isinstance(right, A.NoneLit) or isinstance(left, A.NoneLit)):
            return f"({lc} {'==' if op == 'is' else '!='} {rc})"  # members are values: the same member is equal
        if op in ("in", "not in") and isinstance(rt, StructType) and rt.enum is not None and rt.enum.flag:
            return f"{'' if op == 'in' else '!'}sd::enums::flag_contains({rc}, {lc})"  # P.R in perms
        if isinstance(right, A.NoneLit) or isinstance(left, A.NoneLit):
            subject = lc if isinstance(right, A.NoneLit) else rc
            is_none = op in ("is", "==")
            return f"(!{subject}.has_value())" if is_none else f"{subject}.has_value()"
        match op:
            case "in":
                return f"sd::contains({rc}, {lc})"
            case "not in":
                return f"(!sd::contains({rc}, {lc}))"
            case "is" | "is not" if isinstance(lt, OptionalType) or isinstance(rt, OptionalType):  # node.parent is root
                return f"(sd::object_identity({lc}) {'==' if op == 'is' else '!='} sd::object_identity({rc}))"
            case "is" | "is not" if not isinstance(left.ty, StructType):  # the same list, dict or set
                return f"({lc}.identity() {'==' if op == 'is' else '!='} {rc}.identity())"
            case "is":
                return f"({lc}.get() == {rc}.get())"  # identity, whatever __eq__ says
            case "is not":
                return f"({lc}.get() != {rc}.get())"
            case "<=" | "<" | ">=" | ">" if isinstance(left.ty, SetType):  # subset and superset
                a, b = (lc, rc) if op[0] == "<" else (rc, lc)
                return f"sd::set_{'issubset' if '=' in op else 'proper_subset'}({a}, {b})"
        return f"({lc} {op} {rc})"

    def comprehension(self, e: A.Expr) -> str:
        result = self.fresh("r")
        match e:
            case A.DictComp(key, value, gens):
                add = f"{result}[{self.expr_as(key, e.ty.key)}] = {self.expr_as(value, e.ty.value)};"
                cpp = self.cpp_type(e.ty)
            case A.SetComp(elt, gens):
                add = f"{result}.insert({self.expr_as(elt, e.ty.elem)});"
                cpp = self.cpp_type(e.ty)
            case A.ListComp(elt, gens) | A.GeneratorExp(elt, gens):
                add = f"{result}.push_back({self.expr_as(elt, e.ty.elem)});"
                cpp = self.cpp_type(ListType(e.ty.elem))
        code = [f"[&]() {{ {cpp} {result};"]
        for gen in gens:
            v = self.fresh("v")
            code.append(f"for (auto&& {v} : sd::iter({self.expr(gen.iter)})) {{")
            code.extend(self.bind_comprehension(gen.target, v))
            code.extend(f"if (!{self.cond(c)}) continue;" for c in gen.ifs)
        code.append(add)
        code.append("}" * len(gens))
        code.append(f"return {result}; }}()")
        return " ".join(code)

    def generator_expression(self, e: A.GeneratorExp) -> str:
        """(f(x) for x in xs if p(x)): a coroutine made on the spot. Like Python, the first
        iterable is evaluated now; the variables it uses from around it are passed in
        (a coroutine can outlive the expression, so it can't capture by reference)."""
        elem = e.ty.elem
        bound = set()  # variables the expression binds itself (its loops', nested ones', lambdas')
        for node in walk_expr(e):
            if isinstance(node, A.Comprehension):
                bound |= {id(n.sym) for n in walk_expr(node.target) if isinstance(n, A.Name)}
            elif isinstance(node, A.Lambda):
                bound |= {id(p.sym) for p in node.params}
        outer: dict[str, str] = {}  # C++ parameter -> the argument for it
        uses_self = False
        for gen_index, gen in enumerate(e.generators):
            parts = [gen.target, *gen.ifs] + ([] if gen_index == 0 else [gen.iter])
            for part in [*parts, e.elt]:
                for n in walk_expr(part):
                    if not isinstance(n, A.Name) or not isinstance(n.sym, Var) or id(n.sym) in bound:
                        continue
                    var = n.sym
                    if is_self(n):
                        uses_self = True
                    elif var.kind != "global":
                        outer[ident(var.cpp_name)] = ident(var.cpp_name)  # a cell stays shared
        params = ["auto sd_first", *(f"auto {p}" for p in outer)]
        args = [self.expr(e.generators[0].iter), *outer.values()]
        if uses_self:
            self_var = next(n.sym for n in walk_expr(e) if isinstance(n, A.Name) and is_self(n))
            params.append("auto sd_self")
            args.append(self.var_code(self_var, self_var.type))
            self.lambda_self += 1
        try:
            code = [f"[]({', '.join(params)}) -> {self.cpp_type(e.ty)} {{"]
            for i, gen in enumerate(e.generators):
                v = self.fresh("v")
                source = "sd_first" if i == 0 else self.expr(gen.iter)
                code.append(f"for (auto&& {v} : sd::iter({source})) {{")
                code.extend(self.bind_comprehension(gen.target, v))
                code.extend(f"if (!{self.cond(c)}) continue;" for c in gen.ifs)
            code.append(f"co_yield {self.expr_as(e.elt, elem)};")
            code.append("}" * len(e.generators))
            code.append(f"}}({', '.join(args)})")
        finally:
            if uses_self:
                self.lambda_self -= 1
        return " ".join(code)

    def bind_comprehension(self, target: A.Expr, source: str) -> list[str]:
        if isinstance(target, A.Name):
            var: Var = target.sym
            return [f"[[maybe_unused]] {self.cpp_type(var.type)} {ident(var.cpp_name)} = {source};"]
        tmp = self.fresh("t")
        out = [f"auto&& {tmp} = {source};"]
        for i, elt in enumerate(target.elts):
            out.extend(self.bind_comprehension(elt, f"std::get<{i}>({tmp})"))
        return out

    # =========================================================================
    # Calls
    # =========================================================================

    def call(self, e: A.Call) -> str:
        # (arguments like add_argument's type=int or hmac's digestmod=hashlib.sha256 are
        # instructions to the compiler, not values to evaluate)
        operands = [x for x in [*e.args, *(k.value for k in e.keywords)] if not getattr(x, "compile_time", False)]
        return self.in_order(operands, lambda: self.call_inner(e), keep_refs=True)

    def call_inner(self, e: A.Call) -> str:
        target: CallTarget = e.sym
        match target.kind:
            case "func":
                fn: FuncInfo = target.target
                return f"{qualified(fn_name(fn), fn.module)}({self.call_args(target.args, fn)})"
            case "method":
                fn = target.target
                recv = e.func.value
                args = self.call_args(target.args, fn)
                return f"{self.member(recv, fn)}({args})"
            case "static_method":  # Point.origin(), cls.make(), p.helper()
                fn = target.target
                return f"{class_name(fn.owner)}::{fn_name(fn)}({self.call_args(target.args, fn)})"
            case "ctor":
                st: StructType = target.target
                args = self.slot_codes(target.args, target.params)
                if st.enum is not None:  # Color(1): the member with that value (Color(member) is itself)
                    return args[0] if target.args[0].ty == st else f"{class_name(st)}::sd_lookup({args[0]})"
                if st.init is not None or (st.kind == "class" and not st.is_exception):
                    args = ["sd::init", *args]
                if st.kind == "class":
                    return f"std::make_shared<{class_name(st)}>({', '.join(args)})"
                return f"{class_name(st)}({', '.join(args)})"
            case "call_dunder":  # obj(args) -> obj.__call__(args)
                m = target.target
                return self.dunder_call(Dunder(m), self.expr(e.func), e.func.ty, self.slot_codes(target.args, m.params))
            case "super_method":
                fn = target.target
                prefix = self.self_prefix(self.func.owner)
                args = self.call_args(target.args, fn)
                return f"{prefix}{class_name(fn.owner)}::{fn_name(fn)}({args})"
            case "super_init":
                owner = target.target
                prefix = self.self_prefix(self.func.owner)
                return f"{prefix}{class_name(owner)}::sd_init({', '.join(self.slot_codes(target.args, target.params))})"
            case "isinstance":
                tests = " || ".join(f"sd::isinstance_of<{class_name(c)}>(sd_obj)" for c in target.target)
                return f"[&](const auto& sd_obj) {{ return {tests}; }}({self.expr(e.args[0])})"
            case "sync_new":
                return self.sync_new(e, *target.target)
            case "collection_new":
                return self.collection_new(e, *target.target)
            case "self_call":
                fn: FuncInfo = target.target
                rec = next(r for info, r in reversed(self.recursion) if info is fn)
                args = [self.expr_as(a, p.type) for a, p in zip(e.args, fn.params)]
                return f"{rec}({', '.join([rec, *args])})"
            case "value":
                ft: FuncType = target.target
                args = ", ".join(self.expr_as(a, pt) for a, pt in zip(e.args, ft.params))
                f = self.expr(e.func)
                return f"{f}({args})" if isinstance(e.func, A.Name) else f"({f})({args})"
            case "builtin":
                return self.builtin_call(target.target, e)
            case "builtin_method":
                recv_type, name = target.target
                return self.method_call(recv_type, name, e)
            case "module_func":
                mod, name = target.target
                return self.module_call(mod, name, e)
            case "class_func":  # Path.cwd(), datetime.now(tz)
                return self.function_call(target.target, e)
        raise NotImplementedError(f"codegen for call kind {target.kind}")

    def collection_new(self, e: A.Call, t: Type, extra: dict) -> str:
        cpp = self.cpp_type(t)
        match t:
            case DefaultDictType(key, value):
                if not e.args:
                    return f"{cpp}()"  # no factory: d[k] raises KeyError, like Python
                factory = extra["factory"]
                if factory is None:  # defaultdict(list): the value type's empty value
                    code = f"[]() -> {self.cpp_type(value)} {{ return {{}}; }}"
                else:
                    code = self.expr_as(factory, FuncType((), value))
                args = [code, cpp_string(extra["factory_repr"])]
                if len(e.args) == 2:
                    args.append(self.expr_as(e.args[1], DictType(key, value)))
                return f"{cpp}({', '.join(args)})"
            case CounterType(key):
                if not e.args:
                    return f"{cpp}()"
                if extra.get("counts"):
                    return f"{cpp}(sd::dict<{self.cpp_type(key)}, std::int64_t>({self.expr(e.args[0])}))"
                return f"{cpp}::from_items({self.expr(e.args[0])})"
            case DequeType(elem):
                maxlen = extra["maxlen"]
                ml = "std::nullopt" if maxlen is None else self.expr_as(maxlen, OptionalType(INT))
                if extra["items"] is None:
                    return f"{cpp}({ml})"
                return f"{cpp}({self.expr(extra['items'])}, {ml})"
        raise NotImplementedError(f"codegen for {t}()")

    # ---- csv --------------------------------------------------------------------------

    def csv_call(self, name: str, e: A.Call) -> str:
        kw = e.csv if name in ("DictReader", "DictWriter") else {**e.csv}
        dialect_node = kw.get("dialect") or (e.args[1] if name in ("reader", "writer") and len(e.args) > 1 else None)
        opt = lambda k, t: f"std::optional<{t}>({self.expr(kw[k])})" if k in kw else "std::nullopt"
        if "quotechar" in kw:
            q = kw["quotechar"]
            inner = "std::optional<std::string>()" if isinstance(q, A.NoneLit) else f"std::optional<std::string>({self.expr(q)})"
            quotechar = f"std::optional<std::optional<std::string>>({inner})"
        else:
            quotechar = "std::nullopt"
        escape = kw.get("escapechar")
        dialect = (f"sd::csv::make_dialect({self.expr(dialect_node) if dialect_node is not None else chr(34) + 'excel' + chr(34) + 's'}, "
                   f"{opt('delimiter', 'std::string')}, {quotechar}, "
                   f"{'std::nullopt' if escape is None or isinstance(escape, A.NoneLit) else opt('escapechar', 'std::string')}, "
                   f"{opt('doublequote', 'bool')}, {opt('skipinitialspace', 'bool')}, {opt('lineterminator', 'std::string')}, "
                   f"{opt('quoting', 'std::int64_t')}, {opt('strict', 'bool')})")
        match name:
            case "reader":
                return f"sd::csv::reader({self.expr(e.args[0])}, {dialect})"
            case "writer":
                return f"sd::csv::Writer({self.expr(e.args[0])}, {dialect})"
            case "DictReader":
                names = kw.get("fieldnames")
                names_code = "std::nullopt" if names is None or isinstance(names, A.NoneLit) else (
                    f"std::optional<sd::list<std::string>>(sd::to_list({self.expr(names)}))")
                restval = kw.get("restval")
                restval_code = "std::nullopt" if restval is None or isinstance(restval, A.NoneLit) else (
                    f"std::optional<std::string>({self.expr(restval)})")
                return f"sd::csv::DictReader({self.expr(kw['f'])}, {names_code}, {restval_code}, {dialect})"
            case "DictWriter":
                restval = f"sd::str({self.expr(kw['restval'])})" if "restval" in kw else '""s'
                extras = self.expr(kw["extrasaction"]) if "extrasaction" in kw else '"raise"s'
                return f"sd::csv::DictWriter({self.expr(kw['f'])}, {self.expr(kw['fieldnames'])}, {restval}, {extras}, {dialect})"
        raise NotImplementedError(name)

    # ---- logging ----------------------------------------------------------------------

    def log_call(self, e: A.Call, target: str) -> str:
        """logging.info(msg, *args) -> sd::logging::root_log(site, level, exc_info, msg, args...)."""
        info = e.log_call
        name = info["level"]
        func = self.func.name if self.func is not None else "<module>"
        site = f"sd::logging::Site{{{cpp_string(self.source_path)[:-1]}, {e.loc.line}, {cpp_string(func)[:-1]}}}"
        level = self.expr(e.args[0]) if name == "log" else str(builtins.LOG_LEVELS[name])
        exc = self.keyword(e, "exc_info")
        exc_code = "true" if name == "exception" and exc is None else (self.expr(exc) if exc is not None else "false")
        args = [self.expr(a) for a in e.args[info["first"]:]]
        return f"{target}({', '.join([site, level, exc_code, *args])})"

    # ---- hashlib, hmac ----------------------------------------------------------------

    def hash_call(self, mod: str, name: str, e: A.Call) -> str:
        args = e.hash_args

        def digest(node: A.Expr) -> str:
            return cpp_string(node.hash_name) if hasattr(node, "hash_name") else self.expr(node)

        data = next((self.expr(args[k]) for k in ("data", "string") if k in args), "sd::bytes()")
        if mod == "hashlib":
            algorithm = self.expr(args["name"]) if name == "new" else cpp_string(name)
            return f"sd::hashlib::Hash({algorithm}, {data})"
        if name == "new":
            msg = args.get("msg")
            msg_code = f"std::optional<sd::bytes>({self.expr(msg)})" if msg is not None and not isinstance(msg, A.NoneLit) else "std::nullopt"
            return f"sd::hmac::new_({self.expr(args['key'])}, {msg_code}, {digest(args['digestmod'])})"
        return f"sd::hmac::digest({self.expr(args['key'])}, {self.expr(args['msg'])}, {digest(args['digest'])})"

    # ---- itertools ------------------------------------------------------------------

    def itertools_call(self, name: str, e: A.Call) -> str:
        ns = "sd::itertools::"
        info = e.itertools
        args = info.get("args", {})
        out = e.ty.elem if isinstance(e.ty, GeneratorType) else None
        T = self.cpp_type(out) if out is not None else None
        a = [self.expr(x) for x in e.args]
        present = lambda k: k in args and not isinstance(args[k], A.NoneLit)
        opt_int = lambda k: self.expr_as(args[k], OptionalType(INT)) if present(k) else "std::nullopt"
        source_elem = lambda node: self.cpp_type(element_type(node.ty) if not hasattr(node, "tuple_elem") else node.tuple_elem)
        match name:
            case "count":
                start = self.expr_as(args["start"], out) if "start" in args else f"{T}(0)"
                step = self.expr_as(args["step"], out) if "step" in args else f"{T}(1)"
                return f"{ns}count<{T}>({start}, {step})"
            case "cycle":
                return f"{ns}cycle<{T}>({a[0]})"
            case "pairwise":
                return f"{ns}pairwise<{self.cpp_type(out.elts[0])}>({a[0]})"
            case "repeat":
                return f"{ns}repeat<{T}>({self.expr_as(args['object'], out)}, {opt_int('times')})"
            case "accumulate":
                func = self.expr(args["func"]) if present("func") else "[](const auto& x, const auto& y) { return x + y; }"
                initial = f"std::optional<{T}>({self.expr_as(args['initial'], out)})" if present("initial") else "std::nullopt"
                return f"{ns}accumulate<{T}>({self.expr(args['iterable'])}, {func}, {initial})"
            case "chain":
                parts = ", ".join(f"{ns}as_generator<{T}>({x})" for x in a)
                return f"{ns}chain<{T}>({{{parts}}})"
            case "chain.from_iterable":
                return f"{ns}from_iterable<{T}>({a[0]})"
            case "compress":
                return f"{ns}compress<{T}>({a[0]}, {a[1]})"
            case "dropwhile" | "takewhile" | "filterfalse":
                return f"{ns}{name}<{T}>({a[0]}, {a[1]})"
            case "groupby":
                key_t, item_t = out.elts[0], out.elts[1].elem
                key = self.expr(args["key"]) if present("key") else "[](const auto& x) { return x; }"
                return f"{ns}groupby<{self.cpp_type(key_t)}, {self.cpp_type(item_t)}>({self.expr(args['iterable'])}, {key})"
            case "islice":
                idx = [self.expr_as(x, OptionalType(INT)) for x in e.args[1:]]
                start, stop, step = ("std::nullopt", idx[0], "std::nullopt") if len(idx) == 1 else (
                    idx[0], idx[1], idx[2] if len(idx) > 2 else "std::nullopt")
                return f"{ns}islice<{T}>({a[0]}, {start}, {stop}, {step})"
            case "starmap":
                return f"{ns}starmap<{T}>({a[0]}, {a[1]})"
            case "tee":
                return f"{ns}tee<{self.cpp_type(e.ty.elts[0].elem)}, {info['n']}>({a[0]})"
            case "zip_longest":
                fill = self.keyword(e, "fillvalue")
                fill_code = self.expr(fill) if fill is not None and not isinstance(fill, A.NoneLit) else "std::nullopt"
                gens = ", ".join(f"{ns}as_generator<{self.cpp_type(t)}>({x})" for t, x in zip(info["elems"], a))
                return f"{ns}zip_longest<{T}>({fill_code}, {gens})"
            case "product":
                pools = ", ".join(f"sd::to_list({x})" for x in a)
                repeated = ", ".join(["sd_pools..."] * info["repeat"])
                return f"[&](auto... sd_pools) {{ return {ns}product<{T}>({repeated}); }}({pools})"
            case "permutations":
                items = args["iterable"]
                return f"{ns}permutations<{T}, {source_elem(items)}>(sd::to_list({self.expr(items)}), {opt_int('r')})"
            case "combinations" | "combinations_with_replacement":
                items = args["iterable"]
                repl = "true" if name == "combinations_with_replacement" else "false"
                return (f"{ns}combinations<{T}, {source_elem(items)}>(sd::to_list({self.expr(items)}), "
                        f"{self.expr(args['r'])}, {repl})")
            case "batched":
                return f"{ns}batched<{self.cpp_type(out.elem)}>({self.expr(args['iterable'])}, {self.expr(args['n'])})"
        raise NotImplementedError(f"codegen for itertools.{name}")

    # ---- heapq and bisect -----------------------------------------------------------

    def heapq_bisect_call(self, mod: str, name: str, e: A.Call) -> str:
        args = getattr(e, "lib_args", {})
        key = args.get("key", self.keyword(e, "key"))
        key_code = self.expr(key) if key is not None and not isinstance(key, A.NoneLit) else "nullptr"
        if mod == "heapq":
            match name:
                case "heapify" | "heappop":
                    return f"sd::heapq::{name}({self.expr(e.args[0])})"
                case "heappush" | "heappushpop" | "heapreplace":
                    return f"sd::heapq::{name}({self.expr(e.args[0])}, {self.expr_as(e.args[1], e.args[0].ty.elem)})"
                case "nlargest" | "nsmallest":
                    T = self.cpp_type(e.ty.elem)
                    return (f"sd::heapq::{name}<{T}>({self.expr_as(args['n'], INT)}, {self.expr(args['iterable'])}, "
                            f"{key_code})")
                case "merge":
                    T = self.cpp_type(e.ty.elem)
                    inputs = ", ".join(f"sd::heapq::as_generator<{T}>({self.expr(x)})" for x in e.args)
                    reverse = self.keyword(e, "reverse")
                    return (f"sd::heapq::merge<{T}>(std::vector<sd::Generator<{T}>>{{{inputs}}}, {key_code}, "
                            f"{self.expr(reverse) if reverse is not None else 'false'})")
            raise NotImplementedError(f"codegen for heapq.{name}")
        func = {"bisect": "bisect_right", "insort": "insort_right"}.get(name, name)
        a = args["a"]
        x = self.expr_as(args["x"], a.ty.elem if func.startswith("insort") else e.compared_as)
        lo = self.expr_as(args["lo"], INT) if "lo" in args else "0"
        hi = self.expr_as(args["hi"], OptionalType(INT)) if "hi" in args else "std::nullopt"
        return f"sd::bisect::{func}({self.expr(a)}, {x}, {lo}, {hi}, {key_code})"

    # ---- subprocess -----------------------------------------------------------------

    def process_call(self, e: A.Call) -> str:
        spec = e.process
        kw, streams, op = spec["kw"], spec["streams"], spec["op"]

        def redirect(name: str) -> str:
            kind = streams[name]
            if kind == "inherit":
                return "sd::subprocess::Redirect()"
            if kind == "file":
                return f"sd::subprocess::Redirect({self.expr(kw[name])})"
            return f"sd::subprocess::Redirect(sd::subprocess::{kind})"

        fields = [f".in = {redirect('stdin')}", f".out = {redirect('stdout')}", f".err = {redirect('stderr')}"]
        if "shell" in kw:
            fields.append(f".shell = {self.expr(kw['shell'])}")
        fields.append(f".text = {'true' if spec['text'] else 'false'}")
        if "cwd" in kw and not isinstance(kw["cwd"], A.NoneLit):
            fields.append(f".cwd = {self.expr(kw['cwd'])}")
        if "env" in kw:
            fields.append(f".env = {self.expr(kw['env'])}")
        options = f"sd::subprocess::Options{{{', '.join(fields)}}}"
        args = f"sd::subprocess::Args({self.expr(e.args[0])})"
        timeout = self.expr_as(kw["timeout"], OptionalType(FLOAT)) if "timeout" in kw else "std::nullopt"
        data = f"std::optional<std::string>(sd::raw({self.expr(kw['input'])}))" if "input" in kw else "std::nullopt"
        match op:
            case "run":
                check = self.expr(kw["check"]) if "check" in kw else "false"
                return f"sd::subprocess::run({args}, {options}, {data}, {timeout}, {check})"
            case "check_output":
                out = f"sd::subprocess::check_output({args}, {options}, {data}, {timeout})"
                return out if spec["text"] else f"sd::bytes({out})"
            case "call" | "check_call":
                return f"sd::subprocess::{op}({args}, {options}, {timeout})"
            case "Popen":
                return f"sd::subprocess::Popen({args}, {options})"
        raise NotImplementedError(op)

    def process_attribute(self, obj: str, t: ProcessType, attr: str, result: Type) -> str:
        if attr == "args":
            src = f"{obj}.args" if t.kind == "CompletedProcess" else f"{obj}.args_value()"
            return f"(*{src}.str_)" if result == STR else f"{src}.list_"
        if t.kind == "CompletedProcess":
            if attr in ("stdout", "stderr"):
                raw = "out" if attr == "stdout" else "err"
                return f"sd::subprocess::as<{self.cpp_type(result)}>({obj}.{raw})"
            return f"{obj}.{attr}"
        if attr in ("stdin", "stdout", "stderr"):
            return f"{obj}.{attr}_{'text' if t.text else 'binary'}()"
        return f"{obj}.{attr}()"

    def process_method(self, r: str, t: ProcessType, name: str, e: A.Call) -> str:
        if name == "communicate":
            args = e.regex_args
            data = "std::nullopt"
            if "input" in args and not isinstance(args["input"], A.NoneLit):
                data = f"std::optional<std::string>(sd::raw({self.expr(args['input'])}))"
            timeout = self.expr_as(args["timeout"], OptionalType(FLOAT)) if "timeout" in args else "std::nullopt"
            t0, t1 = (self.cpp_type(x) for x in e.ty.elts)
            return (f"[](auto sd_r) {{ return std::make_tuple(sd::subprocess::as<{t0}>(std::get<0>(sd_r)), "
                    f"sd::subprocess::as<{t1}>(std::get<1>(sd_r))); }}({r}.communicate({data}, {timeout}))")
        if name == "wait":
            node = e.args[0] if e.args else self.keyword(e, "timeout")
            return f"{r}.wait({self.expr_as(node, OptionalType(FLOAT)) if node is not None else 'std::nullopt'})"
        args = ", ".join(self.expr(a) for a in e.args)
        return f"{r}.{name}({args})"

    # ---- re -------------------------------------------------------------------------

    def re_call(self, name: str, e: A.Call) -> str:
        if name == "escape":
            return f"sd::re::escape({self.expr(e.args[0])})"
        if name == "purge":
            return "(void)0"
        args = e.regex_args
        pattern = args["pattern"]
        flags = getattr(e, "regex_static", None)
        if flags is not None and isinstance(pattern, A.StrLit):
            # A literal pattern is compiled once, the first time this line runs (thread-safe).
            code = (f"[]() -> const sd::re::Pattern& {{ static const sd::re::Pattern sd_p("
                    f"{cpp_string(pattern.value)}, {flags}); return sd_p; }}()")
        else:
            fl = self.expr(args["flags"]) if "flags" in args else "0"
            code = f"sd::re::compile_cached({self.expr(pattern)}, {fl})"
        if name == "compile":
            return f"sd::re::Pattern({code})"
        return self.regex_op(code, name, args, e)

    def regex_op(self, pat: str, op: str, args: dict, e: A.Call) -> str:
        """pattern.<op>(...) for re.search(...) and compiled.search(...) alike."""
        s = self.expr(args["string"])
        match op:
            case "search" | "match" | "fullmatch":
                pos = self.expr(args["pos"]) if "pos" in args else "0"
                end = self.expr_as(args["endpos"], OptionalType(INT)) if "endpos" in args else "std::nullopt"
                return f"{pat}.{op}({s}, {pos}, {end})"
            case "findall" if isinstance(e.ty.elem, TupleType):
                return f"{pat}.findall_tuples<{len(e.ty.elem.elts)}>({s})"
            case "findall" | "finditer":
                return f"{pat}.{op}({s})"
            case "sub" | "subn":
                count = self.expr(args["count"]) if "count" in args else "0"
                if getattr(e, "regex_repl_fn", False):
                    repl = self.expr_as(args["repl"], FuncType((MatchType(None),), STR))
                    return f"{pat}.{op}_fn({repl}, {s}, {count})"
                return f"{pat}.{op}({self.expr(args['repl'])}, {s}, {count})"
            case "split":
                maxsplit = self.expr(args["maxsplit"]) if "maxsplit" in args else "0"
                fn = "split_opt" if isinstance(e.ty.elem, OptionalType) else "split"
                return f"{pat}.{fn}({s}, {maxsplit})"
        raise NotImplementedError(f"codegen for re {op}")

    def match_group(self, m: str, node: A.Expr, t: Type) -> str:
        """m.group(g) / m[g]: a str when the group always matches, else str?."""
        g = getattr(node, "regex_group", None)
        arg = f"std::int64_t{{{g}}}" if g is not None else self.expr(node)
        return f"{m}.{'group_opt' if isinstance(t, OptionalType) else 'group_str'}({arg})"

    def match_method(self, r: str, name: str, e: A.Call) -> str:
        match name:
            case "group" if not e.args:
                return f"{r}.group_str(0)"
            case "group" if len(e.args) == 1:
                return self.match_group(r, e.args[0], e.ty)
            case "group":
                return f"std::make_tuple({', '.join(self.match_group(r, a, t) for a, t in zip(e.args, e.ty.elts))})"
            case "groups" if isinstance(e.ty, TupleType):
                parts = [f"{r}.{'group_opt' if isinstance(t, OptionalType) else 'group_str'}({i})"
                         for i, t in enumerate(e.ty.elts, 1)]
                return f"std::make_tuple({', '.join(parts)})"
            case "groups":
                return f"{r}.groups_list()"
            case "groupdict":
                return f"{r}.{'groupdict' if isinstance(e.ty.value, OptionalType) else 'groupdict_str'}()"
            case "start" | "end" | "span":
                if not e.args:
                    return f"{r}.{name}()"
                g = getattr(e.args[0], "regex_group", None)
                return f"{r}.{name}({f'std::int64_t{{{g}}}' if g is not None else self.expr(e.args[0])})"
            case "expand":
                return f"{r}.expand({self.expr(e.args[0])})"
        raise NotImplementedError(f"codegen for re.Match.{name}")

    def sync_new(self, e: A.Call, t: SyncType, extra: dict) -> str:
        cpp = self.cpp_type(t)
        if t.kind in ("Atomic", "Mutex", "RWMutex", "Queue"):
            node = extra.get("value") or extra.get("maxsize")
            if node is None:
                return f"{cpp}()"
            want = t.args[0] if t.kind in ("Mutex", "RWMutex") else INT
            return f"{cpp}({self.sent(node, self.expr_as(node, want))})"
        if t.kind != "Thread":
            return f"{cpp}()"
        # The thread gets its own copies of the target and its arguments.
        ft: FuncType = extra["target_type"]
        args = extra["args"]
        if isinstance(args, A.TupleLit):
            packed = f"std::make_tuple({', '.join(self.sent(a, self.expr_as(a, p)) for a, p in zip(args.elts, ft.params))})"
        elif args is not None:
            packed = self.coerce(self.expr(args), args.ty, TupleType(ft.params))
        else:
            packed = "std::make_tuple()"
        name = self.expr(extra["name"]) if extra["name"] is not None else "std::nullopt"
        daemon = self.expr(extra["daemon"]) if extra["daemon"] is not None else "false"
        body = f"[sd_f = {self.expr(extra['target'])}, sd_a = sd::send({packed})]() mutable {{ std::apply(sd_f, sd_a); }}"
        return f"sd::threading::Thread({body}, {name}, {cpp_string(extra['target_name'])}, {daemon})"

    def call_args(self, slots: list[A.Expr | None], fn: FuncInfo) -> str:
        return ", ".join(self.slot_codes(slots, fn.params))

    def slot_codes(self, slots: list[A.Expr | None], params: list) -> list[str]:
        """One argument per parameter; missing ones use the parameter's default expression."""
        out = []
        for slot, p in zip(slots, params):
            arg = slot if slot is not None else p.default
            out.append(self.expr_as(arg, p.type))
        return out

    def keyword(self, e: A.Call, name: str) -> A.Expr | None:
        for kw in e.keywords:
            if kw.name == name:
                return kw.value
        return None

    def builtin_call(self, name: str, e: A.Call) -> str:
        args = [self.expr(a) for a in e.args]
        a = args[0] if args else None
        match name:
            case "print":
                sep = self.keyword(e, "sep")
                end = self.keyword(e, "end")
                head = [self.expr(sep) if sep else '" "', self.expr(end) if end else r'"\n"']
                flush = self.keyword(e, "flush")
                if flush is not None and not (isinstance(flush, A.BoolLit) and not flush.value):
                    if file := self.keyword(e, "file"):
                        out = self.fresh("out")
                        return (f"[&] {{ auto {out} = {self.expr(file)}; sd::print_to({', '.join([out, *head, *args])}); "
                                f"if ({self.expr(flush)}) {out}->flush(); }}()")
                    return f"[&] {{ sd::print({', '.join(head + args)}); if ({self.expr(flush)}) std::fflush(stdout); }}()"
                if file := self.keyword(e, "file"):
                    return f"sd::print_to({', '.join([self.expr(file), *head, *args])})"
                return f"sd::print({', '.join(head + args)})"
            case "open":
                path = f"{a}.str()" if e.args[0].ty == PATH else a
                return self.open_call(path, e.args[1] if len(e.args) > 1 else self.keyword(e, "mode"), e)
            case "len" if (m := user_dunder(e.args[0].ty, "__len__")):
                return self.dunder_call(Dunder(m), a, e.args[0].ty, [])
            case "len":
                return f"sd::len({a})"
            case "hash":
                return f"static_cast<std::int64_t>(sd::Hash{{}}({a}))"
            case "str" if len(e.args) == 2 or e.keywords:  # str(data, "utf-8")
                return f"sd::bytes_decode({a}, {self.expr(e.args[1] if len(e.args) == 2 else e.keywords[0].value)})"
            case "str":
                return f"sd::str({a})" if a else '""s'
            case "repr":
                return f"sd::repr({a})"
            case "dict.fromkeys":
                value = self.expr_as(e.args[1], e.ty.value) if len(e.args) == 2 else f"{self.cpp_type(e.ty.value)}{{}}"
                return f"sd::dict_fromkeys<{self.cpp_type(e.ty.key)}, {self.cpp_type(e.ty.value)}>({a}, {value})"
            case "str.maketrans" | "bytes.maketrans" | "bytes.fromhex":
                return f"sd::{name.replace('.', '_')}({', '.join(args)})"
            case "int.from_bytes" | "float.fromhex":  # (with keywords and defaults)
                check = builtins.TYPE_FUNCTIONS[tuple(name.split("."))]
                return self.function_call(builtins.Function(name, check, f"sd::{name.replace('.', '_')}", check.params), e)
            case "bin" | "oct" | "hex":
                return f"sd::{name}({a})"
            case "divmod":
                return f"sd::divmod({', '.join(self.expr_as(x, e.ty.elts[0]) for x in e.args)})"
            case "exit" | "quit":
                return f"throw sd::Exit{{static_cast<int>({a or '0'})}}"
            case "__same_class__":  # (in generated dataclass methods: Python compares only the same class)
                return f"sd::same_class({args[0]}, {args[1]})"
            case "__class_name__":
                return f"{a}->sd_class_name()"
            case "format" if len(e.args) == 1 or (isinstance(e.args[1], A.StrLit) and not e.args[1].value):
                return f"sd::str({a})"
            case "format" if isinstance(e.args[1], A.StrLit):
                return f"sd::format_any({a}, {cpp_string(e.args[1].value)[:-1]}sv)"
            case "format":
                return f"sd::format_any({', '.join(args)})"
            case "ascii":
                return f"sd::ascii(sd::repr({a}))"
            case "int":
                base = e.args[1] if len(e.args) == 2 else self.keyword(e, "base")
                return f"sd::to_int({', '.join(x for x in (a, self.expr(base) if base else None) if x)})"
            case "float":
                return f"sd::to_float({a or ''})"
            case "bool":
                return self.cond(e.args[0]) if a else "false"
            case "range":
                return f"sd::range({', '.join(args)})"
            case "abs" if (m := user_dunder(e.args[0].ty, "__abs__")):
                return self.dunder_call(Dunder(m), a, e.args[0].ty, [])
            case "pow" if len(e.args) == 3:
                return f"sd::powmod({', '.join(args)})"
            case "pow":
                return f"sd::pow({', '.join(self.expr_as(x, e.ty) for x in e.args)})"
            case "abs" if e.args[0].ty in DATETIME_TYPES:
                return f"sd::datetime::abs({a})"
            case "abs":
                return f"sd::abs({a})"
            case "min" | "max" if (default := self.keyword(e, "default")) is not None:
                fallback, result = self.expr_as(default, e.ty), self.cpp_type(e.ty)
                if key := self.keyword(e, "key"):
                    return f"sd::{name}_by_or<{result}>({a}, {self.expr(key)}, {fallback})"
                return f"sd::{name}_of_or<{result}>({a}, {fallback})"
            case "min" | "max":
                if key := self.keyword(e, "key"):
                    return f"sd::{name}_by({a}, {self.expr(key)})"
                if len(args) == 1:
                    return f"sd::{name}_of({a})"
                values = ", ".join(self.expr_as(x, e.ty) for x in e.args)
                return f"std::{name}({{{values}}})"
            case "sum" if len(e.args) == 2 or e.keywords:
                start = e.args[1] if len(e.args) == 2 else self.keyword(e, "start")
                return f"sd::sum({self.expr(e.args[0])}, {self.expr_as(start, e.ty)})"
            case "zip" if (strict := self.keyword(e, "strict")) is not None:
                return f"sd::zip_strict<{self.cpp_type(e.ty.elem)}>({', '.join([self.expr(strict), *args])})"
            case "zip":
                return f"sd::zip_lazy<{self.cpp_type(e.ty.elem)}>({', '.join(args)})"
            case "sum" | "any" | "all" | "reversed" | "ord" | "chr":
                return f"sd::{name}({', '.join(args)})"
            case "sorted":
                rev = self.keyword(e, "reverse")
                rev_code = self.expr(rev) if rev else "false"
                if key := self.keyword(e, "key"):
                    return f"sd::sorted_by({a}, {self.expr(key)}, {rev_code})"
                return f"sd::sorted({a}, {rev_code})"
            case "map":
                return f"sd::map_lazy<{self.cpp_type(e.ty.elem)}>({', '.join(args)})"
            case "filter" if isinstance(e.args[0], A.NoneLit):  # the items that are true
                return f"sd::filter_lazy<{self.cpp_type(e.ty.elem)}>([](const auto& x) {{ return sd::truthy(x); }}, {args[1]})"
            case "filter":
                return f"sd::filter_lazy<{self.cpp_type(e.ty.elem)}>({args[0]}, {args[1]})"
            case "enumerate":
                item = self.cpp_type(e.ty.elem.elts[1])
                start = e.args[1] if len(e.args) > 1 else self.keyword(e, "start")
                return f"sd::enumerate_lazy<{item}>({args[0]}, {self.expr(start) if start else '0'})"
            case "list":
                return f"sd::to_list({a})" if a else f"{self.cpp_type(e.ty)}{{}}"
            case "iter" if isinstance(e.args[0].ty, GeneratorType):
                return a
            case "iter":
                return f"sd::iterate<{self.cpp_type(e.ty.elem)}>(sd::to_list({a}))"
            case "next" if e.args[0].ty == CSV_DICT_READER:
                return f"sd::next({a}.stream())"
            case "next" if len(e.args) == 2:
                return f"sd::next_or<{self.cpp_type(e.ty)}>({a}, {self.expr_as(e.args[1], e.ty)})"
            case "next":
                return f"sd::next({a})"
            case "set":
                return f"sd::to_set({a})" if a else f"{self.cpp_type(e.ty)}{{}}"
            case "tuple" if isinstance(e.ty, VarTupleType):
                return f"{self.cpp_type(e.ty)}(sd::to_list({a}))" if a else f"{self.cpp_type(e.ty)}{{}}"
            case "tuple":
                return "std::tuple<>{}"
            case "dict" if e.keywords:  # dict(a=1), dict(other, b=2): the keywords are str keys
                tmp = self.fresh("d")
                positional = A.Call(e.func, e.args, [], loc=e.loc, ty=e.ty)
                positional.pairs = getattr(e, "pairs", False)
                base = self.builtin_call("dict", positional)
                sets = " ".join(f"{tmp}[{cpp_string(kw.name)}] = {self.expr_as(kw.value, e.ty.value)};" for kw in e.keywords)
                return f"[&] {{ auto {tmp} = {base}; {sets} return {tmp}; }}()"
            case "dict" if getattr(e, "pairs", False):
                k, v = e.ty.key, e.ty.value
                return f"sd::dict_from_pairs<{self.cpp_type(k)}, {self.cpp_type(v)}>({a})"
            case "dict" if a:
                return f"{self.cpp_type(e.ty)}(sd::shallow_copy({a}))"  # a copy (of a plain dict, defaultdict or Counter)
            case "dict":
                return f"{self.cpp_type(e.ty)}{{}}"
            case "input":
                return f"sd::input({a or ''})"
            case "bytes" if len(e.args) == 2 or e.keywords:  # bytes(text, "utf-8")
                return f"sd::str_encode({a}, {self.expr(e.args[1] if len(e.args) == 2 else e.keywords[0].value)})"
            case "bytes":
                return f"sd::to_bytes({a or ''})"
            case "round":
                return f"sd::round({', '.join(args)})"
        raise NotImplementedError(f"codegen for builtin {name}()")

    def open_call(self, path: str, mode: A.Expr | None, e: A.Call) -> str:
        """open(path) / open(fd) / os.fdopen(fd): the runtime overloads on the path or fd."""
        mode_code = self.expr(mode) if mode else '"r"s'
        closefd = self.keyword(e, "closefd")
        if e.ty.binary:
            return f"sd::open_binary({path}, {mode_code}{', ' + self.expr(closefd) if closefd else ''})"
        encoding, newline = self.keyword(e, "encoding"), self.keyword(e, "newline")
        args = [path, mode_code]
        if encoding or newline or closefd:
            args.append(self.expr(encoding) if encoding else '"utf-8"s')
        if newline is not None and not isinstance(newline, A.NoneLit):
            args.append(f"std::optional<std::string>({self.expr(newline)})")
        elif closefd:
            args.append("std::nullopt")
        if closefd:
            args.append(self.expr(closefd))
        return f"sd::open_text({', '.join(args)})"

    def struct_op(self, name: str, e: A.Call, receiver: str | None) -> str:
        """struct.pack(fmt, ...) / unpack(fmt, buffer) / unpack_from / iter_unpack, or the same
        on a Struct object (then the format isn't an argument)."""
        args = list(e.args)
        head = [] if receiver is not None else [self.expr(args.pop(0))]
        target = f"{receiver}." if receiver is not None else "sd::structmod::"
        if name == "pack":
            values = [self.expr_as(a, t) for a, t in zip(args, e.struct_args)]
            return f"{target}pack({', '.join(head + values)})"
        row = self.cpp_type(e.struct_row)
        rest = [self.expr(args[0])]
        if name == "unpack_from":
            offset = args[1] if len(args) > 1 else self.keyword(e, "offset")
            if offset is not None:
                rest.append(self.expr_as(offset, INT))
        return f"{target}{name}<{row}>({', '.join(head + rest)})"

    def argument_spec(self, e: A.Call) -> str:
        """An argparse Spec built from the literal add_argument(...) call."""
        a = e.argparse
        kw = a["kw"]
        lines = [f"s.flags = {{{', '.join(cpp_string(f) for f in a['flags'])}}};", f"s.dest = {cpp_string(a['dest'])};",
                 f"s.action = {cpp_string(a['action'])};", f"s.kind = sd::argparse::{a['kind']};"]
        nargs = a["nargs"]
        lines.append(f"s.nargs = {nargs if nargs.isdigit() else 'sd::argparse::' + nargs};")
        for name, field in (("default", "default_"), ("const", "const_")):
            if name in kw and not isinstance(kw[name], A.NoneLit):
                lines.append(f"s.{field} = sd::argparse::Value({self.expr(kw[name])});")
        if "choices" in kw:
            items = ", ".join(f"sd::argparse::Value({self.expr(c)})" for c in kw["choices"].elts)
            lines.append(f"s.choices = {{{items}}};")
        if a["required"]:
            lines.append("s.required = true;")
        for name in ("help", "metavar", "version"):
            if name in kw:
                lines.append(f"s.{name} = {self.expr(kw[name])};")
        return f"[&] {{ sd::argparse::Spec s; {' '.join(lines)} return s; }}()"

    def sent(self, node: A.Expr, code: str) -> str:
        """An argument that another thread receives (sd::send copies it): moved instead, when
        it's a local list the sender never reads again."""
        if getattr(node, "moved_into_mutex", False) and code == self.expr(node):
            return f"std::move({code})"  # the Mutex owns it now (the checker stops later reads)
        if (
            isinstance(node, A.Name) and isinstance(node.sym, Var) and node.sym.kind == "local" and not node.sym.captured
            and holds_references(node.sym.type) and code == self.expr(node) and self.func is not None
            and last_use(self.func.node.body, node)
        ):
            return f"std::move({code})"
        return code

    def str_format(self, e: A.Call) -> str:
        """"...".format(...). A literal format string was compiled into an f-string over the
        arguments: evaluate each once (self.call did them in order if that matters), then build
        that. Any other is parsed at run time."""
        values = e.args + [kw.value for kw in e.keywords]
        fstring = getattr(e, "format_fstring", None)
        if fstring is None:
            fmt = e.func.value
            types = ", ".join(cpp_string(str(strip_optional(v.ty)).split("[")[0])[:-1] for v in values)
            names = ", ".join(cpp_string(kw.name)[:-1] for kw in e.keywords)
            return f"sd::str_format({self.expr(fmt)}, {{{types}}}, {{{names}}}{''.join(', ' + self.expr(v) for v in values)})"
        decls, args = [], []
        for v in values:
            if is_simple(v) or id(v) in self.precomputed:
                args.append(self.expr(v))
            else:  # (an argument the format string doesn't use is still evaluated)
                tmp = self.fresh("a")
                decls.append(f"[[maybe_unused]] auto {tmp} = {self.expr(v)};")
                args.append(tmp)
        saved, self.format_args = self.format_args, args
        try:
            body = self.expr(fstring)
        finally:
            self.format_args = saved
        return f"[&] {{ {' '.join(decls)} return {body}; }}()" if decls else body

    def method_call(self, recv_type: Type, name: str, e: A.Call) -> str:
        r = self.expr(e.func.value)
        args = [self.expr(a) for a in e.args]
        rest = "".join(", " + a for a in args)
        if recv_type == STR and name == "format":
            return self.str_format(e)
        if recv_type == STR and name == "format_map":
            vt = e.args[0].ty.value
            return f"sd::str_format_map({r}, {args[0]}, {cpp_string(str(strip_optional(vt)).split('[')[0])[:-1]})"
        if recv_type in (STR, BYTES, INT, FLOAT):
            prefix = str(recv_type)
            handler = builtins.method_for(recv_type, name)
            if hasattr(handler, "params"):  # keywords and defaults: s.split(maxsplit=1), s.find(x, 2)
                codes = []
                for i, (pname, ptype, *default) in enumerate(handler.params):
                    node = e.args[i] if i < len(e.args) else self.keyword(e, pname)
                    codes.append(default[0] if node is None else self.expr_as(node, handler.resolve(ptype, recv_type)))
                rest = "".join(", " + c for c in codes)
            return f"sd::{prefix}_{name}({r}{rest})"
        if isinstance(recv_type, FileType):
            return f"{r}->{'fileno_' if name == 'fileno' else name}({', '.join(args)})"  # (fileno: a macro on macOS)
        if recv_type == JSON_VALUE:
            return f"{r}.{name}({', '.join(args)})"
        if recv_type == PATH and name == "joinpath":
            return f"{r}.joinpath({', '.join(args)})"
        if recv_type == PATH and name == "open":
            return self.open_call(f"{r}.str()", e.args[0] if e.args else self.keyword(e, "mode"), e)
        if isinstance(recv_type, ParserType) and name == "add_argument":
            return f"{r}.add_argument({self.argument_spec(e)})"
        if isinstance(recv_type, ParserType) and name == "add_subparsers":
            sub = e.argparse_sub
            lines = [f"s.dest = {cpp_string(sub['dest'])};", f"s.required = {'true' if sub['required'] else 'false'};"]
            for field in ("help", "metavar"):
                if field in sub["kw"]:
                    lines.append(f"s.{field} = {self.expr(sub['kw'][field])};")
            return f"{r}.add_subparsers([&] {{ sd::argparse::Spec s; {' '.join(lines)} return s; }}())"
        if isinstance(recv_type, SubParsersType):  # add_parser(name, help=, aliases=, description=)
            cmd = e.argparse_cmd
            opt = lambda k: self.expr_as(cmd["kw"][k], OptionalType(STR)) if k in cmd["kw"] else "std::nullopt"
            aliases = ", ".join(cpp_string(a) for a in cmd["aliases"])
            return (f"{r}.add_parser({cpp_string(cmd['name'])}, {opt('help')}, sd::list<std::string>{{{aliases}}}, "
                    f"{opt('description')})")
        if isinstance(recv_type, ParserType) and name == "parse_args":
            node = e.regex_args["args"]
            given = node is not None and not isinstance(node, A.NoneLit)
            return f"{r}.parse_args({self.expr_as(node, OptionalType(ListType(STR))) if given else 'std::nullopt'})"
        if recv_type == STR_TEMPLATE and name in ("substitute", "safe_substitute"):
            keywords = ", ".join(f"{{{cpp_string(k.name)}, sd::str({self.expr(k.value)})}}" for k in e.keywords)
            mapping = f"sd::stringmod::stringify({self.expr(e.args[0])})" if e.args else "std::nullopt"
            safe = "true" if name == "safe_substitute" else "false"
            return f"{r}.substitute(sd::dict<std::string, std::string>{{{keywords}}}, {mapping}, {safe})"
        if recv_type == DATETIME and name in ("date", "time"):
            return f"{r}.to_{name}()"  # (a C++ member can't share its class's name)
        if recv_type == LOGGER and hasattr(e, "log_call"):
            return self.log_call(e, f"{r}.log")
        if recv_type in (LOGGER, LOG_HANDLER) and name == "setLevel":
            return f"{r}.setLevel(sd::logging::level_of({self.expr(e.args[0])}))"
        if recv_type == LOGGER and name == "getChild":
            return f"sd::logging::getLogger({r}.name() + \".\" + {self.expr(e.args[0])})"
        if recv_type == EXECUTOR and name in ("submit", "map"):
            params, result = e.work_types
            fn = self.expr_as(e.args[0], FuncType(params, result))
            if name == "submit":
                lent = getattr(e, "lent", set())  # shared with the task, not copied (threads.Spawn.lend_arguments)
                args = ", ".join([fn, *(f"sd::lend({self.expr_as(a, p)})" if i in lent else self.sent(a, self.expr_as(a, p))
                                        for i, (a, p) in enumerate(zip(e.args[1:], params), start=1))])
                return f"{r}.submit<{self.cpp_type(result)}>({args})"
            timeout = self.keyword(e, "timeout")
            t = self.expr_as(timeout, OptionalType(FLOAT)) if timeout is not None else "std::nullopt"
            return f"{r}.map<{self.cpp_type(result)}>({', '.join([fn, t, *(self.expr(a) for a in e.args[1:])])})"
        if isinstance(recv_type, FutureType) and name == "add_done_callback":
            return f"{r}.add_done_callback({self.expr_as(e.args[0], FuncType((recv_type,), NONE))})"
        if recv_type in (HASH, HMAC_T) and name == "update":
            return f"{r}.update({self.expr(e.args[0])})"
        if isinstance(recv_type, StructFormatType):
            return self.struct_op(name, e, r)
        if recv_type in (SQLITE_CONNECTION, SQLITE_CURSOR):
            if name in ("fetchone", "fetchmany", "fetchall"):  # the row type comes from the context
                return f"{r}.{name}<{self.cpp_type(e.sqlite_row)}>({', '.join(self.expr(a) for a in e.args)})"
            args = [self.expr(a) for a in e.args] + [self.expr(k.value) for k in e.keywords]
            return f"{r}.{name}({', '.join(args)})"
        if recv_type in (CSV_WRITER, CSV_DICT_WRITER):
            return f"{r}.{name}({', '.join(self.expr(a) for a in e.args)})"
        if recv_type == HTTP_CONNECTION and name == "request":
            args = e.http_args
            body = args.get("body")
            if body is None or isinstance(body, A.NoneLit):
                body_code = "std::nullopt"
            elif body.ty == STR:  # sent as Latin-1, like Python
                body_code = f"std::optional<sd::bytes>(sd::httpclient::latin1_body({self.expr(body)}))"
            else:
                body_code = f"std::optional<sd::bytes>({self.expr(body)})"
            headers = self.expr(args["headers"]) if "headers" in args else "sd::dict<std::string, std::string>{}"
            return f"{r}.request({self.expr(args['method'])}, {self.expr(args['url'])}, {body_code}, {headers})"
        if recv_type == EXIT_STACK and name == "enter_context":
            return self.enter_context(r, e)
        if recv_type == EXIT_STACK and name == "callback":
            fn = e.ty
            given = [self.expr_as(a, p) for a, p in zip(e.args[1:], fn.params)]
            return f"{r}.callback({', '.join([f'{self.cpp_type(fn)}({self.expr(e.args[0])})', *given])})"
        if isinstance(recv_type, (SyncType, ParserType, FutureType, StructType, BuiltinClass, HTTPServerType)):
            handler = builtins.method_for(recv_type, name)
            codes = []
            for i, (pname, ptype, *default) in enumerate(handler.params):
                node = e.args[i] if i < len(e.args) else self.keyword(e, pname)
                want = handler.resolve(ptype, recv_type)
                if node is None:
                    codes.append(default[0])
                else:
                    code = self.expr_as(node, want) if isinstance(want, Type) else self.expr(node)
                    if isinstance(recv_type, SyncType) and (recv_type.kind, name) in (("Queue", "put"), ("Queue", "put_nowait"), ("Mutex", "set"), ("RWMutex", "set")):
                        code = self.sent(node, code)  # the receiving thread gets it: moved if we're done with it
                    codes.append(code)
            dot = "->" if isinstance(recv_type, StructType) else "."  # (a built-in exception: HTTPError.read())
            return f"{r}{dot}{name}({', '.join(codes)})"
        match recv_type:
            case VarTupleType(elem):
                return f"{r}.{name}({self.expr_as(e.args[0], elem)})"
            case TupleType():
                return f"sd::tuple_{'count' if name == 'count' else 'find'}({r}, {args[0]})"
            case ProcessType():
                return self.process_method(r, recv_type, name, e)
            case PatternType():
                return self.regex_op(r, name, e.regex_args, e)
            case MatchType():
                return self.match_method(r, name, e)
            case DequeType(elem):
                if name == "copy":
                    return f"sd::shallow_copy({r})"
                if name in ("append", "appendleft", "count", "index", "remove"):
                    return f"{r}.{name}({self.expr_as(e.args[0], elem)})"
                if name == "insert":
                    return f"{r}.insert({args[0]}, {self.expr_as(e.args[1], elem)})"
                return f"{r}.{name}({', '.join(args)})"
            case CounterType() if name in ("most_common", "elements", "total"):
                return f"{r}.{name}({', '.join(args)})"
            case CounterType() if name in ("update", "subtract"):
                how = "counts" if getattr(e, "counts", False) else "items"
                return f"{r}.{name}_{how}({args[0]})"
            case ListType():
                match name:
                    case "append":
                        return f"{r}.push_back({self.expr_as(e.args[0], recv_type.elem)})"
                    case "clear":
                        return f"{r}.clear()"
                    case "copy":
                        return f"sd::shallow_copy({r})"
                    case "sort":
                        rev = self.keyword(e, "reverse")
                        rev_code = self.expr(rev) if rev else "false"
                        if key := self.keyword(e, "key"):
                            return f"sd::list_sort_by({r}, {self.expr(key)}, {rev_code})"
                        return f"sd::list_sort({r}, {rev_code})"
                return f"sd::list_{name}({r}{rest})"
            case DictType(_, value):
                match name:
                    case "get" if len(args) == 1:
                        if isinstance(value, OptionalType):
                            return f"sd::dict_get_or({r}, {args[0]}, {self.cpp_type(value)}{{}})"
                        return f"sd::dict_get({r}, {args[0]})"
                    case "get":
                        return f"sd::dict_get_or({r}{rest})"
                    case "keys" | "values" | "items" | "clear":
                        return f"{r}.{name}()"
                    case "copy":
                        return f"sd::shallow_copy({r})"
                    case "update" if e.keywords or not e.args:  # d.update(other, key=value)
                        tmp = self.fresh("d")
                        steps = [f"sd::dict_update({tmp}{rest});"] if e.args else []
                        steps += [f"{tmp}[{cpp_string(kw.name)}] = {self.expr_as(kw.value, value)};" for kw in e.keywords]
                        return f"[&] {{ auto {tmp} = {r}; {' '.join(steps)} }}()"
                return f"sd::dict_{name}({r}{rest})"
            case SetType():
                match name:
                    case "add":
                        return f"{r}.insert({args[0]})"
                    case "discard":
                        return f"{r}.erase({args[0]})"
                    case "clear":
                        return f"{r}.clear()"
                    case "copy":
                        return f"sd::shallow_copy({r})"
                return f"sd::set_{name}({r}{rest})"  # (the rest take other collections: s.union(xs, ys))
        raise NotImplementedError(f"codegen for {recv_type}.{name}()")

    def function_call(self, member: builtins.Function, e: A.Call) -> str:
        """A built-in function with named parameters: each from a positional argument, a
        keyword, or its C++ default."""
        codes = []
        for i, (pname, ptype, *default) in enumerate(member.params or ()):
            node = e.args[i] if i < len(e.args) else self.keyword(e, pname)
            if node is None:
                codes.append(default[0])
            else:  # (a parameter taking "str or Path" converts in C++)
                codes.append(self.expr_as(node, ptype) if isinstance(ptype, Type) else self.expr(node))
        return f"{member.cpp}({', '.join(codes)})"

    def module_call(self, module: builtins.Module, name: str, e: A.Call) -> str:
        mod = module.name
        if mod == "re":
            return self.re_call(name, e)
        if mod == "subprocess" and hasattr(e, "process"):
            return self.process_call(e)
        if mod == "itertools":
            return self.itertools_call(name, e)
        if mod in ("heapq", "bisect"):
            return self.heapq_bisect_call(mod, name, e)
        if mod in ("hashlib", "hmac") and hasattr(e, "hash_args"):
            return self.hash_call(mod, name, e)
        if mod == "urllib.request" and name in ("urlopen", "Request"):
            args = e.url_args
            opt = lambda k, t: self.expr_as(args[k], OptionalType(t)) if k in args else "std::nullopt"
            if name == "Request":
                headers = self.expr(args["headers"]) if "headers" in args else "sd::dict<std::string, std::string>{}"
                return f"sd::urlrequest::Request({self.expr(args['url'])}, {opt('data', BYTES)}, {headers}, {opt('method', STR)})"
            ctx = self.expr_as(args["context"], OptionalType(builtins.SSL_CONTEXT)) if "context" in args else "std::nullopt"
            return f"sd::urlrequest::urlopen({self.expr(args['url'])}, {opt('data', BYTES)}, {opt('timeout', FLOAT)}, {ctx})"
        if mod == "os" and name == "fdopen":
            return self.open_call(self.expr(e.args[0]), e.args[1] if len(e.args) > 1 else self.keyword(e, "mode"), e)
        if mod == "copy" and name in ("copy", "deepcopy"):
            t = strip_optional(e.args[0].ty)
            if name == "copy" and isinstance(t, StructType) and strip_optional(e.ty) is not t:
                # an inherited __copy__ makes its own class's objects
                return f"sd::copymod::copy_as<{class_name(strip_optional(e.ty))}>({self.expr(e.args[0])})"
            if isinstance(t, FuncType):
                return self.expr(e.args[0])  # (a function is copied as itself, as in Python)
            return f"sd::copymod::{name}({self.expr(e.args[0])})"
        if mod in ("dataclasses", "copy") and name == "replace":  # a copy with some fields changed
            t: StructType = e.ty
            tmp = self.fresh("r")
            obj = self.expr(e.args[0])
            arrow = "->" if t.kind == "class" else "."
            fields = t.all_fields()
            sets = []
            for kw in e.keywords:
                value = self.expr_as(kw.value, fields[kw.name].type)
                if holds_references(fields[kw.name].type) and t.kind == "struct":
                    value = f"sd::value_copy({value})"  # (a @value class's lists are its own)
                sets.append(f"{tmp}{arrow}{ident(kw.name)} = {value};")
            copy = f"std::make_shared<{class_name(t)}>(*{obj})" if t.kind == "class" else obj
            return f"[&] {{ auto {tmp} = {copy}; {' '.join(sets)} return {tmp}; }}()"
        if mod == "urllib.parse" and name == "urlencode":
            doseq = e.args[1] if len(e.args) > 1 else self.keyword(e, "doseq")
            return f"sd::urlparse::urlencode({self.expr(e.args[0])}, {self.expr(doseq) if doseq is not None else 'false'})"
        if mod in builtins.COMPRESSED_OPEN_OPTIONS and name in ("open", "GzipFile", "BZ2File", "LZMAFile"):
            filename = e.args[0] if e.args else self.keyword(e, "filename")
            path = self.expr(filename) + (".str()" if filename.ty == PATH else "")
            mode = e.args[1] if len(e.args) > 1 else self.keyword(e, "mode")
            args = [path, self.expr(mode) if mode else '"rb"s']
            for i, (option, t, default) in enumerate(builtins.COMPRESSED_OPEN_OPTIONS[mod]):
                node = e.args[2] if i == 0 and len(e.args) > 2 else self.keyword(e, option)
                args.append(self.expr_as(node, t) if node is not None else default)
            if e.ty.binary:
                return f"sd::{mod}::open_binary({', '.join(args)})"
            encoding, newline = self.keyword(e, "encoding"), self.keyword(e, "newline")
            if encoding or newline:
                args.append(self.expr(encoding) if encoding else '"utf-8"s')
            if newline is not None and not isinstance(newline, A.NoneLit):
                args.append(f"std::optional<std::string>({self.expr(newline)})")
            return f"sd::{mod}::open_text({', '.join(args)})"
        if mod == "struct" and name in ("pack", "unpack", "unpack_from", "iter_unpack", "Struct"):
            if name == "Struct":
                return f"sd::structmod::Struct({self.expr(e.args[0])})"
            return self.struct_op(name, e, None)
        if mod == "csv" and name in ("reader", "writer", "DictReader", "DictWriter"):
            return self.csv_call(name, e)
        if mod == "logging" and hasattr(e, "log_call"):
            return self.log_call(e, "sd::logging::root_log")
        if mod == "logging" and name == "basicConfig":
            kw = {k.name: k.value for k in e.keywords}
            level = kw.get("level")
            level_t = "std::string" if level is not None and level.ty == STR else "std::int64_t"
            opt = lambda k, t=STR: self.expr_as(kw[k], OptionalType(t)) if k in kw else "std::nullopt"
            filename = kw.get("filename")
            fname = "std::nullopt" if filename is None else (
                f"std::optional<std::string>({self.expr(filename)}{'.str()' if filename.ty == PATH else ''})")
            parts = [
                f"std::optional<{level_t}>({self.expr(level)})" if level is not None else f"std::optional<{level_t}>()",
                opt("format"), opt("datefmt"), self.expr(kw["style"]) if "style" in kw else '"%"s', fname,
                self.expr(kw["filemode"]) if "filemode" in kw else '"a"s',
                self.expr(kw["stream"]) if "stream" in kw else "nullptr",
                opt("handlers", ListType(LOG_HANDLER)), self.expr(kw["force"]) if "force" in kw else "false", opt("encoding"),
            ]
            return f"sd::logging::basicConfig<{level_t}>({', '.join(parts)})"
        if mod == "concurrent.futures" and name in ("as_completed", "wait"):
            elem = self.cpp_type(e.ty.elem.elem if name == "as_completed" else e.ty.elts[0].elem.elem)
            timeout_node = e.args[1] if len(e.args) > 1 else self.keyword(e, "timeout")
            timeout = self.expr_as(timeout_node, OptionalType(FLOAT)) if timeout_node is not None else "std::nullopt"
            if name == "as_completed":
                return f"sd::futures::as_completed<{elem}>({self.expr(e.args[0])}, {timeout})"
            when = e.args[2] if len(e.args) > 2 else self.keyword(e, "return_when")
            return (f"sd::futures::wait<{elem}>({self.expr(e.args[0])}, {timeout}, "
                    f"{self.expr(when) if when is not None else chr(34) + 'ALL_COMPLETED' + chr(34) + 's'})")
        if mod == "functools" and name == "partial":
            return self.partial_code(e)
        if mod == "contextlib":
            return self.contextlib_call(name, e)
        if mod == "functools" and name == "reduce":
            items = self.expr(e.args[1])
            init = [self.expr_as(e.args[2], e.ty)] if len(e.args) == 3 else []
            return f"sd::reduce<{self.cpp_type(e.ty)}>({', '.join([self.expr(e.args[0]), items, *init])})"
        if mod == "functools" and name == "cmp_to_key":
            return f"sd::cmp_to_key<{self.cpp_type(e.ty.params[0])}>({self.expr(e.args[0])})"
        if mod == "http.server" and name in ("HTTPServer", "ThreadingHTTPServer"):
            args = e.http_args
            st: StructType = args["RequestHandlerClass"].sym  # made for each connection, from its fields' defaults
            fields = [self.expr_as(f.default, f.type) for f in st.all_fields().values()]
            ctor = [] if st.builtin else ["sd::init", *fields]
            base = "sd::httpserver::BaseHTTPRequestHandler"
            settings = getattr(e, "handler_settings", {})  # partial(Handler, directory=...): set on each handler
            captures = ", ".join(f"sd_s{i} = {self.expr_as(v, st.find_field(k).type)}" for i, (k, v) in enumerate(settings.items()))
            sets = " ".join(f"sd_h->{ident(k)} = sd::send(sd_s{i});" for i, k in enumerate(settings))
            make = (f"[{captures}] {{ auto sd_h = std::make_shared<{class_name(st)}>({', '.join(ctor)}); {sets} "
                    f"return std::static_pointer_cast<{base}>(sd_h); }}")
            bind = self.expr(args["bind_and_activate"]) if "bind_and_activate" in args else "true"
            address = self.expr_as(args["server_address"], TupleType((STR, INT)))
            return f"sd::httpserver::HTTPServer({address}, {make}, {bind}, {'true' if name == 'ThreadingHTTPServer' else 'false'})"
        member = module.members[name]
        if member.cpp is not None and "{T}" in member.cpp:  # the result type picks the template: json.loads
            member = builtins.Function(member.name, member.check, member.cpp.replace("{T}", self.cpp_type(e.ty)), member.params)
        if member.params is not None:
            # Named parameters: fill each in order from positional args, keywords, or its default.
            codes = []
            for i, (pname, ptype, *default) in enumerate(member.params):
                node = e.args[i] if i < len(e.args) else self.keyword(e, pname)
                if node is None:
                    codes.append(default[0])
                else:  # (a parameter taking "str or Path" converts in C++)
                    codes.append(self.expr_as(node, ptype) if isinstance(ptype, Type) else self.expr(node))
            return f"{member.cpp}({', '.join(codes)})"
        args = [self.expr(a) for a in e.args]
        if member.cpp is not None:
            return f"{member.cpp}({', '.join(args)})"
        doubles = [self.expr_as(a, FLOAT) for a in e.args]
        if mod == "math":
            if name in MATH_FLOAT_1 or name in ("atan2", "pow", "hypot"):
                return f"std::{name}({', '.join(doubles)})"
            if name in ("floor", "ceil", "trunc"):
                return f"static_cast<std::int64_t>(std::{name}({doubles[0]}))"
            if name in ("isnan", "isinf"):
                return f"std::{name}({doubles[0]})"
            if name == "log":
                return f"std::log({doubles[0]})" if len(args) == 1 else f"(std::log({doubles[0]}) / std::log({doubles[1]}))"
            if name == "gcd":
                return f"std::gcd({args[0]}, {args[1]})"
        if mod == "sys" and name == "exit":
            return f"throw sd::Exit{{static_cast<int>({args[0] if args else '0'})}}"
        raise NotImplementedError(f"codegen for {mod}.{name}()")


# ---- helpers ------------------------------------------------------------------


def unwrapped(code: str) -> str:
    """`(a == b)` without the parentheses around the whole of it, for `if (...)`: clang
    warns about `if ((a == b))`. `(a) == (b)` is left alone."""
    if not code.startswith("("):
        return code
    depth, quote, i = 0, "", 0
    while i < len(code):
        c = code[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return code[1:-1] if i == len(code) - 1 else code
        i += 1
    return code


def constant_int(e: A.Expr) -> int | None:
    match e:
        case A.IntLit(v):
            return v
        case A.UnaryOp("-", A.IntLit(v)):
            return -v
    return None


def is_static(fn: FuncInfo) -> bool:
    return fn.kind in ("static", "classmethod")


def is_synchronized(st: StructType) -> bool:
    return any(t.builtin and t.name == "Synchronized" for t in st.ancestors())


def holds_references(t: Type) -> bool:
    """Does a value of type t contain a list, dict or set (which a struct must copy, not share)?
    Structs inside copy themselves; class instances are always shared."""
    match t:
        case ListType() | SetType() | DictType() | DequeType() | CounterType() | DefaultDictType():
            return True
        case OptionalType(inner) | VarTupleType(inner):
            return holds_references(inner)
        case TupleType(elts):
            return any(holds_references(e) for e in elts)
    return False


def synchronized_copy(fn: FuncInfo | None, t: Type) -> bool:
    """A Synchronized class's methods copy the lists, dicts and sets they take and return,
    so its state is never shared with code that doesn't hold its lock."""
    return fn is not None and fn.owner is not None and is_synchronized(fn.owner) and holds_references(t)


def by_value(t: Type) -> bool:
    """Passed by value: small scalars, and classes (a shared pointer)."""
    return t in (INT, FLOAT, BOOL, SOCKET) or isinstance(t, (FuncType, SyncType)) or (isinstance(t, StructType) and t.kind == "class")


def attr_chain(e: A.Expr) -> list[str]:
    match e:
        case A.Name(name):
            return [name]
        case A.Attribute(value, attr):
            return attr_chain(value) + [attr]
    return ["..."]


def has_call(e: A.Expr) -> bool:
    """Could evaluating `e` have side effects (or observe them)?"""
    return any(isinstance(n, (A.Call, A.NamedExpr)) for n in walk_expr(e, into_lambdas=False))


def paren_all(tests: list[str]) -> str:
    return f"({' && '.join(tests)})" if tests else "true"


def fstring_values(parts: list) -> list[A.Expr]:
    """An f-string's expressions in evaluation order, including nested ones in specs."""
    values = []
    for p in parts:
        if isinstance(p, A.FormattedValue):
            values.append(p.value)
            if isinstance(p.spec, A.FString):
                values += fstring_values(p.spec.parts)
    return values


def walk_expr(e: A.Node, into_lambdas: bool = True):
    """Every node in an expression. Creating a lambda runs none of its body."""
    import dataclasses

    yield e
    if isinstance(e, A.Lambda) and not into_lambdas:
        return
    for f in dataclasses.fields(e):
        if f.name in ("loc", "sym", "ty", "dunder"):
            continue
        value = getattr(e, f.name)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, A.Node):
                yield from walk_expr(item, into_lambdas)


def is_self(e: A.Expr) -> bool:
    return isinstance(e, A.Name) and isinstance(e.sym, Var) and e.sym.name == "self" and e.sym.kind == "param"


def is_literal(e: A.Expr) -> bool:
    """A constant: evaluating it neither causes nor observes side effects."""
    return isinstance(e, (A.IntLit, A.FloatLit, A.StrLit, A.BytesLit, A.BoolLit, A.NoneLit, A.Lambda)) or (
        isinstance(e, A.UnaryOp) and isinstance(e.operand, (A.IntLit, A.FloatLit))
    )


def is_simple(e: A.Expr) -> bool:
    """Cheap and side-effect free, so it's fine to evaluate twice."""
    return isinstance(e, (A.Name, A.IntLit, A.FloatLit, A.StrLit, A.BoolLit, A.NoneLit))


def base_name(e: A.Expr) -> str | None:
    match e:
        case A.Name(name):
            return name
        case A.Attribute(value) | A.Index(value):
            return base_name(value)
    return None


def modified_names(body: list[A.Stmt]) -> set[str]:
    """Names that a function body assigns to or mutates through (xs.append, p.x = ...)."""
    from .checker import walk

    names: set[str] = set()
    for node in walk(body):
        targets: list[A.Expr] = []
        match node:
            case A.Assign(ts):
                targets = ts
            case A.AnnAssign(t) | A.AugAssign(t) | A.For(t) | A.NamedExpr(t):
                targets = [t]
            case A.Call(_, [first, *_]) if (
                isinstance(node.sym, CallTarget) and node.sym.kind == "module_func"
                and getattr(node.sym.target[0].members.get(node.sym.target[1]), "mutates_first_arg", False)
            ):
                targets = [first]  # random.shuffle(xs)
            case A.Call(A.Attribute(recv, attr)):
                target = node.sym
                if isinstance(target, CallTarget) and (
                    target.kind == "method" or (target.kind == "builtin_method" and attr in MUTATING_METHODS)
                ):
                    targets = [recv]
        for t in targets:
            for sub in walk(t):
                if (name := base_name(sub)) is not None:
                    names.add(name)
    return names


def ends_with_return(body: list[A.Stmt]) -> bool:
    return bool(body) and isinstance(body[-1], A.Return)
