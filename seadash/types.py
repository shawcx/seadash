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
BYTES = Prim("bytes")
NONE = Prim("None")
JSON_VALUE = Prim("json.Value")  # a dynamically typed JSON value (the json module)
UNKNOWN = Prim("?")  # only while inferring literals: the element type of an empty []
SOCKET = Prim("socket")  # socket.socket: a thread-safe handle
PATH = Prim("Path")  # pathlib.Path: an immutable path value
TEXT_WRAPPER = Prim("TextWrapper")  # textwrap.TextWrapper
STR_TEMPLATE = Prim("Template")  # string.Template
UUID_T = Prim("UUID")  # uuid.UUID: an immutable 16-byte value

# Incremental (de)compressors: handles (copies share the state) whose methods and attributes
# are in builtins.CODEC_METHODS and CODEC_ATTRIBUTES. Maps each type to its C++ class.
CODEC_TYPES: dict[Prim, str] = {}


def codec_type(name: str, cpp: str) -> Prim:
    t = Prim(name)
    CODEC_TYPES[t] = cpp
    return t


ZLIB_COMPRESS = codec_type("zlib.Compress", "sd::zlib::Compress")
ZLIB_DECOMPRESS = codec_type("zlib.Decompress", "sd::zlib::Decompress")
BZ2_COMPRESSOR = codec_type("bz2.BZ2Compressor", "sd::bz2::BZ2Compressor")
BZ2_DECOMPRESSOR = codec_type("bz2.BZ2Decompressor", "sd::bz2::BZ2Decompressor")
LZMA_COMPRESSOR = codec_type("lzma.LZMACompressor", "sd::lzma::LZMACompressor")
LZMA_DECOMPRESSOR = codec_type("lzma.LZMADecompressor", "sd::lzma::LZMADecompressor")
HASH = Prim("hash")  # a hashlib hash object
EXECUTOR = Prim("ThreadPoolExecutor")  # concurrent.futures.ThreadPoolExecutor
LOGGER = Prim("Logger")  # logging.Logger
LOG_HANDLER = Prim("Handler")  # logging.StreamHandler / FileHandler / NullHandler
LOG_FORMATTER = Prim("Formatter")  # logging.Formatter
CSV_WRITER = Prim("csv.writer")
CSV_DICT_READER = Prim("csv.DictReader")
CSV_DICT_WRITER = Prim("csv.DictWriter")
HTTP_RESPONSE = Prim("http.client.HTTPResponse")
HTTP_HEADERS = Prim("http.client.HTTPMessage")
URL_REQUEST = Prim("urllib.request.Request")
URL_PARTS = Prim("urllib.parse.ParseResult")
HMAC_T = Prim("HMAC")  # hmac.HMAC
TEMPDIR = Prim("TemporaryDirectory")  # tempfile.TemporaryDirectory: removed when done

DATE = Prim("date")  # the datetime module's value types
TIME = Prim("time")
DATETIME = Prim("datetime")
TIMEDELTA = Prim("timedelta")
TIMEZONE = Prim("timezone")
DATETIME_TYPES = (DATE, TIME, DATETIME, TIMEDELTA, TIMEZONE)

PRIMITIVES = {"int": INT, "float": FLOAT, "bool": BOOL, "str": STR, "bytes": BYTES, "None": NONE}


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
class DefaultDictType(DictType):
    """collections.defaultdict: a dict whose d[k] adds factory() for a missing key."""

    def __str__(self) -> str:
        return f"defaultdict[{self.key}, {self.value}]"


@dataclass(frozen=True)
class CounterType(DictType):
    """collections.Counter: a dict of counts (value is always int); c[k] is 0 if missing."""

    def __str__(self) -> str:
        return f"Counter[{self.key}]"


@dataclass(frozen=True)
class DequeType(Type):
    """collections.deque: a sequence with fast appends and pops at both ends."""

    elem: Type

    def __str__(self) -> str:
        return f"deque[{self.elem}]"


