# Roadmap

What's planned next, and decisions that are made but not yet built. Finished work belongs
in the README and in the design docs, not here.

## Next: HTTP

1. **`http.client`** (done): `HTTPConnection`/`HTTPSConnection`, streaming `HTTPResponse`,
   an `ssl` module, and `urllib.request` rebuilt on them.
2. **`http.server`** (done): `HTTPServer`, `ThreadingHTTPServer`, `BaseHTTPRequestHandler`.
   HTTPS serving (done). Not yet: `self.server` in a handler, `socketserver` as a module.
3. **`SimpleHTTPRequestHandler`** (done), with `directory=` through `functools.partial`. Not
   yet: changing its `extensions_map`.

## Next: threads

1. **`seadash` module** (done): `Mutex`, `RWMutex`, `Atomic`, `Synchronized` come from
   `seadash`, with Python versions in `seadash/__init__.py`.
2. **Shared pool arguments** (done for `pool.submit` with a plain function and a variable):
   see `docs/values-and-references.md` ("Shared with pool tasks"). Next: `pool.map` (its
   items), lambdas and nested defs as tasks, and attributes or items as arguments.

## Not supported yet (known gaps)

- `http.client`: file or iterable request bodies, `encode_chunked`.
- `sqlite3`: `row_factory`/`sqlite3.Row`, `create_function`, iterating a cursor directly.
- `uuid`: `u.int` (128 bits), `getnode()`.
- `tarfile`: sparse files' contents, encodings other than UTF-8, `errorlevel=0`, Zstandard.
- `zipfile`: encrypted members (`pwd=`, `setpassword`), Zstandard members, `zipfile.Path`, `ZipInfo.from_file`, `PyZipFile`.
- `fnmatch`, `glob`: bytes patterns and names.
- `tomllib`: `parse_float=`; class patterns for dates and times on a `json.Value` (`case date():`).
- `io`: `readline(size)`, `readlines(hint)`, `getbuffer`, `read1`/`readinto`, `io.UnsupportedOperation`,
  `TextIOWrapper`; copying a `StringIO`; `gzip.GzipFile(fileobj=...)` over one.
- `select`: `epoll`, `kqueue`, `devpoll` (selectors use `poll()` everywhere); `socket.socketpair()`.
- `enum`: `__init__`/`__new__` on enums (members built from tuples), `_missing_` and other `_sunder_` hooks,
  `__members__`, the functional API, inheriting from a member-less enum, `print(Color)`; containers of
  `IntEnum`/`StrEnum` members used as containers of ints/strs.
- `math`: `fsum` (an exactly rounded sum; `sum()` adds floats left to right).
- `copy`: `__deepcopy__` and `deepcopy(x, memo)` (they need a type for the memo); copying exceptions.
- Built-in functions: `issubclass`, `frozenset`, `memoryview`, `complex`, `slice`; `getattr`/`hasattr`/`setattr`
  with a name chosen at run time, and calling `type(x)(...)`; `isinstance` on anything but class instances.
- Built-in types: printing a `range` or a dict view (`print(d.keys())`), keeping a `range` in a
  variable or indexing it, `float.as_integer_ratio` (the ratio often needs more than 64 bits),
  `int.real`/`imag`/`numerator`/`denominator`/`conjugate`, `tuple * n`, `f.encoding`, `%` formatting.
- Statements: augmented assignment to a slice (`xs[1:3] += ys`); unpacking a list into other builtins
  (`max(*xs)`; a tuple works), into parameters with defaults, or into zip/chain/product alongside other
  arguments (`zip(a, *rows)`); `**d` into builtins, or more than one `**d` in a call.
- Decorators on nested functions (other than `@functools.wraps`).
- `contextlib`: `chdir`, `redirect_stdout`/`redirect_stderr`, `ContextDecorator`, `ExitStack.push`, async
  context managers. Generators can't `yield` in an `except`/`finally` block (C++ can't suspend there).
- Functions: default values in nested functions and lambdas, and keywords or defaults when
  calling a function held in a parameter, field or container (its signature isn't known there).
- Tracebacks (debug builds): Python's `~~~^^^` markers.
- `signal`: `KeyboardInterrupt` and other exceptions raised in the main thread by a handler (handlers run
  on their own thread); `setitimer`, `siginterrupt`, `set_wakeup_fd`, `sigwait`, `pthread_sigmask`,
  `pthread_kill`, `Handlers`; signal names only one platform has (`SIGINFO`, `SIGRTMIN`...).

## Open questions

- **Keywords on built-in methods.** Some accept keywords Python doesn't (`"x".center(width=5)`), others
  take none at all (`xs.pop(index=0)`): the built-ins declare their parameters in three different ways.

## Decided against

- **Implied handles** (a file or socket field in a value class stored as its fd and rebuilt
  on access): value classes are for simple data; store the fd as an int and manage it.
  See `docs/values-and-references.md`.
