"""Built-in functions, methods on built-in types, and built-in modules.

Each built-in is a small handler that receives a CallContext, checks the
arguments, and returns the result type. Keeping them as a table means adding
a built-in is one function here plus (later) one line of code generation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from . import ast as A
from .errors import CheckError
from .errors import Loc
from .types import (
    BOOL, BYTES, FLOAT, INT, JSON_VALUE, NONE, STR,
    BINARY_FILE, TEXT_FILE,
    DictType, Field, FileType, FuncType, IterType, ListType, ModuleType, OptionalType, SetType, StructType, TupleType, Type,
    assignable, element_type, is_hashable, is_numeric, join,
)

# Generic built-in types and how many type arguments they take (None = any number).
CONTAINER_TYPES = {"list": 1, "set": 1, "dict": 2, "tuple": None}


class CallContext:
    """What a built-in handler sees: the call, plus helpers to check its arguments."""

    def __init__(self, checker, call: A.Call, what: str, expected: Type | None, receiver: Type | None = None):
        self.checker = checker
        self.call = call
        self.what = what  # e.g. "len()" or "list.append()", for messages
        self.expected = expected
        self.receiver = receiver

    @property
    def args(self) -> list[A.Expr]:
        return self.call.args

    def error(self, message: str, node: A.Node | None = None) -> CheckError:
        return CheckError(message, (node or self.call).loc)

    def arity(self, lo: int, hi: int | None = None, keywords: tuple[str, ...] = ()) -> int:
        hi = lo if hi is None else hi
        for kw in self.call.keywords:
            if kw.name not in keywords:
                raise self.error(f"{self.what} got an unexpected keyword argument '{kw.name}'", kw)
        n = len(self.args)
        if not lo <= n <= hi:
            if lo == hi:
                want = f"exactly {lo}"
            elif hi == MANY:
                want = f"at least {lo}"
            else:
                want = f"{lo} to {hi}"
            raise self.error(f"{self.what} takes {want} argument{'s' if hi != 1 else ''} ({n} given)")
        return n

    def arg(self, i: int, expected: Type | None = None) -> Type:
        return self.checker.check_expr(self.args[i], expected)

    def expect(self, i: int, t: Type) -> Type:
        actual = self.arg(i, t)
        if not assignable(actual, t):
            raise self.error(f"{self.what} argument must be {t}, not {actual}", self.args[i])
        return actual

    def need(self, i: int, ok: Callable[[Type], bool], what: str) -> Type:
        t = self.arg(i)
        if not ok(t):
            raise self.error(f"{self.what} argument must be {what}, not {t}", self.args[i])
        return t

    def keyword(self, name: str, t: Type) -> None:
        for kw in self.call.keywords:
            if kw.name == name:
                actual = self.checker.check_expr(kw.value, t)
                if not assignable(actual, t):
                    raise self.error(f"{self.what} argument '{name}' must be {t}, not {actual}", kw.value)

    def keyword_arg(self, name: str) -> A.Expr | None:
        return next((kw.value for kw in self.call.keywords if kw.name == name), None)

    def function(self, node: A.Expr, params: tuple[Type, ...], what: str) -> Type:
        """Check a function-valued argument (often a lambda) taking `params`; returns its result type."""
        t = self.checker.check_expr(node, FuncType(params, None))
        if not isinstance(t, FuncType) or t.params != params:
            want = f"({', '.join(map(str, params))}) -> ..."
            raise self.error(f"{self.what} {what} must be a function like {want}, not {t}", node)
        return t.ret

    def sort_key(self, elem: Type) -> None:
        """Check an optional key= function for sorted/min/max/list.sort."""
        key = self.keyword_arg("key")
        if key is None:
            if not ordered(elem):
                raise self.error(f"{self.what} can't compare {elem} values")
            return
        result = self.function(key, (elem,), "key")
        if not ordered(result):
            raise self.error(f"{self.what} key must return something comparable, not {result}", key)

    def iterable(self, i: int) -> Type:
        t = self.arg(i)
        elem = element_type(t)
        if elem is None:
            raise self.error(f"{self.what} argument must be something you can loop over, not {t}", self.args[i])
        return elem


MANY = 1_000


@dataclass
class Function:
    name: str
    check: Callable[[CallContext], Type]
    cpp: str | None = None  # C++ function to call with the arguments as given, if that's all it takes
    params: tuple | None = None  # for keyword support: ((name, type[, C++ default]), ...)


@dataclass
class Value:
    name: str
    type: Type
    cpp: str | None = None  # C++ expression for a module constant


@dataclass
class NamedType:
    """A type a module defines, usable in annotations: `json.Value`."""

    name: str
    type: Type


@dataclass
class TypeAlias:
    """A name from `typing` (Callable, TextIO, ...): importable so code also runs under Python."""

    name: str


@dataclass
class Module:
    name: str
    members: dict[str, Function | Value | StructType | Module | TypeAlias | NamedType]  # StructType: e.g. zlib.error
    header: str | None = None  # runtime header to #include, relative to the runtime directory
    libs: tuple[str, ...] = ()  # libraries to link, e.g. ("z",) for -lz


# ---- predicates -------------------------------------------------------------


def printable(t: Type) -> bool:
    return not isinstance(t, (IterType, ModuleType))


def sized(t: Type) -> bool:
    return t in (STR, BYTES, JSON_VALUE) or isinstance(t, (ListType, DictType, SetType, TupleType))


def ordered(t: Type) -> bool:
    return t in (INT, FLOAT, STR, BYTES) or isinstance(t, (TupleType, ListType))


def bytes_like(t: Type) -> bool:
    return t in (BYTES, STR)


# ---- built-in functions -----------------------------------------------------


def b_print(ctx: CallContext) -> Type:
    ctx.arity(0, MANY, keywords=("sep", "end", "file"))
    for i in range(len(ctx.args)):
        ctx.need(i, printable, "something printable")
    ctx.keyword("sep", STR)
    ctx.keyword("end", STR)
    ctx.keyword("file", TEXT_FILE)
    return NONE


OPEN_MODES = set("rwaxb+t")


def b_open(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2, keywords=("mode", "encoding"))
    ctx.expect(0, STR)
    mode_node = ctx.args[1] if n == 2 else ctx.keyword_arg("mode")
    mode = "r"
    if mode_node is not None:
        if not isinstance(mode_node, A.StrLit):
            raise ctx.error("open() mode must be a string literal like 'r', 'w' or 'rb' "
                            "(it decides whether you get str or bytes)", mode_node)
        ctx.checker.check_expr(mode_node)
        mode = mode_node.value
        kinds = sum(mode.count(c) for c in "rwax")
        if not mode or set(mode) - OPEN_MODES or kinds != 1 or len(set(mode)) != len(mode) or ("b" in mode and "t" in mode):
            raise ctx.error(f"invalid mode: {mode!r}", mode_node)
    encoding = ctx.keyword_arg("encoding")
    if encoding is not None:
        if "b" in mode:
            raise ctx.error("binary mode doesn't take an encoding argument", encoding)
        ctx.keyword("encoding", STR)
    return BINARY_FILE if "b" in mode else TEXT_FILE


def b_len(ctx: CallContext) -> Type:
    ctx.arity(1)
    ctx.need(0, sized, "a str, list, dict, set or tuple")
    return INT


def b_str(ctx: CallContext) -> Type:
    if ctx.arity(0, 1):
        ctx.need(0, printable, "something printable")
    return STR


def b_repr(ctx: CallContext) -> Type:
    ctx.arity(1)
    ctx.need(0, printable, "something printable")
    return STR


def b_int(ctx: CallContext) -> Type:
    if ctx.arity(0, 1):
        ctx.need(0, lambda t: t in (INT, FLOAT, BOOL, STR), "an int, float, bool or str")
    return INT


def b_float(ctx: CallContext) -> Type:
    if ctx.arity(0, 1):
        ctx.need(0, lambda t: t in (INT, FLOAT, BOOL, STR), "an int, float, bool or str")
    return FLOAT


def b_bool(ctx: CallContext) -> Type:
    if ctx.arity(0, 1):
        t = ctx.arg(0)
        ctx.checker.check_truthy(t, ctx.args[0])
    return BOOL


def b_range(ctx: CallContext) -> Type:
    for i in range(ctx.arity(1, 3)):
        ctx.expect(i, INT)
    return IterType(INT, "range")


def b_abs(ctx: CallContext) -> Type:
    ctx.arity(1)
    return ctx.need(0, is_numeric, "a number")


def b_min_max(ctx: CallContext) -> Type:
    n = ctx.arity(1, MANY, keywords=("key",))
    if n == 1:
        elem = ctx.iterable(0)
        ctx.sort_key(elem)
        return elem
    if ctx.keyword_arg("key") is not None:
        raise ctx.error(f"{ctx.what} only supports key= with a single iterable argument")
    result = ctx.need(0, ordered, "a number, str, tuple or list")
    for i in range(1, n):
        t = ctx.arg(i)
        joined = join(result, t)
        if joined is None or not ordered(joined):
            raise ctx.error(f"{ctx.what} arguments must all be the same kind, not {result} and {t}", ctx.args[i])
        result = joined
    return result


def b_sum(ctx: CallContext) -> Type:
    ctx.arity(1)
    elem = ctx.iterable(0)
    if not is_numeric(elem):
        raise ctx.error(f"{ctx.what} needs numbers, not {elem}", ctx.args[0])
    return elem


def b_sorted(ctx: CallContext) -> Type:
    ctx.arity(1, keywords=("reverse", "key"))
    elem = ctx.iterable(0)
    ctx.sort_key(elem)
    ctx.keyword("reverse", BOOL)
    return ListType(elem)


def b_map(ctx: CallContext) -> Type:
    ctx.arity(2)
    elem = ctx.iterable(1)  # the iterable first: it decides the function's parameter type
    result = ctx.function(ctx.args[0], (elem,), "function")
    if result == NONE:
        raise ctx.error(f"{ctx.what} function must return a value", ctx.args[0])
    return IterType(result, "map")


def b_filter(ctx: CallContext) -> Type:
    ctx.arity(2)
    elem = ctx.iterable(1)
    result = ctx.function(ctx.args[0], (elem,), "function")
    ctx.checker.check_truthy(result, ctx.args[0])
    return IterType(elem, "filter")


def b_reversed(ctx: CallContext) -> Type:
    ctx.arity(1)
    t = ctx.need(0, lambda t: isinstance(t, ListType) or t == STR, "a list or str")
    return IterType(element_type(t), "reversed")


def b_enumerate(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2)
    elem = ctx.iterable(0)
    if n == 2:
        ctx.expect(1, INT)
    return IterType(TupleType((INT, elem)), "enumerate")


def b_zip(ctx: CallContext) -> Type:
    n = ctx.arity(2, MANY)
    return IterType(TupleType(tuple(ctx.iterable(i) for i in range(n))), "zip")


def b_list(ctx: CallContext) -> Type:
    if ctx.arity(0, 1):
        return ListType(ctx.iterable(0))
    if isinstance(ctx.expected, ListType):
        return ctx.expected
    raise ctx.error("can't tell what type of list this is; annotate the variable, e.g. `xs: list[int] = []`")


def b_set(ctx: CallContext) -> Type:
    if ctx.arity(0, 1):
        elem = ctx.iterable(0)
        if not is_hashable(elem):
            raise ctx.error(f"set elements must be int, float, str, bool, or a tuple of those; not {elem}")
        return SetType(elem)
    if isinstance(ctx.expected, SetType):
        return ctx.expected
    raise ctx.error("can't tell what type of set this is; annotate the variable, e.g. `s: set[int] = set()`")


def b_dict(ctx: CallContext) -> Type:
    ctx.arity(0)
    if isinstance(ctx.expected, DictType):
        return ctx.expected
    raise ctx.error("can't tell what type of dict this is; annotate the variable, e.g. `d: dict[str, int] = {}`")


def b_any_all(ctx: CallContext) -> Type:
    ctx.arity(1)
    elem = ctx.iterable(0)
    ctx.checker.check_truthy(elem, ctx.args[0])
    return BOOL


def b_input(ctx: CallContext) -> Type:
    if ctx.arity(0, 1):
        ctx.expect(0, STR)
    return STR


def b_bytes(ctx: CallContext) -> Type:
    if not ctx.arity(0, 1):
        return BYTES
    t = ctx.arg(0)
    if t == STR:
        raise ctx.error("bytes(str) needs an encoding; use s.encode() instead", ctx.args[0])
    if t != INT and element_type(t) != INT:
        raise ctx.error(f"bytes() needs a length or a list of ints (0-255), not {t}", ctx.args[0])
    return BYTES


def b_ord(ctx: CallContext) -> Type:
    ctx.arity(1)
    ctx.expect(0, STR)
    return INT


def b_chr(ctx: CallContext) -> Type:
    ctx.arity(1)
    ctx.expect(0, INT)
    return STR


def b_round(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2)
    ctx.need(0, is_numeric, "a number")
    if n == 2:
        ctx.expect(1, INT)
        return FLOAT
    return INT


FUNCTIONS: dict[str, Callable[[CallContext], Type]] = {
    "print": b_print,
    "len": b_len,
    "str": b_str,
    "repr": b_repr,
    "int": b_int,
    "float": b_float,
    "bool": b_bool,
    "range": b_range,
    "abs": b_abs,
    "min": b_min_max,
    "max": b_min_max,
    "sum": b_sum,
    "sorted": b_sorted,
    "map": b_map,
    "filter": b_filter,
    "reversed": b_reversed,
    "enumerate": b_enumerate,
    "zip": b_zip,
    "list": b_list,
    "set": b_set,
    "dict": b_dict,
    "any": b_any_all,
    "all": b_any_all,
    "input": b_input,
    "bytes": b_bytes,
    "open": b_open,
    "ord": b_ord,
    "chr": b_chr,
    "round": b_round,
}

VALUES: dict[str, Value] = {
    "__name__": Value("__name__", STR),
}


# ---- methods on built-in types ----------------------------------------------


def returns(t: Type | Callable[[Type], Type], lo: int = 0, hi: int | None = None, args: tuple = ()):
    """A method taking `args` (types, or functions of the receiver type) and returning `t`."""

    def handler(ctx: CallContext) -> Type:
        n = ctx.arity(lo, len(args) if hi is None else hi)
        for i in range(n):
            want = args[i](ctx.receiver) if callable(args[i]) else args[i]
            ctx.expect(i, want)
        return t(ctx.receiver) if callable(t) else t

    return handler


def elem_of(t: Type) -> Type:
    return t.elem


def key_of(t: Type) -> Type:
    return t.key


def value_of(t: Type) -> Type:
    return t.value


def same(t: Type) -> Type:
    return t


def str_join(ctx: CallContext) -> Type:
    ctx.arity(1)
    elem = ctx.iterable(0)
    if elem != STR:
        raise ctx.error(f"str.join() needs strings, not {elem} (convert with str(...) first)", ctx.args[0])
    return STR


def list_extend(ctx: CallContext) -> Type:
    ctx.arity(1)
    elem = ctx.iterable(0)
    if not assignable(elem, ctx.receiver.elem):
        raise ctx.error(f"can't extend a {ctx.receiver} with {elem} items", ctx.args[0])
    return NONE


def list_sort(ctx: CallContext) -> Type:
    ctx.arity(0, keywords=("reverse", "key"))
    ctx.sort_key(ctx.receiver.elem)
    ctx.keyword("reverse", BOOL)
    return NONE


def dict_get(ctx: CallContext) -> Type:
    d: DictType = ctx.receiver
    n = ctx.arity(1, 2)
    ctx.expect(0, d.key)
    if n == 2:
        ctx.expect(1, d.value)
        return d.value
    return d.value if isinstance(d.value, OptionalType) else OptionalType(d.value)


def dict_pop(ctx: CallContext) -> Type:
    d: DictType = ctx.receiver
    n = ctx.arity(1, 2)
    ctx.expect(0, d.key)
    if n == 2:
        ctx.expect(1, d.value)
    return d.value


STR_METHODS = {
    **{name: returns(STR, 0, 1, (STR,)) for name in ("strip", "lstrip", "rstrip")},
    **{name: returns(STR) for name in ("upper", "lower", "title", "capitalize")},
    **{name: returns(BOOL) for name in ("isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower")},
    **{name: returns(BOOL, args=(STR,)) for name in ("startswith", "endswith")},
    **{name: returns(INT, args=(STR,)) for name in ("find", "count")},
    "split": returns(ListType(STR), 0, 1, (STR,)),
    "splitlines": returns(ListType(STR)),
    "replace": returns(STR, args=(STR, STR)),
    "join": str_join,
    "encode": returns(BYTES, 0, 1, (STR,)),
}

BYTES_METHODS = {
    "decode": returns(STR, 0, 1, (STR,)),
    "hex": returns(STR),
    **{name: returns(BOOL, args=(BYTES,)) for name in ("startswith", "endswith")},
    **{name: returns(INT, args=(BYTES,)) for name in ("find", "count")},
}

LIST_METHODS = {
    "append": returns(NONE, args=(elem_of,)),
    "insert": returns(NONE, args=(INT, elem_of)),
    "pop": returns(elem_of, 0, 1, (INT,)),
    "remove": returns(NONE, args=(elem_of,)),
    "index": returns(INT, args=(elem_of,)),
    "count": returns(INT, args=(elem_of,)),
    "reverse": returns(NONE),
    "clear": returns(NONE),
    "copy": returns(same),
    "extend": list_extend,
    "sort": list_sort,
}

DICT_METHODS = {
    "get": dict_get,
    "pop": dict_pop,
    "setdefault": returns(value_of, args=(key_of, value_of)),
    "update": returns(NONE, args=(same,)),
    "keys": returns(lambda d: IterType(d.key, "keys")),
    "values": returns(lambda d: IterType(d.value, "values")),
    "items": returns(lambda d: IterType(TupleType((d.key, d.value)), "items")),
    "clear": returns(NONE),
    "copy": returns(same),
}

SET_METHODS = {
    "add": returns(NONE, args=(elem_of,)),
    "remove": returns(NONE, args=(elem_of,)),
    "discard": returns(NONE, args=(elem_of,)),
    "clear": returns(NONE),
    "copy": returns(same),
    **{name: returns(same, args=(same,)) for name in ("union", "intersection", "difference")},
    **{name: returns(BOOL, args=(same,)) for name in ("issubset", "issuperset")},
}


def content(f: FileType) -> Type:
    return BYTES if f.binary else STR


def file_writelines(ctx: CallContext) -> Type:
    ctx.arity(1)
    elem = ctx.iterable(0)
    if elem != content(ctx.receiver):
        raise ctx.error(f"{ctx.receiver}.writelines() needs {content(ctx.receiver)} items, not {elem}", ctx.args[0])
    return NONE


FILE_METHODS = {
    "read": returns(content, 0, 1, (INT,)),
    "readline": returns(content),
    "readlines": returns(lambda f: ListType(content(f))),
    "write": returns(INT, args=(content,)),
    "writelines": file_writelines,
    "close": returns(NONE),
    "flush": returns(NONE),
}


def json_value_get(ctx: CallContext) -> Type:
    ctx.arity(1)
    ctx.expect(0, STR)
    return OptionalType(JSON_VALUE)


JSON_VALUE_METHODS = {
    "as_int": returns(INT),
    "as_float": returns(FLOAT),
    "as_str": returns(STR),
    "as_bool": returns(BOOL),
    "as_list": returns(ListType(JSON_VALUE)),
    "as_dict": returns(DictType(STR, JSON_VALUE)),
    **{name: returns(BOOL) for name in ("is_null", "is_int", "is_float", "is_str", "is_bool", "is_list", "is_dict")},
    "keys": returns(ListType(STR)),
    "values": returns(ListType(JSON_VALUE)),
    "items": returns(ListType(TupleType((STR, JSON_VALUE)))),
    "get": json_value_get,
}


def method_for(t: Type, name: str) -> Callable[[CallContext], Type] | None:
    match t:
        case _ if t == JSON_VALUE:
            return JSON_VALUE_METHODS.get(name)
        case FileType():
            return FILE_METHODS.get(name)
        case ListType():
            table = LIST_METHODS
        case DictType():
            table = DICT_METHODS
        case SetType():
            table = SET_METHODS
        case _ if t == STR:
            table = STR_METHODS
        case _ if t == BYTES:
            table = BYTES_METHODS
        case _:
            return None
    return table.get(name)


# ---- modules ----------------------------------------------------------------


def float_fn(n_args: int) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        for i in range(ctx.arity(n_args)):
            ctx.need(i, is_numeric, "a number")
        return FLOAT

    return handler


def m_log(ctx: CallContext) -> Type:
    for i in range(ctx.arity(1, 2)):
        ctx.need(i, is_numeric, "a number")
    return FLOAT


def numeric_to(t: Type) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        ctx.arity(1)
        ctx.need(0, is_numeric, "a number")
        return t

    return handler


def sys_exit(ctx: CallContext) -> Type:
    if ctx.arity(0, 1):
        ctx.expect(0, INT)
    return NONE


def module(name: str, **members: Callable[[CallContext], Type] | Type) -> Module:
    table: dict[str, Function | Value] = {}
    for member, m in members.items():
        table[member] = Value(member, m) if isinstance(m, Type) else Function(member, m)
    return Module(name, table)


def bytes_fn(result: Type, lo: int = 1, hi: int = 1, extra: tuple[Type, ...] = ()) -> Callable[[CallContext], Type]:
    """A function whose first argument is bytes (a str is accepted and encoded as UTF-8)."""

    def handler(ctx: CallContext) -> Type:
        n = ctx.arity(lo, hi)
        ctx.need(0, bytes_like, "bytes or str")
        for i in range(1, n):
            ctx.expect(i, extra[i - 1])
        return result

    return handler


MODULES: dict[str, Module] = {
    "math": module(
        "math",
        **{f: float_fn(1) for f in ("sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "exp", "log2", "log10", "fabs")},
        **{f: float_fn(2) for f in ("atan2", "pow", "hypot")},
        **{f: numeric_to(INT) for f in ("floor", "ceil", "trunc")},
        **{f: numeric_to(BOOL) for f in ("isnan", "isinf")},
        log=m_log,
        gcd=returns(INT, args=(INT, INT)),
        pi=FLOAT, e=FLOAT, tau=FLOAT, inf=FLOAT, nan=FLOAT,
    ),
    "sys": module(
        "sys",
        argv=ListType(STR),
        exit=sys_exit,
    ),
}


# ---- exceptions -------------------------------------------------------------

# Built-in exception classes: (name, base). Kept in sync with seadash.hpp.
EXCEPTION_TREE = [
    ("BaseException", None),
    ("Exception", "BaseException"),
    ("OSError", "Exception"),
    ("ArithmeticError", "Exception"),
    ("ZeroDivisionError", "ArithmeticError"),
    ("OverflowError", "ArithmeticError"),
    ("LookupError", "Exception"),
    ("IndexError", "LookupError"),
    ("KeyError", "LookupError"),
    ("ValueError", "Exception"),
    ("TypeError", "Exception"),
    ("AssertionError", "Exception"),
    ("RuntimeError", "Exception"),
    ("NotImplementedError", "RuntimeError"),
    ("EOFError", "Exception"),
    ("FileNotFoundError", "OSError"),
    ("FileExistsError", "OSError"),
    ("PermissionError", "OSError"),
    ("IsADirectoryError", "OSError"),
    ("NotADirectoryError", "OSError"),
    ("UnicodeError", "ValueError"),
    ("UnicodeDecodeError", "UnicodeError"),
]


def make_exceptions() -> dict[str, StructType]:
    out: dict[str, StructType] = {}
    for name, base in EXCEPTION_TREE:
        out[name] = StructType(name, "class", None, base=out.get(base), builtin=True)
    # Every exception carries a message: `ValueError("bad")`, `str(e)`, `e.message`.
    empty = A.StrLit("")
    empty.ty = STR
    out["BaseException"].fields["message"] = Field("message", STR, empty, Loc(0, 0))
    return out


EXCEPTIONS: dict[str, StructType] = make_exceptions()
BASE_EXCEPTION = EXCEPTIONS["BaseException"]


# ---- modules backed by runtime headers --------------------------------------


def runtime_module(name: str, header: str, libs: tuple[str, ...] = (), **members) -> Module:
    """Functions are (handler, "C++ name"); constants are (Type, "C++ expression")."""
    table: dict[str, Function | Value | StructType] = {}
    for member, spec in members.items():
        if isinstance(spec, StructType):
            table[member] = spec
        elif isinstance(spec[0], Type):
            table[member] = Value(member, spec[0], spec[1])
        else:
            table[member] = Function(member, spec[0], spec[1])
    return Module(name, table, header, libs)


def exception_class(name: str, cpp: str, base: str = "Exception") -> StructType:
    return StructType(name, "class", None, base=EXCEPTIONS[base], builtin=True, cpp_name=cpp)


MODULES["base64"] = runtime_module(
    "base64", "modules/base64.hpp",
    b64encode=(bytes_fn(BYTES), "sd::base64::b64encode"),
    b64decode=(bytes_fn(BYTES), "sd::base64::b64decode"),
    standard_b64encode=(bytes_fn(BYTES), "sd::base64::b64encode"),
    standard_b64decode=(bytes_fn(BYTES), "sd::base64::b64decode"),
    urlsafe_b64encode=(bytes_fn(BYTES), "sd::base64::urlsafe_b64encode"),
    urlsafe_b64decode=(bytes_fn(BYTES), "sd::base64::urlsafe_b64decode"),
    b16encode=(bytes_fn(BYTES), "sd::base64::b16encode"),
    b16decode=(bytes_fn(BYTES), "sd::base64::b16decode"),
)

MODULES["zlib"] = runtime_module(
    "zlib", "modules/zlib.hpp", ("z",),
    compress=(bytes_fn(BYTES, 1, 2, (INT,)), "sd::zlib::compress"),
    decompress=(bytes_fn(BYTES), "sd::zlib::decompress"),
    crc32=(bytes_fn(INT, 1, 2, (INT,)), "sd::zlib::crc32"),
    adler32=(bytes_fn(INT, 1, 2, (INT,)), "sd::zlib::adler32"),
    error=exception_class("error", "sd::zlib::error"),
    Z_BEST_SPEED=(INT, "1_i"),
    Z_BEST_COMPRESSION=(INT, "9_i"),
    Z_DEFAULT_COMPRESSION=(INT, "(-1_i)"),
    ZLIB_VERSION=(STR, "std::string(ZLIB_VERSION)"),
)


def signature(result: Type, *params: tuple) -> Callable[[CallContext], Type]:
    """A module function with named parameters: each is (name, type) or (name, type, C++ default).
    Keyword arguments work; codegen passes every parameter in order (see Function.params)."""

    def handler(ctx: CallContext) -> Type:
        names = [p[0] for p in params]
        if len(ctx.args) > len(params):
            raise ctx.error(f"{ctx.what} takes at most {len(params)} arguments ({len(ctx.args)} given)")
        for kw in ctx.call.keywords:
            if kw.name not in names:
                raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{kw.name}'", kw)
            if names.index(kw.name) < len(ctx.args):
                raise ctx.error(f"{ctx.what} got multiple values for argument '{kw.name}'", kw)
        for i, p in enumerate(params):
            node = ctx.args[i] if i < len(ctx.args) else ctx.keyword_arg(p[0])
            if node is None:
                if len(p) < 3:
                    raise ctx.error(f"{ctx.what} is missing argument '{p[0]}'")
                continue
            actual = ctx.checker.check_expr(node, p[1])
            if not assignable(actual, p[1]):
                raise ctx.error(f"{ctx.what} argument '{p[0]}' must be {p[1]}, not {actual}", node)
        return result

    handler.params = params
    return handler


def os_getenv(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2)
    ctx.expect(0, STR)
    if n == 2:
        ctx.expect(1, STR)
        return STR
    return OptionalType(STR)


def path_join(ctx: CallContext) -> Type:
    for i in range(ctx.arity(1, MANY)):
        ctx.expect(i, STR)
    return STR


def module_with_params(mod: Module) -> Module:
    for m in mod.members.values():
        if isinstance(m, Function) and hasattr(m.check, "params"):
            m.params = m.check.params
    return mod


OS_PATH = module_with_params(runtime_module(
    "os.path", "modules/os.hpp",
    exists=(signature(BOOL, ("path", STR)), "sd::os::path::exists"),
    isfile=(signature(BOOL, ("path", STR)), "sd::os::path::isfile"),
    isdir=(signature(BOOL, ("path", STR)), "sd::os::path::isdir"),
    join=(path_join, "sd::os::path::join"),
    basename=(signature(STR, ("path", STR)), "sd::os::path::basename"),
    dirname=(signature(STR, ("path", STR)), "sd::os::path::dirname"),
    abspath=(signature(STR, ("path", STR)), "sd::os::path::abspath"),
    splitext=(signature(TupleType((STR, STR)), ("path", STR)), "sd::os::path::splitext"),
    getsize=(signature(INT, ("path", STR)), "sd::os::path::getsize"),
    sep=(STR, "sd::os::path::sep()"),
))

MODULES["os"] = module_with_params(runtime_module(
    "os", "modules/os.hpp",
    getcwd=(signature(STR), "sd::os::getcwd"),
    listdir=(signature(ListType(STR), ("path", STR, '"."s')), "sd::os::listdir"),
    mkdir=(signature(NONE, ("path", STR)), "sd::os::mkdir"),
    makedirs=(signature(NONE, ("name", STR), ("exist_ok", BOOL, "false")), "sd::os::makedirs"),
    remove=(signature(NONE, ("path", STR)), "sd::os::remove"),
    unlink=(signature(NONE, ("path", STR)), "sd::os::remove"),
    rmdir=(signature(NONE, ("path", STR)), "sd::os::rmdir"),
    rename=(signature(NONE, ("src", STR), ("dst", STR)), "sd::os::rename"),
    getenv=(os_getenv, "sd::os::getenv"),
    sep=(STR, "sd::os::path::sep()"),
))
MODULES["os"].members["path"] = OS_PATH

MODULES["typing"] = Module("typing", {
    name: TypeAlias(name) for name in ("Callable", "TextIO", "BinaryIO", "Optional", "List", "Dict", "Set", "Tuple")
})


# ---- json -------------------------------------------------------------------


def json_problem(t: Type, decoding: bool, seen: frozenset = frozenset()) -> str | None:
    """Why `t` can't be converted to/from JSON, or None if it can."""
    match t:
        case _ if t in (INT, FLOAT, BOOL, STR, JSON_VALUE):
            return None
        case ListType(elem) | SetType(elem) | OptionalType(elem):
            return json_problem(elem, decoding, seen)
        case TupleType(elts):
            return next((p for e in elts if (p := json_problem(e, decoding, seen))), None)
        case DictType(key, value):
            keys_ok = key == STR if decoding else key in (STR, INT, FLOAT, BOOL)
            if not keys_ok:
                return f"JSON object keys are strings, so {t} can't be {'decoded' if decoding else 'encoded'}"
            return json_problem(value, decoding, seen)
        case StructType() if not t.is_exception:
            if t in seen:
                return None
            for f in t.all_fields().values():
                if p := json_problem(f.type, decoding, seen | {t}):
                    return p
            return None
    return f"{t} can't be converted to or from JSON"


