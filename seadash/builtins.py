"""Built-in functions, methods on built-in types, and built-in modules.

Each built-in is a small handler that receives a CallContext, checks the
arguments, and returns the result type. Keeping them as a table means adding
a built-in is one function here plus (later) one line of code generation. The standard
library's modules are in seadash/stdlib/, one file each, built on the helpers here.
"""

from __future__ import annotations

import _string  # Python's own format-string parser, for str.format()
from collections.abc import Callable
from dataclasses import dataclass

from . import ast as A
from .errors import CheckError
from .errors import Loc
from .types import (
    BOOL, BYTEARRAY, BYTES, FLOAT, INT, JSON_VALUE, NONE, SOCKET, STR, BINARY_FILE, TEXT_FILE, CounterType, DequeType,
    PATH, DATE, DATETIME, TIME, TIMEDELTA, VarTupleType, GeneratorType, UUID_T, BuiltinClass, CSV_DICT_READER,
    HTTP_HEADERS, DictType, Field, FileType, FuncType, user_dunder, IterType, ListType, OptionalType, SetType,
    StructType, TupleType, Type, ClassRefType, CmpKeyType, assignable, filled_for, fills_defaults, element_type,
    enum_mixin, is_hashable, is_numeric, join,
)

# Generic built-in types and how many type arguments they take (None = any number).
CONTAINER_TYPES = {"list": 1, "set": 1, "dict": 2, "tuple": None}


class CallContext:
    """What a built-in handler sees: the call, plus helpers to check its arguments."""

    def __init__(self, checker, call: A.Call, what: str, expected: Type | None, receiver: Type | None = None):
        self.checker = checker
        self.call = call
        self.what = what  # e.g. "len()" or "list.append()", for messages
        if call.double_star:
            raise CheckError(f"{what} can't take its arguments from a dict with '**' yet; pass them by name",
                             call.double_star[0].loc)
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
        if hi == 0 and n and keywords:  # (keywords only: xs.sort(key=f))
            raise self.error(f"{self.what} takes no positional arguments")
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
        if not ok(t) and enum_mixin(t) is not None and ok(enum_mixin(t)):
            t = self.decay(i)  # len(Mode.FAST): a StrEnum member is a str
        if not ok(t):
            raise self.error(f"{self.what} argument must be {what}, not {t}", self.args[i])
        return t

    def decay(self, i: int) -> Type:
        """Use an IntEnum/StrEnum member argument as its value (`member.value`); returns its type."""
        self.args[i] = A.Attribute(self.args[i], "value", loc=self.args[i].loc)
        return self.arg(i)

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
        if isinstance(t, FuncType) and t.params != params and fills_defaults(t, FuncType(params, t.ret)):
            node.notes["fill_to"] = FuncType(params, t.ret)  # (codegen passes a lambda filling in the defaults)
            return t.ret
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
            raise self.error(f"{self.what} argument must be something you can loop over, not {t}{mixed_tuple_hint(t)}",
                             self.args[i])
        mark_tuple_iterable(self.args[i], t, elem)
        return elem


def mark_tuple_iterable(node, t: Type, elem: Type) -> None:
    """A tuple used as an iterable is converted to a list of its common element type
    (codegen reads this mark)."""
    if isinstance(t, TupleType):
        node.notes["tuple_elem"] = elem


def mixed_tuple_hint(t: Type) -> str:
    if isinstance(t, TupleType) and len(t.elts) > 1:
        return " (its items have different types, so there's no single type for the loop variable)"
    return ""


MANY = 1_000


@dataclass
class Function:
    name: str
    check: Callable[[CallContext], Type]
    cpp: str | None = None  # C++ function to call with the arguments as given, if that's all it takes
    params: tuple | None = None  # for keyword support: ((name, type[, C++ default]), ...)
    mutates_first_arg: bool = False  # random.shuffle(xs) changes xs in place
    as_type: Type | None = None  # also a type in annotations: socket.socket
    # also a type taking type arguments in annotations (selectors.DefaultSelector[socket.socket, int]):
    # (checker, [type arguments], node) -> Type
    generic_type: Callable | None = None


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


@dataclass
class UserModule(Module):
    """A module compiled from a .sd file: members are FuncInfo, StructType and global Var."""

    namespace: str = ""  # C++ namespace, e.g. "sdm::geometry::shapes"
    info: object = None  # its checker.ModuleInfo
    path: str = ""  # the .sd file ("" for a folder of modules)


def module_repr(mod: Module) -> str:
    """How print() shows a module, like Python: <module 'math' (built-in)>."""
    if isinstance(mod, UserModule):
        return f"<module '{mod.name}' from '{mod.path}'>" if mod.path else f"<module '{mod.name}' (namespace)>"
    return f"<module '{mod.name}' (built-in)>"


# ---- predicates -------------------------------------------------------------


def printable(t: Type) -> bool:
    return not isinstance(t, (IterType, ClassRefType))


def sized(t: Type) -> bool:
    return t in (STR, BYTES, BYTEARRAY, JSON_VALUE, HTTP_HEADERS) or isinstance(t, (ListType, DictType, SetType, TupleType, DequeType, VarTupleType)) or bool(
        user_dunder(t, "__len__")
    ) or (isinstance(t, IterType) and t.kind in ("keys", "values", "items", "range")) or (  # len(d.keys()), len(range(n))
        isinstance(t, ClassRefType) and t.st.enum is not None) or (  # len(Color)
        isinstance(t, StructType) and t.enum is not None and t.enum.flag)  # len(perms): its members


def ordered(t: Type) -> bool:
    return t in (INT, FLOAT, STR, BYTES, BYTEARRAY, PATH, DATE, TIME, DATETIME, TIMEDELTA, UUID_T) or isinstance(t, (TupleType, ListType, VarTupleType, CmpKeyType)) or bool(user_dunder(t, "__lt__")) or (
        isinstance(t, StructType) and t.enum is not None and t.enum.mixin is not None)  # IntEnum, StrEnum


def unhashable_hint(t: Type) -> str:
    return " (a bytearray can change, so it's unhashable: use bytes(...))" if t == BYTEARRAY else ""


def bytes_like(t: Type) -> bool:
    return t in (BYTES, BYTEARRAY, STR)


# ---- built-in functions -----------------------------------------------------


def b_print(ctx: CallContext) -> Type:
    ctx.arity(0, MANY, keywords=("sep", "end", "file", "flush"))
    for i, arg in enumerate(ctx.args):
        if isinstance(arg, A.Starred):  # print(*xs): each item, separated by sep
            t = ctx.checker.check_expr(arg.value)
            arg.ty = t
            if (items := element_type(t)) is None or not printable(items):
                raise ctx.error(f"print(*...) needs something iterable whose items can be printed, not {t}", arg)
            continue
        ctx.need(i, printable, "something printable")
    ctx.keyword("sep", STR)
    ctx.keyword("end", STR)
    ctx.keyword("file", TEXT_FILE)
    ctx.keyword("flush", BOOL)
    return NONE


OPEN_MODES = set("rwaxb+t")


def b_open(ctx: CallContext) -> Type:
    """open(path) or open(fd): a file object (os.fdopen(fd) is the same thing)."""
    n = ctx.arity(1, 2, keywords=("mode", "encoding", "newline", "closefd"))
    t = ctx.need(0, lambda t: t in (STR, PATH, INT), "a str, a Path or a file descriptor (int)")
    if (nl := ctx.keyword_arg("newline")) is not None:  # (seadash never translates newlines: newline="" is the norm)
        ctx.checker.check_expr(nl)
    if (closefd := ctx.keyword_arg("closefd")) is not None:
        ctx.keyword("closefd", BOOL)
        if t != INT and not (isinstance(closefd, A.BoolLit) and closefd.value):
            raise ctx.error("Cannot use closefd=False with file name", closefd)
    return open_mode(ctx, ctx.args[1] if n == 2 else ctx.keyword_arg("mode"))


def os_fdopen(ctx: CallContext) -> Type:
    """os.fdopen(fd, mode="r", ...): open() for a file descriptor."""
    ctx.need(0, lambda t: t == INT, "a file descriptor (int)") if ctx.args else None
    return b_open(ctx)


