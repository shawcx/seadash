"""`email.utils`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, Module, OPT_FLOAT, module_with_params, runtime_module, signature
from ..types import BOOL, DATETIME, INT, OptionalType, STR, TupleType


EMAIL_UTILS_MOD = module_with_params(runtime_module(
    "email.utils", "modules/emailutils.hpp",
    formatdate=(signature(STR, ("timeval", OPT_FLOAT, "std::nullopt"), ("localtime", BOOL, "false"),
                          ("usegmt", BOOL, "false")), "sd::emailutils::formatdate"),
    format_datetime=(signature(STR, ("dt", DATETIME), ("usegmt", BOOL, "false")), "sd::emailutils::format_datetime"),
    parsedate_to_datetime=(signature(DATETIME, ("data", STR)), "sd::emailutils::parsedate_to_datetime"),
    parsedate_tz=(signature(OptionalType(TupleType((INT,) * 10)), ("data", STR)), "sd::emailutils::parsedate_tz"),
    parsedate=(signature(OptionalType(TupleType((INT,) * 9)), ("data", STR)), "sd::emailutils::parsedate"),
))
MODULES["email"] = Module("email", {"utils": EMAIL_UTILS_MOD})
