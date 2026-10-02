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
BYTEARRAY = Prim("bytearray")  # mutable, and a shared reference like a list; it goes where bytes go (a copy)
NONE = Prim("None")
JSON_VALUE = Prim("json.Value")  # a dynamically typed JSON value (the json module)
UNKNOWN = Prim("?")  # only while inferring literals: the element type of an empty []


@dataclass(frozen=True)
class BuiltinClass(Prim):
    """A standard-library class with no type parameters: socket.socket, logging.Logger,
    datetime.date... Its C++ type and thread rule are declared here, and builtins.py fills
    in `methods` and `attributes` next to its module; the checker, threads.py and codegen
    look them up, so a new class needs no case of its own in them. (Compared by name, like
    any Prim. What only a few classes do is still by name: `with`, iterating, ordering.)"""

    cpp: str = field(compare=False)  # its C++ type
    # How it crosses to another thread: IMMUTABLE, VALUE, COPIED, LOCKED, or "" if it can't
    # (then `unsendable` may say why, and what to do instead).
    threads: str = field(default="", compare=False)
    unsendable: str = field(default="", compare=False)
    methods: dict = field(default_factory=dict, compare=False, repr=False)  # name -> checks a call (builtins.sync_method)
    attributes: dict = field(default_factory=dict, compare=False, repr=False)  # name -> lambda t: its type
    setters: dict = field(default_factory=dict, compare=False, repr=False)  # name -> its type: `obj.name = v` (obj.set_name(v))


IMMUTABLE = "immutable"  # can never change: threads share it, and a @value class may hold it
VALUE = "value"  # copied to the other thread; a @value class may hold it
COPIED = "copied"  # copied to the other thread, but it has identity: not for a @value class
LOCKED = "locked"  # locks itself: every thread uses the same one

SOCKET = BuiltinClass("socket", "sd::socket::Socket", LOCKED)
TYPE_OBJECT = BuiltinClass("type", "sd::type_object", IMMUTABLE)  # type(x): its __name__, compared with classes
PATH = BuiltinClass("Path", "sd::pathlib::Path", IMMUTABLE)
TEXT_WRAPPER = BuiltinClass("TextWrapper", "sd::textwrap::TextWrapper")
STR_TEMPLATE = BuiltinClass("Template", "sd::stringmod::Template")
UUID_T = BuiltinClass("UUID", "sd::uuid::UUID")  # an immutable 16-byte value
SQLITE_CONNECTION = BuiltinClass("sqlite3.Connection", "sd::sqlite3::Connection")  # handles: copies share the
SQLITE_CURSOR = BuiltinClass("sqlite3.Cursor", "sd::sqlite3::Cursor")  # connection / cursor
# Incremental (de)compressors: handles (copies share the state).
ZLIB_COMPRESS = BuiltinClass("zlib.Compress", "sd::zlib::Compress")
ZLIB_DECOMPRESS = BuiltinClass("zlib.Decompress", "sd::zlib::Decompress")
BZ2_COMPRESSOR = BuiltinClass("bz2.BZ2Compressor", "sd::bz2::BZ2Compressor")
BZ2_DECOMPRESSOR = BuiltinClass("bz2.BZ2Decompressor", "sd::bz2::BZ2Decompressor")
LZMA_COMPRESSOR = BuiltinClass("lzma.LZMACompressor", "sd::lzma::LZMACompressor")
LZMA_DECOMPRESSOR = BuiltinClass("lzma.LZMADecompressor", "sd::lzma::LZMADecompressor")
HASH = BuiltinClass("hash", "sd::hashlib::Hash")  # a hashlib hash object
HMAC_T = BuiltinClass("HMAC", "sd::hmac::HMAC")
EXECUTOR = BuiltinClass("ThreadPoolExecutor", "sd::futures::ThreadPoolExecutor", LOCKED)
LOGGER = BuiltinClass("Logger", "sd::logging::Logger", LOCKED)
LOG_HANDLER = BuiltinClass("Handler", "sd::logging::Handler", LOCKED)  # StreamHandler / FileHandler / NullHandler
LOG_FORMATTER = BuiltinClass("Formatter", "sd::logging::Formatter", LOCKED)
CSV_WRITER = BuiltinClass("csv.writer", "sd::csv::Writer")
CSV_DICT_READER = BuiltinClass("csv.DictReader", "sd::csv::DictReader")
CSV_DICT_WRITER = BuiltinClass("csv.DictWriter", "sd::csv::DictWriter")
HTTP_RESPONSE = BuiltinClass(
    "http.client.HTTPResponse", "sd::httpclient::HTTPResponse",
    unsendable="a response (reading it from two threads would interleave; pass what you read from it)")
