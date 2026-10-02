"""`copy`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import CallContext, Function, MODULES, Module, strip_optional_type
from ..types import (
    BuiltinClass, DequeType, DictType, FileType, FuncType, JSON_VALUE, ListType, OptionalType, PatternType, Prim,
    SOCKET, SetType, StructType, SyncType, TupleType, Type, UUID_T, VarTupleType,
)
from .decorators import dataclasses_replace


def copy_problem(t: Type, deep: bool, structs: list[StructType], seen: set | None = None) -> tuple | None:
    """Why copy.copy (deep=False) or copy.deepcopy can't copy a t: (where, what, why) for the
    message, or None if it can. `where` is the field holding the culprit (None: t itself, or
    an item of it). A shallow copy shares what's inside, so only the outside matters; a deep
    copy copies everything reachable, including what a subclass of a class adds (`structs`:
    the classes that may be one)."""
    seen = set() if seen is None else seen
    unpicklable = "(Python raises TypeError: cannot pickle it)"
    match t:
        case BuiltinClass():
            if t.threads in ("immutable", "value") or t == UUID_T:
                return None
            if t == SOCKET:
                return None, "a socket", f"{unpicklable}; open another connection, or share this one"
            return None, f"a {t}", "(not supported yet)"
        case PatternType() | FuncType():
            return None  # (immutable: copies are the same object, as in Python)
        case _ if isinstance(t, Prim) or t == JSON_VALUE:
            return None
        case SyncType(kind):
            if kind in ("Lock", "RLock"):
                return None, f"a {kind}", f"{unpicklable}; make a new one, or share this one"
            return None, f"{'an' if kind[0] in 'AEIOU' else 'a'} {kind}", ": share this one, or make a new one"
        case FileType(memory=True):
            return None, f"a {t}", "(not supported yet); make a new one from its getvalue(), or share this one"
        case FileType():
            return None, "a file", f"{unpicklable}; open it again, or share this one"
        case OptionalType(inner):
            return copy_problem(inner, deep, structs, seen)
        case ListType(elem) | SetType(elem) | DequeType(elem) | VarTupleType(elem):
            return copy_problem(elem, deep, structs, seen) if deep else None
        case DictType(key, value):
            return (copy_problem(key, deep, structs, seen) or copy_problem(value, deep, structs, seen)) if deep else None
        case TupleType(elts):
            return next((p for e in elts if (p := copy_problem(e, deep, structs, seen))), None) if deep else None
        case StructType():
            if t.is_exception:
                return None, t.name, ": copying an exception isn't supported yet"
            if any(a.name == "Synchronized" for a in t.ancestors() if a.builtin):
                return None, t.name, ": a Synchronized object's lock can't be copied; make a new one from its fields"
            if any(a.builtin for a in t.ancestors()):
                return None, f"a {t.name}", "(its base class is from the standard library)"
            if t.kind != "class" or not deep or t in seen:
                return None  # (a @value class is a value all the way down: copying it copies it all)
            seen.add(t)
            if (m := t.find_method("__deepcopy__")) is not None:
                return (None, f"{m.owner.name}, which defines __deepcopy__",
                        "(seadash doesn't call __deepcopy__ yet: it has no type for the memo); remove __deepcopy__ "
                        "to copy every field, or copy it yourself")
            for sub in [t, *(s for s in structs if s is not t and s.is_subclass_of(t))]:
                for f in (t.all_fields() if sub is t else sub.fields).values():
                    if (p := copy_problem(f.type, deep, structs, seen)) is not None:
                        where = f"{sub.name}.{f.name}" if sub is t else f"subclass {sub.name}.{f.name}"
                        return p if p[0] is not None else (where, *p[1:])
            return None
    return None, f"a {t}", "(not supported yet)"


def copy_check(deep: bool):
    """copy.copy(x) / copy.deepcopy(x): x's own type (a class's __copy__ may return its class)."""
    def check(ctx: CallContext) -> Type:
        what = "deepcopy" if deep else "copy"
        if deep and len(ctx.args) == 2 or ctx.call.keywords:
            raise ctx.error(f"{what}()'s memo argument isn't supported yet: call {what}(x)", (ctx.args[1:] or ctx.call.keywords)[0])
        ctx.arity(1)
        t = ctx.arg(0, ctx.expected)
        checker = ctx.checker
        structs = [*checker.structs.values(), *checker.out_structs]
        if (p := copy_problem(t, deep, structs)) is not None:
            where, thing, why = p
            sep = "" if why[0] in ";:" else " "
            if where is None and not isinstance(t, OptionalType) and not isinstance(t, (ListType, SetType, DictType, DequeType, TupleType, VarTupleType)):
                raise ctx.error(f"copy.{what}() can't copy {thing}{sep}{why}", ctx.args[0])
            holder = where or "it"
            why = ";" + why[1:] if why[0] == ":" else why
            raise ctx.error(f"copy.{what}() can't copy {t}: {holder} holds {thing}, which can't be copied{sep}{why}", ctx.args[0])
        inner = strip_optional_type(t)
        if not deep and isinstance(inner, StructType) and inner.kind == "class":
            for sub in [inner, *(s for s in structs if s.is_subclass_of(inner))]:
                m = sub.methods.get("__copy__")
                if m is not None and (m.params or not isinstance(m.ret, StructType) or m.ret is not sub):
                    raise ctx.error(f"{sub.name}.__copy__ must take only self and return a {sub.name} "
                                    f"(def __copy__(self) -> {sub.name}:), for copy.copy()", m.node)
            if (m := inner.find_method("__copy__")) is not None and m.owner is not inner:
                return OptionalType(m.ret) if isinstance(t, OptionalType) else m.ret  # (Base.__copy__ makes a Base)
        return t
    return check


MODULES["copy"] = Module("copy", {
    "copy": Function("copy", copy_check(False)),
    "deepcopy": Function("deepcopy", copy_check(True)),
    "replace": Function("replace", dataclasses_replace),  # (Python 3.13)
}, "modules/copy.hpp")
