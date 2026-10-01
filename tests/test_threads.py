"""Thread safety: unsafe sharing is a compile error; the safe patterns compile."""

import textwrap

import pytest

from seadash.driver import translate
from seadash.errors import CompileError

PRELUDE = "import threading\nimport queue\nfrom seadash import Atomic, Mutex, RWMutex, Synchronized\n"


def compile_ok(src: str) -> None:
    translate(PRELUDE + textwrap.dedent(src))


def compile_error(src: str) -> CompileError:
    with pytest.raises(CompileError) as info:
        translate(PRELUDE + textwrap.dedent(src))
    return info.value


SAFE = {
    "copies in args": """
        def work(xs: list[int], d: dict[str, int]):
            xs.append(1)
            xs.sort()
            print(xs[0], d)
        data = [1, 2]
        threading.Thread(target=work, args=(data, {"a": 1})).start()
    """,
    "read-only global": """
        LIMITS = [1, 2, 3]
        def work():
            print(sum(LIMITS))
        threading.Thread(target=work).start()
    """,
    "global built before threads start": """
        TABLE: dict[str, int] = {}
        TABLE["a"] = 1
        def work():
            print(TABLE["a"])
        threading.Thread(target=work).start()
    """,
    "thread-safe globals": """
        q: queue.Queue[int] = queue.Queue()
        counter = Atomic()
        box = Mutex([0])
        lock = threading.Lock()
        def work():
            q.put(1)
            counter.add(1)
            with box as b:
                b.append(2)
            with lock:
                pass
        threading.Thread(target=work).start()
    """,
    "synchronized class in args": """
        class Account(Synchronized):
            balance: int
            def deposit(self, n: int):
                self.balance += n
        def work(a: Account):
            a.deposit(1)
        threading.Thread(target=work, args=(Account(0),)).start()
    """,
    "lambda capturing a queue": """
        def main():
            results: queue.Queue[str] = queue.Queue()
            t = threading.Thread(target=lambda: results.put("hi"))
            t.start()
        main()
    """,
    "nested def capturing a value nobody changes": """
        def main():
            name = "worker"
            def run():
                print(name)
            threading.Thread(target=run).start()
        main()
    """,
    "local objects inside the thread": """
        class Node:
            value: int
        def work():
            n = Node(1)
            n.value += 1
            items: list[Node] = [n]
        threading.Thread(target=work).start()
    """,
    "bound method of a synchronized object": """
        class Log(Synchronized):
            lines: list[str]
            def write(self, s: str):
                self.lines.append(s)
        log = Log([])
        threading.Thread(target=log.write, args=("x",)).start()
    """,
    "closures get copies of what they capture": """
        def keep(xs: list[int]) -> list[int]:
            return xs
        def main():
            for i in range(3):
                threading.Thread(target=lambda: print(i)).start()
            items: list[int] = []
            def run():
                items.append(0)
                print(len(items))
            threading.Thread(target=run).start()
            items.append(1)
            keep(items)
            match [1, 2]:
                case [*items]:
                    pass
        main()
    """,
    "changing a copy and using it": """
        from concurrent.futures import ThreadPoolExecutor
        def grow(xs: list[int]) -> list[int]:
            xs.append(1)
            return xs
        def fill(q: queue.Queue[list[int]], xs: list[int]):
            xs.append(2)
            q.put(xs)
        with ThreadPoolExecutor() as pool:
            pool.submit(grow, [0])
        results: queue.Queue[list[int]] = queue.Queue()
        threading.Thread(target=fill, args=(results, [0])).start()
    """,
    "frozen classes that can't change are shared": """
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Limits:
            low: int
            high: int
        @dataclass(frozen=True)
        class Config:
            name: str
            limits: Limits
            tags: tuple[str, ...]
            def show(self) -> None:
                print(self.name)
        CONFIG = Config("x", Limits(1, 2), ("a",))
        def work(c: Config):
            print(c.limits.high, CONFIG.name)
        threading.Thread(target=work, args=(CONFIG,)).start()
        threading.Thread(target=CONFIG.show).start()
        q: queue.Queue[Config] = queue.Queue()
    """,
    "reading nested lists through aliases": """
        GRID = [[1, 2], [3]]
        def work():
            for row in GRID:
                print(len(row), sorted(row))
            total = sum(len(r) for r in GRID)
            copy = [list(r) for r in GRID]
            copy[0].append(total)
        threading.Thread(target=work).start()
    """,
    "mutex view used in place": """
        box = Mutex([[0]])
        def work():
            with box as rows:
                rows[0].append(1)
                for r in rows:
                    r.append(2)
                n = len(rows)
                snapshot = [list(r) for r in rows]
        threading.Thread(target=work).start()
    """,
}


