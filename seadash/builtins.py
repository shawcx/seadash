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
    CSV_WRITER, CSV_DICT_READER, CSV_DICT_WRITER, HTTP_RESPONSE, HTTP_HEADERS, URL_REQUEST, URL_PARTS,
    DictType, Field, FileType, SyncType, FuncType, user_dunder, IterType, ListType, ModuleType, OptionalType, SetType, StructType, TupleType, Type,
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
    )


def ordered(t: Type) -> bool:
    return t in (INT, FLOAT, STR, BYTES, PATH, DATE, TIME, DATETIME, TIMEDELTA, UUID_T) or isinstance(t, (TupleType, ListType, VarTupleType)) or bool(user_dunder(t, "__lt__"))


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
    n = ctx.arity(1, 2, keywords=("mode", "encoding", "newline"))
    ctx.need(0, lambda t: t in (STR, PATH), "a str or Path")
    if (nl := ctx.keyword_arg("newline")) is not None:  # (seadash never translates newlines: newline="" is the norm)
        ctx.checker.check_expr(nl)
    return open_mode(ctx, ctx.args[1] if n == 2 else ctx.keyword_arg("mode"))


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
    if ctx.arity(0, 1):
        ctx.need(0, printable, "something printable")
    return STR


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
    ctx.arity(2)
    elem = ctx.iterable(1)  # the iterable first: it decides the function's parameter type
    result = ctx.function(ctx.args[0], (elem,), "function")
    if result == NONE:
        raise ctx.error(f"{ctx.what} function must return a value", ctx.args[0])
    return GeneratorType(result)


def b_filter(ctx: CallContext) -> Type:
    ctx.arity(2)
    elem = ctx.iterable(1)
    result = ctx.function(ctx.args[0], (elem,), "function")
    ctx.checker.check_truthy(result, ctx.args[0])
    return GeneratorType(elem)


def b_reversed(ctx: CallContext) -> Type:
    ctx.arity(1)
    t = ctx.need(0, lambda t: isinstance(t, (ListType, TupleType)) or t == STR, "a list, tuple or str")
    if isinstance(t, TupleType):
        return IterType(ctx.iterable(0), "reversed")
    return IterType(element_type(t), "reversed")


def b_enumerate(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2)
    elem = ctx.iterable(0)
    if n == 2:
        ctx.expect(1, INT)
    return GeneratorType(TupleType((INT, elem)))


def b_zip(ctx: CallContext) -> Type:
    n = ctx.arity(2, MANY)
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


def b_dict(ctx: CallContext) -> Type:
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
    if not ctx.arity(0, 1):
        return BYTES
    t = ctx.arg(0)
    if t == STR:
        raise ctx.error("bytes(str) needs an encoding; use s.encode() instead", ctx.args[0])
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
        return FLOAT
    return INT


