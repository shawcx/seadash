# seadash

A language with Python's syntax that compiles to C++23: "C++ with the simplicity of Python".
The compiler is written in Python; `sd run file.sd` translates, compiles with g++-14 and runs.
The name is final (a nod to the author's two kids).

## Design (settled; ask before changing)

- **Python's spelling first.** New features follow Python's syntax and behaviour unless
  there's a good reason not to. seadash may have its own idioms, but diverging from Python
  is a decision for the user.
- **Static types, inferred.** Names can be rebound to a new type (`a = 2; a = str(a)`): each
  (name, type) gets its own C++ variable. `T?` / `T | None` is optional, and the checker
  narrows it (also on attributes, `isinstance`, `args.command == "add"`...). A function's
  unannotated parameters take the types they're called with (`def f(x)` is `def f[T](x: T)`,
  compiled per call like a C++ template) and its return type then comes from its body; a
  parameter with a default takes the default's type. Methods still need annotations.
- **A `@value` class is a value, any other class a shared reference** (`std::shared_ptr`).
  `@value` comes from `from seadash import value` (it was a `struct` keyword, which broke
  `import struct`); internally its kind is still "struct" and the C++ is a plain struct.
  Lists, dicts and sets are shared references like Python's (`sd::list` is a handle to a
  vector); a value class copies its lists with it, its fields must be values, and changing
  a copy then dropping it is an error (`flow.py` has the statement-level analyses).
  Anything crossing into another thread is copied all the way down (`sd::value_copy`).
  The design is in `docs/values-and-references.md`.
- **Closures share captured variables** like Python (cells).
- **Threads without a GIL**, made safe by the checker (`threads.py`): threads receive copies
  or thread-safe objects (Lock, Queue, Mutex[T], RWMutex[T], Atomic, Synchronized classes); unsafe
  sharing is a compile error with a suggested fix. What isn't Python's comes from the `seadash`
  module (`from seadash import value, Mutex`), never from a standard-library one, and
  `seadash/__init__.py` has Python versions of it so such programs also run under python3.
- **Typed JSON**: `json.loads` decodes into the expected type; `json.Value` for any JSON.
- **Literal arguments decide types** where Python's result depends on them: `open(p, "rb")`,
  `subprocess.run(..., text=True)`, `permutations(xs, 2)`, `add_argument(type=int)`.
- **Floats are IEEE doubles**, evaluated as written, with the same result on every platform
  (hence `-ffp-contract=off`, and never `-ffast-math`). Float results don't need to match
  Python's, in operators or in the library: `1.0 // 0.1` is the floor of the quotient, `10.0`
  (Python works from the remainder and gives `9.0`), and `sum()` isn't compensated. Don't
  change float behaviour to agree with Python, and keep such cases out of the
  Python-comparison tests.
- "Fast development" means fast to write code; a build step is fine.

## Layout

- `seadash/lexer.py` → `parser.py` (`ast.py`) → `checker.py` (types in `types.py`, builtins
  and standard-library typing in `builtins.py`, thread safety in `threads.py`) →
  `codegen.py` → C++. `driver.py` runs the pipeline and the C++ compiler (with the build
  cache); `cli.py` is the `sd` command.
- `seadash/runtime/seadash.hpp` is the core runtime; `seadash/runtime/modules/*.hpp` are
  standard-library modules (a module declares its header and libraries in `builtins.py`).
- A standard-library class without type parameters (`socket`, `Logger`, `date`...) is a
  `BuiltinClass` in `types.py`, with its C++ type and thread rule; `builtins.py` adds its
  `methods` and `attributes` next to its module. The checker, `threads.py` and codegen look
  those up, so only a class that needs special code generation gets a case of its own.
- `tests/programs/*.sd` are end-to-end tests: `.out` is the expected stdout (and optional
  `.err` / `.exit`). They're valid Python that python3 gives the same output for
  (`tests/test_python_parity.py` checks; `NEEDS_PYTHON` there lists the version-dependent ones).
  Programs that use seadash's own syntax, types or behaviour go in `tests/programs/seadash/`.
  `tests/test_*.py` are unit tests; checker error messages are tested in `tests/test_checker.py`.
- `bench/*.sd` run unchanged under python3 and seadash.

## Commands

```sh
.venv/bin/python -m pytest -q tests/          # the tests (~25s; less once the build cache is warm)
.venv/bin/python tools/check.py               # all the pre-commit checks (~3 min): tests, warnings, ASan, TSan
.venv/bin/python tools/check.py -k http asan  # some checks, on the programs whose names contain "http"
.venv/bin/sd run file.sd                      # also: build, check, emit (the C++), clean (the cache)
.venv/bin/python bench/run.py [name]          # benchmarks vs python3, checks identical output
tools/linux/check.sh [--sanitize]             # on macOS: the same checks on Linux/g++-14 (Docker)
```