@pytest.mark.parametrize("name", SAFE)
def test_safe_patterns_compile(name):
    compile_ok(SAFE[name])


UNSAFE = [
    ("""
        results: list[int] = []
        def work(n: int):
            results.append(n)
        threading.Thread(target=work, args=(1,)).start()
     """, "thread code uses the module-level 'results' (list[int]), but it's modified (line 7)"),
    ("""
        seen: dict[str, int] = {}
        def record(k: str):
            seen[k] = 1
        def work():
            record("x")
        threading.Thread(target=work).start()
     """, "thread code uses the module-level 'seen' (dict[str, int]), but it's modified (line 7)"),
    ("""
        class Node:
            value: int
        def work(n: Node):
            n.value += 1
        threading.Thread(target=work, args=(Node(1),)).start()
     """, "can't pass this to a thread: a Node is a class instance, shared by reference (make it a seadash.Synchronized class, "
                "or a frozen dataclass whose fields can't change either)"),
    ("""
        def work():
            pass
        f = work
        threading.Thread(target=f).start()
     """, "pass the thread's function directly (a def, a nested def, or a lambda)"),
    ("""
        class Counter:
            n: int
            def tick(self):
                self.n += 1
        c = Counter(0)
        threading.Thread(target=c.tick).start()
     """, "a thread can't run a method of a Counter: the object would be shared by both threads"),
    ("""
        class Node:
            v: int
        q: queue.Queue[Node] = queue.Queue()
     """, "a Queue can only hold values that can be copied between threads: a Node is a class instance"),
    ("""
        class Account(Synchronized):
            balance: int
        a = Account(0)
        print(a.balance)
     """, "Account is Synchronized, so its fields can only be used inside its methods (through self)"),
    ("""
        total = [0]
        def work():
            print(total[0])
        threading.Thread(target=work).start()
        total[0] = 5
     """, "thread code uses the module-level 'total' (list[int]), but it's modified (line 9)"),
    ("""
        def work(n: int):
            pass
        threading.Thread(target=work, args=("x",))
     """, "target takes (int), but args gives (str)"),
    # a closure run by a thread gets copies of what it captures: changing them is lost
    ("""
        def main():
            results: list[int] = []
            threading.Thread(target=lambda: results.append(1)).start()
        main()
     """, "the thread's function changes 'results', but a thread works on its own copy of the variables it uses"),
    ("""
        def main():
            seen: set[int] = set()
            def run():
                seen.add(1)
            threading.Thread(target=run).start()
        main()
     """, "the thread's function changes 'seen', but a thread works on its own copy"),
    ("""
        def main():
            items = [1]
            def run(n: int):
                if n > 0:
                    run(n - 1)
                print(len(items))
            threading.Thread(target=run, args=(3,)).start()
            items.append(2)
        main()
     """, "the thread's function uses 'items' from the enclosing function, but the enclosing function changes 'items' (and a recursive nested def"),
    ("""
        def main(loud: bool):
            items = [1]
            if loud:
                def run():
                    print(items, "!")
            else:
                def run():
                    print(items)
            threading.Thread(target=run).start()
            items.append(2)
        main(True)
     """, "the thread's function uses 'items' from the enclosing function, but the enclosing function changes 'items' (and a recursive nested def, or one defined more than once"),
    ("""
        from dataclasses import dataclass
        @dataclass(frozen=True)
        class Config:
            tags: list[str]
        def work(c: Config):
            print(c)
        threading.Thread(target=work, args=(Config([]),)).start()
     """, "can't pass this to a thread: a Config is a class instance, shared by reference"),
    ("""
        from dataclasses import dataclass
        @dataclass(frozen=True, eq=False)
        class Config:
            name: str
        @dataclass(frozen=True, eq=False)
        class Extended(Config):
            extra: list[str]
        def work(c: Config):
            print(c)
        threading.Thread(target=work, args=(Config("x"),)).start()
     """, "can't pass this to a thread: a Config is a class instance, shared by reference"),
    ("""
        import http.client
        def work(c: http.client.HTTPConnection):
            c.close()
        threading.Thread(target=work, args=(http.client.HTTPConnection("h"),)).start()
     """, "can't pass this to a thread: an HTTP connection (give each thread its own)"),
    # a thread's arguments are copies: changing one and never using it is lost work
    ("""
        results: list[int] = []
        def work(out: list[int]):
            out.append(1)
        threading.Thread(target=work, args=(results,)).start()
     """, "work() changes its copy of 'out' but never uses it: a thread gets its own copy of each argument"),
    ("""
        def work(n: int, table: dict[str, list[int]]):
            table["a"] = [n]
            table["a"][0] += 1
        threading.Thread(target=work, args=(1, {"a": [0]})).start()
     """, "work() changes its copy of 'table' but never uses it"),
    ("""
        from concurrent.futures import ThreadPoolExecutor
        def work(xs: list[int]) -> int:
            xs.clear()
            return 0
        with ThreadPoolExecutor() as pool:
            pool.submit(work, [1])
     """, "work() changes its copy of 'xs' but never uses it"),
    # lists are shared references: aliases, and functions they're passed to, can change them
    ("""
        LIMITS = [1, 2]
        def work():
            print(len(LIMITS))
        threading.Thread(target=work).start()
        other = LIMITS
        other.append(3)
     """, "thread code uses the module-level 'LIMITS' (list[int]), but it's modified"),
    ("""
        LIMITS = [1, 2]
        def grow(xs: list[int]):
            xs.append(3)
        def work():
            print(len(LIMITS))
        threading.Thread(target=work).start()
        grow(LIMITS)
     """, "thread code uses the module-level 'LIMITS' (list[int]), but it's modified"),
    ("""
        GRID = [[1], [2]]
        def work():
            for row in GRID:
                row.append(0)
        threading.Thread(target=work).start()
     """, "thread code uses the module-level 'GRID' (list[list[int]]), but it's modified"),
]


