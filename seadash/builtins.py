"""Built-in functions, methods on built-in types, and built-in modules.

Each built-in is a small handler that receives a CallContext, checks the
arguments, and returns the result type. Keeping them as a table means adding
a built-in is one function here plus (later) one line of code generation.
"""

from __future__ import annotations

import _string  # Python's own format-string parser, for str.format()
from collections.abc import Callable
from dataclasses import dataclass

from . import ast as A
from .errors import CheckError
from .errors import Loc
from .types import (
    BOOL, BYTES, FLOAT, INT, JSON_VALUE, NONE, SOCKET, STR,
    BINARY_FILE, TEXT_FILE,
    CounterType, DefaultDictType, DequeType, MatchType, PatternType, ProcessType, RegexInfo, PATH, TEMPDIR,
    DATE, DATETIME, TIME, TIMEDELTA, TIMEZONE, PARSER, NamespaceType, ParserType, SubParsersType, VarTupleType,
    GeneratorType, TEXT_WRAPPER, STR_TEMPLATE, HASH, HMAC_T, EXECUTOR, FutureType, LOGGER, LOG_HANDLER, LOG_FORMATTER, UUID_T,
    BuiltinClass, ZLIB_COMPRESS, ZLIB_DECOMPRESS, BZ2_COMPRESSOR, BZ2_DECOMPRESSOR, LZMA_COMPRESSOR, LZMA_DECOMPRESSOR,
    SQLITE_CONNECTION, SQLITE_CURSOR, StructFormatType,
    CSV_WRITER, CSV_DICT_READER, CSV_DICT_WRITER, HTTP_RESPONSE, HTTP_CONNECTION, HTTP_HEADERS, URL_REQUEST, URL_PARTS,
    DictType, Field, FileType, SyncType, FuncType, user_dunder, IterType, ListType, ModuleType, OptionalType, SetType, StructType, TupleType, Type,
    ClassAttr, ClassRefType, FuncInfo, HTTPServerType, Param, Var,
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
            raise self.error(f"{self.what} argument must be something you can loop over, not {t}{mixed_tuple_hint(t)}",
                             self.args[i])
        mark_tuple_iterable(self.args[i], t, elem)
        return elem


def mark_tuple_iterable(node, t: Type, elem: Type) -> None:
    """A tuple used as an iterable is converted to a list of its common element type
    (codegen reads this mark)."""
    if isinstance(t, TupleType):
        node.tuple_elem = elem


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
    return not isinstance(t, IterType)


def sized(t: Type) -> bool:
    return t in (STR, BYTES, JSON_VALUE, HTTP_HEADERS) or isinstance(t, (ListType, DictType, SetType, TupleType, DequeType, VarTupleType)) or bool(
        user_dunder(t, "__len__")
    ) or (isinstance(t, IterType) and t.kind in ("keys", "values", "items", "range"))  # len(d.keys()), len(range(n))


def ordered(t: Type) -> bool:
    return t in (INT, FLOAT, STR, BYTES, PATH, DATE, TIME, DATETIME, TIMEDELTA, UUID_T) or isinstance(t, (TupleType, ListType, VarTupleType)) or bool(user_dunder(t, "__lt__"))


def bytes_like(t: Type) -> bool:
    return t in (BYTES, STR)


# ---- built-in functions -----------------------------------------------------


def b_print(ctx: CallContext) -> Type:
    ctx.arity(0, MANY, keywords=("sep", "end", "file", "flush"))
    for i in range(len(ctx.args)):
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
    ctx.need(0, sized, "a str, list, dict, set or tuple")
    return INT


def b_str(ctx: CallContext) -> Type:
    n = ctx.arity(0, 2, keywords=("encoding",))
    if n == 2 or ctx.call.keywords:  # str(data, "utf-8") is data.decode("utf-8")
        if n == 0 or ctx.arg(0) != BYTES:
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
    if t not in (INT, BOOL):
        raise ctx.error(f"'{t}' object cannot be interpreted as an integer", ctx.args[0])
    return STR


def b_divmod(ctx: CallContext) -> Type:
    ctx.arity(2)
    types = [ctx.arg(i) for i in range(2)]
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
    t = ctx.need(0, lambda t: isinstance(t, (ListType, TupleType)) or t == STR or t == IterType(INT, "range"),
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


def b_zip(ctx: CallContext) -> Type:
    n = ctx.arity(1, MANY, keywords=("strict",))
    ctx.keyword("strict", BOOL)
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
            raise ctx.error(f"set elements must be int, float, str, bool, or a tuple of those; not {elem}")
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
            raise ctx.error(f"dict keys must be int, float, str, bool, or a tuple of those; not {pair.elts[0]}")
        ctx.call.pairs = True
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
        ctx.call.format_fstring = fstring
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
                                 ("end", OptionalType(INT), "std::nullopt"))
    splits = lambda: sync_method(ListType(t), ("sep", OptionalType(t), "std::nullopt"), ("maxsplit", INT, "-1_i"))
    return {
        **{name: search() for name in ("find", "rfind", "index", "rindex", "count")},
        "split": splits(),
        "rsplit": splits(),
        **{name: sync_method(t, ("width", INT), ("fillchar", t, fill_default)) for name in ("ljust", "rjust", "center")},
        "zfill": sync_method(t, ("width", INT)),
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
                  ("bytes", "fromhex"): bytes_fromhex}


STR_METHODS = {
    **{name: returns(STR, 0, 1, (STR,)) for name in ("strip", "lstrip", "rstrip")},
    **{name: returns(STR) for name in ("upper", "lower", "title", "capitalize", "casefold")},
    **{name: returns(BOOL) for name in ("isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower",
                                        "isdecimal", "isnumeric", "isidentifier", "isprintable")},
    **{name: returns(BOOL, args=(STR,)) for name in ("startswith", "endswith")},
    "translate": str_translate,
    "format_map": str_format_map,
    "splitlines": returns(ListType(STR)),
    **{name: returns(TupleType((STR, STR, STR)), args=(STR,)) for name in ("partition", "rpartition")},
    "replace": returns(STR, args=(STR, STR)),
    "join": str_join,
    "format": str_format,
    "encode": returns(BYTES, 0, 1, (STR,)),
}

def bytes_join(ctx: CallContext) -> Type:
    ctx.arity(1)
    elem = ctx.iterable(0)
    if elem != BYTES:
        raise ctx.error(f"bytes.join() needs bytes items, not {elem}", ctx.args[0])
    return BYTES


BYTES_METHODS = {
    "decode": returns(STR, 0, 1, (STR,)),
    "hex": returns(STR),
    **{name: returns(BOOL, args=(BYTES,)) for name in ("startswith", "endswith")},
    **{name: returns(BYTES) for name in ("upper", "lower", "title", "capitalize")},
    **{name: returns(BYTES, 0, 1, (BYTES,)) for name in ("strip", "lstrip", "rstrip")},
    **{name: returns(BOOL) for name in ("isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower")},
    "splitlines": returns(ListType(BYTES)),
    **{name: returns(TupleType((BYTES, BYTES, BYTES)), args=(BYTES,)) for name in ("partition", "rpartition")},
    "replace": returns(BYTES, args=(BYTES, BYTES)),
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
        raise ctx.error(f"dict keys must be int, float, str, bool, or a tuple of those; not {key}", ctx.args[0])
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
        ctx.call.counts = True  # codegen: add counts, don't count keys
        return NONE
    elem = ctx.iterable(0)
    if not assignable(elem, c.key):
        raise ctx.error(f"{ctx.what} needs {c.key} items (or a Counter), not {elem}", ctx.args[0])
    return NONE


COUNTER_METHODS = {
    **DICT_METHODS,
    "most_common": returns(lambda c: ListType(TupleType((c.key, INT))), 0, 1, (INT,)),
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
    if isinstance(t, SyncType):
        return SYNC_METHODS[t.kind].get(name)
    match t:
        case _ if t == JSON_VALUE:
            return JSON_VALUE_METHODS.get(name)
        case BuiltinClass():
            return t.methods.get(name)
        case FileType():
            return FILE_METHODS.get(name)
        case ListType():
            table = LIST_METHODS
        case DequeType():
            table = DEQUE_METHODS
        case VarTupleType():
            table = {"count": returns(INT, args=(elem_of,)), "index": returns(INT, args=(elem_of,))}
        case TupleType():
            table = TUPLE_METHODS
        case HTTPServerType():
            return HTTP_SERVER_METHODS.get(name)
        case ParserType():
            return PARSER_METHODS.get(name)
        case SubParsersType():
            return {"add_parser": subparsers_add_parser}.get(name)
        case StructType() if (methods := EXCEPTION_METHODS.get(t.cpp_name)) is not None:
            return methods.get(name)
        case FutureType():
            return FUTURE_METHODS.get(name)
        case StructFormatType():
            return STRUCT_METHODS.get(name)
        case ProcessType(kind):
            table = PROCESS_METHODS[kind]
        case PatternType():
            table = PATTERN_METHODS
        case MatchType():
            table = MATCH_METHODS
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
        case _ if t == INT:
            table = INT_METHODS
        case _ if t == FLOAT:
            table = FLOAT_METHODS
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


# ---- modules backed by runtime headers --------------------------------------


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

ZLIB_CONSTANTS = {
    "Z_BEST_SPEED": 1, "Z_BEST_COMPRESSION": 9, "Z_DEFAULT_COMPRESSION": -1, "Z_NO_FLUSH": 0, "Z_PARTIAL_FLUSH": 1,
    "Z_SYNC_FLUSH": 2, "Z_FULL_FLUSH": 3, "Z_FINISH": 4, "Z_BLOCK": 5, "DEFLATED": 8, "MAX_WBITS": 15, "DEF_MEM_LEVEL": 8,
    "DEF_BUF_SIZE": 16384, "Z_DEFAULT_STRATEGY": 0, "Z_FILTERED": 1, "Z_HUFFMAN_ONLY": 2, "Z_RLE": 3, "Z_FIXED": 4,
}


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
            if p[1] is PATH_LIKE:
                actual = ctx.checker.check_expr(node)
                if actual not in (STR, PATH):
                    raise ctx.error(f"{ctx.what} argument '{p[0]}' must be a str or Path, not {actual}", node)
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
    chmod=(signature(NONE, ("path", STR), ("mode", INT)), "sd::os::chmod"),
    rename=(signature(NONE, ("src", STR), ("dst", STR)), "sd::os::rename"),
    getenv=(os_getenv, "sd::os::getenv"),
    sep=(STR, "sd::os::path::sep()"),
    # file descriptors (the program closes what it opens)
    open=(signature(INT, ("path", STR), ("flags", INT), ("mode", INT, "511_i")), "sd::os::open"),
    close=(signature(NONE, ("fd", INT)), "sd::os::close"),
    read=(signature(BYTES, ("fd", INT), ("n", INT)), "sd::os::read"),
    write=(signature(INT, ("fd", INT), ("data", BYTES)), "sd::os::write"),
    dup=(signature(INT, ("fd", INT)), "sd::os::dup"),
    pipe=(signature(TupleType((INT, INT))), "sd::os::pipe"),
    fdopen=(os_fdopen, None),
    **{c: (INT, f"static_cast<std::int64_t>({c})") for c in (
        "O_RDONLY", "O_WRONLY", "O_RDWR", "O_CREAT", "O_EXCL", "O_TRUNC", "O_APPEND", "O_NONBLOCK", "O_CLOEXEC")},
))
MODULES["os"].members["path"] = OS_PATH

MODULES["typing"] = Module("typing", {
    name: TypeAlias(name) for name in ("Callable", "TextIO", "BinaryIO", "Optional", "List", "Dict", "Set", "Tuple", "Iterator", "Iterable")
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

MODULES["__future__"] = Module("__future__", {"annotations": TypeAlias("annotations")})  # accepted, no effect

MODULES["time"] = module_with_params(runtime_module(
    "time", "modules/time.hpp",
    perf_counter=(signature(FLOAT), "sd::time::perf_counter"),
    monotonic=(signature(FLOAT), "sd::time::monotonic"),
    time=(signature(FLOAT), "sd::time::time"),
    sleep=(signature(NONE, ("secs", FLOAT)), "sd::time::sleep"),
))


# ---- random -------------------------------------------------------------------


def sequence_elem(t: Type) -> Type | None:
    """What indexing a sequence gives, for choice()/sample(): lists, str, bytes, range, uniform tuples."""
    match t:
        case ListType(elem):
            return elem
        case IterType(elem, "range"):
            return elem
        case TupleType(elts) if elts and len(set(elts)) == 1:
            return elts[0]
    if t == STR:
        return STR
    if t == BYTES:
        return INT
    return None


def sequence_arg(ctx: CallContext, i: int) -> Type:
    t = ctx.arg(i)
    elem = sequence_elem(t)
    if elem is None:
        raise ctx.error(f"{ctx.what} needs a sequence (a list, str, bytes, range or tuple), not {t}", ctx.args[i])
    return elem


def random_randrange(ctx: CallContext) -> Type:
    for i in range(ctx.arity(1, 3)):
        ctx.expect(i, INT)
    return INT


def random_choice(ctx: CallContext) -> Type:
    ctx.arity(1)
    return sequence_arg(ctx, 0)


def random_shuffle(ctx: CallContext) -> Type:
    ctx.arity(1)
    t = ctx.arg(0)
    if not isinstance(t, ListType):
        raise ctx.error(f"{ctx.what} shuffles a list in place, not a {t}", ctx.args[0])
    return NONE


def random_sample(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2, keywords=("k",))
    elem = sequence_arg(ctx, 0)
    k = ctx.args[1] if n == 2 else ctx.keyword_arg("k")
    if k is None:
        raise ctx.error(f"{ctx.what} is missing argument 'k'")
    ctx.checker.expect_type(k, INT, "sample size")
    return ListType(elem)


CHOICES_PARAMS = (
    ("population", None),
    ("weights", OptionalType(ListType(FLOAT)), "std::nullopt"),
    ("cum_weights", OptionalType(ListType(FLOAT)), "std::nullopt"),
    ("k", INT, "1_i"),
)


def random_choices(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2, keywords=("weights", "cum_weights", "k"))
    elem = sequence_arg(ctx, 0)
    weights = ctx.args[1] if n == 2 else ctx.keyword_arg("weights")
    for node in (weights, ctx.keyword_arg("cum_weights")):
        if node is not None:
            t = ctx.checker.check_expr(node, ListType(FLOAT))
            if not assignable(t, OptionalType(ListType(FLOAT))):
                raise ctx.error(f"{ctx.what} weights must be a list[float], not {t}", node)
    ctx.keyword("k", INT)
    return ListType(elem)


random_choices.params = CHOICES_PARAMS
random_sample.params = (("population", None), ("k", INT))

MODULES["random"] = module_with_params(runtime_module(
    "random", "modules/random.hpp",
    seed=(signature(NONE, ("a", OptionalType(INT), "std::nullopt")), "sd::random::seed"),
    random=(signature(FLOAT), "sd::random::random"),
    randint=(signature(INT, ("a", INT), ("b", INT)), "sd::random::randint"),
    randrange=(random_randrange, "sd::random::randrange"),
    uniform=(signature(FLOAT, ("a", FLOAT), ("b", FLOAT)), "sd::random::uniform"),
    gauss=(signature(FLOAT, ("mu", FLOAT, "0.0"), ("sigma", FLOAT, "1.0")), "sd::random::gauss"),
    getrandbits=(signature(INT, ("k", INT)), "sd::random::getrandbits"),
    randbytes=(signature(BYTES, ("n", INT)), "sd::random::randbytes"),
    choice=(random_choice, "sd::random::choice"),
    choices=(random_choices, "sd::random::choices"),
    shuffle=(random_shuffle, "sd::random::shuffle"),
    sample=(random_sample, "sd::random::sample"),
))
MODULES["random"].members["shuffle"].mutates_first_arg = True


# ---- threading and queue ----------------------------------------------------------


@dataclass
class SyncTypeDef:
    """threading.Lock, queue.Queue, ...: callable to construct one, and usable as a type."""

    kind: str  # a key of types.SYNC_CPP


def sync_method(result, *params):
    """A method on a threading/queue type. Types (and the result) may depend on the receiver,
    e.g. Queue[T].put takes a T. Keyword arguments work; codegen fills parameters in order."""

    def resolve(t, receiver):
        return t(receiver) if callable(t) else t

    def handler(ctx: CallContext) -> Type:
        names = [p[0] for p in params]
        if len(ctx.args) > len(params):
            raise ctx.error(f"{ctx.what} takes at most {plural_args(len(params))} ({len(ctx.args)} given)")
        for kw in ctx.call.keywords:
            if kw.name in names and names.index(kw.name) < len(ctx.args):
                raise ctx.error(f"{ctx.what} got multiple values for argument '{kw.name}'", kw)
        for kw in ctx.call.keywords:
            if kw.name not in names:
                raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{kw.name}'", kw)
        for i, p in enumerate(params):
            node = ctx.args[i] if i < len(ctx.args) else ctx.keyword_arg(p[0])
            if node is None:
                if len(p) < 3:
                    raise ctx.error(f"{ctx.what} is missing argument '{p[0]}'")
                continue
            want = resolve(p[1], ctx.receiver)
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
            actual = ctx.checker.check_expr(node, want)
            if not assignable(actual, want):
                raise ctx.error(f"{ctx.what} argument '{p[0]}' must be {want}, not {actual}", node)
        return resolve(result, ctx.receiver)

    handler.params = params
    handler.resolve = resolve
    return handler


# (these use sync_method, for keywords and defaults: s.split(maxsplit=1), s.find(x, 2))
STR_METHODS.update(text_methods(STR, '" "s'))
BYTES_METHODS.update(text_methods(BYTES, 'sd::bytes(" "s)'))
BYTES_METHODS["translate"] = sync_method(BYTES, ("table", OptionalType(BYTES)), ("delete", BYTES, "sd::bytes()"))

INT_METHODS = {
    "bit_length": sync_method(INT),
    "bit_count": sync_method(INT),
    "to_bytes": sync_method(BYTES, ("length", INT, "1_i"), ("byteorder", STR, '"big"s'), ("signed", BOOL, "false")),
    "is_integer": sync_method(BOOL),
    "as_integer_ratio": sync_method(TupleType((INT, INT))),
}
FLOAT_METHODS = {
    "is_integer": sync_method(BOOL),
    "hex": sync_method(STR),
}
TYPE_FUNCTIONS[("int", "from_bytes")] = sync_method(INT, ("bytes", BYTES), ("byteorder", STR, '"big"s'), ("signed", BOOL, "false"))
TYPE_FUNCTIONS[("float", "fromhex")] = sync_method(FLOAT, ("string", STR))


def plural_args(n: int) -> str:
    return "1 argument" if n == 1 else f"{n} arguments"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def elem0(t) -> Type:
    return t.args[0]


OPT_FLOAT = OptionalType(FLOAT)

SYNC_METHODS: dict[str, dict] = {
    "Lock": {
        "acquire": sync_method(BOOL, ("blocking", BOOL, "true"), ("timeout", FLOAT, "-1.0")),
        "release": sync_method(NONE),
        "locked": sync_method(BOOL),
    },
    "RLock": {
        "acquire": sync_method(BOOL, ("blocking", BOOL, "true"), ("timeout", FLOAT, "-1.0")),
        "release": sync_method(NONE),
    },
    "Event": {
        "set": sync_method(NONE),
        "clear": sync_method(NONE),
        "is_set": sync_method(BOOL),
        "wait": sync_method(BOOL, ("timeout", OPT_FLOAT, "std::nullopt")),
    },
    "Atomic": {
        "get": sync_method(INT),
        "set": sync_method(NONE, ("value", INT)),
        "add": sync_method(INT, ("n", INT, "1_i")),
        "sub": sync_method(INT, ("n", INT, "1_i")),
        "compare_and_set": sync_method(BOOL, ("expected", INT), ("value", INT)),
    },
    "Mutex": {
        "get": sync_method(elem0),
        "set": sync_method(NONE, ("value", elem0)),
    },
    "RWMutex": {
        "get": sync_method(elem0),
        "set": sync_method(NONE, ("value", elem0)),
        "read": sync_method(lambda r: SyncType("RWRead", r.args)),
        "write": sync_method(lambda r: SyncType("RWWrite", r.args)),
    },
    "Queue": {
        "put": sync_method(NONE, ("item", elem0), ("block", BOOL, "true"), ("timeout", OPT_FLOAT, "std::nullopt")),
        "get": sync_method(elem0, ("block", BOOL, "true"), ("timeout", OPT_FLOAT, "std::nullopt")),
        "put_nowait": sync_method(NONE, ("item", elem0)),
        "get_nowait": sync_method(elem0),
        "empty": sync_method(BOOL),
        "full": sync_method(BOOL),
        "qsize": sync_method(INT),
        "task_done": sync_method(NONE),
        "join": sync_method(NONE),
    },
    "Thread": {
        "start": sync_method(NONE),
        "join": sync_method(NONE, ("timeout", OPT_FLOAT, "std::nullopt")),
        "is_alive": sync_method(BOOL),
    },
}
THREAD_ATTRIBUTES = {"name": STR, "daemon": BOOL}

SYNCHRONIZED = StructType("Synchronized", "class", None, builtin=True, cpp_name="sd::threading::Synchronized")

MODULES["threading"] = Module("threading", {
    **{kind: SyncTypeDef(kind) for kind in ("Thread", "Lock", "RLock", "Event")},
}, "modules/threading.hpp", ("pthread",))
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


MODULES["collections"] = Module("collections", {
    name: CollectionTypeDef(name) for name in ("defaultdict", "Counter", "deque")
}, "modules/collections.hpp")

# ---- re -----------------------------------------------------------------------------
#
# Every operation is typed by regex_op(), whether it's re.search(pattern, s) or
# compiled.search(s). What's known about a literal pattern (regex.analyze) makes the
# results precise: m.group(1) is str when group 1 always matches.

REGEX_OPS = {  # operation -> its parameters after the pattern: (name, type[, has default])
    "search": (("string", STR), ("pos", INT, True), ("endpos", INT, True)),
    "match": (("string", STR), ("pos", INT, True), ("endpos", INT, True)),
    "fullmatch": (("string", STR), ("pos", INT, True), ("endpos", INT, True)),
    "findall": (("string", STR),),
    "finditer": (("string", STR),),
    "sub": (("repl", None), ("string", STR), ("count", INT, True)),
    "subn": (("repl", None), ("string", STR), ("count", INT, True)),
    "split": (("string", STR), ("maxsplit", INT, True)),
}


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


def group_type(info: RegexInfo | None, group: int | None) -> Type:
    if group == 0:
        return STR
    if info is None or group is None or group in info.optional:
        return OptionalType(STR)
    return STR


def regex_op(ctx: CallContext, op: str, info: RegexInfo | None, args: dict[str, A.Expr]) -> Type:
    """Check the arguments of a pattern operation (all but the pattern) and give its result."""
    for name, t, *_ in REGEX_OPS[op]:
        if name in args and t is not None:
            actual = ctx.checker.check_expr(args[name], t)
            if not assignable(actual, t):
                raise ctx.error(f"{ctx.what} argument '{name}' must be {t}, not {actual}", args[name])
    match op:
        case "search" | "match" | "fullmatch":
            return OptionalType(MatchType(info))
        case "findall":
            if info is not None and info.groups > 1:
                return ListType(TupleType((STR,) * info.groups))
            return ListType(STR)
        case "finditer":
            return ListType(MatchType(info))
        case "sub" | "subn":
            repl = args["repl"]
            rt = ctx.checker.check_expr(repl, FuncType((MatchType(info),), STR))
            if isinstance(rt, FuncType):
                if rt.params != (MatchType(info),) or rt.ret != STR:
                    raise ctx.error(f"{ctx.what} replacement function must take a re.Match and return str, not {rt}", repl)
                ctx.call.regex_repl_fn = True
            elif rt != STR:
                raise ctx.error(f"{ctx.what} replacement must be a str or a function, not {rt}", repl)
            return STR if op == "sub" else TupleType((STR, INT))
        case "split":
            if info is not None and info.optional:
                return ListType(OptionalType(STR))  # a group that didn't match is None
            return ListType(STR)
    raise AssertionError(op)


def constant_flags(ctx: CallContext, node: A.Expr | None) -> int | None:
    """re.I | re.M and friends, when they're written out; None if computed at run time."""
    from .regex import FLAGS
    if node is None:
        return 0
    match node:
        case A.IntLit(v):
            return v
        case A.Attribute(A.Name(mod), attr) if ctx.checker.modules.get(mod) is MODULES["re"] and attr in FLAGS:
            return FLAGS[attr]
        case A.Name(name) if name in ctx.checker.imported and ctx.checker.imported[name][0] is MODULES["re"]:
            return FLAGS.get(ctx.checker.imported[name][1])
        case A.BinOp(left, "|", right):
            lv, rv = constant_flags(ctx, left), constant_flags(ctx, right)
            return lv | rv if lv is not None and rv is not None else None
    return None


def regex_pattern(ctx: CallContext, node: A.Expr, flags_node: A.Expr | None) -> RegexInfo | None:
    """Check the pattern (and flags) arguments; analyze the pattern if it's a literal."""
    import re as pyre
    from .regex import analyze
    ctx.checker.expect_type(node, STR, f"{ctx.what} pattern")
    if flags_node is not None:
        ctx.checker.expect_type(flags_node, INT, f"{ctx.what} flags")
    flags = constant_flags(ctx, flags_node)
    if not isinstance(node, A.StrLit):
        return None
    try:
        info = analyze(node.value, flags or 0)
    except pyre.error as e:
        if flags is None:  # the flags (say, VERBOSE) aren't known until run time
            return None
        raise ctx.error(f"invalid regular expression: {e}", node)
    ctx.call.regex_static = flags  # known flags: compile once, into a static
    return info


def re_function(op: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        if op == "escape":
            ctx.arity(1)
            ctx.expect(0, STR)
            return STR
        if op == "purge":
            ctx.arity(0)
            return NONE
        rest = REGEX_OPS.get(op, ())
        # Python's module functions take the operation's arguments but not pos/endpos.
        rest = tuple(p for p in rest if p[0] not in ("pos", "endpos"))
        params = (("pattern", STR), *rest, ("flags", INT, True))
        args = bind_args(ctx, params)
        info = regex_pattern(ctx, args["pattern"], args.get("flags"))
        ctx.call.regex_args = args
        if op == "compile":
            return PatternType(info)
        return regex_op(ctx, op, info, args)

    return handler


def pattern_method(op: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        args = bind_args(ctx, REGEX_OPS[op])
        ctx.call.regex_args = args
        return regex_op(ctx, op, ctx.receiver.info, args)

    return handler


def match_group_arg(ctx: CallContext, node: A.Expr) -> Type:
    """One argument of m.group(...), m[...], m.start(...): a group number or name."""
    info: RegexInfo | None = ctx.receiver.info
    t = ctx.checker.check_expr(node)
    if t not in (INT, STR):
        raise ctx.error(f"a group is a number or a name, not {t}", node)
    group = None
    if isinstance(node, A.IntLit):
        group = node.value
    elif isinstance(node, A.StrLit) and info is not None:
        group = info.group_number(node.value)
        if group is None:
            raise ctx.error(f"the pattern has no group named '{node.value}'", node)
    if group is not None and info is not None and not 0 <= group <= info.groups:
        raise ctx.error(f"the pattern has no group {group} (it has {plural(info.groups, 'group')})", node)
    node.regex_group = group  # codegen: a known group number
    return group_type(info, group)


def match_group(ctx: CallContext) -> Type:
    n = ctx.arity(0, MANY)
    if n == 0:
        return STR
    types = [match_group_arg(ctx, a) for a in ctx.args]
    return types[0] if n == 1 else TupleType(tuple(types))


def match_groups(ctx: CallContext) -> Type:
    ctx.arity(0)
    info: RegexInfo | None = ctx.receiver.info
    if info is None:
        return ListType(OptionalType(STR))  # unknown pattern: how many groups isn't known
    return TupleType(tuple(group_type(info, g) for g in range(1, info.groups + 1)))


def match_groupdict(ctx: CallContext) -> Type:
    ctx.arity(0)
    info: RegexInfo | None = ctx.receiver.info
    if info is not None and not any(i in info.optional for _, i in info.names):
        return DictType(STR, STR)
    return DictType(STR, OptionalType(STR))


def match_position(result: Type) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        if ctx.arity(0, 1):
            match_group_arg(ctx, ctx.args[0])
        return result

    return handler


PATTERN_METHODS = {op: pattern_method(op) for op in REGEX_OPS}

MATCH_METHODS = {
    "group": match_group,
    "groups": match_groups,
    "groupdict": match_groupdict,
    "start": match_position(INT),
    "end": match_position(INT),
    "span": match_position(TupleType((INT, INT))),
    "expand": returns(STR, args=(STR,)),
}

# Attributes of built-in types: name -> type (given the receiver's type).
PATTERN_ATTRIBUTES = {
    "pattern": lambda t: STR, "flags": lambda t: INT, "groups": lambda t: INT,
    "groupindex": lambda t: DictType(STR, INT),
}
MATCH_ATTRIBUTES = {
    "string": lambda t: STR, "pos": lambda t: INT, "endpos": lambda t: INT,
    "re": lambda t: PatternType(t.info), "lastindex": lambda t: OptionalType(INT),
}


def type_attributes(t: Type) -> dict | None:
    if isinstance(t, BuiltinClass):
        return t.attributes
    if isinstance(t, StructFormatType):
        return {"size": lambda t: INT, "format": lambda t: STR}
    if isinstance(t, NamespaceType):
        return {name: (lambda _, ft=ft: ft) for name, ft in t.fields}
    if isinstance(t, DequeType):
        return {"maxlen": lambda t: OptionalType(INT)}
    if isinstance(t, HTTPServerType):
        return HTTP_SERVER_ATTRIBUTES
    if isinstance(t, FileType):
        return FILE_ATTRIBUTES
    if isinstance(t, PatternType):
        return PATTERN_ATTRIBUTES
    if isinstance(t, MatchType):
        return MATCH_ATTRIBUTES
    if isinstance(t, ProcessType):
        return COMPLETED_ATTRIBUTES if t.kind == "CompletedProcess" else POPEN_ATTRIBUTES
    return None


# ---- pathlib ------------------------------------------------------------------------

PATH_LIKE = object()  # a parameter taking a str or a Path (sync_method checks it)

PATH.attributes.update({
    "name": lambda t: STR, "stem": lambda t: STR, "suffix": lambda t: STR, "anchor": lambda t: STR,
    "suffixes": lambda t: ListType(STR), "parts": lambda t: VarTupleType(STR),
    "parent": lambda t: PATH, "parents": lambda t: ListType(PATH),
})


def path_parts(ctx: CallContext) -> Type:
    """Path(*parts) and p.joinpath(*parts): each a str or a Path."""
    ctx.arity(0, MANY)
    for i in range(len(ctx.args)):
        ctx.need(i, lambda t: t in (STR, PATH), "a str or Path")
    return PATH


def path_open(ctx: CallContext) -> Type:
    n = ctx.arity(0, 1, keywords=("mode", "encoding"))
    return open_mode(ctx, ctx.args[0] if n == 1 else ctx.keyword_arg("mode"))


STAT_RESULT = StructType("stat_result", "struct", None, builtin=True, cpp_name="sd::pathlib::StatResult")
for _field, _t in (("st_size", INT), ("st_mode", INT), ("st_uid", INT), ("st_gid", INT), ("st_nlink", INT),
                   ("st_ino", INT), ("st_mtime", FLOAT), ("st_atime", FLOAT), ("st_ctime", FLOAT)):
    STAT_RESULT.fields[_field] = Field(_field, _t, None, Loc(0, 0))

PATH.methods.update({
    **{name: sync_method(BOOL) for name in ("exists", "is_file", "is_dir", "is_symlink", "is_absolute")},
    **{name: sync_method(PATH) for name in ("absolute", "resolve", "expanduser")},
    "as_posix": sync_method(STR),
    "stat": sync_method(STAT_RESULT),
    "read_text": sync_method(STR, ("encoding", OptionalType(STR), "std::nullopt")),
    "read_bytes": sync_method(BYTES),
    "write_text": sync_method(INT, ("data", STR), ("encoding", OptionalType(STR), "std::nullopt")),
    "write_bytes": sync_method(INT, ("data", BYTES)),
    "mkdir": sync_method(NONE, ("mode", INT, "0777"), ("parents", BOOL, "false"), ("exist_ok", BOOL, "false")),
    "rmdir": sync_method(NONE),
    "unlink": sync_method(NONE, ("missing_ok", BOOL, "false")),
    "touch": sync_method(NONE, ("mode", INT, "0666"), ("exist_ok", BOOL, "true")),
    "rename": sync_method(PATH, ("target", PATH_LIKE)),
    "replace": sync_method(PATH, ("target", PATH_LIKE)),
    "iterdir": sync_method(ListType(PATH)),
    "glob": sync_method(ListType(PATH), ("pattern", STR)),
    "rglob": sync_method(ListType(PATH), ("pattern", STR)),
    "with_name": sync_method(PATH, ("name", STR)),
    "with_suffix": sync_method(PATH, ("suffix", STR)),
    "with_stem": sync_method(PATH, ("stem", STR)),
    "relative_to": sync_method(PATH, ("other", PATH_LIKE)),
    "is_relative_to": sync_method(BOOL, ("other", PATH_LIKE)),
    "match": sync_method(BOOL, ("pattern", STR)),
    "joinpath": path_parts,
    "open": path_open,
})

MODULES["pathlib"] = Module("pathlib", {
    "Path": Function("Path", path_parts, "sd::pathlib::Path", as_type=PATH),
    "PosixPath": Function("PosixPath", path_parts, "sd::pathlib::Path", as_type=PATH),
}, "modules/pathlib.hpp")


# ---- shutil, tempfile -------------------------------------------------------------------

def builtin_struct(name: str, cpp: str, fields: dict[str, Type]) -> StructType:
    st = StructType(name, "struct", None, builtin=True, cpp_name=cpp)
    for fname, ft in fields.items():
        st.fields[fname] = Field(fname, ft, None, Loc(0, 0))
    return st


DISK_USAGE = builtin_struct("usage", "sd::shutil::DiskUsage", {"total": INT, "used": INT, "free": INT})
SHUTIL_ERROR = StructType("Error", "class", None, base=EXCEPTIONS["OSError"], builtin=True, cpp_name="sd::shutil::Error")
SRC_DST = (("src", PATH_LIKE), ("dst", PATH_LIKE))

MODULES["shutil"] = module_with_params(runtime_module(
    "shutil", "modules/shutil.hpp",
    copyfile=(signature(STR, *SRC_DST), "sd::shutil::copyfile"),
    copy=(signature(STR, *SRC_DST), "sd::shutil::copy"),
    copy2=(signature(STR, *SRC_DST), "sd::shutil::copy2"),
    copymode=(signature(NONE, *SRC_DST), "sd::shutil::copymode"),
    copystat=(signature(NONE, *SRC_DST), "sd::shutil::copystat"),
    copytree=(signature(STR, *SRC_DST, ("dirs_exist_ok", BOOL, "false")), "sd::shutil::copytree"),
    rmtree=(signature(NONE, ("path", PATH_LIKE), ("ignore_errors", BOOL, "false")), "sd::shutil::rmtree"),
    move=(signature(STR, *SRC_DST), "sd::shutil::move"),
    which=(signature(OptionalType(STR), ("cmd", STR), ("path", OptionalType(STR), "std::nullopt")), "sd::shutil::which"),
    disk_usage=(signature(DISK_USAGE, ("path", PATH_LIKE)), "sd::shutil::disk_usage"),
    Error=SHUTIL_ERROR,
    SameFileError=StructType("SameFileError", "class", None, base=SHUTIL_ERROR, builtin=True,
                             cpp_name="sd::shutil::SameFileError"),
))

TEMP_PARAMS = (("suffix", STR, '""s'), ("prefix", OptionalType(STR), "std::nullopt"),
               ("dir", OptionalType(STR), "std::nullopt"))
MODULES["tempfile"] = module_with_params(runtime_module(
    "tempfile", "modules/tempfile.hpp",
    gettempdir=(signature(STR), "sd::tempfile::gettempdir"),
    mkdtemp=(signature(STR, *TEMP_PARAMS), "sd::tempfile::mkdtemp"),
    TemporaryDirectory=(signature(TEMPDIR, *TEMP_PARAMS), "sd::tempfile::TemporaryDirectory"),
))
MODULES["tempfile"].members["TemporaryDirectory"].as_type = TEMPDIR
TEMPDIR.methods["cleanup"] = sync_method(NONE)
TEMPDIR.attributes["name"] = lambda t: STR


# ---- datetime ---------------------------------------------------------------------------

def attrs(result: Type, *names: str) -> dict:
    return {name: (lambda t, r=result: r) for name in names}


OPT_INT, OPT_TZ = OptionalType(INT), OptionalType(TIMEZONE)
DATE_FIELDS = (("year", OPT_INT, "std::nullopt"), ("month", OPT_INT, "std::nullopt"), ("day", OPT_INT, "std::nullopt"))
TIME_FIELDS = (("hour", OPT_INT, "std::nullopt"), ("minute", OPT_INT, "std::nullopt"),
               ("second", OPT_INT, "std::nullopt"), ("microsecond", OPT_INT, "std::nullopt"))

DATE.attributes.update(attrs(INT, "year", "month", "day"))
TIME.attributes.update({**attrs(INT, "hour", "minute", "second", "microsecond"), "tzinfo": lambda t: OPT_TZ})
DATETIME.attributes.update({**attrs(INT, "year", "month", "day", "hour", "minute", "second", "microsecond"),
                            "tzinfo": lambda t: OPT_TZ})
TIMEDELTA.attributes.update(attrs(INT, "days", "seconds", "microseconds"))
DATE_METHODS = {
    "isoformat": sync_method(STR),
    "strftime": sync_method(STR, ("format", STR)),
    "ctime": sync_method(STR),
    "weekday": sync_method(INT),
    "isoweekday": sync_method(INT),
    "toordinal": sync_method(INT),
}
DATE.methods.update({**DATE_METHODS, "replace": sync_method(DATE, *DATE_FIELDS)})
TIME.methods.update({
    "isoformat": sync_method(STR, ("timespec", STR, '"auto"s')),
    "strftime": sync_method(STR, ("format", STR)),
    "replace": sync_method(TIME, *TIME_FIELDS),
})
DATETIME.methods.update({
    **DATE_METHODS,
    "isoformat": sync_method(STR, ("sep", STR, '"T"s'), ("timespec", STR, '"auto"s')),
    "date": sync_method(DATE),
    "time": sync_method(TIME),
    "timestamp": sync_method(FLOAT),
    "utcoffset": sync_method(OptionalType(TIMEDELTA)),
    "tzname": sync_method(OptionalType(STR)),
    "astimezone": sync_method(DATETIME, ("tz", OPT_TZ, "std::nullopt")),
    "replace": sync_method(DATETIME, *DATE_FIELDS, *TIME_FIELDS,
                           ("tzinfo", OPT_TZ, "std::optional<std::optional<sd::datetime::timezone>>()")),
})
TIMEDELTA.methods.update({"total_seconds": sync_method(FLOAT)})
TIMEZONE.methods.update({"tzname": sync_method(STR, ("dt", OptionalType(DATETIME), "std::nullopt"))})

DT = "sd::datetime::"
TIME_PARAMS = (("hour", INT, "0"), ("minute", INT, "0"), ("second", INT, "0"), ("microsecond", INT, "0"),
               ("tzinfo", OPT_TZ, "std::nullopt"))
DATETIME_MODULE = {
    "date": Function("date", signature(DATE, ("year", INT), ("month", INT), ("day", INT)), DT + "date", as_type=DATE),
    "time": Function("time", signature(TIME, *TIME_PARAMS), DT + "time", as_type=TIME),
    "datetime": Function("datetime", signature(DATETIME, ("year", INT), ("month", INT), ("day", INT), *TIME_PARAMS),
                         DT + "datetime", as_type=DATETIME),
    "timedelta": Function("timedelta", signature(TIMEDELTA, *((name, FLOAT, "0.0") for name in (
        "days", "seconds", "microseconds", "milliseconds", "minutes", "hours", "weeks"))), DT + "timedelta",
        as_type=TIMEDELTA),
    "timezone": Function("timezone", signature(TIMEZONE, ("offset", TIMEDELTA), ("name", OptionalType(STR), "std::nullopt")),
                         DT + "timezone", as_type=TIMEZONE),
    "MINYEAR": Value("MINYEAR", INT, DT + "MINYEAR"),
    "MAXYEAR": Value("MAXYEAR", INT, DT + "MAXYEAR"),
    "UTC": Value("UTC", TIMEZONE, DT + "timezone::utc()"),
}
for _f in DATETIME_MODULE.values():
    if isinstance(_f, Function):
        _f.params = _f.check.params
DATETIME_MODULE["tzinfo"] = NamedType("tzinfo", TIMEZONE)  # for annotations: any time zone
MODULES["datetime"] = Module("datetime", DATETIME_MODULE, "modules/datetime.hpp")

MODULES["zoneinfo"] = module_with_params(runtime_module(
    "zoneinfo", "modules/zoneinfo.hpp",
    ZoneInfo=(signature(TIMEZONE, ("key", STR)), "sd::zoneinfo::ZoneInfo"),
    available_timezones=(signature(SetType(STR)), "sd::zoneinfo::available_timezones"),
    ZoneInfoNotFoundError=StructType("ZoneInfoNotFoundError", "class", None, base=EXCEPTIONS["KeyError"], builtin=True,
                                     cpp_name="sd::zoneinfo::ZoneInfoNotFoundError"),
))
MODULES["zoneinfo"].members["ZoneInfo"].as_type = TIMEZONE


def class_function(name: str, result: Type, cpp: str, *params) -> Function:
    return Function(name, signature(result, *params), cpp, params)


# Members of a built-in class itself: Path.cwd(), datetime.now(), timezone.utc.
CLASS_MEMBERS: dict[Type, dict[str, Function | Value]] = {
    PATH: {
        "cwd": class_function("Path.cwd", PATH, "sd::pathlib::Path::cwd"),
        "home": class_function("Path.home", PATH, "sd::pathlib::Path::home"),
    },
    DATE: {
        "today": class_function("date.today", DATE, DT + "date::today"),
        "fromisoformat": class_function("date.fromisoformat", DATE, DT + "date::fromisoformat", ("date_string", STR)),
        "fromordinal": class_function("date.fromordinal", DATE, DT + "date::fromordinal", ("ordinal", INT)),
        "fromtimestamp": class_function("date.fromtimestamp", DATE, DT + "date::fromtimestamp", ("timestamp", FLOAT)),
    },
    TIME: {
        "fromisoformat": class_function("time.fromisoformat", TIME, DT + "time::fromisoformat", ("time_string", STR)),
    },
    DATETIME: {
        "now": class_function("datetime.now", DATETIME, DT + "datetime::now", ("tz", OPT_TZ, "std::nullopt")),
        "today": class_function("datetime.today", DATETIME, DT + "datetime::today"),
        "utcnow": class_function("datetime.utcnow", DATETIME, DT + "datetime::utcnow"),
        "fromtimestamp": class_function("datetime.fromtimestamp", DATETIME, DT + "datetime::fromtimestamp",
                                        ("timestamp", FLOAT), ("tz", OPT_TZ, "std::nullopt")),
        "utcfromtimestamp": class_function("datetime.utcfromtimestamp", DATETIME, DT + "datetime::utcfromtimestamp",
                                           ("timestamp", FLOAT)),
        "fromisoformat": class_function("datetime.fromisoformat", DATETIME, DT + "datetime::fromisoformat",
                                        ("date_string", STR)),
        "strptime": class_function("datetime.strptime", DATETIME, DT + "datetime::strptime",
                                   ("date_string", STR), ("format", STR)),
        "combine": class_function("datetime.combine", DATETIME, DT + "datetime::combine",
                                  ("date", DATE), ("time", TIME), ("tzinfo", OPT_TZ, "std::nullopt")),
    },
    TIMEZONE: {"utc": Value("utc", TIMEZONE, DT + "timezone::utc()")},
}


# ---- argparse ---------------------------------------------------------------------------
#
# The parsed Namespace is typed from the parser's add_argument() calls, which the checker
# sees in order: args.count is an int (not "any"), and a misspelled args.cuont is a compile
# error. So the arguments are written out (literal names, action=, nargs=, type=) and
# added in the function that calls parse_args() -- as almost every script does.

ACTIONS = ("store", "store_true", "store_false", "store_const", "count", "append", "help", "version")
ARG_KEYWORDS = ("action", "nargs", "const", "default", "type", "choices", "required", "help", "metavar", "dest", "version")


def parser_key(ctx: CallContext) -> int:
    key = ctx.receiver.key
    if key is None:
        raise ctx.error(f"{ctx.what} needs a parser created in this module (so its arguments are known)")
    return key


def new_parser(ctx: CallContext) -> Type:
    """ArgumentParser(...): a parser of its own, for add_argument() to describe."""
    ARGUMENT_PARSER_SIGNATURE(ctx)
    return ParserType(id(ctx.call))


def parser_add_subparsers(ctx: CallContext) -> Type:
    key = parser_key(ctx)
    ctx.arity(0, keywords=("dest", "required", "title", "description", "help", "metavar"))
    kw = {k.name: k.value for k in ctx.call.keywords}
    dest = literal_str(ctx, kw["dest"], "dest=") if "dest" in kw else ""
    required = False
    if "required" in kw:
        if not isinstance(kw["required"], A.BoolLit):
            raise ctx.error("required= must be True or False written out", kw["required"])
        kw["required"].ty = BOOL
        required = kw["required"].value
    for name in ("title", "description", "help", "metavar"):
        if name in kw:
            ctx.checker.expect_type(kw[name], STR, name)
    if key in ctx.checker.subcommands:
        raise ctx.error("a parser can only have one add_subparsers()", ctx.call)
    ctx.checker.subcommands[key] = {"dest": dest, "required": required, "commands": []}
    ctx.call.argparse_sub = {"dest": dest, "required": required, "kw": kw}
    return SubParsersType(key)


def subparsers_add_parser(ctx: CallContext) -> Type:
    parent = ctx.receiver.parent
    if not ctx.args:
        raise ctx.error("add_parser() needs the subcommand's name")
    ctx.arity(1, keywords=("help", "aliases", "description"))
    name = literal_str(ctx, ctx.args[0], "the subcommand's name")
    kw = {k.name: k.value for k in ctx.call.keywords}
    aliases: list[str] = []
    if "aliases" in kw:
        if not isinstance(kw["aliases"], (A.ListLit, A.TupleLit)):
            raise ctx.error("aliases= must be a list written out, like aliases=['rm']", kw["aliases"])
        aliases = [literal_str(ctx, a, "an alias") for a in kw["aliases"].elts]
        kw["aliases"].ty = ListType(STR)
    for field in ("help", "description"):
        if field in kw:
            ctx.checker.expect_type(kw[field], STR, field)
    commands = ctx.checker.subcommands[parent]["commands"]
    for names, _ in commands:
        if name in names or set(aliases) & set(names):
            raise ctx.error(f"conflicting subparser: {name}", ctx.call)
    child = ParserType(id(ctx.call))
    commands.append(((name, *aliases), child.key))
    ctx.call.argparse_cmd = {"name": name, "aliases": aliases, "kw": kw}
    return child


def literal_str(ctx: CallContext, node: A.Expr, what: str) -> str:
    if not isinstance(node, A.StrLit):
        raise ctx.error(f"{what} must be a string written out (it decides the parsed type)", node)
    node.ty = STR
    return node.value


def argument_kind(ctx: CallContext, node: A.Expr | None) -> tuple[str, Type]:
    """type=int / float / str / Path -> (runtime kind, value type)."""
    if node is None:
        return "STR", STR
    node.compile_time = True
    if isinstance(node, A.Name) and node.id in ("int", "float", "str") and not ctx.checker.state.names.get(node.id):
        return node.id.upper(), {"int": INT, "float": FLOAT, "str": STR}[node.id]
    if ctx.checker.builtin_class(node) == PATH or (
        isinstance(node, A.Name) and node.id in ctx.checker.imported
        and ctx.checker.imported[node.id][0] is MODULES["pathlib"]
    ):
        return "PATH", PATH
    raise ctx.error("type= must be int, float, str or Path (written out: it decides the parsed type)", node)


def parser_add_argument(ctx: CallContext) -> Type:
    var = parser_key(ctx)
    if not ctx.args:
        raise ctx.error("add_argument() needs a name, or option flags like '-v', '--verbose'")
    names = [literal_str(ctx, a, "an argument name") for a in ctx.args]
    kw: dict[str, A.Expr] = {}
    for k in ctx.call.keywords:
        if k.name not in ARG_KEYWORDS:
            raise ctx.error(f"add_argument() got an unexpected keyword argument '{k.name}'", k)
        kw[k.name] = k.value
    positional = not names[0].startswith("-")
    if positional and len(names) > 1:
        raise ctx.error("a positional argument has one name (options start with '-')", ctx.args[1])
    if not positional and any(not n.startswith("-") for n in names):
        raise ctx.error("option flags all start with '-'", ctx.call)
    action = literal_str(ctx, kw["action"], "action=") if "action" in kw else "store"
    if action not in ACTIONS:
        raise ctx.error(f"unknown action {action!r} (supported: {', '.join(ACTIONS)})", kw["action"])
    nargs = "ONE"
    if "nargs" in kw:
        n = kw["nargs"]
        if isinstance(n, A.IntLit):
            nargs = str(n.value)
        elif isinstance(n, A.StrLit) and n.value in ("?", "*", "+"):
            nargs = {"?": "OPTIONAL", "*": "ANY", "+": "SOME"}[n.value]
        else:
            raise ctx.error("nargs= must be a number, '?', '*' or '+' (written out)", n)
        n.ty = INT if isinstance(n, A.IntLit) else STR
    if "dest" in kw:
        dest = literal_str(ctx, kw["dest"], "dest=")
    elif positional:
        dest = names[0]
    else:
        long = next((n for n in names if n.startswith("--")), names[0])
        dest = long.lstrip("-").replace("-", "_")
    if positional and "required" in kw:
        raise ctx.error("'required' is an invalid argument for positionals", kw["required"])
    kind, item = argument_kind(ctx, kw.get("type"))
    required = False
    if "required" in kw:
        if not isinstance(kw["required"], A.BoolLit):
            raise ctx.error("required= must be True or False written out", kw["required"])
        kw["required"].ty = BOOL
        required = kw["required"].value
    many = nargs not in ("ONE", "OPTIONAL")
    has_default = "default" in kw and not isinstance(kw["default"], A.NoneLit)
    # What the attribute holds, following Python's rules for when it can be None.
    match action:
        case "store_true" | "store_false":
            t = BOOL
        case "count":
            t = INT if has_default else OptionalType(INT)
        case "append":
            t = ListType(item) if has_default else OptionalType(ListType(item))
        case "store_const":
            if "const" not in kw:
                raise ctx.error("action='store_const' needs const=", ctx.call)
            t = ctx.checker.check_expr(kw["const"])
            if not has_default:
                t = t if isinstance(t, OptionalType) else OptionalType(t)
        case "help" | "version":
            t = None
        case _:
            base = ListType(item) if many else item
            always = (positional and nargs in ("ONE", "SOME") or nargs.isdigit()) or (positional and nargs == "ANY")
            t = base if always or has_default or required else OptionalType(base)
    if action == "version" and "version" not in kw:
        raise ctx.error("action='version' needs version=", ctx.call)
    for name, want in (("help", STR), ("metavar", STR), ("version", STR)):
        if name in kw:
            ctx.checker.expect_type(kw[name], want, name)
    if has_default and t is not None:
        want = strip_optional_type(t)
        actual = ctx.checker.check_expr(kw["default"], want)
        if not assignable(actual, want):
            raise ctx.error(f"default= must be {want} for this argument, not {actual}", kw["default"])
    elif "default" in kw:
        ctx.checker.check_expr(kw["default"])
    if "const" in kw and action != "store_const":
        ctx.checker.expect_type(kw["const"], item, "const")
    if "choices" in kw:
        ch = kw["choices"]
        if not isinstance(ch, (A.ListLit, A.TupleLit)):
            raise ctx.error("choices= must be a list written out, like choices=['fast', 'slow']", ch)
        for c in ch.elts:
            ctx.checker.expect_type(c, item, "each choice")
        ch.ty = ListType(item)
    specs = ctx.checker.argument_parsers.setdefault(var, [])
    for existing, _ in specs:
        if existing == dest and t is not None:
            raise ctx.error(f"'{dest}' is already an argument of this parser", ctx.call)
    if t is not None:
        specs.append((dest, t))
    ctx.call.argparse = {"flags": [] if positional else names, "dest": dest, "action": action, "nargs": nargs,
                         "kind": kind, "kw": kw, "required": required}
    return NONE


def strip_optional_type(t: Type) -> Type:
    return t.inner if isinstance(t, OptionalType) else t


def parser_parse_args(ctx: CallContext) -> Type:
    key = parser_key(ctx)
    ctx.arity(0, 1, keywords=("args",))
    node = ctx.args[0] if ctx.args else ctx.keyword_arg("args")
    if node is not None and not isinstance(node, A.NoneLit):
        ctx.checker.expect_type(node, ListType(STR), "args")
    ctx.call.regex_args = {"args": node}  # (the bound argument, for codegen)
    fields = list(ctx.checker.argument_parsers.get(key, []))
    commands = []
    sub = ctx.checker.subcommands.get(key)
    if sub is not None:
        if sub["dest"]:
            fields.append((sub["dest"], STR if sub["required"] else OptionalType(STR)))
        merged: dict[str, Type] = {}
        for names, child in sub["commands"]:
            own = tuple(ctx.checker.argument_parsers.get(child, []))
            commands.append((sub["dest"], names, own))
            for name, t in own:
                opt = t if isinstance(t, OptionalType) else OptionalType(t)  # only there for that subcommand
                if any(name == f for f, _ in fields):
                    raise ctx.error(f"subcommand '{names[0]}' has an argument '{name}' that its parser already has")
                if name in merged and merged[name] != opt:
                    raise ctx.error(f"'{name}' is {merged[name]} in one subcommand and {opt} in another")
                merged[name] = opt
        fields.extend(merged.items())
    return NamespaceType(tuple(fields), tuple(commands))


PARSER_METHODS = {
    "add_argument": parser_add_argument,
    "add_subparsers": parser_add_subparsers,
    "parse_args": parser_parse_args,
    "print_help": sync_method(NONE),
    "print_usage": sync_method(NONE),
    "format_help": sync_method(STR),
    "format_usage": sync_method(STR),
    "error": sync_method(NONE, ("message", STR)),
    "exit": sync_method(NONE, ("status", INT, "0"), ("message", OptionalType(STR), "std::nullopt")),
}
ARGUMENT_PARSER_SIGNATURE = signature(
    PARSER, *((name, OptionalType(STR), "std::nullopt") for name in ("prog", "usage", "description", "epilog")),
    ("add_help", BOOL, "true"))
MODULES["argparse"] = Module("argparse", {
    "ArgumentParser": Function("ArgumentParser", new_parser, "sd::argparse::ArgumentParser", as_type=PARSER),
    "Namespace": NamedType("Namespace", NamespaceType()),
}, "modules/argparse.hpp")
MODULES["argparse"].members["ArgumentParser"].params = ARGUMENT_PARSER_SIGNATURE.params


# ---- itertools ------------------------------------------------------------------------
#
# Everything returns a lazy Iterator[T]. Tuple results are typed exactly when their size is
# written out: permutations(xs, 2) gives tuple[T, T], permutations(xs) gives tuple[T, ...].

def it_bind(ctx: CallContext, params: tuple) -> dict:
    return bind_args(ctx, params)


def literal_int(node: A.Expr | None) -> int | None:
    if isinstance(node, A.IntLit):
        return node.value
    return None


def fixed_or_var_tuple(elem: Type, r: A.Expr | None) -> Type:
    n = literal_int(r)
    return TupleType((elem,) * n) if n is not None and n >= 0 else VarTupleType(elem)


def expect_opt_int(ctx: CallContext, node: A.Expr | None, what: str) -> None:
    if node is not None and not isinstance(node, A.NoneLit):
        ctx.checker.expect_type(node, INT, what)
    elif node is not None:
        ctx.checker.check_expr(node)


def iterable_of(ctx: CallContext, node: A.Expr, what: str = "argument") -> Type:
    t = ctx.checker.check_expr(node)
    elem = element_type(t)
    if elem is None:
        raise ctx.error(f"{ctx.what} {what} must be something you can loop over, not {t}{mixed_tuple_hint(t)}", node)
    mark_tuple_iterable(node, t, elem)
    return elem


def itertools_function(name: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        info: dict = {}
        ctx.call.itertools = info
        match name:
            case "count":
                args = it_bind(ctx, (("start", None, True), ("step", None, True)))
                types = [ctx.checker.check_expr(args[k]) for k in ("start", "step") if k in args]
                if any(not is_numeric(t) for t in types):
                    raise ctx.error("count() takes numbers")
                info["args"] = args
                return GeneratorType(FLOAT if FLOAT in types else INT)
            case "cycle" | "pairwise":
                ctx.arity(1)
                elem = iterable_of(ctx, ctx.args[0])
                return GeneratorType(TupleType((elem, elem)) if name == "pairwise" else elem)
            case "repeat":
                args = it_bind(ctx, (("object", None), ("times", None, True)))
                expect_opt_int(ctx, args.get("times"), "repeat() times")
                info["args"] = args
                return GeneratorType(ctx.checker.check_expr(args["object"]))
            case "accumulate":
                args = it_bind(ctx, (("iterable", None), ("func", None, True), ("initial", None, True)))
                elem = iterable_of(ctx, args["iterable"])
                if "func" in args and not isinstance(args["func"], A.NoneLit):
                    result = ctx.function(args["func"], (elem, elem), "func")
                    if not assignable(result, elem):
                        raise ctx.error(f"accumulate() func must return {elem}, not {result}", args["func"])
                elif not (is_numeric(elem) or elem in (STR, TIMEDELTA) or isinstance(elem, ListType)):
                    raise ctx.error(f"accumulate() adds its items, but {elem} values can't be added (pass func=)")
                if "initial" in args and not isinstance(args["initial"], A.NoneLit):
                    ctx.checker.expect_type(args["initial"], elem, "accumulate() initial")
                info["args"] = args
                return GeneratorType(elem)
            case "chain":
                ctx.arity(1, MANY)
                elem = None
                for a in ctx.args:
                    e = iterable_of(ctx, a)
                    elem = e if elem is None else join(elem, e)
                    if elem is None:
                        raise ctx.error("chain() needs iterables of the same kind of item", a)
                return GeneratorType(elem)
            case "chain.from_iterable":
                ctx.arity(1)
                inner = iterable_of(ctx, ctx.args[0])
                elem = element_type(inner)
                if elem is None:
                    raise ctx.error(f"chain.from_iterable() needs an iterable of iterables, not of {inner}", ctx.args[0])
                return GeneratorType(elem)
            case "compress":
                ctx.arity(2)
                elem = iterable_of(ctx, ctx.args[0])
                iterable_of(ctx, ctx.args[1], "selectors")
                return GeneratorType(elem)
            case "dropwhile" | "takewhile" | "filterfalse":
                ctx.arity(2)
                elem = iterable_of(ctx, ctx.args[1])
                result = ctx.function(ctx.args[0], (elem,), "predicate")
                ctx.checker.check_truthy(result, ctx.args[0])
                return GeneratorType(elem)
            case "groupby":
                args = it_bind(ctx, (("iterable", None), ("key", None, True)))
                elem = iterable_of(ctx, args["iterable"])
                key = elem
                if "key" in args and not isinstance(args["key"], A.NoneLit):
                    key = ctx.function(args["key"], (elem,), "key")
                info["args"] = args
                return GeneratorType(TupleType((key, GeneratorType(elem))))
            case "islice":
                ctx.arity(2, 4)
                elem = iterable_of(ctx, ctx.args[0])
                for a in ctx.args[1:]:
                    expect_opt_int(ctx, a, "islice() index")
                return GeneratorType(elem)
            case "starmap":
                ctx.arity(2)
                elem = iterable_of(ctx, ctx.args[1])
                if not isinstance(elem, TupleType):
                    raise ctx.error(f"starmap() needs an iterable of tuples (the arguments), not of {elem}", ctx.args[1])
                result = ctx.function(ctx.args[0], elem.elts, "function")
                return GeneratorType(result)
            case "tee":
                ctx.arity(1, 2)
                elem = iterable_of(ctx, ctx.args[0])
                n = 2
                if len(ctx.args) == 2:
                    n = literal_int(ctx.args[1])
                    if n is None or n < 0:
                        raise ctx.error("tee()'s n must be a number written out (it decides how many you get)", ctx.args[1])
                    ctx.checker.check_expr(ctx.args[1])
                info["n"] = n
                return TupleType((GeneratorType(elem),) * n)
            case "zip_longest":
                ctx.arity(1, MANY, keywords=("fillvalue",))
                elems = [iterable_of(ctx, a) for a in ctx.args]
                fill = ctx.keyword_arg("fillvalue")
                if fill is None or isinstance(fill, A.NoneLit):
                    if fill is not None:
                        ctx.checker.check_expr(fill)
                    info["elems"] = elems
                    return GeneratorType(TupleType(tuple(e if isinstance(e, OptionalType) else OptionalType(e) for e in elems)))
                ft = ctx.checker.check_expr(fill)
                joined = []
                for e in elems:
                    j = join(e, ft)
                    if j is None:
                        raise ctx.error(f"zip_longest() fillvalue must fit every input's items: {e}, not {ft}", fill)
                    joined.append(j)
                info["elems"] = elems
                return GeneratorType(TupleType(tuple(joined)))
            case "product":
                ctx.arity(1, MANY, keywords=("repeat",))
                elems = [iterable_of(ctx, a) for a in ctx.args]
                rep = ctx.keyword_arg("repeat")
                n = 1
                if rep is not None:
                    n = literal_int(rep)
                    if n is None or n < 0:
                        raise ctx.error("product()'s repeat must be a number written out (it decides the tuple size)", rep)
                    ctx.checker.check_expr(rep)
                info["repeat"] = n
                return GeneratorType(TupleType(tuple(elems) * n))
            case "permutations":
                args = it_bind(ctx, (("iterable", None), ("r", None, True)))
                elem = iterable_of(ctx, args["iterable"])
                expect_opt_int(ctx, args.get("r"), "permutations() r")
                info["args"] = args
                return GeneratorType(fixed_or_var_tuple(elem, args.get("r")))
            case "combinations" | "combinations_with_replacement":
                args = it_bind(ctx, (("iterable", None), ("r", None)))
                elem = iterable_of(ctx, args["iterable"])
                ctx.checker.expect_type(args["r"], INT, f"{name}() r")
                info["args"] = args
                return GeneratorType(fixed_or_var_tuple(elem, args["r"]))
            case "batched":
                args = it_bind(ctx, (("iterable", None), ("n", None)))
                elem = iterable_of(ctx, args["iterable"])
                ctx.checker.expect_type(args["n"], INT, "batched() n")
                info["args"] = args
                return GeneratorType(VarTupleType(elem))
        raise AssertionError(name)

    return handler


ITERTOOLS = ("count", "cycle", "repeat", "accumulate", "chain", "chain.from_iterable", "compress", "dropwhile",
             "takewhile", "filterfalse", "groupby", "islice", "starmap", "tee", "zip_longest", "product",
             "permutations", "combinations", "combinations_with_replacement", "pairwise", "batched")
MODULES["itertools"] = Module("itertools", {
    name: Function(name, itertools_function(name)) for name in ITERTOOLS
}, "modules/itertools.hpp")


# ---- textwrap -----------------------------------------------------------------------------

WRAP_OPTIONS = (
    ("initial_indent", STR, '""s'), ("subsequent_indent", STR, '""s'), ("expand_tabs", BOOL, "true"),
    ("replace_whitespace", BOOL, "true"), ("fix_sentence_endings", BOOL, "false"), ("break_long_words", BOOL, "true"),
    ("drop_whitespace", BOOL, "true"), ("break_on_hyphens", BOOL, "true"), ("tabsize", INT, "8"),
    ("max_lines", OptionalType(INT), "std::nullopt"), ("placeholder", STR, '" [...]"s'),
)
WIDTH_70 = ("width", INT, "70")
MODULES["textwrap"] = module_with_params(runtime_module(
    "textwrap", "modules/textwrap.hpp", ("pcre2-8",),
    wrap=(signature(ListType(STR), ("text", STR), WIDTH_70, *WRAP_OPTIONS), "sd::textwrap::wrap"),
    fill=(signature(STR, ("text", STR), WIDTH_70, *WRAP_OPTIONS), "sd::textwrap::fill"),
    shorten=(signature(STR, ("text", STR), ("width", INT), *WRAP_OPTIONS), "sd::textwrap::shorten"),
    dedent=(signature(STR, ("text", STR)), "sd::textwrap::dedent"),
    indent=(signature(STR, ("text", STR), ("prefix", STR), ("predicate", FuncType((STR,), BOOL),
                                                               "[](const std::string& l) { return !sd::textwrap::is_blank(l); }")),
            "sd::textwrap::indent"),
    TextWrapper=(signature(TEXT_WRAPPER, WIDTH_70, *WRAP_OPTIONS), "sd::textwrap::make"),
))
MODULES["textwrap"].members["TextWrapper"].as_type = TEXT_WRAPPER
TEXT_WRAPPER.methods.update({"wrap": sync_method(ListType(STR), ("text", STR)), "fill": sync_method(STR, ("text", STR))})


# ---- string ---------------------------------------------------------------------------

def template_substitute(ctx: CallContext) -> Type:
    """t.substitute(mapping, **keywords): the mapping's values and the keywords can be anything
    printable (they're shown with str())."""
    ctx.arity(0, 1, keywords=tuple(k.name for k in ctx.call.keywords))
    if ctx.args:
        t = ctx.arg(0)
        if not (isinstance(t, DictType) and t.key == STR):
            raise ctx.error(f"{ctx.what} takes a dict with str keys (and/or keyword arguments), not {t}", ctx.args[0])
    for kw in ctx.call.keywords:
        ctx.checker.check_printable(ctx.checker.check_expr(kw.value), kw.value)
    return STR


MODULES["string"] = module_with_params(runtime_module(
    "string", "modules/string.hpp",
    capwords=(signature(STR, ("s", STR), ("sep", OptionalType(STR), "std::nullopt")), "sd::stringmod::capwords"),
    Template=(signature(STR_TEMPLATE, ("template", STR)), "sd::stringmod::Template"),
    **{name: (STR, f"sd::stringmod::{name}") for name in (
        "ascii_letters", "ascii_lowercase", "ascii_uppercase", "digits", "hexdigits", "octdigits",
        "punctuation", "printable", "whitespace")},
))
MODULES["string"].members["Template"].as_type = STR_TEMPLATE
STR_TEMPLATE.methods.update({"substitute": template_substitute, "safe_substitute": template_substitute,
                             "get_identifiers": sync_method(ListType(STR)), "is_valid": sync_method(BOOL)})
STR_TEMPLATE.attributes["template"] = lambda t: STR


# ---- zlib ----------------------------------------------------------------------------

MODULES["zlib"] = module_with_params(runtime_module(
    "zlib", "modules/zlib.hpp", ("z",),
    compress=(bytes_fn(BYTES, 1, 2, (INT,)), "sd::zlib::compress"),
    decompress=(bytes_fn(BYTES), "sd::zlib::decompress"),
    crc32=(bytes_fn(INT, 1, 2, (INT,)), "sd::zlib::crc32"),
    adler32=(bytes_fn(INT, 1, 2, (INT,)), "sd::zlib::adler32"),
    compressobj=(signature(ZLIB_COMPRESS, ("level", INT, "(-1_i)"), ("method", INT, "8_i"), ("wbits", INT, "15_i"),
                           ("memLevel", INT, "8_i"), ("strategy", INT, "0_i")), "sd::zlib::compressobj"),
    decompressobj=(signature(ZLIB_DECOMPRESS, ("wbits", INT, "15_i")), "sd::zlib::decompressobj"),
    error=exception_class("error", "sd::zlib::error"),
    **{name: (INT, f"({value}_i)") for name, value in ZLIB_CONSTANTS.items()},
    ZLIB_VERSION=(STR, "std::string(ZLIB_VERSION)"),
))

# The incremental (de)compressors' methods and attributes.
ZLIB_COMPRESS.methods.update({
    "compress": sync_method(BYTES, ("data", BYTES)), "flush": sync_method(BYTES, ("mode", INT, "4_i")),
    "copy": sync_method(ZLIB_COMPRESS)})
ZLIB_DECOMPRESS.methods.update({
    "decompress": sync_method(BYTES, ("data", BYTES), ("max_length", INT, "0_i")),
    "flush": sync_method(BYTES, ("length", INT, "16384_i")), "copy": sync_method(ZLIB_DECOMPRESS)})
ZLIB_DECOMPRESS.attributes.update(
    {"eof": lambda t: BOOL, "unused_data": lambda t: BYTES, "unconsumed_tail": lambda t: BYTES})
for _t in (BZ2_COMPRESSOR, LZMA_COMPRESSOR):
    _t.methods.update({"compress": sync_method(BYTES, ("data", BYTES)), "flush": sync_method(BYTES)})
for _t in (BZ2_DECOMPRESSOR, LZMA_DECOMPRESSOR):
    _t.methods.update({"decompress": sync_method(BYTES, ("data", BYTES), ("max_length", INT, "(-1_i)"))})
    _t.attributes.update({"eof": lambda t: BOOL, "needs_input": lambda t: BOOL, "unused_data": lambda t: BYTES})
LZMA_DECOMPRESSOR.attributes["check"] = lambda t: INT


# ---- binascii ------------------------------------------------------------------------

MODULES["binascii"] = module_with_params(runtime_module(
    "binascii", "modules/binascii.hpp", ("z",),
    hexlify=(signature(BYTES, ("data", BYTES), ("sep", OptionalType(STR), "std::nullopt"), ("bytes_per_sep", INT, "1_i")),
             "sd::binascii::hexlify"),
    b2a_hex=(signature(BYTES, ("data", BYTES), ("sep", OptionalType(STR), "std::nullopt"), ("bytes_per_sep", INT, "1_i")),
             "sd::binascii::hexlify"),
    unhexlify=(bytes_fn(BYTES), "sd::binascii::unhexlify"),
    a2b_hex=(bytes_fn(BYTES), "sd::binascii::unhexlify"),
    crc32=(bytes_fn(INT, 1, 2, (INT,)), "sd::binascii::crc32"),
    crc_hqx=(signature(INT, ("data", BYTES), ("value", INT)), "sd::binascii::crc_hqx"),
    b2a_base64=(signature(BYTES, ("data", BYTES), ("newline", BOOL, "true")), "sd::binascii::b2a_base64"),
    a2b_base64=(bytes_fn(BYTES), "sd::binascii::a2b_base64"),
    Error=exception_class("Error", "sd::binascii::Error", "ValueError"),
))


# ---- gzip ----------------------------------------------------------------------------

# gzip.open(filename, mode="rb", compresslevel=9, ...), bz2.open (the same) and
# lzma.open(filename, mode="rb", *, format=None, check=-1, preset=None, ...): binary unless
# the mode says 't', like Python (and unlike open()). Codegen passes these options in order.
COMPRESSED_OPEN_OPTIONS = {
    "gzip": (("compresslevel", INT, "9_i"),),
    "bz2": (("compresslevel", INT, "9_i"),),
    "lzma": (("format", OptionalType(INT), "std::nullopt"), ("check", INT, "(-1_i)"), ("preset", OptionalType(INT), "std::nullopt")),
}


def compressed_open(module: str, binary_only: bool = False) -> Callable[[CallContext], Type]:
    """gzip.open and friends; binary_only for the GzipFile/BZ2File/LZMAFile classes, which
    are the binary file object (mode "r" means "rb"; text modes are an error)."""
    options = COMPRESSED_OPEN_OPTIONS[module]
    positional = 3 if module != "lzma" else 2  # (lzma's options are keyword-only)

    def handler(ctx: CallContext) -> Type:
        n = ctx.arity(0, positional, keywords=("filename", "mode", *(o[0] for o in options), "encoding", "newline"))
        if n == 0:
            if ctx.keyword_arg("filename") is None:
                raise ctx.error(f"{ctx.what} needs a filename (an existing file object isn't supported yet)")
            ctx.keyword("filename", STR)
        else:
            ctx.need(0, lambda t: t in (STR, PATH), "a str or Path")
        mode_node = ctx.args[1] if n >= 2 else ctx.keyword_arg("mode")
        if binary_only and isinstance(mode_node, A.StrLit) and "t" in mode_node.value:
            raise ctx.error(f"Invalid mode: {mode_node.value!r} ({ctx.what} is a binary file; use {module}.open for text)", mode_node)
        if n == 3:
            ctx.expect(2, options[0][1])
        else:
            for name, t, _ in options:
                ctx.keyword(name, t)
        if (nl := ctx.keyword_arg("newline")) is not None:
            ctx.checker.check_expr(nl)
        if mode_node is None:
            return BINARY_FILE
        open_mode(ctx, mode_node)
        return TEXT_FILE if "t" in mode_node.value else BINARY_FILE

    return handler


MODULES["gzip"] = module_with_params(runtime_module(
    "gzip", "modules/gzip.hpp", ("z",),
    compress=(signature(BYTES, ("data", BYTES), ("compresslevel", INT, "9_i"), ("mtime", OptionalType(INT), "std::nullopt")),
              "sd::gzip::compress"),
    decompress=(signature(BYTES, ("data", BYTES)), "sd::gzip::decompress"),
    open=(compressed_open("gzip"), None),
    GzipFile=(compressed_open("gzip", binary_only=True), None),
    BadGzipFile=exception_class("BadGzipFile", "sd::gzip::BadGzipFile", "OSError"),
))
MODULES["gzip"].members["GzipFile"].as_type = BINARY_FILE

MODULES["bz2"] = module_with_params(runtime_module(
    "bz2", "modules/bz2.hpp", ("bz2",),
    compress=(signature(BYTES, ("data", BYTES), ("compresslevel", INT, "9_i")), "sd::bz2::compress"),
    decompress=(signature(BYTES, ("data", BYTES)), "sd::bz2::decompress"),
    open=(compressed_open("bz2"), None),
    BZ2File=(compressed_open("bz2", binary_only=True), None),
    BZ2Compressor=(signature(BZ2_COMPRESSOR, ("compresslevel", INT, "9_i")), "sd::bz2::BZ2Compressor"),
    BZ2Decompressor=(signature(BZ2_DECOMPRESSOR), "sd::bz2::BZ2Decompressor"),
))
MODULES["bz2"].members["BZ2File"].as_type = BINARY_FILE
MODULES["bz2"].members["BZ2Compressor"].as_type = BZ2_COMPRESSOR
MODULES["bz2"].members["BZ2Decompressor"].as_type = BZ2_DECOMPRESSOR

MODULES["lzma"] = module_with_params(runtime_module(
    "lzma", "modules/lzma.hpp", ("lzma",),
    compress=(signature(BYTES, ("data", BYTES), ("format", INT, "sd::lzma::FORMAT_XZ"), ("check", INT, "(-1_i)"),
                        ("preset", OptionalType(INT), "std::nullopt")), "sd::lzma::compress"),
    decompress=(signature(BYTES, ("data", BYTES), ("format", INT, "sd::lzma::FORMAT_AUTO")), "sd::lzma::decompress"),
    open=(compressed_open("lzma"), None),
    LZMAFile=(compressed_open("lzma", binary_only=True), None),
    LZMACompressor=(signature(LZMA_COMPRESSOR, ("format", INT, "sd::lzma::FORMAT_XZ"), ("check", INT, "(-1_i)"),
                              ("preset", OptionalType(INT), "std::nullopt")), "sd::lzma::LZMACompressor"),
    LZMADecompressor=(signature(LZMA_DECOMPRESSOR, ("format", INT, "sd::lzma::FORMAT_AUTO"), ("memlimit", OptionalType(INT), "std::nullopt")),
                      "sd::lzma::LZMADecompressor"),
    is_check_supported=(signature(BOOL, ("check_id", INT)), "sd::lzma::is_check_supported"),
    LZMAError=exception_class("LZMAError", "sd::lzma::LZMAError"),
    **{name: (INT, f"sd::lzma::{name}") for name in (
        "FORMAT_AUTO", "FORMAT_XZ", "FORMAT_ALONE", "FORMAT_RAW", "CHECK_NONE", "CHECK_CRC32", "CHECK_CRC64", "CHECK_SHA256",
        "CHECK_UNKNOWN", "PRESET_DEFAULT", "PRESET_EXTREME")},
))


# ---- uuid ----------------------------------------------------------------------------

MODULES["uuid"] = module_with_params(runtime_module(
    "uuid", "modules/uuid.hpp", ("crypto",),
    UUID=(signature(UUID_T, ("hex", OptionalType(STR), "std::nullopt"), ("bytes", OptionalType(BYTES), "std::nullopt"),
                    ("version", OptionalType(INT), "std::nullopt")), "sd::uuid::UUID::make"),
    uuid1=(signature(UUID_T, ("node", OptionalType(INT), "std::nullopt"), ("clock_seq", OptionalType(INT), "std::nullopt")),
           "sd::uuid::uuid1"),
    uuid3=(signature(UUID_T, ("namespace", UUID_T), ("name", STR)), "sd::uuid::uuid3"),
    uuid4=(signature(UUID_T), "sd::uuid::uuid4"),
    uuid5=(signature(UUID_T, ("namespace", UUID_T), ("name", STR)), "sd::uuid::uuid5"),
    **{ns: (UUID_T, f"sd::uuid::{ns}") for ns in ("NAMESPACE_DNS", "NAMESPACE_URL", "NAMESPACE_OID", "NAMESPACE_X500")},
    RESERVED_NCS=(STR, '"reserved for NCS compatibility"s'),
    RFC_4122=(STR, '"specified in RFC 4122"s'),
    RESERVED_MICROSOFT=(STR, '"reserved for Microsoft compatibility"s'),
    RESERVED_FUTURE=(STR, '"reserved for future definition"s'),
))
MODULES["uuid"].members["UUID"].as_type = UUID_T
UUID_T.attributes.update({
    "hex": lambda t: STR, "bytes": lambda t: BYTES, "version": lambda t: OptionalType(INT),
    "variant": lambda t: STR, "urn": lambda t: STR, "fields": lambda t: TupleType((INT,) * 6),
    **{f: (lambda t: INT) for f in ("time_low", "time_mid", "time_hi_version", "clock_seq_hi_variant",
                                    "clock_seq_low", "node", "clock_seq", "time")},
})


# ---- errno ---------------------------------------------------------------------------

ERRNO_NAMES = (
    "EPERM", "ENOENT", "ESRCH", "EINTR", "EIO", "ENXIO", "E2BIG", "ENOEXEC", "EBADF",
    "ECHILD", "EAGAIN", "ENOMEM", "EACCES", "EFAULT", "ENOTBLK", "EBUSY", "EEXIST", "EXDEV",
    "ENODEV", "ENOTDIR", "EISDIR", "EINVAL", "ENFILE", "EMFILE", "ENOTTY", "ETXTBSY", "EFBIG",
    "ENOSPC", "ESPIPE", "EROFS", "EMLINK", "EPIPE", "EDOM", "ERANGE", "EDEADLK", "ENAMETOOLONG",
    "ENOLCK", "ENOSYS", "ENOTEMPTY", "ELOOP", "EWOULDBLOCK", "ENOMSG", "EIDRM", "ENOSTR", "ENODATA",
    "ETIME", "ENOSR", "EREMOTE", "ENOLINK", "EPROTO", "EMULTIHOP", "EBADMSG", "EOVERFLOW", "EILSEQ",
    "EUSERS", "ENOTSOCK", "EDESTADDRREQ", "EMSGSIZE", "EPROTOTYPE", "ENOPROTOOPT", "EPROTONOSUPPORT", "ESOCKTNOSUPPORT", "EOPNOTSUPP",
    "ENOTSUP", "EPFNOSUPPORT", "EAFNOSUPPORT", "EADDRINUSE", "EADDRNOTAVAIL", "ENETDOWN", "ENETUNREACH", "ENETRESET", "ECONNABORTED",
    "ECONNRESET", "ENOBUFS", "EISCONN", "ENOTCONN", "ESHUTDOWN", "ETOOMANYREFS", "ETIMEDOUT", "ECONNREFUSED", "EHOSTDOWN",
    "EHOSTUNREACH", "EALREADY", "EINPROGRESS", "ESTALE", "EDQUOT", "ECANCELED", "EOWNERDEAD", "ENOTRECOVERABLE",
)

MODULES["errno"] = runtime_module(
    "errno", "modules/errno.hpp",
    **{name: (INT, f"static_cast<std::int64_t>({name})") for name in ERRNO_NAMES},
    errorcode=(DictType(INT, STR), "sd::errnomod::errorcode()"),
)


# ---- stat ----------------------------------------------------------------------------

STAT_CONSTANTS = (
    "S_IFDIR", "S_IFCHR", "S_IFBLK", "S_IFREG", "S_IFIFO", "S_IFLNK", "S_IFSOCK", "S_ISUID",
    "S_ISGID", "S_ISVTX", "S_ENFMT", "S_IREAD", "S_IWRITE", "S_IEXEC", "S_IRWXU", "S_IRUSR",
    "S_IWUSR", "S_IXUSR", "S_IRWXG", "S_IRGRP", "S_IWGRP", "S_IXGRP", "S_IRWXO", "S_IROTH",
    "S_IWOTH", "S_IXOTH",
)

MODULES["stat"] = module_with_params(runtime_module(
    "stat", "modules/stat.hpp",
    **{name: (INT, f"static_cast<std::int64_t>({name})") for name in STAT_CONSTANTS},
    **{f"S_IS{kind.upper()}": (signature(BOOL, ("mode", INT)), f"sd::statmod::is_{kind}")
       for kind in ("dir", "chr", "blk", "reg", "fifo", "lnk", "sock")},
    S_IMODE=(signature(INT, ("mode", INT)), "sd::statmod::imode"),
    S_IFMT=(signature(INT, ("mode", INT)), "sd::statmod::ifmt"),
    filemode=(signature(STR, ("mode", INT)), "sd::statmod::filemode"),
))


# ---- sqlite3 -------------------------------------------------------------------------

SQLITE_VALUE_TYPES = (INT, FLOAT, STR, BYTES, BOOL)


def sqlite_value(t: Type) -> bool:
    return t in SQLITE_VALUE_TYPES or t == NONE or (isinstance(t, OptionalType) and t.inner in SQLITE_VALUE_TYPES)


def sqlite_parameters(ctx: CallContext, node: A.Expr, what: str) -> None:
    """execute()'s parameters: a tuple (any mix), a list, or a dict of :names, holding
    int, float, str, bytes, bool or None."""
    t = ctx.checker.check_expr(node)
    ok = (isinstance(t, TupleType) and all(sqlite_value(e) for e in t.elts)) or \
        (isinstance(t, (ListType, VarTupleType)) and sqlite_value(t.elem)) or \
        (isinstance(t, DictType) and t.key == STR and sqlite_value(t.value))
    if not ok:
        raise ctx.error(f"{what} must be a tuple, list or dict of int, float, str, bytes, bool or None values, not {t}", node)


def sqlite_execute(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2, keywords=("parameters",))
    ctx.expect(0, STR)
    node = ctx.args[1] if n == 2 else ctx.keyword_arg("parameters")
    if node is not None:
        sqlite_parameters(ctx, node, f"{ctx.what} parameters")
    return SQLITE_CURSOR


def sqlite_executemany(ctx: CallContext) -> Type:
    ctx.arity(2)
    ctx.expect(0, STR)
    t = ctx.checker.check_expr(ctx.args[1])
    if not isinstance(t, ListType):
        raise ctx.error(f"{ctx.what} takes a list of parameter tuples, not {t}", ctx.args[1])
    elem = t.elem
    ok = (isinstance(elem, TupleType) and all(sqlite_value(e) for e in elem.elts)) or \
        (isinstance(elem, (ListType, VarTupleType)) and sqlite_value(elem.elem)) or \
        (isinstance(elem, DictType) and elem.key == STR and sqlite_value(elem.value))
    if not ok:
        raise ctx.error(f"{ctx.what} takes a list of tuples (or lists, or dicts) of int, float, str, bytes, bool or None values, not {t}", ctx.args[1])
    return SQLITE_CURSOR


def sqlite_fetch(kind: str) -> Callable[[CallContext], Type]:
    """fetchone() / fetchmany(size) / fetchall(): the row type comes from the context, as
    with json.loads: `row: tuple[int, str] | None = cur.fetchone()`."""

    def handler(ctx: CallContext) -> Type:
        n = ctx.arity(0, 1 if kind == "many" else 0, keywords=("size",) if kind == "many" else ())
        if kind == "many":
            if n == 1:
                ctx.expect(0, INT)
            else:
                ctx.keyword("size", INT)
        target = ctx.expected
        row = None
        if kind == "one" and isinstance(target, OptionalType):
            row = target.inner
        elif kind == "one" and isinstance(target, TupleType):
            row = target
        elif kind != "one" and isinstance(target, ListType):
            row = target.elem
        example = "row: tuple[int, str] | None = cur.fetchone()" if kind == "one" else f"rows: list[tuple[int, str]] = cur.fetch{kind}()"
        if not isinstance(row, TupleType):
            raise ctx.error(f"{ctx.what} needs to know the row type; annotate the variable, e.g. `{example}`")
        for elt in row.elts:
            if not (elt in SQLITE_VALUE_TYPES or (isinstance(elt, OptionalType) and elt.inner in SQLITE_VALUE_TYPES)):
                raise ctx.error(f"a row column is int, float, str, bytes or bool (or one of those | None), not {elt}")
        ctx.call.sqlite_row = row
        return OptionalType(row) if kind == "one" else ListType(row)

    return handler


SQLITE_CURSOR.methods.update({
    "execute": sqlite_execute,
    "executemany": sqlite_executemany,
    "executescript": sync_method(SQLITE_CURSOR, ("sql_script", STR)),
    "fetchone": sqlite_fetch("one"),
    "fetchmany": sqlite_fetch("many"),
    "fetchall": sqlite_fetch("all"),
    "close": sync_method(NONE),
})
SQLITE_CONNECTION.methods.update({
    "cursor": sync_method(SQLITE_CURSOR),
    "execute": sqlite_execute,
    "executemany": sqlite_executemany,
    "executescript": sync_method(SQLITE_CURSOR, ("sql_script", STR)),
    "commit": sync_method(NONE),
    "rollback": sync_method(NONE),
    "close": sync_method(NONE),
})
SQLITE_CONNECTION.attributes.update(
    {"in_transaction": lambda t: BOOL, "total_changes": lambda t: INT, "isolation_level": lambda t: OptionalType(STR)})
SQLITE_CURSOR.attributes.update({
    "rowcount": lambda t: INT, "lastrowid": lambda t: OptionalType(INT),
    "description": lambda t: OptionalType(ListType(TupleType((STR, NONE, NONE, NONE, NONE, NONE, NONE)))),
})

SQLITE_ERRORS: dict[str, StructType] = {}


def sqlite_exception(name: str, base: str) -> StructType:
    st = StructType(name, "class", None, base=SQLITE_ERRORS.get(base) or EXCEPTIONS[base], builtin=True, cpp_name=f"sd::sqlite3::{name}")
    SQLITE_ERRORS[name] = st
    return st


MODULES["sqlite3"] = module_with_params(runtime_module(
    "sqlite3", "modules/sqlite3.hpp", ("sqlite3",),
    connect=(signature(SQLITE_CONNECTION, ("database", PATH_LIKE), ("timeout", FLOAT, "5.0"),
                       ("isolation_level", OptionalType(STR), "std::string()")), "sd::sqlite3::connect"),
    Connection=NamedType("Connection", SQLITE_CONNECTION),
    Cursor=NamedType("Cursor", SQLITE_CURSOR),
    sqlite_version=(STR, "sd::sqlite3::version()"),
    Warning=sqlite_exception("Warning", "Exception"),
    Error=sqlite_exception("Error", "Exception"),
    InterfaceError=sqlite_exception("InterfaceError", "Error"),
    DatabaseError=sqlite_exception("DatabaseError", "Error"),
    DataError=sqlite_exception("DataError", "DatabaseError"),
    OperationalError=sqlite_exception("OperationalError", "DatabaseError"),
    IntegrityError=sqlite_exception("IntegrityError", "DatabaseError"),
    InternalError=sqlite_exception("InternalError", "DatabaseError"),
    ProgrammingError=sqlite_exception("ProgrammingError", "DatabaseError"),
    NotSupportedError=sqlite_exception("NotSupportedError", "DatabaseError"),
))


# ---- struct --------------------------------------------------------------------------

STRUCT_CODES = {**{c: INT for c in "bBhHiIlLqQnNP"}, "?": BOOL, "e": FLOAT, "f": FLOAT, "d": FLOAT, "c": BYTES, "s": BYTES, "p": BYTES}


def struct_types(fmt: str) -> list[Type]:
    """The value types a format packs/unpacks, in order (the runtime checks the same rules)."""
    i = 0
    native = True
    if fmt and fmt[0] in "@=<>!":
        native = fmt[0] == "@"
        i = 1
    out: list[Type] = []
    while i < len(fmt):
        if fmt[i].isspace():
            i += 1
            continue
        count = 1
        if fmt[i].isdigit():
            start = i
            while i < len(fmt) and fmt[i].isdigit():
                i += 1
            count = int(fmt[start:i])
            if i >= len(fmt):
                raise ValueError("repeat count given without format specifier")
        c = fmt[i]
        i += 1
        if c == "x":
            continue
        if c not in STRUCT_CODES or (not native and c in "nNP"):
            raise ValueError("bad char in struct format")
        out.extend([BYTES] if c in "sp" else [STRUCT_CODES[c]] * count)
    return out


def struct_format(ctx: CallContext) -> list[Type]:
    """The format literal of a struct call (its first argument), as value types."""
    if isinstance(ctx.receiver, StructFormatType):
        fmt = ctx.receiver.fmt
    else:
        node = ctx.args[0] if ctx.args else ctx.keyword_arg("format")
        if not isinstance(node, A.StrLit):
            raise ctx.error(f"{ctx.what} needs its format as a string literal (it decides the types)", node)
        ctx.checker.check_expr(node)
        fmt = node.value
    try:
        return struct_types(fmt)
    except ValueError as e:
        raise ctx.error(str(e), ctx.args[0] if ctx.args else None)


def struct_pack(ctx: CallContext) -> Type:
    ctx.arity(0 if isinstance(ctx.receiver, StructFormatType) else 1, MANY)
    wanted = struct_format(ctx)
    values = ctx.args[0 if isinstance(ctx.receiver, StructFormatType) else 1:]
    if len(values) != len(wanted):
        raise ctx.error(f"pack expected {len(wanted)} items for packing (got {len(values)})")
    for node, t in zip(values, wanted):
        actual = ctx.checker.check_expr(node, t)
        ok = assignable(actual, t) or (t == FLOAT and actual == INT) or (t == BOOL and actual == INT) or (t == INT and actual == BOOL)
        if not ok:
            raise ctx.error(f"{ctx.what}: this value must be {t}, not {actual}", node)
    ctx.call.struct_args = wanted
    return BYTES


def struct_unpack(kind: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        on_object = isinstance(ctx.receiver, StructFormatType)
        lo = 1 if on_object else 2
        n = ctx.arity(lo, lo + 1 if kind == "unpack_from" else lo, keywords=("offset",) if kind == "unpack_from" else ())
        wanted = struct_format(ctx)
        ctx.expect(lo - 1, BYTES)
        if kind == "unpack_from":
            if n == lo + 1:
                ctx.expect(lo, INT)
            else:
                ctx.keyword("offset", INT)
        row = TupleType(tuple(wanted))
        ctx.call.struct_row = row
        return GeneratorType(row) if kind == "iter_unpack" else row

    return handler


def struct_new(ctx: CallContext) -> Type:
    ctx.arity(1)
    node = ctx.args[0]
    if not isinstance(node, A.StrLit):
        raise ctx.error("Struct() needs its format as a string literal (it decides the types)", node)
    ctx.checker.check_expr(node)
    try:
        struct_types(node.value)
    except ValueError as e:
        raise ctx.error(str(e), node)
    return StructFormatType(node.value)


STRUCT_METHODS = {
    "pack": struct_pack,
    "unpack": struct_unpack("unpack"),
    "unpack_from": struct_unpack("unpack_from"),
    "iter_unpack": struct_unpack("iter_unpack"),
}

MODULES["struct"] = module_with_params(runtime_module(
    "struct", "modules/struct.hpp",
    pack=(struct_pack, None),
    unpack=(struct_unpack("unpack"), None),
    unpack_from=(struct_unpack("unpack_from"), None),
    iter_unpack=(struct_unpack("iter_unpack"), None),
    calcsize=(signature(INT, ("format", STR)), "sd::structmod::calcsize"),
    Struct=(struct_new, None),
    error=exception_class("error", "sd::structmod::error"),
))


# ---- shlex ----------------------------------------------------------------------------

MODULES["shlex"] = module_with_params(runtime_module(
    "shlex", "modules/shlex.hpp",
    split=(signature(ListType(STR), ("s", STR), ("comments", BOOL, "false"), ("posix", BOOL, "true")), "sd::shlex::split"),
    quote=(signature(STR, ("s", STR)), "sd::shlex::quote"),
    join=(signature(STR, ("split_command", ListType(STR))), "sd::shlex::join"),
))


# ---- mimetypes ------------------------------------------------------------------------
# Python 3.12's built-in tables only: the system's mime.types files aren't read.

MODULES["mimetypes"] = module_with_params(runtime_module(
    "mimetypes", "modules/mimetypes.hpp",
    guess_type=(signature(TupleType((OptionalType(STR), OptionalType(STR))), ("url", PATH_LIKE),
                          ("strict", BOOL, "true")), "sd::mimetypes::guess_type"),
    guess_extension=(signature(OptionalType(STR), ("type", STR), ("strict", BOOL, "true")),
                     "sd::mimetypes::guess_extension"),
    guess_all_extensions=(signature(ListType(STR), ("type", STR), ("strict", BOOL, "true")),
                          "sd::mimetypes::guess_all_extensions"),
    add_type=(signature(NONE, ("type", STR), ("ext", STR), ("strict", BOOL, "true")), "sd::mimetypes::add_type"),
    types_map=(DictType(STR, STR), "sd::mimetypes::types_map()"),
    common_types=(DictType(STR, STR), "sd::mimetypes::common_types()"),
    encodings_map=(DictType(STR, STR), "sd::mimetypes::encodings_map()"),
    suffix_map=(DictType(STR, STR), "sd::mimetypes::suffix_map()"),
))


# ---- html -----------------------------------------------------------------------------

MODULES["html"] = module_with_params(runtime_module(
    "html", "modules/html.hpp",
    escape=(signature(STR, ("s", STR), ("quote", BOOL, "true")), "sd::html::escape"),
    unescape=(signature(STR, ("s", STR)), "sd::html::unescape"),
))


# ---- hashlib, hmac ------------------------------------------------------------------------

HASH_NAMES = ("md5", "sha1", "sha224", "sha256", "sha384", "sha512", "sha3_224", "sha3_256", "sha3_384", "sha3_512",
              "blake2b", "blake2s", "shake_128", "shake_256")


def hash_data(ctx: CallContext, node: A.Expr, what: str) -> None:
    t = ctx.checker.check_expr(node, BYTES)
    if t == STR:
        raise ctx.error(f"{what}: strings must be encoded before hashing; use s.encode()", node)
    if t != BYTES:
        raise ctx.error(f"{what} must be bytes, not {t}", node)


def hash_constructor(name: str | None) -> Callable[[CallContext], Type]:
    """hashlib.sha256(data=b"") / hashlib.new(name, data=b"")."""
    def handler(ctx: CallContext) -> Type:
        params = ((("name", STR),) if name is None else ()) + (("data", None, True), ("string", None, True),
                                                             ("usedforsecurity", BOOL, True))
        args = bind_args(ctx, params)
        if name is None:
            ctx.checker.expect_type(args["name"], STR, "hashlib.new() name")
        for key in ("data", "string"):
            if key in args:
                hash_data(ctx, args[key], f"{ctx.what} data")
        if "usedforsecurity" in args:
            ctx.checker.expect_type(args["usedforsecurity"], BOOL, "usedforsecurity")
        ctx.call.hash_args = args
        return HASH

    return handler


def hash_update(ctx: CallContext) -> Type:
    ctx.arity(1)
    hash_data(ctx, ctx.args[0], f"{ctx.what} argument")
    return NONE


def digest_name(ctx: CallContext, node: A.Expr) -> None:
    """digestmod= / digest=: a name ('sha256') or a hashlib constructor (hashlib.sha256)."""
    target = None
    if isinstance(node, A.Attribute) and isinstance(node.value, A.Name) and ctx.checker.modules.get(node.value.id) is MODULES["hashlib"]:
        target = node.attr
    elif isinstance(node, A.Name) and node.id in ctx.checker.imported and ctx.checker.imported[node.id][0] is MODULES["hashlib"]:
        target = ctx.checker.imported[node.id][1]
    if target in HASH_NAMES:
        node.hash_name = target
        node.compile_time = True
        node.ty = STR
        return
    ctx.checker.expect_type(node, STR, f"{ctx.what} digest")


def hmac_new(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("key", None), ("msg", None, True), ("digestmod", None, True)))
    hash_data(ctx, args["key"], "hmac key")
    if "msg" in args and not isinstance(args["msg"], A.NoneLit):
        hash_data(ctx, args["msg"], "hmac msg")
    if "digestmod" not in args:
        raise ctx.error("hmac.new() needs digestmod= (like hashlib.sha256 or 'sha256')")
    digest_name(ctx, args["digestmod"])
    ctx.call.hash_args = args
    return HMAC_T


def hmac_digest(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("key", None), ("msg", None), ("digest", None)))
    hash_data(ctx, args["key"], "hmac key")
    hash_data(ctx, args["msg"], "hmac msg")
    digest_name(ctx, args["digest"])
    ctx.call.hash_args = args
    return BYTES


def compare_digest(ctx: CallContext) -> Type:
    ctx.arity(2)
    a, b = ctx.arg(0), ctx.arg(1)
    if a != b or a not in (STR, BYTES):
        raise ctx.error(f"compare_digest() compares two str or two bytes, not {a} and {b}")
    return BOOL


HASH.methods.update({
    "update": hash_update,
    "digest": sync_method(BYTES, ("length", OptionalType(INT), "std::nullopt")),
    "hexdigest": sync_method(STR, ("length", OptionalType(INT), "std::nullopt")),
    "copy": sync_method(HASH),
})
HMAC_T.methods.update({
    "update": hash_update,
    "digest": sync_method(BYTES),
    "hexdigest": sync_method(STR),
    "copy": sync_method(HMAC_T),
})
for _t in (HASH, HMAC_T):
    _t.attributes.update({"name": lambda t: STR, "digest_size": lambda t: INT, "block_size": lambda t: INT})

MODULES["hashlib"] = Module("hashlib", {
    **{name: Function(name, hash_constructor(name), as_type=HASH) for name in HASH_NAMES},
    "new": Function("new", hash_constructor(None)),
    "pbkdf2_hmac": Function("pbkdf2_hmac", signature(
        BYTES, ("hash_name", STR), ("password", BYTES), ("salt", BYTES), ("iterations", INT),
        ("dklen", OptionalType(INT), "std::nullopt")), "sd::hashlib::pbkdf2_hmac"),
    "file_digest": Function("file_digest", signature(HASH, ("fileobj", BINARY_FILE), ("digest", STR)),
                            "sd::hashlib::file_digest"),
    "algorithms_guaranteed": Value("algorithms_guaranteed", SetType(STR), "sd::hashlib::algorithms_guaranteed()"),
    "algorithms_available": Value("algorithms_available", SetType(STR), "sd::hashlib::algorithms_available()"),
}, "modules/hashlib.hpp", ("crypto",))
for _name in ("pbkdf2_hmac", "file_digest"):
    MODULES["hashlib"].members[_name].params = MODULES["hashlib"].members[_name].check.params
MODULES["hmac"] = Module("hmac", {
    "new": Function("new", hmac_new),
    "digest": Function("digest", hmac_digest),
    "compare_digest": Function("compare_digest", compare_digest, "sd::hmac::compare_digest"),
    "HMAC": NamedType("HMAC", HMAC_T),
}, "modules/hashlib.hpp", ("crypto",))


# ---- secrets -------------------------------------------------------------------------

MODULES["secrets"] = module_with_params(runtime_module(
    "secrets", "modules/secrets.hpp", ("crypto",),
    token_bytes=(signature(BYTES, ("nbytes", OptionalType(INT), "std::nullopt")), "sd::secrets::token_bytes"),
    token_hex=(signature(STR, ("nbytes", OptionalType(INT), "std::nullopt")), "sd::secrets::token_hex"),
    token_urlsafe=(signature(STR, ("nbytes", OptionalType(INT), "std::nullopt")), "sd::secrets::token_urlsafe"),
    randbelow=(signature(INT, ("exclusive_upper_bound", INT)), "sd::secrets::randbelow"),
    randbits=(signature(INT, ("k", INT)), "sd::secrets::randbits"),
    choice=(random_choice, "sd::secrets::choice"),
    compare_digest=(compare_digest, "sd::secrets::compare_digest"),
    DEFAULT_ENTROPY=(INT, "sd::secrets::DEFAULT_ENTROPY"),
))


# ---- concurrent.futures -------------------------------------------------------------------
#
# The work runs on other threads, so submit() and map() are checked like threading.Thread:
# arguments and results must be values that can be copied (or thread-safe objects), and
# the function mustn't reach shared mutable state (threads.Spawn).

def record_spawn(ctx: CallContext, fn: A.Expr, arg_nodes: list[A.Expr], arg_types: tuple, result: Type) -> None:
    args = A.TupleLit(list(arg_nodes), loc=ctx.call.loc)
    args.ty = TupleType(tuple(arg_types))
    ctx.call.spawn_extra = {"target": fn, "args": args if arg_nodes else None, "result": result}
    ctx.checker.spawns.append((ctx.call, ctx.checker.scope, ctx.checker.module_name))


def work_function(ctx: CallContext, fn: A.Expr, params: tuple) -> Type:
    ft = ctx.checker.check_expr(fn, FuncType(params, None))
    if not isinstance(ft, FuncType):
        raise ctx.error(f"{ctx.what} needs a function to run, not {ft}", fn)
    if len(ft.params) != len(params) or not all(assignable(a, p) for a, p in zip(params, ft.params)):
        takes = ", ".join(map(str, ft.params)) or "no arguments"
        given = ", ".join(map(str, params)) or "none"
        raise ctx.error(f"the function takes ({takes}), but it's given ({given})", fn)
    return ft.ret


def executor_submit(ctx: CallContext) -> Type:
    if not ctx.args:
        raise ctx.error("submit() needs the function to run")
    if ctx.call.keywords:
        raise ctx.error("submit() passes positional arguments only (wrap keyword arguments in a lambda)", ctx.call.keywords[0])
    arg_types = tuple(ctx.checker.check_expr(a) for a in ctx.args[1:])
    result = work_function(ctx, ctx.args[0], arg_types)
    record_spawn(ctx, ctx.args[0], ctx.args[1:], arg_types, result)
    ctx.call.work_types = (arg_types, result)
    return FutureType(result)


def executor_map(ctx: CallContext) -> Type:
    ctx.arity(2, MANY, keywords=("timeout", "chunksize"))
    elems = tuple(iterable_of(ctx, a) for a in ctx.args[1:])
    result = work_function(ctx, ctx.args[0], elems)
    if result == NONE:
        raise ctx.error("map()'s function must return a value (use submit() for work without a result)", ctx.args[0])
    ctx.keyword("timeout", OptionalType(FLOAT))
    ctx.keyword("chunksize", INT)
    record_spawn(ctx, ctx.args[0], ctx.args[1:], elems, result)
    ctx.call.work_types = (elems, result)
    return GeneratorType(result)


def future_callback(ctx: CallContext) -> Type:
    """The callback may run on the worker thread, so it's checked like work given to a thread."""
    ctx.arity(1)
    ctx.checker.expect_type(ctx.args[0], FuncType((ctx.receiver,), NONE), "add_done_callback() callback")
    record_spawn(ctx, ctx.args[0], [], (), None)
    return NONE


def futures_of(ctx: CallContext) -> FutureType:
    elem = iterable_of(ctx, ctx.args[0])
    if not isinstance(elem, FutureType):
        raise ctx.error(f"{ctx.what} needs futures, not {elem}", ctx.args[0])
    return elem


def futures_as_completed(ctx: CallContext) -> Type:
    ctx.arity(1, 2, keywords=("timeout",))
    f = futures_of(ctx)
    if len(ctx.args) == 2:
        ctx.expect(1, OptionalType(FLOAT))
    ctx.keyword("timeout", OptionalType(FLOAT))
    return GeneratorType(f)


def futures_wait(ctx: CallContext) -> Type:
    ctx.arity(1, 3, keywords=("timeout", "return_when"))
    f = futures_of(ctx)
    if len(ctx.args) > 1:
        ctx.expect(1, OptionalType(FLOAT))
    if len(ctx.args) > 2:
        ctx.expect(2, STR)
    ctx.keyword("timeout", OptionalType(FLOAT))
    ctx.keyword("return_when", STR)
    return TupleType((SetType(f), SetType(f)))


EXECUTOR.methods.update({
    "submit": executor_submit,
    "map": executor_map,
    "shutdown": sync_method(NONE, ("wait", BOOL, "true"), ("cancel_futures", BOOL, "false")),
})
FUTURE_METHODS = {
    "result": sync_method(lambda f: f.elem, ("timeout", OptionalType(FLOAT), "std::nullopt")),
    "exception": sync_method(OptionalType(EXCEPTIONS["Exception"]), ("timeout", OptionalType(FLOAT), "std::nullopt")),
    "done": sync_method(BOOL),
    "running": sync_method(BOOL),
    "cancelled": sync_method(BOOL),
    "cancel": sync_method(BOOL),
    "add_done_callback": future_callback,
}
FUTURES = module_with_params(runtime_module(
    "concurrent.futures", "modules/futures.hpp", ("pthread",),
    ThreadPoolExecutor=(signature(EXECUTOR, ("max_workers", OptionalType(INT), "std::nullopt"),
                                  ("thread_name_prefix", STR, '""s')), "sd::futures::ThreadPoolExecutor"),
    as_completed=(futures_as_completed, None),
    wait=(futures_wait, None),
    CancelledError=exception_class("CancelledError", "sd::futures::CancelledError"),
    TimeoutError=EXCEPTIONS["TimeoutError"],
    **{name: (STR, cpp_string_literal(name)) for name in ("FIRST_COMPLETED", "FIRST_EXCEPTION", "ALL_COMPLETED")},
))
FUTURES.members["ThreadPoolExecutor"].as_type = EXECUTOR
FUTURE_MARKER = NamedType("Future", FutureType(NONE))  # Future[T] in annotations (see checker.resolve_type_name)
FUTURES.members["Future"] = FUTURE_MARKER
MODULES["concurrent"] = Module("concurrent", {"futures": FUTURES})


# ---- sys streams ---------------------------------------------------------------------------

MODULES["sys"].members.update({
    "stdout": Value("stdout", TEXT_FILE, "sd::std_stream(1)"),
    "stderr": Value("stderr", TEXT_FILE, "sd::std_stream(2)"),
    "stdin": Value("stdin", TEXT_FILE, "sd::std_stream(0)"),
    "platform": Value("platform", STR, "std::string(SD_PLATFORM)"),
    "maxsize": Value("maxsize", INT, "std::int64_t{INT64_MAX}"),
})


# ---- logging -------------------------------------------------------------------------------
#
# Messages use Python's lazy %-style arguments: log.info("took %.2fs", t). The arguments are
# evaluated as usual, but only formatted if the record is emitted.

LOG_LEVELS = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40, "critical": 50, "fatal": 50,
              "exception": 40}


def log_call(level_name: str) -> Callable[[CallContext], Type]:
    """logging.info(msg, *args) / logger.info(...) / log(level, msg, *args)."""
    def handler(ctx: CallContext) -> Type:
        for kw in ctx.call.keywords:
            if kw.name not in ("exc_info", "stack_info", "stacklevel"):
                raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{kw.name}'", kw)
        ctx.keyword("exc_info", BOOL)
        ctx.keyword("stack_info", BOOL)
        ctx.keyword("stacklevel", INT)
        first = 0
        if level_name == "log":
            if not ctx.args:
                raise ctx.error("log() needs a level and a message")
            ctx.expect(0, INT)
            first = 1
        if len(ctx.args) <= first:
            raise ctx.error(f"{ctx.what} needs a message")
        for i in range(first, len(ctx.args)):
            ctx.need(i, printable, "something printable")
        ctx.call.log_call = {"level": level_name, "first": first}
        return NONE

    return handler


def level_arg(ctx: CallContext, node: A.Expr, what: str) -> None:
    t = ctx.checker.check_expr(node)
    if t not in (INT, STR):
        raise ctx.error(f"{what} must be a level: logging.INFO (an int) or 'INFO', not {t}", node)


def log_set_level(ctx: CallContext) -> Type:
    ctx.arity(1)
    level_arg(ctx, ctx.args[0], "setLevel()")
    return NONE


def log_basic_config(ctx: CallContext) -> Type:
    if ctx.args:
        raise ctx.error("basicConfig() takes keyword arguments only")
    types = {"format": STR, "datefmt": STR, "style": STR, "filemode": STR, "stream": TEXT_FILE,
             "handlers": ListType(LOG_HANDLER), "force": BOOL, "encoding": STR}
    for kw in ctx.call.keywords:
        if kw.name == "level":
            level_arg(ctx, kw.value, "basicConfig() level")
        elif kw.name == "filename":
            t = ctx.checker.check_expr(kw.value)
            if t not in (STR, PATH):
                raise ctx.error(f"basicConfig() filename must be a str or Path, not {t}", kw.value)
        elif kw.name in types:
            ctx.keyword(kw.name, types[kw.name])
        else:
            raise ctx.error(f"basicConfig() got an unexpected keyword argument '{kw.name}'", kw)
    return NONE


LOGGER.methods.update({
    **{name: log_call(name) for name in (*LOG_LEVELS, "log")},
    "setLevel": log_set_level,
    "addHandler": sync_method(NONE, ("hdlr", LOG_HANDLER)),
    "removeHandler": sync_method(NONE, ("hdlr", LOG_HANDLER)),
    "hasHandlers": sync_method(BOOL),
    "getEffectiveLevel": sync_method(INT),
    "isEnabledFor": sync_method(BOOL, ("level", INT)),
    "getChild": sync_method(LOGGER, ("suffix", STR)),
})
LOGGER.attributes.update({
    "name": lambda t: STR, "level": lambda t: INT, "propagate": lambda t: BOOL,
    "handlers": lambda t: ListType(LOG_HANDLER), "parent": lambda t: OptionalType(LOGGER),
})
LOG_HANDLER.methods.update({
    "setLevel": log_set_level,
    "setFormatter": sync_method(NONE, ("fmt", LOG_FORMATTER)),
    "flush": sync_method(NONE),
    "close": sync_method(NONE),
})
LOG = "sd::logging::"
MODULES["logging"] = module_with_params(runtime_module(
    "logging", "modules/logging.hpp",
    **{name: (log_call(name), None) for name in (*LOG_LEVELS, "log")},
    basicConfig=(log_basic_config, None),
    getLogger=(signature(LOGGER, ("name", OptionalType(STR), "std::nullopt")), LOG + "getLogger"),
    getLevelName=(signature(STR, ("level", INT)), LOG + "level_name"),
    disable=(signature(NONE, ("level", INT, LOG + "CRITICAL")), LOG + "disable"),
    StreamHandler=(signature(LOG_HANDLER, ("stream", OptionalType(TEXT_FILE), "std::nullopt")), LOG + "Handler::stream"),
    FileHandler=(signature(LOG_HANDLER, ("filename", STR), ("mode", STR, '"a"s'),
                           ("encoding", OptionalType(STR), "std::nullopt")), LOG + "Handler::file"),
    NullHandler=(signature(LOG_HANDLER), LOG + "Handler"),
    Formatter=(signature(LOG_FORMATTER, ("fmt", OptionalType(STR), "std::nullopt"),
                         ("datefmt", OptionalType(STR), "std::nullopt"), ("style", STR, '"%"s')), LOG + "Formatter"),
    **{name: (INT, LOG + name) for name in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "NOTSET")},
    WARN=(INT, LOG + "WARNING"), FATAL=(INT, LOG + "CRITICAL"),
    BASIC_FORMAT=(STR, LOG + "BASIC_FORMAT"),
    root=(LOGGER, LOG + "Logger::root()"),
))
for _name, _t in (("StreamHandler", LOG_HANDLER), ("FileHandler", LOG_HANDLER),
                  ("NullHandler", LOG_HANDLER), ("Formatter", LOG_FORMATTER)):
    MODULES["logging"].members[_name].as_type = _t
MODULES["logging"].members["Logger"] = NamedType("Logger", LOGGER)
MODULES["logging"].members["Handler"] = NamedType("Handler", LOG_HANDLER)


# ---- csv ----------------------------------------------------------------------------------

CSV_FORMAT = {"dialect": STR, "delimiter": STR, "quotechar": OptionalType(STR), "escapechar": OptionalType(STR),
              "doublequote": BOOL, "skipinitialspace": BOOL, "lineterminator": STR, "quoting": INT, "strict": BOOL}


def csv_format(ctx: CallContext, allowed: dict) -> dict:
    kw = {}
    for k in ctx.call.keywords:
        if k.name not in allowed and k.name not in CSV_FORMAT:
            raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{k.name}'", k)
        kw[k.name] = k.value
    for name, t in CSV_FORMAT.items():
        if name in kw:
            ctx.checker.expect_type(kw[name], t, name)
    return kw


def csv_lines(ctx: CallContext, node: A.Expr) -> None:
    elem = iterable_of(ctx, node, "csvfile")
    if elem != STR:
        raise ctx.error(f"{ctx.what} reads lines of text (a file opened in text mode, or strs), not {elem}", node)


def csv_reader(ctx: CallContext) -> Type:
    if len(ctx.args) not in (1, 2):
        raise ctx.error(f"{ctx.what} takes a file (or lines) and an optional dialect")
    csv_lines(ctx, ctx.args[0])
    if len(ctx.args) == 2:
        ctx.expect(1, STR)
    ctx.call.csv = csv_format(ctx, {})
    return GeneratorType(ListType(STR))


def csv_writer(ctx: CallContext) -> Type:
    if len(ctx.args) not in (1, 2):
        raise ctx.error(f"{ctx.what} takes a file and an optional dialect")
    ctx.expect(0, TEXT_FILE)
    if len(ctx.args) == 2:
        ctx.expect(1, STR)
    ctx.call.csv = csv_format(ctx, {})
    return CSV_WRITER


def csv_dict_reader(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("f", None), ("fieldnames", None, True), ("restkey", None, True), ("restval", None, True),
                           ("dialect", STR, True), *((k, t, True) for k, t in CSV_FORMAT.items() if k != "dialect")))
    csv_lines(ctx, args["f"])
    if "fieldnames" in args and not isinstance(args["fieldnames"], A.NoneLit):
        if iterable_of(ctx, args["fieldnames"], "fieldnames") != STR:
            raise ctx.error("fieldnames must be strs", args["fieldnames"])
    if "restkey" in args and not isinstance(args["restkey"], A.NoneLit):
        raise ctx.error("restkey isn't supported: seadash's DictReader rows are dict[str, str] (extra fields are dropped)",
                        args["restkey"])
    if "restval" in args and not isinstance(args["restval"], A.NoneLit):
        ctx.checker.expect_type(args["restval"], STR, "restval")
    for name, t in CSV_FORMAT.items():
        if name in args:
            ctx.checker.expect_type(args[name], t, name)
    ctx.call.csv = args
    return CSV_DICT_READER


def csv_dict_writer(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("f", None), ("fieldnames", None), ("restval", None, True), ("extrasaction", STR, True),
                           ("dialect", STR, True), *((k, t, True) for k, t in CSV_FORMAT.items() if k != "dialect")))
    ctx.checker.expect_type(args["f"], TEXT_FILE, "DictWriter file")
    if iterable_of(ctx, args["fieldnames"], "fieldnames") != STR:
        raise ctx.error("fieldnames must be strs", args["fieldnames"])
    if "restval" in args:
        ctx.checker.check_printable(ctx.checker.check_expr(args["restval"]), args["restval"])
    for name, t in (*CSV_FORMAT.items(), ("extrasaction", STR)):
        if name in args:
            ctx.checker.expect_type(args[name], t, name)
    ctx.call.csv = args
    return CSV_DICT_WRITER


def csv_row(ctx: CallContext, node: A.Expr) -> None:
    t = ctx.checker.check_expr(node)
    if isinstance(t, TupleType):
        for e, et in zip(node.elts if isinstance(node, A.TupleLit) else [node] * len(t.elts), t.elts):
            ctx.checker.check_printable(et, e)
        return
    elem = element_type(t)
    if elem is None or not printable(elem) or t == STR:
        raise ctx.error(f"a row is a list or tuple of values, not {t}", node)
    mark_tuple_iterable(node, t, elem)


def csv_writerow(ctx: CallContext) -> Type:
    ctx.arity(1)
    csv_row(ctx, ctx.args[0])
    return INT


def csv_writerows(ctx: CallContext) -> Type:
    ctx.arity(1)
    rows = ctx.checker.check_expr(ctx.args[0])
    row = element_type(rows)
    if row is None or not (isinstance(row, TupleType) or element_type(row) is not None) or row == STR:
        raise ctx.error(f"writerows() needs rows (lists or tuples of values), not {rows}", ctx.args[0])
    return NONE


def csv_dict_writerow(ctx: CallContext) -> Type:
    ctx.arity(1)
    t = ctx.arg(0)
    if not (isinstance(t, DictType) and t.key == STR and printable(t.value)):
        raise ctx.error(f"DictWriter.writerow() needs a dict with str keys, not {t}", ctx.args[0])
    return INT


def csv_dict_writerows(ctx: CallContext) -> Type:
    ctx.arity(1)
    row = iterable_of(ctx, ctx.args[0])
    if not (isinstance(row, DictType) and row.key == STR):
        raise ctx.error(f"DictWriter.writerows() needs dicts with str keys, not {row}", ctx.args[0])
    return NONE


MODULES["csv"] = module_with_params(runtime_module(
    "csv", "modules/csv.hpp",
    reader=(csv_reader, None),
    writer=(csv_writer, None),
    DictReader=(csv_dict_reader, None),
    DictWriter=(csv_dict_writer, None),
    Error=exception_class("Error", "sd::csv::Error"),
    **{name: (INT, f"sd::csv::{name}") for name in ("QUOTE_MINIMAL", "QUOTE_ALL", "QUOTE_NONNUMERIC", "QUOTE_NONE",
                                                   "QUOTE_STRINGS", "QUOTE_NOTNULL")},
))
for _name, _t in (("writer", CSV_WRITER), ("DictReader", CSV_DICT_READER), ("DictWriter", CSV_DICT_WRITER)):
    MODULES["csv"].members[_name].as_type = _t
CSV_WRITER.methods.update({"writerow": csv_writerow, "writerows": csv_writerows})
CSV_DICT_WRITER.methods.update({"writerow": csv_dict_writerow, "writerows": csv_dict_writerows,
                                "writeheader": sync_method(INT)})
CSV_DICT_READER.attributes["fieldnames"] = lambda t: ListType(STR)


# ---- ssl ----------------------------------------------------------------------------------
#
# As far as HTTPS clients need it: an SSLContext (a built-in class, so check_hostname and
# verify_mode are fields that can be set) and the functions that make one.

SSL_CONTEXT = StructType("SSLContext", "class", None, builtin=True, cpp_name="sd::ssl::SSLContext")
SSL_CONTEXT.fields["check_hostname"] = Field("check_hostname", BOOL, None, Loc(0, 0))
SSL_CONTEXT.fields["verify_mode"] = Field("verify_mode", INT, None, Loc(0, 0))
SSL_ERROR = exception_class("SSLError", "sd::ssl::SSLError", "OSError")
SSL_CERT_ERROR = StructType("SSLCertVerificationError", "class", None, base=SSL_ERROR, builtin=True,
                            cpp_name="sd::ssl::SSLCertVerificationError")
MODULES["ssl"] = module_with_params(runtime_module(
    "ssl", "modules/ssl.hpp", ("ssl", "crypto"),
    SSLContext=(signature(SSL_CONTEXT, ("protocol", INT, "sd::ssl::PROTOCOL_TLS_CLIENT")), "sd::ssl::SSLContext_new"),
    create_default_context=(signature(SSL_CONTEXT), "sd::ssl::create_default_context"),
    _create_unverified_context=(signature(SSL_CONTEXT), "sd::ssl::create_unverified_context"),
    SSLError=SSL_ERROR,
    SSLCertVerificationError=SSL_CERT_ERROR,
    **{c: (INT, f"sd::ssl::{c}") for c in ("CERT_NONE", "CERT_OPTIONAL", "CERT_REQUIRED", "PROTOCOL_TLS_CLIENT")},
))
MODULES["ssl"].members["SSLContext"].as_type = SSL_CONTEXT

# ---- urllib -------------------------------------------------------------------------------

OPT_STR = OptionalType(STR)
HTTP_RESPONSE.methods.update({
    "read": sync_method(BYTES, ("amt", OptionalType(INT), "std::nullopt")),
    "isclosed": sync_method(BOOL),
    "readline": sync_method(BYTES),
    "readlines": sync_method(ListType(BYTES)),
    "getheader": sync_method(OPT_STR, ("name", STR), ("default", OPT_STR, "std::nullopt")),
    "getheaders": sync_method(ListType(TupleType((STR, STR)))),
    "geturl": sync_method(STR),
    "getcode": sync_method(INT),
    "info": sync_method(HTTP_HEADERS),
    "close": sync_method(NONE),
})
HTTP_RESPONSE.attributes.update({
    "status": lambda t: INT, "code": lambda t: INT, "reason": lambda t: STR, "url": lambda t: STR,
    "headers": lambda t: HTTP_HEADERS, "msg": lambda t: HTTP_HEADERS, "version": lambda t: INT,
    "closed": lambda t: BOOL,
})
HTTP_HEADERS.methods.update({
    "get": sync_method(OPT_STR, ("name", STR), ("failobj", OPT_STR, "std::nullopt")),
    "get_all": sync_method(OptionalType(ListType(STR)), ("name", STR)),
    "items": sync_method(ListType(TupleType((STR, STR)))),
    "keys": sync_method(ListType(STR)),
    "values": sync_method(ListType(STR)),
    "get_content_type": sync_method(STR),
    "get_content_charset": sync_method(OPT_STR),
})
URL_REQUEST.methods.update({
    "add_header": sync_method(NONE, ("key", STR), ("val", STR)),
    "has_header": sync_method(BOOL, ("header_name", STR)),
    "get_header": sync_method(OPT_STR, ("header_name", STR), ("default", OPT_STR, "std::nullopt")),
    "get_method": sync_method(STR),
    "get_full_url": sync_method(STR),
    "header_items": sync_method(ListType(TupleType((STR, STR)))),
})
URL_REQUEST.attributes.update({
    "full_url": lambda t: STR, "data": lambda t: OptionalType(BYTES), "method": lambda t: OptionalType(STR),
    "headers": lambda t: DictType(STR, STR),
})
URL_PARTS.methods["geturl"] = sync_method(STR)
URL_PARTS.attributes.update({
    **{f: (lambda t: STR) for f in ("scheme", "netloc", "path", "params", "query", "fragment")},
    "hostname": lambda t: OptionalType(STR), "port": lambda t: OptionalType(INT),
    "username": lambda t: OptionalType(STR), "password": lambda t: OptionalType(STR),
})
EXCEPTION_METHODS = {  # methods of built-in classes (by C++ name): HTTPError is also a response
    "sd::ssl::SSLContext": {
        "load_default_certs": sync_method(NONE),
        "load_verify_locations": sync_method(NONE, ("cafile", OPT_STR, "std::nullopt"), ("capath", OPT_STR, "std::nullopt"),
                                             ("cadata", OPT_STR, "std::nullopt")),
    },
    "sd::urlerror::HTTPError": {
        "read": sync_method(BYTES, ("amt", OptionalType(INT), "std::nullopt")),
        "getcode": sync_method(INT),
        "geturl": sync_method(STR),
        "info": sync_method(HTTP_HEADERS),
    },
}


def url_data(ctx: CallContext, node: A.Expr, what: str) -> None:
    t = ctx.checker.check_expr(node, BYTES)
    if t != BYTES:
        hint = "; use s.encode() (or urlencode(fields).encode() for a form)" if t == STR else ""
        raise ctx.error(f"{what} must be bytes, not {t}{hint}", node)


def url_request(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("url", STR), ("data", None, True), ("headers", None, True), ("origin_req_host", None, True),
                           ("unverifiable", None, True), ("method", None, True)))
    ctx.checker.expect_type(args["url"], STR, "Request url")
    if "data" in args and not isinstance(args["data"], A.NoneLit):
        url_data(ctx, args["data"], "Request data")
    if "headers" in args:
        ctx.checker.expect_type(args["headers"], DictType(STR, STR), "Request headers")
    if "method" in args and not isinstance(args["method"], A.NoneLit):
        ctx.checker.expect_type(args["method"], STR, "Request method")
    for ignored in ("origin_req_host", "unverifiable"):
        if ignored in args:
            ctx.checker.check_expr(args[ignored])
    ctx.call.url_args = args
    return URL_REQUEST


