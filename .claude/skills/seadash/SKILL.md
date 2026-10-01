---
name: seadash
description: Write, run, and debug programs in seadash, a statically typed language with Python's syntax that compiles to C++23 (`.sd` files, the `sd` command). Use when writing or porting a `.sd` program, fixing a seadash compile error, deciding whether a Python feature or standard-library module works in seadash, sharing data between seadash threads (Mutex, RWMutex, Atomic, Synchronized), using `@value` classes, or reading the C++ that `sd emit` generates.
---

# Writing seadash programs

seadash looks like Python and is type-checked before it runs. Write ordinary, well-typed
Python 3.12. Most small programs compile unchanged. The rules below cover where seadash is
stricter or different.

The authoritative references, in this repository:
- `README.md`: "The language in a nutshell", the **Standard library** table (what each
  module supports), and **Differences from Python**.
- `docs/roadmap.md`: "Not supported yet" (the known gaps).
- `docs/values-and-references.md`: copying, sharing and threads in depth.
- `tests/programs/*.sd`: working examples of nearly every feature. Grep them for the module
  or construct you need before guessing.

## Commands

Run these from the repo root. The compiler lives in `.venv`.

```sh
.venv/bin/sd run prog.sd [args...]   # compile (cached) and run
.venv/bin/sd check prog.sd           # type-check only; prints every variable's inferred type
.venv/bin/sd emit prog.sd            # print the generated C++
.venv/bin/sd build prog.sd -o prog   # native binary (-O3); --debug builds faster
echo 'print(1 + 1)' | .venv/bin/sd run
```

Run `sd check` first: it's fast, and its type listing shows what the checker inferred. Compile
errors point at the line, say what's wrong and suggest a fix. Read the message before changing
code.

## Typing rules

- **Annotate function parameters and returns.** Local types are inferred. Annotate an empty
  container when its use doesn't decide its type: `names: list[str] = []`.
- **Containers hold one type.** `[1, "a"]` is an error, and `[1, 2.5]` widens to
  `list[float]`. Model a mix with a class, a tuple, or `json.Value`.
- **Optional is `T?` or `T | None`.** No other unions are allowed. Before using an optional,
  narrow it with `is not None`, `if x:`, a walrus (`if (m := re.match(...)) is not None:`),
  `isinstance`, or an early `return`/`continue`. Narrowing works on attributes too.
- **Rebinding to a new type is allowed**: `n = 2; n = str(n)`.
- **Classes have typed fields.** Declare fields in the class body (`name: str`). A class with
  fields and no `__init__` is constructed from its fields in order, as with `@dataclass`.
  Use `@dataclass` anyway when the file should also run under python3.
- **Class-body assignments are constants** (`version = "1.0"`). For state that changes, use
  a field.
- **Generics:** `def first[T](xs: list[T]) -> T | None:` and `class Stack[T]:`.
- **Ints are 64-bit and wrap on overflow.** Floats are IEEE doubles. Strings are UTF-8 bytes,
  so `len`, indexes and `find` count bytes.

## Values and references

- Lists, dicts, sets and ordinary class instances are **shared references**, as in Python.
- A `@value` class (`from seadash import value`) is **copied** on assignment and when passed,
  lists and all. Its fields must be values: no ordinary classes, locks, files or functions.
  `xs = p.items` gives a copy, while `p.items.append(x)` changes `p`.
- Changing a copy and then dropping it is a compile error. For example,
  `for p in points: p.x += 1` only changes copies. Loop over indexes instead
  (`points[i].x += 1`) or write the copy back.

## Threads (no GIL, checked for races)

Anything that goes into another thread is **copied** all the way down: `Thread` args, queue
items, `pool.submit` arguments and results, and the variables a thread's closure captures.
Use one of these to share:

```python
import threading
from seadash import Atomic, Mutex, RWMutex, Synchronized

hits = Atomic()                          # hits.add(), hits.get(), compare_and_set
log: Mutex[list[str]] = Mutex([])        # owns its data
with log as items:                       # holds the lock; `items` can't escape the block
    items.append("x")
cfg = RWMutex({"mode": "fast"})          # with cfg.read() as c: / with cfg.write() as c:

class Stats(Synchronized):               # every method holds the object's lock
    count: int
    def bump(self):
        self.count += 1
    def total(self) -> int:              # fields are only reachable through methods:
        return self.count                # `stats.count` from outside is an error
```

`threading.Lock`, `queue.Queue` and `concurrent.futures` work too. Sending results back
through a `queue.Queue` is often simplest. Threads may read module globals that nothing
changes. Writing to a global from a thread is an error, and the error message names the fix.
`Mutex(data)` moves `data` in, so using `data` afterwards is an error until it's reassigned.

## Types that come from what you write

- `json.loads` decodes into the annotated type: `users: list[User] = json.loads(text)`
  (dataclasses, lists, dicts, optionals). Use `json.Value` for JSON whose shape isn't known,
  and take it apart with `match`.
- `sqlite3` rows are typed by annotation: `rows: list[tuple[int, str]] = cur.fetchall()`.
- Literal arguments decide result types: `open(p, "rb")` reads bytes,
  `subprocess.run(..., capture_output=True, text=True).stdout` is a `str`,
  `struct.unpack("<hf", b)` is a `tuple[int, float]`, and `args.count` comes out typed from
  `add_argument(type=int)`.

## Not available (check `docs/roadmap.md` for the current list)

`del`, slice assignment, star-unpacking (`a, *rest = xs`, `f(*args)`, `[*a, *b]`, `{**d}`),
`**kwargs`, keyword-only and positional-only parameters, multiple inheritance, unions other
than `T | None`, `type(x)`, `getattr`/`hasattr`/`setattr`, `eval`, `frozenset`, `bytearray`,
`%` string formatting, arithmetic on bools, generator `send`/`throw`, and generators nested
inside functions. If you need one, restructure. For example, use `xs = xs[:i] + xs[j:]`
instead of `del xs[i:j]`, and f-strings instead of `%`.

## Workflow for a new program

1. If the program can be valid Python, keep it that way, so Python's output is a reference
   to compare against. `.venv/bin/python prog.sd` finds the `seadash` package (plain
   `python3` needs `PYTHONPATH=.`), which has Python versions of `value`, `Mutex` and the rest.
2. `sd check`, fix the errors, then `sd run`.
3. Output that differs from Python is often expected. Check README "Differences from
   Python" before treating it as a bug: set order, float corner cases, thread copies, and
   no tracebacks are all expected differences.
4. If a seadash bug is real (a crash in generated C++, or wrong output that isn't a listed
   difference), reduce it to a few lines and look at `sd emit`. Fixing it is compiler work:
   follow `CLAUDE.md`, which covers the layout, tests and commit rules.
