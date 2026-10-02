"""`html`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, module_with_params, runtime_module, signature
from ..types import BOOL, STR


MODULES["html"] = module_with_params(runtime_module(
    "html", "modules/html.hpp",
    escape=(signature(STR, ("s", STR), ("quote", BOOL, "true")), "sd::html::escape"),
    unescape=(signature(STR, ("s", STR)), "sd::html::unescape"),
))