HTTP_CONNECTION = BuiltinClass(  # (HTTPSConnection too; a thread gets the connection, or a new one to the same place)
    "http.client.HTTPConnection", "sd::httpclient::HTTPConnection", COPIED)
HTTP_HEADERS = BuiltinClass("http.client.HTTPMessage", "sd::httpclient::HTTPMessage", COPIED)
URL_REQUEST = BuiltinClass("urllib.request.Request", "sd::urlrequest::Request", COPIED)
URL_PARTS = BuiltinClass("urllib.parse.ParseResult", "sd::urlparse::Parts", VALUE)
TEMPDIR = BuiltinClass("TemporaryDirectory", "sd::tempfile::TemporaryDirectory")  # removed when done
NORMAL_DIST = BuiltinClass("NormalDist", "sd::statistics::NormalDist", IMMUTABLE)  # statistics
LINEAR_REGRESSION = BuiltinClass("LinearRegression", "sd::statistics::LinearRegression", IMMUTABLE)
POLL = BuiltinClass(  # select.poll(): the descriptors it watches, in registration order
    "select.poll", "sd::select::Poll", unsendable="a poll object (give each thread its own)")
EXIT_STACK = BuiltinClass(  # contextlib.ExitStack: copies share the stack of exits
    "ExitStack", "sd::contextlib::ExitStack",
    unsendable="an ExitStack (the exits it holds belong to this thread's code)")
# signal: what signal.signal() and getsignal() give (SIG_DFL, SIG_IGN, default_int_handler or a
# function, which the checker has checked as thread code), and a handler's frame (always None).
SIGNAL_HANDLER = BuiltinClass("signal handler", "sd::signal::Handler", IMMUTABLE)
FRAME = BuiltinClass("FrameType", "sd::signal::Frame", IMMUTABLE)

# calendar.Calendar / TextCalendar / HTMLCalendar: handles whose one setting (firstweekday) is atomic
CALENDAR = BuiltinClass("calendar.Calendar", "sd::calendar::Calendar", "locked")
TEXT_CALENDAR = BuiltinClass("calendar.TextCalendar", "sd::calendar::TextCalendar", "locked")
HTML_CALENDAR = BuiltinClass("calendar.HTMLCalendar", "sd::calendar::HTMLCalendar", "locked")
DATE = BuiltinClass("date", "sd::datetime::date", IMMUTABLE)  # the datetime module's value types
TIME = BuiltinClass("time", "sd::datetime::time", IMMUTABLE)
DATETIME = BuiltinClass("datetime", "sd::datetime::datetime", IMMUTABLE)
TIMEDELTA = BuiltinClass("timedelta", "sd::datetime::timedelta", IMMUTABLE)
TIMEZONE = BuiltinClass("timezone", "sd::datetime::timezone", IMMUTABLE)
DATETIME_TYPES = (DATE, TIME, DATETIME, TIMEDELTA, TIMEZONE)

PRIMITIVES = {"int": INT, "float": FLOAT, "bool": BOOL, "str": STR, "bytes": BYTES, "bytearray": BYTEARRAY, "None": NONE, "type": TYPE_OBJECT}


@dataclass(frozen=True)
class ListType(Type):
    elem: Type

    def __post_init__(self) -> None:  # (a container holds plain function types: see FuncType.sig)
        object.__setattr__(self, "elem", strip_sig(self.elem))

    def __str__(self) -> str:
        return f"list[{self.elem}]"


@dataclass(frozen=True)
class SetType(Type):
    elem: Type

    def __post_init__(self) -> None:
        object.__setattr__(self, "elem", strip_sig(self.elem))

    def __str__(self) -> str:
        return f"set[{self.elem}]"


@dataclass(frozen=True)
class DictType(Type):
    key: Type
    value: Type

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", strip_sig(self.value))

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "elem", strip_sig(self.elem))

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
class StructFormatType(Type):
    """struct.Struct(fmt): the format is a literal, so pack/unpack know their types."""

    fmt: str

    def __str__(self) -> str:
        return "struct.Struct"


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


