"""Thread safety: seadash runs threads in parallel (no global lock), so the checker
proves they can't race on memory.

The rule: a thread may only reach
  * values it received as copies (arguments to Thread(...), items from a Queue), and
  * thread-safe objects: Lock, RLock, Event, Queue[T], Mutex[T], RWMutex[T], Atomic, Thread,
    and instances of seadash.Synchronized classes (their methods hold a lock).

Values are copied (all the way down: sd::value_copy) when they cross into a thread, so
the only ways to *share* memory are class instances, closure-captured variables, and
module globals. This module checks each Thread(...) call:

  * its args must be sendable (copyable values or thread-safe objects);
  * its target is followed through every function/method it can call, and every module
    global it touches must be thread-safe, or a sendable value that nothing modifies;
  * if the target is a closure, each captured variable must be thread-safe or sendable;
    the thread gets copies of them, made when it's created (codegen rebuilds the closure
    from copies: `snapshot`), so the thread changing its copy without using it is an error;
  * a parameter the thread's function changes but never uses is an error too (the change
    would be lost: the thread got a copy).

Lists, dicts and sets are shared references, so "modifies" includes changing one through
another name (`other = xs; other.append(1)`, `for row in grid: row.append(0)`) and
passing it anywhere that could keep or change it (see `uses`).

Races on *logic* (check-then-act) are still possible, as in any language; races on
memory are not.
"""

from __future__ import annotations

import dataclasses

from . import ast as A
from .errors import CheckError, Loc
from .types import (
    element_type,
    BOOL, BYTES, FLOAT, INT, JSON_VALUE, NONE, SOCKET, STR, IMMUTABLE, LOCKED, VALUE, BuiltinClass,
    DefaultDictType, DequeType, DictType, FutureType, GeneratorType, MatchType, PatternType, ProcessType, VarTupleType, FileType, FuncInfo, FuncType, ListType, OptionalType, SetType, StructType, SyncType, TupleType, Type, Var,
)

MUTATING_METHODS = frozenset(
    "append insert pop remove extend sort reverse clear update setdefault add discard "
    "appendleft popleft extendleft rotate subtract popitem intersection_update difference_update "
    "symmetric_difference_update".split()
)

# Built-in functions that only read their arguments (or copy them): passing a list to
# one doesn't let anything change it later...
READING_BUILTINS = frozenset(
    "len print str repr ascii format sorted list set dict tuple sum min max any all enumerate zip map "
    "filter reversed iter hash isinstance bool abs round int float".split()
)
# ...except that these hand back its items, which are shared if they're lists themselves.
ITEM_BUILTINS = frozenset("sorted list set dict tuple min max enumerate zip map filter reversed iter".split())


# Built-in methods that copy (put, submit, Mutex.set) or only read a list they're given.
# Others (d.get(k, xs), xs.append(ys)...) may hand it back or keep it.
READING_METHODS = frozenset(
    "put put_nowait submit map set count index union intersection difference symmetric_difference "
    "issubset issuperset isdisjoint update intersection_update difference_update symmetric_difference_update".split()
)


def holds_references(t: Type) -> bool:
    """Does a value of type t contain a list, dict or set (a shared reference)?"""
    match t:
        case ListType() | SetType() | DictType() | DequeType() | DefaultDictType():
            return True
        case OptionalType(inner) | VarTupleType(inner):
            return holds_references(inner)
        case TupleType(elts):
            return any(holds_references(e) for e in elts)
        case StructType() if t.kind == "struct":
            return False  # (a struct copies its lists)
    return False


class ThreadSafetyError(CheckError):
    def __init__(self, message: str, loc: Loc, module: str):
        super().__init__(message, loc)
        self.module = module


def is_synchronized(st: StructType) -> bool:
    return any(t.builtin and t.name == "Synchronized" for t in st.ancestors())


# Every class the checker declares (for deeply_immutable: a subclass could add a list).
ALL_CLASSES: list[StructType] = []


