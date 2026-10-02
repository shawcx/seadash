"""`select` and `selectors`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from .. import ast as A
from ..builtins import (
    CallContext, EXCEPTIONS, FILE_LIKE, Function, Module, module_type, module_with_params, MODULES, OPT_FLOAT,
    runtime_module, SELECTABLE, selectable, signature, strip_optional_type, sync_method, Value,
)
from ..errors import CheckError
from ..types import (
    assignable, BINARY_FILE, DictType, INT, join, ListType, NONE, OptionalType, POLL, SelectorKeyType, SelectorSlot,
    SelectorType, SOCKET, TEXT_FILE, TupleType, Type,
)


def select_select(ctx: CallContext) -> Type:
    """select.select(rlist, wlist, xlist[, timeout]): the ready items of each list, in its
    order. A list holds one type (sockets, files or ints); an empty `[]` takes its neighbours'."""
    if ctx.call.keywords:
        raise ctx.error("select.select() takes no keyword arguments", ctx.call.keywords[0])
    n = ctx.arity(3, 4)
    elem = None
    for i in range(3):
        if not (isinstance(ctx.args[i], A.ListLit) and not ctx.args[i].elts):
            t = ctx.arg(i)
            if not isinstance(t, ListType) or not selectable(t.elem):
                raise ctx.error(f"select() takes lists of sockets, files or file descriptors (ints), not {t}", ctx.args[i])
            elem = elem or t.elem
    lists = [ctx.arg(i, ListType(elem or INT)) if ctx.args[i].ty is None else ctx.args[i].ty for i in range(3)]
    if n == 4 and not assignable(t := ctx.arg(3, OPT_FLOAT), OPT_FLOAT):
        raise ctx.error(f"select() timeout must be a number of seconds or None, not {t}", ctx.args[3])
    return TupleType(tuple(lists))


POLL.methods.update({
    "register": sync_method(NONE, ("fd", FILE_LIKE), ("eventmask", INT, "static_cast<std::int64_t>(POLLIN | POLLPRI | POLLOUT)")),
    "modify": sync_method(NONE, ("fd", FILE_LIKE), ("eventmask", INT)),
    "unregister": sync_method(NONE, ("fd", FILE_LIKE)),
    "poll": sync_method(ListType(TupleType((INT, INT))), ("timeout", OPT_FLOAT, "std::nullopt")),
})
POLL_CONSTANTS = ["POLLIN", "POLLPRI", "POLLOUT", "POLLERR", "POLLHUP", "POLLNVAL", "POLLRDNORM", "POLLRDBAND",
                  "POLLWRNORM", "POLLWRBAND"]
MODULES["select"] = module_with_params(runtime_module(
    "select", "modules/select.hpp",
    select=(select_select, "sd::select::select"),
    poll=(signature(POLL), "sd::select::Poll"),
    **{c: (INT, f"static_cast<std::int64_t>({c})") for c in POLL_CONSTANTS},
))
MODULES["select"].members["select"].params = (
    ("rlist", FILE_LIKE), ("wlist", FILE_LIKE), ("xlist", FILE_LIKE), ("timeout", OPT_FLOAT, "std::nullopt"))
MODULES["select"].members["poll"].as_type = POLL
MODULES["select"].members["error"] = EXCEPTIONS["OSError"]  # select.error is OSError
SELECTOR_EXAMPLE = "`sel: selectors.DefaultSelector[socket.socket, int] = selectors.DefaultSelector()`"


def selector_type(checker, args: list[Type], node: A.Node) -> Type:
    """selectors.DefaultSelector[F, D] in an annotation: watches F objects, each registered with
    a D (DefaultSelector[F] has no data: it's None). Bare, its types come from its first register()."""
    if not args:
        return SelectorType()
    if len(args) > 2:
        raise CheckError("a selector takes the type of what it watches and of the data registered with it, "
                         "e.g. DefaultSelector[socket.socket, int] (DefaultSelector[socket.socket] has no data)", node.loc)
    if not selectable(args[0]):
        raise CheckError(f"a selector watches sockets, files or file descriptors (ints), not {args[0]}", node.loc)
    return SelectorType(SelectorSlot(args[0], args[1] if len(args) == 2 else NONE))


def selector_key_type(checker, args: list[Type], node: A.Node) -> Type:
    """selectors.SelectorKey[F, D]: what a DefaultSelector[F, D] gives for each registration."""
    if not 1 <= len(args) <= 2 or not selectable(args[0]):
        raise CheckError("SelectorKey takes the types its selector has, e.g. SelectorKey[socket.socket, int] "
                         "(SelectorKey[socket.socket] has no data)", node.loc)
    return SelectorKeyType(args[0], args[1] if len(args) == 2 else NONE)


def new_selector(ctx: CallContext) -> Type:
    """DefaultSelector(), PollSelector(), SelectSelector(): its types come from the annotation, or
    from its first register()."""
    ctx.arity(0)
    want = strip_optional_type(ctx.expected) if ctx.expected is not None else None
    return want if isinstance(want, SelectorType) else SelectorType()


def abstract_selector(ctx: CallContext) -> Type:
    raise ctx.error("BaseSelector is abstract (it's for annotations): make a selectors.DefaultSelector()")


def no_key_constructor(ctx: CallContext) -> Type:
    raise ctx.error("a SelectorKey comes from a selector: register(), get_key(), select() or get_map()")


def need_known(ctx: CallContext) -> SelectorType:
    sel: SelectorType = ctx.receiver
    if not sel.known:
        raise ctx.error("can't tell yet what this selector watches: register something with it first, or "
                        f"annotate it, e.g. {SELECTOR_EXAMPLE}")
    return sel