@pytest.mark.parametrize("src,msg", UNSAFE)
def test_unsafe_sharing_is_an_error(src, msg):
    assert compile_error(src).message.startswith(msg)


FUTURES = "from concurrent.futures import ThreadPoolExecutor\n"


def test_executor_work_is_checked_like_threads():
    # a done-callback may run on a worker thread
    e = compile_error(FUTURES + "seen: list[int] = []\nwith ThreadPoolExecutor() as pool:\n"
                      "    pool.submit(abs, -1).add_done_callback(lambda f: seen.append(f.result()))\n")
    assert e.message.startswith("thread code uses the module-level 'seen' (list[int]), but it's modified")
    e = compile_error(FUTURES + "class Box:\n    n: int\nwith ThreadPoolExecutor() as pool:\n    f = pool.submit(lambda: Box(1))\n")
    assert e.message.startswith("work on another thread can't return this: a Box is a class instance")
    e = compile_error(FUTURES + "class Box:\n    n: int\nb = Box(1)\nwith ThreadPoolExecutor() as pool:\n"
                      "    f = pool.submit(lambda x: x.n, b)\n")
    assert e.message.startswith("can't pass this to a thread: a Box is a class instance")
    compile_ok(FUTURES + "import queue\nq: queue.Queue[int] = queue.Queue()\nwith ThreadPoolExecutor() as pool:\n"
               "    pool.submit(abs, -1).add_done_callback(lambda f: q.put(f.result()))\n")


