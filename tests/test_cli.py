"""The `sd` command: reading programs from stdin, running with arguments."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).parent.parent


def sd(args: list[str], stdin: str, cwd, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "seadash.cli", *args], input=stdin, capture_output=True, text=True, cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
    )


def test_run_reads_stdin(tmp_path):
    (tmp_path / "helpers.sd").write_text(textwrap.dedent("""
        def shout(f: Callable[[str], str]) -> Callable[[str], str]:
            return lambda s: f(s).upper()

        @shout
        def greet(name: str) -> str:
            return "hi " + name
    """))
    program = "import sys\nimport helpers\nprint(helpers.greet('stdin'), sys.argv[1:])\n"
    r = sd(["run"], program, tmp_path)  # imports are found in the current directory
    assert (r.returncode, r.stdout, r.stderr) == (0, "HI STDIN []\n", "")
    r = sd(["run", "-", "a", "b"], program, tmp_path)
    assert r.stdout == "HI STDIN ['a', 'b']\n"


def test_stdin_errors_name_stdin(tmp_path):
    r = sd(["run"], "x = 1\nx.foo()\n", tmp_path)
    assert r.returncode == 1
    assert r.stderr.startswith("<stdin>:2:3: error: int has no method 'foo'")
    r = sd(["check", "-"], "print(1)\n", tmp_path)
    assert r.returncode == 0 and r.stderr == "<stdin>: ok\n"


def test_package_data_covers_the_runtime():
    # A wheel ships only what pyproject.toml lists; without the runtime headers nothing builds.
    import tomllib
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["seadash"]
    package = ROOT / "seadash"
    shipped = {path for pattern in patterns for path in package.glob(pattern)}
    assert {path for path in (package / "runtime").rglob("*") if path.is_file()} <= shipped


def test_formatdate_localtime_follows_tz(tmp_path):
    """formatdate(localtime=True) uses the local zone (kept out of tests/programs, which can't set TZ).
    The expected lines are python3's, with the same TZ."""
    program = "from email.utils import formatdate\nprint(formatdate(1700000000, localtime=True), formatdate(1720000000, True, True))\n"
    expected = {
        "IST-5:30": "Wed, 15 Nov 2023 03:43:20 +0530 Wed, 03 Jul 2024 15:16:40 +0530\n",
        "EST5EDT,M3.2.0,M11.1.0": "Tue, 14 Nov 2023 17:13:20 -0500 Wed, 03 Jul 2024 05:46:40 -0400\n",
        "UTC0": "Tue, 14 Nov 2023 22:13:20 +0000 Wed, 03 Jul 2024 09:46:40 +0000\n",
    }
    for tz, out in expected.items():
        r = sd(["run"], program, tmp_path, env={"TZ": tz})
        assert (r.returncode, r.stdout, r.stderr) == (0, out, "")


def test_a_closed_pipe_is_a_broken_pipe_error(tmp_path):
    # As in Python: SIGPIPE is ignored, writing to a closed pipe raises BrokenPipeError, and output
    # that can't be flushed at exit is reported, with exit code 120. Children get SIGPIPE back.
    (tmp_path / "many.sd").write_text("for i in range(500000):\n    print(i)\n")
    r = sd(["build", "many.sd", "-o", "many"], "", tmp_path)
    assert r.returncode == 0, r.stderr
    p = subprocess.Popen([str(tmp_path / "many")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert p.stdout.readline() == b"0\n"
    p.stdout.close()
    err = p.stderr.read().decode()
    # Python's own ending differs by platform: on macOS what it couldn't write fails again at exit
    # (reported, exit code 120); on Linux it's dropped (exit code 1). seadash's stdio does the same.
    if sys.platform == "darwin":
        assert p.wait() == 120
        assert err == ("BrokenPipeError: [Errno 32] Broken pipe\n"
                       "Exception ignored while flushing sys.stdout:\nBrokenPipeError: [Errno 32] Broken pipe\n")
    else:
        assert p.wait() == 1
        assert err == "BrokenPipeError: [Errno 32] Broken pipe\n"
    (tmp_path / "child.sd").write_text(  # (`yes` ignoring SIGPIPE would say "yes: stdout: Broken pipe")
        "import subprocess\n"
        "r = subprocess.run(['sh', '-c', 'yes | head -1'], capture_output=True, text=True)\n"
        "print(repr(r.stdout), repr(r.stderr))\n")
    r = sd(["run", "child.sd"], "", tmp_path)
    assert r.stdout == "'y\\n' ''\n"



TRACEBACK_MAIN = """\
import logging
import threading

import helpers


class Store:
    def get(self, key: str) -> int:
        def find(k: str) -> int:
            return helpers.lookup({"a": 1}, k)
        return find(key)


def worker() -> None:
    Store().get("from thread")


logging.basicConfig(format="%(levelname)s %(message)s")
try:
    Store().get("logged")
except KeyError:
    logging.exception("lookup failed")
t = threading.Thread(target=worker)
t.start()
t.join()
try:
    Store().get("again")
except KeyError as e:
    raise e
"""
TRACEBACK_HELPERS = """\
def lookup(table: dict[str, int], key: str) -> int:
    return table[key]
"""
# What Python prints (but for the source markers like ~~~^^^ under a line, and the frames of
# Python's own threading.py in a thread's traceback).
TRACEBACK_EXPECTED = """\
ERROR lookup failed
Traceback (most recent call last):
  File "DIR/main.sd", line 20, in <module>
    Store().get("logged")
  File "DIR/main.sd", line 11, in get
    return find(key)
  File "DIR/main.sd", line 10, in find
    return helpers.lookup({"a": 1}, k)
  File "DIR/helpers.sd", line 2, in lookup
    return table[key]
KeyError: 'logged'
Exception in thread Thread-1 (worker):
Traceback (most recent call last):
  File "DIR/main.sd", line 15, in worker
    Store().get("from thread")
  File "DIR/main.sd", line 11, in get
    return find(key)
  File "DIR/main.sd", line 10, in find
    return helpers.lookup({"a": 1}, k)
  File "DIR/helpers.sd", line 2, in lookup
    return table[key]
KeyError: 'from thread'
Traceback (most recent call last):
  File "DIR/main.sd", line 29, in <module>
    raise e
  File "DIR/main.sd", line 27, in <module>
    Store().get("again")
  File "DIR/main.sd", line 11, in get
    return find(key)
  File "DIR/main.sd", line 10, in find
    return helpers.lookup({"a": 1}, k)
  File "DIR/helpers.sd", line 2, in lookup
    return table[key]
KeyError: 'again'
"""


def test_debug_builds_print_tracebacks(tmp_path):
    (tmp_path / "main.sd").write_text(TRACEBACK_MAIN)
    (tmp_path / "helpers.sd").write_text(TRACEBACK_HELPERS)
    r = sd(["run", "--debug", "main.sd"], "", tmp_path)
    assert r.returncode == 1
    assert r.stderr.replace(str(tmp_path.resolve()) + "/", "DIR/") == TRACEBACK_EXPECTED
    r = sd(["run", "main.sd"], "", tmp_path)  # optimized: no tracebacks
    assert "Traceback" not in r.stderr and r.stderr.endswith("KeyError: 'again'\n")


# Generators (a paused one isn't on the stack), `yield from`, a @contextmanager, a generator
# method and lambdas each have their frame; a caught exception's traceback starts at the handler.
# Python's also shows contextlib's __exit__ frame, and says "division by zero".
TRACEBACK_GENERATORS = """\
import logging
from contextlib import contextmanager
from typing import Iterator


def numbers(n: int) -> Iterator[int]:
    for i in range(n):
        if i == 2:
            raise ValueError("two")
        yield i


def delegate() -> Iterator[int]:
    yield from numbers(5)


@contextmanager
def managed() -> Iterator[None]:
    yield
    raise KeyError("exit")


class Box:
    items: list[int]

    def __init__(self, items: list[int]) -> None:
        self.items = items

    def each(self) -> Iterator[int]:
        for x in self.items:
            yield 10 // x


def main() -> None:
    paused = numbers(5)
    print(next(paused))
    try:
        for x in delegate():
            print(x)
    except ValueError:
        logging.exception("delegate")
    try:
        with managed():
            print("body")
    except KeyError:
        logging.exception("managed")
    try:
        print(list(Box([5, 0]).each()))
    except ZeroDivisionError:
        logging.exception("method")
    counts = {"a": 1}
    print(sorted(["a"], key=lambda k: counts[k]))
    print(sorted(["a", "b"], key=lambda k: counts[k]))


main()
"""

TRACEBACK_GENERATORS_EXPECTED = """\
ERROR:root:delegate
Traceback (most recent call last):
  File "DIR/main.sd", line 38, in main
    for x in delegate():
  File "DIR/main.sd", line 14, in delegate
    yield from numbers(5)
  File "DIR/main.sd", line 9, in numbers
    raise ValueError("two")
ValueError: two
ERROR:root:managed
Traceback (most recent call last):
  File "DIR/main.sd", line 43, in main
    with managed():
  File "DIR/main.sd", line 20, in managed
    raise KeyError("exit")
KeyError: 'exit'
ERROR:root:method
Traceback (most recent call last):
  File "DIR/main.sd", line 48, in main
    print(list(Box([5, 0]).each()))
  File "DIR/main.sd", line 31, in each
    yield 10 // x
ZeroDivisionError: integer division or modulo by zero
Traceback (most recent call last):
  File "DIR/main.sd", line 56, in <module>
    main()
  File "DIR/main.sd", line 53, in main
    print(sorted(["a", "b"], key=lambda k: counts[k]))
  File "DIR/main.sd", line 53, in <lambda>
    print(sorted(["a", "b"], key=lambda k: counts[k]))
KeyError: 'b'
"""


def test_debug_tracebacks_show_generators_and_lambdas(tmp_path):
    (tmp_path / "main.sd").write_text(TRACEBACK_GENERATORS)
    r = sd(["run", "--debug", "main.sd"], "", tmp_path)
    assert r.returncode == 1
    assert r.stdout == "0\n0\n1\nbody\n['a']\n"
    assert r.stderr.replace(str(tmp_path.resolve()) + "/", "DIR/") == TRACEBACK_GENERATORS_EXPECTED
