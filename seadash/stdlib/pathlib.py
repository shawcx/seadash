"""`pathlib`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import CallContext, Function, MANY, MODULES, Module, PATH_LIKE, needing, open_mode, sync_method
from ..types import BOOL, BYTES, INT, ListType, NONE, OptionalType, PATH, STR, Type, VarTupleType
from .os import STAT_RESULT


PATH.attributes.update({
    "name": lambda t: STR, "stem": lambda t: STR, "suffix": lambda t: STR, "anchor": lambda t: STR,
    "suffixes": lambda t: ListType(STR), "parts": lambda t: VarTupleType(STR),
    "parent": lambda t: PATH, "parents": lambda t: ListType(PATH),
})


def path_parts(ctx: CallContext) -> Type:
    """Path(*parts) and p.joinpath(*parts): each a str or a Path."""
    ctx.arity(0, MANY)
    for i in range(len(ctx.args)):
        ctx.need(i, lambda t: t in (STR, PATH), "a str or Path")
    return PATH


def path_open(ctx: CallContext) -> Type:
    n = ctx.arity(0, 1, keywords=("mode", "encoding"))
    return open_mode(ctx, ctx.args[0] if n == 1 else ctx.keyword_arg("mode"))


PATH.methods.update({
    **{name: sync_method(BOOL) for name in ("exists", "is_file", "is_dir", "is_symlink", "is_absolute")},
    **{name: sync_method(PATH) for name in ("absolute", "resolve", "expanduser")},
    "as_posix": sync_method(STR),
    "stat": sync_method(STAT_RESULT),
    "readlink": sync_method(PATH),
    "read_text": sync_method(STR, ("encoding", OptionalType(STR), "std::nullopt")),
    "read_bytes": sync_method(BYTES),
    "write_text": sync_method(INT, ("data", STR), ("encoding", OptionalType(STR), "std::nullopt")),
    "write_bytes": sync_method(INT, ("data", BYTES)),
    "mkdir": sync_method(NONE, ("mode", INT, "0777"), ("parents", BOOL, "false"), ("exist_ok", BOOL, "false")),
    "rmdir": sync_method(NONE),
    "unlink": sync_method(NONE, ("missing_ok", BOOL, "false")),
    "touch": sync_method(NONE, ("mode", INT, "0666"), ("exist_ok", BOOL, "true")),
    "rename": sync_method(PATH, ("target", PATH_LIKE)),
    "replace": sync_method(PATH, ("target", PATH_LIKE)),
    "iterdir": sync_method(ListType(PATH)),
    "glob": needing("fnmatch", sync_method(ListType(PATH), ("pattern", STR))),  # (matched as Python does)
    "rglob": needing("fnmatch", sync_method(ListType(PATH), ("pattern", STR))),
    "with_name": sync_method(PATH, ("name", STR)),
    "with_suffix": sync_method(PATH, ("suffix", STR)),
    "with_stem": sync_method(PATH, ("stem", STR)),
    "relative_to": sync_method(PATH, ("other", PATH_LIKE)),
    "is_relative_to": sync_method(BOOL, ("other", PATH_LIKE)),
    "match": needing("fnmatch", sync_method(BOOL, ("pattern", STR))),
    "joinpath": path_parts,
    "open": path_open,
})
MODULES["pathlib"] = Module("pathlib", {
    "Path": Function("Path", path_parts, "sd::pathlib::Path", as_type=PATH),
    "PosixPath": Function("PosixPath", path_parts, "sd::pathlib::Path", as_type=PATH),
}, "modules/pathlib.hpp")
