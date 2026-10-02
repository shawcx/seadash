"""`datetime` and `zoneinfo`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import (
    class_function, CLASS_MEMBERS, exception_class, Function, Module, module_with_params, MODULES, NamedType,
    runtime_module, signature, sync_method, Value,
)
from ..types import (
    DATE, DATETIME, FLOAT, INT, ListType, NORMAL_DIST, OptionalType, SetType, STR, TIME, TIMEDELTA, TIMEZONE, Type,
)


def attrs(result: Type, *names: str) -> dict:
    return {name: (lambda t, r=result: r) for name in names}


OPT_INT, OPT_TZ = OptionalType(INT), OptionalType(TIMEZONE)
DATE_FIELDS = (("year", OPT_INT, "std::nullopt"), ("month", OPT_INT, "std::nullopt"), ("day", OPT_INT, "std::nullopt"))
TIME_FIELDS = (("hour", OPT_INT, "std::nullopt"), ("minute", OPT_INT, "std::nullopt"),
               ("second", OPT_INT, "std::nullopt"), ("microsecond", OPT_INT, "std::nullopt"))
DATE.attributes.update(attrs(INT, "year", "month", "day"))
TIME.attributes.update({**attrs(INT, "hour", "minute", "second", "microsecond"), "tzinfo": lambda t: OPT_TZ})
DATETIME.attributes.update({**attrs(INT, "year", "month", "day", "hour", "minute", "second", "microsecond"),
                            "tzinfo": lambda t: OPT_TZ})
TIMEDELTA.attributes.update(attrs(INT, "days", "seconds", "microseconds"))
DATE_METHODS = {
    "isoformat": sync_method(STR),
    "strftime": sync_method(STR, ("format", STR)),
    "ctime": sync_method(STR),
    "weekday": sync_method(INT),
    "isoweekday": sync_method(INT),
    "toordinal": sync_method(INT),
}
DATE.methods.update({**DATE_METHODS, "replace": sync_method(DATE, *DATE_FIELDS)})
TIME.methods.update({
    "isoformat": sync_method(STR, ("timespec", STR, '"auto"s')),
    "strftime": sync_method(STR, ("format", STR)),
    "replace": sync_method(TIME, *TIME_FIELDS),
})
DATETIME.methods.update({
    **DATE_METHODS,
    "isoformat": sync_method(STR, ("sep", STR, '"T"s'), ("timespec", STR, '"auto"s')),
    "date": sync_method(DATE),
    "time": sync_method(TIME),
    "timestamp": sync_method(FLOAT),
    "utcoffset": sync_method(OptionalType(TIMEDELTA)),
    "tzname": sync_method(OptionalType(STR)),
    "astimezone": sync_method(DATETIME, ("tz", OPT_TZ, "std::nullopt")),
    "replace": sync_method(DATETIME, *DATE_FIELDS, *TIME_FIELDS,
                           ("tzinfo", OPT_TZ, "std::optional<std::optional<sd::datetime::timezone>>()")),
})
TIMEDELTA.methods.update({"total_seconds": sync_method(FLOAT)})
TIMEZONE.methods.update({"tzname": sync_method(STR, ("dt", OptionalType(DATETIME), "std::nullopt"))})
_DT = "sd::datetime::"
TIME_PARAMS = (("hour", INT, "0"), ("minute", INT, "0"), ("second", INT, "0"), ("microsecond", INT, "0"),
               ("tzinfo", OPT_TZ, "std::nullopt"))
DATETIME_MODULE = {
    "date": Function("date", signature(DATE, ("year", INT), ("month", INT), ("day", INT)), _DT + "date", as_type=DATE),
    "time": Function("time", signature(TIME, *TIME_PARAMS), _DT + "time", as_type=TIME),
    "datetime": Function("datetime", signature(DATETIME, ("year", INT), ("month", INT), ("day", INT), *TIME_PARAMS),
                         _DT + "datetime", as_type=DATETIME),
    "timedelta": Function("timedelta", signature(TIMEDELTA, *((name, FLOAT, "0.0") for name in (
        "days", "seconds", "microseconds", "milliseconds", "minutes", "hours", "weeks"))), _DT + "timedelta",
        as_type=TIMEDELTA),
    "timezone": Function("timezone", signature(TIMEZONE, ("offset", TIMEDELTA), ("name", OptionalType(STR), "std::nullopt")),
                         _DT + "timezone", as_type=TIMEZONE),
    "MINYEAR": Value("MINYEAR", INT, _DT + "MINYEAR"),
    "MAXYEAR": Value("MAXYEAR", INT, _DT + "MAXYEAR"),
    "UTC": Value("UTC", TIMEZONE, _DT + "timezone::utc()"),
}
for _f in DATETIME_MODULE.values():
    if isinstance(_f, Function):
        _f.params = _f.check.params
DATETIME_MODULE["tzinfo"] = NamedType("tzinfo", TIMEZONE)  # for annotations: any time zone
MODULES["datetime"] = Module("datetime", DATETIME_MODULE, "modules/datetime.hpp")
MODULES["zoneinfo"] = module_with_params(runtime_module(
    "zoneinfo", "modules/zoneinfo.hpp",
    ZoneInfo=(signature(TIMEZONE, ("key", STR)), "sd::zoneinfo::ZoneInfo"),
    available_timezones=(signature(SetType(STR)), "sd::zoneinfo::available_timezones"),
    ZoneInfoNotFoundError=exception_class("ZoneInfoNotFoundError", "sd::zoneinfo::ZoneInfoNotFoundError", "KeyError"),
))
MODULES["zoneinfo"].members["ZoneInfo"].as_type = TIMEZONE


CLASS_MEMBERS.update({
    DATE: {
        "today": class_function("date.today", DATE, _DT + "date::today"),
        "fromisoformat": class_function("date.fromisoformat", DATE, _DT + "date::fromisoformat", ("date_string", STR)),
        "fromordinal": class_function("date.fromordinal", DATE, _DT + "date::fromordinal", ("ordinal", INT)),
        "fromtimestamp": class_function("date.fromtimestamp", DATE, _DT + "date::fromtimestamp", ("timestamp", FLOAT)),
    },
    TIME: {
        "fromisoformat": class_function("time.fromisoformat", TIME, _DT + "time::fromisoformat", ("time_string", STR)),
    },
    DATETIME: {
        "now": class_function("datetime.now", DATETIME, _DT + "datetime::now", ("tz", OPT_TZ, "std::nullopt")),
        "today": class_function("datetime.today", DATETIME, _DT + "datetime::today"),
        "utcnow": class_function("datetime.utcnow", DATETIME, _DT + "datetime::utcnow"),
        "fromtimestamp": class_function("datetime.fromtimestamp", DATETIME, _DT + "datetime::fromtimestamp",
                                        ("timestamp", FLOAT), ("tz", OPT_TZ, "std::nullopt")),
        "utcfromtimestamp": class_function("datetime.utcfromtimestamp", DATETIME, _DT + "datetime::utcfromtimestamp",
                                           ("timestamp", FLOAT)),
        "fromisoformat": class_function("datetime.fromisoformat", DATETIME, _DT + "datetime::fromisoformat",
                                        ("date_string", STR)),
        "strptime": class_function("datetime.strptime", DATETIME, _DT + "datetime::strptime",
                                   ("date_string", STR), ("format", STR)),
        "combine": class_function("datetime.combine", DATETIME, _DT + "datetime::combine",
                                  ("date", DATE), ("time", TIME), ("tzinfo", OPT_TZ, "std::nullopt")),
    },
    TIMEZONE: {"utc": Value("utc", TIMEZONE, _DT + "timezone::utc()")},
})
NORMAL_DIST.methods.update({
    "pdf": sync_method(FLOAT, ("x", FLOAT)),
    "cdf": sync_method(FLOAT, ("x", FLOAT)),
    "inv_cdf": sync_method(FLOAT, ("p", FLOAT)),
    "zscore": sync_method(FLOAT, ("x", FLOAT)),
    "quantiles": sync_method(ListType(FLOAT), ("n", INT, "4")),
    "overlap": sync_method(FLOAT, ("other", NORMAL_DIST)),
    "samples": sync_method(ListType(FLOAT), ("n", INT), ("seed", OptionalType(INT), "std::nullopt")),
})