def deeply_immutable(t: Type, seen: frozenset = frozenset()) -> bool:
    """Can nothing about a value of type t ever change? Then threads may share it with no
    copy and no lock: numbers, strings, tuples of those, frozen @value classes (frozen all
    the way down), and frozen classes whose fields (and subclasses' fields) are all like that."""
    match t:
        case _ if t in (INT, FLOAT, BOOL, STR, BYTES, NONE):
            return True
        case BuiltinClass():
            return t.threads == IMMUTABLE
        case TupleType(xs):
            return all(deeply_immutable(x, seen) for x in xs)
        case VarTupleType(x) | OptionalType(x):
            return deeply_immutable(x, seen)
        case StructType() if t.kind == "struct":
            return t.frozen
        case StructType() if t.kind == "class" and not t.builtin and not is_synchronized(t):
            if t in seen:
                return True
            classes = [t] + [c for c in ALL_CLASSES if c is not t and c.is_subclass_of(t)]
            return all(
                any(a.frozen for a in c.ancestors())
                and all(deeply_immutable(f.type, seen | {t}) for f in c.all_fields().values())
                for c in classes
            )
    return False


def unsendable(t: Type, seen: frozenset = frozenset()) -> str | None:
    """Why a value of type `t` can't be handed to another thread, or None if it can."""
    match t:
        case _ if t in (INT, FLOAT, BOOL, STR, BYTES, NONE, JSON_VALUE):
            return None
        case SyncType():
            return None
        case BuiltinClass() if t.threads:  # shared (it can't change, or locks itself), or copied
            return None
        case BuiltinClass() if t.unsendable:
            return t.unsendable
        case FutureType(x):  # its result is copied out
            return unsendable(x, seen)
        case PatternType() | MatchType():  # immutable (a Match holds its own copy of the string)
            return None
        case ProcessType(kind="CompletedProcess"):
            return None
        case ProcessType():
            return "a Popen (its pipes aren't safe to use from two threads; pass what you read from it)"
        case ListType(x) | SetType(x) | OptionalType(x) | DequeType(x) | VarTupleType(x):
            return unsendable(x, seen)
        case GeneratorType():
            return "an iterator (it's shared, and runs its generator's code when read; pass list(it) instead)"
        case DefaultDictType():
            return "a defaultdict (its factory function could share variables it captured; pass dict(d) instead)"
        case DictType(k, v):
            return unsendable(k, seen) or unsendable(v, seen)
        case TupleType(xs):
            return next((r for x in xs if (r := unsendable(x, seen))), None)
        case StructType() if t.kind == "struct":
            return None  # a @value class holds only values (checked where it's defined), so it copies
        case StructType() if is_synchronized(t) or deeply_immutable(t):
            return None  # (thread-safe, or can't change: shared, not copied)
        case StructType():
            return (f"a {t.name} is a class instance, shared by reference (make it a seadash.Synchronized class, "
                    f"or a frozen dataclass whose fields can't change either)")
        case FuncType():
            return "a function value (it could share variables it captured)"
    return f"a {t} can't be shared between threads"


def not_a_value(t: Type) -> str | None:
    """Why a @value class can't have a field of type t (it has identity: copying the class
    would share it), or None. A @value class is a value all the way down."""
    match t:
        case _ if t in (INT, FLOAT, BOOL, STR, BYTES, NONE, JSON_VALUE):
            return None
        case BuiltinClass() if t.threads in (IMMUTABLE, VALUE):
            return None
        case ListType(x) | SetType(x) | OptionalType(x) | DequeType(x) | VarTupleType(x):
            return not_a_value(x)
        case DictType(k, v) if not isinstance(t, DefaultDictType):
            return not_a_value(k) or not_a_value(v)
        case TupleType(xs):
            return next((r for x in xs if (r := not_a_value(x))), None)
        case StructType() if t.kind == "struct":
            return None
        case PatternType() | MatchType() | ProcessType(kind="CompletedProcess"):
            return None  # (immutable)
        case StructType():
            return f"{t.name}, an ordinary class (a shared reference)"
        case SyncType():
            return f"a {t.kind}, a thread-safe object shared by reference"
        case DefaultDictType():
            return "a defaultdict (its factory function could share what it captured; use a dict)"
        case FuncType():
            return "a function (it could share the variables it captured)"
        case GeneratorType():
            return "an iterator (it's shared, and runs code when read; store list(it))"
        case FileType():
            return "an open file (a @value class may keep its descriptor, f.fileno(), as an int)"
        case _ if t == SOCKET:
            return "a socket (a @value class may keep its descriptor, sock.fileno(), as an int)"
    return f"a {t} (it has identity: copying the @value class would share it)"


def shareable(t: Type) -> bool:
    """Safe to access from several threads at once without copying."""
    return isinstance(t, (SyncType, FutureType)) or t == JSON_VALUE or (isinstance(t, BuiltinClass) and t.threads == LOCKED) or (
        isinstance(t, StructType) and is_synchronized(t)
    )


