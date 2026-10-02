"""`calendar`: how the checker types it, and what codegen calls."""

from __future__ import annotations

from ..builtins import CallContext, EXCEPTIONS, MODULES, module_with_params, runtime_module, signature, sync_method
from ..errors import Loc
from ..types import (
    BOOL, BYTES, CALENDAR, DATE, EnumInfo, EnumMember, GeneratorType, HTML_CALENDAR, INT, ListType, NONE,
    OptionalType, STR, StructType, TEXT_CALENDAR, TupleType, Type, VarTupleType,
)


def calendar_enum(name: str, members: list[str], first: int) -> StructType:
    """calendar.Day / calendar.Month: IntEnums whose members print as calendar.MONDAY."""
    info = EnumInfo("IntEnum", INT)
    for i, m in enumerate(members):
        info.members[m] = EnumMember(m, first + i, Loc(0, 0))
    return StructType(name, "struct", None, builtin=True, cpp_name=f"sd::calendar::{name}", module="calendar",
                      frozen=True, enum=info)


DAY_NAMES = "MONDAY TUESDAY WEDNESDAY THURSDAY FRIDAY SATURDAY SUNDAY".split()
MONTH_NAMES = "JANUARY FEBRUARY MARCH APRIL MAY JUNE JULY AUGUST SEPTEMBER OCTOBER NOVEMBER DECEMBER".split()
CALENDAR_DAY = calendar_enum("Day", DAY_NAMES, 0)
CALENDAR_MONTH = calendar_enum("Month", MONTH_NAMES, 1)
WEEK2 = ListType(TupleType((INT, INT)))  # a week of (day, weekday) pairs, as monthdays2calendar() gives
YEAR_MONTH = (("year", INT), ("month", INT))
CALENDAR_METHODS = {
    "getfirstweekday": sync_method(INT),
    "setfirstweekday": sync_method(NONE, ("firstweekday", INT)),
    "iterweekdays": sync_method(GeneratorType(INT)),
    "itermonthdates": sync_method(GeneratorType(DATE), *YEAR_MONTH),
    "itermonthdays": sync_method(GeneratorType(INT), *YEAR_MONTH),
    "itermonthdays2": sync_method(GeneratorType(TupleType((INT, INT))), *YEAR_MONTH),
    "itermonthdays3": sync_method(GeneratorType(TupleType((INT, INT, INT))), *YEAR_MONTH),
    "itermonthdays4": sync_method(GeneratorType(TupleType((INT, INT, INT, INT))), *YEAR_MONTH),
    "monthdatescalendar": sync_method(ListType(ListType(DATE)), *YEAR_MONTH),
    "monthdays2calendar": sync_method(ListType(WEEK2), *YEAR_MONTH),
    "monthdayscalendar": sync_method(ListType(ListType(INT)), *YEAR_MONTH),
    "yeardatescalendar": sync_method(ListType(ListType(ListType(ListType(DATE)))), ("year", INT), ("width", INT, "3")),
    "yeardays2calendar": sync_method(ListType(ListType(ListType(WEEK2))), ("year", INT), ("width", INT, "3")),
    "yeardayscalendar": sync_method(ListType(ListType(ListType(ListType(INT)))), ("year", INT), ("width", INT, "3")),
}
CALENDAR.methods.update(CALENDAR_METHODS)
TEXT_CALENDAR.methods.update(CALENDAR_METHODS)
TEXT_CALENDAR.methods.update({
    "formatday": sync_method(STR, ("day", INT), ("weekday", INT), ("width", INT)),
    "formatweek": sync_method(STR, ("theweek", WEEK2), ("width", INT)),
    "formatweekday": sync_method(STR, ("day", INT), ("width", INT)),
    "formatweekheader": sync_method(STR, ("width", INT)),
    "formatmonthname": sync_method(STR, ("theyear", INT), ("themonth", INT), ("width", INT), ("withyear", BOOL, "true")),
    "formatmonth": sync_method(STR, ("theyear", INT), ("themonth", INT), ("w", INT, "0"), ("l", INT, "0")),
    "formatyear": sync_method(STR, ("theyear", INT), ("w", INT, "2"), ("l", INT, "1"), ("c", INT, "6"), ("m", INT, "3")),
    "prweek": sync_method(NONE, ("theweek", WEEK2), ("width", INT)),
    "prmonth": sync_method(NONE, ("theyear", INT), ("themonth", INT), ("w", INT, "0"), ("l", INT, "0")),
    "pryear": sync_method(NONE, ("theyear", INT), ("w", INT, "0"), ("l", INT, "0"), ("c", INT, "6"), ("m", INT, "3")),
})
HTML_CALENDAR.methods.update(CALENDAR_METHODS)
HTML_CALENDAR.methods.update({
    "formatday": sync_method(STR, ("day", INT), ("weekday", INT)),
    "formatweek": sync_method(STR, ("theweek", WEEK2)),
    "formatweekday": sync_method(STR, ("day", INT)),
    "formatweekheader": sync_method(STR),
    "formatmonthname": sync_method(STR, ("theyear", INT), ("themonth", INT), ("withyear", BOOL, "true")),
    "formatmonth": sync_method(STR, ("theyear", INT), ("themonth", INT), ("withyear", BOOL, "true")),
    "formatyear": sync_method(STR, ("theyear", INT), ("width", INT, "3")),
    "formatyearpage": sync_method(BYTES, ("theyear", INT), ("width", INT, "3"),
                                  ("css", OptionalType(STR), "std::optional<std::string>(\"calendar.css\")"),
                                  ("encoding", OptionalType(STR), "std::nullopt")),
})
for _cls in (CALENDAR, TEXT_CALENDAR, HTML_CALENDAR):
    _cls.attributes["firstweekday"] = lambda t: INT