def url_open(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("url", None), ("data", None, True), ("timeout", None, True), ("context", None, True)))
    if "context" in args and not isinstance(args["context"], A.NoneLit):
        ctx.checker.expect_type(args["context"], SSL_CONTEXT, "urlopen() context")
    t = ctx.checker.check_expr(args["url"])
    if t not in (STR, URL_REQUEST):
        raise ctx.error(f"urlopen() needs a URL (str) or a Request, not {t}", args["url"])
    if "data" in args and not isinstance(args["data"], A.NoneLit):
        url_data(ctx, args["data"], "urlopen() data")
    if "timeout" in args and not isinstance(args["timeout"], A.NoneLit):
        ctx.checker.expect_type(args["timeout"], FLOAT, "urlopen() timeout")
    ctx.call.url_args = args
    return HTTP_RESPONSE


def url_encode(ctx: CallContext) -> Type:
    ctx.arity(1, 2, keywords=("doseq",))
    t = ctx.arg(0)
    pairs = isinstance(t, DictType) or isinstance(element_type(t) or NONE, TupleType)
    if not pairs:
        raise ctx.error(f"urlencode() needs a dict or a list of (key, value) pairs, not {t}", ctx.args[0])
    if len(ctx.args) == 2:
        ctx.expect(1, BOOL)
    ctx.keyword("doseq", BOOL)
    return STR