def open_mode(ctx: CallContext, mode_node: A.Expr | None) -> Type:
    """open()'s mode decides the file type: TextIO or BinaryIO."""
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
    ctx.need(0, sized, "a str, bytes, list, dict, set or tuple")
    return INT


def b_str(ctx: CallContext) -> Type:
    n = ctx.arity(0, 2, keywords=("encoding",))
    if n == 2 or ctx.call.keywords:  # str(data, "utf-8") is data.decode("utf-8")
        if n == 0 or ctx.arg(0) not in (BYTES, BYTEARRAY):
            what = "decoding str is not supported" if n and ctx.args[0].ty == STR else "str() with an encoding needs bytes to decode"
            raise ctx.error(what, ctx.args[0] if n else None)
        encoding_argument(ctx, n)
    elif n:
        ctx.need(0, printable, "something printable")
    return STR


def encoding_argument(ctx: CallContext, n: int) -> None:
    """The encoding of str(data, encoding) or bytes(text, encoding): the second argument or encoding=."""
    if n == 2 and ctx.call.keywords:
        raise ctx.error(f"{ctx.what} got multiple values for argument 'encoding'", ctx.call.keywords[0])
    if n == 2:
        ctx.expect(1, STR)
    ctx.keyword("encoding", STR)


def b_repr(ctx: CallContext) -> Type:
    ctx.arity(1)
    ctx.need(0, printable, "something printable")
    return STR


FORMAT_SAMPLES = {INT: 0, FLOAT: 0.0, BOOL: False, STR: ""}


def format_spec_error(t: Type, spec: str | None) -> str | None:
    """Why a `t` can't be formatted with `spec` (None: a spec computed at run time), if it can't.
    A constant spec is checked by Python's own rules, so the message is Python's."""
    if isinstance(t, StructType) and t.enum is not None:
        t = t.enum.mixin or STR  # an enum member formats its str(), an IntEnum's its value
    if t in (DATE, DATETIME, TIME):
        return None  # a strftime format
    article = ("an " if str(t)[:1].lower() in "aeiou" else "a ") + str(t)
    if t not in FORMAT_SAMPLES:
        return f"a format spec needs an int, float, str or date, not {article}; convert it first, e.g. with str()"
    if spec is not None:
        try:
            format(FORMAT_SAMPLES[t], spec)
        except ValueError as err:
            return f"bad format spec ':{spec}' for {article}: {err}"
    return None


def b_same_class(ctx: CallContext) -> Type:
    """__same_class__(a, b): are two class instances of exactly the same class? (Used by the
    methods @dataclass generates; not meant for programs.)"""
    ctx.arity(2)
    for i in range(2):
        ctx.need(i, lambda t: isinstance(t, StructType) and t.kind == "class", "a class instance")
    return BOOL


def b_class_name(ctx: CallContext) -> Type:
    """__class_name__(obj): the name of obj's class at run time (for generated messages)."""
    ctx.arity(1)
    ctx.need(0, lambda t: isinstance(t, StructType) and t.kind == "class", "a class instance")
    return STR


def b_format(ctx: CallContext) -> Type:
    """format(x, spec): like f"{x:spec}"."""
    n = ctx.arity(1, 2)
    t = ctx.need(0, printable, "something printable")
    if n == 2:
        ctx.expect(1, STR)
        spec = ctx.args[1]
        if not (isinstance(spec, A.StrLit) and not spec.value):
            if isinstance(t, OptionalType) and format_spec_error(strip_optional_type(t), None) is None:
                raise ctx.error(f"{t} might be None; check it first", ctx.args[0])
            if message := format_spec_error(t, spec.value if isinstance(spec, A.StrLit) else None):
                raise ctx.error(message, ctx.args[0] if message.startswith("a format spec") else spec)
    return STR


def b_int(ctx: CallContext) -> Type:
    n = ctx.arity(0, 2, keywords=("base",))
    if n:
        ctx.need(0, lambda t: t in (INT, FLOAT, BOOL, STR), "an int, float, bool or str")
    base = ctx.args[1] if n == 2 else ctx.keyword_arg("base")
    if base is not None:
        if n == 2 and ctx.keyword_arg("base") is not None:
            raise ctx.error("int() got multiple values for argument 'base'", ctx.keyword_arg("base"))
        if n == 0:
            raise ctx.error("int() missing string argument")
        if ctx.args[0].ty != STR:
            raise ctx.error("int() can't convert non-string with explicit base", ctx.args[0])
        if ctx.checker.check_expr(base, INT) != INT:
            raise ctx.error(f"int() base must be an int, not {base.ty}", base)
    return INT


def b_digits(ctx: CallContext) -> Type:
    """bin(n), oct(n), hex(n)."""
    ctx.arity(1)
    t = ctx.arg(0)
    if enum_mixin(t) is not None:
        t = ctx.decay(0)  # hex(N.ONE)
    if t not in (INT, BOOL):
        raise ctx.error(f"'{t}' object cannot be interpreted as an integer", ctx.args[0])
    return STR


def b_divmod(ctx: CallContext) -> Type:
    ctx.arity(2)
    types = [ctx.decay(i) if enum_mixin(ctx.arg(i)) is not None else ctx.args[i].ty for i in range(2)]
    for i, t in enumerate(types):
        if t not in (INT, FLOAT, BOOL):
            raise ctx.error(f"divmod() arguments must be numbers, not {t}", ctx.args[i])
    result = FLOAT if FLOAT in types else INT
    return TupleType((result, result))


def b_exit(ctx: CallContext) -> Type:
    """exit() and quit(): like sys.exit()."""
    if ctx.arity(0, 1):
        ctx.expect(0, INT)
    return NONE


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
    t = ctx.arg(0)
    if enum_mixin(t) is not None:
        t = ctx.decay(0)  # abs(N.ONE)
    if m := user_dunder(t, "__abs__"):
        return m.ret
    if t == TIMEDELTA:
        return t
    if not is_numeric(t):
        raise ctx.error(f"{ctx.what} argument must be a number, not {t}", ctx.args[0])
    return t


def b_hash(ctx: CallContext) -> Type:
    ctx.arity(1)
    t = ctx.arg(0)
    if not is_hashable(t):
        raise ctx.error(f"{t} isn't hashable (define __hash__ and __eq__ on it)", ctx.args[0])
    return INT


def b_min_max(ctx: CallContext) -> Type:
    n = ctx.arity(1, MANY, keywords=("key", "default"))
    default = ctx.keyword_arg("default")
    if n == 1:
        elem = ctx.iterable(0)
        ctx.sort_key(elem)
        if default is None:
            return elem
        result = join(elem, ctx.checker.check_expr(default, elem))
        if result is None:
            raise ctx.error(f"{ctx.what} default must be {elem} (or None), not {default.ty}", default)
        return result
    if default is not None:
        raise ctx.error(f"Cannot specify a default for {ctx.what} with multiple positional arguments", default)
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
    n = ctx.arity(1, 2, keywords=("start",))
    elem = ctx.iterable(0)
    start = ctx.args[1] if n == 2 else ctx.keyword_arg("start")
    if elem == TIMEDELTA and start is None:
        raise ctx.error(f"{ctx.what} of timedeltas needs a starting value: sum(items, timedelta())", ctx.args[0])
    if elem == BOOL:  # sum(x > 0 for x in xs): how many are true
        elem = INT
    if not (is_numeric(elem) or elem == TIMEDELTA):
        raise ctx.error(f"{ctx.what} needs numbers (or timedeltas), not {elem}", ctx.args[0])
    if start is None:
        return elem
    st = ctx.checker.check_expr(start, elem)
    if elem == TIMEDELTA or st == TIMEDELTA:
        if st != elem:
            raise ctx.error(f"{ctx.what} can't add {elem} items to a {st} start", start)
        return elem
    if not is_numeric(st):
        raise ctx.error(f"{ctx.what} start must be a number, not {st}", start)
    return FLOAT if FLOAT in (st, elem) else INT