class SelectorSlot:
    """What a selector watches and carries: the type of its file objects (a socket, a file
    or an int) and of the data registered with them. Unknown until an annotation says, or
    its first register() decides; selectors passed where another is expected are unified
    (a union-find: `link` points to the slot that decides)."""

    def __init__(self, fileobj: Type | None = None, data: Type | None = None, decided_at: Loc | None = None):
        self.fileobj, self.data, self.decided_at = fileobj, data, decided_at
        self.link: SelectorSlot | None = None

    def root(self) -> SelectorSlot:
        slot = self
        while slot.link is not None:
            slot = slot.link
        return slot

    def decide(self, fileobj: Type, data: Type, at: Loc | None) -> None:
        self.fileobj, self.data, self.decided_at = fileobj, strip_sig(data), at


class SelectorType(Type):
    """selectors.DefaultSelector[F, D]: watches F objects (socket, file or int), each with
    data of type D. A handle: copies share the registrations. Selectors whose types are
    still unknown are told apart by identity (their slot); known ones by their types."""

    def __init__(self, slot: SelectorSlot | None = None):
        self.slot = slot or SelectorSlot()

    @property
    def known(self) -> bool:
        return self.slot.root().fileobj is not None

    @property
    def fileobj(self) -> Type | None:
        return self.slot.root().fileobj

    @property
    def data(self) -> Type | None:
        return self.slot.root().data

    def __eq__(self, other) -> bool:
        if not isinstance(other, SelectorType):
            return False
        a, b = self.slot.root(), other.slot.root()
        return a is b or (a.fileobj is not None and (a.fileobj, a.data) == (b.fileobj, b.data))

    def __hash__(self) -> int:
        return hash("DefaultSelector")

    def __str__(self) -> str:
        return f"DefaultSelector[{self.fileobj}, {self.data}]" if self.known else "DefaultSelector"

    __repr__ = __str__


def unify_selectors(src: SelectorType, dst: SelectorType) -> bool:
    """A selector stored where another is expected: they're the same selector type from now
    on (whichever is still unknown takes the other's types)."""
    a, b = src.slot.root(), dst.slot.root()
    if a is b:
        return True
    if a.fileobj is not None and b.fileobj is not None:
        return (a.fileobj, a.data) == (b.fileobj, b.data)
    if a.fileobj is None:
        a.link = b
    else:
        b.link = a
    return True


@dataclass(frozen=True)
class SelectorKeyType(Type):
    """selectors.SelectorKey[F, D]: a registration (`fileobj`, `fd`, `events`, `data`)."""

    fileobj: Type
    data: Type

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", strip_sig(self.data))

    def __str__(self) -> str:
        return f"SelectorKey[{self.fileobj}, {self.data}]"


@dataclass(frozen=True)
class GeneratorType(Type):
    """What a generator function returns (and iter(xs)): values produced on demand.
    Copies share their position, like Python's iterator objects."""

    elem: Type

    def __str__(self) -> str:
        return f"Iterator[{self.elem}]"


@dataclass(frozen=True)
class ContextManagerType(Type):
    """What contextlib makes: a @contextmanager function's result, nullcontext(), closing(),
    suppress(). A `with` statement's `as` target gets `elem` (None: nothing to bind). Copies
    share it, like Python's objects."""

    elem: Type

    def __str__(self) -> str:
        return f"ContextManager[{self.elem}]"


@dataclass(frozen=True)
class VarTupleType(Type):
    """tuple[T, ...]: a tuple of any length (tuple(xs), Path.parts)."""

    elem: Type

    def __post_init__(self) -> None:
        object.__setattr__(self, "elem", strip_sig(self.elem))

    def __str__(self) -> str:
        return f"tuple[{self.elem}, ...]"


@dataclass(frozen=True)
class CmpKeyType(Type):
    """What functools.cmp_to_key(cmp) makes of a value: a key that orders by cmp."""

    elem: Type

    def __str__(self) -> str:
        return "functools.KeyWrapper"


