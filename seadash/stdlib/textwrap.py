"""`textwrap`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, module_with_params, runtime_module, signature, sync_method
from ..types import BOOL, FuncType, INT, ListType, OptionalType, STR, TEXT_WRAPPER


WRAP_OPTIONS = (
    ("initial_indent", STR, '""s'), ("subsequent_indent", STR, '""s'), ("expand_tabs", BOOL, "true"),
    ("replace_whitespace", BOOL, "true"), ("fix_sentence_endings", BOOL, "false"), ("break_long_words", BOOL, "true"),
    ("drop_whitespace", BOOL, "true"), ("break_on_hyphens", BOOL, "true"), ("tabsize", INT, "8"),
    ("max_lines", OptionalType(INT), "std::nullopt"), ("placeholder", STR, '" [...]"s'),
)
WIDTH_70 = ("width", INT, "70")
MODULES["textwrap"] = module_with_params(runtime_module(
    "textwrap", "modules/textwrap.hpp", ("pcre2-8",),
    wrap=(signature(ListType(STR), ("text", STR), WIDTH_70, *WRAP_OPTIONS), "sd::textwrap::wrap"),
    fill=(signature(STR, ("text", STR), WIDTH_70, *WRAP_OPTIONS), "sd::textwrap::fill"),
    shorten=(signature(STR, ("text", STR), ("width", INT), *WRAP_OPTIONS), "sd::textwrap::shorten"),
    dedent=(signature(STR, ("text", STR)), "sd::textwrap::dedent"),
    indent=(signature(STR, ("text", STR), ("prefix", STR), ("predicate", FuncType((STR,), BOOL),
                                                               "[](const std::string& l) { return !sd::textwrap::is_blank(l); }")),
            "sd::textwrap::indent"),
    TextWrapper=(signature(TEXT_WRAPPER, WIDTH_70, *WRAP_OPTIONS), "sd::textwrap::make"),
))
MODULES["textwrap"].members["TextWrapper"].as_type = TEXT_WRAPPER
TEXT_WRAPPER.methods.update({"wrap": sync_method(ListType(STR), ("text", STR)), "fill": sync_method(STR, ("text", STR))})