Requires g++-14 on Linux or Apple clang on macOS, and the zlib, PCRE2 and OpenSSL dev
headers (Homebrew's `pcre2` and `openssl@3` on macOS).

## How work is done here

- **One feature per commit**, and commit only when the user says so (they usually say
  "commit this and start X"). Commit messages end with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Every feature gets a test program that is valid Python** where possible, with `.out`
  generated by running it with `python3`; seadash's output must match exactly. Where
  seadash intentionally differs, keep that out of the Python-comparison test and cover it
  separately (a checker test, or a seadash-only program with hand-checked output).
- Match Python's exact output: `repr`s, error messages, formatting, edge cases. Probe real
  Python first (`python3 -c ...`) rather than guessing. For intricate algorithms (textwrap),
  fuzz against Python.
- Checker errors should say what's wrong and how to fix it; add them to `test_checker.py`.
- Before committing a feature, run `tools/check.py`: the full test suite passes, the generated
  C++ compiles cleanly with `-Wall -Wextra`, every test program runs clean under
  `-fsanitize=address,undefined`, and the thread programs under `-fsanitize=thread`.
  (By hand, ThreadSanitizer needs `setarch $(uname -m) -R ./binary` on Linux.)
  - Compile emitted C++ with
    `g++-14 -std=c++23 -fwrapv -ffp-contract=off -Iseadash/runtime file.cpp [-l...]`.
    On macOS it's `clang++`, plus `-I/opt/homebrew/include -L/opt/homebrew/lib` for the
    libraries, and ThreadSanitizer needs no `setarch`.
- Keep the README's standard-library table and "Differences from Python" section current.
- When reporting results, say what differs from Python and what isn't supported yet.

## Working with the user

- Build step by step together: discuss a design before coding it, and give a recommendation
  rather than a survey of options. "Go with your recommendations" means proceed.
- The user loves Python's ergonomics and dislikes C++'s template syntax, `std::` verbosity and
  cast spellings: seadash code should never need them.
- Terminology: "structs" means seadash's `@value` classes; "python struct" means Python's
  `struct` module.
- Another Claude session may be pushing to the same remote (usually new standard-library
  modules). "Rebase" means: `git stash push -u`, `git fetch`, `git rebase origin/main`,
  `git stash pop`, then run the tests. Push only when asked.
- Plans and open questions are in `docs/roadmap.md`; keep it current as work finishes.

## Gotchas

- Coroutines (generators, lazy builtins, itertools) outlive the call that made them: pass
  what they need by value, never capture by reference. Generator methods take their object
  at the call (`generator_method` in codegen).
- `sd::list`/`dict`/`set`/`deque` are handles: `out = a; out.push_back(x)` changes `a`
  too. Use `a.copy()` / `sd::shallow_copy` for a new container and `sd::value_copy` for one
  sharing nothing (value-class fields, thread boundaries). A moved-from handle is a fresh,
  empty container. Runtime code may use `.vec()` (the `std::vector`) with std algorithms.
- Arguments that only instruct the compiler (`type=int`, `digestmod=hashlib.sha256`) are
  marked `node.notes["compile_time"]` so codegen doesn't evaluate them as values.
- What the checker works out for codegen beyond a node's `sym` and `ty` goes in `node.notes[name]`
  (a call's bound arguments, `partial`, `lent`...), never in an undeclared attribute.
- C library names that are macros (`stdout`, `st_mtime`...) are renamed by `codegen.ident`.
- File objects over callbacks (`gzip.open`, `bz2.open`, `lzma.open`: `modules/cookie_file.hpp`)
  can't throw through C stdio: a callback parks its exception in `sd::pending_file_error`
  and fails the read; `FileBase::check_error` rethrows it.
- Linux and macOS are both supported, and only one is at hand at a time. libc++ doesn't
  include headers transitively the way libstdc++ does (include what you use), lacks the
  `<chrono>` time zone database (`modules/tzif.hpp` stands in), and some libc calls are
  Linux-only (`sigtimedwait`, `st_mtim`...). Test output must not depend on the platform:
  errno numbers, `/tmp` (a symlink on macOS), the last digit of libm results.
- Builds are cached in `~/.cache/seadash` (or `$SEADASH_CACHE_DIR`); `sd clean` empties it,
  `--no-cache` bypasses it. The benchmark always builds with `--no-cache`.