def b_sorted(ctx: CallContext) -> Type:
    ctx.arity(1, keywords=("reverse", "key"))
    elem = ctx.iterable(0)
    ctx.sort_key(elem)
    ctx.keyword("reverse", BOOL)
    return ListType(elem)


def b_map(ctx: CallContext) -> Type:
    n = ctx.arity(2, MANY)
    elems = tuple(ctx.iterable(i) for i in range(1, n))  # the iterables first: they decide the function's parameter types
    result = ctx.function(ctx.args[0], elems, "function")
    if result == NONE:
        raise ctx.error(f"{ctx.what} function must return a value", ctx.args[0])
    return GeneratorType(result)


def b_filter(ctx: CallContext) -> Type:
    ctx.arity(2)
    elem = ctx.iterable(1)
    if isinstance(ctx.args[0], A.NoneLit):  # filter(None, xs): the items that are true
        ctx.checker.check_truthy(elem, ctx.args[1])
        return GeneratorType(elem)
    result = ctx.function(ctx.args[0], (elem,), "function")
    ctx.checker.check_truthy(result, ctx.args[0])
    return GeneratorType(elem)


def b_reversed(ctx: CallContext) -> Type:
    ctx.arity(1)
    t = ctx.need(0, lambda t: isinstance(t, (ListType, TupleType)) or t == STR or t == IterType(INT, "range") or (
        isinstance(t, ClassRefType) and t.st.enum is not None),  # reversed(Color)
                 "a list, tuple, str or range")
    if isinstance(t, TupleType):
        return IterType(ctx.iterable(0), "reversed")
    return IterType(element_type(t), "reversed")


def b_enumerate(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2, keywords=("start",))
    elem = ctx.iterable(0)
    if n == 2:
        if ctx.call.keywords:
            raise ctx.error("enumerate() got multiple values for argument 'start'", ctx.call.keywords[0])
        ctx.expect(1, INT)
    ctx.keyword("start", INT)
    return GeneratorType(TupleType((INT, elem)))


def spread_items(ctx: CallContext, what: str) -> Type | None:
    """f(*rows) where f takes any number of iterables: the type of the rows' items, or None
    if the call has no starred list. (Only on its own: zip(*rows), not zip(a, *rows).)"""
    if not any(isinstance(a, A.Starred) for a in ctx.args):
        return None
    if len(ctx.args) != 1:
        raise ctx.error(f"{ctx.what} can unpack a list with '*' only when it's the only {what}", ctx.call)
    star = ctx.args[0]
    inner = element_type(star.ty)
    items = element_type(inner) if inner is not None else None
    if items is None:
        raise ctx.error(f"{ctx.what} needs iterables, not {inner}", star)
    return items


def b_zip(ctx: CallContext) -> Type:
    ctx.keyword("strict", BOOL)
    if (items := spread_items(ctx, "argument")) is not None:  # zip(*rows): tuples as long as rows
        ctx.call.notes["spread_zip"] = True
        return GeneratorType(VarTupleType(items))
    n = ctx.arity(1, MANY, keywords=("strict",))
    return GeneratorType(TupleType(tuple(ctx.iterable(i) for i in range(n))))


def b_iter(ctx: CallContext) -> Type:
    """iter(xs): an iterator over xs (what __iter__ usually returns)."""
    ctx.arity(1)
    t = ctx.arg(0)
    if isinstance(t, GeneratorType):
        return t  # iter(it) is it
    return GeneratorType(ctx.iterable(0))


def b_next(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2)
    t = ctx.arg(0)
    if t == CSV_DICT_READER:  # next(reader): its next row
        t = GeneratorType(DictType(STR, STR))
    if not isinstance(t, GeneratorType):
        raise ctx.error(f"next() needs an iterator (a generator, or iter(...)), not {t}", ctx.args[0])
    if n == 1:
        return t.elem
    d = ctx.arg(1, t.elem)
    result = join(t.elem, d)
    if result is None:
        raise ctx.error(f"next()'s default must be a {t.elem} (or None), not {d}", ctx.args[1])
    return result


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
            raise ctx.error(f"set elements must be int, float, str, bool, or a tuple of those; not {elem}" + unhashable_hint(elem))
        return SetType(elem)
    if isinstance(ctx.expected, SetType):
        return ctx.expected
    raise ctx.error("can't tell what type of set this is; annotate the variable, e.g. `s: set[int] = set()`")


def b_pow(ctx: CallContext) -> Type:
    """pow(a, b) is a ** b; pow(a, b, m) is (a ** b) % m for ints, computed efficiently."""
    n = ctx.arity(2, 3)
    if n == 3:
        for i in range(3):
            ctx.expect(i, INT)
        return INT
    for i in range(2):
        ctx.need(i, is_numeric, "a number")
    return FLOAT if FLOAT in (ctx.args[0].ty, ctx.args[1].ty) else INT


def b_tuple(ctx: CallContext) -> Type:
    if ctx.arity(0, 1):
        return VarTupleType(ctx.iterable(0))
    if isinstance(ctx.expected, VarTupleType):
        return ctx.expected
    return TupleType(())


def dict_keywords(ctx: CallContext) -> Type:
    """dict(a=1, b=2) and dict(other, c=3): the keywords are str keys."""
    if len(ctx.args) > 1:
        raise ctx.error(f"dict() takes at most 1 argument ({len(ctx.args)} given)")
    keywords, ctx.call.keywords = ctx.call.keywords, []
    try:
        result = b_dict(ctx) if ctx.args else None  # (the dict or pairs they're added to)
    finally:
        ctx.call.keywords = keywords
    if result is not None and result.key != STR:
        raise ctx.error(f"dict() keywords are str keys, but this has {result.key} keys", ctx.args[0])
    value = result.value if result is not None else ctx.expected.value if (
        isinstance(ctx.expected, DictType) and ctx.expected.key == STR) else None
    fixed = value is not None
    for kw in keywords:
        t = ctx.checker.check_expr(kw.value, value)
        if value is None:
            value = t
        elif fixed and not assignable(t, value):
            raise ctx.error(f"dict() argument '{kw.name}' must be {value}, not {t}", kw.value)
        elif not fixed:
            if (joined := join(value, t)) is None:
                raise ctx.error(f"dict() values must all be one type, not {value} and {t}", kw.value)
            value = joined
    return DictType(STR, value)


def b_dict(ctx: CallContext) -> Type:
    if ctx.call.keywords:
        return dict_keywords(ctx)
    if ctx.arity(0, 1):  # dict(other_dict) copies; dict(pairs) builds from (key, value) tuples
        hint = ctx.expected if type(ctx.expected) is DictType else None
        t = ctx.arg(0, hint)
        if isinstance(t, DictType):
            return DictType(t.key, t.value)
        pair = ctx.iterable(0)
        if not (isinstance(pair, TupleType) and len(pair.elts) == 2):
            raise ctx.error(f"dict() needs a dict or (key, value) pairs, not {t}", ctx.args[0])
        if not is_hashable(pair.elts[0]):
            raise ctx.error(f"dict keys must be int, float, str, bool, or a tuple of those; not {pair.elts[0]}" + unhashable_hint(pair.elts[0]))
        ctx.call.notes["pairs"] = True
        return DictType(*pair.elts)
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
    n = ctx.arity(0, 2, keywords=("encoding",))
    if n == 2 or ctx.call.keywords:  # bytes(text, "utf-8") is text.encode("utf-8")
        if n == 0 or ctx.arg(0) != STR:
            raise ctx.error("encoding without a string argument", ctx.args[0] if n else None)
        encoding_argument(ctx, n)
        return BYTES
    if not n:
        return BYTES
    t = ctx.arg(0)
    if t == STR:
        raise ctx.error('bytes(str) needs an encoding: bytes(s, "utf-8"), or s.encode()', ctx.args[0])
    if t != INT and element_type(t) != INT:
        raise ctx.error(f"bytes() needs a length or a list of ints (0-255), not {t}", ctx.args[0])
    mark_tuple_iterable(ctx.args[0], t, INT)
    return BYTES


