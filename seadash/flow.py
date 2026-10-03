"""Statement-level flow questions about a function body, shared by the checker and codegen:
is this the last use of a variable (so a list can be moved to another thread), and does a
change to a copy ever get read (a change that's dropped is a mistake)? Also where a
@value class's list is used as a value, so it must be copied rather than shared.

All of it is conservative: loops, jumps and exception handlers count as "might read it".
"""

from __future__ import annotations

from . import ast as A
from .types import StructType, Var, element_type


def walk(node):
    """Every node under `node` (a node or a list of them), including itself."""
    if isinstance(node, list):
        for n in node:
            yield from walk(n)
        return
    yield node
    for f in node.__dataclass_fields__:
        if f in ("loc", "sym", "ty", "dunder"):
            continue
        value = getattr(node, f)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, A.Node):
                yield from walk(item)


# ---- last use: when handing a list to another thread can move it instead of copying ----


def sub_blocks(stmt: A.Stmt) -> list[list[A.Stmt]]:
    match stmt:
        case A.If(_, body, orelse) | A.While(_, body, orelse) | A.For(_, _, body, orelse):
            return [body, orelse]
        case A.With(_, body):
            return [body]
        case A.Try(body, handlers, orelse, finalbody):
            return [body, *(h.body for h in handlers), orelse, finalbody]
        case A.Match(_, cases):
            return [c.body for c in cases]
    return []  # (a nested def's body belongs to another function)


def statement_path(block: list[A.Stmt], node: A.Node) -> list[tuple[list[A.Stmt], int]] | None:
    """The blocks and statement indexes leading from `block` down to the innermost
    statement containing `node`."""
    for i, stmt in enumerate(block):
        for sub in sub_blocks(stmt):
            if (path := statement_path(sub, node)) is not None:
                return [(block, i), *path]
        if not isinstance(stmt, A.FunctionDef) and any(n is node for n in walk(stmt)):
            return [(block, i)]
    return None


def mentions(node: A.Node, var: Var) -> int:
    return sum(1 for n in walk(node) if isinstance(n, A.Name) and n.sym is var)


def jumps(stmt: A.Stmt) -> bool:
    """Could this statement leave its block early (other than by returning)?"""
    return any(isinstance(n, (A.Break, A.Continue, A.Raise)) for n in walk(stmt))


def rebinds(stmt: A.Stmt, var: Var) -> bool:
    """`x = ...` / `x: T = ...` replacing var's value without reading it."""
    match stmt:
        case A.Assign([A.Name() as t], value) | A.AnnAssign(A.Name() as t, _, value) if value is not None:
            return t.sym is var and not mentions(value, var)
    return False


def unread_after(body: list[A.Stmt], node: A.Node, var: Var) -> bool:
    """After the statement containing `node`, is `var` never read again: no mention, or it's
    given a new value first (including by a loop header, `for p in ps`)? Conservative:
    loops, jumps and exception handlers that could reach another read say no."""
    path = statement_path(body, node)
    if path is None:
        return False
    for depth in range(len(path) - 1, -1, -1):
        block, i = path[depth]
        for stmt in block[i + 1:]:
            if mentions(stmt, var):
                return rebinds(stmt, var)  # `batch = []`: the old value is never read again
            if jumps(stmt):
                return False  # (a continue, break or exception could reach a read elsewhere)
        if depth > 0:
            outer, j = path[depth - 1]
            owner = outer[j]
            if isinstance(owner, (A.For, A.While)):
                # The next iteration reads it, unless the loop gives it a new value first.
                if block is not owner.body or any(mentions(s, var) for s in owner.orelse):
                    return False
                if isinstance(owner, A.While):
                    if mentions(owner.test, var):
                        return False
                    by_header = False
                else:
                    by_header = isinstance(owner.target, A.Name) and owner.target.sym is var
                    if mentions(owner.iter, var) or (mentions(owner.target, var) and not by_header):
                        return False
                if not by_header:
                    before = next((s for s in owner.body[:i] if mentions(s, var)), None)
                    if before is None or not rebinds(before, var):
                        return False
            if isinstance(owner, A.Try) and any(mentions(s, var) for s in [*owner.orelse, *owner.finalbody]
                                                 + [s for h in owner.handlers for s in h.body]):
                return False
    return True


def last_use(body: list[A.Stmt], node: A.Name) -> bool:
    """Is `node` the last time its (local) variable is read (so its list can be moved)?"""
    var = node.sym
    path = statement_path(body, node)
    if path is None:
        return False
    block, i = path[-1]
    if mentions(block[i], var) != 1 or isinstance(block[i], (A.For, A.While)):
        return False  # read again in the same statement, or in a loop header
    return unread_after(body, node, var)




