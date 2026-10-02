"""`shlex`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, module_with_params, runtime_module, signature
from ..types import BOOL, ListType, STR


MODULES["shlex"] = module_with_params(runtime_module(
    "shlex", "modules/shlex.hpp",
    split=(signature(ListType(STR), ("s", STR), ("comments", BOOL, "false"), ("posix", BOOL, "true")), "sd::shlex::split"),
    quote=(signature(STR, ("s", STR)), "sd::shlex::quote"),
    join=(signature(STR, ("split_command", ListType(STR))), "sd::shlex::join"),
))