def children(node):
    for f in dataclasses.fields(node):
        if f.name in ("loc", "sym", "ty", "dunder"):
            continue
        value = getattr(node, f.name)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, A.Node):
                yield item


def walk(node):
    yield node
    for c in children(node):
        yield from walk(c)


def walk_all(nodes):
    for n in nodes:
        yield from walk(n)


def base_var(e: A.Expr) -> Var | None:
    match e:
        case A.Name():
            return e.sym if isinstance(e.sym, Var) else None
        case A.Attribute(value) | A.Index(value):
            return base_var(value)
    return None


def target_vars(target: A.Expr) -> list[Var]:
    match target:
        case A.Name():
            return [target.sym] if isinstance(target.sym, Var) else []
        case A.TupleLit(elts) | A.ListLit(elts):
            return [v for elt in elts for v in target_vars(elt)]
    return []


def changes(nodes) -> tuple[dict[int, int], dict[int, Loc]]:
    """Per variable (by id): how many times it's assigned, and where it's first modified in place."""
    assigned, modified, _ = uses(nodes)
    return assigned, modified


def escapes(nodes) -> dict[int, Loc]:
    """Per variable (by id): where a list, dict or set it holds first escapes (is bound to
    another name, stored, returned or passed on), so something else could reach it."""
    return uses(nodes)[2]


def direct_changes(nodes) -> dict[int, Loc]:
    """Per variable (by id): where its value is first changed in place (directly, or through
    an alias like `for row in grid`), not counting it being passed on or stored."""
    return uses(nodes, direct=True)[1]


def changes_self(fn: FuncInfo) -> bool:
    """Does this method change its object (a field, a list in a field, another method that
    does)? Syntactic and conservative; used for @value classes' methods. (Cached on fn, like
    flow.escaping_params.)"""
    if (cached := getattr(fn, "_changes_self", None)) is not None:
        return cached
    fn._changes_self = False  # (recursion: assume not, while looking)
    result = False
    for n in walk_all(fn.node.body):
        target = None
        match n:
            case A.Assign(targets):
                result = result or any(isinstance(t, (A.Attribute, A.Index)) and base_name(t) == "self" for t in targets)
            case A.AugAssign(t):
                target = t
            case A.Call(A.Attribute(recv, attr)):
                kind = getattr(n.sym, "kind", None)
                if kind == "builtin_method" and attr in MUTATING_METHODS and base_name(recv) == "self":
                    result = True
                elif kind == "method" and base_name(recv) == "self" and n.sym.target is not fn:
                    result = result or changes_self(n.sym.target)
        if target is not None and base_name(target) == "self":
            result = True
        if result:
            break
    fn._changes_self = result
    return result


def base_name(e: A.Expr) -> str | None:
    while isinstance(e, (A.Attribute, A.Index)):
        e = e.value
    return e.id if isinstance(e, A.Name) else None


