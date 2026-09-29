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

from . import ast as A
from . import builtins
from .checker import CallTarget, ModuleInfo
from .types import (
    BOOL, FLOAT, INT, NONE, STR,
    DictType, FuncInfo, FuncType, IterType, ListType, OptionalType, SetType, StructType,
    TupleType, Type, Var, element_type, is_numeric,
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
    """.split()
)

# Built-in methods that modify their receiver (so a parameter used this way is passed by value).
MUTATING_METHODS = frozenset(
    "append insert pop remove extend sort reverse clear update setdefault add discard".split()
)

MATH_FLOAT_1 = {"sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "exp", "log2", "log10", "fabs"}
MATH_VALUES = {
    "pi": "std::numbers::pi",
    "e": "std::numbers::e",
    "tau": "(2 * std::numbers::pi)",
    "inf": "std::numeric_limits<double>::infinity()",
    "nan": "std::numeric_limits<double>::quiet_NaN()",
}


def generate(module: A.Module, info: ModuleInfo) -> str:
    return CodeGen(module, info).generate()


def ident(name: str) -> str:
    """A user identifier, made safe for C++."""
    if "__" in name:
        name = name.replace("__", "_u_")
    if name in CPP_KEYWORDS or name.startswith("sd_") or name.startswith("_"):
        return name + "_"
    return name


def class_name(st: StructType) -> str:
    """The C++ name of a struct/class; built-in exceptions live in the runtime."""
    return f"sd::{st.name}" if st.builtin else ident(st.name)


def cpp_string(s: str) -> str:
    """A C++ std::string literal ("..."s) holding the UTF-8 bytes of `s`."""
    out = []
    for b in s.encode("utf-8"):
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
    def __init__(self, module: A.Module, info: ModuleInfo):
        self.module = module
        self.info = info
        self.lines: list[str] = []
        self.depth = 0
        self.counter = 0
        self.func: FuncInfo | None = None
        # For `break` in loops with an else: [goto label, whether a break used it]
        self.loop_labels: list[list] = []
        # Inside a lambda that uses `self`, self is a captured copy named sd_self.
        self.lambda_self = 0
        # Expressions already evaluated into temporaries (see `in_order`).
        self.precomputed: dict[int, str] = {}
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
            case _ if t == NONE:
                return "void"
            case ListType(elem):
                return f"sd::list<{self.cpp_type(elem)}>"
            case SetType(elem):
                return f"std::set<{self.cpp_type(elem)}>"
            case DictType(key, value):
                return f"sd::dict<{self.cpp_type(key)}, {self.cpp_type(value)}>"
            case TupleType(elts):
                return f"std::tuple<{', '.join(self.cpp_type(e) for e in elts)}>"
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
        raise NotImplementedError(f"no C++ type for {t}")

    def coerce(self, code: str, src: Type, dst: Type) -> str:
        """Convert `code` (of type src) to dst: int -> float, T/None -> T?, tuples elementwise."""
        if src == dst or dst is None or isinstance(src, IterType):
            return code
        if src == NONE:
            return f"{self.cpp_type(dst)}{{}}"
        return f"static_cast<{self.cpp_type(dst)}>({code})"

    def expr_as(self, e: A.Expr, dst: Type) -> str:
        return self.coerce(self.expr(e), e.ty, dst)

    # =========================================================================
    # Program structure
    # =========================================================================

    def generate(self) -> str:
        self.line("// Generated by the seadash compiler. Do not edit.")
        self.line('#include "seadash.hpp"')
        self.line()
        self.line("namespace prog {")
        self.line()

        structs = self.ordered_structs()
        if structs:
            for st in structs:
                self.line(f"struct {ident(st.name)};")
            self.line()
        if self.info.functions:
            for fn in self.info.functions:
                self.line(self.signature(fn) + ";")
            self.line()
        for st in structs:
            self.struct_definition(st)
        if self.info.globals:
            for var in self.info.globals:
                self.line(f"{self.cpp_type(var.type)} {ident(var.cpp_name)}{{}};")
            self.line()
        for st in structs:
            self.struct_members(st)
        for fn in self.info.functions:
            self.function(fn)

        top_level = [s for s in self.module.body if not isinstance(s, (A.FunctionDef, A.ClassDef, A.Import, A.ImportFrom))]
        self.func = None
        self.open("void module_main()")
        self.hoist(self.info.main_locals)
        self.block(top_level)
        self.close()
        self.line()
        self.line("}  // namespace prog")
        self.line()
        self.line("int main(int argc, char** argv) { return sd::run_main(argc, argv, prog::module_main); }")
        return "\n".join(self.lines) + "\n"

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
        if st.is_exception:
            self.exception_definition(st)
            return
        name = ident(st.name)
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
            inits = ", ".join(f"{ident(f.name)}(std::move(sd_{f.name}))" for f in st.fields.values())
            self.line(f"{name}({params}) : {inits} {{}}")
        for m in st.methods.values():
            if m.name != "__init__":
                self.line(f"{self.cpp_type(m.ret)} {ident(m.name)}({', '.join(self.params(m))});")
        self.line("std::string sd_repr() const;")
        if st.kind == "struct":
            self.line(f"bool operator==(const {name}&) const = default;")
        self.close(";")
        self.line()

    def exception_definition(self, st: StructType) -> None:
        """`class NotFound(ValueError)` derives from the runtime's sd::ValueError."""
        name = ident(st.name)
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
        name = ident(st.name)
        if st.is_exception:
            for m in st.methods.values():
                if m.name == "__init__":
                    self.function_body(m, f"{name}::{name}({', '.join(['sd::init_t', *self.params(m)])})")
                else:
                    self.function_body(m, f"{self.cpp_type(m.ret)} {name}::{ident(m.name)}({', '.join(self.params(m))})")
            return
        fields = list(st.fields.values())
        parts = [cpp_string(f"{st.name}(")]
        for i, f in enumerate(fields):
            parts.append(cpp_string(("" if i == 0 else ", ") + f"{f.name}="))
            parts.append(f"sd::repr({ident(f.name)})")
        parts.append(cpp_string(")"))
        self.line(f"std::string {name}::sd_repr() const {{ return {' + '.join(parts)}; }}")
        self.line()
        for m in st.methods.values():
            if m.name == "__init__":
                params = ", ".join(["sd::init_t", *self.params(m)])
                self.function_body(m, f"{name}::{name}({params})")
            else:
                self.function_body(m, f"{self.cpp_type(m.ret)} {name}::{ident(m.name)}({', '.join(self.params(m))})")

    def signature(self, fn: FuncInfo) -> str:
        return f"{self.cpp_type(fn.ret)} {ident(fn.name)}({', '.join(self.params(fn))})"

    def params(self, fn: FuncInfo) -> list[str]:
        modified = modified_names(fn.node.body)
        out = []
        for p in fn.params:
            t = self.cpp_type(p.type)
            by_value = p.name in modified or p.type in (INT, FLOAT, BOOL) or (
                isinstance(p.type, StructType) and p.type.kind == "class"
            )
            out.append(f"{t} {ident(p.name)}" if by_value else f"const {t}& {ident(p.name)}")
        return out

    def function(self, fn: FuncInfo) -> None:
        self.function_body(fn, self.signature(fn))

    def function_body(self, fn: FuncInfo, header: str) -> None:
        self.func = fn
        self.open(header)
        self.hoist(fn.locals)
        self.block(fn.node.body)
        if isinstance(fn.ret, OptionalType) and not ends_with_return(fn.node.body):
            self.line("return std::nullopt;")
        self.close()
        self.line()

    def hoist(self, variables: list[Var]) -> None:
        for var in variables:
            if var.kind != "global":
                self.line(f"{self.cpp_type(var.type)} {ident(var.cpp_name)}{{}};")

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
            case A.Assert(test, msg):
                message = f"sd::str({self.expr(msg)})" if msg is not None else '""s'
                self.line(f'if (!{self.cond(test)}) sd::raise("AssertionError", {message});')
            case A.Raise(exc):
                if exc is None:
                    self.line("throw;")
                elif isinstance(s.sym, StructType):  # `raise ValueError`
                    self.line(f"throw sd::Thrown{{std::make_shared<{class_name(s.sym)}>()}};")
                else:
                    self.line(f"throw sd::Thrown{{{self.expr(exc)}}};")
            case A.Try():
                self.try_stmt(s)
            case A.If():
                self.if_stmt(s)
            case A.While(test, body, orelse):
                self.loop(f"while ({self.cond(test)})", body, orelse)
            case A.For(target, it, body, orelse):
                v = self.fresh("v")
                elem = element_type(it.ty)
                self.loop(
                    f"for (auto&& {v} : sd::iter({self.expr(it)}))", body, orelse,
                    prologue=lambda: self.assign(target, v, elem),
                )
            case _:
                raise NotImplementedError(f"codegen for {type(s).__name__}")

    def return_stmt(self, value: A.Expr | None) -> None:
        ret = self.func.ret if self.func else NONE
        if self.func is not None and self.func.name == "__init__":
            self.line("return;")
        elif value is None:
            self.line("return std::nullopt;" if isinstance(ret, OptionalType) else "return;")
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
                self.line(f"{ident(var.cpp_name)} = std::dynamic_pointer_cast<{class_name(var.type)}>({caught}.exc);")
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
        self.open(f"if ({self.cond(s.test)})")
        self.block(s.body)
        orelse = s.orelse
        while len(orelse) == 1 and isinstance(orelse[0], A.If):
            self.depth -= 1
            self.line(f"}} else if ({self.cond(orelse[0].test)}) {{")
            self.depth += 1
            self.block(orelse[0].body)
            orelse = orelse[0].orelse
        if orelse:
            self.depth -= 1
            self.line("} else {")
            self.depth += 1
            self.block(orelse)
        self.close()

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
                self.line(f"{ident(var.cpp_name)} = {self.coerce(code, ty, var.type)};")
            case A.Attribute():
                self.line(f"{self.attribute(target)} = {self.coerce(code, ty, target.ty)};")
            case A.Index(container, index):
                c = self.expr(container)
                if isinstance(container.ty, DictType):
                    key = self.expr_as(index, container.ty.key)
                    self.line(f"{c}[{key}] = {self.coerce(code, ty, target.ty)};")
                else:
                    self.line(f"sd::index({c}, {self.expr(index)}) = {self.coerce(code, ty, target.ty)};")
            case A.TupleLit(elts) | A.ListLit(elts):
                tmp = self.fresh("t")
                self.open("")
                self.line(f"auto {tmp} = {code};")
                for i, elt in enumerate(elts):
                    self.assign(elt, f"std::get<{i}>({tmp})", ty.elts[i])
                self.close()

    def aug_assign(self, s: A.AugAssign, target: A.Expr, op: str, value: A.Expr) -> None:
        read_var, read_type, result = s.sym
        if isinstance(target, A.Name):
            write: Var = target.sym
            if op == "+" and isinstance(read_type, ListType) and write is read_var:
                self.line(f"sd::list_extend({ident(write.cpp_name)}, {self.expr(value)});")
                return
            current = self.var_code(read_var, read_type)
            if write is read_var and read_type == result == write.type == value.ty and (
                (op in ("+", "-", "*") and is_numeric(result)) or (op == "+" and result == STR)
            ):
                self.line(f"{ident(write.cpp_name)} {op}= {self.expr(value)};")  # the readable form
                return
            new = self.binop_code(op, current, read_type, self.expr(value), value.ty, result)
            self.line(f"{ident(write.cpp_name)} = {self.coerce(new, result, write.type)};")
            return
        ref = self.fresh("ref")
        lvalue = self.attribute(target) if isinstance(target, A.Attribute) else self.index(target)
        self.open("")
        self.line(f"auto& {ref} = {lvalue};")
        new = self.binop_code(op, ref, read_type, self.expr(value), value.ty, result)
        self.line(f"{ref} = {self.coerce(new, result, read_type)};")
        self.close()

    # =========================================================================
    # Expressions
    # =========================================================================

    def cond(self, e: A.Expr) -> str:
        """`e` as a C++ bool, applying Python truthiness."""
        code = self.expr(e)
        return code if e.ty == BOOL else f"sd::truthy({code})"

    def in_order(self, operands: list[A.Expr], build) -> str:
        """Evaluate operands left to right, as Python does.

        C++ leaves the order of function arguments and of most binary
        operands unspecified, so `print(f(), g())` might call g first. When at
        least two operands could have side effects, evaluate them into
        temporaries in source order inside a lambda, then build the expression
        from those. `build` is called with no arguments and uses self.expr as
        usual; the temporaries are substituted transparently.
        """
        if sum(has_call(x) for x in operands) < 2:
            return build()
        decls = []
        for x in operands:
            tmp = self.fresh("a")
            decls.append(f"auto&& {tmp} = {self.expr(x)};")
            self.precomputed[id(x)] = tmp
        try:
            inner = build()
        finally:
            for x in operands:
                del self.precomputed[id(x)]
        return f"[&]() {{ {' '.join(decls)} return {inner}; }}()"

    def expr(self, e: A.Expr) -> str:
        if id(e) in self.precomputed:
            return self.precomputed[id(e)]
        match e:
            case A.IntLit(v):
                return f"{v}_i"
            case A.FloatLit(v):
                return float_literal(v)
            case A.StrLit(v):
                return cpp_string(v)
            case A.BoolLit(v):
                return "true" if v else "false"
            case A.NoneLit():
                return "std::nullopt"
            case A.FString(parts):
                values = [p.value for p in parts if isinstance(p, A.FormattedValue)]
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
            case A.TupleLit(elts):
                args = ", ".join(self.expr_as(x, t) for x, t in zip(elts, e.ty.elts))
                return f"{self.cpp_type(e.ty)}{{{args}}}"  # braces: evaluated left to right
            case A.ListComp() | A.SetComp() | A.DictComp() | A.GeneratorExp():
                return self.comprehension(e)
            case A.UnaryOp("not", operand):
                return f"(!{self.cond(operand)})"
            case A.UnaryOp(op, operand):
                return f"({op}{self.expr(operand)})"
            case A.BinOp(op, left, right):
                return self.in_order(
                    [left, right],
                    lambda: self.binop_code(op, self.expr(left), left.ty, self.expr(right), right.ty, e.ty),
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
                code = f"({ident(var.cpp_name)} = {self.expr_as(value, var.type)})"
                return self.view(code, var.type, e.ty)
            case A.Call():
                return self.call(e)
            case A.Attribute():
                return self.attribute(e)
            case A.Index():
                return self.index(e)
        raise NotImplementedError(f"codegen for {type(e).__name__}")

    def view(self, code: str, declared: Type, seen: Type) -> str:
        """Unwrap an optional that the checker has narrowed to its inner type."""
        if isinstance(declared, OptionalType) and not isinstance(seen, OptionalType) and seen != NONE:
            return f"(*{code})"
        return code

    def var_code(self, var: Var, seen: Type) -> str:
        if var.name == "self" and var.kind == "param":
            if self.lambda_self:
                return "sd_self"
            if var.type.kind == "struct":
                return "(*this)"
            if var.type.is_exception:  # shared_from_this() gives the BaseException pointer
                return f"std::static_pointer_cast<{class_name(var.type)}>(this->shared_from_this())"
            return "this->shared_from_this()"
        return self.view(ident(var.cpp_name), var.type, seen)

    def name(self, e: A.Name) -> str:
        sym = e.sym
        if isinstance(sym, Var):
            return self.var_code(sym, e.ty)
        if isinstance(sym, FuncInfo):
            return ident(sym.name)  # a function used as a value
        if isinstance(sym, A.Lambda):
            return self.expr(sym)  # `key=len` was wrapped as `lambda p: len(p)`
        if isinstance(sym, builtins.Value):
            if sym.name == "__name__":
                return '"__main__"s'
            return self.module_value(sym)
        raise NotImplementedError(f"codegen for name {e.id}")

    def module_value(self, v: builtins.Value) -> str:
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
        params = ", ".join(f"{self.cpp_type(pt)} {ident(p.sym.cpp_name)}" for p, pt in zip(e.params, t.params))
        capture = "[=]"
        uses_self = any(isinstance(n, A.Name) and is_self(n) for n in walk_expr(e.body))
        if uses_self:
            self_var = next(n.sym for n in walk_expr(e.body) if isinstance(n, A.Name) and is_self(n))
            capture = f"[=, sd_self = {self.var_code(self_var, self_var.type)}]"
            self.lambda_self += 1
        try:
            body = self.expr_as(e.body, t.ret)
        finally:
            if uses_self:
                self.lambda_self -= 1
        if t.ret == NONE:  # `lambda: print(x)` or `lambda s: None`: evaluate for effect only
            statement = "" if isinstance(e.body, A.NoneLit) else f" {body};"
            return f"{capture}({params}) mutable -> void {{{statement} }}"
        return f"{capture}({params}) mutable -> {self.cpp_type(t.ret)} {{ return {body}; }}"

    def bound_method(self, obj: A.Expr, m: FuncInfo) -> str:
        """`counter.tick` as a value: a lambda holding (a copy of / reference to) the object."""
        names = [self.fresh("p") for _ in m.params]
        params = ", ".join(f"{self.cpp_type(p.type)} {n}" for p, n in zip(m.params, names))
        arrow = "->" if m.owner.kind == "class" else "."
        call = f"sd_o{arrow}{ident(m.name)}({', '.join(names)})"
        return f"[sd_o = {self.expr(obj)}]({params}) mutable -> {self.cpp_type(m.ret)} {{ return {call}; }}"

    def fstring(self, parts: list) -> str:
        pieces = []
        for p in parts:
            if isinstance(p, str):
                pieces.append(cpp_string(p))
            elif p.spec is not None:
                fmt = cpp_string("{:" + p.spec + "}")[:-1]  # a plain "..." literal, as std::format wants
                pieces.append(f"std::format({fmt}, {self.expr(p.value)})")
            else:
                pieces.append(f"sd::str({self.expr(p.value)})")
        return "(" + " + ".join(pieces) + ")" if len(pieces) > 1 else pieces[0]

    def attribute(self, e: A.Attribute) -> str:
        if isinstance(e.sym, builtins.Value):
            return self.module_value(e.sym)
        if isinstance(e.sym, A.Lambda):
            return self.expr(e.sym)  # `key=str.lower`
        if isinstance(e.sym, FuncInfo):
            return self.bound_method(e.value, e.sym)
        obj = e.value
        field = ident(e.attr)
        if is_self(obj):
            return f"{self.self_prefix(obj.sym.type)}{field}"
        arrow = "->" if isinstance(obj.ty, StructType) and obj.ty.kind == "class" else "."
        return f"{self.expr(obj)}{arrow}{field}"

    def index(self, e: A.Index) -> str:
        v = self.expr(e.value)
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
        return f"sd::index({v}, {self.expr(idx)})"

    def binop_code(self, op: str, lc: str, lt: Type, rc: str, rt: Type, t: Type) -> str:
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
        if len(e.ops) == 1:
            return self.in_order(operands, lambda: self.comparison(e.ops[0], self.expr(e.left), e.left, self.expr(e.comparators[0]), e.comparators[0]))
        if all(is_simple(x) for x in operands[1:-1]):
            parts = [
                self.comparison(op, self.expr(l), l, self.expr(r), r)
                for op, l, r in zip(e.ops, operands, operands[1:])
            ]
            return parts[0] if len(parts) == 1 else "(" + " && ".join(parts) + ")"
        # a < f() < c: evaluate each middle operand once, and stop early like Python.
        temps = [self.fresh("c") for _ in operands]
        body = [f"auto {temps[0]} = {self.expr(operands[0])};"]
        for i, op in enumerate(e.ops):
            body.append(f"auto {temps[i + 1]} = {self.expr(operands[i + 1])};")
            test = self.comparison(op, temps[i], operands[i], temps[i + 1], operands[i + 1])
            body.append(f"if (!{test}) return false;")
        return "[&]() -> bool { " + " ".join(body) + " return true; }()"

    def comparison(self, op: str, lc: str, left: A.Expr, rc: str, right: A.Expr) -> str:
        if isinstance(right, A.NoneLit) or isinstance(left, A.NoneLit):
            subject = lc if isinstance(right, A.NoneLit) else rc
            is_none = op in ("is", "==")
            return f"(!{subject}.has_value())" if is_none else f"{subject}.has_value()"
        match op:
            case "in":
                return f"sd::contains({rc}, {lc})"
            case "not in":
                return f"(!sd::contains({rc}, {lc}))"
            case "is":
                return f"({lc} == {rc})"
            case "is not":
                return f"({lc} != {rc})"
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

    def bind_comprehension(self, target: A.Expr, source: str) -> list[str]:
        if isinstance(target, A.Name):
            var: Var = target.sym
            return [f"{self.cpp_type(var.type)} {ident(var.cpp_name)} = {source};"]
        tmp = self.fresh("t")
        out = [f"auto&& {tmp} = {source};"]
        for i, elt in enumerate(target.elts):
            out.extend(self.bind_comprehension(elt, f"std::get<{i}>({tmp})"))
        return out

    # =========================================================================
    # Calls
    # =========================================================================

    def call(self, e: A.Call) -> str:
        return self.in_order([*e.args, *(k.value for k in e.keywords)], lambda: self.call_inner(e))

    def call_inner(self, e: A.Call) -> str:
        target: CallTarget = e.sym
        match target.kind:
            case "func":
                fn: FuncInfo = target.target
                return f"{ident(fn.name)}({self.call_args(target.args, fn)})"
            case "method":
                fn = target.target
                recv = e.func.value
                args = self.call_args(target.args, fn)
                if is_self(recv):
                    return f"{self.self_prefix(recv.sym.type)}{ident(fn.name)}({args})"
                arrow = "->" if fn.owner.kind == "class" else "."
                return f"{self.expr(recv)}{arrow}{ident(fn.name)}({args})"
            case "ctor":
                st: StructType = target.target
                args = self.slot_codes(target.args, target.params)
                if st.init is not None:
                    args = ["sd::init", *args]
                if st.kind == "class":
                    return f"std::make_shared<{class_name(st)}>({', '.join(args)})"
                return f"{class_name(st)}({', '.join(args)})"
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
                return self.module_call(mod.name, name, e)
        raise NotImplementedError(f"codegen for call kind {target.kind}")

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
                return f"sd::print({', '.join(head + args)})"
            case "len":
                return f"sd::len({a})"
            case "str":
                return f"sd::str({a})" if a else '""s'
            case "repr":
                return f"sd::repr({a})"
            case "int":
                return f"sd::to_int({a or ''})"
            case "float":
                return f"sd::to_float({a or ''})"
            case "bool":
                return self.cond(e.args[0]) if a else "false"
            case "range":
                return f"sd::range({', '.join(args)})"
            case "abs":
                return f"sd::abs({a})"
            case "min" | "max":
                if key := self.keyword(e, "key"):
                    return f"sd::{name}_by({a}, {self.expr(key)})"
                if len(args) == 1:
                    return f"sd::{name}_of({a})"
                values = ", ".join(self.expr_as(x, e.ty) for x in e.args)
                return f"std::{name}({{{values}}})"
            case "sum" | "any" | "all" | "reversed" | "zip" | "ord" | "chr":
                return f"sd::{name}({', '.join(args)})"
            case "sorted":
                rev = self.keyword(e, "reverse")
                rev_code = self.expr(rev) if rev else "false"
                if key := self.keyword(e, "key"):
                    return f"sd::sorted_by({a}, {self.expr(key)}, {rev_code})"
                return f"sd::sorted({a}, {rev_code})"
            case "map" | "filter":
                return f"sd::{name}({args[0]}, {args[1]})"
            case "enumerate":
                return f"sd::enumerate({', '.join(args)})"
            case "list":
                return f"sd::to_list({a})" if a else f"{self.cpp_type(e.ty)}{{}}"
            case "set":
                return f"sd::to_set({a})" if a else f"{self.cpp_type(e.ty)}{{}}"
            case "dict":
                return f"{self.cpp_type(e.ty)}{{}}"
            case "input":
                return f"sd::input({a or ''})"
            case "round":
                return f"sd::round({', '.join(args)})"
        raise NotImplementedError(f"codegen for builtin {name}()")

    def method_call(self, recv_type: Type, name: str, e: A.Call) -> str:
        r = self.expr(e.func.value)
        args = [self.expr(a) for a in e.args]
        rest = "".join(", " + a for a in args)
        if recv_type == STR:
            return f"sd::str_{name}({r}{rest})"
        match recv_type:
            case ListType():
                match name:
                    case "append":
                        return f"{r}.push_back({self.expr_as(e.args[0], recv_type.elem)})"
                    case "clear":
                        return f"{r}.clear()"
                    case "copy":
                        return f"{self.cpp_type(recv_type)}({r})"
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
                        return f"{self.cpp_type(recv_type)}({r})"
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
                        return f"{self.cpp_type(recv_type)}({r})"
                    case "remove":
                        return f"sd::set_remove({r}{rest})"
                    case "union":
                        return f"sd::set_or({r}{rest})"
                    case "intersection":
                        return f"sd::set_and({r}{rest})"
                    case "difference":
                        return f"sd::set_sub({r}{rest})"
                    case "issubset":
                        return f"sd::set_issubset({r}{rest})"
                    case "issuperset":
                        return f"sd::set_issubset({args[0]}, {r})"
        raise NotImplementedError(f"codegen for {recv_type}.{name}()")

    def module_call(self, mod: str, name: str, e: A.Call) -> str:
        args = [self.expr(a) for a in e.args]
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


def constant_int(e: A.Expr) -> int | None:
    match e:
        case A.IntLit(v):
            return v
        case A.UnaryOp("-", A.IntLit(v)):
            return -v
    return None


def has_call(e: A.Expr) -> bool:
    """Could evaluating `e` have side effects (or observe them)?"""
    return any(isinstance(n, (A.Call, A.NamedExpr)) for n in walk_expr(e, into_lambdas=False))


def walk_expr(e: A.Node, into_lambdas: bool = True):
    """Every node in an expression. Creating a lambda runs none of its body."""
    import dataclasses

    yield e
    if isinstance(e, A.Lambda) and not into_lambdas:
        return
    for f in dataclasses.fields(e):
        if f.name in ("loc", "sym", "ty"):
            continue
        value = getattr(e, f.name)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, A.Node):
                yield from walk_expr(item, into_lambdas)


def is_self(e: A.Expr) -> bool:
    return isinstance(e, A.Name) and isinstance(e.sym, Var) and e.sym.name == "self" and e.sym.kind == "param"


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
