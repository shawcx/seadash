"""Thread safety: seadash runs threads in parallel (no global lock), so the checker
proves they can't race on memory.

The rule: a thread may only reach
  * values it received as copies (arguments to Thread(...), items from a Queue), and
  * thread-safe objects: Lock, RLock, Event, Queue[T], Mutex[T], Atomic, Thread,
    and instances of threading.Synchronized classes (their methods hold a lock).

Values are copied when they cross into a thread (lists, dicts, structs... are values in
seadash), so the only ways to *share* memory are class instances, closure-captured
variables, and module globals. This module checks each Thread(...) call:

  * its args must be sendable (copyable values or thread-safe objects);
  * its target is followed through every function/method it can call, and every module
    global it touches must be thread-safe, or a sendable value that nothing modifies;
  * if the target is a closure, each captured variable must be thread-safe, or a
    sendable value the enclosing function never changes.

Races on *logic* (check-then-act) are still possible, as in any language; races on
memory are not.
"""

from __future__ import annotations

import dataclasses

from . import ast as A
from .errors import CheckError, Loc
from .types import (
    BOOL, BYTES, FLOAT, INT, JSON_VALUE, NONE, PATH, SOCKET, STR, DATETIME_TYPES,
    DefaultDictType, DequeType, DictType, MatchType, PatternType, ProcessType, FuncInfo, FuncType, ListType, OptionalType, SetType, StructType, SyncType, TupleType, Type, Var,
)

MUTATING_METHODS = frozenset(
    "append insert pop remove extend sort reverse clear update setdefault add discard "
    "appendleft popleft extendleft rotate subtract".split()
)


class ThreadSafetyError(CheckError):
    def __init__(self, message: str, loc: Loc, module: str):
        super().__init__(message, loc)
        self.module = module


def is_synchronized(st: StructType) -> bool:
    return any(t.builtin and t.name == "Synchronized" for t in st.ancestors())


def unsendable(t: Type, seen: frozenset = frozenset()) -> str | None:
    """Why a value of type `t` can't be handed to another thread, or None if it can."""
    match t:
        case _ if t in (INT, FLOAT, BOOL, STR, BYTES, NONE, JSON_VALUE, PATH, *DATETIME_TYPES):
            return None
        case SyncType():
            return None
        case _ if t == SOCKET:
            return None
        case PatternType() | MatchType():  # immutable (a Match holds its own copy of the string)
            return None
        case ProcessType(kind="CompletedProcess"):
            return None
        case ProcessType():
            return "a Popen (its pipes aren't safe to use from two threads; pass what you read from it)"
        case ListType(x) | SetType(x) | OptionalType(x) | DequeType(x):
            return unsendable(x, seen)
        case DefaultDictType():
            return "a defaultdict (its factory function could share variables it captured; pass dict(d) instead)"
        case DictType(k, v):
            return unsendable(k, seen) or unsendable(v, seen)
        case TupleType(xs):
            return next((r for x in xs if (r := unsendable(x, seen))), None)
        case StructType() if t.kind == "struct":
            if t in seen:
                return None
            return next((r for f in t.all_fields().values() if (r := unsendable(f.type, seen | {t}))), None)
        case StructType() if is_synchronized(t):
            return None
        case StructType():
            return f"a {t.name} is a class instance, shared by reference (make it a threading.Synchronized class)"
        case FuncType():
            return "a function value (it could share variables it captured)"
    return f"a {t} can't be shared between threads"


def shareable(t: Type) -> bool:
    """Safe to access from several threads at once without copying."""
    return isinstance(t, SyncType) or t in (JSON_VALUE, SOCKET) or (isinstance(t, StructType) and is_synchronized(t))


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
    assigned: dict[int, int] = {}
    modified: dict[int, Loc] = {}

    def assign(var: Var | None) -> None:
        if var is not None:
            assigned[id(var)] = assigned.get(id(var), 0) + 1

    def modify(var: Var | None, loc: Loc) -> None:
        if var is not None:
            modified.setdefault(id(var), loc)

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
            case A.Assign(ts):
                targets = ts
            case A.AnnAssign(t) | A.AugAssign(t) | A.For(t):
                targets = [t]
            case A.NamedExpr(t):
                targets = [t]
            case A.WithItem(_, t) if t is not None:
                targets = [t]
            case A.Call(func, args):
                ct = n.sym
                kind = getattr(ct, "kind", None)
                if kind == "builtin_method" and isinstance(func, A.Attribute) and func.attr in MUTATING_METHODS:
                    modify(base_var(func.value), n.loc)
                elif kind == "method" and isinstance(func, A.Attribute):
                    owner = ct.target.owner
                    if owner is not None and owner.kind == "struct":  # a struct method may change the value
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
    return assigned, modified


@dataclasses.dataclass
class Program:
    """Everything the check needs from the checked modules."""

    units: list  # (A.Module, ModuleInfo, module name)

    def __post_init__(self) -> None:
        bodies = []
        for module, info, name in self.units:
            spawn_seen = False
            top = [s for s in module.body if not isinstance(s, (A.FunctionDef, A.ClassDef, A.Import, A.ImportFrom))]
            for stmt in top:
                # Module code that runs before any thread starts may build its globals freely.
                spawn_seen = spawn_seen or any(isinstance(n, A.Call) and getattr(n.sym, "kind", None) == "sync_new"
                                               and n.sym.target[0].kind == "Thread" for n in walk(stmt))
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
        _, self.extra = call.sym.target

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
        roots, captured = self.roots(self.extra["target"])
        self.check_captures(captured, roots)
        self.check_reachable(roots)

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
                if isinstance(obj_type, StructType) and obj_type.kind == "class" and not is_synchronized(obj_type):
                    raise self.fail(
                        f"a thread can't run a method of a {obj_type.name}: the object would be shared by both "
                        f"threads. Make {obj_type.name} a threading.Synchronized class", target,
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
        if not closures:
            return
        enclosing = self.scope.info.node.body if self.scope.info is not None else self.module_body()
        assigned, modified = changes(enclosing)
        own = {id(p.sym) for c in closures if isinstance(c, (A.Lambda, A.FunctionDef)) for p in c.params}
        for root in roots:
            for n in walk(root):
                if not (isinstance(n, A.Name) and isinstance(n.sym, Var)):
                    continue
                var = n.sym
                if var.kind not in ("local", "param") or id(var) in own or not var.captured:
                    continue
                if shareable(var.type):
                    continue
                reason = unsendable(var.type)
                changed = assigned.get(id(var), 0) > (0 if var.kind == "param" else 1) or id(var) in modified
                if reason or changed:
                    why = reason or f"the enclosing function changes '{var.name}'"
                    raise self.fail(
                        f"the thread's function uses '{var.name}' from the enclosing function, but {why}. "
                        f"Pass it in args= instead (the thread gets its own copy), or use a thread-safe type "
                        f"(queue.Queue, threading.Mutex, threading.Atomic)", n,
                    )

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
            f"it safely: wrap it in a threading.Mutex, send data through a queue.Queue, or pass a copy in args=", use,
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