def uses(nodes, direct: bool = False) -> tuple[dict[int, int], dict[int, Loc], dict[int, Loc]]:
    """direct: count only changes made in place (not a list escaping somewhere) as modified."""
    assigned: dict[int, int] = {}
    modified: dict[int, Loc] = {}
    escaped: dict[int, Loc] = {}

    # `for row in grid`: row is one of grid's lists, so changing or keeping row reaches grid.
    aliases: dict[int, list[Var]] = {}

    def sources(var: Var) -> list[Var]:
        out, todo, seen = [], [var], set()
        while todo:
            v = todo.pop()
            if id(v) in seen:
                continue
            seen.add(id(v))
            out.append(v)
            todo.extend(aliases.get(id(v), []))
        return out

    looped: set[int] = set()  # the iterables of for loops and comprehensions: enumerate(grid)

    def targets_of(ts: list[A.Expr]) -> None:  # `a, b = ...`: a tuple of targets, not of values
        for t in ts:
            for n in walk(t):
                if isinstance(n, (A.TupleLit, A.ListLit)):
                    copied.add(id(n))

    def alias(targets: list[Var], iterable: A.Expr) -> None:
        looped.add(id(iterable))
        elem = element_type(iterable.ty) if iterable.ty is not None else None
        roots = [n.sym for n in walk(iterable) if isinstance(n, A.Name) and isinstance(n.sym, Var)
                 and (holds_references(n.sym.type) or (isinstance(n.sym.type, StructType) and n.sym.type.kind == "struct"))]
        if roots and (elem is None or holds_references(elem)):
            for v in targets:
                aliases.setdefault(id(v), []).extend(roots)

    def assign(var: Var | None) -> None:
        if var is not None:
            assigned[id(var)] = assigned.get(id(var), 0) + 1

    def modify(var: Var | None, loc: Loc) -> None:
        if var is not None:
            for v in sources(var):
                modified.setdefault(id(v), loc)

    copied: set[int] = set()  # literals handed straight to something that copies them: Thread(args=(xs,))

    def escape(e: A.Expr | None) -> None:
        """A list, dict or set used as a value (bound to another name, stored, returned,
        passed to a function) could be changed through that other reference: count it as
        modified."""
        if isinstance(e, (A.Name, A.Attribute, A.Index)) and e.ty is not None and holds_references(e.ty):
            if not direct:
                modify(base_var(e), e.loc)
            if (var := base_var(e)) is not None:
                for v in sources(var):
                    escaped.setdefault(id(v), e.loc)
        elif isinstance(e, A.IfExp):
            escape(e.body)
            escape(e.orelse)
        elif isinstance(e, A.BoolOp):
            escape(e.left)
            escape(e.right)

    def visit(n: A.Node, in_loop: bool) -> None:
        handle(n, in_loop)
        if isinstance(n, A.For):
            visit(n.iter, in_loop)
            for part in (n.target, *n.body, *n.orelse):
                visit(part, True)  # the target and body run once per iteration
            return
        if isinstance(n, A.While):
            visit(n.test, True)
            for part in (*n.body, *n.orelse):
                visit(part, True)
            return
        for c in children(n):
            visit(c, in_loop)

    def handle(n: A.Node, in_loop: bool) -> None:
        targets: list[A.Expr] = []
        match n:
            case A.Assign(ts, value):
                targets = ts
                targets_of(ts)
                escape(value)
            case A.AnnAssign(t, _, value):
                targets = [t]
                escape(value)
            case A.For(t, iterable):
                targets = [t]
                targets_of([t])
                alias(target_vars(t), iterable)
            case A.AugAssign(t):
                targets = [t]
            case A.ListComp(elt, gens) | A.SetComp(elt, gens) | A.GeneratorExp(elt, gens):
                for g in gens:
                    targets_of([g.target])
                    alias(target_vars(g.target), g.iter)
                escape(elt)
            case A.DictComp(_, value, gens):
                for g in gens:
                    targets_of([g.target])
                    alias(target_vars(g.target), g.iter)
                escape(value)
            case A.NamedExpr(t, value):
                targets = [t]
                escape(value)
            case A.Return(value) | A.Yield(value):
                escape(value)
            case A.ListLit(elts) | A.TupleLit(elts) | A.SetLit(elts) if id(n) not in copied:
                for elt in elts:
                    escape(elt)
            case A.DictLit(keys, values) if id(n) not in copied:
                for v in values:
                    escape(v)
            case A.WithItem(_, t) if t is not None:
                targets = [t]
            case A.MatchAs(_, t) | A.MatchStar(t) | A.MatchMapping(_, _, t) if t is not None:
                targets = [t]  # a pattern's capture assigns the name
            case A.Call(func, args):
                ct = n.sym
                kind = getattr(ct, "kind", None)
                reads_only = (
                    (kind == "builtin" and ct.target in READING_BUILTINS)
                    or (kind == "builtin_method" and isinstance(func, A.Attribute) and func.attr in READING_METHODS)
                    or kind in ("module_func", "sync_new", "collection_new", "isinstance")  # (these copy, or only read)
                    or (kind == "ctor" and isinstance(ct.target, StructType) and ct.target.kind == "struct")  # (copies)
                )
                if not reads_only:
                    for arg in [*args, *(k.value for k in n.keywords)]:
                        escape(arg)
                elif kind == "builtin" and ct.target in ITEM_BUILTINS and id(n) not in looped:
                    for arg in args:  # list(grid) shares grid's rows
                        if arg.ty is not None and (elem := element_type(arg.ty)) is not None and holds_references(elem):
                            escape(arg)
                elif kind != "builtin":  # (a built-in function returns a new value; these take a copy)
                    copied.update(id(a) for a in [*args, *(k.value for k in n.keywords)])
                if kind == "builtin_method" and isinstance(func, A.Attribute) and func.attr in MUTATING_METHODS:
                    modify(base_var(func.value), n.loc)
                elif kind == "method" and isinstance(func, A.Attribute):
                    owner = ct.target.owner
                    if owner is not None and owner.kind == "struct" and changes_self(ct.target):  # (it changes the value)
                        modify(base_var(func.value), n.loc)
                elif kind == "module_func" and args:
                    member = ct.target[0].members.get(ct.target[1])
                    if getattr(member, "mutates_first_arg", False):
                        modify(base_var(args[0]), n.loc)
        for t in targets:
            if isinstance(t, (A.Index, A.Attribute)):
                modify(base_var(t), t.loc)
            for v in target_vars(t):
                assign(v)
                if in_loop or isinstance(n, A.For):
                    assign(v)  # assigned again on every iteration
                if isinstance(n, A.AugAssign):
                    modify(v, t.loc)

    for root in nodes:
        visit(root, False)
    return assigned, modified, escaped


