"""`hashlib` and `hmac`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from .. import ast as A
from ..builtins import CallContext, Function, MODULES, Module, NamedType, Value, bind_args, signature, sync_method
from ..types import BINARY_FILE, BOOL, BYTEARRAY, BYTES, HASH, HMAC_T, INT, NONE, OptionalType, STR, SetType, Type


HASH_NAMES = ("md5", "sha1", "sha224", "sha256", "sha384", "sha512", "sha3_224", "sha3_256", "sha3_384", "sha3_512",
              "blake2b", "blake2s", "shake_128", "shake_256")


def hash_data(ctx: CallContext, node: A.Expr, what: str) -> None:
    t = ctx.checker.check_expr(node, BYTES)
    if t == STR:
        raise ctx.error(f"{what}: strings must be encoded before hashing; use s.encode()", node)
    if t not in (BYTES, BYTEARRAY):
        raise ctx.error(f"{what} must be bytes, not {t}", node)


def hash_constructor(name: str | None) -> Callable[[CallContext], Type]:
    """hashlib.sha256(data=b"") / hashlib.new(name, data=b"")."""
    def handler(ctx: CallContext) -> Type:
        params = ((("name", STR),) if name is None else ()) + (("data", None, True), ("string", None, True),
                                                             ("usedforsecurity", BOOL, True))
        args = bind_args(ctx, params)
        if name is None:
            ctx.checker.expect_type(args["name"], STR, "hashlib.new() name")
        for key in ("data", "string"):
            if key in args:
                hash_data(ctx, args[key], f"{ctx.what} data")
        if "usedforsecurity" in args:
            ctx.checker.expect_type(args["usedforsecurity"], BOOL, "usedforsecurity")
        ctx.call.notes["hash_args"] = args
        return HASH

    return handler


def hash_update(ctx: CallContext) -> Type:
    ctx.arity(1)
    hash_data(ctx, ctx.args[0], f"{ctx.what} argument")
    return NONE


def digest_name(ctx: CallContext, node: A.Expr) -> None:
    """digestmod= / digest=: a name ('sha256') or a hashlib constructor (hashlib.sha256)."""
    target = None
    if isinstance(node, A.Attribute) and isinstance(node.value, A.Name) and ctx.checker.modules.get(node.value.id) is MODULES["hashlib"]:
        target = node.attr
    elif isinstance(node, A.Name) and node.id in ctx.checker.imported and ctx.checker.imported[node.id][0] is MODULES["hashlib"]:
        target = ctx.checker.imported[node.id][1]
    if target in HASH_NAMES:
        node.notes["hash_name"] = target
        node.notes["compile_time"] = True
        node.ty = STR
        return
    ctx.checker.expect_type(node, STR, f"{ctx.what} digest")


def hmac_new(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("key", None), ("msg", None, True), ("digestmod", None, True)))
    hash_data(ctx, args["key"], "hmac key")
    if "msg" in args and not isinstance(args["msg"], A.NoneLit):
        hash_data(ctx, args["msg"], "hmac msg")
    if "digestmod" not in args:
        raise ctx.error("hmac.new() needs digestmod= (like hashlib.sha256 or 'sha256')")
    digest_name(ctx, args["digestmod"])
    ctx.call.notes["hash_args"] = args
    return HMAC_T


def hmac_digest(ctx: CallContext) -> Type:
    args = bind_args(ctx, (("key", None), ("msg", None), ("digest", None)))
    hash_data(ctx, args["key"], "hmac key")
    hash_data(ctx, args["msg"], "hmac msg")
    digest_name(ctx, args["digest"])
    ctx.call.notes["hash_args"] = args
    return BYTES


def compare_digest(ctx: CallContext) -> Type:
    ctx.arity(2)
    a, b = ctx.arg(0), ctx.arg(1)
    if a != b or a not in (STR, BYTES):
        raise ctx.error(f"compare_digest() compares two str or two bytes, not {a} and {b}")
    return BOOL


HASH.methods.update({
    "update": hash_update,
    "digest": sync_method(BYTES, ("length", OptionalType(INT), "std::nullopt")),
    "hexdigest": sync_method(STR, ("length", OptionalType(INT), "std::nullopt")),
    "copy": sync_method(HASH),
})
HMAC_T.methods.update({
    "update": hash_update,
    "digest": sync_method(BYTES),
    "hexdigest": sync_method(STR),
    "copy": sync_method(HMAC_T),
})
for _t in (HASH, HMAC_T):
    _t.attributes.update({"name": lambda t: STR, "digest_size": lambda t: INT, "block_size": lambda t: INT})
MODULES["hashlib"] = Module("hashlib", {
    **{name: Function(name, hash_constructor(name), as_type=HASH) for name in HASH_NAMES},
    "new": Function("new", hash_constructor(None)),
    "pbkdf2_hmac": Function("pbkdf2_hmac", signature(
        BYTES, ("hash_name", STR), ("password", BYTES), ("salt", BYTES), ("iterations", INT),
        ("dklen", OptionalType(INT), "std::nullopt")), "sd::hashlib::pbkdf2_hmac"),
    "file_digest": Function("file_digest", signature(HASH, ("fileobj", BINARY_FILE), ("digest", STR)),
                            "sd::hashlib::file_digest"),
    "algorithms_guaranteed": Value("algorithms_guaranteed", SetType(STR), "sd::hashlib::algorithms_guaranteed()"),
    "algorithms_available": Value("algorithms_available", SetType(STR), "sd::hashlib::algorithms_available()"),
}, "modules/hashlib.hpp", ("crypto",))
for _name in ("pbkdf2_hmac", "file_digest"):
    MODULES["hashlib"].members[_name].params = MODULES["hashlib"].members[_name].check.params
MODULES["hmac"] = Module("hmac", {
    "new": Function("new", hmac_new),
    "digest": Function("digest", hmac_digest),
    "compare_digest": Function("compare_digest", compare_digest, "sd::hmac::compare_digest"),
    "HMAC": NamedType("HMAC", HMAC_T),
}, "modules/hashlib.hpp", ("crypto",))