# ---- a @value class's lists never leak out shared ----------------------------------------


def into_value(e: A.Expr) -> bool:
    """Does e reach into a @value class: `s.items`, `s.grid[0]`, `self.items`?"""
    while isinstance(e, (A.Attribute, A.Index)):
        if (
            isinstance(e, A.Attribute) and isinstance(e.value.ty, StructType) and e.value.ty.kind == "struct"
            and not isinstance(e.sym, tuple) and hasattr(e.sym, "type")  # a field (not a property or method)
        ):
            return True
        e = e.value
    return False


def into_frozen(e: A.Expr) -> bool:
    """Does e reach into a frozen @value class (whose lists mustn't change)?"""
    while isinstance(e, (A.Attribute, A.Index)):
        owner = e.value.ty if isinstance(e, A.Attribute) else None
        if isinstance(owner, StructType) and owner.kind == "struct" and owner.frozen:
            return True
        e = e.value
    return False


# Analyses of a function are cached on the function itself. (Not in a dict by id(fn): once a
# program's functions are freed, another program's can get the same ids, and its stale answers.)


def escaping_params(fn) -> set[int]:
    """The parameters (by Var id) a function keeps: stores, returns, captures or passes on."""
    from . import threads

    if (cached := getattr(fn, "_escaping_params", None)) is None:
        params = [p.sym for p in fn.node.params]
        if fn.generator:  # (a generator keeps everything it's given until it's done)
            cached = {id(v) for v in params}
        else:
            escaped = threads.escapes(fn.node.body)
            cached = {id(v) for v in params if id(v) in escaped or v.captured}
        fn._escaping_params = cached
    return cached


def mark_copy_outs(body) -> None:
    """Mark (copy_out) each place a @value class's list is used as a value (bound to a name,
    returned, stored, passed to something that keeps it), so codegen copies it there. Used
    in place (s.items.append(x), for x in s.items, fill(s.items)) it's the class's own."""
    from . import threads

    def mark(e: A.Expr | None) -> None:
        match e:
            case A.IfExp(_, body_, orelse):
                mark(body_)
                mark(orelse)
            case A.BoolOp(_, left, right):
                mark(left)
                mark(right)
            case A.Attribute() | A.Index() if e.ty is not None and threads.holds_references(e.ty) and into_value(e):
                e.notes["copy_out"] = True

    targets: set[int] = set()  # `a, b = ...`: tuples of targets, not of values
    for n in walk(body):
        if isinstance(n, (A.Assign, A.For)):
            for t in (n.targets if isinstance(n, A.Assign) else [n.target]):
                targets.update(id(x) for x in walk(t))
        elif isinstance(n, A.Comprehension):
            targets.update(id(x) for x in walk(n.target))

    for n in walk(body):
        match n:
            case A.Assign(_, value) | A.AnnAssign(_, _, value) | A.NamedExpr(_, value):
                mark(value)
            case A.Return(value) | A.Yield(value):
                mark(value)
            case A.Lambda(_, body_):
                mark(body_)
            case A.ListLit(elts) | A.TupleLit(elts) | A.SetLit(elts) if id(n) not in targets:
                for elt in elts:
                    mark(elt)
            case A.DictLit(_, values):
                for v in values:
                    mark(v)
            case A.ListComp(elt) | A.SetComp(elt) | A.GeneratorExp(elt):
                mark(elt)
            case A.DictComp(_, value):
                mark(value)
            case A.Call(func, args):
                mark_call(n, func, args, mark, threads)


def mark_call(n: A.Call, func: A.Expr, args: list, mark, threads) -> None:
    ct = n.sym
    kind = getattr(ct, "kind", None)
    everything = [*args, *(k.value for k in n.keywords)]
    if kind in ("func", "method", "self_call", "static_method", "class_func", "super_method", "base_method", "super_init", "ctor"):
        fn = ct.target
        if kind in ("ctor", "super_init"):
            st = ct.target
            if st.kind == "struct":
                return  # (a @value class copies what it's given)
            fn = st.init
            if fn is None:  # a dataclass-style constructor keeps every argument
                for a in everything:
                    mark(a)
                return
        if getattr(fn, "node", None) is None:
            return  # a built-in (date.fromisoformat...): reads its arguments
        keeps = escaping_params(fn)
        params = fn.node.params[1:] if fn.owner is not None and fn.kind != "static" else fn.node.params
        slots = ct.args if ct.args is not None else args  # (one per parameter, in order)
        for param, arg in zip(params, slots):
            if arg is not None and (id(param.sym) in keeps or into_frozen(arg)):
                mark(arg)  # (a frozen value's list is never handed over to be changed)
    elif kind == "builtin":
        if ct.target in threads.ITEM_BUILTINS:
            for a in args:  # list(s.grid): the new list would share s's rows
                if a.ty is not None and (elem := element_type(a.ty)) is not None and threads.holds_references(elem):
                    mark(a)
    elif kind == "builtin_method":
        if not (isinstance(func, A.Attribute) and func.attr in threads.READING_METHODS):
            for a in everything:  # (other.append(s.items) would keep it)
                mark(a)
    elif kind in ("module_func", "sync_new", "collection_new", "isinstance"):
        return  # (these read, or copy)
    else:  # a call through a function value: it might keep anything
        for a in everything:
            mark(a)


