"""`statistics`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from .. import ast as A
from ..builtins import (
    CallContext, MODULES, NamedType, bind_args, exception_class, mark_tuple_iterable, mixed_tuple_hint,
    module_with_params, ordered, runtime_module, signature,
)
from ..types import (
    BOOL, FLOAT, INT, LINEAR_REGRESSION, ListType, NORMAL_DIST, OptionalType, STR, Type, assignable, element_type,
    is_hashable, is_numeric,
)


NUMBERS = object()  # an iterable of ints or floats (passed to C++ as it is)
ORDERED_ITEMS = object()  # an iterable of anything ordered
HASHABLE_ITEMS = object()  # an iterable of anything hashable
NUMBERS_OR_NONE = object()  # weights=: an iterable of numbers, or None
ITEM_KINDS = {NUMBERS: "numbers (ints or floats)", NUMBERS_OR_NONE: "numbers (ints or floats)",
              ORDERED_ITEMS: "items that can be compared", HASHABLE_ITEMS: "hashable items"}


def stats_fn(result, *params, positional: int | None = None, positional_only: int = 0):
    """A statistics function. A param is (name, type or one of the item kinds above[, C++
    default]); `positional` is how many may be given positionally (the rest are keyword-only),
    and the first `positional_only` can't be given by keyword. `result` is a Type or a
    function of the first argument's item type."""

    def handler(ctx: CallContext) -> Type:
        most = len(params) if positional is None else positional
        if len(ctx.args) > most:
            takes = f"{most} positional argument{'s' if most != 1 else ''}"
            raise ctx.error(f"{ctx.what} takes {takes} but {len(ctx.args)} were given")
        only = [p[0] for p in params[:positional_only]]
        if bad := [kw.name for kw in ctx.call.keywords if kw.name in only]:
            raise ctx.error(f"{ctx.what} got some positional-only arguments passed as keyword arguments: "
                            f"'{', '.join(bad)}'")
        args = bind_args(ctx, params)
        first = None
        for name, kind, *_ in params:
            node = args.get(name)
            if node is None:
                continue
            if kind is NUMBERS_OR_NONE and isinstance(node, A.NoneLit):
                ctx.checker.check_expr(node)
                continue
            if kind in ITEM_KINDS:
                empty = isinstance(node, A.ListLit) and not node.elts and kind in (NUMBERS, NUMBERS_OR_NONE)
                t = ctx.checker.check_expr(node, ListType(FLOAT) if empty else None)  # (mean([]) is an error at run time)
                elem = element_type(t)
                if elem is None:
                    raise ctx.error(f"{ctx.what} argument '{name}' must be something you can loop over, not {t}"
                                    f"{mixed_tuple_hint(t)}", node)
                mark_tuple_iterable(node, t, elem)
                ok = {ORDERED_ITEMS: ordered, HASHABLE_ITEMS: is_hashable}.get(kind, is_numeric)(elem)
                if not ok:
                    raise ctx.error(f"{ctx.what} argument '{name}' needs {ITEM_KINDS[kind]}, not {t}", node)
                first = elem if first is None else first
                continue
            actual = ctx.checker.check_expr(node, kind)
            if not assignable(actual, kind):
                raise ctx.error(f"{ctx.what} argument '{name}' must be {kind}, not {actual}", node)
        return result(first) if callable(result) else result

    handler.params = params
    return handler


def stats_spread(name: str, center: str):
    """variance(data, xbar=None) and the like."""
    return stats_fn(FLOAT, ("data", NUMBERS), (center, OptionalType(FLOAT), "std::nullopt")), f"sd::statistics::{name}"


MODULES["statistics"] = module_with_params(runtime_module(
    "statistics", "modules/statistics.hpp",
    mean=(stats_fn(FLOAT, ("data", NUMBERS)), "sd::statistics::mean"),
    fmean=(stats_fn(FLOAT, ("data", NUMBERS), ("weights", NUMBERS_OR_NONE, "std::nullopt")), "sd::statistics::fmean"),
    geometric_mean=(stats_fn(FLOAT, ("data", NUMBERS)), "sd::statistics::geometric_mean"),
    harmonic_mean=(stats_fn(FLOAT, ("data", NUMBERS), ("weights", NUMBERS_OR_NONE, "std::nullopt")),
                   "sd::statistics::harmonic_mean"),
    median=(stats_fn(FLOAT, ("data", NUMBERS)), "sd::statistics::median"),
    median_low=(stats_fn(lambda elem: elem, ("data", ORDERED_ITEMS)), "sd::statistics::median_low"),
    median_high=(stats_fn(lambda elem: elem, ("data", ORDERED_ITEMS)), "sd::statistics::median_high"),
    median_grouped=(stats_fn(FLOAT, ("data", NUMBERS), ("interval", FLOAT, "1.0")), "sd::statistics::median_grouped"),
    mode=(stats_fn(lambda elem: elem, ("data", HASHABLE_ITEMS)), "sd::statistics::mode"),
    multimode=(stats_fn(ListType, ("data", HASHABLE_ITEMS)), "sd::statistics::multimode"),
    quantiles=(stats_fn(ListType(FLOAT), ("data", NUMBERS), ("n", INT, "4"), ("method", STR, '"exclusive"s'),
                        positional=1), "sd::statistics::quantiles"),
    variance=stats_spread("variance", "xbar"),
    stdev=stats_spread("stdev", "xbar"),
    pvariance=stats_spread("pvariance", "mu"),
    pstdev=stats_spread("pstdev", "mu"),
    covariance=(stats_fn(FLOAT, ("x", NUMBERS), ("y", NUMBERS), positional_only=2), "sd::statistics::covariance"),
    correlation=(stats_fn(FLOAT, ("x", NUMBERS), ("y", NUMBERS), ("method", STR, '"linear"s'), positional=2,
                          positional_only=2), "sd::statistics::correlation"),
    linear_regression=(stats_fn(LINEAR_REGRESSION, ("x", NUMBERS), ("y", NUMBERS), ("proportional", BOOL, "false"),
                                positional=2, positional_only=2), "sd::statistics::linear_regression"),
    NormalDist=(signature(NORMAL_DIST, ("mu", FLOAT, "0.0"), ("sigma", FLOAT, "1.0")), "sd::statistics::NormalDist::make"),
    StatisticsError=exception_class("StatisticsError", "sd::statistics::StatisticsError", "ValueError"),
))
MODULES["statistics"].members["NormalDist"].as_type = NORMAL_DIST
MODULES["statistics"].members["LinearRegression"] = NamedType("LinearRegression", LINEAR_REGRESSION)  # (annotations)
LINEAR_REGRESSION.attributes.update({"slope": lambda t: FLOAT, "intercept": lambda t: FLOAT})
NORMAL_DIST.attributes.update({name: (lambda t: FLOAT) for name in ("mean", "median", "mode", "stdev", "variance")})