def b_bytearray(ctx: CallContext) -> Type:
    """bytearray(), bytearray(n), bytearray(b"..."), bytearray(ints), bytearray(s, encoding)."""
    n = ctx.arity(0, 2, keywords=("encoding",))
    if n == 2 or ctx.call.keywords:
        if n == 0 or ctx.arg(0) != STR:
            raise ctx.error("encoding without a string argument", ctx.args[0] if n else None)
        encoding_argument(ctx, n)
        return BYTEARRAY
    if not n:
        return BYTEARRAY
    t = ctx.arg(0)
    if t == STR:
        raise ctx.error('bytearray(str) needs an encoding: bytearray(s, "utf-8")', ctx.args[0])
    if t not in (INT, BYTES, BYTEARRAY) and element_type(t) != INT:
        raise ctx.error(f"bytearray() needs a length, bytes or a list of ints (0-255), not {t}", ctx.args[0])
    mark_tuple_iterable(ctx.args[0], t, INT)
    return BYTEARRAY


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
        return ctx.args[0].ty  # round(1250, -2) is the int 1200
    return INT


FUNCTIONS: dict[str, Callable[[CallContext], Type]] = {
    "print": b_print,
    "len": b_len,
    "str": b_str,
    "repr": b_repr,
    "ascii": b_repr,
    "format": b_format,
    "__same_class__": b_same_class,
    "__class_name__": b_class_name,
    "int": b_int,
    "float": b_float,
    "bool": b_bool,
    "range": b_range,
    "abs": b_abs,
    "hash": b_hash,
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
    "iter": b_iter,
    "next": b_next,
    "set": b_set,
    "dict": b_dict,
    "tuple": b_tuple,
    "pow": b_pow,
    "any": b_any_all,
    "all": b_any_all,
    "input": b_input,
    "bytes": b_bytes,
    "bytearray": b_bytearray,
    "open": b_open,
    "ord": b_ord,
    "chr": b_chr,
    "round": b_round,
    "bin": b_digits,
    "oct": b_digits,
    "hex": b_digits,
    "divmod": b_divmod,
    "exit": b_exit,
    "quit": b_exit,
}

VALUES: dict[str, Value] = {
    "__name__": Value("__name__", STR),
}


# ---- methods on built-in types ----------------------------------------------


def prefix_method(t: Type) -> Callable[[CallContext], Type]:
    """s.startswith(prefix) / endswith: a prefix, or a tuple of them (any matches)."""

    def handler(ctx: CallContext) -> Type:
        ctx.arity(1)
        arg = ctx.arg(0)
        tuple_of = (isinstance(arg, TupleType) and arg.elts and all(assignable(e, t) for e in arg.elts)) or (
            isinstance(arg, VarTupleType) and t == STR and arg.elem == STR)
        if not (assignable(arg, t) or tuple_of):
            raise ctx.error(f"{ctx.what} argument must be {t} or a tuple of {t}, not {arg}", ctx.args[0])
        return BOOL

    return handler


class OneOf:
    """A parameter taking any of these types, which C++ overloads (or a template) take each:
    ZipFile(file) is a str, a Path or a binary file."""

    def __init__(self, *types: Type, what: str):
        self.types, self.what = types, what

    def check(self, ctx: CallContext, name: str, node: A.Expr) -> None:
        functions = [t for t in self.types if isinstance(t, FuncType)]  # (a lambda's parameter types come from it)
        actual = ctx.checker.check_expr(node, functions[0] if functions else None)
        if not any(assignable(actual, t) for t in self.types):
            raise ctx.error(f"{ctx.what} argument '{name}' must be {self.what}, not {actual}", node)


def plural_args(n: int) -> str:
    return "1 argument" if n == 1 else f"{n} arguments"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


PATH_LIKE = object()  # a parameter taking a str or a Path (sync_method checks it)


BYTES_OR_STR = object()  # a parameter taking bytes, or str (sent as UTF-8)


FILE_LIKE = object()  # a parameter taking a file descriptor (int), a socket or a file


SELECTABLE = "a file descriptor (int), a socket or a file"


def selectable(t: Type) -> bool:
    """What select(), poll() and selectors watch: an int, or something with fileno()."""
    return t in (INT, SOCKET) or isinstance(t, FileType)


def sync_method(result, *params):
    """A function or method taking `params`: (name, type) or (name, type, C++ default), where a
    type (and the result) may depend on the receiver, e.g. Queue[T].put takes a T. As in Python,
    "/" ends the positional-only parameters and "*" starts the keyword-only ones:
    sync_method(STR, ("width", INT), ("fillchar", STR, '" "s'), "/") is str.center. Codegen fills
    the parameters in order (handler.params, without the markers)."""
    positional_only = params.index("/") if "/" in params else 0
    params = tuple(p for p in params if p != "/")
    keyword_only = params.index("*") if "*" in params else len(params)
    params = tuple(p for p in params if p != "*")

    def resolve(t, receiver):
        return t(receiver) if callable(t) else t

    def handler(ctx: CallContext) -> Type:
        names = [p[0] for p in params]
        if len(ctx.args) > keyword_only:
            if keyword_only == 0:
                raise ctx.error(f"{ctx.what} takes no positional arguments")
            most = plural_args(keyword_only).replace("argument", "positional argument") if keyword_only < len(params) \
                else plural_args(keyword_only)
            raise ctx.error(f"{ctx.what} takes at most {most} ({len(ctx.args)} given)")
        for kw in ctx.call.keywords:
            if positional_only == len(params):
                raise ctx.error(f"{ctx.what} takes no keyword arguments", kw)
            if kw.name in names[:positional_only]:
                raise ctx.error(f"{ctx.what} got some positional-only arguments passed as keyword arguments: "
                                f"'{kw.name}' (pass it by position)", kw)
            if kw.name in names[positional_only:] and names.index(kw.name) < len(ctx.args):
                raise ctx.error(f"{ctx.what} got multiple values for argument '{kw.name}'", kw)
        for kw in ctx.call.keywords:
            if kw.name not in names[positional_only:]:
                raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{kw.name}'", kw)
        for i, p in enumerate(params):
            node = ctx.args[i] if i < len(ctx.args) else ctx.keyword_arg(p[0])
            if node is None:
                if len(p) < 3:
                    raise ctx.error(f"{ctx.what} is missing argument '{p[0]}'")
                continue
            want = resolve(p[1], ctx.receiver)
            if want is None:  # any value (PrettyPrinter.pformat(object))
                ctx.checker.check_expr(node)
                continue
            if want is PATH_LIKE:
                actual = ctx.checker.check_expr(node)
                if actual not in (STR, PATH):
                    raise ctx.error(f"{ctx.what} argument '{p[0]}' must be a str or Path, not {actual}", node)
                continue
            if want is BYTES_OR_STR:
                actual = ctx.checker.check_expr(node)
                if not bytes_like(actual):
                    raise ctx.error(f"{ctx.what} argument '{p[0]}' must be bytes (or str), not {actual}", node)
                continue
            if want is FILE_LIKE:
                actual = ctx.checker.check_expr(node)
                if not selectable(actual):
                    raise ctx.error(f"{ctx.what} argument '{p[0]}' must be {SELECTABLE}, not {actual}", node)
                continue
            if isinstance(want, OneOf):
                want.check(ctx, p[0], node)
                continue
            actual = ctx.checker.check_expr(node, want)
            if not assignable(actual, want):
                raise ctx.error(f"{ctx.what} argument '{p[0]}' must be {want}, not {actual}", node)
        return resolve(result, ctx.receiver)

    handler.params = params
    handler.positional_only = positional_only
    handler.keyword_only = keyword_only
    handler.resolve = resolve
    return handler


def returns(t: Type | Callable[[Type], Type], lo: int = 0, hi: int | None = None, args: tuple = ()):
    """A method taking `args` (types, or functions of the receiver type) and returning `t`."""

    def handler(ctx: CallContext) -> Type:
        if ctx.call.keywords:  # (like Python's own methods with positional-only parameters)
            raise ctx.error(f"{ctx.what} takes no keyword arguments", ctx.call.keywords[0])
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


