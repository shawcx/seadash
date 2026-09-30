# Roadmap

What's planned next, and decisions that are made but not yet built. Finished work belongs
in the README and in the design docs, not here.

## Next: HTTP

1. **`http.client`** (done): `HTTPConnection`/`HTTPSConnection`, streaming `HTTPResponse`,
   a client-side `ssl` module, and `urllib.request` rebuilt on them.
2. **`http.server`**: `HTTPServer`, `ThreadingHTTPServer`, `BaseHTTPRequestHandler`
   (`do_GET`... methods on a user subclass, `send_response`/`send_header`/`end_headers`,
   `wfile`/`rfile`, `log_message`). A threading server runs handlers on several threads, so
   the handler class and whatever it touches must pass the thread checker.
3. **`SimpleHTTPRequestHandler`**, which needs `mimetypes`, `html.escape` and
   `email.utils.formatdate`.

Server-side TLS (`SSLContext.load_cert_chain`, `wrap_socket(server_side=True)`) would also
make HTTPS testable locally; today HTTPS is only checked by hand against public hosts.

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
- `math`: `fsum` (an exactly rounded sum; `sum()` adds floats left to right).
- No tracebacks for uncaught exceptions.

## Decided against

- **Implied handles** (a file or socket field in a value class stored as its fd and rebuilt
  on access): value classes are for simple data; store the fd as an int and manage it.
  See `docs/values-and-references.md`.
