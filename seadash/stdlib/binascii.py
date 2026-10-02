"""`binascii`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, bytes_fn, exception_class, module_with_params, runtime_module, signature
from ..types import BOOL, BYTES, INT, OptionalType, STR


MODULES["binascii"] = module_with_params(runtime_module(
    "binascii", "modules/binascii.hpp", ("z",),
    hexlify=(signature(BYTES, ("data", BYTES), ("sep", OptionalType(STR), "std::nullopt"), ("bytes_per_sep", INT, "1_i")),
             "sd::binascii::hexlify"),
    b2a_hex=(signature(BYTES, ("data", BYTES), ("sep", OptionalType(STR), "std::nullopt"), ("bytes_per_sep", INT, "1_i")),
             "sd::binascii::hexlify"),
    unhexlify=(bytes_fn(BYTES), "sd::binascii::unhexlify"),
    a2b_hex=(bytes_fn(BYTES), "sd::binascii::unhexlify"),
    crc32=(bytes_fn(INT, 1, 2, (INT,)), "sd::binascii::crc32"),
    crc_hqx=(signature(INT, ("data", BYTES), ("value", INT)), "sd::binascii::crc_hqx"),
    b2a_base64=(signature(BYTES, ("data", BYTES), ("newline", BOOL, "true")), "sd::binascii::b2a_base64"),
    a2b_base64=(bytes_fn(BYTES), "sd::binascii::a2b_base64"),
    Error=exception_class("Error", "sd::binascii::Error", "ValueError"),
))
