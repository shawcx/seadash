"""`pprint`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import CallContext, MODULES, module_with_params, runtime_module, signature, sync_method
from ..types import BOOL, BuiltinClass, INT, NONE, OptionalType, STR, TEXT_FILE, Type


PRETTY_PRINTER = BuiltinClass("pprint.PrettyPrinter", "sd::pprint::PrettyPrinter")
PPRINT_OPTIONS = (("compact", BOOL, "false"), ("sort_dicts", BOOL, "true"), ("underscore_numbers", BOOL, "false"))
PPRINT_LAYOUT = (("indent", INT, "1"), ("width", INT, "80"), ("depth", OptionalType(INT), "std::nullopt"))
PPRINT_STREAM = ("stream", OptionalType(TEXT_FILE), "std::optional<std::shared_ptr<sd::TextFile>>()")
PRETTY_PRINTER.methods.update({
    "pformat": sync_method(STR, ("object", None)),
    "pprint": sync_method(NONE, ("object", None)),
    "isreadable": sync_method(BOOL, ("object", None)),
    "isrecursive": sync_method(BOOL, ("object", None)),
})


def pprint_pp(ctx: CallContext) -> Type:
    """pp(object, ...): pprint() with sort_dicts=False."""
    return signature(NONE, ("object", None), PPRINT_STREAM, *PPRINT_LAYOUT, PPRINT_OPTIONS[0],
                     ("sort_dicts", BOOL, "false"), PPRINT_OPTIONS[2])(ctx)


pprint_pp.params = (("object", None), PPRINT_STREAM, *PPRINT_LAYOUT, PPRINT_OPTIONS[0], ("sort_dicts", BOOL, "false"),
                    PPRINT_OPTIONS[2])
MODULES["pprint"] = module_with_params(runtime_module(
    "pprint", "modules/pprint.hpp",
    pformat=(signature(STR, ("object", None), *PPRINT_LAYOUT, *PPRINT_OPTIONS), "sd::pprint::pformat"),
    pprint=(signature(NONE, ("object", None), PPRINT_STREAM, *PPRINT_LAYOUT, *PPRINT_OPTIONS), "sd::pprint::pprint"),
    pp=(pprint_pp, "sd::pprint::pprint"),
    saferepr=(signature(STR, ("object", None)), "sd::pprint::saferepr"),
    isreadable=(signature(BOOL, ("object", None)), "sd::pprint::isreadable"),
    isrecursive=(signature(BOOL, ("object", None)), "sd::pprint::isrecursive"),
    PrettyPrinter=(signature(PRETTY_PRINTER, *PPRINT_LAYOUT, PPRINT_STREAM, *PPRINT_OPTIONS), "sd::pprint::make_printer"),
))
MODULES["pprint"].members["PrettyPrinter"].as_type = PRETTY_PRINTER
