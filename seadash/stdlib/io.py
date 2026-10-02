"""`io`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from .. import ast as A
from ..builtins import CallContext, MODULES, NamedType, module_with_params, runtime_module, signature
from ..types import BINARY_FILE, BYTES, BYTES_IO, OptionalType, STR, STRING_IO, TEXT_FILE, Type


STRING_IO_SIGNATURE = signature(STRING_IO, ("initial_value", OptionalType(STR), "std::nullopt"),
                                ("newline", OptionalType(STR), 'std::optional<std::string>("\\n"s)'))


def string_io(ctx: CallContext) -> Type:
    """io.StringIO(initial_value="", newline="\\n"), with a literal newline= checked here."""
    t = STRING_IO_SIGNATURE(ctx)
    node = ctx.args[1] if len(ctx.args) > 1 else ctx.keyword_arg("newline")
    if isinstance(node, A.StrLit) and node.value not in ("", "\n", "\r", "\r\n"):
        raise ctx.error(f"illegal newline value: {node.value!r} (it can be None, '', '\\n', '\\r' or '\\r\\n')", node)
    return t


string_io.params = STRING_IO_SIGNATURE.params
MODULES["io"] = module_with_params(runtime_module(
    "io", "modules/io.hpp",
    StringIO=(string_io, "sd::io::string_io"),
    BytesIO=(signature(BYTES_IO, ("initial_bytes", BYTES, "sd::bytes()")), "sd::io::bytes_io"),
    TextIOBase=NamedType("TextIOBase", TEXT_FILE),
    BufferedIOBase=NamedType("BufferedIOBase", BINARY_FILE),
))
MODULES["io"].members["StringIO"].as_type = STRING_IO
MODULES["io"].members["BytesIO"].as_type = BYTES_IO
