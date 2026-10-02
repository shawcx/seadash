"""`mimetypes`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, PATH_LIKE, module_with_params, runtime_module, signature
from ..types import BOOL, DictType, ListType, NONE, OptionalType, STR, TupleType


MODULES["mimetypes"] = module_with_params(runtime_module(
    "mimetypes", "modules/mimetypes.hpp",
    guess_type=(signature(TupleType((OptionalType(STR), OptionalType(STR))), ("url", PATH_LIKE),
                          ("strict", BOOL, "true")), "sd::mimetypes::guess_type"),
    guess_extension=(signature(OptionalType(STR), ("type", STR), ("strict", BOOL, "true")),
                     "sd::mimetypes::guess_extension"),
    guess_all_extensions=(signature(ListType(STR), ("type", STR), ("strict", BOOL, "true")),
                          "sd::mimetypes::guess_all_extensions"),
    add_type=(signature(NONE, ("type", STR), ("ext", STR), ("strict", BOOL, "true")), "sd::mimetypes::add_type"),
    types_map=(DictType(STR, STR), "sd::mimetypes::types_map()"),
    common_types=(DictType(STR, STR), "sd::mimetypes::common_types()"),
    encodings_map=(DictType(STR, STR), "sd::mimetypes::encodings_map()"),
    suffix_map=(DictType(STR, STR), "sd::mimetypes::suffix_map()"),
))