@pytest.mark.parametrize("body,msg", [
    ("saved = rows", "'rows' is only valid while the mutex is held, and this would let the data in it escape the lock"),
    ("first = rows[0]", "'rows' is only valid while the mutex is held"),
    ("for r in rows:\n                keep = r", "'rows' is only valid while the mutex is held"),
    ("everything = list(rows)", "or copy each item too, e.g. [list(row) for row in rows]"),
    ("print(helper(rows))", "'rows' is only valid while the mutex is held"),
])
def test_mutex_view_cant_escape(body, msg):
    e = compile_error(f"""
        box = Mutex([[0]])
        def helper(xs: list[list[int]]) -> int:
            return len(xs)
        with box as rows:
            {body}
    """)
    assert msg in e.message


@pytest.mark.parametrize("body,moved", [
    ("q.put(xs)", True),                                        # never used again
    ("q.put(xs)\n    print(xs)", False),                        # read afterwards
    ("q.put(xs)\n    xs = [2]\n    print(xs)", True),           # replaced before any read
    ("for i in range(3):\n        xs = [i]\n        q.put(xs)", True),   # each iteration makes a new one
    ("for i in range(3):\n        q.put(xs)", False),           # the next iteration sends it again
    ("for i in range(3):\n        xs.append(i)\n        q.put(xs)", False),
    ("while True:\n        q.put(xs)\n        if len(xs) > 0:\n            continue\n        xs = []", False),
    ("try:\n        q.put(xs)\n    except ValueError:\n        print(xs)", False),
    ("if len(xs) > 1:\n        q.put(xs)\n    else:\n        print(xs)", True),   # the other branch doesn't run after
])
def test_last_use_moves_instead_of_copying(body, moved):
    cpp = translate(PRELUDE + "def send(q: queue.Queue[list[int]]):\n    xs = [1]\n    " + body + "\n").cpp
    assert ("std::move(xs)" in cpp) == moved


@pytest.mark.parametrize("body,msg", [
    ("data = [1]\n    shared = Mutex(data)\n    data.append(2)",
     "'data' was moved into a Mutex (line 6), which owns it now: use it through the Mutex (`with shared as data:`), "
     "or give 'data' a new value first"),
    ("shared = Mutex([0])\n    data = [1]\n    shared.set(data)\n    print(data)", "'data' was moved into a Mutex"),
    ("data = [1]\n    for i in range(2):\n        shared = Mutex(data)", "'data' was moved into a Mutex"),
    ("data = [1]\n    if len(data) > 0:\n        shared = Mutex(data)\n    print(data)", "'data' was moved into a Mutex"),
    ("config = RWMutex({'a': 1})\n    with config.read() as c:\n        c['a'] = 2",
     "config.read() gives read-only access (other threads may be reading too), but this changes 'c'. "
     "Use `with config.write() as c:` to change it"),
    ("config = RWMutex([[1]])\n    with config.read() as c:\n        for row in c:\n            row.append(2)",
     "config.read() gives read-only access"),
    ("config = RWMutex([1])\n    with config as c:\n        pass", "say which: `with config.read() as data:`"),
    ("config = RWMutex([1])\n    view = config.read()",
     "read() gives a view of the data that's only valid while locked: use it in a with statement"),
    ("config = RWMutex([1])\n    keep: list[int] = []\n    with config.write() as c:\n        keep = c",
     "'c' is only valid while the mutex is held"),
])
def test_mutex_ownership_and_rwmutex_errors(body, msg):
    e = compile_error("def f():\n    " + body + "\nf()\n")
    assert msg in e.message


def test_mutex_ownership_allows_new_values():
    compile_ok("""
        def f():
            data = [1]
            shared = Mutex(data)
            data = [5]
            data.append(6)
            config = RWMutex({"a": [1]})
            with config.read() as c:
                print(len(c["a"]), sorted(c))
            with config.write() as c:
                c["a"].append(2)
                c["b"] = [data[0]]
            print(config.get(), shared.get())
        f()
    """)


LOOP_HEADER = """from seadash import value
@value
class P:
    x: int
    tags: list[str]
    def total(self) -> int:
        return self.x + len(self.tags)
    def bump(self):
        self.x += 1
def grow(ps: list[P]):
    ps.append(P(0, []))
"""