URL_ERROR = StructType("URLError", "class", None, base=EXCEPTIONS["OSError"], builtin=True, cpp_name="sd::urlerror::URLError")
URL_ERROR.fields["reason"] = Field("reason", STR, None, Loc(0, 0))
HTTP_ERROR = StructType("HTTPError", "class", None, base=URL_ERROR, builtin=True, cpp_name="sd::urlerror::HTTPError")
for _field, _t in (("code", INT), ("msg", STR), ("headers", HTTP_HEADERS), ("url", STR)):
    HTTP_ERROR.fields[_field] = Field(_field, _t, None, Loc(0, 0))

URL_PARSE = module_with_params(runtime_module(
    "urllib.parse", "modules/urllib.hpp", ("ssl", "crypto"),
    quote=(signature(STR, ("string", STR), ("safe", STR, '"/"s')), "sd::urlparse::quote"),
    quote_plus=(signature(STR, ("string", STR), ("safe", STR, '""s')), "sd::urlparse::quote_plus"),
    unquote=(signature(STR, ("string", STR)), "sd::urlparse::unquote"),
    unquote_plus=(signature(STR, ("string", STR)), "sd::urlparse::unquote_plus"),
    urlencode=(url_encode, None),
    urlparse=(signature(URL_PARTS, ("urlstring", STR), ("scheme", STR, '""s'), ("allow_fragments", BOOL, "true")),
              "sd::urlparse::urlparse"),
    urlsplit=(signature(URL_PARTS, ("urlstring", STR), ("scheme", STR, '""s'), ("allow_fragments", BOOL, "true")),
              "sd::urlparse::urlsplit"),
    urljoin=(signature(STR, ("base", STR), ("url", STR), ("allow_fragments", BOOL, "true")), "sd::urlparse::urljoin"),
    parse_qs=(signature(DictType(STR, ListType(STR)), ("qs", STR), ("keep_blank_values", BOOL, "false"),
                        ("strict_parsing", BOOL, "false")), "sd::urlparse::parse_qs"),
    parse_qsl=(signature(ListType(TupleType((STR, STR))), ("qs", STR), ("keep_blank_values", BOOL, "false"),
                         ("strict_parsing", BOOL, "false")), "sd::urlparse::parse_qsl"),
))
URL_REQUEST_MOD = module_with_params(runtime_module(
    "urllib.request", "modules/urllib.hpp", ("ssl", "crypto"),
    urlopen=(url_open, None),
    Request=(url_request, None),
))
URL_REQUEST_MOD.members["Request"].as_type = URL_REQUEST
URL_ERROR_MOD = Module("urllib.error", {"URLError": URL_ERROR, "HTTPError": HTTP_ERROR}, "modules/urllib.hpp",
                       ("ssl", "crypto"))