@dataclass(frozen=True)
class HTTPServerType(Type):
    """http.server.HTTPServer / ThreadingHTTPServer, with the handler class it serves, so the
    thread checker knows what code its threads run (None: a server from elsewhere, as in an
    annotation)."""

    handler: object = None  # StructType (compared by identity)
    threading: bool = False

    def __str__(self) -> str:
        return "ThreadingHTTPServer" if self.threading else "HTTPServer"


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


@dataclass(eq=False)
class Signature:
    """The parameter names, kinds and defaults of the function a value is known to be (a def,
    a bound method, a lambda, a partial), so a call through the value can use keywords and
    leave out arguments that have defaults. Compared by identity: one per function."""

    what: str  # for messages: "f()"
    params: list  # Param


@dataclass(frozen=True)
class FuncType(Type):
    """`(int, str) -> bool`. In *expected-type hints only*, ret may be None,
    meaning "infer it" (e.g. the key function passed to sorted()). `sig`: which function
    it is, when that's known here (see Signature); only a name's flow-sensitive view keeps it,
    so what a variable or container holds is a plain function type."""

    params: tuple[Type, ...]
    ret: Type | None
    sig: Signature | None = field(default=None, hash=False)

    def __str__(self) -> str:
        return f"({', '.join(map(str, self.params))}) -> {self.ret if self.ret is not None else '?'}"


def fills_defaults(src: FuncType, dst: FuncType) -> bool:
    """Can a known function stand in for a function type with fewer parameters, because the
    rest have defaults (or are *args)? `def greet(name, greeting="hi")` as a (str) -> str."""
    if src.sig is None or len(dst.params) >= len(src.sig.params):
        return False
    given, rest = src.sig.params[:len(dst.params)], src.sig.params[len(dst.params):]
    return (all(not p.star and p.kind != "kwonly" for p in given) and tuple(p.type for p in given) == dst.params
            and all(p.star or p.double_star or p.default is not None for p in rest))


def filled_for(ft: Type, given: tuple) -> FuncType | None:
    """For a known function called with arguments of types `given` (fewer than it takes):
    the function type it stands in as, its other parameters left to their defaults."""
    if not isinstance(ft, FuncType) or ft.sig is None or len(given) >= len(ft.sig.params):
        return None
    if not all(assignable(a, p.type) for a, p in zip(given, ft.sig.params)):
        return None
    want = FuncType(tuple(p.type for p in ft.sig.params[:len(given)]), ft.ret)
    return want if fills_defaults(ft, want) else None


def strip_sig(t: Type) -> Type:
    """t without what it knows about which function a value is (for what gets stored)."""
    match t:
        case FuncType(params, ret, sig) if sig is not None:
            return FuncType(params, ret)
        case OptionalType(inner) if isinstance(inner, FuncType) and inner.sig is not None:
            return OptionalType(strip_sig(inner))
        case TupleType(elts) if any(strip_sig(x) is not x for x in elts):
            return TupleType(tuple(strip_sig(x) for x in elts))
    return t


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
    """What open() returns: TextIO (reads/writes str) or BinaryIO (bytes). io.StringIO and
    io.BytesIO are the same file objects over memory (`memory`), with getvalue() as well;
    they fit wherever a TextIO / BinaryIO is expected."""

    binary: bool
    memory: bool = False

    def __str__(self) -> str:
        if self.memory:
            return "BytesIO" if self.binary else "StringIO"
        return "BinaryIO" if self.binary else "TextIO"


TEXT_FILE = FileType(False)
BINARY_FILE = FileType(True)
STRING_IO = FileType(False, memory=True)
BYTES_IO = FileType(True, memory=True)


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


@dataclass
class ClassAttr:
    """`version = "1.0"` in a class body: a constant shared by the class and its objects
    (read as self.version or Cls.version), which a subclass may set to its own value."""

    name: str
    type: Type
    value: object  # ast.Expr: a constant
    loc: Loc


