"""`gzip`, `bz2` and `lzma`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from collections.abc import Callable

from .. import ast as A
from ..builtins import (
    CallContext, MODULES, exception_class, module, module_with_params, open_mode, runtime_module, signature,
)
from ..types import (
    BINARY_FILE, BOOL, BYTES, BZ2_COMPRESSOR, BZ2_DECOMPRESSOR, INT, LZMA_COMPRESSOR, LZMA_DECOMPRESSOR,
    OptionalType, PATH, STR, TEXT_FILE, TYPE_OBJECT, Type,
)


# gzip.open(filename, mode="rb", compresslevel=9, ...), bz2.open (the same) and
# lzma.open(filename, mode="rb", *, format=None, check=-1, preset=None, ...): binary unless
# the mode says 't', like Python (and unlike open()). Codegen passes these options in order.
COMPRESSED_OPEN_OPTIONS = {
    "gzip": (("compresslevel", INT, "9_i"),),
    "bz2": (("compresslevel", INT, "9_i"),),
    "lzma": (("format", OptionalType(INT), "std::nullopt"), ("check", INT, "(-1_i)"), ("preset", OptionalType(INT), "std::nullopt")),
}


def compressed_open(module: str, binary_only: bool = False) -> Callable[[CallContext], Type]:
    """gzip.open and friends; binary_only for the GzipFile/BZ2File/LZMAFile classes, which
    are the binary file object (mode "r" means "rb"; text modes are an error)."""
    options = COMPRESSED_OPEN_OPTIONS[module]
    positional = 3 if module != "lzma" else 2  # (lzma's options are keyword-only)

    def handler(ctx: CallContext) -> Type:
        n = ctx.arity(0, positional, keywords=("filename", "mode", *(o[0] for o in options), "encoding", "newline"))
        if n == 0:
            if ctx.keyword_arg("filename") is None:
                raise ctx.error(f"{ctx.what} needs a filename (an existing file object isn't supported yet)")
            ctx.keyword("filename", STR)
        else:
            ctx.need(0, lambda t: t in (STR, PATH), "a str or Path")
        mode_node = ctx.args[1] if n >= 2 else ctx.keyword_arg("mode")
        if binary_only and isinstance(mode_node, A.StrLit) and "t" in mode_node.value:
            raise ctx.error(f"Invalid mode: {mode_node.value!r} ({ctx.what} is a binary file; use {module}.open for text)", mode_node)
        if n == 3:
            ctx.expect(2, options[0][1])
        else:
            for name, t, _ in options:
                ctx.keyword(name, t)
        if (nl := ctx.keyword_arg("newline")) is not None:
            ctx.checker.check_expr(nl)
        if mode_node is None:
            return BINARY_FILE
        open_mode(ctx, mode_node)
        return TEXT_FILE if "t" in mode_node.value else BINARY_FILE

    return handler


MODULES["gzip"] = module_with_params(runtime_module(
    "gzip", "modules/gzip.hpp", ("z",),
    compress=(signature(BYTES, ("data", BYTES), ("compresslevel", INT, "9_i"), ("mtime", OptionalType(INT), "std::nullopt")),
              "sd::gzip::compress"),
    decompress=(signature(BYTES, ("data", BYTES)), "sd::gzip::decompress"),
    open=(compressed_open("gzip"), None),
    GzipFile=(compressed_open("gzip", binary_only=True), None),
    BadGzipFile=exception_class("BadGzipFile", "sd::gzip::BadGzipFile", "OSError"),
))
MODULES["gzip"].members["GzipFile"].as_type = BINARY_FILE
MODULES["bz2"] = module_with_params(runtime_module(
    "bz2", "modules/bz2.hpp", ("bz2",),
    compress=(signature(BYTES, ("data", BYTES), ("compresslevel", INT, "9_i")), "sd::bz2::compress"),
    decompress=(signature(BYTES, ("data", BYTES)), "sd::bz2::decompress"),
    open=(compressed_open("bz2"), None),
    BZ2File=(compressed_open("bz2", binary_only=True), None),
    BZ2Compressor=(signature(BZ2_COMPRESSOR, ("compresslevel", INT, "9_i")), "sd::bz2::BZ2Compressor"),
    BZ2Decompressor=(signature(BZ2_DECOMPRESSOR), "sd::bz2::BZ2Decompressor"),
))
MODULES["bz2"].members["BZ2File"].as_type = BINARY_FILE
MODULES["bz2"].members["BZ2Compressor"].as_type = BZ2_COMPRESSOR
MODULES["bz2"].members["BZ2Decompressor"].as_type = BZ2_DECOMPRESSOR
MODULES["lzma"] = module_with_params(runtime_module(
    "lzma", "modules/lzma.hpp", ("lzma",),
    compress=(signature(BYTES, ("data", BYTES), ("format", INT, "sd::lzma::FORMAT_XZ"), ("check", INT, "(-1_i)"),
                        ("preset", OptionalType(INT), "std::nullopt")), "sd::lzma::compress"),
    decompress=(signature(BYTES, ("data", BYTES), ("format", INT, "sd::lzma::FORMAT_AUTO")), "sd::lzma::decompress"),
    open=(compressed_open("lzma"), None),
    LZMAFile=(compressed_open("lzma", binary_only=True), None),
    LZMACompressor=(signature(LZMA_COMPRESSOR, ("format", INT, "sd::lzma::FORMAT_XZ"), ("check", INT, "(-1_i)"),
                              ("preset", OptionalType(INT), "std::nullopt")), "sd::lzma::LZMACompressor"),
    LZMADecompressor=(signature(LZMA_DECOMPRESSOR, ("format", INT, "sd::lzma::FORMAT_AUTO"), ("memlimit", OptionalType(INT), "std::nullopt")),
                      "sd::lzma::LZMADecompressor"),
    is_check_supported=(signature(BOOL, ("check_id", INT)), "sd::lzma::is_check_supported"),
    LZMAError=exception_class("LZMAError", "sd::lzma::LZMAError"),
    **{name: (INT, f"sd::lzma::{name}") for name in (
        "FORMAT_AUTO", "FORMAT_XZ", "FORMAT_ALONE", "FORMAT_RAW", "CHECK_NONE", "CHECK_CRC32", "CHECK_CRC64", "CHECK_SHA256",
        "CHECK_UNKNOWN", "PRESET_DEFAULT", "PRESET_EXTREME")},
))
MODULES["lzma"].members["LZMAFile"].as_type = BINARY_FILE
MODULES["lzma"].members["LZMACompressor"].as_type = LZMA_COMPRESSOR
MODULES["lzma"].members["LZMADecompressor"].as_type = LZMA_DECOMPRESSOR
# type(x): its name (and module), compared with classes in the checker (Checker.named_type_object)
TYPE_OBJECT.attributes.update({"__name__": lambda t: STR, "__qualname__": lambda t: STR, "__module__": lambda t: STR})