@dataclass(frozen=True)
class RegexInfo:
    """What the compiler knows about a pattern written as a literal (see regex.py)."""

    groups: int
    names: tuple[tuple[str, int], ...]  # (name, group number)
    optional: frozenset[int]  # groups that may not take part in a match

    def group_number(self, name: str) -> int | None:
        return next((i for n, i in self.names if n == name), None)


@dataclass(frozen=True)
class PatternType(Type):
    """re.Pattern. `info` is known when the pattern was a literal; it only refines types
    (all patterns are the same C++ type)."""

    info: RegexInfo | None = None

    def __str__(self) -> str:
        return "re.Pattern"


@dataclass(frozen=True)
class MatchType(Type):
    """re.Match, from a pattern with (possibly) known groups."""

    info: RegexInfo | None = None

    def __str__(self) -> str:
        return "re.Match"


@dataclass(frozen=True)
class ProcessType(Type):
    """subprocess.CompletedProcess or subprocess.Popen. The checker knows (from the call's
    literal arguments) whether output is text and which streams are pipes, so
    `r.stdout` is exactly str or bytes -- and a compile error if it wasn't captured."""

    kind: str  # "CompletedProcess" or "Popen"
    text: bool = False
    stdin: bool = False  # a pipe we can write to (Popen)
    stdout: bool = False  # captured / a pipe
    stderr: bool = False
    args: Type | None = None  # list[str] or str, as given

    def __str__(self) -> str:
        return f"subprocess.{self.kind}"


@dataclass(frozen=True)
class FutureType(Type):
    """concurrent.futures.Future[T]: a result being computed on another thread."""

    elem: Type

    def __str__(self) -> str:
        return f"Future[{self.elem}]"


@dataclass(frozen=True)
class GeneratorType(Type):
    """What a generator function returns (and iter(xs)): values produced on demand.
    Copies share their position, like Python's iterator objects."""

    elem: Type

    def __str__(self) -> str:
        return f"Iterator[{self.elem}]"


@dataclass(frozen=True)
class VarTupleType(Type):
    """tuple[T, ...]: a tuple of any length (tuple(xs), Path.parts)."""

    elem: Type

    def __str__(self) -> str:
        return f"tuple[{self.elem}, ...]"


@dataclass(frozen=True)
class ParserType(Type):
    """argparse.ArgumentParser. Each parser created in the code has its own key, which
    the checker files its add_argument() calls under (None: a parser from elsewhere)."""

    key: int | None = None

    def __str__(self) -> str:
        return "argparse.ArgumentParser"


@dataclass(frozen=True)
class SubParsersType(Type):
    """What add_subparsers() returns: add_parser() makes the subcommands."""

    parent: int

    def __str__(self) -> str:
        return "argparse._SubParsersAction"


PARSER = ParserType()


@dataclass(frozen=True)
class NamespaceType(Type):
    """argparse.Namespace from parse_args(): one typed attribute per argument added."""

    fields: tuple[tuple[str, Type], ...] = ()
    # Subcommands: (dest, (name and aliases), their arguments' types), for narrowing by
    # `if args.command == "add":` (outside such a check they're optional).
    commands: tuple[tuple[str, tuple[str, ...], tuple[tuple[str, Type], ...]], ...] = ()

    def __str__(self) -> str:
        return "argparse.Namespace"


@dataclass(frozen=True)
class TupleType(Type):
    elts: tuple[Type, ...]

    def __str__(self) -> str:
        return f"tuple[{', '.join(map(str, self.elts))}]"


@dataclass(frozen=True)
class FuncType(Type):
    """`(int, str) -> bool`. In *expected-type hints only*, ret may be None,
    meaning "infer it" (e.g. the key function passed to sorted())."""

    params: tuple[Type, ...]
    ret: Type | None

    def __str__(self) -> str:
        return f"({', '.join(map(str, self.params))}) -> {self.ret if self.ret is not None else '?'}"


