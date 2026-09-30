# Design: values, references and the thread boundary

(Value types were first written with a `struct` keyword; they're `@value` classes now,
`from seadash import value`, so that Python's `import struct` works.)

Status: **phase 1 implemented** (see Phases). This replaces the earlier rule that lists,
dicts and sets were values.

## Summary

- Within a thread, seadash behaves like Python: lists, dicts, sets and class instances
  are **references**. Assigning or passing one shares it.
- At a **thread boundary**, anything that isn't thread-safe is **deep-copied
  automatically**. Thread-safe objects (locks, queues, `Mutex[T]`, `Atomic`,
  `Synchronized` classes, deeply frozen values) are shared.
- A `@value` class is seadash's **value type**, and a value all the way down: its fields must be
  values, and copying a value class copies everything in it.
- `@dataclass(frozen=True)` makes a value class or an ordinary class immutable. Immutability is checked at
  compile time.
- `threading.Mutex(x)` **takes ownership** of `x`. The view inside `with m as t:` can't
  escape the block.
- A **modified copy that is never used** is a compile error, because the change is lost.

Two short rules for the README:

1. **Everything behaves like Python, except `@value` classes, which are values.**
2. **Threads get copies of anything that isn't thread-safe.**

## Why change

Today lists, dicts and sets are values: assigning or passing one copies it (the generated
code passes `const&` and copies only when the function modifies the parameter). That
silently changes the meaning of valid, common Python:

```python
def dfs(node: int, visited: set[int]):
    visited.add(node)                  # today: adds to dfs's own copy
    for n in graph[node]:
        if n not in visited:
            dfs(n, visited)
```

Depending on the code this gives wrong results, exponential time, or an infinite loop on a
graph with a cycle, with no error. Helpers that fill an `out` list break the same way. Most
programs are single-threaded, and for them Python's reference semantics are both what
people expect and the cheapest option.

Value semantics were chosen to keep threads safe. This design keeps that safety by copying
at the thread boundary instead of on every assignment.

## Values and references

| kind | types | `b = a` / passing |
|---|---|---|
| immutable (value and reference look the same) | `int`, `float`, `bool`, `str`, `bytes`, `None`, dates and times, tuples (of values) | either; the compiler picks |
| references (Python semantics) | `list`, `dict`, `set`, `deque`, `Counter`, `defaultdict`, classes | shares |
| values | `@value` class | copies |
| thread-safe references | `Lock`, `Event`, `Queue`, `Mutex[T]`, `Atomic`, `Synchronized` classes | shares, also across threads |

A tuple is immutable, so its own semantics don't matter. Its elements follow their own
rules: a tuple holding a list shares that list, as in Python.

`is` becomes meaningful for lists, dicts and sets, as in Python.

## The thread boundary

### What counts as a boundary

A value crosses into another thread when it is:

- passed in `threading.Thread(target=..., args=..., kwargs=...)`;
- captured by the thread's target, as a closure variable or a bound method's `self`;
- passed to `ThreadPoolExecutor.submit` or `map`, or returned through a `Future`;
- put on a `queue.Queue`;
- returned from `thread.join()`-style results (if added later).

### What happens

- **Thread-safe** objects are shared by reference.
- **Deeply immutable** objects are shared by reference, with no copy (see Frozen types).
- **Everything else is deep-copied** automatically. Aliasing inside the copied data is kept:
  if two list entries refer to one object, the copy's two entries refer to one new object,
  as with `copy.deepcopy`. Cycles are handled the same way.
- **Moves:** when the value is never used again on the sending side (for example
  `q.put(batch)` as the last use of `batch`), the compiler moves it instead of copying.
  This is an optimization only; the meaning is still "the receiver has its own".
- **Module globals** keep today's rule: thread code may only read them, unless they're
  thread-safe. They can't be copied implicitly because every thread names the same one.

### Modifying a copy in a thread is an error

Copying moves the old surprise to the boundary: a worker that appends to its copy and
expects the parent to see it. So if a thread's code **modifies a value it received as a
copy and never uses it**, the checker reports an error:

```
error: work() changes its copy of 'out' but never uses it: a thread gets its own copy of
each argument, so the change never reaches the caller. Share the data with
threading.Mutex, send results back through a queue.Queue, or return them
(ThreadPoolExecutor)
```

A copy that is changed and then used (sorted and printed, put on a queue, returned) is fine,
and so is reading a copied value. The same rule applies to the variables a thread's closure
captures: each thread gets copies, made when the thread is created.

## Sharing a non-thread-safe object: `Mutex[T]`

`threading.Mutex` (which already exists) is the container for sharing any object:

```python
shared = threading.Mutex([])        # takes ownership of the list

def worker(n: int):
    with shared as xs:              # holds the lock while the block runs
        xs.append(n)
```

New rules:

1. **`Mutex(x)` and `m.set(x)` take ownership of `x`.** If `x` is a local name, it's moved
   in, and reading it afterwards is an error until it's given a new value ("'xs' was moved
   into a Mutex (line 4), which owns it now: use it through the Mutex (`with shared as
   xs:`), or give 'xs' a new value first"). The check is flow-sensitive, so a loop that
   wraps the same name twice is an error too. An alias the checker can't see (`other = xs`
   earlier) makes the runtime copy instead (`sd::send`), so the Mutex's data is never
   shared. A module-level name is copied (functions may still read it), but the module's
   own code reading it again is the same error. Anything else (a field, a literal, a
   captured variable) is copied.