@pytest.mark.parametrize("body,by_reference", [
    ("for p in ps:\n        print(p.x, p.total(), len(p.tags))", True),
    ("out: list[P] = []\n    for p in ps:\n        out.append(P(p.x + 1, p.tags))\n    print(out)", True),
    ("p = P(0, [])\n    for p in ps:\n        print(p.x)\n    print(p)", False),  # read after the loop
    ("for p in ps:\n        ps.append(p)", False),                           # grows the list
    ("for p in ps:\n        grow(ps)", False),
    ("other = ps\n    for p in ps:\n        other.append(p)", False),         # an alias grows it
    ("for p in ps:\n        p.bump()\n        print(p)", False),             # changes its copy
    ("for p in ps:\n        p = P(1, [])\n        print(p)", False),
    ("for p in ps:\n        f = lambda: p.x\n        print(f())", False),
    ("names = ['a']\n    for n in names:\n        print(n.upper())", True),   # strings too
])
def test_loops_refer_to_items_when_nothing_can_tell(body, by_reference):
    src = LOOP_HEADER + "def f(ps: list[P]):\n    " + body + "\n"
    cpp = translate(src).cpp
    assert ("auto& p = " in cpp or "auto& n = " in cpp) == by_reference


@pytest.mark.parametrize("src,name", [
    ("m = threading.Mutex([1])", "Mutex"),
    ("c = threading.Atomic()", "Atomic"),
    ("m = threading.RWMutex({'a': 1})", "RWMutex"),
    ("m = threading.Mutex[list[int]]([])", "Mutex"),
    ("def f(m: threading.Mutex[list[int]]):\n    pass", "Mutex"),
    ("class S(threading.Synchronized):\n    n: int", "Synchronized"),
    ("from threading import Mutex", "Mutex"),
])
def test_seadash_types_are_not_in_threading(src, name):
    e = compile_error(src + "\n")
    assert e.message == f"{name} is seadash's own, not Python's: `from seadash import {name}`"


def test_seadash_types_by_module_name():
    compile_ok("""
        import seadash
        m = seadash.Mutex([1])
        class S(seadash.Synchronized):
            n: int
        def f(x: seadash.Mutex[list[int]], y: seadash.RWMutex[dict[str, int]], z: seadash.Atomic):
            with x as xs:
                xs.append(1)
        f(m, seadash.RWMutex({"a": 1}), seadash.Atomic())
        print(S(1))
    """)


def test_only_the_thread_types_need_threadings_header():
    assert "threading.hpp" not in translate("from seadash import value\n").cpp
    assert "threading.hpp" in translate("from seadash import Atomic\nc = Atomic()\n").cpp
    assert "pthread" in translate("import seadash\nc = seadash.Atomic()\n").libs


@pytest.mark.parametrize("name", ["threads_seadash", "threads_rwmutex"])
def test_seadash_thread_types_also_run_under_python(name, tmp_path):
    # seadash/__init__.py has Python versions of them, so these programs give the same output under python3.
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).parent.parent
    program = root / "tests" / "programs" / f"{name}.sd"
    result = subprocess.run([sys.executable, str(program)], capture_output=True, text=True, timeout=60, cwd=tmp_path,
                            env={**os.environ, "PYTHONPATH": str(root)})
    assert result.stdout == program.with_suffix(".out").read_text()


POOL = """
from concurrent.futures import ThreadPoolExecutor
class Box:
    items: list[int]
def total(xs: list[int]) -> int:
    return sum(xs)
def grow(xs: list[int]) -> int:
    xs.append(1)
    return len(xs)
def keep(xs: list[int]) -> list[int]:
    return xs
def helper():
    pass
def main():
    xs = [1, 2, 3]
    ys = xs
    d = {"k": xs}
    box = Box(xs)
    print(ys, d, box)
"""