@dataclass(eq=False)
class Param:
    name: str
    type: Type
    default: object | None  # ast.Expr
    loc: Loc
    star: bool = False  # `*args: T`: type is tuple[T, ...], filled from the remaining positional arguments
    kind: str = "normal"  # or "posonly" (before `/`), "kwonly" (after `*` or `*args`): by name only
    double_star: bool = False  # `**kwargs: T`: type is dict[str, T], filled from the other keyword arguments


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
    # @contextlib.contextmanager: a generator returning a ContextManager[T] (see check_context_generator);
    # cm_suppresses: an exception thrown in at its yield may be caught (and so swallowed)
    context_manager: bool = False
    cm_suppresses: bool = False
    lazy: bool = False  # @functools.cached_property: a getter whose value is kept after the first time

    def __str__(self) -> str:
        params = ", ".join(f"*{p.name}: {p.type.elem}" if p.star else f"**{p.name}: {p.type.value}" if p.double_star
                           else f"{p.name}: {p.type}" for p in self.params)
        return f"def {self.name}({params}) -> {self.ret}"

    def value_type(self) -> FuncType:
        """Its type as a value (`f`, `obj.method`), which knows its parameters' names and defaults."""
        if (sig := self.__dict__.get("_signature")) is None:
            sig = self.__dict__["_signature"] = Signature(f"{self.name}()", self.params)
        return FuncType(tuple(p.type for p in self.params), self.ret, sig)


@dataclass(eq=False)
class EnumMember:
    """`RED = 1` in an enum's body. Its value is computed when compiling (members are constants)."""

    name: str
    value: object  # the Python value: an int, str, float, bool, bytes, or a tuple of those
    loc: Loc
    alias_of: str | None = None  # `CRIMSON = 1` after `RED = 1` is another name for RED


@dataclass(eq=False)
class EnumInfo:
    """An enum class (`class Color(Enum):`): a fixed set of named, immutable members."""

    base: str  # 'Enum', 'IntEnum', 'StrEnum', 'Flag' or 'IntFlag'
    value_type: Type
    members: dict[str, EnumMember] = field(default_factory=dict)  # every name, aliases too, in order
    unique: bool = False  # @unique

    @property
    def flag(self) -> bool:
        return self.base in ("Flag", "IntFlag")

    @property
    def mixin(self) -> Type | None:
        """IntEnum/IntFlag members are ints and StrEnum members strs, wherever one is expected."""
        return {"IntEnum": INT, "IntFlag": INT, "StrEnum": STR}.get(self.base)

    @property
    def canonical(self) -> list[EnumMember]:
        """The members iterating the class gives: not aliases (nor, in a Flag, 0 or several bits)."""
        if self.flag:
            return [m for m in self.members.values() if m.alias_of is None and m.value > 0 and m.value & (m.value - 1) == 0]
        return [m for m in self.members.values() if m.alias_of is None]

    @property
    def distinct(self) -> list[EnumMember]:
        """Every member that isn't another name for an earlier one."""
        return [m for m in self.members.values() if m.alias_of is None]


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
    # A runtime base class whose fields are its members in C++ (BaseHTTPRequestHandler): a
    # subclass reaches them as fields, but they aren't part of its constructor or repr.
    runtime_fields: bool = False
    class_attrs: dict[str, ClassAttr] = field(default_factory=dict)  # declared here (a subclass may redefine one)
    enum: EnumInfo | None = None  # an enum class (kind 'struct', frozen: its members are immutable values)

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
        """Inherited fields first, like a dataclass (not a runtime base class's: runtime_fields)."""
        out: dict[str, Field] = {}
        for t in reversed(self.ancestors()):
            if not t.runtime_fields:
                out.update(t.fields)
        return out

    def find_field(self, name: str) -> Field | None:
        if (f := self.all_fields().get(name)) is not None:
            return f
        return next((t.fields[name] for t in self.ancestors() if t.runtime_fields and name in t.fields), None)

    def find_method(self, name: str) -> FuncInfo | None:
        for t in self.ancestors():
            if name in t.methods:
                return t.methods[name]
        return None

    def find_class_attr(self, name: str) -> ClassAttr | None:
        for t in self.ancestors():
            if name in t.class_attrs:
                return t.class_attrs[name]
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
    if user_dunder(t, "__hash__") or (isinstance(t, StructType) and t.enum is not None):
        return True
    if isinstance(t, TupleType):
        return all(is_hashable(e) for e in t.elts)
    if isinstance(t, VarTupleType):
        return is_hashable(t.elem)
    return isinstance(t, FutureType)  # (by identity)