def json_loads_fn(from_file: bool) -> Callable[[CallContext], Type]:
    """json.loads(text) / json.load(file): the result type comes from the context."""

    def handler(ctx: CallContext) -> Type:
        ctx.arity(1)
        if from_file:
            ctx.expect(0, TEXT_FILE)
        else:
            ctx.need(0, bytes_like, "str or bytes")
        target = ctx.expected
        if target is None:
            raise ctx.error(
                f"{ctx.what} needs to know what type to produce; annotate the variable, e.g. "
                f"`data: dict[str, int] = ...`, or use `json.Value` for any JSON"
            )
        if problem := json_problem(target, decoding=True):
            raise ctx.error(problem)
        return target

    return handler


def json_dumps_fn(to_file: bool) -> Callable[[CallContext], Type]:
    options = (
        ("indent", OptionalType(INT), "std::nullopt"),
        ("sort_keys", BOOL, "false"),
        ("ensure_ascii", BOOL, "true"),
        ("separators", OptionalType(TupleType((STR, STR))), "std::nullopt"),
    )
    params = (("obj", None),) + ((("fp", TEXT_FILE),) if to_file else ()) + options

    def handler(ctx: CallContext) -> Type:
        if not ctx.args:
            raise ctx.error(f"{ctx.what} is missing argument 'obj'")
        obj = ctx.arg(0)
        if problem := json_problem(obj, decoding=False):
            raise ctx.error(problem, ctx.args[0])
        rest = signature(NONE, *params[1:])
        shifted = CallContext(ctx.checker, A.Call(ctx.call.func, ctx.args[1:], ctx.call.keywords, loc=ctx.call.loc),
                              ctx.what, None)
        rest(shifted)
        return NONE if to_file else STR

    handler.params = params
    return handler


MODULES["json"] = module_with_params(runtime_module(
    "json", "modules/json.hpp",
    loads=(json_loads_fn(False), "sd::json::loads<{T}>"),
    load=(json_loads_fn(True), "sd::json::load<{T}>"),
    dumps=(json_dumps_fn(False), "sd::json::dumps"),
    dump=(json_dumps_fn(True), "sd::json::dump"),
    JSONDecodeError=exception_class("JSONDecodeError", "sd::json::JSONDecodeError", "ValueError"),
))
MODULES["json"].members["Value"] = NamedType("Value", JSON_VALUE)