@pytest.mark.parametrize("block,shared", [
    ("f = pool.submit(total, xs)\nprint(f.result())", True),
    ("fs = [pool.submit(total, xs) for _ in range(3)]\nprint(sum(f.result() for f in fs), len(xs))", True),
    ("f = pool.submit(grow, xs)", False),  # the task changes it
    ("f = pool.submit(keep, xs)", False),  # the task keeps it
    ("f = pool.submit(total, xs)\nxs.append(4)", False),  # the block changes it
    ("f = pool.submit(total, xs)\nys.append(4)", False),  # ...or something that could be it
    ('f = pool.submit(total, xs)\nd["k"].append(4)', False),
    ("f = pool.submit(total, xs)\nbox.items.append(4)", False),
    ("f = pool.submit(total, xs)\nhelper()", False),  # the program's own code could change anything
    ("f = pool.submit(total, xs)\ng = lambda: 1", False),
    ("f = pool.submit(total, xs)\npool.shutdown(wait=False)", False),
])
def test_pool_tasks_share_what_nothing_changes(block, shared):
    body = textwrap.indent(block, " " * 8)
    cpp = translate(PRELUDE + POOL + f"    with ThreadPoolExecutor() as pool:\n{body}\nmain()\n").cpp
    assert ("sd::lend(" in cpp) == shared


def test_pool_tasks_outside_a_with_get_copies():
    cpp = translate(PRELUDE + POOL + "    pool = ThreadPoolExecutor()\n    f = pool.submit(total, xs)\nmain()\n").cpp
    assert "sd::lend(" not in cpp


SERVER = "from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer\n"


@pytest.mark.parametrize("src,msg", [
    ("seen: list[str] = []\nclass H(BaseHTTPRequestHandler):\n    def do_GET(self):\n        seen.append(self.path)\n"
     "ThreadingHTTPServer(('', 0), H).serve_forever()\n",
     "thread code uses the module-level 'seen' (list[str]), but it's modified (line 8)"),
    ("count = 0\nclass H(BaseHTTPRequestHandler):\n    def do_GET(self):\n        global count\n        count += 1\n"
     "s = HTTPServer(('', 0), H)\nthreading.Thread(target=s.serve_forever).start()\n",
     "thread code uses the module-level 'count' (int), but it's modified (line 9)"),
    ("def run(s: HTTPServer):\n    threading.Thread(target=s.serve_forever).start()\n",
     "can't tell which handler class this server runs"),
])
def test_handler_code_is_thread_code_when_it_runs_on_threads(src, msg):
    assert msg in compile_error(SERVER + src).message


def test_a_server_on_the_main_thread_may_change_globals():
    compile_ok(SERVER + textwrap.dedent("""
        count = 0
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                global count
                count += 1
        HTTPServer(('', 0), H).serve_forever()
    """))


@pytest.mark.parametrize("src,msg", [
    ("class H(BaseHTTPRequestHandler):\n    def __init__(self, x: int):\n        pass\nHTTPServer(('', 0), H)\n",
     "H can't have an __init__: the server makes a new H for each connection, with no arguments of yours "
     "(keep shared state in a module-level Synchronized object)"),
    ("class H(BaseHTTPRequestHandler):\n    n: int\nHTTPServer(('', 0), H)\n",
     "H.n needs a default value: the server makes a new H for each connection, with no arguments"),
    ("class H(BaseHTTPRequestHandler):\n    def do_GET(self, x: int):\n        pass\nHTTPServer(('', 0), H)\n",
     "H.do_GET() is called for each request with no arguments (besides self), and returns nothing"),
    ("class H(BaseHTTPRequestHandler):\n    pass\nHTTPServer(('', 0), H())\n",
     "http.server.HTTPServer() needs the handler class itself (a subclass of BaseHTTPRequestHandler with do_GET... "
     "methods), not an instance or anything else"),
    ("class H(BaseHTTPRequestHandler):\n    def log_message(self, format: str) -> None:\n        pass\n",
     "H.log_message() overrides BaseHTTPRequestHandler.log_message(), so it must have the same parameter and return "
     "types: def log_message(format: str, *args: str) -> None"),
])
def test_handler_errors(src, msg):
    assert compile_error(SERVER + src).message == msg


TABLE_ERROR = ("mimetypes.types_map is shared by the whole program, and this program starts threads, which may be reading it "
               "(mimetypes.guess_type() and the rest read it too): change it only in module code before the first thread "
               "starts, or work on a copy, e.g. `dict(mimetypes.types_map)`; mimetypes.add_type() is safe anywhere")