def str_format(ctx: CallContext) -> Type:
    """"...".format(...): a literal format string is compiled into an f-string over the
    arguments (A.FormatArg), so its fields are checked here; any other is parsed at run time."""
    values = list(ctx.args) + [kw.value for kw in ctx.call.keywords]
    types = [ctx.checker.check_expr(v) for v in values]
    for v, t in zip(values, types):
        if not printable(t):
            raise ctx.error(f"{t} can't be converted to a string", v)
    fmt = ctx.call.func.value
    if isinstance(fmt, A.StrLit):
        fstring = compile_format(ctx, fmt.value, types)
        ctx.checker.check_expr(fstring)
        ctx.call.notes["format_fstring"] = fstring
    return STR


def compile_format(ctx: CallContext, fmt: str, types: list[Type]) -> A.FString:
    loc = ctx.call.loc
    positional = len(ctx.args)
    keywords = [kw.name for kw in ctx.call.keywords]
    numbering = {"next": 0, "manual": False}

    def argument(first: int | str) -> A.Expr:
        if first == "":
            if numbering["manual"]:
                raise ValueError("cannot switch from manual field specification to automatic field numbering")
            first = numbering["next"]
            numbering["next"] += 1
        elif isinstance(first, int):
            if numbering["next"]:
                raise ValueError("cannot switch from automatic field numbering to manual field specification")
            numbering["manual"] = True
        if isinstance(first, int):
            if first >= positional:
                given = f"{positional} {'was' if positional == 1 else 'were'} given"
                raise ctx.error(f"the format string needs at least {plural_args(first + 1)}, but {given}")
            index = first
        elif first in keywords:
            index = positional + keywords.index(first)
        else:
            raise ctx.error(f"the format string uses {{{first}}}, but there's no keyword argument '{first}'")
        node = A.FormatArg(index, loc=loc)
        node.ty = types[index]
        return node

    def parts(text: str, depth: int) -> list:
        if depth == 0:
            raise ValueError("Max string recursion exceeded")
        out: list = []
        for literal, name, spec, conversion in _string.formatter_parser(text):
            if literal:
                out.append(literal)
            if name is None:
                continue
            first, rest = _string.formatter_field_name_split(name)
            value = argument(first)
            for is_attribute, key in rest:  # {0.name}, {0[1]}, {0[key]}
                if is_attribute:
                    value = A.Attribute(value, key, loc=loc)
                else:
                    index = A.IntLit(key, loc=loc) if isinstance(key, int) else A.StrLit(key, loc=loc)
                    value = A.Index(value, index, loc=loc)
            if conversion not in (None, "r", "s", "a"):
                raise ValueError(f"Unknown conversion specifier {conversion}")
            if "{" in spec:
                spec = A.FString(parts(spec, depth - 1), loc=loc)
            out.append(A.FormattedValue(value, spec or None, conversion, loc=loc))
        return out

    try:
        return A.FString(parts(fmt, 2), loc=loc)
    except ValueError as err:
        raise ctx.error(f"bad format string: {err}", ctx.call.func.value) from None


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


def text_methods(t: Type, fill_default: str) -> dict:
    """The methods str and bytes share (t is STR or BYTES): searching with start/end,
    split/rsplit with maxsplit, padding..."""
    search = lambda: sync_method(INT, ("sub", t), ("start", OptionalType(INT), "std::nullopt"),
                                 ("end", OptionalType(INT), "std::nullopt"), "/")
    splits = lambda: sync_method(ListType(t), ("sep", OptionalType(t), "std::nullopt"), ("maxsplit", INT, "-1_i"))
    return {
        **{name: search() for name in ("find", "rfind", "index", "rindex", "count")},
        "split": splits(),
        "rsplit": splits(),
        **{name: sync_method(t, ("width", INT), ("fillchar", t, fill_default), "/") for name in ("ljust", "rjust", "center")},
        "zfill": sync_method(t, ("width", INT), "/"),
        "expandtabs": sync_method(t, ("tabsize", INT, "8_i")),
        **{name: returns(t, args=(t,)) for name in ("removeprefix", "removesuffix")},
        "swapcase": returns(t),
        **{name: returns(BOOL) for name in ("isascii", "istitle")},
    }


def str_translate(ctx: CallContext) -> Type:
    """s.translate(table): table maps code points to a str, a code point, or None (delete)."""
    ctx.arity(1)
    t = ctx.arg(0)
    if not (isinstance(t, DictType) and t.key == INT and strip_optional_type(t.value) in (INT, STR)):
        raise ctx.error(f"translate() takes a table from str.maketrans() (a dict[int, str | int | None]), not {t}",
                        ctx.args[0])
    return STR


def str_format_map(ctx: CallContext) -> Type:
    """s.format_map(mapping): str.format with the mapping's items as keywords."""
    ctx.arity(1)
    t = ctx.arg(0)
    if not (isinstance(t, DictType) and t.key == STR and printable(t.value)):
        raise ctx.error(f"format_map() takes a dict with str keys, not {t}", ctx.args[0])
    return STR


def str_maketrans(ctx: CallContext) -> Type:
    """str.maketrans(x, y, z) or str.maketrans({"a": "b"}): a table for s.translate()."""
    n = ctx.arity(1, 3)
    if n == 1:
        t = ctx.arg(0)
        if not (isinstance(t, DictType) and t.key == STR and strip_optional_type(t.value) == STR):
            raise ctx.error(f"str.maketrans() with one argument takes a dict[str, str], not {t}", ctx.args[0])
        return DictType(INT, OptionalType(STR))
    for i in range(n):
        ctx.expect(i, STR)
    return DictType(INT, OptionalType(INT))


def bytes_maketrans(ctx: CallContext) -> Type:
    ctx.arity(2)
    ctx.expect(0, BYTES)
    ctx.expect(1, BYTES)
    return BYTES


def bytes_fromhex(ctx: CallContext) -> Type:
    ctx.arity(1)
    ctx.expect(0, STR)
    return BYTES


# `str.maketrans(...)`, `bytes.fromhex(...)`: called on the type itself.
TYPE_FUNCTIONS = {("str", "maketrans"): str_maketrans, ("bytes", "maketrans"): bytes_maketrans,
                  ("bytes", "fromhex"): bytes_fromhex, ("bytearray", "fromhex"): returns(BYTEARRAY, 1, 1, (STR,))}


STR_METHODS = {
    **{name: returns(STR, 0, 1, (STR,)) for name in ("strip", "lstrip", "rstrip")},
    **{name: returns(STR) for name in ("upper", "lower", "title", "capitalize", "casefold")},
    **{name: returns(BOOL) for name in ("isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower",
                                        "isdecimal", "isnumeric", "isidentifier", "isprintable")},
    **{name: prefix_method(STR) for name in ("startswith", "endswith")},
    "translate": str_translate,
    "format_map": str_format_map,
    "splitlines": sync_method(ListType(STR), ("keepends", BOOL, "false")),
    **{name: returns(TupleType((STR, STR, STR)), args=(STR,)) for name in ("partition", "rpartition")},
    "replace": sync_method(STR, ("old", STR), ("new", STR), "/", ("count", INT, "-1_i")),
    "join": str_join,
    "format": str_format,
    "encode": sync_method(BYTES, ("encoding", STR, '"utf-8"s')),
}

def bytes_join(ctx: CallContext) -> Type:
    ctx.arity(1)
    elem = ctx.iterable(0)
    if elem not in (BYTES, BYTEARRAY):
        raise ctx.error(f"bytes.join() needs bytes items, not {elem}", ctx.args[0])
    return BYTES


BYTES_METHODS = {
    "decode": sync_method(STR, ("encoding", STR, '"utf-8"s')),
    "hex": returns(STR),
    **{name: prefix_method(BYTES) for name in ("startswith", "endswith")},
    **{name: returns(BYTES) for name in ("upper", "lower", "title", "capitalize")},
    **{name: returns(BYTES, 0, 1, (BYTES,)) for name in ("strip", "lstrip", "rstrip")},
    **{name: returns(BOOL) for name in ("isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower")},
    "splitlines": sync_method(ListType(BYTES), ("keepends", BOOL, "false")),
    **{name: returns(TupleType((BYTES, BYTES, BYTES)), args=(BYTES,)) for name in ("partition", "rpartition")},
    "replace": sync_method(BYTES, ("old", BYTES), ("new", BYTES), ("count", INT, "-1_i"), "/"),
    "join": bytes_join,
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
    # (`del xs[i]` and `del xs[a:b:c]`, as the checker writes them)
    "__delitem__": returns(NONE, args=(INT,)),
    "__delslice__": returns(NONE, args=(OptionalType(INT), OptionalType(INT), OptionalType(INT))),
}

