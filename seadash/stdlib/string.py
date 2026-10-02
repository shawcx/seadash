"""`string`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import CallContext, MODULES, module_with_params, runtime_module, signature, sync_method
from ..types import BOOL, DictType, ListType, OptionalType, STR, STR_TEMPLATE, Type


def template_substitute(ctx: CallContext) -> Type:
    """t.substitute(mapping, **keywords): the mapping's values and the keywords can be anything
    printable (they're shown with str())."""
    ctx.arity(0, 1, keywords=tuple(k.name for k in ctx.call.keywords))
    if ctx.args:
        t = ctx.arg(0)
        if not (isinstance(t, DictType) and t.key == STR):
            raise ctx.error(f"{ctx.what} takes a dict with str keys (and/or keyword arguments), not {t}", ctx.args[0])
    for kw in ctx.call.keywords:
        ctx.checker.check_printable(ctx.checker.check_expr(kw.value), kw.value)
    return STR


MODULES["string"] = module_with_params(runtime_module(
    "string", "modules/string.hpp",
    capwords=(signature(STR, ("s", STR), ("sep", OptionalType(STR), "std::nullopt")), "sd::stringmod::capwords"),
    Template=(signature(STR_TEMPLATE, ("template", STR)), "sd::stringmod::Template"),
    **{name: (STR, f"sd::stringmod::{name}") for name in (
        "ascii_letters", "ascii_lowercase", "ascii_uppercase", "digits", "hexdigits", "octdigits",
        "punctuation", "printable", "whitespace")},
))
MODULES["string"].members["Template"].as_type = STR_TEMPLATE
STR_TEMPLATE.methods.update({"substitute": template_substitute, "safe_substitute": template_substitute,
                             "get_identifiers": sync_method(ListType(STR)), "is_valid": sync_method(BOOL)})
STR_TEMPLATE.attributes["template"] = lambda t: STR
