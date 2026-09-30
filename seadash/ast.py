"""Syntax tree node definitions.

Nodes are plain dataclasses. Every node has a `loc` (keyword-only, ignored by
==) so tests can build expected trees without caring about positions.

The shapes deliberately mirror Python's own `ast` module where that makes
sense (If.orelse holds elifs, Compare holds a chain, etc.), with a separate
small tree for type annotations, since seadash types (`int?`, `list[int]`)
are not ordinary expressions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Loc

NOLOC = Loc(0, 0)


@dataclass
class Node:
    loc: Loc = field(default=NOLOC, compare=False, repr=False, kw_only=True)
    # Filled in by the checker: what a name/call/attribute/definition resolved
    # to (a types.Var, types.FuncInfo, types.StructType, builtin tag, ...).
    sym: object = field(default=None, compare=False, repr=False, kw_only=True)
    # Filled in by the checker when an operator/protocol resolves to a dunder method on a
    # user type: a checker.Dunder (or a list of them, one per operator in a comparison chain).
    dunder: object = field(default=None, compare=False, repr=False, kw_only=True)


# ---- types ------------------------------------------------------------------


@dataclass
class TypeExpr(Node):
    pass


@dataclass
class TypeName(TypeExpr):
    """`int`, `list[int]`, `dict[str, int]`, `mod.Thing`."""

    name: str
    args: list[TypeExpr] = field(default_factory=list)

    def __str__(self) -> str:
        if self.args:
            return f"{self.name}[{', '.join(map(str, self.args))}]"
        return self.name


@dataclass
class OptionalType(TypeExpr):
    """`int?`"""

    inner: TypeExpr

    def __str__(self) -> str:
        inner = f"({self.inner})" if isinstance(self.inner, (FuncTypeExpr, UnionType)) else str(self.inner)
        return f"{inner}?"


@dataclass
class UnionType(TypeExpr):
    """`int | str`"""

    options: list[TypeExpr]

    def __str__(self) -> str:
        return " | ".join(map(str, self.options))


@dataclass
class FuncTypeExpr(TypeExpr):
    """`(int, str) -> bool`, or Python's `Callable[[int, str], bool]`."""

    params: list[TypeExpr]
    ret: TypeExpr

    def __str__(self) -> str:
        return f"({', '.join(map(str, self.params))}) -> {self.ret}"


# ---- expressions ------------------------------------------------------------


@dataclass
class Expr(Node):
    # Filled in by the checker: this expression's types.Type.
    ty: object = field(default=None, compare=False, repr=False, kw_only=True)


@dataclass
class IntLit(Expr):
    value: int


@dataclass
class FloatLit(Expr):
    value: float


@dataclass
class StrLit(Expr):
    value: str


@dataclass
class BytesLit(Expr):
    value: bytes


@dataclass
class BoolLit(Expr):
    value: bool


@dataclass
class NoneLit(Expr):
    pass


@dataclass
class FormattedValue(Node):
    """One `{expr:spec}` hole in an f-string."""

    value: Expr
    spec: str | None = None


@dataclass
class FString(Expr):
    parts: list[str | FormattedValue]


@dataclass
class Name(Expr):
    id: str


@dataclass
class ListLit(Expr):
    elts: list[Expr]


@dataclass
class TupleLit(Expr):
    elts: list[Expr]


@dataclass
class SetLit(Expr):
    elts: list[Expr]


@dataclass
class DictLit(Expr):
    keys: list[Expr]
    values: list[Expr]


@dataclass
class Comprehension(Node):
    """One `for target in iter if cond...` clause."""

    target: Expr
    iter: Expr
    ifs: list[Expr] = field(default_factory=list)


@dataclass
class ListComp(Expr):
    elt: Expr
    generators: list[Comprehension]


@dataclass
class SetComp(Expr):
    elt: Expr
    generators: list[Comprehension]


@dataclass
class DictComp(Expr):
    key: Expr
    value: Expr
    generators: list[Comprehension]


@dataclass
class GeneratorExp(Expr):
    elt: Expr
    generators: list[Comprehension]


@dataclass
class UnaryOp(Expr):
    op: str  # '-', '+', '~', 'not'
    operand: Expr


@dataclass
class BinOp(Expr):
    op: str  # '+', '-', '*', '/', '//', '%', '**', '@', '<<', '>>', '&', '|', '^'
    left: Expr
    right: Expr


@dataclass
class BoolOp(Expr):
    op: str  # 'and', 'or'
    left: Expr
    right: Expr


@dataclass
class Compare(Expr):
    """`a < b <= c` is one Compare: left=a, ops=['<', '<='], comparators=[b, c]."""

    left: Expr
    ops: list[str]  # '<', '>', '==', '!=', '<=', '>=', 'in', 'not in', 'is', 'is not'
    comparators: list[Expr]