@dataclass(frozen=True)
class OptionalType(Type):
    inner: Type

    def __str__(self) -> str:
        inner = f"({self.inner})" if isinstance(self.inner, FuncType) else str(self.inner)
        return f"{inner}?"


@dataclass(frozen=True)
class IterType(Type):
    """Something you can only loop over: range(), enumerate(), dict.items(), zip()..."""

    elem: Type
    kind: str  # 'range', 'enumerate', 'items', 'keys', 'values', 'zip', 'reversed'

    def __str__(self) -> str:
        return f"{self.kind}[{self.elem}]"


@dataclass(frozen=True)
class FileType(Type):
    """What open() returns: TextIO (reads/writes str) or BinaryIO (bytes)."""

    binary: bool

    def __str__(self) -> str:
        return "BinaryIO" if self.binary else "TextIO"


TEXT_FILE = FileType(False)
BINARY_FILE = FileType(True)


@dataclass(frozen=True)
class SyncType(Type):
    """A thread-safe handle from `threading`/`queue`: Lock, RLock, Event, Thread, Atomic,
    Mutex[T] or Queue[T]. Copies share the same underlying object."""

    kind: str
    args: tuple = ()

    def __str__(self) -> str:
        return f"{self.kind}[{', '.join(map(str, self.args))}]" if self.args else self.kind


SYNC_CPP = {
    "Lock": "sd::threading::Lock", "RLock": "sd::threading::RLock", "Event": "sd::threading::Event",
    "Thread": "sd::threading::Thread", "Atomic": "sd::threading::Atomic",
    "Mutex": "sd::threading::Mutex", "Queue": "sd::queue::Queue",
    "RWMutex": "sd::threading::RWMutex",
    # what `with m.read() as data:` / `with m.write() as data:` hold (only ever a with item)
    "RWRead": "sd::threading::ReadGuard", "RWWrite": "sd::threading::WriteGuard",
}
SYNC_ARITY = {"Lock": 0, "RLock": 0, "Event": 0, "Thread": 0, "Atomic": 0, "Mutex": 1, "Queue": 1, "RWMutex": 1}


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
    var: Var | None = None  # for a nested def: the local variable holding it
    module: str = "__main__"
    cpp_name: str | None = None  # generic instances: first[int] is first__int in C++
    # 'method', 'static' (@staticmethod), 'classmethod', 'getter' (@property), 'setter' (@x.setter)
    kind: str = "method"
    cached: bool = False  # @functools.cache
    generator: bool = False  # has `yield`: returns an Iterator[T] that runs the body on demand

    def __str__(self) -> str:
        params = ", ".join(f"{p.name}: {p.type}" for p in self.params)
        return f"def {self.name}({params}) -> {self.ret}"


@dataclass(eq=False)
class StructType(Type):
    """A user `struct` (value semantics) or `class` (shared reference semantics).

    Built-in exception classes are StructTypes too (builtin=True). Only
    exception classes may have a base class (for now).
    """

    name: str
    kind: str  # 'struct' or 'class'
    node: object  # ast.ClassDef (None for built-ins)
    fields: dict[str, Field] = field(default_factory=dict)  # declared here, not inherited
    methods: dict[str, FuncInfo] = field(default_factory=dict)
    base: StructType | None = None
    builtin: bool = False
    cpp_name: str | None = None  # for built-ins defined in the runtime, e.g. "sd::zlib::error"
    module: str = "__main__"  # the .sd module that defines it
    # Generic instances (Stack[int]): the C++ name, the generic's name, and the type arguments.
    mangled: str | None = None
    origin: str | None = None
    type_args: tuple = ()
    frozen: bool = False  # @dataclass(frozen=True): fields are read-only after construction

    def __str__(self) -> str:
        return self.name

    @property
    def init(self) -> FuncInfo | None:
        return self.find_method("__init__")

    def ancestors(self) -> list[StructType]:
        """This class, then its base, then its base's base..."""
        chain = []
        t: StructType | None = self
        while t is not None:
            chain.append(t)
            t = t.base
        return chain

    def is_subclass_of(self, other: StructType) -> bool:
        return other in self.ancestors()

    @property
    def is_exception(self) -> bool:
        return any(t.builtin and t.name == "BaseException" for t in self.ancestors())

    def all_fields(self) -> dict[str, Field]:
        """Inherited fields first, like a dataclass."""
        out: dict[str, Field] = {}
        for t in reversed(self.ancestors()):
            out.update(t.fields)
        return out

    def find_field(self, name: str) -> Field | None:
        return self.all_fields().get(name)

    def find_method(self, name: str) -> FuncInfo | None:
        for t in self.ancestors():
            if name in t.methods:
                return t.methods[name]
        return None