def assignable(src: Type, dst: Type) -> bool:
    """Can a value of type `src` be stored where `dst` is expected?

    Implicit conversions are deliberately few: int -> float, T / None -> T?, and bytearray ->
    bytes (a copy, for an argument: see Checker.no_bytearray). Containers are invariant (a
    list[int] is not a list[float]).
    """
    if src == dst:
        return True
    if (src, dst) in ((INT, FLOAT), (BYTEARRAY, BYTES)):
        return True
    if isinstance(src, SelectorType) and isinstance(dst, SelectorType):
        return unify_selectors(src, dst)
    if isinstance(src, StructType) and src.enum is not None and src.enum.mixin is not None and not isinstance(dst, StructType):
        return assignable(src.enum.mixin, dst)  # an IntEnum member is an int, a StrEnum member a str
    if isinstance(src, StructType) and isinstance(dst, StructType):
        return src.is_subclass_of(dst)
    if isinstance(src, FileType) and isinstance(dst, FileType):  # a StringIO is a TextIO
        return src.binary == dst.binary and not dst.memory
    if isinstance(src, HTTPServerType) and isinstance(dst, HTTPServerType):  # `server: HTTPServer`
        return dst.handler is None and (src.threading or not dst.threading)
    if isinstance(src, ProcessType) and isinstance(dst, ProcessType):  # an annotation: subprocess.Popen
        return src.kind == dst.kind and dst.args is None
    if type(src) is type(dst) and isinstance(src, (PatternType, MatchType)):
        return dst.info is None  # any pattern fits where no particular one is expected
    if isinstance(src, DictType) and type(dst) is DictType:  # defaultdict/Counter -> dict (a copy)
        return src.key == dst.key and src.value == dst.value
    if isinstance(src, FuncType) and isinstance(dst, FuncType):
        # Same parameters; any result is fine where the result is ignored (-> None).
        returns = dst.ret == NONE or assignable(src.ret, dst.ret)
        return returns and (src.params == dst.params or fills_defaults(src, dst))
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
    if isinstance(a, FuncType) and isinstance(b, FuncType) and strip_sig(a) == strip_sig(b):
        return strip_sig(a)  # two different functions of the same type
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
    if isinstance(a, FileType) and isinstance(b, FileType) and a.binary == b.binary:
        return FileType(a.binary)  # a StringIO and an open() file: a TextIO
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
        case Prim("bytes") | Prim("bytearray"):
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
        case ClassRefType(st) if st.enum is not None:
            return st  # for c in Color
        case StructType() if t.enum is not None and t.enum.flag:
            return t  # the members a flag value holds
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


def enum_mixin(t: Type) -> Type | None:
    """int for an IntEnum or IntFlag member, str for a StrEnum member (it's used as one), else None."""
    return t.enum.mixin if isinstance(t, StructType) and t.enum is not None else None


def enum_decays(op: str, t: Type, other: Type, right: bool = False) -> bool:
    """In `t op other` (or `other op t`, if `right`), is the IntEnum/StrEnum member t used
    as its value? It is, except where it meets its own enum: `N.ONE == N.ONE`, `n in N`."""
    if enum_mixin(t) is None or op in ("is", "is not"):
        return False
    if op in ("in", "not in"):
        return enum_mixin(t) == STR if right else element_type(other) != t  # "a" in Mode.FAST
    return op in ("<", "<=", ">", ">=") or strip_optional(other) != t


def bool_decays(op: str, l: Type, r: Type) -> bool:
    """In `l op r`, is a bool used as the int it is in Python (True + 1, sum of bools,
    (a > b) - (a < b))? Not where two bools meet in & | ^, which give a bool."""
    if BOOL not in (l, r) or op not in ("+", "-", "*", "/", "//", "%", "**", "&", "|", "^", "<<", ">>"):
        return False
    if l == r == BOOL and op in ("&", "|", "^"):
        return False
    return all(t in (BOOL, INT, FLOAT) for t in (l, r))


def enum_flag_op(op: str, l: Type, r: Type) -> Type | None:
    """The flag type `l op r` gives, if it's a flag operation: `P.R | P.W`, or an IntFlag
    with an int (`IP.R | 8`)."""
    if op not in ("|", "&", "^"):
        return None
    for a, b in ((l, r), (r, l)):
        if isinstance(a, StructType) and a.enum is not None and a.enum.flag:
            if b == a or (a.enum.base == "IntFlag" and b == INT):
                return a
    return None