# ---- a change to a copy that's never read ------------------------------------------------


def expr_text(e: A.Expr) -> str:
    """Short source text for messages: `ps[i]`, `team.members`."""
    match e:
        case A.Name(name):
            return name
        case A.Attribute(value, attr):
            return f"{expr_text(value)}.{attr}"
        case A.Index(value, index):
            if isinstance(index, (A.Name, A.Attribute, A.Index)):
                inner = expr_text(index)
            elif isinstance(index, (A.IntLit, A.StrLit)):
                inner = repr(index.value)
            else:
                inner = "..."
            return f"{expr_text(value)}[{inner}]"
    return "..."


def base_var(e: A.Expr) -> Var | None:
    while isinstance(e, (A.Attribute, A.Index)):
        e = e.value
    return e.sym if isinstance(e, A.Name) and isinstance(e.sym, Var) else None


def is_value_class(t) -> bool:
    return isinstance(t, StructType) and t.kind == "struct"


def copied_from(value: A.Expr | None) -> bool:
    """Does binding a name to `value` make a copy: a @value class read out of a container or
    a field, or a @value class's list?"""
    from . import threads

    return isinstance(value, (A.Index, A.Attribute)) and value.ty is not None and (
        is_value_class(value.ty) or (threads.holds_references(value.ty) and into_value(value))
    )


def dropped_changes(params: list, body: list[A.Stmt]):
    """(statement, var, description) for each change to a copy that's never read afterwards.
    Copies: @value parameters, names bound to a copy (p = ps[i], xs = s.items), and the
    variable of `for p in ps` over @value classes."""
    from . import threads

    copies: dict[int, tuple[Var, str]] = {}
    fresh: set[int] = set()  # names also bound to something that isn't a copy: left alone
    for p in params:
        if p.sym is not None and is_value_class(p.sym.type):
            copies[id(p.sym)] = (p.sym, f"the function's own copy of the caller's {p.sym.type.name}")
    for n in walk(body):
        match n:
            case A.Assign([A.Name() as t], value) | A.AnnAssign(A.Name() as t, _, value) if isinstance(t.sym, Var):
                if copied_from(value):
                    copies.setdefault(id(t.sym), (t.sym, f"a copy of {expr_text(value)}"))
                elif value is not None:
                    fresh.add(id(t.sym))
            case A.For(A.Name() as t, iterable) if isinstance(t.sym, Var):
                if iterable.ty is not None and is_value_class(element_type(iterable.ty)):
                    copies.setdefault(id(t.sym), (t.sym, f"a copy of an item of {expr_text(iterable)} (looping over "
                                                         f"@value classes gives copies)"))
                else:
                    fresh.add(id(t.sym))
            case A.Assign(targets) | A.For(targets):
                for t in (targets if isinstance(targets, list) else [targets]):
                    if isinstance(t, (A.TupleLit, A.ListLit)):  # (unpacking: not tracked)
                        fresh.update(id(x.sym) for x in walk(t) if isinstance(x, A.Name) and isinstance(x.sym, Var))
    for key, (var, what) in copies.items():
        if key in fresh or var.captured or var.name == "self":
            continue
        for stmt in changes_to(body, var, threads):
            if unread_after(body, stmt, var):
                yield stmt, var, what
                break


def changes_to(body: list[A.Stmt], var: Var, threads):
    """The simple statements that change var's value in place."""
    for n in walk(body):
        match n:
            case A.ExprStmt(A.Call(A.Attribute(recv, attr))) if base_var(recv) is var:
                kind = getattr(n.value.sym, "kind", None)
                if (kind == "builtin_method" and attr in threads.MUTATING_METHODS) or (
                    kind == "method" and getattr(n.value.sym.target.owner, "kind", None) == "struct"
                ):
                    yield n
            case A.Assign(targets) if any(isinstance(t, (A.Attribute, A.Index)) and base_var(t) is var for t in targets):
                yield n
            case A.AugAssign(target) if base_var(target) is var or (isinstance(target, A.Name) and target.sym is var):
                yield n


