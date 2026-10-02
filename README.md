<p align="center">
  <img src="docs/logo.svg" alt="seadash" width="640">
</p>

<p align="center"><b>C++ with the simplicity of Python.</b></p>

---

**seadash** is a programming language that looks like Python and runs like C++. You write
code with Python's syntax: indentation, slicing, f-strings, list comprehensions,
`import base64`. The compiler type-checks the whole program and translates it into C++23,
and a C++ compiler builds a native binary.

- **Familiar:** most small Python programs are valid seadash, and many of the test programs run unchanged under `python3`.
- **Checked:** types are inferred and checked before anything runs. `None` can't sneak into a `str`, and threads can't race on shared data.
- **Fast:** no interpreter and no GIL. Typical programs run 7–70x faster than CPython.
- **Small:** the compiler is plain Python, and the runtime is a header-only C++ library.

```python
from collections import Counter
from dataclasses import dataclass


@dataclass
class Book:
    title: str
    words: list[str]

    @property
    def longest(self) -> str:
        return max(self.words, key=len)


def find(books: list[Book], title: str) -> Book | None:
    for b in books:
        if b.title == title:
            return b
    return None


books = [
    Book("sea", "the sea the sea is calm".split()),
    Book("dash", "dash off a quick note".split()),
]
counts = Counter(w for b in books for w in b.words)
print(counts.most_common(2))

book = find(books, "dash")
if book is not None:  # book is Book? until checked
    print(f"{book.title}: {len(book.words)} words, longest is {repr(book.longest)}")
```

```console
$ sd run books.sd
[('the', 2), ('sea', 2)]
dash: 5 words, longest is 'quick'
```

This program is also valid Python, and `python3 books.sd` prints the same thing.

## Why

Python is a joy to write and often too slow to run. C++ is fast, but its templates,
standard-library spelling and `reinterpret_cast`s get in the way. seadash keeps the
Python you write and gives you the C++ you'd want underneath.

