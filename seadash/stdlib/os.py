"""`os`, `os.path`, `typing`, `time` and `__future__`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from .. import ast as A
from ..builtins import (
    builtin_struct, CallContext, EXCEPTIONS, MANY, Module, module_with_params, MODULES, OneOf, os_fdopen,
    runtime_module, signature, TypeAlias,
)
from ..types import (
    BOOL, BYTES, element_type, FLOAT, FuncType, GeneratorType, INT, ListType, NONE, OptionalType, PATH, STR,
    TupleType, Type,
)


def os_getenv(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2)
    ctx.expect(0, STR)
    if n == 2:
        ctx.expect(1, STR)
        return STR
    return OptionalType(STR)


def path_join(ctx: CallContext) -> Type:
    for i, arg in enumerate(ctx.args):
        if isinstance(arg, A.Starred):  # os.path.join(root, *parts)
            if element_type(arg.ty) != STR:
                raise ctx.error(f"os.path.join() needs strs, not {arg.ty} unpacked", arg)
            ctx.call.notes["spread_join"] = True
        else:
            ctx.expect(i, STR)
    if not ctx.args:
        ctx.arity(1, MANY)
    return STR


STAT_RESULT = builtin_struct("stat_result", "sd::pathlib::StatResult", {  # os.stat, Path.stat
    "st_size": INT, "st_mode": INT, "st_uid": INT, "st_gid": INT, "st_nlink": INT, "st_ino": INT, "st_dev": INT,
    "st_mtime": FLOAT, "st_atime": FLOAT, "st_ctime": FLOAT, "st_mtime_ns": INT, "st_atime_ns": INT, "st_ctime_ns": INT,
})
OS_PATH_ARG = OneOf(STR, PATH, what="a str or Path")
OS_PATH = module_with_params(runtime_module(
    "os.path", "modules/os.hpp",
    exists=(signature(BOOL, ("path", STR)), "sd::os::path::exists"),
    isfile=(signature(BOOL, ("path", STR)), "sd::os::path::isfile"),
    isdir=(signature(BOOL, ("path", STR)), "sd::os::path::isdir"),
    join=(path_join, "sd::os::path::join"),
    basename=(signature(STR, ("path", STR)), "sd::os::path::basename"),
    dirname=(signature(STR, ("path", STR)), "sd::os::path::dirname"),
    abspath=(signature(STR, ("path", STR)), "sd::os::path::abspath"),
    splitext=(signature(TupleType((STR, STR)), ("path", STR)), "sd::os::path::splitext"),
    getsize=(signature(INT, ("path", STR)), "sd::os::path::getsize"),
    islink=(signature(BOOL, ("path", STR)), "sd::os::path::islink"),
    relpath=(signature(STR, ("path", STR), ("start", OptionalType(STR), "std::nullopt")), "sd::os::path::relpath"),
    realpath=(signature(STR, ("path", STR), ("strict", BOOL, "false")), "sd::os::path::realpath"),
    normpath=(signature(STR, ("path", STR)), "sd::os::path::normpath"),
    sep=(STR, "sd::os::path::sep()"),
))
MODULES["os"] = module_with_params(runtime_module(
    "os", "modules/os.hpp",
    getcwd=(signature(STR), "sd::os::getcwd"),
    listdir=(signature(ListType(STR), ("path", STR, '"."s')), "sd::os::listdir"),
    mkdir=(signature(NONE, ("path", STR)), "sd::os::mkdir"),
    makedirs=(signature(NONE, ("name", STR), ("exist_ok", BOOL, "false")), "sd::os::makedirs"),
    remove=(signature(NONE, ("path", STR)), "sd::os::remove"),
    unlink=(signature(NONE, ("path", STR)), "sd::os::remove"),
    rmdir=(signature(NONE, ("path", STR)), "sd::os::rmdir"),
    chmod=(signature(NONE, ("path", STR), ("mode", INT)), "sd::os::chmod"),
    rename=(signature(NONE, ("src", STR), ("dst", STR)), "sd::os::rename"),
    symlink=(signature(NONE, ("src", STR), ("dst", STR)), "sd::os::symlink"),
    chdir=(signature(NONE, ("path", STR)), "sd::os::chdir"),
    getenv=(os_getenv, "sd::os::getenv"),
    sep=(STR, "sd::os::path::sep()"),
    stat=(signature(STAT_RESULT, ("path", OneOf(STR, PATH, INT, what="a str, a Path or a file descriptor")),
                    ("follow_symlinks", BOOL, "true")), "sd::os::stat"),
    lstat=(signature(STAT_RESULT, ("path", OS_PATH_ARG)), "sd::os::lstat"),
    readlink=(signature(STR, ("path", OS_PATH_ARG)), "sd::os::readlink"),
    utime=(signature(NONE, ("path", OS_PATH_ARG), ("times", OptionalType(TupleType((FLOAT, FLOAT))), "std::nullopt"),
                     ("ns", OptionalType(TupleType((INT, INT))), "std::nullopt"), ("follow_symlinks", BOOL, "true")),
           "sd::os::utime"),
    walk=(signature(GeneratorType(TupleType((STR, ListType(STR), ListType(STR)))), ("top", STR), ("topdown", BOOL, "true"),
                    ("onerror", OneOf(FuncType((EXCEPTIONS["OSError"],), NONE), NONE, what="a function (OSError) -> None"),
                     "std::nullopt"), ("followlinks", BOOL, "false")), "sd::os::walk"),
    # file descriptors (the program closes what it opens)
    open=(signature(INT, ("path", STR), ("flags", INT), ("mode", INT, "511_i")), "sd::os::open"),
    close=(signature(NONE, ("fd", INT)), "sd::os::close"),
    read=(signature(BYTES, ("fd", INT), ("n", INT)), "sd::os::read"),
    write=(signature(INT, ("fd", INT), ("data", BYTES)), "sd::os::write"),
    dup=(signature(INT, ("fd", INT)), "sd::os::dup"),
    pipe=(signature(TupleType((INT, INT))), "sd::os::pipe"),
    fdopen=(os_fdopen, None),
    getpid=(signature(INT), "sd::os::getpid"),
    kill=(signature(NONE, ("pid", INT), ("signal", INT)), "sd::os::kill"),
    **{c: (INT, f"static_cast<std::int64_t>({c})") for c in (
        "O_RDONLY", "O_WRONLY", "O_RDWR", "O_CREAT", "O_EXCL", "O_TRUNC", "O_APPEND", "O_NONBLOCK", "O_CLOEXEC")},
))
MODULES["os"].members["path"] = OS_PATH
MODULES["typing"] = Module("typing", {
    name: TypeAlias(name) for name in ("Callable", "TextIO", "BinaryIO", "Optional", "List", "Dict", "Set", "Tuple", "Iterator", "Iterable",
                                       "ContextManager")
})
MODULES["__future__"] = Module("__future__", {"annotations": TypeAlias("annotations")})  # accepted, no effect
MODULES["time"] = module_with_params(runtime_module(
    "time", "modules/time.hpp",
    perf_counter=(signature(FLOAT), "sd::time::perf_counter"),
    monotonic=(signature(FLOAT), "sd::time::monotonic"),
    time=(signature(FLOAT), "sd::time::time"),
    sleep=(signature(NONE, ("secs", FLOAT)), "sd::time::sleep"),
))