MODULES["urllib"] = Module("urllib", {"parse": URL_PARSE, "request": URL_REQUEST_MOD, "error": URL_ERROR_MOD})

# ---- http.client --------------------------------------------------------------------------


def http_request(ctx: CallContext) -> Type:
    """conn.request(method, url, body=None, headers={}): body is bytes, or str (sent as Latin-1)."""
    args = bind_args(ctx, (("method", STR), ("url", STR), ("body", None, True), ("headers", None, True),
                           ("encode_chunked", None, True)))
    ctx.checker.expect_type(args["method"], STR, "request() method")
    ctx.checker.expect_type(args["url"], STR, "request() url")
    if "body" in args and not isinstance(args["body"], A.NoneLit):
        t = ctx.checker.check_expr(args["body"])
        if t not in (BYTES, STR):
            raise ctx.error(f"request() body must be bytes or str, not {t}", args["body"])
    if "headers" in args:
        ctx.checker.expect_type(args["headers"], DictType(STR, STR), "request() headers")
    if "encode_chunked" in args:
        raise ctx.error("request(encode_chunked=...) isn't supported yet", args["encode_chunked"])
    ctx.call.http_args = args
    return NONE


HTTP_CONNECTION.methods.update({
    "request": http_request,
    "getresponse": sync_method(HTTP_RESPONSE),
    "connect": sync_method(NONE),
    "close": sync_method(NONE),
    "putrequest": sync_method(NONE, ("method", STR), ("url", STR), ("skip_host", BOOL, "false"),
                              ("skip_accept_encoding", BOOL, "false")),
    "putheader": sync_method(NONE, ("header", STR), ("value", STR)),
    "endheaders": sync_method(NONE, ("message_body", OptionalType(BYTES), "std::nullopt")),
    "send": sync_method(NONE, ("data", BYTES)),
    "set_debuglevel": sync_method(NONE, ("level", INT)),
})
HTTP_CONNECTION.attributes.update({"host": lambda t: STR, "port": lambda t: INT, "timeout": lambda t: OPT_FLOAT})


