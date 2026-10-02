"""`fnmatch` and `glob`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from ..builtins import (
    CallContext, MODULES, bind_args, iterable_of, module_with_params, plural_args, runtime_module, signature,
)
from ..types import BOOL, GeneratorType, INT, ListType, OptionalType, PATH, STR, Type, assignable


def fnmatch_filter(ctx: CallContext) -> Type:
    """fnmatch.filter(names, pat): names is any iterable of str."""
    args = bind_args(ctx, (("names", None), ("pat", STR)))
    elem = iterable_of(ctx, args["names"], "argument 'names'")
    if elem != STR:
        raise ctx.error(f"{ctx.what} names must be strings, not {elem}", args["names"])
    ctx.checker.expect_type(args["pat"], STR, f"{ctx.what} argument 'pat'")
    return ListType(STR)


fnmatch_filter.params = (("names", None), ("pat", STR))
OPT_PATH_LIKE = object()  # root_dir=: None, a str or a Path (converted in C++)


def keyword_only(result: Type, positional: tuple, keywords: tuple) -> Callable[[CallContext], Type]:
    """A function whose `keywords` parameters can only be passed by keyword, as glob.glob's."""
    params = (*positional, *keywords)

    def handler(ctx: CallContext) -> Type:
        if len(ctx.args) > len(positional):
            want = plural_args(len(positional)).replace("argument", "positional argument")
            raise ctx.error(f"{ctx.what} takes {want} but {len(ctx.args)} were given")
        args = bind_args(ctx, params)
        for name, t, *_ in params:
            if (node := args.get(name)) is None:
                continue
            if t is OPT_PATH_LIKE:
                actual = ctx.checker.check_expr(node, OptionalType(STR))
                if actual != PATH and not assignable(actual, OptionalType(STR)):
                    raise ctx.error(f"{ctx.what} argument '{name}' must be a str, Path or None, not {actual}", node)
            else:
                ctx.checker.expect_type(node, t, f"{ctx.what} argument '{name}'")
        return result

    handler.params = params
    return handler


GLOB_OPTIONS = (("root_dir", OPT_PATH_LIKE, "std::nullopt"), ("dir_fd", OptionalType(INT), "std::nullopt"),
                ("recursive", BOOL, "false"), ("include_hidden", BOOL, "false"))
MODULES["fnmatch"] = module_with_params(runtime_module(
    "fnmatch", "modules/fnmatch.hpp", ("pcre2-8",),
    fnmatch=(signature(BOOL, ("name", STR), ("pat", STR)), "sd::fnmatch::fnmatch"),
    fnmatchcase=(signature(BOOL, ("name", STR), ("pat", STR)), "sd::fnmatch::fnmatchcase"),
    filter=(fnmatch_filter, "sd::fnmatch::filter"),
    filterfalse=(fnmatch_filter, "sd::fnmatch::filterfalse"),
    translate=(signature(STR, ("pat", STR)), "sd::fnmatch::translate"),
))
MODULES["glob"] = module_with_params(runtime_module(
    "glob", "modules/glob.hpp", ("pcre2-8",),
    glob=(keyword_only(ListType(STR), (("pathname", STR),), GLOB_OPTIONS), "sd::glob::glob"),
    iglob=(keyword_only(GeneratorType(STR), (("pathname", STR),), GLOB_OPTIONS), "sd::glob::iglob"),
    escape=(signature(STR, ("pathname", STR)), "sd::glob::escape"),
    translate=(keyword_only(STR, (("pat", STR),), (("recursive", BOOL, "false"), ("include_hidden", BOOL, "false"),
                                                     ("seps", OptionalType(STR), "std::nullopt"))),
               "sd::glob::translate"),
))