@pytest.mark.parametrize("src", [
    "def work():\n    mimetypes.types_map['.x'] = 'text/x'\nthreading.Thread(target=work).start()\n",
    "threading.Thread(target=print).start()\nmimetypes.types_map.update({'.x': 'y'})\n",
    "def f():\n    m = mimetypes.types_map\n    m['.x'] = 'y'\nthreading.Thread(target=f).start()\n",
    "def g(d: dict[str, str]):\n    d['a'] = 'b'\ndef f():\n    g(mimetypes.types_map)\nthreading.Thread(target=f).start()\n",
])
def test_library_tables_cant_change_once_threads_may_run(src):
    assert compile_error("import mimetypes\n" + src).message == TABLE_ERROR


@pytest.mark.parametrize("src", [
    "mimetypes.types_map['.x'] = 'text/x'\nthreading.Thread(target=print).start()\n",  # before any thread starts
    "def f():\n    m = mimetypes.types_map\n    print(m['.html'])\nthreading.Thread(target=f).start()\n",
    "def f():\n    m = dict(mimetypes.types_map)\n    m['.x'] = 'y'\nthreading.Thread(target=f).start()\n",
    "def f():\n    mimetypes.add_type('text/x', '.x')\nthreading.Thread(target=f).start()\n",
    "def f():\n    mimetypes.types_map['.x'] = 'y'\nf()\n",  # no threads at all
])
def test_library_tables_that_stay_safe(src):
    compile_ok("import mimetypes\n" + src)


def test_sys_argv_is_a_library_table():
    e = compile_error("import sys\ndef f():\n    sys.argv.append('x')\nthreading.Thread(target=f).start()\n")
    assert e.message.startswith("sys.argv is shared by the whole program") and "`list(sys.argv)`" in e.message



@pytest.mark.parametrize("src,msg", [
    ("def work(xs: list[int]) -> None:\n    xs.append(1)\ndata = [1]\nthreading.Thread(target=partial(work, data)).start()\n",
     "work() changes its copy of 'xs' but never uses it: a thread gets its own copy of what partial() holds"),
    ("class C:\n    n: int\n    def run(self, k: int) -> None:\n        print(k)\nc = C(1)\n"
     "threading.Thread(target=partial(c.run, 2)).start()\n", "a thread can't run a method of a C"),
    ("def work(k: int) -> None:\n    print(k)\ng: Callable[[int], None] = work\nthreading.Thread(target=partial(g, 1)).start()\n",
     "pass partial() a function defined with def (or a class)"),
    ("seen: list[int] = []\ndef work(k: int) -> None:\n    seen.append(k)\nthreading.Thread(target=partial(work, 1)).start()\n",
     "thread code uses the module-level 'seen' (list[int]), but it's modified"),
])
def test_partial_as_thread_work(src, msg):
    assert msg in compile_error("from functools import partial\nfrom typing import Callable\n" + src).message



@pytest.mark.parametrize("src,msg", [
    ("HTTPServer(('', 0), partial(SimpleHTTPRequestHandler, 'public'))\n",
     "partial() of a handler class gives it settings by keyword only, e.g. partial(SimpleHTTPRequestHandler, directory='public')"),
    ("HTTPServer(('', 0), partial(SimpleHTTPRequestHandler, folder='public'))\n",
     "SimpleHTTPRequestHandler has no setting 'folder' (its fields: "),
    ("HTTPServer(('', 0), partial(SimpleHTTPRequestHandler, directory=3))\n",
     "SimpleHTTPRequestHandler.directory is str, not int"),
    ("seen: list[str] = []\nclass H(SimpleHTTPRequestHandler):\n    def translate_path(self, path: str) -> str:\n"
     "        seen.append(path)\n        return path\nThreadingHTTPServer(('', 0), H).serve_forever()\n",
     "thread code uses the module-level 'seen' (list[str]), but it's modified"),
])
def test_simple_handler_errors(src, msg):
    head = "from functools import partial\nfrom http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler\n"
    assert msg in compile_error(head + src).message


def test_simple_handler_on_threads():
    compile_ok("from functools import partial\nfrom http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler\n"
               "ThreadingHTTPServer(('', 0), partial(SimpleHTTPRequestHandler, directory='.')).serve_forever()\n")