FUNCTIONS: dict[str, Callable[[CallContext], Type]] = {
    "print": b_print,
    "len": b_len,
    "str": b_str,
    "repr": b_repr,
    "ascii": b_repr,
    "format": b_format,
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


STR_METHODS = {
    **{name: returns(STR, 0, 1, (STR,)) for name in ("strip", "lstrip", "rstrip")},
    **{name: returns(STR) for name in ("upper", "lower", "title", "capitalize")},
    **{name: returns(BOOL) for name in ("isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower")},
    **{name: returns(BOOL, args=(STR,)) for name in ("startswith", "endswith")},
    **{name: returns(INT, args=(STR,)) for name in ("find", "count")},
    "split": returns(ListType(STR), 0, 1, (STR,)),
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
    **{name: returns(INT, args=(BYTES,)) for name in ("find", "count")},
    **{name: returns(BYTES) for name in ("upper", "lower", "title", "capitalize")},
    **{name: returns(BYTES, 0, 1, (BYTES,)) for name in ("strip", "lstrip", "rstrip")},
    **{name: returns(BOOL) for name in ("isdigit", "isalpha", "isalnum", "isspace", "isupper", "islower")},
    "split": returns(ListType(BYTES), 0, 1, (BYTES,)),
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
    if isinstance(t, SyncType):
        return SYNC_METHODS[t.kind].get(name)
    if t == SOCKET:
        return SOCKET_METHODS.get(name)
    match t:
        case _ if t == JSON_VALUE:
            return JSON_VALUE_METHODS.get(name)
        case FileType():
            return FILE_METHODS.get(name)
        case ListType():
            table = LIST_METHODS
        case DequeType():
            table = DEQUE_METHODS
        case VarTupleType():
            table = {"count": returns(INT, args=(elem_of,)), "index": returns(INT, args=(elem_of,))}
        case _ if t in DATETIME_METHODS:
            return DATETIME_METHODS[t].get(name)
        case ParserType():
            return PARSER_METHODS.get(name)
        case SubParsersType():
            return {"add_parser": subparsers_add_parser}.get(name)
        case _ if t == PATH:
            return PATH_METHODS.get(name)
        case _ if t == HASH:
            return HASH_METHODS.get(name)
        case _ if t == EXECUTOR:
            return EXECUTOR_METHODS.get(name)
        case _ if t == LOGGER:
            return LOGGER_METHODS.get(name)
        case _ if t == HTTP_RESPONSE:
            return RESPONSE_METHODS.get(name)
        case _ if t == HTTP_HEADERS:
            return HEADERS_METHODS.get(name)
        case _ if t == URL_REQUEST:
            return REQUEST_METHODS.get(name)
        case _ if t == URL_PARTS:
            return {"geturl": sync_method(STR)}.get(name)
        case StructType() if (methods := EXCEPTION_METHODS.get(t.cpp_name)) is not None:
            return methods.get(name)
        case _ if t == CSV_WRITER:
            return {"writerow": csv_writerow, "writerows": csv_writerows}.get(name)
        case _ if t == CSV_DICT_WRITER:
            return {"writerow": csv_dict_writerow, "writerows": csv_dict_writerows,
                    "writeheader": sync_method(INT)}.get(name)
        case _ if t == LOG_HANDLER:
            return HANDLER_METHODS.get(name)
        case FutureType():
            return FUTURE_METHODS.get(name)
        case _ if t == HMAC_T:
            return HMAC_METHODS.get(name)
        case _ if t == STR_TEMPLATE:
            return {"substitute": template_substitute, "safe_substitute": template_substitute,
                    "get_identifiers": sync_method(ListType(STR)), "is_valid": sync_method(BOOL)}.get(name)
        case _ if t == TEXT_WRAPPER:
            return {"wrap": sync_method(ListType(STR), ("text", STR)), "fill": sync_method(STR, ("text", STR))}.get(name)
        case _ if t == TEMPDIR:
            return {"cleanup": sync_method(NONE)}.get(name)
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


def cpp_string_literal(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"s'


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
    rename=(signature(NONE, ("src", STR), ("dst", STR)), "sd::os::rename"),
    getenv=(os_getenv, "sd::os::getenv"),
    sep=(STR, "sd::os::path::sep()"),
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
    **{kind: SyncTypeDef(kind) for kind in ("Thread", "Lock", "RLock", "Event", "Atomic", "Mutex", "RWMutex")},
    "Synchronized": SYNCHRONIZED,
}, "modules/threading.hpp", ("pthread",))

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
    if t == HTTP_RESPONSE:
        return {"status": lambda t: INT, "code": lambda t: INT, "reason": lambda t: STR, "url": lambda t: STR,
                "headers": lambda t: HTTP_HEADERS}
    if t == URL_REQUEST:
        return {"full_url": lambda t: STR, "data": lambda t: OptionalType(BYTES), "method": lambda t: OptionalType(STR),
                "headers": lambda t: DictType(STR, STR)}
    if t == URL_PARTS:
        return {**{f: (lambda t: STR) for f in ("scheme", "netloc", "path", "params", "query", "fragment")},
                "hostname": lambda t: OptionalType(STR), "port": lambda t: OptionalType(INT),
                "username": lambda t: OptionalType(STR), "password": lambda t: OptionalType(STR)}
    if t == CSV_DICT_READER:
        return {"fieldnames": lambda t: ListType(STR)}
    if t == LOGGER:
        return {"name": lambda t: STR, "level": lambda t: INT, "propagate": lambda t: BOOL,
                "handlers": lambda t: ListType(LOG_HANDLER), "parent": lambda t: OptionalType(LOGGER)}
    if t in (HASH, HMAC_T):
        return {"name": lambda t: STR, "digest_size": lambda t: INT, "block_size": lambda t: INT}
    if t == STR_TEMPLATE:
        return {"template": lambda t: STR}
    if t == UUID_T:
        return {"hex": lambda t: STR, "bytes": lambda t: BYTES, "version": lambda t: OptionalType(INT),
                "variant": lambda t: STR, "urn": lambda t: STR, "fields": lambda t: TupleType((INT,) * 6),
                **{f: (lambda t: INT) for f in ("time_low", "time_mid", "time_hi_version", "clock_seq_hi_variant",
                                                "clock_seq_low", "node", "clock_seq", "time")}}
    if isinstance(t, NamespaceType):
        return {name: (lambda _, ft=ft: ft) for name, ft in t.fields}
    if t in DATETIME_ATTRIBUTES:
        return DATETIME_ATTRIBUTES[t]
    if t == PATH:
        return PATH_ATTRIBUTES
    if t == TEMPDIR:
        return {"name": lambda t: STR}
    if isinstance(t, PatternType):
        return PATTERN_ATTRIBUTES
    if isinstance(t, MatchType):
        return MATCH_ATTRIBUTES
    if isinstance(t, ProcessType):
        return COMPLETED_ATTRIBUTES if t.kind == "CompletedProcess" else POPEN_ATTRIBUTES
    return None


# ---- pathlib ------------------------------------------------------------------------

PATH_LIKE = object()  # a parameter taking a str or a Path (sync_method checks it)

PATH_ATTRIBUTES = {
    "name": lambda t: STR, "stem": lambda t: STR, "suffix": lambda t: STR, "anchor": lambda t: STR,
    "suffixes": lambda t: ListType(STR), "parts": lambda t: VarTupleType(STR),
    "parent": lambda t: PATH, "parents": lambda t: ListType(PATH),
}


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

PATH_METHODS = {
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
}

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


# ---- datetime ---------------------------------------------------------------------------

def attrs(result: Type, *names: str) -> dict:
    return {name: (lambda t, r=result: r) for name in names}


OPT_INT, OPT_TZ = OptionalType(INT), OptionalType(TIMEZONE)
DATE_FIELDS = (("year", OPT_INT, "std::nullopt"), ("month", OPT_INT, "std::nullopt"), ("day", OPT_INT, "std::nullopt"))
TIME_FIELDS = (("hour", OPT_INT, "std::nullopt"), ("minute", OPT_INT, "std::nullopt"),
               ("second", OPT_INT, "std::nullopt"), ("microsecond", OPT_INT, "std::nullopt"))

DATETIME_ATTRIBUTES = {
    DATE: attrs(INT, "year", "month", "day"),
    TIME: {**attrs(INT, "hour", "minute", "second", "microsecond"), "tzinfo": lambda t: OPT_TZ},
    DATETIME: {**attrs(INT, "year", "month", "day", "hour", "minute", "second", "microsecond"),
               "tzinfo": lambda t: OPT_TZ},
    TIMEDELTA: attrs(INT, "days", "seconds", "microseconds"),
    TIMEZONE: {},
}
DATE_METHODS = {
    "isoformat": sync_method(STR),
    "strftime": sync_method(STR, ("format", STR)),
    "ctime": sync_method(STR),
    "weekday": sync_method(INT),
    "isoweekday": sync_method(INT),
    "toordinal": sync_method(INT),
}
DATETIME_METHODS = {
    DATE: {**DATE_METHODS, "replace": sync_method(DATE, *DATE_FIELDS)},
    TIME: {
        "isoformat": sync_method(STR, ("timespec", STR, '"auto"s')),
        "strftime": sync_method(STR, ("format", STR)),
        "replace": sync_method(TIME, *TIME_FIELDS),
    },
    DATETIME: {
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
    },
    TIMEDELTA: {"total_seconds": sync_method(FLOAT)},
    TIMEZONE: {"tzname": sync_method(STR, ("dt", OptionalType(DATETIME), "std::nullopt"))},
}

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

def gzip_open(ctx: CallContext) -> Type:
    """gzip.open(filename, mode="rb", compresslevel=9, encoding=None, errors=None, newline=None):
    binary unless the mode says 't', like Python's gzip (and unlike open())."""
    n = ctx.arity(1, 3, keywords=("mode", "compresslevel", "encoding", "newline"))
    ctx.need(0, lambda t: t in (STR, PATH), "a str or Path")
    mode_node = ctx.args[1] if n >= 2 else ctx.keyword_arg("mode")
    if n == 3:
        ctx.expect(2, INT)
    else:
        ctx.keyword("compresslevel", INT)
    if (nl := ctx.keyword_arg("newline")) is not None:
        ctx.checker.check_expr(nl)
    if mode_node is None:
        return BINARY_FILE
    open_mode(ctx, mode_node)
    return TEXT_FILE if "t" in mode_node.value else BINARY_FILE


MODULES["gzip"] = module_with_params(runtime_module(
    "gzip", "modules/gzip.hpp", ("z",),
    compress=(signature(BYTES, ("data", BYTES), ("compresslevel", INT, "9_i"), ("mtime", OptionalType(INT), "std::nullopt")),
              "sd::gzip::compress"),
    decompress=(signature(BYTES, ("data", BYTES)), "sd::gzip::decompress"),
    open=(gzip_open, None),
    BadGzipFile=exception_class("BadGzipFile", "sd::gzip::BadGzipFile", "OSError"),
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


# ---- shlex ----------------------------------------------------------------------------

MODULES["shlex"] = module_with_params(runtime_module(
    "shlex", "modules/shlex.hpp",
    split=(signature(ListType(STR), ("s", STR), ("comments", BOOL, "false"), ("posix", BOOL, "true")), "sd::shlex::split"),
    quote=(signature(STR, ("s", STR)), "sd::shlex::quote"),
    join=(signature(STR, ("split_command", ListType(STR))), "sd::shlex::join"),
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


HASH_METHODS = {
    "update": hash_update,
    "digest": sync_method(BYTES, ("length", OptionalType(INT), "std::nullopt")),
    "hexdigest": sync_method(STR, ("length", OptionalType(INT), "std::nullopt")),
    "copy": sync_method(HASH),
}
HMAC_METHODS = {
    "update": hash_update,
    "digest": sync_method(BYTES),
    "hexdigest": sync_method(STR),
    "copy": sync_method(HMAC_T),
}

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


EXECUTOR_METHODS = {
    "submit": executor_submit,
    "map": executor_map,
    "shutdown": sync_method(NONE, ("wait", BOOL, "true"), ("cancel_futures", BOOL, "false")),
}
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


LOGGER_METHODS = {
    **{name: log_call(name) for name in (*LOG_LEVELS, "log")},
    "setLevel": log_set_level,
    "addHandler": sync_method(NONE, ("hdlr", LOG_HANDLER)),
    "removeHandler": sync_method(NONE, ("hdlr", LOG_HANDLER)),
    "hasHandlers": sync_method(BOOL),
    "getEffectiveLevel": sync_method(INT),
    "isEnabledFor": sync_method(BOOL, ("level", INT)),
    "getChild": sync_method(LOGGER, ("suffix", STR)),
}
HANDLER_METHODS = {
    "setLevel": log_set_level,
    "setFormatter": sync_method(NONE, ("fmt", LOG_FORMATTER)),
    "flush": sync_method(NONE),
    "close": sync_method(NONE),
}
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


# ---- urllib -------------------------------------------------------------------------------

OPT_STR = OptionalType(STR)
RESPONSE_METHODS = {
    "read": sync_method(BYTES, ("amt", OptionalType(INT), "std::nullopt")),
    "readline": sync_method(BYTES),
    "readlines": sync_method(ListType(BYTES)),
    "getheader": sync_method(OPT_STR, ("name", STR), ("default", OPT_STR, "std::nullopt")),
    "getheaders": sync_method(ListType(TupleType((STR, STR)))),
    "geturl": sync_method(STR),
    "getcode": sync_method(INT),
    "info": sync_method(HTTP_HEADERS),
    "close": sync_method(NONE),
}
HEADERS_METHODS = {
    "get": sync_method(OPT_STR, ("name", STR), ("failobj", OPT_STR, "std::nullopt")),
    "get_all": sync_method(OptionalType(ListType(STR)), ("name", STR)),
    "items": sync_method(ListType(TupleType((STR, STR)))),
    "keys": sync_method(ListType(STR)),
    "values": sync_method(ListType(STR)),
    "get_content_type": sync_method(STR),
    "get_content_charset": sync_method(OPT_STR),
}
REQUEST_METHODS = {
    "add_header": sync_method(NONE, ("key", STR), ("val", STR)),
    "has_header": sync_method(BOOL, ("header_name", STR)),
    "get_header": sync_method(OPT_STR, ("header_name", STR), ("default", OPT_STR, "std::nullopt")),
    "get_method": sync_method(STR),
    "get_full_url": sync_method(STR),
    "header_items": sync_method(ListType(TupleType((STR, STR)))),
}
EXCEPTION_METHODS = {  # methods of built-in exception classes: HTTPError is also a response
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
    args = bind_args(ctx, (("url", None), ("data", None, True), ("timeout", None, True)))
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

SOCKET_METHODS = {
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
}

SOCKET_CONSTANTS = [
    "AF_INET", "AF_INET6", "AF_UNSPEC", "SOCK_STREAM", "SOCK_DGRAM", "SOL_SOCKET", "SO_REUSEADDR",
    "SO_REUSEPORT", "SO_KEEPALIVE", "SO_BROADCAST", "IPPROTO_TCP", "IPPROTO_UDP", "TCP_NODELAY",
    "SHUT_RD", "SHUT_WR", "SHUT_RDWR",
]

MODULES["socket"] = module_with_params(runtime_module(
    "socket", "modules/socket.hpp",
    socket=(signature(SOCKET, ("family", INT, "static_cast<std::int64_t>(AF_INET)"),
                      ("type", INT, "static_cast<std::int64_t>(SOCK_STREAM)")), "sd::socket::Socket"),
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
})
MODULES["functools"] = Module("functools", {
    "cache": DecoratorName("cache"),
    "lru_cache": DecoratorName("lru_cache"),
})