@dataclasses.dataclass
class _At:
    loc: Loc


def lost_change(body: list, var: Var) -> Loc | None:
    """Where `var` is first changed, if every mention of it only changes it (xs.append(1),
    xs[i] = v, xs += ys) and nothing ever reads it."""
    changing: set[int] = set()  # Name nodes that are the thing being changed
    first: Loc | None = None

    def base_name(e: A.Expr) -> A.Name | None:
        while isinstance(e, (A.Index, A.Attribute)):
            e = e.value
        return e if isinstance(e, A.Name) and e.sym is var else None

    for n in (x for stmt in body for x in walk(stmt)):
        name = None
        match n:
            case A.ExprStmt(A.Call(A.Attribute(recv, attr))) if n.value.sym is not None and (
                (n.value.sym.kind == "builtin_method" and attr in MUTATING_METHODS)
                or (n.value.sym.kind == "method" and getattr(n.value.sym.target.owner, "kind", None) == "struct")
            ):
                name = base_name(recv)
            case A.Assign(targets):
                for t in targets:
                    if isinstance(t, (A.Index, A.Attribute)) and (b := base_name(t)) is not None:
                        changing.add(id(b))
                        first = first or t.loc
            case A.AugAssign(t):
                name = base_name(t)
        if name is not None:
            changing.add(id(name))
            first = first or n.loc
    if first is None:
        return None
    for n in (x for stmt in body for x in walk(stmt)):
        if isinstance(n, A.Name) and n.sym is var and id(n) not in changing:
            return None  # read somewhere
    return first


@dataclasses.dataclass
class Program:
    """Everything the check needs from the checked modules."""

    units: list  # (A.Module, ModuleInfo, module name)

    def __post_init__(self) -> None:
        bodies = []
        for module, info, name in self.units:
            spawn_seen = False
            spawn_calls = {id(call) for call, _, _ in info.spawns}  # Thread(...), executor.submit/map, callbacks
            top = [s for s in module.body if not isinstance(s, (A.FunctionDef, A.ClassDef, A.Import, A.ImportFrom))]
            for stmt in top:
                # Module code that runs before any thread starts may build its globals freely.
                spawn_seen = spawn_seen or any(id(n) in spawn_calls for n in walk(stmt))
                if spawn_seen:
                    bodies.append(stmt)
            for fn in self.all_functions(info):
                bodies.extend(fn.node.body)
        self.global_assigned, self.global_modified = changes(bodies)

    @staticmethod
    def all_functions(info) -> list[FuncInfo]:
        out = list(info.functions)
        for st in info.structs:
            out.extend(st.methods.values())
        return out


def verify(units: list) -> None:
    """units: (A.Module, ModuleInfo, module name) for every module in the program."""
    program = Program(units)
    for module, info, name in units:
        for call, scope, _ in info.spawns:
            Spawn(program, call, scope, name).check()