@dataclass(frozen=True)
class ClassRefType(Type):
    """A class itself, as a value: `cls` in a classmethod, `Point` in `Point.origin()`.
    It can be called (to construct) or used to call static/class methods."""

    st: StructType

    def __str__(self) -> str:
        return f"type[{self.st.name}]"


@dataclass(eq=False)
class Var:
    """One C++ variable. A source name rebound to a new type gets a new Var."""

    name: str  # the source name
    cpp_name: str  # unique within its function, e.g. 'a', 'a_1'
    type: Type
    kind: str  # 'local', 'param', 'global', 'comp' (comprehension loop variable), 'lambda' (lambda parameter)
    loc: Loc
    frame: int = 0  # which function/lambda body created it
    captured: bool = False  # read or written by a closure: stored in a shared cell
    module: str = "__main__"  # for globals: the module that owns it

    def __repr__(self) -> str:
        return f"Var({self.cpp_name}: {self.type})"


# ---- relations --------------------------------------------------------------


def is_numeric(t: Type) -> bool:
    return t in (INT, FLOAT)


def user_dunder(t: Type, name: str) -> FuncInfo | None:
    return t.find_method(name) if isinstance(t, StructType) and not t.builtin else None


def is_hashable(t: Type) -> bool:
    if t in (INT, FLOAT, BOOL, STR, BYTES, PATH, DATE, TIME, DATETIME, TIMEDELTA, UUID_T):
        return True
    if user_dunder(t, "__hash__"):
        return True
    if isinstance(t, TupleType):
        return all(is_hashable(e) for e in t.elts)
    if isinstance(t, VarTupleType):
        return is_hashable(t.elem)
    return isinstance(t, FutureType)  # (by identity)


def assignable(src: Type, dst: Type) -> bool:
    """Can a value of type `src` be stored where `dst` is expected?

    Implicit conversions are deliberately few: int -> float, and T / None -> T?.
    Containers are invariant (a list[int] is not a list[float]).
    """
    if src == dst:
        return True
    if src == INT and dst == FLOAT:
        return True
    if isinstance(src, StructType) and isinstance(dst, StructType):
        return src.is_subclass_of(dst)
    if isinstance(src, ProcessType) and isinstance(dst, ProcessType):  # an annotation: subprocess.Popen
        return src.kind == dst.kind and dst.args is None
    if type(src) is type(dst) and isinstance(src, (PatternType, MatchType)):
        return dst.info is None  # any pattern fits where no particular one is expected
    if isinstance(src, DictType) and type(dst) is DictType:  # defaultdict/Counter -> dict (a copy)
        return src.key == dst.key and src.value == dst.value
    if isinstance(src, FuncType) and isinstance(dst, FuncType):
        # Same parameters; any result is fine where the result is ignored (-> None).
        return src.params == dst.params and (dst.ret == NONE or assignable(src.ret, dst.ret))
    if isinstance(dst, OptionalType):
        if src == NONE:
            return True
        if isinstance(src, OptionalType):
            return assignable(src.inner, dst.inner)
        return assignable(src, dst.inner)
    if isinstance(dst, TupleType) and isinstance(src, TupleType) and len(src.elts) == len(dst.elts):
        return all(assignable(s, d) for s, d in zip(src.elts, dst.elts))
    if isinstance(dst, GeneratorType) and isinstance(src, (IterType, ListType)) and src.elem == dst.elem:
        return True  # `return iter(items)` / a list where an Iterator[T] is expected
    if isinstance(dst, VarTupleType) and isinstance(src, TupleType):  # (1, 2) is a tuple[int, ...]
        return all(assignable(s, dst.elem) for s in src.elts)
    return False


