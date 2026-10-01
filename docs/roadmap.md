# Roadmap

What's planned next, and decisions that are made but not yet built. Finished work belongs
in the README and in the design docs, not here.

## Next: HTTP

1. **`http.client`** (done): `HTTPConnection`/`HTTPSConnection`, streaming `HTTPResponse`,
   a client-side `ssl` module, and `urllib.request` rebuilt on them.
2. **`http.server`** (done): `HTTPServer`, `ThreadingHTTPServer`, `BaseHTTPRequestHandler`.
   Not yet: `self.server` in a handler, `socketserver` as a module, HTTPS serving.
3. **`SimpleHTTPRequestHandler`** (done), with `directory=` through `functools.partial`. Not
   yet: changing its `extensions_map`.

Server-side TLS (`SSLContext.load_cert_chain`, `wrap_socket(server_side=True)`) would also
make HTTPS testable locally; today HTTPS is only checked by hand against public hosts.

## Next: threads

1. **`seadash` module** (done): `Mutex`, `RWMutex`, `Atomic`, `Synchronized` come from
   `seadash`, with Python versions in `seadash/__init__.py`.
2. **Shared pool arguments** (done for `pool.submit` with a plain function and a variable):
   see `docs/values-and-references.md` ("Shared with pool tasks"). Next: `pool.map` (its
   items), lambdas and nested defs as tasks, and attributes or items as arguments.

## Decided, not built

- **Passing an `HTTPConnection` to a thread** (it's currently a compile error). Recommended:
  a copy is a new connection to the same host/port/timeout/context that connects on first
  use (a `sd::value_copy` overload; `threads.py` makes it sendable; `SSLContext::native()`
  needs a mutex because the copies share the context). Later, for connection pools, moving
  a connection through a `Queue` could keep its socket if its last response was fully read.
  Responses stay unsendable. Waiting for the user's go-ahead.

## Not supported yet (known gaps)

- `http.client`: file or iterable request bodies, `encode_chunked`.
- `sqlite3`: `row_factory`/`sqlite3.Row`, `create_function`, iterating a cursor directly.
- `uuid`: `u.int` (128 bits), `getnode()`.
- `fnmatch`, `glob`: bytes patterns and names.
- `enum`: `__init__`/`__new__` on enums (members built from tuples), `_missing_` and other `_sunder_` hooks,
  `__members__`, the functional API, inheriting from a member-less enum, `print(Color)`; containers of
  `IntEnum`/`StrEnum` members used as containers of ints/strs.
- `math`: `fsum` (an exactly rounded sum; `sum()` adds floats left to right).
- `copy`: `__deepcopy__` and `deepcopy(x, memo)` (they need a type for the memo); copying exceptions.
- Built-in functions: `callable`, `id`, `issubclass`, `type`, `getattr`/`hasattr`/`setattr`, `frozenset`,
  `bytearray`, `memoryview`, `complex`, `slice`; `isinstance` on anything but class instances.
- Built-in types: printing a `range` or a dict view (`print(d.keys())`), keeping a `range` in a
  variable or indexing it, `float.as_integer_ratio` (the ratio often needs more than 64 bits),
  `int.real`/`imag`/`numerator`/`denominator`/`conjugate`, `tuple * n`, `f.encoding`, `%` formatting.
- Statements: augmented assignment to a slice (`xs[1:3] += ys`); unpacking arguments (`f(*args)`, `f(**kwargs)`).
- Arithmetic on bools (`(a > b) - (a < b)`, `True + 1`, `sum([True, False])`).
- Decorators on nested functions (other than `@functools.wraps`).
- `contextlib`: `chdir`, `redirect_stdout`/`redirect_stderr`, `ContextDecorator`, `ExitStack.push`, async
  context managers. Generators can't `yield` in an `except`/`finally` block (C++ can't suspend there).
- Functions: keyword-only and positional-only parameters (`*`, `/`), `**kwargs`, default
  values in nested functions and lambdas, and keywords or defaults when calling a function held in
  a variable.
- No tracebacks for uncaught exceptions.

## Open questions

- **Function values that keep their signature** (decided as the next step for functions, not
  built): when the compiler knows which function a value is (a `def`, a bound method, a
  `partial`), its type keeps parameter names and defaults, so calls through it can use keywords
  and leave arguments out, as in Python. That lifts `partial`'s limits too.

- **Mutable default arguments.** `def f(xs: list[int] = [])` makes a new list on every call; Python
  makes one when the function is defined and shares it between calls. Match Python, or keep this and
  list it in the README's differences?
- **Keywords on built-in methods.** Some accept keywords Python doesn't (`"x".center(width=5)`), others
  take none at all (`xs.pop(index=0)`): the built-ins declare their parameters in three different ways.
- **`uuid.UUID` and threads.** A UUID can't be passed to a thread or kept in a `@value` class, though
  it's an immutable value; marking it `IMMUTABLE` in `types.py` would allow both.

## Decided against

- **Implied handles** (a file or socket field in a value class stored as its fd and rebuilt
  on access): value classes are for simple data; store the fd as an int and manage it.
  See `docs/values-and-references.md`.
