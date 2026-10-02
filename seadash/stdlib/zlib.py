"""`zlib`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import MODULES, bytes_fn, exception_class, module_with_params, runtime_module, signature, sync_method
from ..types import (
    BOOL, BYTES, BZ2_COMPRESSOR, BZ2_DECOMPRESSOR, INT, LZMA_COMPRESSOR, LZMA_DECOMPRESSOR, STR, ZLIB_COMPRESS,
    ZLIB_DECOMPRESS,
)


ZLIB_CONSTANTS = {
    "Z_BEST_SPEED": 1, "Z_BEST_COMPRESSION": 9, "Z_DEFAULT_COMPRESSION": -1, "Z_NO_FLUSH": 0, "Z_PARTIAL_FLUSH": 1,
    "Z_SYNC_FLUSH": 2, "Z_FULL_FLUSH": 3, "Z_FINISH": 4, "Z_BLOCK": 5, "DEFLATED": 8, "MAX_WBITS": 15, "DEF_MEM_LEVEL": 8,
    "DEF_BUF_SIZE": 16384, "Z_DEFAULT_STRATEGY": 0, "Z_FILTERED": 1, "Z_HUFFMAN_ONLY": 2, "Z_RLE": 3, "Z_FIXED": 4,
}
MODULES["zlib"] = module_with_params(runtime_module(
    "zlib", "modules/zlib.hpp", ("z",),
    compress=(bytes_fn(BYTES, 1, 2, (INT,)), "sd::zlib::compress"),
    decompress=(bytes_fn(BYTES), "sd::zlib::decompress"),
    crc32=(bytes_fn(INT, 1, 2, (INT,)), "sd::zlib::crc32"),
    adler32=(bytes_fn(INT, 1, 2, (INT,)), "sd::zlib::adler32"),
    compressobj=(signature(ZLIB_COMPRESS, ("level", INT, "(-1_i)"), ("method", INT, "8_i"), ("wbits", INT, "15_i"),
                           ("memLevel", INT, "8_i"), ("strategy", INT, "0_i")), "sd::zlib::compressobj"),
    decompressobj=(signature(ZLIB_DECOMPRESS, ("wbits", INT, "15_i")), "sd::zlib::decompressobj"),
    error=exception_class("error", "sd::zlib::error"),
    **{name: (INT, f"({value}_i)") for name, value in ZLIB_CONSTANTS.items()},
    ZLIB_VERSION=(STR, "std::string(ZLIB_VERSION)"),
))
# The incremental (de)compressors' methods and attributes.
ZLIB_COMPRESS.methods.update({
    "compress": sync_method(BYTES, ("data", BYTES)), "flush": sync_method(BYTES, ("mode", INT, "4_i")),
    "copy": sync_method(ZLIB_COMPRESS)})
ZLIB_DECOMPRESS.methods.update({
    "decompress": sync_method(BYTES, ("data", BYTES), ("max_length", INT, "0_i")),
    "flush": sync_method(BYTES, ("length", INT, "16384_i")), "copy": sync_method(ZLIB_DECOMPRESS)})
ZLIB_DECOMPRESS.attributes.update(
    {"eof": lambda t: BOOL, "unused_data": lambda t: BYTES, "unconsumed_tail": lambda t: BYTES})
for _t in (BZ2_COMPRESSOR, LZMA_COMPRESSOR):
    _t.methods.update({"compress": sync_method(BYTES, ("data", BYTES)), "flush": sync_method(BYTES)})
for _t in (BZ2_DECOMPRESSOR, LZMA_DECOMPRESSOR):
    _t.methods.update({"decompress": sync_method(BYTES, ("data", BYTES), ("max_length", INT, "(-1_i)"))})
    _t.attributes.update({"eof": lambda t: BOOL, "needs_input": lambda t: BOOL, "unused_data": lambda t: BYTES})
LZMA_DECOMPRESSOR.attributes["check"] = lambda t: INT