2. **The view doesn't escape.** Inside `with m as t:`, `t` can't be stored in an outer
   variable or field, returned, captured by a closure, sent to another thread, or put in a
   container that outlives the block. Anything read out of `t` that is a reference (for
   example `row = t[0]` when `t` is a `list[list[int]]`) is subject to the same rule; copy
   what you need (`list(t)`, `[list(row) for row in t]`).
3. **`threading.RWMutex(x)`** for data read far more than written: `with m.read() as t:`
   allows many readers at once (a `std::shared_mutex`), and `with m.write() as t:` one
   writer. A `read()` view is read-only: changing it (also through `for row in t:
   row.append(...)`) is a compile error. There's no plain `with m as t:`, and `read()` /
   `write()` can only be used as a with item. `get()` and `set()` work as on `Mutex`.

Where it fits, handing data over through a `Queue` is often simpler than a lock. With a
move, sending costs nothing.

## Value classes

### A value class is a value all the way down

- Copying a value class copies all of its fields, including lists, dicts and sets.
- **Value class fields must be values:** numbers, `str`, `bytes`, dates and times, `None`,
  tuples/lists/dicts/sets of values, other value classes, and optionals of those. A class, a
  thread-safe object, a file, socket, generator, future, logger or function is not allowed,
  directly or inside a container:

  ```
  error: value class fields must be values, but 'customer' is a Customer, which is a class (a
  shared reference). Store an id instead, make Order a class, or make Customer a value class.
  ```

- Consequences: every value class can cross a thread boundary as a plain copy; a frozen value class
  is always deeply immutable; equality and hashing are by value with no hidden identity;
  a value class can nest by value (`children: list[Tree]`) but can never contain a cycle.

### Handles in value classes

A value class may hold an **operating system handle as a plain number**: a file descriptor from
`f.fileno()`, `sock.fileno()` or `os.open(...)`. The developer is explicitly responsible
for it: copying the value class copies the number, not the resource, and closing it
(`os.close(fd)`) is up to the program. To use it, a program turns it back into an object
(`os.fdopen(fd)`, `socket.socket(fileno=fd)`).

Work needed for this: `os.open`, `os.close`, `os.fdopen`, and `socket.socket(fileno=...)`
where missing.

See **Future: implied handles** for making this safer later.

### Modifying value classes

- **Parameters:** a value class parameter is the function's own copy, which it may modify. If
  the caller needs the change, the function returns it.