def http_exception(name: str, base: StructType) -> StructType:
    return StructType(name, "class", None, base=base, builtin=True, cpp_name=f"sd::httpclient::{name}")


HTTP_EXCEPTION = http_exception("HTTPException", EXCEPTIONS["Exception"])
IMPROPER_STATE = http_exception("ImproperConnectionState", HTTP_EXCEPTION)
INCOMPLETE_READ = http_exception("IncompleteRead", HTTP_EXCEPTION)
INCOMPLETE_READ.fields["partial"] = Field("partial", BYTES, None, Loc(0, 0))
INCOMPLETE_READ.fields["expected"] = Field("expected", OptionalType(INT), None, Loc(0, 0))
HTTP_CLIENT_MOD = module_with_params(runtime_module(
    "http.client", "modules/httpclient.hpp", ("ssl", "crypto"),
    HTTPConnection=(signature(HTTP_CONNECTION, ("host", STR), ("port", OptionalType(INT), "std::nullopt"),
                              ("timeout", OPT_FLOAT, "std::nullopt")), "sd::httpclient::HTTPConnection"),
    HTTPSConnection=(signature(HTTP_CONNECTION, ("host", STR), ("port", OptionalType(INT), "std::nullopt"),
                               ("timeout", OPT_FLOAT, "std::nullopt"), ("context", OptionalType(SSL_CONTEXT), "std::nullopt")),
                     "sd::httpclient::HTTPSConnection"),
    responses=(DictType(INT, STR), "sd::httpclient::responses()"),
    HTTPException=HTTP_EXCEPTION,
    NotConnected=http_exception("NotConnected", HTTP_EXCEPTION),
    InvalidURL=http_exception("InvalidURL", HTTP_EXCEPTION),
    ImproperConnectionState=IMPROPER_STATE,
    CannotSendRequest=http_exception("CannotSendRequest", IMPROPER_STATE),
    CannotSendHeader=http_exception("CannotSendHeader", IMPROPER_STATE),
    ResponseNotReady=http_exception("ResponseNotReady", IMPROPER_STATE),
    BadStatusLine=http_exception("BadStatusLine", HTTP_EXCEPTION),
    LineTooLong=http_exception("LineTooLong", HTTP_EXCEPTION),
    RemoteDisconnected=http_exception("RemoteDisconnected", EXCEPTIONS["ConnectionResetError"]),
    IncompleteRead=INCOMPLETE_READ,
    **{"ACCEPTED": (INT, "202_i"), "ALREADY_REPORTED": (INT, "208_i"), "BAD_GATEWAY": (INT, "502_i"), "BAD_REQUEST": (INT, "400_i"), "CONFLICT": (INT, "409_i"), "CONTINUE": (INT, "100_i"), "CREATED": (INT, "201_i"), "EARLY_HINTS": (INT, "103_i"), "EXPECTATION_FAILED": (INT, "417_i"), "FAILED_DEPENDENCY": (INT, "424_i"), "FORBIDDEN": (INT, "403_i"), "FOUND": (INT, "302_i"), "GATEWAY_TIMEOUT": (INT, "504_i"), "GONE": (INT, "410_i"), "HTTPS_PORT": (INT, "443_i"), "HTTP_PORT": (INT, "80_i"), "HTTP_VERSION_NOT_SUPPORTED": (INT, "505_i"), "IM_A_TEAPOT": (INT, "418_i"), "IM_USED": (INT, "226_i"), "INSUFFICIENT_STORAGE": (INT, "507_i"), "INTERNAL_SERVER_ERROR": (INT, "500_i"), "LENGTH_REQUIRED": (INT, "411_i"), "LOCKED": (INT, "423_i"), "LOOP_DETECTED": (INT, "508_i"), "METHOD_NOT_ALLOWED": (INT, "405_i"), "MISDIRECTED_REQUEST": (INT, "421_i"), "MOVED_PERMANENTLY": (INT, "301_i"), "MULTIPLE_CHOICES": (INT, "300_i"), "MULTI_STATUS": (INT, "207_i"), "NETWORK_AUTHENTICATION_REQUIRED": (INT, "511_i"), "NON_AUTHORITATIVE_INFORMATION": (INT, "203_i"), "NOT_ACCEPTABLE": (INT, "406_i"), "NOT_EXTENDED": (INT, "510_i"), "NOT_FOUND": (INT, "404_i"), "NOT_IMPLEMENTED": (INT, "501_i"), "NOT_MODIFIED": (INT, "304_i"), "NO_CONTENT": (INT, "204_i"), "OK": (INT, "200_i"), "PARTIAL_CONTENT": (INT, "206_i"), "PAYMENT_REQUIRED": (INT, "402_i"), "PERMANENT_REDIRECT": (INT, "308_i"), "PRECONDITION_FAILED": (INT, "412_i"), "PRECONDITION_REQUIRED": (INT, "428_i"), "PROCESSING": (INT, "102_i"), "PROXY_AUTHENTICATION_REQUIRED": (INT, "407_i"), "REQUESTED_RANGE_NOT_SATISFIABLE": (INT, "416_i"), "REQUEST_ENTITY_TOO_LARGE": (INT, "413_i"), "REQUEST_HEADER_FIELDS_TOO_LARGE": (INT, "431_i"), "REQUEST_TIMEOUT": (INT, "408_i"), "REQUEST_URI_TOO_LONG": (INT, "414_i"), "RESET_CONTENT": (INT, "205_i"), "SEE_OTHER": (INT, "303_i"), "SERVICE_UNAVAILABLE": (INT, "503_i"), "SWITCHING_PROTOCOLS": (INT, "101_i"), "TEMPORARY_REDIRECT": (INT, "307_i"), "TOO_EARLY": (INT, "425_i"), "TOO_MANY_REQUESTS": (INT, "429_i"), "UNAUTHORIZED": (INT, "401_i"), "UNAVAILABLE_FOR_LEGAL_REASONS": (INT, "451_i"), "UNPROCESSABLE_ENTITY": (INT, "422_i"), "UNSUPPORTED_MEDIA_TYPE": (INT, "415_i"), "UPGRADE_REQUIRED": (INT, "426_i"), "USE_PROXY": (INT, "305_i"), "VARIANT_ALSO_NEGOTIATES": (INT, "506_i"), "_MAXHEADERS": (INT, "100_i"), "_MAXLINE": (INT, "65536_i"), "_MIN_READ_BUF_SIZE": (INT, "1048576_i")},
))
for _name in ("HTTPConnection", "HTTPSConnection"):
    HTTP_CLIENT_MOD.members[_name].as_type = HTTP_CONNECTION
