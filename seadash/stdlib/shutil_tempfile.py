"""`shutil` and `tempfile`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import (
    builtin_struct, CallContext, exception_class, module_with_params, MODULES, PATH_LIKE, runtime_module, signature,
    sync_method,
)
from ..types import BINARY_FILE, BOOL, FileType, INT, NONE, OptionalType, STR, TEMPDIR, TEXT_FILE, Type


DISK_USAGE = builtin_struct("usage", "sd::shutil::DiskUsage", {"total": INT, "used": INT, "free": INT})
SHUTIL_ERROR = exception_class("Error", "sd::shutil::Error", "OSError")
SRC_DST = (("src", PATH_LIKE), ("dst", PATH_LIKE))


def shutil_copyfileobj(ctx: CallContext) -> Type:
    """copyfileobj(fsrc, fdst, length=64 KiB): both text files or both binary (StringIO too)."""
    if not ctx.args:
        raise ctx.error("shutil.copyfileobj() missing required argument 'fsrc'")
    src = ctx.checker.check_expr(ctx.args[0])
    if not isinstance(src, FileType):
        raise ctx.error(f"shutil.copyfileobj() needs a file to read, not {src}", ctx.args[0])
    kind = BINARY_FILE if src.binary else TEXT_FILE
    return signature(NONE, ("fsrc", kind), ("fdst", kind), ("length", INT, "64 * 1024"))(ctx)


# (codegen passes the files as they are: their exact types vary)
shutil_copyfileobj.params = (("fsrc", None), ("fdst", None), ("length", INT, "64 * 1024"))
MODULES["shutil"] = module_with_params(runtime_module(
    "shutil", "modules/shutil.hpp",
    copyfile=(signature(STR, *SRC_DST), "sd::shutil::copyfile"),
    copy=(signature(STR, *SRC_DST), "sd::shutil::copy"),
    copy2=(signature(STR, *SRC_DST), "sd::shutil::copy2"),
    copymode=(signature(NONE, *SRC_DST), "sd::shutil::copymode"),
    copystat=(signature(NONE, *SRC_DST), "sd::shutil::copystat"),
    copytree=(signature(STR, *SRC_DST, ("dirs_exist_ok", BOOL, "false")), "sd::shutil::copytree"),
    rmtree=(signature(NONE, ("path", PATH_LIKE), ("ignore_errors", BOOL, "false")), "sd::shutil::rmtree"),
    move=(signature(STR, *SRC_DST), "sd::shutil::move"),
    which=(signature(OptionalType(STR), ("cmd", STR), ("path", OptionalType(STR), "std::nullopt")), "sd::shutil::which"),
    disk_usage=(signature(DISK_USAGE, ("path", PATH_LIKE)), "sd::shutil::disk_usage"),
    copyfileobj=(shutil_copyfileobj, "sd::shutil::copyfileobj"),
    Error=SHUTIL_ERROR,
    SameFileError=exception_class("SameFileError", "sd::shutil::SameFileError", SHUTIL_ERROR),
))
TEMP_PARAMS = (("suffix", STR, '""s'), ("prefix", OptionalType(STR), "std::nullopt"),
               ("dir", OptionalType(STR), "std::nullopt"))
MODULES["tempfile"] = module_with_params(runtime_module(
    "tempfile", "modules/tempfile.hpp",
    gettempdir=(signature(STR), "sd::tempfile::gettempdir"),
    mkdtemp=(signature(STR, *TEMP_PARAMS), "sd::tempfile::mkdtemp"),
    TemporaryDirectory=(signature(TEMPDIR, *TEMP_PARAMS), "sd::tempfile::TemporaryDirectory"),
))
MODULES["tempfile"].members["TemporaryDirectory"].as_type = TEMPDIR
TEMPDIR.methods["cleanup"] = sync_method(NONE)
TEMPDIR.attributes["name"] = lambda t: STR