- **Containers:** a value class read out of a container is a copy, and changes are written back
  explicitly:

  ```python
  p = ps[i]
  p.x += 1
  ps[i] = p
  ```

- **In place:** methods may modify `self` in place, and so may direct assignments through
  a container: `ps[i].step()` and `ps[i].x += v` change the element in the list.

### A value class's list fields

A value class's list (or dict or set) is reached through the value class:

| code | meaning |
|---|---|
| `s.items.append(x)`, `s.items[0] = y`, `for x in s.items` | works on the value class's own list |
| `xs = s.items` | `xs` is a copy; write it back with `s.items = xs` |
| `s.items = xs` | copies `xs` into the value class |
| `fill(s.items)` | `fill` works on the value class's own list, so its changes land in `s` |

If a function stores a parameter it was given this way (in a field, a closure or a
container), the compiler stores a copy.

The rule underneath: **nothing holds a reference into a value class beyond a single expression
or call.** Anything that would have to hold one gets a copy. This also keeps it
memory-safe, since a reference into a value class would dangle once the value class is gone.

## Frozen types

`@dataclass(frozen=True)`, Python's spelling, works on both value classes and classes:

```python
@value
@dataclass(frozen=True)
class Point:
    x: int
    y: int

p = Point(1, 2)
p.x = 5                          # compile error: Point is frozen; use dataclasses.replace(p, x=5)
q = dataclasses.replace(p, x=5)
```

- Assigning a field is a compile error. Python raises `FrozenInstanceError` at run time.
- `dataclasses.replace(obj, **changes)` makes a modified copy.
- Frozen types are hashable, as in Python.
- A frozen **value class** is deeply immutable (its lists are part of the value), so it can
  always be shared between threads without copying.
- A frozen **class** is shallowly frozen, as in Python: fields can't be reassigned, but a
  `list` field can still change. It's deeply immutable, and so shared between threads
  without copying, only when every field type is immutable (numbers, `str`, `bytes`,
  tuples of immutables, `frozenset`, other deeply frozen types).

## Modified copies that are never used

Anywhere a copy is made (a value class parameter, a value class read out of a container, a list
field bound to a name, a value received by a thread), modifying the copy and then never
reading it again means the change is lost. That's almost always a mistake, so it's an
error:

```
error: this changes a copy of ps[i] and then drops it. Write it back (ps[i] = p) or
return it.
```

A copy that is modified and then used (returned, stored, passed on, printed) is never
flagged.

## Memory safety

References bring aliasing, and with C++ containers aliasing can mean undefined behaviour:

- **Changing a container while iterating it.** A loop over `xs` can call a function that
  appends to the same list; the vector reallocates and the loop reads freed memory. The
  checker can't find every alias, so the runtime guards against it: lists iterate by
  index (appending during a loop behaves as in Python), and dicts and sets keep a
  modification counter and raise `RuntimeError: dictionary changed size during iteration`,
  as Python does.
- **References into containers.** An element reference (`auto&`) held across a call that
  might resize the container would dangle. Generated code re-indexes instead of holding
  element references across calls.
- **References into value classes** follow the value class rule above.

## Implementation notes

- **Representation.** Lists, dicts and sets become reference-counted heap objects, and so
  do classes (today `std::shared_ptr`). Objects that never cross a thread boundary can use
  a **non-atomic** reference count, because the checker guarantees they're only touched by
  one thread. Only thread-safe types need atomic counts. This is cheaper than
  `std::shared_ptr`, whose counts are always atomic, and than free-threaded CPython.
- **Avoiding the pointer hop.** A parameter that doesn't escape the function can be passed
  as a plain C++ reference with no reference counting. A local that is never aliased can
  stay inline. This is an optimization for a later phase.
- **Deep copy.** A runtime `sd::deep_copy(value, memo)` generated per type, keeping
  aliasing and handling cycles.
- **Checker.** Escape analysis for `Mutex` views and for parameters that might be
  references into value classes; ownership tracking for `Mutex(x)`; the modified-copy check; the
  @value
  class field rule; frozen types.