def dict_update(ctx: CallContext) -> Type:
    """d.update(other), d.update(pairs), d.update(key=value): any of them, or both."""
    d: DictType = ctx.receiver
    if len(ctx.args) > 1:
        raise ctx.error(f"update expected at most 1 argument, got {len(ctx.args)}")
    if ctx.args:
        t = ctx.arg(0, DictType(d.key, d.value))
        if isinstance(t, DictType):
            if not (assignable(t.key, d.key) and assignable(t.value, d.value)):
                raise ctx.error(f"dict.update() argument must be {DictType(d.key, d.value)}, not {t}", ctx.args[0])
        else:
            pair = ctx.iterable(0)
            if not (isinstance(pair, TupleType) and len(pair.elts) == 2 and assignable(pair.elts[0], d.key)
                    and assignable(pair.elts[1], d.value)):
                raise ctx.error(f"dict.update() needs a {DictType(d.key, d.value)} or ({d.key}, {d.value}) pairs, not {t}",
                                ctx.args[0])
    for kw in ctx.call.keywords:
        if d.key != STR:
            raise ctx.error(f"dict.update() keywords are str keys, but this dict has {d.key} keys", kw)
        if not assignable(ctx.checker.check_expr(kw.value, d.value), d.value):
            raise ctx.error(f"dict.update() argument '{kw.name}' must be {d.value}, not {kw.value.ty}", kw.value)
    return NONE


def dict_fromkeys(ctx: CallContext) -> Type:
    """dict.fromkeys(keys, value): every key with the same value (None if there's no value)."""
    n = ctx.arity(1, 2)
    key = ctx.iterable(0)
    if not is_hashable(key):
        raise ctx.error(f"dict keys must be int, float, str, bool, or a tuple of those; not {key}" + unhashable_hint(key), ctx.args[0])
    hint = ctx.expected.value if isinstance(ctx.expected, DictType) else None
    if n == 2:
        value = ctx.arg(1, hint)
        if hint is not None and assignable(value, hint):
            value = hint
        return DictType(key, value)
    if not isinstance(hint, OptionalType):
        raise ctx.error("dict.fromkeys() without a value makes every value None: say what they'll hold later, "
                        "e.g. `d: dict[str, int | None] = dict.fromkeys(names)`")
    return DictType(key, hint)


TYPE_FUNCTIONS[("dict", "fromkeys")] = dict_fromkeys

DICT_METHODS = {
    "get": dict_get,
    "pop": dict_pop,
    "__delitem__": returns(NONE, args=(key_of,)),  # (`del d[k]`)
    "popitem": returns(lambda d: TupleType((d.key, d.value))),
    "setdefault": returns(value_of, args=(key_of, value_of)),
    "update": dict_update,
    "keys": returns(lambda d: IterType(d.key, "keys")),
    "values": returns(lambda d: IterType(d.value, "values")),
    "items": returns(lambda d: IterType(TupleType((d.key, d.value)), "items")),
    "clear": returns(NONE),
    "copy": returns(same),
}

def counter_counts(ctx: CallContext) -> Type:
    """Counter.update / subtract: items to count, or another mapping of counts."""
    c: CounterType = ctx.receiver
    ctx.arity(1)
    t = ctx.arg(0)
    if isinstance(t, DictType) and t.value == INT and t.key == c.key:
        ctx.call.notes["counts"] = True  # codegen: add counts, don't count keys
        return NONE
    elem = ctx.iterable(0)
    if not assignable(elem, c.key):
        raise ctx.error(f"{ctx.what} needs {c.key} items (or a Counter), not {elem}", ctx.args[0])
    return NONE


COUNTER_METHODS = {
    **DICT_METHODS,
    "most_common": sync_method(lambda c: ListType(TupleType((c.key, INT))), ("n", OptionalType(INT), "std::nullopt")),
    "elements": returns(lambda c: ListType(c.key)),
    "total": returns(INT),
    "update": counter_counts,
    "subtract": counter_counts,
}


def deque_extend(ctx: CallContext) -> Type:
    ctx.arity(1)
    elem = ctx.iterable(0)
    if not assignable(elem, ctx.receiver.elem):
        raise ctx.error(f"can't extend a {ctx.receiver} with {elem} items", ctx.args[0])
    return NONE


DEQUE_METHODS = {
    "append": returns(NONE, args=(elem_of,)),
    "appendleft": returns(NONE, args=(elem_of,)),
    "__delitem__": returns(NONE, args=(INT,)),  # (`del q[i]`)
    "pop": returns(elem_of),
    "popleft": returns(elem_of),
    "extend": deque_extend,
    "extendleft": deque_extend,
    "rotate": returns(NONE, 0, 1, (INT,)),
    "insert": returns(NONE, args=(INT, elem_of)),
    "remove": returns(NONE, args=(elem_of,)),
    "index": returns(INT, args=(elem_of,)),
    "count": returns(INT, args=(elem_of,)),
    "reverse": returns(NONE),
    "clear": returns(NONE),
    "copy": returns(same),
}

def set_others(result, lo: int = 1, hi: int = 1):
    """A set method taking other collections of the same items (any iterables, as in Python)."""

    def handler(ctx: CallContext) -> Type:
        for i in range(ctx.arity(lo, hi)):
            elem = ctx.iterable(i)
            if not assignable(elem, ctx.receiver.elem):
                raise ctx.error(f"{ctx.what} needs {ctx.receiver.elem} items, not {elem}", ctx.args[i])
        return result(ctx.receiver) if callable(result) else result

    return handler


SET_METHODS = {
    "add": returns(NONE, args=(elem_of,)),
    "remove": returns(NONE, args=(elem_of,)),
    "discard": returns(NONE, args=(elem_of,)),
    "pop": returns(elem_of),
    "clear": returns(NONE),
    "copy": returns(same),
    **{name: set_others(same, 0, MANY) for name in ("union", "intersection", "difference")},
    "symmetric_difference": set_others(same),
    **{name: set_others(NONE, 0, MANY) for name in ("update", "intersection_update", "difference_update")},
    "symmetric_difference_update": set_others(NONE),
    **{name: set_others(BOOL) for name in ("issubset", "issuperset", "isdisjoint")},
}


def tuple_search(ctx: CallContext) -> Type:
    """t.count(x) and t.index(x) on a tuple: x is compared with each item it could equal."""
    ctx.arity(1)
    x = ctx.arg(0)
    if not any(join(x, item) is not None or (is_numeric(x) and is_numeric(item)) for item in ctx.receiver.elts):
        raise ctx.error(f"a {x} can never be in a {ctx.receiver}", ctx.args[0])
    return INT


TUPLE_METHODS = {"count": tuple_search, "index": tuple_search}


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
    "fileno": returns(INT),
    **{name: returns(BOOL) for name in ("readable", "writable", "seekable", "isatty")},
    "tell": returns(INT),
    "seek": returns(INT, 1, 2, (INT, INT)),
    "truncate": returns(INT, 0, 1, (INT,)),
}
FILE_ATTRIBUTES = {"closed": lambda t: BOOL, "name": lambda t: STR, "mode": lambda t: STR}
# io.StringIO / io.BytesIO: no name or mode (as in Python), and getvalue().
MEMORY_FILE_METHODS = {**FILE_METHODS, "getvalue": returns(content)}
MEMORY_FILE_ATTRIBUTES = {"closed": FILE_ATTRIBUTES["closed"]}


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
    "as_date": returns(DATE),  # TOML's dates and times (tomllib)
    "as_time": returns(TIME),
    "as_datetime": returns(DATETIME),
    **{name: returns(BOOL) for name in ("is_null", "is_int", "is_float", "is_str", "is_bool", "is_list", "is_dict",
                                        "is_date", "is_time", "is_datetime")},
    "keys": returns(ListType(STR)),
    "values": returns(ListType(JSON_VALUE)),
    "items": returns(ListType(TupleType((STR, JSON_VALUE)))),
    "get": json_value_get,
}


