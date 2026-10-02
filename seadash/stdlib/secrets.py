"""`secrets`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, module_with_params, runtime_module, signature
from ..types import BYTES, INT, OptionalType, STR
from .random import random_choice
from .hashlib import compare_digest


MODULES["secrets"] = module_with_params(runtime_module(
    "secrets", "modules/secrets.hpp", ("crypto",),
    token_bytes=(signature(BYTES, ("nbytes", OptionalType(INT), "std::nullopt")), "sd::secrets::token_bytes"),
    token_hex=(signature(STR, ("nbytes", OptionalType(INT), "std::nullopt")), "sd::secrets::token_hex"),
    token_urlsafe=(signature(STR, ("nbytes", OptionalType(INT), "std::nullopt")), "sd::secrets::token_urlsafe"),
    randbelow=(signature(INT, ("exclusive_upper_bound", INT)), "sd::secrets::randbelow"),
    randbits=(signature(INT, ("k", INT)), "sd::secrets::randbits"),
    choice=(random_choice, "sd::secrets::choice"),
    compare_digest=(compare_digest, "sd::secrets::compare_digest"),
    DEFAULT_ENTROPY=(INT, "sd::secrets::DEFAULT_ENTROPY"),
))