# ---- looping without copying each item ------------------------------------------------


def _fresh_local(var: Var, body_of_function: list) -> bool:
    """Is every value ever bound to var a newly built list/dict/set (so it can't be another
    list under a different name)?"""
    for n in walk(body_of_function):
        match n:
            case A.Assign(targets, value) if any(isinstance(t, A.Name) and t.sym is var for t in targets):
                if not isinstance(value, (A.ListLit, A.ListComp, A.DictLit, A.DictComp, A.SetLit, A.SetComp)):
                    return False
            case A.AnnAssign(A.Name() as t, _, value) if t.sym is var:
                if value is not None and not isinstance(value, (A.ListLit, A.ListComp, A.DictLit, A.DictComp, A.SetLit, A.SetComp)):
                    return False
            case A.For(target) | A.NamedExpr(target) if any(isinstance(x, A.Name) and x.sym is var for x in walk(target)):
                return False
    return var.kind == "local"


def harmless_method(fn) -> bool:
    """Does this method (and what it calls) change nothing but its own locals, and call only
    built-ins? Then a loop can call it without the list it's looping over changing."""
    from . import threads

    if (cached := getattr(fn, "_harmless", None)) is not None:
        return cached
    fn._harmless = False  # (while looking: recursion isn't harmless)
    ok = getattr(fn, "node", None) is not None and not fn.generator
    if ok:
        for n in walk(fn.node.body):
            if isinstance(n, (A.FunctionDef, A.Lambda, A.Global, A.Nonlocal, A.Yield)):
                ok = False
            elif isinstance(n, (A.Assign, A.AugAssign)):
                targets = n.targets if isinstance(n, A.Assign) else [n.target]
                ok = ok and all(isinstance(t, A.Name) and getattr(t.sym, "kind", None) == "local" for t in targets)
            elif isinstance(n, A.Call):
                kind = getattr(n.sym, "kind", None)
                if kind == "builtin":
                    continue
                if kind == "builtin_method" and isinstance(n.func, A.Attribute) and n.func.attr not in threads.MUTATING_METHODS:
                    continue
                if kind in ("method", "self_call") and harmless_method(n.sym.target):
                    continue
                ok = False
            if not ok:
                break
    fn._harmless = ok
    return ok


def loop_by_reference(fn_body: list, loop: A.For) -> bool:
    """Can `for x in xs:` bind x to each item in place instead of copying it? Only when
    nothing can tell: the body doesn't change or keep x, x isn't read after the loop, and
    nothing in the body can grow xs (which would move its items)."""
    from . import threads

    target, iterable = loop.target, loop.iter
    if not (isinstance(target, A.Name) and isinstance(target.sym, Var) and isinstance(iterable, A.Name)
            and isinstance(iterable.sym, Var)):
        return False
    var, seq = target.sym, iterable.sym
    elem = element_type(seq.type) if seq.type is not None else None
    if var.captured or seq.captured or seq.kind not in ("local", "param") or elem is None or var.type != elem:
        return False
    from .types import BOOL, FLOAT, INT, ListType
    if not isinstance(seq.type, ListType) or elem in (INT, FLOAT, BOOL):
        return False  # (only lists; scalars are as cheap to copy as to refer to)
    body = loop.body
    if any(mentions(s, var) for s in loop.orelse) or mentions(body, seq):
        return False
    assigned, _, _ = threads.uses(body)
    if assigned.get(id(var), 0) or (is_value_class(var.type) and id(var) in threads.direct_changes(body)):
        return False
    for n in walk(body):
        if isinstance(n, (A.FunctionDef, A.Lambda, A.Yield, A.Global, A.Nonlocal)):
            return False
        if isinstance(n, A.Call):
            kind = getattr(n.sym, "kind", None)
            if kind == "builtin":
                continue
            if kind == "builtin_method" and isinstance(n.func, A.Attribute):
                if n.func.attr not in threads.MUTATING_METHODS:
                    continue
                recv = n.func.value
                owner = base_var(recv)
                if recv.ty != seq.type or (owner is not None and owner is not seq and _fresh_local(owner, fn_body)):
                    continue  # (a list of another type, or one built here, can't be xs)
                return False
            if kind == "ctor" and n.sym.target.init is None:
                continue  # (a constructor that only stores its fields)
            if kind == "method" and harmless_method(n.sym.target):
                continue
            if kind == "module_func" and not getattr(n.sym.target[0].members.get(n.sym.target[1]), "mutates_first_arg", False):
                continue
            return False
    return unread_after(fn_body, loop, var)
