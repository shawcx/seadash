"""`base64`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, bytes_fn, runtime_module
from ..types import BYTES


MODULES["base64"] = runtime_module(
    "base64", "modules/base64.hpp",
    b64encode=(bytes_fn(BYTES), "sd::base64::b64encode"),
    b64decode=(bytes_fn(BYTES), "sd::base64::b64decode"),
    standard_b64encode=(bytes_fn(BYTES), "sd::base64::b64encode"),
    standard_b64decode=(bytes_fn(BYTES), "sd::base64::b64decode"),
    urlsafe_b64encode=(bytes_fn(BYTES), "sd::base64::urlsafe_b64encode"),
    urlsafe_b64decode=(bytes_fn(BYTES), "sd::base64::urlsafe_b64decode"),
    b16encode=(bytes_fn(BYTES), "sd::base64::b16encode"),
    b16decode=(bytes_fn(BYTES), "sd::base64::b16decode"),
)
