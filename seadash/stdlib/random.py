"""`random`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import CallContext, MODULES, module_with_params, runtime_module, signature
from ..types import (
    BYTEARRAY, BYTES, FLOAT, INT, IterType, ListType, NONE, OptionalType, STR, TupleType, Type, assignable,
)


def sequence_elem(t: Type) -> Type | None:
    """What indexing a sequence gives, for choice()/sample(): lists, str, bytes, range, uniform tuples."""
    match t:
        case ListType(elem):
            return elem
        case IterType(elem, "range"):
            return elem
        case TupleType(elts) if elts and len(set(elts)) == 1:
            return elts[0]
    if t == STR:
        return STR
    if t in (BYTES, BYTEARRAY):
        return INT
    return None


def sequence_arg(ctx: CallContext, i: int) -> Type:
    t = ctx.arg(i)
    elem = sequence_elem(t)
    if elem is None:
        raise ctx.error(f"{ctx.what} needs a sequence (a list, str, bytes, range or tuple), not {t}", ctx.args[i])
    return elem


def random_randrange(ctx: CallContext) -> Type:
    for i in range(ctx.arity(1, 3)):
        ctx.expect(i, INT)
    return INT


def random_choice(ctx: CallContext) -> Type:
    ctx.arity(1)
    return sequence_arg(ctx, 0)


def random_shuffle(ctx: CallContext) -> Type:
    ctx.arity(1)
    t = ctx.arg(0)
    if not isinstance(t, ListType):
        raise ctx.error(f"{ctx.what} shuffles a list in place, not a {t}", ctx.args[0])
    return NONE


def random_sample(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2, keywords=("k",))
    elem = sequence_arg(ctx, 0)
    k = ctx.args[1] if n == 2 else ctx.keyword_arg("k")
    if k is None:
        raise ctx.error(f"{ctx.what} is missing argument 'k'")
    ctx.checker.expect_type(k, INT, "sample size")
    return ListType(elem)


CHOICES_PARAMS = (
    ("population", None),
    ("weights", OptionalType(ListType(FLOAT)), "std::nullopt"),
    ("cum_weights", OptionalType(ListType(FLOAT)), "std::nullopt"),
    ("k", INT, "1_i"),
)


def random_choices(ctx: CallContext) -> Type:
    n = ctx.arity(1, 2, keywords=("weights", "cum_weights", "k"))
    elem = sequence_arg(ctx, 0)
    weights = ctx.args[1] if n == 2 else ctx.keyword_arg("weights")
    for node in (weights, ctx.keyword_arg("cum_weights")):
        if node is not None:
            t = ctx.checker.check_expr(node, ListType(FLOAT))
            if not assignable(t, OptionalType(ListType(FLOAT))):
                raise ctx.error(f"{ctx.what} weights must be a list[float], not {t}", node)
    ctx.keyword("k", INT)
    return ListType(elem)


random_choices.params = CHOICES_PARAMS
random_sample.params = (("population", None), ("k", INT))
MODULES["random"] = module_with_params(runtime_module(
    "random", "modules/random.hpp",
    seed=(signature(NONE, ("a", OptionalType(INT), "std::nullopt")), "sd::random::seed"),
    random=(signature(FLOAT), "sd::random::random"),
    randint=(signature(INT, ("a", INT), ("b", INT)), "sd::random::randint"),
    randrange=(random_randrange, "sd::random::randrange"),
    uniform=(signature(FLOAT, ("a", FLOAT), ("b", FLOAT)), "sd::random::uniform"),
    gauss=(signature(FLOAT, ("mu", FLOAT, "0.0"), ("sigma", FLOAT, "1.0")), "sd::random::gauss"),
    getrandbits=(signature(INT, ("k", INT)), "sd::random::getrandbits"),
    randbytes=(signature(BYTES, ("n", INT)), "sd::random::randbytes"),
    choice=(random_choice, "sd::random::choice"),
    choices=(random_choices, "sd::random::choices"),
    shuffle=(random_shuffle, "sd::random::shuffle"),
    sample=(random_sample, "sd::random::sample"),
))
MODULES["random"].members["shuffle"].mutates_first_arg = True
