"""Thread safety: unsafe sharing is a compile error; the safe patterns compile."""

import textwrap

import pytest

from seadash.driver import translate
from seadash.errors import CompileError

PRELUDE = "import threading\nimport queue\n"


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
        counter = threading.Atomic()
        box = threading.Mutex([0])
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
        class Account(threading.Synchronized):
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
        class Log(threading.Synchronized):
            lines: list[str]
            def write(self, s: str):
                self.lines.append(s)
        log = Log([])
        threading.Thread(target=log.write, args=("x",)).start()
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
     """, "thread code uses the module-level 'results' (list[int]), but it's modified (line 6)"),
    ("""
        seen: dict[str, int] = {}
        def record(k: str):
            seen[k] = 1
        def work():
            record("x")
        threading.Thread(target=work).start()
     """, "thread code uses the module-level 'seen' (dict[str, int]), but it's modified (line 6)"),
    ("""
        class Node:
            value: int
        def work(n: Node):
            n.value += 1
        threading.Thread(target=work, args=(Node(1),)).start()
     """, "can't pass this to a thread: a Node is a class instance, shared by reference (make it a threading.Synchronized class)"),
    ("""
        def main():
            for i in range(3):
                threading.Thread(target=lambda: print(i)).start()
        main()
     """, "the thread's function uses 'i' from the enclosing function, but the enclosing function changes 'i'"),
    ("""
        def main():
            items: list[int] = []
            def run():
                print(len(items))
            threading.Thread(target=run).start()
            items.append(1)
        main()
     """, "the thread's function uses 'items' from the enclosing function, but the enclosing function changes 'items'"),
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
        class Account(threading.Synchronized):
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
     """, "thread code uses the module-level 'total' (list[int]), but it's modified (line 8)"),
    ("""
        def work(n: int):
            pass
        threading.Thread(target=work, args=("x",))
     """, "target takes (int), but args gives (str)"),
    ("""
        def main():
            items: list[int] = []
            def run():
                print(len(items))
            threading.Thread(target=run).start()
            match [1, 2]:
                case [*items]:
                    pass
        main()
     """, "the thread's function uses 'items' from the enclosing function, but the enclosing function changes 'items'"),
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
