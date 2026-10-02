"""`zipfile`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import (
    MODULES, OneOf, PATH_LIKE, exception_class, module_with_params, runtime_module, signature, sync_method,
)
from ..types import (
    BINARY_FILE, BOOL, BYTES, BuiltinClass, INT, ListType, NONE, OptionalType, PATH, STR, TEXT_FILE, TupleType,
)


# A ZipFile is a handle on the archive (as a Python one is a reference); so is a ZipInfo, whose
# fields can be changed, like Python's (changing one from infolist() changes the archive's).
ZIPFILE = BuiltinClass("zipfile.ZipFile", "sd::zipfile::ZipFile")
ZIPINFO = BuiltinClass("zipfile.ZipInfo", "sd::zipfile::ZipInfo")
ZIP_DATE_TIME = TupleType((INT,) * 6)
ZIP_MEMBER = OneOf(STR, ZIPINFO, what="a member's name (str) or a ZipInfo")
ZIP_PATH = OneOf(STR, PATH, NONE, what="a str, a Path or None")
ZIPINFO_FIELDS = {
    "filename": STR, "orig_filename": STR, "date_time": ZIP_DATE_TIME, "compress_type": INT,
    "compress_level": OptionalType(INT), "comment": BYTES, "extra": BYTES,
    **{f: INT for f in ("create_system", "create_version", "extract_version", "reserved", "flag_bits", "volume",
                        "internal_attr", "external_attr", "header_offset", "CRC", "compress_size", "file_size")},
}
ZIPINFO.attributes.update({name: (lambda _, t=t: t) for name, t in ZIPINFO_FIELDS.items()})
ZIPINFO.setters.update(ZIPINFO_FIELDS)
ZIPINFO.methods.update({"is_dir": sync_method(BOOL)})
ZIP_COMPRESSION = (("compress_type", OptionalType(INT), "std::nullopt"), ("compresslevel", OptionalType(INT), "std::nullopt"))
ZIPFILE.methods.update({
    "close": sync_method(NONE),
    "namelist": sync_method(ListType(STR)),
    "infolist": sync_method(ListType(ZIPINFO)),
    "getinfo": sync_method(ZIPINFO, ("name", STR)),
    "printdir": sync_method(NONE, ("file", OptionalType(TEXT_FILE), "std::nullopt")),
    "testzip": sync_method(OptionalType(STR)),
    "read": sync_method(BYTES, ("name", ZIP_MEMBER)),
    "open": sync_method(BINARY_FILE, ("name", ZIP_MEMBER), ("mode", STR, '"r"s'), ("force_zip64", BOOL, "false")),
    "write": sync_method(NONE, ("filename", PATH_LIKE), ("arcname", ZIP_PATH, "std::nullopt"), *ZIP_COMPRESSION),
    "writestr": sync_method(NONE, ("zinfo_or_arcname", ZIP_MEMBER), ("data", OneOf(BYTES, STR, what="bytes or a str")),
                          *ZIP_COMPRESSION),
    "mkdir": sync_method(NONE, ("zinfo_or_directory_name", ZIP_MEMBER), ("mode", INT, "511_i")),
    "extract": sync_method(STR, ("member", ZIP_MEMBER), ("path", ZIP_PATH, "std::nullopt")),
    "extractall": sync_method(NONE, ("path", ZIP_PATH, "std::nullopt"),
                              ("members", OneOf(ListType(STR), ListType(ZIPINFO), NONE, what="a list of names or of ZipInfos"),
                               "std::nullopt")),
})
ZIPFILE.attributes.update({
    "comment": lambda t: BYTES, "filename": lambda t: OptionalType(STR), "mode": lambda t: STR,
    "compression": lambda t: INT, "compresslevel": lambda t: OptionalType(INT),
})
ZIPFILE.setters.update({"comment": BYTES})
ZIP_ARCHIVE = OneOf(STR, PATH, BINARY_FILE, what="a str, a Path or a binary file object")
BAD_ZIP_FILE = exception_class("BadZipFile", "sd::zipfile::BadZipFile")
MODULES["zipfile"] = module_with_params(runtime_module(
    "zipfile", "modules/zipfile.hpp", ("z", "bz2", "lzma"),
    ZipFile=(signature(ZIPFILE, ("file", ZIP_ARCHIVE), ("mode", STR, '"r"s'), ("compression", INT, "0_i"),
                       ("allowZip64", BOOL, "true"), ("compresslevel", OptionalType(INT), "std::nullopt"),
                       ("strict_timestamps", BOOL, "true")), "sd::zipfile::ZipFile"),
    ZipInfo=(signature(ZIPINFO, ("filename", STR, '"NoName"s'),
                       ("date_time", ZIP_DATE_TIME, "sd::zipfile::DateTime{1980, 1, 1, 0, 0, 0}")), "sd::zipfile::make_info"),
    is_zipfile=(signature(BOOL, ("filename", ZIP_ARCHIVE)), "sd::zipfile::is_zipfile"),
    BadZipFile=BAD_ZIP_FILE,
    BadZipfile=BAD_ZIP_FILE,
    error=BAD_ZIP_FILE,
    LargeZipFile=exception_class("LargeZipFile", "sd::zipfile::LargeZipFile"),
    **{name: (INT, f"sd::zipfile::{name}") for name in ("ZIP_STORED", "ZIP_DEFLATED", "ZIP_BZIP2", "ZIP_LZMA")},
))
MODULES["zipfile"].members["ZipFile"].as_type = ZIPFILE
MODULES["zipfile"].members["ZipInfo"].as_type = ZIPINFO