HTTP_CLIENT_MOD.members["HTTPResponse"] = NamedType("HTTPResponse", HTTP_RESPONSE)
HTTP_CLIENT_MOD.members["HTTPMessage"] = NamedType("HTTPMessage", HTTP_HEADERS)

# ---- http.server ----------------------------------------------------------------------------
#
# BaseHTTPRequestHandler is a base class in the runtime (sd::httpserver), which a program
# subclasses with do_GET()... Its fields and methods are declared here as if it were written
# in seadash, so a subclass can use and override them (the C++ signatures match).


def runtime_method(owner: StructType, name: str, ret: Type, *params: tuple) -> FuncInfo:
    """A method of a runtime base class: params are (name, type[, default as a literal node])."""
    resolved = []
    for p in params:
        star = p[0].startswith("*")
        default = p[2] if len(p) > 2 else None
        resolved.append(Param(p[0].lstrip("*"), p[1], default, Loc(0, 0), star))
    # (a node like a parsed method's, self first, so the flow analyses can read it: an empty body keeps nothing)
    node = A.FunctionDef(name, [A.Param("self"), *(A.Param(p.name, None, p.default, p.star) for p in resolved)], None, [])
    node.params[0].sym = Var("self", "self", owner, "param", Loc(0, 0))
    for np, p in zip(node.params[1:], resolved):
        np.sym = Var(p.name, p.name, p.type, "param", Loc(0, 0))
    info = FuncInfo(name, resolved, ret, node, owner)
    node.sym = info
    owner.methods[name] = info
    return info