`bench/run.py` checks that both produce identical output, then times them (best of 3; under
python3, `from seadash import value` finds the compiler's package, whose `value` does nothing):

| benchmark | python3 | seadash | speedup |
|---|---:|---:|---:|
| `lists_dicts`: building and querying lists and dicts | 2.10s | 0.29s | **7.2x** |
| `small_lists`: many small lists, lists through functions (merge sort), dicts of lists | 2.66s | 0.47s | **5.7x** |
| `particles`: a `@value` class holding a list, rebuilt every step and looped over | 1.67s | 0.16s | **10x** |
| `objects`: classes, virtual methods, floats, recursion | 2.52s | 0.04s | **67x** |
| `threads`: parallel CPU work, queues, locks | 7.49s | 0.20s | **37x** |
| `queue_batches`: a producer thread sending big batches through a queue | 3.53s | 0.09s | **41x** |
| `hashing`: hashlib/hmac on small messages, bulk data, pbkdf2 | 1.22s | 0.89s | 1.4x |
| `sockets`: localhost TCP round trips, bulk transfer, connections | 1.87s | 1.75s | 1.1x |
| `json_load`: `json.loads` on large documents and many small ones | 3.24s | 3.28s | 1.0x |

The threads benchmark shows what real parallelism looks like: on CPython, 8 threads take
exactly as long as 1. The last three are honest about where seadash can't help much: most
of their time is spent in OpenSSL, in the kernel, or (for JSON) in a parser that is native
code in both languages. seadash builds with `-O3`.

## Getting started

You need:

- **Python 3.12+**, to run the compiler.
- **A C++23 compiler:** g++ 14 or newer on Linux; on macOS, the clang that comes with Xcode's command line tools (`xcode-select --install`). Set `SEADASH_CXX` to pick a specific one.
- **zlib, PCRE2 and OpenSSL** development headers, for the `zlib`, `re`/`textwrap` and `hashlib` modules (`apt install zlib1g-dev libpcre2-dev libssl-dev`, or `brew install pcre2 openssl@3` on macOS).

```console
$ python3 -m venv .venv && .venv/bin/pip install -e .
$ .venv/bin/sd run books.sd     # the example above
```

| command | what it does |
|---|---|
| `sd run FILE [ARGS...]` | build to a temporary binary and run it |
| `sd build FILE [-o NAME]` | compile to a native binary (`--debug` for a faster, unoptimized build) |
| `sd check FILE` | type-check, and list every variable's inferred type |
| `sd emit FILE` | print the generated C++ |
| `sd tokens` / `sd ast` | debugging views of the lexer and parser |
| `sd clean` | empty the build cache |

Builds are cached (in `~/.cache/seadash`): running a program you haven't changed skips the
C++ compiler entirely, and the runtime header is precompiled once (with g++), so small edits
rebuild quickly. `--no-cache` always compiles.

`sd run` with no file (or `-`) reads the program from stdin:

```console
$ echo 'print(sum(x * x for x in range(10)))' | sd run
285
```

## The language in a nutshell

**Types are inferred, and names can be rebound to a new type.** Each type gets its own C++ variable.

```python
a = 2
a = str(a)          # fine: a is now a str
total: float = 0    # annotate when you want to
```

**`@value` classes are values; other classes are shared references.** Lists, dicts and sets
are shared references too, as in Python; a value class's lists are part of its value, so
they're copied with it. `@value` is ordinary Python syntax, so seadash files still work with
Python's tools, and dropping the decorator gives Python code.

```python
from seadash import value

@value
class Point:
    x: int
    y: int

class Counter:
    n: int
    def bump(self):
        self.n += 1

p = Point(1, 2)
q = p          # a copy: changing q.x leaves p alone
c = Counter(0)
d = c          # the same object: d.bump() changes c.n
xs = [1, 2]
ys = xs        # the same list, as in Python: ys.append(3) changes xs
```

**None-safety.** `T?` (or `T | None`) is "T or None", and the checker makes you handle the `None` case. Checks narrow the type, including checks on attributes and `isinstance`.

```python
def find(xs: list[str], target: str) -> int?:
    ...
if (i := find(names, "bob")) is not None:
    print(names[i])        # i is an int here
```

**`match` statements**, with every kind of pattern Python has: literals, captures, `|`,
`as`, sequences with `*rest`, mappings with `**rest`, class patterns and guards. Captured
names get precise types, matching narrows the subject (`case Dog():`), and patterns that
can never match are compile errors. Class patterns work on `json.Value` too.

```python
match command.split():
    case ["go", direction]:
        go(direction)                # direction is a str
    case ["drop", *items] if items:
        drop(items)                  # items is a list[str]
    case ["quit" | "exit"]:
        quit()

match shape:                         # a json.Value
    case {"type": "circle", "r": float(r)}:
        print(3.14159 * r * r)
    case {"type": "rect", "size": [int(w), int(h)]}:
        print(w * h)
```

**Also supported:**
- **Functions:** default and keyword arguments, keyword-only and positional-only parameters (`def f(a, /, b, *, c)`), `**kwargs` (typed: `**opts: int` is a `dict[str, int]`) and `f(**d)`, `*args` (typed: `*args: str` is a `tuple[str, ...]`), generics (`def first[T](xs: list[T]) -> T | None`, `class Stack[T]:`), closures that share variables like Python's, lambdas, and functions as values, which keep their keywords and defaults (a function with defaults also fits where fewer parameters are expected: `map(greet, names)`).
- **Generators:** `yield` and `yield from` (in functions and methods, including `__iter__`), `next()`, and `iter()`; they're C++20 coroutines, so values are made on demand, even from infinite generators. Generator expressions, `map`, `filter`, `zip` and `enumerate` are lazy too.
- **Bools are ints in arithmetic**, as in Python: `True + 1`, `(a > b) - (a < b)`, `sum(x > 0 for x in xs)` (but not `~`, which Python 3.16 removes for bools).
- **Statements:** `del` of names, items, slices (`del xs[::2]`) and keys, and a class's own `__delitem__`; assigning any iterable to a slice of a list (`xs[1:3] = ...`, `xs[::2] = ...`); unpacking any iterable, with a starred name for the rest (`key, value = line.split("=")`, `first, *rest = xs`, also in `for` loops), in displays (`[*xs, *ys]`, `(*pair, 1)`, `{*a, *b}`, `{**defaults, **chosen}`), and in calls (`f(*args)`, `print(*row, sep=", ")`).
- **Error handling:** exceptions (`try`/`except`/`finally`/`raise`, custom exception classes), and `with` statements.
- **Classes:** class attributes (`version = "1.0"`: constants a subclass can set to its own value), inheritance and dunder methods (`__add__`, `__eq__`, `__lt__`, `__hash__`, `__getitem__`, `__iter__`, `__str__`...).
- **Decorators:** `@dataclass`, `@property`, `@staticmethod`, `@classmethod`, `@functools.cache`, and your own decorators.
- **Modules:** `import` of your own `.sd` files and packages.
- **Built-ins:** Python's built-in functions (`print`, `len`, `range`, `enumerate`, `zip`, `map`, `filter`, `sorted`, `min`/`max`, `sum`, `divmod`, `round`, `bin`/`hex`/`oct`, `int`/`float`/`str`/`bytes`...) with their keyword arguments (`min(xs, default=0)`, `zip(a, b, strict=True)`, `int("ff", 16)`), and the methods of `str`, `bytes`, `int`, `float`, `list`, `dict`, `set`, `tuple` and files.

**Threads without a GIL, checked for data races.** Threads are real OS threads running in
parallel. Anything that crosses into another thread (`Thread` arguments, `queue.Queue`
items, `executor.submit` arguments and results, what goes into and out of a `Mutex`, and
the variables a thread's closure uses from its enclosing function) is copied, lists and
all, unless it's a thread-safe object: `threading.Lock`, `queue.Queue`, or seadash's own
`Mutex[T]`, `RWMutex[T]`, `Atomic` and `Synchronized` classes (`from seadash import Mutex`;
under python3 that import finds working Python versions, so such programs still run there).
A `Mutex` owns what it's given: `shared = Mutex(data)` moves `data` in, and using `data`
afterwards is an error. A list the sender never uses again
is moved rather than copied, and inside `with ThreadPoolExecutor() as pool:` a task that only
reads a list shares it rather than copying it, when nothing can change it until the task ends. A thread that changes its copy and never uses it is an error
(the change would be lost). Threads may read module globals that nothing changes once they've started (module code
before a thread starts may still build them); changing
a list through another name, or passing it to a function that could change it, counts:

```python
total = 0
def work():
    global total
    total += 1   # error: thread code uses the module-level 'total' (int), but it's
                 # modified. Threads can't share it safely: wrap it in a seadash.Mutex,
                 # send data through a queue.Queue, or pass a copy in args=
```

## Standard library

| module | notes |
|---|---|
| `base64`, `binascii`, `zlib` | encode, decode, compress; `hexlify` with separators, `crc32`, `crc_hqx` (not yet: uu and quoted-printable); zlib's `compressobj`/`decompressobj` with `wbits`, `flush` modes, `unconsumed_tail`, `copy` |
| `gzip`, `bz2`, `lzma` | `compress`, `decompress` (Python's exact output and errors) and `open`, which gives the same file objects as `open()`, as do `GzipFile`/`BZ2File`/`LZMAFile`; the incremental `BZ2Compressor`/`BZ2Decompressor` and `LZMACompressor`/`LZMADecompressor` (with `max_length`, `eof`, `needs_input`, `unused_data`); `lzma` does the xz and .lzma formats with presets and integrity checks (not yet: `fileobj=`, `FORMAT_RAW` and filter chains) |
| `errno`, `stat` | the error numbers and `errorcode` (and `OSError` carries `errno`, `strerror` and `filename`); the `st_mode` bits, `S_ISDIR`..., `S_IMODE`, `S_IFMT`, `filemode` |
| `shlex` | `split`, `quote`, `join` (POSIX mode) |
| `mimetypes` | `guess_type` (a str or Path; URLs and `data:` URLs), `guess_extension`, `guess_all_extensions`, `add_type`, and the `types_map`, `common_types`, `encodings_map`, `suffix_map` dicts; Python's built-in table only (not `init`, `MimeTypes` or `read_mime_types`) |
| `html` | `escape` and `unescape`, with the full HTML5 named-reference table and Python's handling of numeric and semicolon-less references (not yet: `html.entities`, `html.parser`) |
| `uuid` | `UUID` values (ordered, hashable), `uuid1`/`uuid3`/`uuid4`/`uuid5`, the namespaces; not yet: `u.int` (128 bits) and `getnode()` |
| `json` | typed: `json.loads` straight into your dataclasses, lists and dicts; `json.Value` for dynamic JSON |
| `tomllib` | `loads`, `load` (a file opened `"rb"`) and `TOMLDecodeError` (with `msg`, `pos`, `lineno`, `colno`): all of TOML 1.0, from a port of Python's parser, so documents are accepted and rejected alike, with Python's messages. Typed like `json.loads` (`config: Config = tomllib.loads(text)`, into classes, nested tables, arrays of tables, `dict[str, ...]`, optionals for missing keys, and `date`/`time`/`datetime` for TOML's dates and times); without an annotation the document is a `dict[str, json.Value]`, whose values can also be dates and times (`v.is_date()`, `v.as_date()`...). Not yet: `parse_float=` |
| `sqlite3` | `connect`, `Connection` and `Cursor` with `execute`/`executemany`/`executescript`, `?` and `:name` parameters, `commit`/`rollback`/`with conn:`, `rowcount`, `lastrowid`, `description`, and Python's exception classes and messages; rows are typed like `json.loads` (`rows: list[tuple[int, str]] = cur.fetchall()`). Not yet: `row_factory`/`sqlite3.Row`, `create_function`, iterating a cursor directly |
| `os`, `os.path` | files, directories, environment, `symlink`, `chdir`; file descriptors (`os.open`/`read`/`write`/`close`/`dup`/`pipe`, `os.fdopen`, `open(fd)`, `f.fileno()`) |
| `sys` | `argv`, `exit`, `stdin`/`stdout`/`stderr`, `platform`, `maxsize` |
| `io` | `StringIO` (with `newline=`) and `BytesIO`: the file objects `open()` gives, over memory, so `print(file=)`, `csv`, `json.dump`/`load`, `logging.StreamHandler`, `hashlib.file_digest` and functions taking a `TextIO`/`BinaryIO` accept them; `getvalue`, `read`/`readline`/`readlines`, iteration, `write`/`writelines`, `seek`/`tell`, `truncate`, `close`/`with`, with Python's errors; `io.TextIOBase`/`io.BufferedIOBase` in annotations. Not yet: `readline(size)`, `readlines(hint)`, `getbuffer`, `read1`/`readinto`, `io.UnsupportedOperation`, `TextIOWrapper` |
| `csv` | `reader`, `writer`, `DictReader`, `DictWriter` with every dialect option and quoting mode; a port of CPython's parser, so quoting edge cases match |
| `logging` | loggers (a dotted hierarchy with levels and propagation), `StreamHandler`/`FileHandler`/`NullHandler`, `Formatter` (`%(levelname)s`, `%(lineno)d`...), `basicConfig`, and lazy `log.info("x=%s", x)` messages; thread-safe |
| `urllib.request`, `urllib.parse`, `urllib.error` | `urlopen` (GET/POST over HTTP and HTTPS with certificate checks, redirects, timeouts, `context=`; the body streams), `Request`, response headers; `urlparse`/`urlsplit`, `quote`/`unquote`, `urlencode`, `urljoin`, `parse_qs`; `HTTPError` (also readable as the error page) and `URLError` |
| `http.client` | `HTTPConnection`/`HTTPSConnection` (keep-alive, reconnecting, chunked bodies), `request` or `putrequest`/`putheader`/`endheaders`, streaming `HTTPResponse` (`read(n)`, `readline`, `getheader`...), `responses` and the status constants, Python's exceptions and connection-state errors |
| `http.server` | `HTTPServer`, `ThreadingHTTPServer` (`serve_forever`, `shutdown`, `handle_request`, `server_close`, `with`) and `BaseHTTPRequestHandler`: subclass it with `do_GET()`... and use `send_response`/`send_header`/`end_headers`/`send_error`, `rfile`/`wfile`, `headers`, `path`; Python's request parsing, errors and log lines; `SimpleHTTPRequestHandler` serves files (listings, index pages, `If-Modified-Since`), from `directory=` given with `functools.partial`. A `ThreadingHTTPServer`'s handlers (or a server running on a thread) are checked like any thread code |
| `ssl` | for HTTPS clients: `create_default_context`, `SSLContext` (`check_hostname`, `verify_mode`, `load_verify_locations`), `_create_unverified_context`, `SSLError`/`SSLCertVerificationError` |
| `time`, `math`, `random` | the usual |
| `socket` | TCP/UDP with Python's API and errors; `socket.socket(fileno=fd)` takes over a descriptor |
| `threading`, `queue` | threads, locks, events, queues |
| `signal` | `signal` (handlers are `def handler(signum: int, frame: FrameType \| None)`, with `from types import FrameType`) and `getsignal`, `SIG_DFL`/`SIG_IGN`/`default_int_handler`, `raise_signal`, `alarm`, `pause`, `strsignal`, `valid_signals`, `NSIG`, and the `Signals` IntEnum with its `SIGINT`, `SIGTERM`... (the signals Linux and macOS share); `os.kill`, `os.getpid`. Handlers run on a thread of their own, checked like any thread code (see the differences below) |
| `seadash` | `@value`, and for sharing between threads `Mutex[T]` (owns its data: `with m as data:`), `RWMutex[T]` (`with m.read()` / `m.write()`), `Atomic` and `Synchronized` classes |
| `concurrent.futures` | `ThreadPoolExecutor` (`submit`, `map`, `shutdown`, `with`), `Future`, `as_completed`, `wait`; work on the pool is checked for data races like threads are |
| `itertools` | all of it, lazily: `count`, `cycle`, `repeat`, `accumulate`, `chain`, `groupby`, `islice`, `tee`, `zip_longest`, `product`, `permutations`, `combinations`, `pairwise`, `batched`... (`permutations(xs, 2)` gives `tuple[T, T]`) |
| `heapq`, `bisect` | `heappush`, `heappop`, `heapify`, `heappushpop`, `heapreplace`, `nlargest`/`nsmallest`, and a lazy `merge` (`key=`, `reverse=`); `bisect_left`/`bisect_right`/`bisect`, `insort_left`/`insort_right`/`insort` with `lo`, `hi`, `key=`. On any list whose items compare with `<` (numbers, strings, tuples, classes with `__lt__`); CPython's algorithms, so a heap holds its items in Python's order. Not yet: Python 3.14's `heappush_max` and friends |
| `struct` | `pack`, `unpack`, `unpack_from`, `iter_unpack`, `calcsize`, `Struct`: every format code, byte order and native alignment, with Python's errors; the format literal decides the types (`struct.unpack("<hf", data)` is a `tuple[int, float]`). Ints are 64-bit, so `Q`/`N` values above 2**63-1 aren't representable |
| `statistics` | `mean`, `fmean` (`weights=`), `geometric_mean`, `harmonic_mean`, `median` and `median_low`/`median_high`/`median_grouped`, `mode`, `multimode`, `quantiles` (`n=`, `method=`), `variance`/`stdev`/`pvariance`/`pstdev` (`xbar=`/`mu=`), `covariance`, `correlation` (`method="ranked"`), `linear_regression` (`.slope`, `.intercept`, `proportional=`), `NormalDist` (`pdf`, `cdf`, `inv_cdf`, `quantiles`, `overlap`, `zscore`, `samples`, `from_samples`, its arithmetic), `StatisticsError`, with Python's algorithms and messages; not yet `kde`/`kde_random` |
| `secrets` | `token_bytes`/`token_hex`/`token_urlsafe`, `randbelow`, `randbits` (up to 63), `choice`, `compare_digest`, on OpenSSL's random source |
| `hashlib`, `hmac` | every guaranteed algorithm (md5, sha1/2/3, blake2, shake) on OpenSSL, `pbkdf2_hmac`, `file_digest`; `hmac.new`, `hmac.digest`, `compare_digest` |
| `string` | the character-set constants, `capwords`, and `Template` (`$name` / `${name}` with `substitute` and `safe_substitute`) |
| `textwrap` | `wrap`, `fill`, `shorten`, `dedent`, `indent`, `TextWrapper`: a port of Python's algorithm (hyphens, em-dashes, `max_lines`), so lines break where Python's do |
| `collections` | `defaultdict`, `Counter`, `deque` |
| `re` | Python's regular expressions on PCRE2; literal patterns are checked at compile time, and `m.group(1)` is a `str` when the group always matches |
| `subprocess` | `run`, `check_output`, `call`, `Popen` (streaming, `communicate`, timeouts); `r.stdout` is `str` or `bytes` depending on `text=`, and reading an uncaptured stream is a compile error |
| `pathlib` | `Path` values: `/` joins, `name`/`stem`/`suffix`/`parent`, `read_text`/`write_text`, `mkdir`, `iterdir`, `glob`/`rglob`, `resolve`...; `open()` takes a `Path` too |
| `fnmatch`, `glob` | `fnmatch`, `fnmatchcase`, `filter`, `filterfalse`, `translate` (Python 3.14's regex, character for character); `glob`, `iglob` (lazy) with `root_dir=` (a str or Path), `dir_fd=`, `recursive=` (`**`) and `include_hidden=`, `escape`, `translate`: ports of Python's, so hidden files, trailing slashes and symlinks behave the same, and results come in directory order (sort them for a stable order, as in Python) |
| `shutil`, `tempfile` | `copy`/`copy2`/`copytree`, `move`, `rmtree`, `which`, `disk_usage`; `TemporaryDirectory` (removes itself), `mkdtemp`, `gettempdir` |
| `datetime`, `zoneinfo` | `date`, `time`, `datetime`, `timedelta`, `timezone`, and named zones like `ZoneInfo("Europe/Paris")` (daylight saving time included): arithmetic, `strftime`/`strptime`, `isoformat`/`fromisoformat`, `now()`/`today()`, time zone conversion |
| `email.utils` | the RFC 2822 dates of HTTP and mail headers: `formatdate` (`usegmt=`, `localtime=`), `format_datetime`, `parsedate_to_datetime` (Python's forgiving parser and its errors), `parsedate_tz`, `parsedate`; not yet the address functions (`parseaddr`, `formataddr`...) or `mktime_tz` |
| `argparse` | `ArgumentParser` with Python's help and errors, and subcommands; the parsed arguments are typed from `add_argument()` (`args.count` is an `int`, a misspelled `args.cuont` is a compile error, and inside `if args.command == "add":` the add subcommand's arguments have their real types) |
| `dataclasses`, `functools` | `@dataclass` (with `frozen=True`, checked when compiling), `field()`, `replace()`; `@cache`, `@lru_cache`, `partial` (also as a thread's or a pool's work), `reduce`, `cmp_to_key`, `@total_ordering`, `@cached_property`, `@wraps` |
| `copy` | `copy` and `deepcopy` of lists, dicts, sets, deques, tuples and class instances (a deep copy keeps objects that appear twice shared and follows cycles, as Python's memo does), a class's `__copy__`, and `replace()`; copying a lock, file or socket is a compile error. Not yet: `__deepcopy__`, `deepcopy`'s `memo` argument |
| `contextlib` | `@contextmanager` (functions and methods; an exception in the with block is raised at the `yield`, so `try`/`except`/`finally` around it work as in Python, with Python's "generator didn't yield"/"didn't stop" errors), `suppress`, `closing`, `nullcontext`, `ExitStack` (`enter_context` of anything a `with` takes, `callback`, `pop_all`, `close`); `AbstractContextManager[T]` annotates what they make |
| `enum` | `Enum`, `IntEnum`, `StrEnum`, `Flag`, `IntFlag`, `auto()`, `@unique` (checked when compiling): members as class attributes (`Color.RED`, `.name`, `.value`), aliases, lookup by value or name (`Color(1)`, `Color["RED"]`) with Python's errors, `for c in Color`, `list(Color)`, `len(Color)`, methods and properties, and Python's repr, str and format. Flags combine with `\|`, `&`, `^`, `~` and `in`. `match` on members counts as covering everything once every member has a case. Members are immutable values, so threads share them and `@value` classes can hold them |
| `typing` | `Callable`, `Optional`, `Iterator`... (so the same code also runs under Python) |


## Differences from Python

seadash borrows Python's syntax, not all of its semantics:

- **Threads get copies.** A list, dict or set passed to a thread, put on a queue, returned from a `Future` or stored in a `Mutex` or `Synchronized` object is copied (all the way down, keeping lists that appear twice shared, like `copy.deepcopy`), so changes made on one side aren't seen on the other. A closure run on a thread gets copies of the enclosing function's variables, made when the thread is created, so `Thread(target=lambda: print(i))` in a loop prints each `i`. Share with a `threading.Mutex` or send results back through a `queue.Queue`. Inside `with m as data:`, `data` can't escape the block.
- **`@value` classes are values** (`from seadash import value`, not in Python): assigning or passing one copies it, lists and all. Their fields must be values too (no ordinary classes, locks, files or functions inside). A value class's list is its own: `s.items.append(x)` and `fill(s.items)` change it, but `xs = s.items`, `return self.items` or storing it elsewhere gives a copy. Changing a copy and never using it is a compile error (`for p in points: p.x += 1` changes copies; loop over the indexes, or write the copy back).
- **Sets print in a fixed order**: numbers and strings sorted, other items (objects with `__eq__` and `__hash__`) in the order they were added. Python's order depends on hashes. `s.pop()` takes the first item in that order.
- **Frozen dataclasses are checked when compiling**: changing a field is a compile error rather than a `FrozenInstanceError`, and a frozen `@value` class is frozen all the way down (its lists can't change either). A frozen class whose fields can't change either is shared between threads without copying.
- **Static types.** Containers hold one type (`list[int]`, not a mix), and there's no dynamic typing or `eval`. Mixed numbers widen, so `[1, 2.5]` is a `list[float]`.
- **Class attributes are constants.** `version = "1.0"` in a class body is read as `self.version` or `Cls.version` and can be redefined by a subclass, but not changed at run time: `self.version = ...` is a compile error (Python would give that one object its own `version`). For something that changes, use a field.
- **Ints are 64 bits.** Arithmetic that overflows wraps around (Python's ints grow instead), and `int(text)` or `int.from_bytes()` with a value that doesn't fit is an `OverflowError`.
- **Floats are IEEE doubles.** Arithmetic is done as written and gives the same result on every platform. It usually agrees with Python but isn't promised to, to the last digit or in the corners: `1.0 // 0.1` is the floor of the quotient, `10.0`, where Python works from the remainder and gives `9.0`; and `sum()` adds floats left to right where Python (3.12 and later) compensates for rounding, so `sum([0.1] * 10)` is `0.9999999999999999` rather than `1.0`.
- **Generators** don't support `send()`/`throw()`, can't `yield` inside an `except` or `finally` block, and nested functions can't be generators yet. (Generators, generator expressions, `map`, `filter`, `zip` and `enumerate` are all lazy, as in Python.)
- **Strings are UTF-8 bytes.** Indexes, lengths and positions (`find`, `index`) count bytes; widths (`ljust`, `center`, `zfill`, `expandtabs`, format specs) count characters. Case and classification (`upper`, `swapcase`, `casefold`, `isdecimal`, `istitle`...) follow ASCII rules. `encode`, `decode`, `str(data, encoding)` and `bytes(text, encoding)` know UTF-8, ASCII and Latin-1.
- **Format specs** (`f"{x:>10,.2f}"`, `format(x, spec)`, `"{:>10}".format(x)`) work on ints, floats, bools, strings and dates (a `strftime` format), and a spec that doesn't suit the value is a compile error. For other values, including `json.Value`, convert first: `{str(x):>10}`. A literal format string is checked when compiling (missing arguments, bad fields); one made at run time can't look up attributes or items (`{0.name}`, `{0[1]}`).
- **`sqlite3` rows are typed by the program.** `fetchone`/`fetchmany`/`fetchall` take their row type from the annotation (`row: tuple[int, str | None] | None = cur.fetchone()`), and a column that doesn't fit is a `TypeError` at run time; a column declared `bool` comes back as a bool, where Python gives 0/1.
- **`tomllib` gives typed values.** Without an annotation, `tomllib.loads` gives a `dict[str, json.Value]` (Python: `dict[str, Any]`); take its values apart with `match`, indexing and `.as_int()`/`.as_date()`... (class patterns like `case date():` don't work on a `json.Value` yet). An integer outside 64 bits is a `TOMLDecodeError` ("Integer out of range"), as the TOML spec allows, where Python reads a big int. Error positions (`e.pos`, the column) count characters, as Python's do.
- **`io.StringIO` positions count bytes**, like seadash's strings: `tell()`, `seek()`, `read(n)` and the count `write()` returns (Python's count characters, so they differ for non-ASCII text). A `StringIO`/`BytesIO` has no `name` or `mode` (a compile error, where Python raises `AttributeError`), and `fileno()` raises `OSError` (Python's `io.UnsupportedOperation` is both an `OSError` and a `ValueError`). Like any file, one can't be copied with `copy`, passed to another thread or kept in a `Mutex`.
- **`csv.DictReader` rows are `dict[str, str]`.** A short row's missing fields get `restval` (`""` by default) rather than `None`, and extra fields are dropped.
- **`http.server`**: a handler's `log_message(format, *args)` gets its arguments as strings (`*args: str`), the `Server:` header names seadash rather than Python, handlers have no `self.server` yet, and an exception in a handler is reported without a traceback. `ThreadingHTTPServer.server_close()` waits for requests still being handled (Python's doesn't). The listening socket's backlog is the system's maximum rather than Python's 5, since seadash's clients and handlers really run in parallel. `SimpleHTTPRequestHandler` doesn't have `extensions_map` to change. No HTTPS serving.
- **HTTP**: `URLError.reason` is always a `str`. `ssl` covers what HTTPS clients need (no server side yet), and its error messages lack Python's `(_ssl.c:1000)` suffix. `HTTPConnection.request` doesn't take a file or iterable body, or `encode_chunked`. After a `str` body fails to encode, Python sends the half-built request's headers along with the next request; seadash sends nothing.
- **`match` checks exhaustiveness only simply.** A match counts as covering every value when a case is a plain capture or `_`, or `case None:` plus a pattern covering the rest of an optional, or a class pattern covering the subject's type, or cases naming every member of an enum. Otherwise the checker assumes no case might match (so a function may need a final `return`). An `int(n) | float(n)` capture is a `float` whichever matches.
- **`mimetypes` uses Python's built-in table only**, not the system's mime.types files (`/etc/mime.types`...), which Python also reads. So an extension only the system knows gets `(None, None)`, and a few guesses differ where the system file overrides Python's default (with `/etc/mime.types`, Python gives `audio/sp-midi` for `.mid`; seadash gives `audio/midi` with `strict=False`, like `mimetypes.MimeTypes()` does).
- **`functools.partial`** isn't for built-in or library functions yet, nor functions with `*args`; there's no `.func`/`.args`/`.keywords`, and a parameter it binds by keyword can't be given again when it's called. As a thread's work it holds copies, like `args=`.
- **Function values** keep the names and defaults of the function they're known to be (`g = greet`, `obj.method`, a lambda, a `partial`), so calls through them take keywords; a function parameter, a list's items or a field are plain function types, called positionally with every argument.
- **`fnmatch` and `glob` take `str` only**, not bytes patterns or names, and `glob.translate`'s `seps=` is a str (Python also takes a list).
- **`copy`**: copying what Python can't copy (a lock, file or socket, or, for `deepcopy`, anything holding one) is a compile error rather than a `TypeError` at run time. So is copying an `Event`, `Queue`, `Mutex` or another threading object, which Python's `copy.copy` turns into a new object sharing the original's insides. `copy.copy` of a `@value` class copies its lists too, since it's a value.
- **`statistics` results are floats.** `mean`, `median`, `variance`, `harmonic_mean`... of ints give a float, so `mean([1, 2, 3])` prints `2.0` where Python, which computes in fractions and converts back to the data's type when the result is whole, prints `2`. (`median_low`, `median_high`, `mode` and `multimode` give the data's own items, as in Python.) The data must be ints or floats (no `Fraction` or `Decimal`), and mean and variance are computed in doubles with exactly rounded sums rather than in fractions, so they can differ from Python's in the last digit. A `LinearRegression` has `.slope` and `.intercept` but can't be unpacked like a tuple. `NormalDist.samples(seed=)` takes an int seed, and a `NormalDist` isn't hashable yet.
- **`contextlib`**: a `@contextmanager` function that always yields twice is a compile error, and it can't use `yield from` or other decorators. A with block whose context manager might swallow an exception (`suppress()`, a `@contextmanager` that catches what's raised at its yield, an `ExitStack` holding one) counts as possibly ending early, like one whose `__exit__` returns a bool: a function may need a `return` after it. `ExitStack.callback` takes a function value (a `def` or lambda, not `print` or `xs.append`) and no keywords; `ExitStack` takes classes with seadash's `__exit__` (not Python's three-argument one), and has no `push`. `AbstractContextManager[T]` only covers what contextlib makes. Not yet: `chdir`, `redirect_stdout`, `ContextDecorator`, the async ones.
- **Enums are fixed when compiling.** A member's value must be a constant (a literal, a tuple of them, or ints combined with operators and earlier members, `RW = R | W`), and all of an enum's values have one type. Not supported: `__init__`/`__new__` on an enum, `_missing_` and the other `_sunder_` hooks, `__members__`, the functional API (`Enum("Color", "RED GREEN")`), other mixins, inheriting from an enum, and `print(Color)` itself. An `IntEnum`/`StrEnum` member is an int/str wherever one is expected (arithmetic, comparisons, arguments, str methods), but a list of them isn't a `list[int]` (so `sum(Level)` or `"-".join(Mode)` need `.value`). A plain `Enum` member compared with another type (`Color.RED == 1`) is a compile error rather than `False`, and so is `isinstance(member, Color)`. A flag's `.name` is a `str \| None`. `match` exhaustiveness counts members, not flag combinations.
- **`f(*xs)` with a list** fills the parameters after the ones given, up to any passed by name, or goes to `*args`; its length is checked when it runs, with Python's `TypeError`. It can't fill parameters that have defaults, and of the builtins only `print`, `zip`, `itertools.chain`/`product` and `os.path.join` take one (a tuple can be unpacked into any call). `zip(*rows)` and `product(*lists)` give `tuple[T, ...]`s, as the number of rows isn't known when compiling, and read each row first.
- **`f(**d)`** takes the arguments it doesn't otherwise get from `d` when it runs (Python's `TypeError`s for a missing, unexpected or repeated one), but only parameters whose type `d`'s values fit, one `**` per call, and not into builtins. A `**kwargs` parameter has one value type, like any dict.
- **Augmented assignment to a slice** (`xs[1:3] += ys`) isn't supported; write `xs[1:3] = xs[1:3] + ys`.
- **`del`** works on names, items, slices and keys, as in Python, but a function can't `del` a global or `nonlocal` variable, nor one a nested function or lambda uses, and functions can't read a module-level name that's deleted. Reading a deleted name is a compile error rather than a `NameError`.
- **Signal handlers run on a thread of their own**, since there's no interpreter loop to stop the main thread between statements. So a handler is checked like a thread's code (to tell the main thread to stop, set a `threading.Event` rather than a global), and it runs alongside the main thread rather than interrupting it. `raise_signal()` and `os.kill()` of the program's own process still wait for the handler, as in Python. Nothing can be raised in the main thread: an exception or `sys.exit()` escaping a handler ends the program at once (no `finally` blocks, and other threads aren't waited for), and there's no `KeyboardInterrupt`: Ctrl-C ends the program, as SIGINT's default action does. A handler's `frame` is always `None`, and a handler may itself call `signal.signal()`. `getsignal(SIGPIPE)` is `SIG_DFL` (Python ignores SIGPIPE when it starts). `valid_signals()` is a `set[int]`. Not yet: `setitimer`, `siginterrupt`, `set_wakeup_fd`, `sigwait`, `pthread_sigmask`, `pthread_kill`, the `Handlers` enum, and calling a handler that `getsignal()` gave.
- **No tracebacks.** An uncaught exception (or `logging.exception()`) shows the exception's type and message, but not the stack of calls that led to it.
- **Not supported yet:** a few dynamic features, such as `type(x)`, `getattr`, unions other than `T | None`, and multiple inheritance. The full list is in `docs/roadmap.md`.

## How it works

```
source.sd → lexer → parser → type checker → C++23 codegen → g++ → native binary
                                   │
                      (flow-sensitive: narrowing, rebinding,
                       None-safety, thread-safety analysis)
```

| path | what's there |
|---|---|
| `seadash/lexer.py`, `parser.py`, `ast.py` | Python-style syntax, `INDENT`/`DEDENT` tokens |
| `seadash/checker.py`, `types.py`, `builtins.py` | type inference and checking; built-in functions and modules |
| `seadash/threads.py` | which values threads may share |
| `seadash/codegen.py` | C++ generation |
| `seadash/runtime/` | the header-only runtime (`seadash.hpp`, plus `modules/*.hpp`) |
| `tests/programs/` | end-to-end programs with expected output (most also run under `python3`) |
| `bench/` | benchmarks against CPython |

## Tests

```console
$ .venv/bin/python -m pytest -q tests/
$ .venv/bin/python bench/run.py
```

---

<sub>The name is a nod to two kids, and it sounds like *C-dash*.</sub>