def join(a: Type, b: Type) -> Type | None:
    """The type that can hold both `a` and `b` (for list literals, if-expressions...), or None."""
    if a == b:
        return a
    if type(a) is type(b) and isinstance(a, (PatternType, MatchType)):
        return type(a)(None)  # different patterns: forget what's known about their groups
    if isinstance(b, VarTupleType) and isinstance(a, TupleType):
        a, b = b, a
    if isinstance(a, VarTupleType) and isinstance(b, (TupleType, VarTupleType)):  # (1, 2) with tuple[int, ...]
        elem = a.elem
        for x in (b.elts if isinstance(b, TupleType) else (b.elem,)):
            elem = join(elem, x)
            if elem is None:
                return None
        return VarTupleType(elem)
    if {a, b} == {INT, FLOAT}:
        return FLOAT
    if isinstance(a, StructType) and isinstance(b, StructType):
        return common_base(a, b)
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
        case ListType(elem) | SetType(elem) | IterType(elem) | DequeType(elem) | VarTupleType(elem) | GeneratorType(elem):
            return elem
        case DictType(key):
            return key
        case Prim("str"):
            return STR
        case Prim("bytes"):
            return INT
        case Prim("json.Value"):
            return JSON_VALUE  # iterating a JSON array
        case FileType(binary):
            return BYTES if binary else STR  # a file iterates over its lines
        case _ if t == CSV_DICT_READER:
            return DictType(STR, STR)
        case _ if t == HTTP_RESPONSE:
            return BYTES  # its lines
        case _ if t == HTTP_HEADERS:
            return STR  # the header names
        case TupleType(elts) if elts:  # the type all the items share: (1, 2.5) -> float
            common = elts[0]
            for x in elts[1:]:
                common = widen(common, x)
                if common is None:
                    return None
            return common
        case StructType() if (m := user_dunder(t, "__iter__")):
            return element_type(m.ret)  # for x in obj -> obj.__iter__()
    return None


def common_base(a: StructType, b: StructType) -> StructType | None:
    """The nearest class both inherit from (e.g. LookupError for KeyError and IndexError)."""
    for t in a.ancestors():
        if b.is_subclass_of(t):
            return t
    return None


def widen(a: Type, b: Type) -> Type | None:
    """Like join, but also combines container types element by element:
    widen(list[float?], list[float]) is list[float?]. Only safe where the values can
    be rebuilt at the wider type (container literals), so join() doesn't do this."""
    if a == UNKNOWN:
        return b
    if b == UNKNOWN:
        return a
    if (j := join(a, b)) is not None:
        return j
    match a, b:
        case ListType(x), ListType(y):
            inner = widen(x, y)
            return ListType(inner) if inner else None
        case SetType(x), SetType(y):
            inner = widen(x, y)
            return SetType(inner) if inner else None
        case DictType(k1, v1), DictType(k2, v2):
            k, v = widen(k1, k2), widen(v1, v2)
            return DictType(k, v) if k and v else None
        case TupleType(xs), TupleType(ys) if len(xs) == len(ys):
            parts = [widen(x, y) for x, y in zip(xs, ys)]
            return TupleType(tuple(parts)) if all(parts) else None
    return None


def contains_unknown(t: Type) -> bool:
    match t:
        case _ if t == UNKNOWN:
            return True
        case ListType(x) | SetType(x) | OptionalType(x):
            return contains_unknown(x)
        case DictType(k, v):
            return contains_unknown(k) or contains_unknown(v)
        case TupleType(xs):
            return any(contains_unknown(x) for x in xs)
    return False