@dataclass
class IfExp(Expr):
    """`body if test else orelse`"""

    test: Expr
    body: Expr
    orelse: Expr


@dataclass
class NamedExpr(Expr):
    """`target := value`"""

    target: Name
    value: Expr


@dataclass
class Lambda(Expr):
    """`lambda x, y: body`. Parameter types come from the expected function type."""

    params: list[Param]
    body: Expr


@dataclass
class Keyword(Node):
    name: str
    value: Expr


@dataclass
class Call(Expr):
    func: Expr
    args: list[Expr] = field(default_factory=list)
    keywords: list[Keyword] = field(default_factory=list)


@dataclass
class Attribute(Expr):
    value: Expr
    attr: str


@dataclass
class Slice(Expr):
    """`lower:upper:step` inside brackets; any part may be missing."""

    lower: Expr | None = None
    upper: Expr | None = None
    step: Expr | None = None


@dataclass
class Index(Expr):
    """`value[index]`; index may be a Slice, or a TupleLit for `m[i, j]`."""

    value: Expr
    index: Expr


# ---- statements -------------------------------------------------------------


@dataclass
class Stmt(Node):
    pass


@dataclass
class ExprStmt(Stmt):
    value: Expr


@dataclass
class Assign(Stmt):
    """`a = b = value` has targets [a, b]."""

    targets: list[Expr]
    value: Expr


@dataclass
class AnnAssign(Stmt):
    """`x: int = 1`, or just `x: int` (value None), e.g. a struct field."""

    target: Expr
    annotation: TypeExpr
    value: Expr | None = None


@dataclass
class AugAssign(Stmt):
    target: Expr
    op: str  # the binary op, e.g. '+' for '+='
    value: Expr


@dataclass
class Pass(Stmt):
    pass


@dataclass
class Break(Stmt):
    pass


@dataclass
class Continue(Stmt):
    pass


@dataclass
class Return(Stmt):
    value: Expr | None = None


@dataclass
class Nonlocal(Stmt):
    """`nonlocal a, b`: assignments in this nested function go to the enclosing function's variables."""

    names: list[str]


@dataclass
class Global(Stmt):
    """`global a`: assignments in this function go to the module-level variable."""

    names: list[str]


@dataclass
class Assert(Stmt):
    test: Expr
    msg: Expr | None = None


@dataclass
class If(Stmt):
    """`elif` is an If nested as the only statement in `orelse`."""

    test: Expr
    body: list[Stmt]
    orelse: list[Stmt] = field(default_factory=list)


@dataclass
class While(Stmt):
    test: Expr
    body: list[Stmt]
    orelse: list[Stmt] = field(default_factory=list)


@dataclass
class For(Stmt):
    target: Expr
    iter: Expr
    body: list[Stmt]
    orelse: list[Stmt] = field(default_factory=list)


@dataclass
class Raise(Stmt):
    """`raise exc from cause`; a bare `raise` (exc None) re-raises inside an except block."""

    exc: Expr | None = None
    cause: Expr | None = None


@dataclass
class ExceptHandler(Node):
    """`except type as name:`. type None is a bare `except:`."""

    type: Expr | None
    name: Name | None
    body: list[Stmt]


@dataclass
class Try(Stmt):
    body: list[Stmt]
    handlers: list[ExceptHandler]
    orelse: list[Stmt] = field(default_factory=list)
    finalbody: list[Stmt] = field(default_factory=list)


@dataclass
class WithItem(Node):
    """`context as target` in a with statement (target may be None)."""

    context: Expr
    target: Expr | None = None


@dataclass
class With(Stmt):
    items: list[WithItem]
    body: list[Stmt]


@dataclass
class Param(Node):
    name: str
    annotation: TypeExpr | None = None
    default: Expr | None = None


@dataclass
class FunctionDef(Stmt):
    name: str
    params: list[Param]
    returns: TypeExpr | None
    body: list[Stmt]
    type_params: list[str] = field(default_factory=list)
    decorators: list[Expr] = field(default_factory=list)


@dataclass
class ClassDef(Stmt):
    """`class` (shared reference semantics) or `struct` (value semantics)."""

    kind: str  # 'class' or 'struct'
    name: str
    bases: list[TypeExpr]
    body: list[Stmt]
    type_params: list[str] = field(default_factory=list)
    decorators: list[Expr] = field(default_factory=list)


@dataclass
class Alias(Node):
    name: str  # dotted, e.g. 'os.path'
    asname: str | None = None


@dataclass
class Import(Stmt):
    names: list[Alias]


@dataclass
class ImportFrom(Stmt):
    module: str
    names: list[Alias]


@dataclass
class Module(Node):
    body: list[Stmt]