CALENDAR_ERRORS = {n: StructType(n, "class", None, base=EXCEPTIONS["ValueError"], builtin=True,
                                 cpp_name=f"sd::calendar::{n}") for n in ("IllegalMonthError", "IllegalWeekdayError")}
FIRSTWEEKDAY = ("firstweekday", INT, "0")
MODULES["calendar"] = module_with_params(runtime_module(
    "calendar", "modules/calendar.hpp",
    isleap=(signature(BOOL, ("year", INT)), "sd::calendar::isleap"),
    leapdays=(signature(INT, ("y1", INT), ("y2", INT)), "sd::calendar::leapdays"),
    weekday=(signature(CALENDAR_DAY, ("year", INT), ("month", INT), ("day", INT)), "sd::calendar::weekday"),
    monthrange=(signature(TupleType((CALENDAR_DAY, INT)), *YEAR_MONTH), "sd::calendar::monthrange"),
    monthcalendar=(signature(ListType(ListType(INT)), *YEAR_MONTH), "sd::calendar::monthcalendar"),
    firstweekday=(signature(INT), "sd::calendar::firstweekday"),
    setfirstweekday=(signature(NONE, ("firstweekday", INT)), "sd::calendar::setfirstweekday"),
    weekheader=(signature(STR, ("width", INT)), "sd::calendar::weekheader"),
    week=(signature(STR, ("theweek", WEEK2), ("width", INT)), "sd::calendar::week"),
    month=(signature(STR, ("theyear", INT), ("themonth", INT), ("w", INT, "0"), ("l", INT, "0")), "sd::calendar::month"),
    prmonth=(signature(NONE, ("theyear", INT), ("themonth", INT), ("w", INT, "0"), ("l", INT, "0")), "sd::calendar::prmonth"),
    calendar=(signature(STR, ("theyear", INT), ("w", INT, "2"), ("l", INT, "1"), ("c", INT, "6"), ("m", INT, "3")),
              "sd::calendar::calendar"),
    prcal=(signature(NONE, ("theyear", INT), ("w", INT, "0"), ("l", INT, "0"), ("c", INT, "6"), ("m", INT, "3")),
           "sd::calendar::prcal"),
    timegm=(lambda ctx: calendar_timegm(ctx), "sd::calendar::timegm"),
    Calendar=(signature(CALENDAR, FIRSTWEEKDAY), "sd::calendar::Calendar"),
    TextCalendar=(signature(TEXT_CALENDAR, FIRSTWEEKDAY), "sd::calendar::TextCalendar"),
    HTMLCalendar=(signature(HTML_CALENDAR, FIRSTWEEKDAY), "sd::calendar::HTMLCalendar"),
    day_name=(ListType(STR), "sd::calendar::day_name()"),
    day_abbr=(ListType(STR), "sd::calendar::day_abbr()"),
    month_name=(ListType(STR), "sd::calendar::month_name()"),
    month_abbr=(ListType(STR), "sd::calendar::month_abbr()"),
    Day=CALENDAR_DAY,
    Month=CALENDAR_MONTH,
    **CALENDAR_ERRORS,
    **{name: (CALENDAR_DAY, f"sd::calendar::Day::sd_at({i})") for i, name in enumerate(DAY_NAMES)},
    **{name: (CALENDAR_MONTH, f"sd::calendar::Month::sd_at({i})") for i, name in enumerate(MONTH_NAMES)},
))
for _name, _cls in (("Calendar", CALENDAR), ("TextCalendar", TEXT_CALENDAR), ("HTMLCalendar", HTML_CALENDAR)):
    MODULES["calendar"].members[_name].as_type = _cls


def calendar_timegm(ctx: CallContext) -> Type:
    """timegm(tuple): Unix time of a UTC (year, month, day, hour, minute, second, ...) tuple."""
    ctx.arity(1)
    t = ctx.arg(0)
    if isinstance(t, VarTupleType) and t.elem == INT:
        return INT
    if not (isinstance(t, TupleType) and len(t.elts) >= 6 and all(x == INT for x in t.elts[:6])):
        raise ctx.error(f"calendar.timegm() takes a (year, month, day, hour, minute, second) tuple of ints, not {t}",
                        ctx.args[0])
    return INT