HANDLER = StructType("BaseHTTPRequestHandler", "class", None, builtin=True, runtime_fields=True,
                     cpp_name="sd::httpserver::BaseHTTPRequestHandler", module="http.server")
for _field, _t in (("command", STR), ("path", STR), ("request_version", STR), ("requestline", STR),
                   ("raw_requestline", BYTES), ("headers", HTTP_HEADERS), ("client_address", TupleType((STR, INT))),
                   ("rfile", BINARY_FILE), ("wfile", BINARY_FILE), ("close_connection", BOOL)):
    HANDLER.fields[_field] = Field(_field, _t, None, Loc(0, 0))
_NO_STR = A.NoneLit()
_OPT_STR = OptionalType(STR)
runtime_method(HANDLER, "send_response", NONE, ("code", INT), ("message", _OPT_STR, _NO_STR))
runtime_method(HANDLER, "send_response_only", NONE, ("code", INT), ("message", _OPT_STR, _NO_STR))
runtime_method(HANDLER, "send_header", NONE, ("keyword", STR), ("value", STR))
runtime_method(HANDLER, "end_headers", NONE)
runtime_method(HANDLER, "flush_headers", NONE)
runtime_method(HANDLER, "send_error", NONE, ("code", INT), ("message", _OPT_STR, _NO_STR), ("explain", _OPT_STR, _NO_STR))
runtime_method(HANDLER, "handle_expect_100", BOOL)
runtime_method(HANDLER, "log_request", NONE, ("code", STR, A.StrLit("-")), ("size", STR, A.StrLit("-")))
runtime_method(HANDLER, "log_error", NONE, ("format", STR), ("*args", VarTupleType(STR)))
runtime_method(HANDLER, "log_message", NONE, ("format", STR), ("*args", VarTupleType(STR)))
runtime_method(HANDLER, "version_string", STR)
runtime_method(HANDLER, "date_time_string", STR, ("timestamp", OptionalType(FLOAT), A.NoneLit()))
runtime_method(HANDLER, "log_date_time_string", STR)
runtime_method(HANDLER, "address_string", STR)
for _name, _value in (("server_version", "BaseHTTP/0.6"), ("sys_version", "seadash/0.0.1"), ("protocol_version", "HTTP/1.0"),
                      ("error_content_type", "text/html;charset=utf-8"), ("error_message_format", ""),
                      ("default_request_version", "HTTP/0.9")):
    HANDLER.class_attrs[_name] = ClassAttr(_name, STR, A.StrLit(_value), Loc(0, 0))