def decided_by(slot: SelectorSlot) -> str:
    return f" (as its first register() on line {slot.decided_at.line} decided)" if slot.decided_at else ""


def selector_annotation(fileobj: Type, data: Type) -> str:
    def spelled(t: Type) -> str:
        return {SOCKET: "socket.socket", TEXT_FILE: "TextIO", BINARY_FILE: "BinaryIO"}.get(t, str(t))
    return f"`sel: selectors.DefaultSelector[{spelled(fileobj)}, {data}] = selectors.DefaultSelector()`"


def selector_register(modify: bool):
    """sel.register(fileobj, events, data=None) and sel.modify(fileobj, events, data=None).
    A selector whose types aren't known yet takes them from its first register()."""
    params = (("fileobj", lambda r: r.fileobj), ("events", INT), ("data", lambda r: r.data, "std::nullopt"))
    names = [p[0] for p in params]

    def handler(ctx: CallContext) -> Type:
        if len(ctx.args) > 3:
            raise ctx.error(f"{ctx.what} takes at most 3 arguments ({len(ctx.args)} given)")
        for kw in ctx.call.keywords:
            if kw.name not in names:
                raise ctx.error(f"{ctx.what} got an unexpected keyword argument '{kw.name}'", kw)
            if names.index(kw.name) < len(ctx.args):
                raise ctx.error(f"{ctx.what} got multiple values for argument '{kw.name}'", kw)
        fnode, enode, dnode = (ctx.args[i] if i < len(ctx.args) else ctx.keyword_arg(name) for i, name in enumerate(names))
        for node, name in ((fnode, "fileobj"), (enode, "events")):
            if node is None:
                raise ctx.error(f"{ctx.what} is missing argument '{name}'")
        sel = need_known(ctx) if modify else ctx.receiver
        slot = sel.slot.root()
        if slot.fileobj is None:  # the first register(): it decides
            ft = ctx.checker.check_expr(fnode)
            if not selectable(ft):
                raise ctx.error(f"{ctx.what} argument 'fileobj' must be {SELECTABLE}, not {ft}", fnode)
            dt = NONE if dnode is None else ctx.checker.check_expr(dnode)
            slot.decide(ft, dt, ctx.call.loc)
        else:
            ft = ctx.checker.check_expr(fnode, slot.fileobj)
            if not assignable(ft, slot.fileobj):
                raise ctx.error(f"this selector watches {slot.fileobj} objects, not {ft}{decided_by(slot)}", fnode)
            if dnode is None:
                if not assignable(NONE, slot.data):
                    raise ctx.error(f"{ctx.what} needs data: this selector's data is {slot.data}{decided_by(slot)}. To "
                                    f"register some without data, annotate it: "
                                    f"{selector_annotation(slot.fileobj, OptionalType(slot.data))}")
            else:
                dt = ctx.checker.check_expr(dnode, slot.data)
                if not assignable(dt, slot.data):
                    both = join(slot.data, dt)
                    hint = f". To attach both, annotate it: {selector_annotation(slot.fileobj, both)}" if both else ""
                    raise ctx.error(f"this selector's data is {slot.data}, not {dt}{decided_by(slot)}{hint}", dnode)
        actual = ctx.checker.check_expr(enode, INT)
        if not assignable(actual, INT):
            raise ctx.error(f"{ctx.what} argument 'events' must be int, not {actual}", enode)
        return SelectorKeyType(slot.fileobj, slot.data)

    handler.params = params
    handler.resolve = lambda t, receiver: t(receiver) if callable(t) else t
    return handler


def known_selector(handler):
    """A selector method that needs to know the selector's types."""

    def check(ctx: CallContext) -> Type:
        need_known(ctx)
        return handler(ctx)

    check.__dict__.update(handler.__dict__)  # (its params, for codegen)
    return check


def selector_key(sel: SelectorType) -> SelectorKeyType:
    return SelectorKeyType(sel.fileobj, sel.data)


SELECTOR_METHODS = {
    "register": selector_register(modify=False),
    "modify": selector_register(modify=True),
    "unregister": known_selector(sync_method(selector_key, ("fileobj", lambda r: r.fileobj))),
    "get_key": known_selector(sync_method(selector_key, ("fileobj", lambda r: r.fileobj))),
    "select": known_selector(sync_method(lambda r: ListType(TupleType((selector_key(r), INT))),
                                         ("timeout", OPT_FLOAT, "std::nullopt"))),
    "get_map": known_selector(sync_method(lambda r: DictType(INT, selector_key(r)))),
    "close": sync_method(NONE),
}
SELECTOR_KEY_ATTRIBUTES = {
    "fileobj": lambda t: t.fileobj, "fd": lambda t: INT, "events": lambda t: INT, "data": lambda t: t.data,
}
MODULES["selectors"] = Module("selectors", {
    "EVENT_READ": Value("EVENT_READ", INT, "sd::selectors::EVENT_READ"),
    "EVENT_WRITE": Value("EVENT_WRITE", INT, "sd::selectors::EVENT_WRITE"),
    # (all three are the same poll() selector: see the README)
    **{name: Function(name, new_selector, "{T}", generic_type=selector_type)
       for name in ("DefaultSelector", "PollSelector", "SelectSelector")},
    "BaseSelector": Function("BaseSelector", abstract_selector, generic_type=selector_type),
    "SelectorKey": Function("SelectorKey", no_key_constructor, generic_type=selector_key_type),
}, "modules/selectors.hpp")
module_type(SelectorType, methods=SELECTOR_METHODS)
module_type(SelectorKeyType, attributes=SELECTOR_KEY_ATTRIBUTES)