# (these use sync_method, for keywords and defaults: s.split(maxsplit=1), s.find(x, 2))
STR_METHODS.update(text_methods(STR, '" "s'))
BYTES_METHODS.update(text_methods(BYTES, 'sd::bytes(" "s)'))
BYTES_METHODS["translate"] = sync_method(BYTES, ("table", OptionalType(BYTES)), "/", ("delete", BYTES, "sd::bytes()"))


def as_bytearray_type(t: Type) -> Type:
    """What a bytes method gives as a bytearray method: bytearrays where it gave bytes."""
    if t == BYTES:
        return BYTEARRAY
    if isinstance(t, ListType):
        return ListType(as_bytearray_type(t.elem))
    if isinstance(t, TupleType):
        return TupleType(tuple(as_bytearray_type(e) for e in t.elts))
    return t


def bytearray_method(handler):
    """A bytes method on a bytearray (codegen converts its result with sd::as_bytearray)."""
    def wrapped(ctx: CallContext) -> Type:
        return as_bytearray_type(handler(ctx))
    for attr in ("params", "positional_only", "keyword_only", "resolve"):  # (keywords and defaults)
        if hasattr(handler, attr):
            setattr(wrapped, attr, getattr(handler, attr))
    return wrapped


def bytearray_extend(ctx: CallContext) -> Type:
    ctx.arity(1)
    t = ctx.arg(0)
    if t not in (BYTES, BYTEARRAY) and element_type(t) != INT:
        raise ctx.error(f"bytearray.extend() takes bytes or ints (0-255), not {t}", ctx.args[0])
    mark_tuple_iterable(ctx.args[0], t, INT)
    return NONE


# The methods bytearray has of its own (members of sd::bytearray), then those it shares with bytes.
BYTEARRAY_OWN_METHODS = {
    "append": returns(NONE, args=(INT,)),
    "extend": bytearray_extend,
    "insert": returns(NONE, args=(INT, INT)),
    "pop": returns(INT, 0, 1, (INT,)),
    "remove": returns(NONE, args=(INT,)),
    "clear": returns(NONE),
    "reverse": returns(NONE),
    "copy": returns(BYTEARRAY),
}
BYTEARRAY_METHODS = {
    **{name: bytearray_method(h) for name, h in BYTES_METHODS.items()},
    **BYTEARRAY_OWN_METHODS,
    # (`del b[i]` and `del b[a:b:c]`, as the checker writes them)
    "__delitem__": returns(NONE, args=(INT,)),
    "__delslice__": returns(NONE, args=(OptionalType(INT), OptionalType(INT), OptionalType(INT))),
}


INT_METHODS = {
    "bit_length": sync_method(INT),
    "bit_count": sync_method(INT),
    "to_bytes": sync_method(BYTES, ("length", INT, "1_i"), ("byteorder", STR, '"big"s'), "*", ("signed", BOOL, "false")),
    "is_integer": sync_method(BOOL),
    "as_integer_ratio": sync_method(TupleType((INT, INT))),
}
FLOAT_METHODS = {
    "is_integer": sync_method(BOOL),
    "hex": sync_method(STR),
}
TYPE_FUNCTIONS[("int", "from_bytes")] = sync_method(INT, ("bytes", BYTES), ("byteorder", STR, '"big"s'), "*", ("signed", BOOL, "false"))
TYPE_FUNCTIONS[("float", "fromhex")] = sync_method(FLOAT, ("string", STR), "/")


# The methods and attributes of the types a standard-library module defines, by the type's class:
# the module's file (seadash/stdlib/) registers them with module_type().
MODULE_TYPE_METHODS: dict[type, Callable[[Type], dict]] = {}
MODULE_TYPE_ATTRIBUTES: dict[type, Callable[[Type], dict]] = {}
# The methods of built-in classes (a StructType with builtin=True), by C++ name: ssl.SSLContext's...
BUILTIN_CLASS_METHODS: dict[str, dict] = {}


def module_type(cls: type, methods: dict | Callable[[Type], dict] | None = None,
                attributes: dict | Callable[[Type], dict] | None = None) -> None:
    """Registers a module's type: its methods and attributes, each a table or a function from the
    type to its table (ProcessType's depend on its kind)."""
    as_lookup = lambda table: table if callable(table) else (lambda t: table)
    if methods is not None:
        MODULE_TYPE_METHODS[cls] = as_lookup(methods)
    if attributes is not None:
        MODULE_TYPE_ATTRIBUTES[cls] = as_lookup(attributes)


def method_for(t: Type, name: str) -> Callable[[CallContext], Type] | None:
    match t:
        case _ if t == JSON_VALUE:
            return JSON_VALUE_METHODS.get(name)
        case BuiltinClass():
            return t.methods.get(name)
        case FileType():
            return (MEMORY_FILE_METHODS if t.memory else FILE_METHODS).get(name)
        case ListType():
            table = LIST_METHODS
        case DequeType():
            table = DEQUE_METHODS
        case VarTupleType():
            table = {"count": returns(INT, args=(elem_of,)), "index": returns(INT, args=(elem_of,))}
        case TupleType():
            table = TUPLE_METHODS
        case StructType() if (methods := BUILTIN_CLASS_METHODS.get(t.cpp_name)) is not None:
            return methods.get(name)
        case CounterType():
            table = COUNTER_METHODS
        case DictType():
            table = DICT_METHODS
        case SetType():
            table = SET_METHODS
        case _ if t == STR:
            table = STR_METHODS
        case _ if t == BYTES:
            table = BYTES_METHODS
        case _ if t == BYTEARRAY:
            table = BYTEARRAY_METHODS
        case _ if t == INT:
            table = INT_METHODS
        case _ if t == FLOAT:
            table = FLOAT_METHODS
        case _ if (lookup := MODULE_TYPE_METHODS.get(type(t))) is not None:
            table = lookup(t)
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
    ("AttributeError", "Exception"),
    ("AssertionError", "Exception"),
    ("RuntimeError", "Exception"),
    ("NotImplementedError", "RuntimeError"),
    ("EOFError", "Exception"),
    ("FileNotFoundError", "OSError"),
    ("FileExistsError", "OSError"),
    ("PermissionError", "OSError"),
    ("IsADirectoryError", "OSError"),
    ("NotADirectoryError", "OSError"),
    ("TimeoutError", "OSError"),
    ("BlockingIOError", "OSError"),
    ("ConnectionError", "OSError"),
    ("BrokenPipeError", "ConnectionError"),
    ("ConnectionAbortedError", "ConnectionError"),
    ("ConnectionRefusedError", "ConnectionError"),
    ("ConnectionResetError", "ConnectionError"),
    ("UnicodeError", "ValueError"),
    ("StopIteration", "Exception"),
    ("UnicodeDecodeError", "UnicodeError"),
    ("UnicodeEncodeError", "UnicodeError"),
]


def make_exceptions() -> dict[str, StructType]:
    out: dict[str, StructType] = {}
    for name, base in EXCEPTION_TREE:
        out[name] = StructType(name, "class", None, base=out.get(base), builtin=True)
    # Every exception carries a message: `ValueError("bad")`, `str(e)`, `e.message`.
    empty = A.StrLit("")
    empty.ty = STR
    out["BaseException"].fields["message"] = Field("message", STR, empty, Loc(0, 0))
    # OSError("...").errno is None; an error from the OS has errno, strerror and filename.
    for name, t in (("errno", INT), ("strerror", STR), ("filename", STR)):
        none = A.NoneLit()
        none.ty = NONE
        out["OSError"].fields[name] = Field(name, OptionalType(t), none, Loc(0, 0))
    return out


EXCEPTIONS: dict[str, StructType] = make_exceptions()
BASE_EXCEPTION = EXCEPTIONS["BaseException"]


