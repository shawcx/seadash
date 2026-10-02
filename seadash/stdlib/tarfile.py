"""`tarfile`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import (
    exception_class, module_with_params, MODULES, OneOf, PATH_LIKE, runtime_module, signature, sync_method,
)
from ..types import (
    BINARY_FILE, BOOL, BYTES, DictType, FLOAT, FuncType, INT, ListType, NONE, OptionalType, PATH, STR, StructType,
    TARFILE, TARINFO,
)


TAR_ERRORS: dict[str, StructType] = {}


def tar_exception(name: str, base: str) -> StructType:
    st = exception_class(name, f"sd::tarfile::{name}", TAR_ERRORS.get(base) or base)
    TAR_ERRORS[name] = st
    return st


TARINFO_FIELDS = {
    "name": STR, "path": STR, "mode": INT, "uid": INT, "gid": INT, "size": INT, "mtime": FLOAT, "chksum": INT,
    "type": BYTES, "linkname": STR, "linkpath": STR, "uname": STR, "gname": STR, "devmajor": INT, "devminor": INT,
    "offset": INT, "offset_data": INT, "pax_headers": DictType(STR, STR),
}
TARINFO.attributes.update({name: (lambda _, t=t: t) for name, t in TARINFO_FIELDS.items()})
TARINFO.setters.update(TARINFO_FIELDS)
TARINFO.methods.update({
    **{name: sync_method(BOOL) for name in ("isreg", "isfile", "isdir", "issym", "islnk", "ischr", "isblk", "isfifo",
                                            "issparse", "isdev")},
    "tobuf": sync_method(BYTES, ("format", INT, "sd::tarfile::DEFAULT_FORMAT"), ("encoding", STR, '"utf-8"s'),
                         ("errors", STR, '"surrogateescape"s')),
    "replace": sync_method(TARINFO, ("name", OptionalType(STR), "std::nullopt"), ("mtime", OptionalType(FLOAT), "std::nullopt"),
                           ("mode", OptionalType(INT), "std::nullopt"), ("linkname", OptionalType(STR), "std::nullopt"),
                           ("uid", OptionalType(INT), "std::nullopt"), ("gid", OptionalType(INT), "std::nullopt"),
                           ("uname", OptionalType(STR), "std::nullopt"), ("gname", OptionalType(STR), "std::nullopt"),
                           ("deep", BOOL, "true")),
})
TAR_MEMBER = OneOf(STR, TARINFO, what="a member's name (str) or a TarInfo")
TAR_PATH = OneOf(STR, PATH, NONE, what="a str, a Path or None")
TAR_FILTER = OneOf(STR, FuncType((TARINFO, STR), OptionalType(TARINFO)), NONE,
                   what='"data", "tar", "fully_trusted" or a function (member, path) -> TarInfo | None')
TARFILE.methods.update({
    "getmember": sync_method(TARINFO, ("name", STR)),
    "getmembers": sync_method(ListType(TARINFO)),
    "getnames": sync_method(ListType(STR)),
    "next": sync_method(OptionalType(TARINFO)),
    "extractfile": sync_method(OptionalType(BINARY_FILE), ("member", TAR_MEMBER)),
    "extract": sync_method(NONE, ("member", TAR_MEMBER), ("path", TAR_PATH, "std::nullopt"), ("set_attrs", BOOL, "true"),
                           ("numeric_owner", BOOL, "false"), ("filter", TAR_FILTER, "std::nullopt")),
    "extractall": sync_method(NONE, ("path", TAR_PATH, "std::nullopt"),
                              ("members", OneOf(ListType(TARINFO), NONE, what="a list of TarInfos or None"), "std::nullopt"),
                              ("numeric_owner", BOOL, "false"), ("filter", TAR_FILTER, "std::nullopt")),
    "add": sync_method(NONE, ("name", PATH_LIKE), ("arcname", TAR_PATH, "std::nullopt"), ("recursive", BOOL, "true"),
                       ("filter", OneOf(FuncType((TARINFO,), OptionalType(TARINFO)), NONE,
                                        what="a function (tarinfo) -> TarInfo | None"), "std::nullopt")),
    "addfile": sync_method(NONE, ("tarinfo", TARINFO), ("fileobj", OptionalType(BINARY_FILE), "std::nullopt")),
    "gettarinfo": sync_method(OptionalType(TARINFO), ("name", TAR_PATH, "std::nullopt"), ("arcname", TAR_PATH, "std::nullopt"),
                              ("fileobj", OptionalType(BINARY_FILE), "std::nullopt")),
    "list": sync_method(NONE, ("verbose", BOOL, "true"), ("members", OptionalType(ListType(TARINFO)), "std::nullopt")),
    "close": sync_method(NONE),
})
TARFILE.attributes.update({
    "name": lambda t: OptionalType(STR), "mode": lambda t: STR, "format": lambda t: INT,
    "pax_headers": lambda t: DictType(STR, STR), "closed": lambda t: BOOL,
})
TAR_OPEN = signature(
    TARFILE, ("name", TAR_PATH, "std::nullopt"), ("mode", STR, '"r"s'), ("fileobj", OptionalType(BINARY_FILE), "std::nullopt"),
    ("bufsize", INT, "10240_i"), ("format", OptionalType(INT), "std::nullopt"), ("compresslevel", OptionalType(INT), "std::nullopt"),
    ("preset", OptionalType(INT), "std::nullopt"), ("ignore_zeros", BOOL, "false"), ("dereference", BOOL, "false"),
    ("errorlevel", INT, "1_i"), ("pax_headers", OptionalType(DictType(STR, STR)), "std::nullopt"))
TAR_FILTER_FN = signature(OptionalType(TARINFO), ("member", TARINFO), ("dest_path", STR))
MODULES["tarfile"] = module_with_params(runtime_module(
    "tarfile", "modules/tarfile.hpp", ("z", "bz2", "lzma"),
    open=(TAR_OPEN, "sd::tarfile::open_"),
    TarFile=(signature(TARFILE, ("name", TAR_PATH, "std::nullopt"), ("mode", STR, '"r"s'),
                       ("fileobj", OptionalType(BINARY_FILE), "std::nullopt"), ("format", OptionalType(INT), "std::nullopt")),
             "sd::tarfile::TarFile"),
    TarInfo=(signature(TARINFO, ("name", STR, '""s')), "sd::tarfile::make_info"),
    is_tarfile=(signature(BOOL, ("name", OneOf(STR, PATH, BINARY_FILE, what="a str, a Path or a binary file object"))),
                "sd::tarfile::is_tarfile"),
    data_filter=(TAR_FILTER_FN, "sd::tarfile::data_filter"),
    tar_filter=(TAR_FILTER_FN, "sd::tarfile::tar_filter"),
    fully_trusted_filter=(TAR_FILTER_FN, "sd::tarfile::fully_trusted_filter"),
    TarError=tar_exception("TarError", "Exception"),
    ReadError=tar_exception("ReadError", "TarError"),
    CompressionError=tar_exception("CompressionError", "TarError"),
    StreamError=tar_exception("StreamError", "TarError"),
    ExtractError=tar_exception("ExtractError", "TarError"),
    HeaderError=tar_exception("HeaderError", "TarError"),
    FilterError=tar_exception("FilterError", "TarError"),
    AbsolutePathError=tar_exception("AbsolutePathError", "FilterError"),
    OutsideDestinationError=tar_exception("OutsideDestinationError", "FilterError"),
    SpecialFileError=tar_exception("SpecialFileError", "FilterError"),
    AbsoluteLinkError=tar_exception("AbsoluteLinkError", "FilterError"),
    LinkOutsideDestinationError=tar_exception("LinkOutsideDestinationError", "FilterError"),
    LinkFallbackError=tar_exception("LinkFallbackError", "FilterError"),
    **{name: (BYTES, f"sd::bytes(sd::tarfile::{name})") for name in (
        "REGTYPE", "AREGTYPE", "LNKTYPE", "SYMTYPE", "CHRTYPE", "BLKTYPE", "DIRTYPE", "FIFOTYPE", "CONTTYPE",
        "GNUTYPE_LONGNAME", "GNUTYPE_LONGLINK", "GNUTYPE_SPARSE", "XHDTYPE", "XGLTYPE", "SOLARIS_XHDTYPE")},
    **{name: (INT, f"sd::tarfile::{name}") for name in ("USTAR_FORMAT", "GNU_FORMAT", "PAX_FORMAT", "DEFAULT_FORMAT")},
    BLOCKSIZE=(INT, "static_cast<std::int64_t>(sd::tarfile::BLOCKSIZE)"),
    RECORDSIZE=(INT, "static_cast<std::int64_t>(sd::tarfile::RECORDSIZE)"),
    ENCODING=(STR, "sd::tarfile::ENCODING"),
))
MODULES["tarfile"].members["TarFile"].as_type = TARFILE
MODULES["tarfile"].members["TarInfo"].as_type = TARINFO