### Phases

1. **Done.** Lists, dicts, sets, deques, `Counter` and `defaultdict` are references
   (`sd::list` and friends are handles to shared storage); iteration guards; `is` on them;
   in-place `+=`, `*=`, `|=`... Value classes copy their lists (`sd::value_copy` in generated copy
   constructors, constructors and field assignments). To keep threads safe, this phase also
   brought in the copies at the thread boundary (`Thread` args, `Queue.put`,
   `executor.submit` args and `Future.result()`, `Mutex` in and out, `Synchronized` method
   arguments and results), the "changed through an alias or a function" rule for shared
   globals and captures, and the rule that a `with m as data:` view can't escape.
   Differences from the plan: reference counts are `std::shared_ptr`'s (atomic), because
   threads may still read a shared global or captured list and copy its handle; classes
   stay on `std::shared_ptr`. Both are left to the performance phase. Deep copies don't
   keep aliasing inside the copied data yet (two entries sharing a list become two lists).
2. **Done.** Deep copies keep aliasing (`CopyMemo`). Moves: codegen passes a list with
   `std::move` at a thread boundary when it's the sender's last use (`last_use`: no read
   afterwards, or replaced first, including at the top of the next loop iteration), and
   `sd::send` hands it over only if nothing else references it (else copies). A thread
   changing a copy it never uses is an error (arguments and captured variables). Captured
   variables are copied when the thread is created: a nested def run by a thread is built
   by a maker from copies of what it captures; a lambda captures copies. A recursive
   nested def, or one defined more than once, keeps the older rule (the enclosing function
   mustn't change what it shares). Not done: `Future.result()` still copies on every call.
3. **Done.** `Mutex` ownership (`Moved` in the checker's flow state) and `RWMutex`.
4. **Done.** Fields of a `@value` class must be values (`threads.not_a_value`), so every
   value class copies to a thread as is. A value class's list used as a value is copied
   (`flow.mark_copy_outs`: bound, returned, stored, put in a literal, or passed to a function
   that keeps its parameter; calls through a function value always get a copy). A change to
   a copy that's never read afterwards is an error (`flow.dropped_changes`), for @value
   parameters, names bound to a copy, and `for p in ps` over @value classes. Not done: the
   file-descriptor functions (`os.open`, `os.fdopen`, `f.fileno()`...), next as their own
   feature.
5. Frozen types and `dataclasses.replace`; sharing deeply immutable values across threads.
6. Performance: non-escaping parameters as plain references, inline locals. Compare with
   the benchmarks before and after each phase.

## Differences from Python after this change

- `@value` class (not Python) is a value type.
- Threads receive copies of anything that isn't thread-safe; modifying such a copy is a
  compile error.
- Frozen dataclasses are checked at compile time.
- The README's "Lists, dicts and sets are values" entry is removed.

## Open questions

- **Deeply frozen classes as value class fields.** They behave like values, apart from `is`.
  Proposed: not allowed at first; add if needed.
- **Mutable default arguments** (`def f(xs=[])`). With reference semantics Python's
  gotcha comes back. Options: match Python, evaluate defaults on each call, or make a
  mutable default a compile error. Proposed: compile error with a suggested fix
  (`xs: list[int] | None = None`).
- **`RWMutex`** naming and whether to add it now.
- **Moves as a visible concept.** Whether the programmer can request a move explicitly,
  or it stays an optimization.

## Future: implied handles

Today a handle in a value class is a plain number the program manages itself. Later, seadash
could understand handles as a kind of value: a file descriptor on Unix, a `HANDLE` on
Windows if seadash adds Windows support, and so on. Questions to explore then:

- what copying a value class with a handle means (share the number, `dup()` the descriptor, or
  forbid the copy and move instead);
- who closes it, and when (ownership, like `with` blocks, or reference counting);
- how a handle crosses a thread boundary;
- one portable type (`os.Handle`?) that is a descriptor on Unix and a `HANDLE` on Windows.
