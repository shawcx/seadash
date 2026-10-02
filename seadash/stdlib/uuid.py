"""`uuid`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, module_with_params, runtime_module, signature
from ..types import BYTES, INT, OptionalType, STR, TupleType, UUID_T


MODULES["uuid"] = module_with_params(runtime_module(
    "uuid", "modules/uuid.hpp", ("crypto",),
    UUID=(signature(UUID_T, ("hex", OptionalType(STR), "std::nullopt"), ("bytes", OptionalType(BYTES), "std::nullopt"),
                    ("version", OptionalType(INT), "std::nullopt")), "sd::uuid::UUID::make"),
    uuid1=(signature(UUID_T, ("node", OptionalType(INT), "std::nullopt"), ("clock_seq", OptionalType(INT), "std::nullopt")),
           "sd::uuid::uuid1"),
    uuid3=(signature(UUID_T, ("namespace", UUID_T), ("name", STR)), "sd::uuid::uuid3"),
    uuid4=(signature(UUID_T), "sd::uuid::uuid4"),
    uuid5=(signature(UUID_T, ("namespace", UUID_T), ("name", STR)), "sd::uuid::uuid5"),
    **{ns: (UUID_T, f"sd::uuid::{ns}") for ns in ("NAMESPACE_DNS", "NAMESPACE_URL", "NAMESPACE_OID", "NAMESPACE_X500")},
    RESERVED_NCS=(STR, '"reserved for NCS compatibility"s'),
    RFC_4122=(STR, '"specified in RFC 4122"s'),
    RESERVED_MICROSOFT=(STR, '"reserved for Microsoft compatibility"s'),
    RESERVED_FUTURE=(STR, '"reserved for future definition"s'),
))
MODULES["uuid"].members["UUID"].as_type = UUID_T
UUID_T.attributes.update({
    "hex": lambda t: STR, "bytes": lambda t: BYTES, "version": lambda t: OptionalType(INT),
    "variant": lambda t: STR, "urn": lambda t: STR, "fields": lambda t: TupleType((INT,) * 6),
    **{f: (lambda t: INT) for f in ("time_low", "time_mid", "time_hi_version", "clock_seq_hi_variant",
                                    "clock_seq_low", "node", "clock_seq", "time")},
})