# ---- shared by the modules' typing (seadash/stdlib/) ----------------------------


def cpp_string_literal(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"s'


def runtime_module(name: str, header: str, libs: tuple[str, ...] = (), **members) -> Module:
    """Functions are (handler, "C++ name"); constants are (Type, "C++ expression")."""
    table: dict[str, Function | Value | StructType] = {}
    for member, spec in members.items():
        if isinstance(spec, (StructType, NamedType)):
            table[member] = spec
        elif isinstance(spec[0], Type):
            table[member] = Value(member, spec[0], spec[1])
        else:
            table[member] = Function(member, spec[0], spec[1])
    return Module(name, table, header, libs)


# Members of a built-in class itself (Path.cwd(), datetime.now(), timezone.utc), added by its module's file.
CLASS_MEMBERS: dict[Type, dict[str, Function | Value]] = {}


def class_function(name: str, result: Type, cpp: str, *params) -> Function:
    return Function(name, signature(result, *params), cpp, params)


def builtin_struct(name: str, cpp: str, fields: dict[str, Type]) -> StructType:
    """A built-in class with plain fields (os.stat's stat_result, shutil.disk_usage's usage)."""
    st = StructType(name, "struct", None, builtin=True, cpp_name=cpp)
    for fname, ft in fields.items():
        st.fields[fname] = Field(fname, ft, None, Loc(0, 0))
    return st


def exception_class(name: str, cpp: str, base: str | StructType = "Exception") -> StructType:
    """A built-in exception class; its base is a built-in exception's name or another class."""
    return StructType(name, "class", None, base=EXCEPTIONS[base] if isinstance(base, str) else base, builtin=True,
                      cpp_name=cpp)


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
            if p[1] is None:  # any value (pprint.pformat(object))
                ctx.checker.check_expr(node)
                continue
            if p[1] is PATH_LIKE:
                actual = ctx.checker.check_expr(node)
                if actual not in (STR, PATH):
                    raise ctx.error(f"{ctx.what} argument '{p[0]}' must be a str or Path, not {actual}", node)
                continue
            if isinstance(p[1], OneOf):
                p[1].check(ctx, p[0], node)
                continue
            actual = ctx.checker.check_expr(node, p[1])
            if not assignable(actual, p[1]):
                raise ctx.error(f"{ctx.what} argument '{p[0]}' must be {p[1]}, not {actual}", node)
        return result

    handler.params = params
    return handler


def module_with_params(mod: Module) -> Module:
    for m in mod.members.values():
        if isinstance(m, Function) and hasattr(m.check, "params"):
            m.params = m.check.params
    return mod


@dataclass
class SyncTypeDef:
    """threading.Lock, queue.Queue, ...: callable to construct one, and usable as a type."""

    kind: str  # a key of types.SYNC_CPP


def needing(module: str, handler):
    """A method whose code is in another module's header (and libraries): checking a call
    to it brings that module into the program, imported or not."""

    def check(ctx: CallContext) -> Type:
        ctx.checker.needed_modules.append(MODULES[module])
        return handler(ctx)

    check.__dict__.update(handler.__dict__)  # (its params, for codegen)
    return check


def elem0(t) -> Type:
    return t.args[0]


OPT_FLOAT = OptionalType(FLOAT)
OPT_STR = OptionalType(STR)
ADDRESS = TupleType((STR, INT))  # a socket address: (host, port)
# seadash's own thread-safe types aren't in Python's threading module: they're imported from seadash.
SEADASH_THREAD_TYPES = ("Mutex", "RWMutex", "Atomic", "Synchronized")


def missing_member(mod: Module, member: str) -> str:
    """The error for `module.member` that doesn't exist (with a hint where one helps)."""
    if mod.name == "threading" and member in SEADASH_THREAD_TYPES:
        return f"{member} is seadash's own, not Python's: `from seadash import {member}`"
    return f"module '{mod.name}' has no member '{member}'"


@dataclass
class CollectionTypeDef:
    """collections.defaultdict / Counter / deque: callable to make one, and a type name."""

    kind: str


def bind_args(ctx: CallContext, params: tuple) -> dict[str, A.Expr]:
    """Positional and keyword arguments by parameter name, with Python's errors."""
    names = [p[0] for p in params]
    if len(ctx.args) > len(params):
        raise ctx.error(f"{ctx.what} takes at most {plural_args(len(params))} ({len(ctx.args)} given)")
    bound = dict(zip(names, ctx.args))
    for kw in ctx.call.keywords:
        if kw.name not in names:
            raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{kw.name}'", kw)
        if kw.name in bound:
            raise ctx.error(f"{ctx.what} got multiple values for argument '{kw.name}'", kw)
        bound[kw.name] = kw.value
    for p in params:
        if p[0] not in bound and len(p) < 3:
            raise ctx.error(f"{ctx.what} is missing argument '{p[0]}'")
    return bound


def type_attributes(t: Type) -> dict | None:
    if isinstance(t, BuiltinClass):
        return t.attributes
    if isinstance(t, DequeType):
        return {"maxlen": lambda t: OptionalType(INT)}
    if isinstance(t, FileType):
        return MEMORY_FILE_ATTRIBUTES if t.memory else FILE_ATTRIBUTES
    if (lookup := MODULE_TYPE_ATTRIBUTES.get(type(t))) is not None:
        return lookup(t)
    return None


def strip_optional_type(t: Type) -> Type:
    return t.inner if isinstance(t, OptionalType) else t


def iterable_of(ctx: CallContext, node: A.Expr, what: str = "argument") -> Type:
    t = ctx.checker.check_expr(node)
    elem = element_type(t)
    if elem is None:
        raise ctx.error(f"{ctx.what} {what} must be something you can loop over, not {t}{mixed_tuple_hint(t)}", node)
    mark_tuple_iterable(node, t, elem)
    return elem


def record_spawn(ctx: CallContext, fn: A.Expr, arg_nodes: list[A.Expr], arg_types: tuple, result: Type) -> None:
    args = A.TupleLit(list(arg_nodes), loc=ctx.call.loc)
    args.ty = TupleType(tuple(arg_types))
    ctx.call.notes["spawn_extra"] = {"target": fn, "args": args if arg_nodes else None, "result": result}
    ctx.checker.spawns.append((ctx.call, ctx.checker.scope, ctx.checker.module_name))


def work_function(ctx: CallContext, fn: A.Expr, params: tuple) -> Type:
    ft = ctx.checker.check_expr(fn, FuncType(params, None))
    if not isinstance(ft, FuncType):
        raise ctx.error(f"{ctx.what} needs a function to run, not {ft}", fn)
    if len(ft.params) != len(params) and (filled := filled_for(ft, params)) is not None:
        fn.notes["fill_to"] = ft = filled  # pool.submit(work) with work(n=3): the defaults filled in
    if len(ft.params) != len(params) or not all(assignable(a, p) for a, p in zip(params, ft.params)):
        takes = ", ".join(map(str, ft.params)) or "no arguments"
        given = ", ".join(map(str, params)) or "none"
        raise ctx.error(f"the function takes ({takes}), but it's given ({given})", fn)
    return ft.ret


MODULES["sys"].members.update({
    "stdout": Value("stdout", TEXT_FILE, "sd::std_stream(1)"),
    "stderr": Value("stderr", TEXT_FILE, "sd::std_stream(2)"),
    "stdin": Value("stdin", TEXT_FILE, "sd::std_stream(0)"),
    "platform": Value("platform", STR, "std::string(SD_PLATFORM)"),
    "maxsize": Value("maxsize", INT, "std::int64_t{INT64_MAX}"),
})


class AttributeUnavailable(Exception):
    """An attribute that exists, but not for this value (the checker adds the location)."""


@dataclass
class DecoratorName:
    """dataclasses.dataclass, dataclasses.field, functools.cache, functools.lru_cache:
    understood by the checker rather than called."""

    name: str


# The standard-library modules' typing, one file each: importing them registers them in MODULES
# (and their names here, which the checker and codegen use as builtins.X).
from .stdlib import *  # noqa: E402,F401,F403