class Spawn:
    def __init__(self, program: Program, call: A.Call, scope, module: str):
        self.program = program
        self.call = call
        self.scope = scope  # the FunctionScope the Thread(...) call is in
        self.module = module
        # threading.Thread(...) keeps its details in its call target; executor.submit()/map() on the call
        self.extra = getattr(call, "spawn_extra", None) or call.sym.target[1]

    def fail(self, message: str, node: A.Node) -> ThreadSafetyError:
        return ThreadSafetyError(message, node.loc, self.module)

    def check(self) -> None:
        args = self.extra["args"]
        if args is not None:
            for i, t in enumerate(args.ty.elts):
                if reason := unsendable(t):
                    node = args.elts[i] if isinstance(args, A.TupleLit) else args
                    raise self.fail(
                        f"can't pass this to a thread: {reason}. Threads receive copies of values, "
                        f"or thread-safe objects (Lock, Queue, Mutex, Atomic, Synchronized classes)", node,
                    )
        result = self.extra.get("result")
        if result is not None and (reason := unsendable(result)):
            raise self.fail(
                f"work on another thread can't return this: {reason}. Return values that can be copied, "
                f"or thread-safe objects", self.extra["target"],
            )
        roots, captured = self.roots(self.extra["target"])
        self.check_lost_changes()
        self.check_captures(captured, roots)
        self.check_reachable(roots)

    def check_lost_changes(self) -> None:
        """A thread gets its own copy of each argument. Changing that copy and never using it
        is almost certainly a mistake: the caller never sees the change."""
        if self.extra["args"] is None:
            return
        target = self.extra["target"]
        node = None
        if isinstance(target.sym, FuncInfo):
            node = target.sym.node
        elif isinstance(target.sym, Var) and isinstance(target.sym.type, FuncType):
            node = self.nested_def(target.sym)
        if not isinstance(node, A.FunctionDef):
            return
        fn = node.sym
        params = node.params[1:] if fn.owner is not None and fn.kind != "static" else node.params
        for param in params:
            var = param.sym
            if not (holds_references(var.type) or (isinstance(var.type, StructType) and var.type.kind == "struct")):
                continue
            if (loc := lost_change(node.body, var)) is not None:
                raise self.fail(
                    f"{fn.name}() changes its copy of '{var.name}' but never uses it: a thread gets its own copy of "
                    f"each argument, so the change never reaches the caller. Share the data with seadash.Mutex, "
                    f"send results back through a queue.Queue, or return them (ThreadPoolExecutor)", _At(loc),
                )

    def roots(self, target: A.Expr) -> tuple[list[A.Node], list[A.Node]]:
        """The code the thread starts in, and the closure bodies whose captures must be checked."""
        sym = target.sym
        if isinstance(target, A.Lambda):
            return [target.body], [target]
        if isinstance(sym, A.Lambda):  # a wrapped built-in, like target=print
            return [sym.body], []
        if isinstance(sym, FuncInfo):
            if sym.owner is not None:  # a bound method: obj.method
                obj_type = target.value.ty
                if (isinstance(obj_type, StructType) and obj_type.kind == "class" and not is_synchronized(obj_type)
                        and not deeply_immutable(obj_type)):
                    raise self.fail(
                        f"a thread can't run a method of a {obj_type.name}: the object would be shared by both "
                        f"threads. Make {obj_type.name} a seadash.Synchronized class", target,
                    )
            return list(sym.node.body), []
        if isinstance(sym, Var) and isinstance(sym.type, FuncType):
            fn = self.nested_def(sym)
            if fn is not None:
                return list(fn.body), [fn]
        raise self.fail(
            "pass the thread's function directly (a def, a nested def, or a lambda), "
            "so seadash can check what it shares", target,
        )

    def nested_def(self, var: Var) -> A.FunctionDef | None:
        body = self.scope.info.node.body if self.scope.info is not None else self.module_body()
        for n in (x for stmt in body for x in walk(stmt)):
            if isinstance(n, A.FunctionDef) and isinstance(n.sym, FuncInfo) and n.sym.var is var:
                return n
        return None

    def module_body(self) -> list:
        for module, _, name in self.program.units:
            if name == self.module:
                return module.body
        return []

    def check_captures(self, closures: list[A.Node], roots: list[A.Node]) -> None:
        """A closure run on another thread gets its own copy of the variables it uses from the
        enclosing function, made when the thread is created (codegen reads `snapshot`). So the
        enclosing function may go on changing them, but the thread changing its copy is lost.
        A recursive nested def can't be rebuilt around copies: it shares, so nothing may change."""
        if not closures:
            return
        closure = closures[0]
        recursive = isinstance(closure, A.FunctionDef) and any(
            isinstance(n, A.Call) and getattr(n.sym, "kind", None) == "self_call" and n.sym.target is closure.sym
            for n in walk_all(closure.body)
        )
        enclosing = self.scope.info.node.body if self.scope.info is not None else self.module_body()
        assigned, modified = changes(enclosing)
        if isinstance(closure, A.FunctionDef):
            # Rebuilt from copies only when the name means just this def (not `if a: def f()... else: def f()...`).
            fn_var = closure.sym.var
            defs = [n for n in walk_all(enclosing) if isinstance(n, A.FunctionDef) and n.sym.var is fn_var]
            recursive = recursive or len(defs) != 1 or assigned.get(id(fn_var), 0) > 0
        inside_assigned, _ = changes(roots)
        # (a lambda's body is an expression; its value is thrown away, like a statement's)
        body = [A.ExprStmt(r, loc=r.loc) if isinstance(r, A.Expr) else r for r in roots]
        own = {id(p.sym) for c in closures if isinstance(c, (A.Lambda, A.FunctionDef)) for p in c.params}
        snapshot: list[Var] = []
        for root in roots:
            for n in walk(root):
                if not (isinstance(n, A.Name) and isinstance(n.sym, Var)):
                    continue
                var = n.sym
                if var.kind not in ("local", "param") or id(var) in own or not var.captured:
                    continue
                if var not in snapshot:
                    snapshot.append(var)
                if shareable(var.type):
                    continue
                if reason := unsendable(var.type):
                    raise self.fail(
                        f"the thread's function uses '{var.name}' from the enclosing function, but {reason}. "
                        f"Pass it in args= instead, or use a thread-safe type "
                        f"(queue.Queue, seadash.Mutex, seadash.Atomic)", n,
                    )
                if inside_assigned.get(id(var), 0) or lost_change(body, var) is not None:
                    raise self.fail(
                        f"the thread's function changes '{var.name}', but a thread works on its own copy of the "
                        f"variables it uses from the enclosing function (made when the thread is created), so the "
                        f"change is lost. Share it with seadash.Mutex, or send results back through a queue.Queue",
                        n,
                    )
                if recursive and (assigned.get(id(var), 0) > (0 if var.kind == "param" else 1) or id(var) in modified):
                    raise self.fail(
                        f"the thread's function uses '{var.name}' from the enclosing function, but the enclosing "
                        f"function changes '{var.name}' (and a recursive nested def, or one defined more than once, "
                        f"shares it rather than getting a copy). Pass it in args= instead, or use a thread-safe "
                        f"type (queue.Queue, seadash.Mutex, seadash.Atomic)", n,
                    )
        if not recursive and snapshot:  # (nothing captured: nothing to copy)
            closure.snapshot = snapshot
            target = self.extra["target"]
            if isinstance(closure, A.FunctionDef) and isinstance(target, A.Name):
                target.snapshot_of = closure  # codegen: build the thread's closure from copies

    def check_reachable(self, roots: list[A.Node]) -> None:
        """Follow calls from the thread's code; every global it touches must be safe to share."""
        seen: set[int] = set()
        todo = list(roots)
        while todo:
            root = todo.pop()
            for n in walk(root):
                if isinstance(n, (A.Name, A.Attribute)) and isinstance(n.sym, Var) and n.sym.kind == "global":
                    self.check_global(n.sym, n)
                if isinstance(n, A.Call):
                    for fn in callees(n):
                        if id(fn) not in seen:
                            seen.add(id(fn))
                            todo.extend(fn.node.body)

    def check_global(self, var: Var, use: A.Node) -> None:
        if shareable(var.type):
            return
        reason = unsendable(var.type)
        loc = self.program.global_modified.get(id(var))
        reassigned = self.program.global_assigned.get(id(var), 0) > 0
        if reason is None and loc is None and not reassigned:
            return  # a value nothing changes once threads can run: reading it is fine
        why = reason or (f"it's modified (line {loc.line})" if loc else "it's reassigned")
        raise self.fail(
            f"thread code uses the module-level '{var.name}' ({var.type}), but {why}. Threads can't share "
            f"it safely: wrap it in a seadash.Mutex, send data through a queue.Queue, or pass a copy in args=", use,
        )


def callees(call: A.Call) -> list[FuncInfo]:
    ct = call.sym
    kind = getattr(ct, "kind", None)
    if kind in ("func", "method", "self_call", "super_method"):
        return [ct.target]
    if kind == "ctor":
        init = ct.target.init
        return [init] if init is not None else []
    if kind == "super_init":
        init = ct.target.init
        return [init] if init is not None else []
    return []
