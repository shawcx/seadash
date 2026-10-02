"""`stat`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, module_with_params, runtime_module, signature
from ..types import BOOL, INT, STR


STAT_CONSTANTS = (
    "S_IFDIR", "S_IFCHR", "S_IFBLK", "S_IFREG", "S_IFIFO", "S_IFLNK", "S_IFSOCK", "S_ISUID",
    "S_ISGID", "S_ISVTX", "S_ENFMT", "S_IREAD", "S_IWRITE", "S_IEXEC", "S_IRWXU", "S_IRUSR",
    "S_IWUSR", "S_IXUSR", "S_IRWXG", "S_IRGRP", "S_IWGRP", "S_IXGRP", "S_IRWXO", "S_IROTH",
    "S_IWOTH", "S_IXOTH",
)
MODULES["stat"] = module_with_params(runtime_module(
    "stat", "modules/stat.hpp",
    **{name: (INT, f"static_cast<std::int64_t>({name})") for name in STAT_CONSTANTS},
    **{f"S_IS{kind.upper()}": (signature(BOOL, ("mode", INT)), f"sd::statmod::is_{kind}")
       for kind in ("dir", "chr", "blk", "reg", "fifo", "lnk", "sock")},
    S_IMODE=(signature(INT, ("mode", INT)), "sd::statmod::imode"),
    S_IFMT=(signature(INT, ("mode", INT)), "sd::statmod::ifmt"),
    filemode=(signature(STR, ("mode", INT)), "sd::statmod::filemode"),
))