def handler_class(ctx: CallContext, node: A.Expr) -> StructType:
    """The RequestHandlerClass argument: a subclass of BaseHTTPRequestHandler, which the server
    makes one of for each connection (with no arguments)."""
    st = ctx.checker.lookup_struct(node.id) if isinstance(node, A.Name) else None
    if st is None or not st.is_subclass_of(HANDLER) or st is HANDLER:
        raise ctx.error(f"{ctx.what} needs the handler class itself (a subclass of BaseHTTPRequestHandler with do_GET... "
                        f"methods), not an instance or anything else", node)
    if st.init is not None and st.init.owner is not HANDLER:
        raise ctx.error(f"{st.name} can't have an __init__: the server makes a new {st.name} for each connection, "
                        f"with no arguments of yours (keep shared state in a module-level Synchronized object)", node)
    for t in st.ancestors():
        for m in t.methods.values():
            if m.name.startswith("do_") and (m.params or m.ret != NONE):
                raise ctx.error(f"{t.name}.{m.name}() is called for each request with no arguments (besides self), "
                                f"and returns nothing", m.node)
    for f in st.all_fields().values():
        if f.default is None:
            raise ctx.error(f"{st.name}.{f.name} needs a default value: the server makes a new {st.name} for each "
                            f"connection, with no arguments", node)
    node.sym, node.ty = st, ClassRefType(st)
    return st


def http_server_new(threading: bool):
    def check(ctx: CallContext) -> Type:
        args = bind_args(ctx, (("server_address", None), ("RequestHandlerClass", None), ("bind_and_activate", BOOL, True)))
        ctx.checker.expect_type(args["server_address"], TupleType((STR, INT)), "server_address")
        if "bind_and_activate" in args:
            ctx.checker.expect_type(args["bind_and_activate"], BOOL, "bind_and_activate")
        st = handler_class(ctx, args["RequestHandlerClass"])
        ctx.call.http_args = args  # (for codegen)
        t = HTTPServerType(st, threading)
        if threading:  # every request is handled on a thread of its own: the handler's code is thread code
            record_spawn(ctx, args["RequestHandlerClass"], [], (), None)
            ctx.call.spawn_extra["handler"] = st
        return t
    return check


HTTP_SERVER_METHODS = {
    "serve_forever": sync_method(NONE, ("poll_interval", FLOAT, "0.5")),
    "shutdown": sync_method(NONE),
    "handle_request": sync_method(NONE),
    "server_close": sync_method(NONE),
    "server_bind": sync_method(NONE),
    "server_activate": sync_method(NONE),
    "fileno": sync_method(INT),
}
HTTP_SERVER_ATTRIBUTES = {"server_address": lambda t: TupleType((STR, INT)), "server_port": lambda t: INT}
HTTP_SERVER_MOD = Module("http.server", {
    "HTTPServer": Function("HTTPServer", http_server_new(False), as_type=HTTPServerType()),
    "ThreadingHTTPServer": Function("ThreadingHTTPServer", http_server_new(True), as_type=HTTPServerType(None, True)),
    "BaseHTTPRequestHandler": HANDLER,
}, "modules/httpserver.hpp", ("ssl", "crypto", "pthread"))
MODULES["http"] = Module("http", {"client": HTTP_CLIENT_MOD, "server": HTTP_SERVER_MOD})

# ---- email.utils --------------------------------------------------------------------------

EMAIL_UTILS_MOD = module_with_params(runtime_module(
    "email.utils", "modules/emailutils.hpp",
    formatdate=(signature(STR, ("timeval", OPT_FLOAT, "std::nullopt"), ("localtime", BOOL, "false"),
                          ("usegmt", BOOL, "false")), "sd::emailutils::formatdate"),
    format_datetime=(signature(STR, ("dt", DATETIME), ("usegmt", BOOL, "false")), "sd::emailutils::format_datetime"),
    parsedate_to_datetime=(signature(DATETIME, ("data", STR)), "sd::emailutils::parsedate_to_datetime"),
    parsedate_tz=(signature(OptionalType(TupleType((INT,) * 10)), ("data", STR)), "sd::emailutils::parsedate_tz"),
    parsedate=(signature(OptionalType(TupleType((INT,) * 9)), ("data", STR)), "sd::emailutils::parsedate"),
))
MODULES["email"] = Module("email", {"utils": EMAIL_UTILS_MOD})



class AttributeUnavailable(Exception):
    """An attribute that exists, but not for this value (the checker adds the location)."""


# ---- subprocess ---------------------------------------------------------------------
#
# The result types follow the call's arguments, which must be written out: text=True
# makes output str (else bytes), and only piped/captured streams can be read --
# `r.stdout` without capture_output=True is a compile error rather than a None.

PROCESS_KEYWORDS = {
    "run": ("stdin", "stdout", "stderr", "input", "capture_output", "text", "universal_newlines", "encoding",
            "check", "shell", "cwd", "timeout", "env"),
    "check_output": ("stdin", "stderr", "input", "text", "universal_newlines", "encoding", "shell", "cwd",
                     "timeout", "env"),
    "call": ("stdin", "stdout", "stderr", "shell", "cwd", "timeout", "env"),
    "check_call": ("stdin", "stdout", "stderr", "shell", "cwd", "timeout", "env"),
    "Popen": ("stdin", "stdout", "stderr", "text", "universal_newlines", "encoding", "shell", "cwd", "env", "bufsize"),
}
STREAM_CONSTANTS = {"PIPE": -1, "STDOUT": -2, "DEVNULL": -3}


def stream_kind(ctx: CallContext, node: A.Expr | None, name: str) -> str:
    """'inherit', 'PIPE', 'STDOUT', 'DEVNULL' or 'file' for stdin=/stdout=/stderr=."""
    if node is None or isinstance(node, A.NoneLit):
        return "inherit"
    const = None
    if isinstance(node, A.Attribute) and isinstance(node.value, A.Name):
        if ctx.checker.modules.get(node.value.id) is MODULES["subprocess"]:
            const = node.attr
    elif isinstance(node, A.Name) and node.id in ctx.checker.imported:
        mod, member = ctx.checker.imported[node.id]
        if mod is MODULES["subprocess"]:
            const = member
    if const in STREAM_CONSTANTS:
        ctx.checker.check_expr(node)
        if const == "STDOUT" and name != "stderr":
            raise ctx.error("only stderr can be subprocess.STDOUT", node)
        return const
    t = ctx.checker.check_expr(node)
    if isinstance(t, FileType):
        return "file"
    raise ctx.error(
        f"{name}= must be subprocess.PIPE, subprocess.DEVNULL, an open file, or None (written out: "
        f"it decides what the result holds), not {t}", node,
    )


def literal_bool(ctx: CallContext, node: A.Expr | None, name: str) -> bool:
    if node is None:
        return False
    if not isinstance(node, A.BoolLit):
        raise ctx.error(f"{name}= must be True or False written out (it decides the result's type)", node)
    node.ty = BOOL
    return node.value


def process_call(op: str) -> Callable[[CallContext], Type]:
    def handler(ctx: CallContext) -> Type:
        if len(ctx.args) != 1:
            raise ctx.error(f"{ctx.what} takes the command as its only positional argument; the rest are keywords")
        kw = {}
        for k in ctx.call.keywords:
            if k.name not in PROCESS_KEYWORDS[op]:
                raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{k.name}'", k)
            kw[k.name] = k.value
        args_t = ctx.arg(0, ListType(STR))
        if args_t not in (STR, ListType(STR)):
            raise ctx.error(f"{ctx.what} command must be a list of strings (or one string), not {args_t}", ctx.args[0])
        text = literal_bool(ctx, kw.get("text"), "text") or literal_bool(ctx, kw.get("universal_newlines"), "universal_newlines")
        if "encoding" in kw:
            ctx.checker.expect_type(kw["encoding"], STR, "encoding")
            text = True  # decoded (as UTF-8)
        capture = literal_bool(ctx, kw.get("capture_output"), "capture_output")
        streams = {name: stream_kind(ctx, kw.get(name), name) for name in ("stdin", "stdout", "stderr")}
        if capture and (streams["stdout"] != "inherit" or streams["stderr"] != "inherit"):
            raise ctx.error("stdout and stderr can't be used with capture_output=True", ctx.call)
        if capture:
            streams["stdout"] = streams["stderr"] = "PIPE"
        if op == "check_output":
            streams["stdout"] = "PIPE"
        if "input" in kw:
            if streams["stdin"] != "inherit":
                raise ctx.error("stdin and input can't both be given", kw["input"])
            ctx.checker.expect_type(kw["input"], STR if text else BYTES, "input" + ("" if text else " (without text=True)"))
        for name, t in (("check", BOOL), ("shell", BOOL), ("timeout", FLOAT), ("env", DictType(STR, STR)),
                        ("bufsize", INT)):
            if name in kw:
                ctx.checker.expect_type(kw[name], t, name)
        if "cwd" in kw and not isinstance(kw["cwd"], A.NoneLit):
            ctx.checker.expect_type(kw["cwd"], STR, "cwd")
        ctx.call.process = {"op": op, "kw": kw, "text": text, "streams": streams}
        content = STR if text else BYTES
        match op:
            case "run":
                return ProcessType("CompletedProcess", text, False, streams["stdout"] == "PIPE",
                                   streams["stderr"] == "PIPE", args_t)
            case "check_output":
                return content
            case "call" | "check_call":
                return INT
            case "Popen":
                return ProcessType("Popen", text, streams["stdin"] == "PIPE", streams["stdout"] == "PIPE",
                                   streams["stderr"] == "PIPE", args_t)
        raise AssertionError(op)

    return handler


def captured(stream: str) -> Callable[[ProcessType], Type]:
    def attribute(t: ProcessType) -> Type:
        if t.args is None:
            raise AttributeUnavailable(f"what {t}.{stream} holds isn't known here (only where the process was started)")
        if not getattr(t, stream):
            how = "capture_output=True" if t.kind == "CompletedProcess" else f"{stream}=subprocess.PIPE"
            raise AttributeUnavailable(f"{stream} wasn't captured, so it's None: pass {how} to read it")
        if t.kind == "Popen":
            return FileType(binary=not t.text)
        return STR if t.text else BYTES

    return attribute


COMPLETED_ATTRIBUTES = {
    "args": lambda t: t.args or ListType(STR), "returncode": lambda t: INT,
    "stdout": captured("stdout"), "stderr": captured("stderr"),
}
POPEN_ATTRIBUTES = {
    "args": lambda t: t.args or ListType(STR), "returncode": lambda t: OptionalType(INT), "pid": lambda t: INT,
    "stdin": captured("stdin"), "stdout": captured("stdout"), "stderr": captured("stderr"),
}


def popen_communicate(ctx: CallContext) -> Type:
    t: ProcessType = ctx.receiver
    args = bind_args(ctx, (("input", None, True), ("timeout", FLOAT, True)))
    content = STR if t.text else BYTES
    if "input" in args and not isinstance(args["input"], A.NoneLit):
        if not t.stdin:
            raise ctx.error("communicate(input) needs Popen(..., stdin=subprocess.PIPE)", args["input"])
        ctx.checker.expect_type(args["input"], content, "input")
    if "timeout" in args:
        ctx.checker.expect_type(args["timeout"], FLOAT, "timeout")
    ctx.call.regex_args = args  # (reused: the bound arguments, for codegen)
    return TupleType(tuple(content if piped else OptionalType(content) for piped in (t.stdout, t.stderr)))


PROCESS_METHODS = {
    "CompletedProcess": {"check_returncode": returns(NONE)},
    "Popen": {
        "communicate": popen_communicate,
        "wait": signature(INT, ("timeout", FLOAT, "std::nullopt")),
        "poll": returns(OptionalType(INT)),
        "kill": returns(NONE),
        "terminate": returns(NONE),
        "send_signal": returns(NONE, args=(INT,)),
    },
}


def subprocess_exception(name: str, fields: dict[str, Type]) -> StructType:
    base = MODULES_EXCEPTION_BASE.get(name)
    st = StructType(name, "class", None, base=base, builtin=True, cpp_name=f"sd::subprocess::{name}")
    for fname, ft in fields.items():
        st.fields[fname] = Field(fname, ft, None, Loc(0, 0))
    return st


MODULES_EXCEPTION_BASE: dict[str, StructType] = {"SubprocessError": EXCEPTIONS["Exception"]}
SUBPROCESS_ERROR = subprocess_exception("SubprocessError", {})
MODULES_EXCEPTION_BASE.update(CalledProcessError=SUBPROCESS_ERROR, TimeoutExpired=SUBPROCESS_ERROR)
STREAM_TEXT = OptionalType(STR)  # captured output, decoded (Python gives bytes unless text=True)

MODULES["subprocess"] = Module("subprocess", {
    **{op: Function(op, process_call(op)) for op in PROCESS_KEYWORDS},
    "getoutput": Function("getoutput", signature(STR, ("cmd", STR)), "sd::subprocess::getoutput"),
    "getstatusoutput": Function("getstatusoutput", signature(TupleType((INT, STR)), ("cmd", STR)),
                                "sd::subprocess::getstatusoutput"),
    **{name: Value(name, INT, f"std::int64_t{{{v}}}") for name, v in STREAM_CONSTANTS.items()},
    "SubprocessError": SUBPROCESS_ERROR,
    "CalledProcessError": subprocess_exception("CalledProcessError", {
        "returncode": INT, "cmd": ListType(STR), "output": STREAM_TEXT, "stdout": STREAM_TEXT, "stderr": STREAM_TEXT,
    }),
    "TimeoutExpired": subprocess_exception("TimeoutExpired", {
        "cmd": ListType(STR), "timeout": FLOAT, "output": STREAM_TEXT, "stdout": STREAM_TEXT, "stderr": STREAM_TEXT,
    }),
    "CompletedProcess": NamedType("CompletedProcess", ProcessType("CompletedProcess")),
    "Popen": Function("Popen", process_call("Popen"), as_type=ProcessType("Popen")),
}, "modules/subprocess.hpp")
for _name in ("getoutput", "getstatusoutput"):
    MODULES["subprocess"].members[_name].params = MODULES["subprocess"].members[_name].check.params


MODULES["re"] = Module("re", {
    **{op: Function(op, re_function(op)) for op in (*REGEX_OPS, "compile", "escape", "purge")},
    **{name: Value(name, INT, f"std::int64_t{{{int(value)}}}") for name, value in __import__("re").RegexFlag.__members__.items()
       if name in ("ASCII", "A", "IGNORECASE", "I", "MULTILINE", "M", "DOTALL", "S", "VERBOSE", "X", "UNICODE", "U", "NOFLAG")},
    "error": exception_class("error", "sd::re::error"),
    "Pattern": NamedType("Pattern", PatternType()),
    "Match": NamedType("Match", MatchType()),
}, "modules/re.hpp", ("pcre2-8",))

MODULES["queue"] = Module("queue", {
    "Queue": SyncTypeDef("Queue"),
    "Empty": exception_class("Empty", "sd::queue::Empty"),
    "Full": exception_class("Full", "sd::queue::Full"),
}, "modules/queue.hpp")


# ---- socket -------------------------------------------------------------------------

BYTES_OR_STR = object()  # a parameter taking bytes, or str (sent as UTF-8)
ADDRESS = TupleType((STR, INT))

SOCKET.methods.update({
    "connect": sync_method(NONE, ("address", ADDRESS)),
    "bind": sync_method(NONE, ("address", ADDRESS)),
    "listen": sync_method(NONE, ("backlog", INT, "128_i")),
    "accept": sync_method(TupleType((SOCKET, ADDRESS))),
    "send": sync_method(INT, ("data", BYTES_OR_STR)),
    "sendall": sync_method(NONE, ("data", BYTES_OR_STR)),
    "recv": sync_method(BYTES, ("bufsize", INT)),
    "sendto": sync_method(INT, ("data", BYTES_OR_STR), ("address", ADDRESS)),
    "recvfrom": sync_method(TupleType((BYTES, ADDRESS)), ("bufsize", INT)),
    "settimeout": sync_method(NONE, ("value", OPT_FLOAT)),
    "gettimeout": sync_method(OPT_FLOAT),
    "setsockopt": sync_method(NONE, ("level", INT), ("optname", INT), ("value", INT)),
    "getsockname": sync_method(ADDRESS),
    "getpeername": sync_method(ADDRESS),
    "shutdown": sync_method(NONE, ("how", INT)),
    "close": sync_method(NONE),
    "fileno": sync_method(INT),
})

SOCKET_CONSTANTS = [
    "AF_INET", "AF_INET6", "AF_UNSPEC", "SOCK_STREAM", "SOCK_DGRAM", "SOL_SOCKET", "SO_REUSEADDR",
    "SO_REUSEPORT", "SO_KEEPALIVE", "SO_BROADCAST", "IPPROTO_TCP", "IPPROTO_UDP", "TCP_NODELAY",
    "SHUT_RD", "SHUT_WR", "SHUT_RDWR",
]

MODULES["socket"] = module_with_params(runtime_module(
    "socket", "modules/socket.hpp",
    socket=(signature(SOCKET, ("family", INT, "static_cast<std::int64_t>(AF_INET)"),
                      ("type", INT, "static_cast<std::int64_t>(SOCK_STREAM)"), ("proto", INT, "0_i"),
                      ("fileno", OptionalType(INT), "std::nullopt")), "sd::socket::Socket"),
    create_connection=(signature(SOCKET, ("address", ADDRESS), ("timeout", OPT_FLOAT, "std::nullopt")),
                       "sd::socket::create_connection"),
    create_server=(signature(SOCKET, ("address", ADDRESS), ("family", INT, "static_cast<std::int64_t>(AF_INET)"),
                             ("backlog", OptionalType(INT), "std::nullopt"), ("reuse_port", BOOL, "false")),
                   "sd::socket::create_server"),
    gethostname=(signature(STR), "sd::socket::gethostname"),
    gethostbyname=(signature(STR, ("hostname", STR)), "sd::socket::gethostbyname"),
    gaierror=exception_class("gaierror", "sd::socket::gaierror", "OSError"),
    **{c: (INT, f"static_cast<std::int64_t>({c})") for c in SOCKET_CONSTANTS},
))
MODULES["socket"].members["socket"].as_type = SOCKET
MODULES["socket"].members["timeout"] = EXCEPTIONS["TimeoutError"]  # socket.timeout is TimeoutError
MODULES["socket"].members["error"] = EXCEPTIONS["OSError"]  # socket.error is OSError


# ---- decorator modules ----------------------------------------------------------------


@dataclass
class DecoratorName:
    """dataclasses.dataclass, dataclasses.field, functools.cache, functools.lru_cache:
    understood by the checker rather than called."""

    name: str


def dataclasses_replace(ctx: CallContext) -> Type:
    """dataclasses.replace(obj, field=value, ...): a copy of obj with those fields changed
    (the way to change a frozen dataclass)."""
    if len(ctx.args) != 1:
        raise ctx.error(f"replace() takes the object, then fields as keywords: replace(obj, x=1) ({len(ctx.args)} given)")
    t = ctx.arg(0)
    if not isinstance(t, StructType) or t.builtin or t.is_exception:
        raise ctx.error(f"replace() needs a dataclass or @value class instance, not {t}", ctx.args[0])
    if any(a.builtin and a.name == "Synchronized" for a in t.ancestors()):
        raise ctx.error(f"replace() can't copy {t.name}: a Synchronized object's lock can't be copied", ctx.args[0])
    if t.kind == "class" and any("__init__" in a.methods for a in t.ancestors() if not a.builtin):
        raise ctx.error(f"replace() rebuilds an object from its fields, but {t.name} has its own __init__", ctx.args[0])
    fields = t.all_fields()
    for kw in ctx.call.keywords:
        f = fields.get(kw.name)
        if f is None:
            raise ctx.error(f"{t.name} has no field '{kw.name}'", kw)
        vt = ctx.checker.check_expr(kw.value, f.type)
        if not assignable(vt, f.type):
            raise ctx.error(f"field '{kw.name}' is {f.type}, not {vt}", kw.value)
    return t


MODULES["dataclasses"] = Module("dataclasses", {
    "dataclass": DecoratorName("dataclass"),
    "field": DecoratorName("field"),
    "replace": Function("replace", dataclasses_replace),
})
# seadash's own: `@value class Point:` makes a value type (copied on assignment, like an int).
MODULES["seadash"] = Module("seadash", {
    "value": DecoratorName("value"),
    **{kind: SyncTypeDef(kind) for kind in ("Atomic", "Mutex", "RWMutex")},  # thread-safe sharing
    "Synchronized": SYNCHRONIZED,
})  # (no header of its own: its thread types need threading's, but @value needs nothing)
MODULES["functools"] = Module("functools", {
    "cache": DecoratorName("cache"),
    "lru_cache": DecoratorName("lru_cache"),
})
