"""Semantic types, as computed by the checker (distinct from the syntax in ast.TypeExpr).

Value types (int, list[int], ...) are frozen dataclasses, so two separately
built `list[int]`s compare and hash equal. User-defined structs/classes are
nominal: each declaration is one StructType object, compared by identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Loc


class Type:
    pass


@dataclass(frozen=True)
class Prim(Type):
    name: str  # 'int', 'float', 'bool', 'str', 'None'

    def __str__(self) -> str:
        return self.name


INT = Prim("int")
FLOAT = Prim("float")
BOOL = Prim("bool")
STR = Prim("str")
NONE = Prim("None")

PRIMITIVES = {"int": INT, "float": FLOAT, "bool": BOOL, "str": STR, "None": NONE}


@dataclass(frozen=True)
class ListType(Type):
    elem: Type

    def __str__(self) -> str:
        return f"list[{self.elem}]"


@dataclass(frozen=True)
class SetType(Type):
    elem: Type

    def __str__(self) -> str:
        return f"set[{self.elem}]"


@dataclass(frozen=True)
class DictType(Type):
    key: Type
    value: Type

    def __str__(self) -> str:
        return f"dict[{self.key}, {self.value}]"


@dataclass(frozen=True)
class TupleType(Type):
    elts: tuple[Type, ...]

    def __str__(self) -> str:
        return f"tuple[{', '.join(map(str, self.elts))}]"


@dataclass(frozen=True)
class OptionalType(Type):
    inner: Type

    def __str__(self) -> str:
        return f"{self.inner}?"


@dataclass(frozen=True)
class IterType(Type):
    """Something you can only loop over: range(), enumerate(), dict.items(), zip()..."""

    elem: Type
    kind: str  # 'range', 'enumerate', 'items', 'keys', 'values', 'zip', 'reversed'

    def __str__(self) -> str:
        return f"{self.kind}[{self.elem}]"


@dataclass(frozen=True)
class ModuleType(Type):
    name: str

    def __str__(self) -> str:
        return f"module '{self.name}'"


@dataclass(eq=False)
class Field:
    name: str
    type: Type
    default: object | None  # ast.Expr
    loc: Loc


@dataclass(eq=False)
class Param:
    name: str
    type: Type
    default: object | None  # ast.Expr
    loc: Loc


@dataclass(eq=False)
class FuncInfo:
    """A function or method signature, plus what the checker learns about its body."""

    name: str
    params: list[Param]  # excludes `self` for methods
    ret: Type
    node: object  # ast.FunctionDef
    owner: StructType | None = None
    locals: list[Var] = field(default_factory=list)  # every local variable, for hoisting

    def __str__(self) -> str:
        params = ", ".join(f"{p.name}: {p.type}" for p in self.params)
        return f"def {self.name}({params}) -> {self.ret}"


@dataclass(eq=False)
class StructType(Type):
    """A user `struct` (value semantics) or `class` (shared reference semantics)."""

    name: str
    kind: str  # 'struct' or 'class'
    node: object  # ast.ClassDef
    fields: dict[str, Field] = field(default_factory=dict)
    methods: dict[str, FuncInfo] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.name

    @property
    def init(self) -> FuncInfo | None:
        return self.methods.get("__init__")


@dataclass(eq=False)
class Var:
    """One C++ variable. A source name rebound to a new type gets a new Var."""

    name: str  # the source name
    cpp_name: str  # unique within its function, e.g. 'a', 'a_1'
    type: Type
    kind: str  # 'local', 'param', 'global', 'comp' (comprehension loop variable)
    loc: Loc

    def __repr__(self) -> str:
        return f"Var({self.cpp_name}: {self.type})"


# ---- relations --------------------------------------------------------------


def is_numeric(t: Type) -> bool:
    return t in (INT, FLOAT)


def is_hashable(t: Type) -> bool:
    if t in (INT, FLOAT, BOOL, STR):
        return True
    if isinstance(t, TupleType):
        return all(is_hashable(e) for e in t.elts)
    return False


def assignable(src: Type, dst: Type) -> bool:
    """Can a value of type `src` be stored where `dst` is expected?

    Implicit conversions are deliberately few: int -> float, and T / None -> T?.
    Containers are invariant (a list[int] is not a list[float]).
    """
    if src == dst:
        return True
    if src == INT and dst == FLOAT:
        return True
    if isinstance(dst, OptionalType):
        if src == NONE:
            return True
        if isinstance(src, OptionalType):
            return assignable(src.inner, dst.inner)
        return assignable(src, dst.inner)
    if isinstance(dst, TupleType) and isinstance(src, TupleType) and len(src.elts) == len(dst.elts):
        return all(assignable(s, d) for s, d in zip(src.elts, dst.elts))
    return False


def join(a: Type, b: Type) -> Type | None:
    """The type that can hold both `a` and `b` (for list literals, if-expressions...), or None."""
    if a == b:
        return a
    if {a, b} == {INT, FLOAT}:
        return FLOAT
    if a == NONE:
        return b if isinstance(b, OptionalType) else OptionalType(b)
    if b == NONE:
        return a if isinstance(a, OptionalType) else OptionalType(a)
    if isinstance(a, OptionalType) or isinstance(b, OptionalType):
        inner = join(strip_optional(a), strip_optional(b))
        return OptionalType(inner) if inner else None
    return None


def strip_optional(t: Type) -> Type:
    return t.inner if isinstance(t, OptionalType) else t


def element_type(t: Type) -> Type | None:
    """What `for x in t` gives you, or None if `t` isn't iterable."""
    match t:
        case ListType(elem) | SetType(elem) | IterType(elem):
            return elem
        case DictType(key):
            return key
        case Prim("str"):
            return STR
    return None
