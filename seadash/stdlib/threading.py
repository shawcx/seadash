"""`threading`, `queue` and `collections`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import CollectionTypeDef, MODULES, Module, OPT_FLOAT, SyncTypeDef, elem0, exception_class, sync_method
from ..types import BOOL, FLOAT, INT, NONE, STR, StructType, SyncType


SYNC_METHODS: dict[str, dict] = {
    "Lock": {
        "acquire": sync_method(BOOL, ("blocking", BOOL, "true"), ("timeout", FLOAT, "-1.0")),
        "release": sync_method(NONE),
        "locked": sync_method(BOOL),
    },
    "RLock": {
        "acquire": sync_method(BOOL, ("blocking", BOOL, "true"), ("timeout", FLOAT, "-1.0")),
        "release": sync_method(NONE),
    },
    "Event": {
        "set": sync_method(NONE),
        "clear": sync_method(NONE),
        "is_set": sync_method(BOOL),
        "wait": sync_method(BOOL, ("timeout", OPT_FLOAT, "std::nullopt")),
    },
    "Atomic": {
        "get": sync_method(INT),
        "set": sync_method(NONE, ("value", INT)),
        "add": sync_method(INT, ("n", INT, "1_i")),
        "sub": sync_method(INT, ("n", INT, "1_i")),
        "compare_and_set": sync_method(BOOL, ("expected", INT), ("value", INT)),
    },
    "Mutex": {
        "get": sync_method(elem0),
        "set": sync_method(NONE, ("value", elem0)),
    },
    "RWMutex": {
        "get": sync_method(elem0),
        "set": sync_method(NONE, ("value", elem0)),
        "read": sync_method(lambda r: SyncType("RWRead", r.args)),
        "write": sync_method(lambda r: SyncType("RWWrite", r.args)),
    },
    "Queue": {
        "put": sync_method(NONE, ("item", elem0), ("block", BOOL, "true"), ("timeout", OPT_FLOAT, "std::nullopt")),
        "get": sync_method(elem0, ("block", BOOL, "true"), ("timeout", OPT_FLOAT, "std::nullopt")),
        "put_nowait": sync_method(NONE, ("item", elem0)),
        "get_nowait": sync_method(elem0),
        "empty": sync_method(BOOL),
        "full": sync_method(BOOL),
        "qsize": sync_method(INT),
        "task_done": sync_method(NONE),
        "join": sync_method(NONE),
    },
    "Thread": {
        "start": sync_method(NONE),
        "join": sync_method(NONE, ("timeout", OPT_FLOAT, "std::nullopt")),
        "is_alive": sync_method(BOOL),
    },
}
THREAD_ATTRIBUTES = {"name": STR, "daemon": BOOL}
SYNCHRONIZED = StructType("Synchronized", "class", None, builtin=True, cpp_name="sd::threading::Synchronized")
MODULES["threading"] = Module("threading", {
    **{kind: SyncTypeDef(kind) for kind in ("Thread", "Lock", "RLock", "Event")},
}, "modules/threading.hpp", ("pthread",))
MODULES["collections"] = Module("collections", {
    name: CollectionTypeDef(name) for name in ("defaultdict", "Counter", "deque")
}, "modules/collections.hpp")
MODULES["queue"] = Module("queue", {
    "Queue": SyncTypeDef("Queue"),
    "Empty": exception_class("Empty", "sd::queue::Empty"),
    "Full": exception_class("Full", "sd::queue::Full"),
}, "modules/queue.hpp")
