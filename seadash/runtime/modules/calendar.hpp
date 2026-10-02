// The `calendar` module: a port of Python's calendar.py. Names are English, as Python gives them
// in the C locale (no LocaleTextCalendar / LocaleHTMLCalendar).
#pragma once

#include <atomic>
#include <string>
#include <tuple>

#include "datetime.hpp"
#include "enum.hpp"
#include "../seadash.hpp"

namespace sd::calendar {

struct IllegalMonthError : ValueError {
    using ValueError::ValueError;
    std::string sd_type() const override { return "calendar.IllegalMonthError"; }
};
struct IllegalWeekdayError : ValueError {
    using ValueError::ValueError;
    std::string sd_type() const override { return "calendar.IllegalWeekdayError"; }
};

// calendar.Day and calendar.Month: IntEnums whose members print as calendar.MONDAY (@global_enum).
template <class Self, int First>
struct Members {
    std::int64_t sd_index = 0;
    static Self sd_at(std::int64_t i) {
        Self x;
        x.sd_index = i;
        return x;
    }
    std::int64_t sd_value() const { return sd_index + First; }
    const std::string& sd_name() const { return Self::sd_table().names[static_cast<std::size_t>(sd_index)]; }
    static Self sd_lookup(std::int64_t v) { return sd_at(sd::enums::lookup(Self::sd_table(), v)); }
    static Self sd_by_name(const std::string& n) { return sd_at(sd::enums::lookup_name(Self::sd_table(), n)); }
    static sd::list<Self> sd_members() {
        sd::list<Self> out;
        for (std::size_t i = 0; i < Self::sd_table().names.size(); ++i) out.push_back(sd_at(static_cast<std::int64_t>(i)));
        return out;
    }
    std::string sd_repr() const { return "calendar." + sd_name(); }
    std::string sd_str() const { return std::to_string(sd_value()); }
    bool sd_truthy() const { return sd_value() != 0; }
    friend std::string format_value(const Self& x, std::string_view spec) { return sd::format_any(x.sd_value(), spec); }
    bool operator==(const Members&) const = default;
    operator std::int64_t() const { return sd_value(); }
    bool operator<(const Members& o) const { return sd_index < o.sd_index; }
};

inline sd::enums::Table<std::int64_t> make_table(const char* cls, std::vector<std::string> names, std::int64_t first) {
    sd::enums::Table<std::int64_t> t{cls, names, {}, {}, {}, 0, false};
    for (std::size_t i = 0; i < names.size(); ++i) {
        t.values.push_back(first + static_cast<std::int64_t>(i));
        t.by_name.emplace_back(names[i], static_cast<std::int64_t>(i));
    }
    return t;
}

struct Day : Members<Day, 0> {
    static const sd::enums::Table<std::int64_t>& sd_table() {
        static const auto t = make_table("Day", {"MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"}, 0);
        return t;
    }
};
struct Month : Members<Month, 1> {
    static const sd::enums::Table<std::int64_t>& sd_table() {
        static const auto t = make_table("Month", {"JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
                                                   "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"}, 1);
        return t;
    }
};

// Full and abbreviated names (month names are 1-based: [0] is "").
inline const char* const DAY_NAMES[] = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"};
inline const char* const MONTH_NAMES[] = {"",     "January", "February", "March",     "April",   "May",      "June",
                                          "July", "August",  "September", "October", "November", "December"};
inline list<std::string> day_name() {
    list<std::string> out;
    for (auto n : DAY_NAMES) out.push_back(n);
    return out;
}
inline list<std::string> day_abbr() {
    list<std::string> out;
    for (auto n : DAY_NAMES) out.push_back(std::string(n, 3));
    return out;
}
inline list<std::string> month_name() {
    list<std::string> out;
    for (auto n : MONTH_NAMES) out.push_back(n);
    return out;
}
inline list<std::string> month_abbr() {
    list<std::string> out;
    for (auto n : MONTH_NAMES) out.push_back(std::string(n).substr(0, 3));
    return out;
}

inline bool isleap(std::int64_t year) { return year % 4 == 0 && (year % 100 != 0 || year % 400 == 0); }
// Leap years in [y1, y2).
inline std::int64_t leapdays(std::int64_t y1, std::int64_t y2) {
    y1 -= 1;
    y2 -= 1;
    return (floordiv(y2, 4_i) - floordiv(y1, 4_i)) - (floordiv(y2, 100_i) - floordiv(y1, 100_i)) +
           (floordiv(y2, 400_i) - floordiv(y1, 400_i));
}
inline Day weekday(std::int64_t year, std::int64_t month, std::int64_t day) {
    if (year < 1 || year > 9999) year = 2000 + mod(year, 400_i);  // the same weekdays, every 400 years
    return Day::sd_at(datetime::date(year, month, day).weekday());
}
inline void validate_month(std::int64_t month) {
    if (month < 1 || month > 12) raise<IllegalMonthError>("bad month number " + std::to_string(month) + "; must be 1-12");
}
inline std::int64_t monthlen(std::int64_t year, std::int64_t month) {
    static const std::int64_t mdays[] = {0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    return mdays[month] + (month == 2 && isleap(year));
}
inline std::tuple<Day, std::int64_t> monthrange(std::int64_t year, std::int64_t month) {
    validate_month(month);
    return {weekday(year, month, 1), monthlen(year, month)};
}
// Unix time of a UTC (year, month, day, hour, minute, second, ...) tuple.
template <class Tuple>
std::int64_t timegm(const Tuple& t) {
    auto at = [&](std::size_t i) -> std::int64_t {
        if constexpr (requires { t.items; }) {
            if (i >= t.items.size()) raise("ValueError", "not enough values to unpack (expected 6, got " + std::to_string(t.items.size()) + ")");
            return t.items[i];
        } else {
            return [&]<std::size_t... I>(std::index_sequence<I...>) {
                std::int64_t out = 0;
                ((I == i ? (out = static_cast<std::int64_t>(std::get<I>(t)), 0) : 0), ...);
                return out;
            }(std::make_index_sequence<std::tuple_size_v<Tuple>>{});
        }
    };
    std::int64_t days = datetime::date(at(0), at(1), 1).toordinal() - datetime::date(1970, 1, 1).toordinal() + at(2) - 1;
    return ((days * 24 + at(3)) * 60 + at(4)) * 60 + at(5);
}

using Week2 = list<std::tuple<std::int64_t, std::int64_t>>;

class Calendar {
    std::shared_ptr<std::atomic<std::int64_t>> first_ = std::make_shared<std::atomic<std::int64_t>>(0);

public:
    explicit Calendar(std::int64_t firstweekday = 0) { first_->store(firstweekday); }
    std::int64_t getfirstweekday() const { return mod(first_->load(), 7_i); }
    void setfirstweekday(std::int64_t f) const { first_->store(f); }
    std::int64_t firstweekday() const { return getfirstweekday(); }

    Generator<std::int64_t> iterweekdays() const {
        std::int64_t f = getfirstweekday();
        for (std::int64_t i = f; i < f + 7; ++i) co_yield i % 7;
    }
    std::vector<std::tuple<std::int64_t, std::int64_t, std::int64_t>> days3(std::int64_t year, std::int64_t month) const {
        auto [day1, ndays] = monthrange(year, month);
        std::int64_t f = getfirstweekday();
        std::int64_t before = mod(day1.sd_value() - f, 7_i), after = mod(f - day1.sd_value() - ndays, 7_i);
        std::vector<std::tuple<std::int64_t, std::int64_t, std::int64_t>> out;
        std::int64_t py = month == 1 ? year - 1 : year, pm = month == 1 ? 12 : month - 1;
        std::int64_t end = monthlen(py, pm) + 1;
        for (std::int64_t d = end - before; d < end; ++d) out.emplace_back(py, pm, d);
        for (std::int64_t d = 1; d <= ndays; ++d) out.emplace_back(year, month, d);
        std::int64_t ny = month == 12 ? year + 1 : year, nm = month == 12 ? 1 : month + 1;
        for (std::int64_t d = 1; d <= after; ++d) out.emplace_back(ny, nm, d);
        return out;
    }
    std::vector<std::int64_t> days(std::int64_t year, std::int64_t month) const {
        auto [day1, ndays] = monthrange(year, month);
        std::int64_t f = getfirstweekday();
        std::vector<std::int64_t> out(static_cast<std::size_t>(mod(day1.sd_value() - f, 7_i)), 0);
        for (std::int64_t d = 1; d <= ndays; ++d) out.push_back(d);
        out.insert(out.end(), static_cast<std::size_t>(mod(f - day1.sd_value() - ndays, 7_i)), 0);
        return out;
    }
    std::vector<std::tuple<std::int64_t, std::int64_t>> days2(std::int64_t year, std::int64_t month) const {
        std::vector<std::tuple<std::int64_t, std::int64_t>> out;
        std::int64_t i = getfirstweekday();
        for (std::int64_t d : days(year, month)) out.emplace_back(d, i++ % 7);
        return out;
    }
    Generator<datetime::date> itermonthdates(std::int64_t year, std::int64_t month) const {
        for (auto [y, m, d] : days3(year, month)) co_yield datetime::date(y, m, d);
    }
    Generator<std::int64_t> itermonthdays(std::int64_t year, std::int64_t month) const {
        for (auto d : days(year, month)) co_yield d;
    }
    Generator<std::tuple<std::int64_t, std::int64_t>> itermonthdays2(std::int64_t year, std::int64_t month) const {
        for (auto d : days2(year, month)) co_yield d;
    }
    Generator<std::tuple<std::int64_t, std::int64_t, std::int64_t>> itermonthdays3(std::int64_t year, std::int64_t month) const {
        for (auto d : days3(year, month)) co_yield d;
    }
    Generator<std::tuple<std::int64_t, std::int64_t, std::int64_t, std::int64_t>> itermonthdays4(std::int64_t year,
                                                                                                   std::int64_t month) const {
        std::int64_t i = 0, f = getfirstweekday();
        for (auto [y, m, d] : days3(year, month)) co_yield std::make_tuple(y, m, d, (f + i++) % 7);
    }

    template <class T>
    static list<list<T>> weeks(const std::vector<T>& items) {
        list<list<T>> out;
        for (std::size_t i = 0; i < items.size(); i += 7) {
            list<T> week;
            for (std::size_t k = i; k < std::min(i + 7, items.size()); ++k) week.push_back(items[k]);
            out.push_back(week);
        }
        return out;
    }
    list<list<datetime::date>> monthdatescalendar(std::int64_t year, std::int64_t month) const {
        std::vector<datetime::date> dates;
        for (auto [y, m, d] : days3(year, month)) dates.emplace_back(y, m, d);
        return weeks(dates);
    }
    list<Week2> monthdays2calendar(std::int64_t year, std::int64_t month) const { return weeks(days2(year, month)); }
    list<list<std::int64_t>> monthdayscalendar(std::int64_t year, std::int64_t month) const { return weeks(days(year, month)); }

    template <class M>
    static list<list<M>> rows(const std::vector<M>& months, std::int64_t width) {
        if (width <= 0) raise("ValueError", "range() arg 3 must not be zero");
        list<list<M>> out;
        for (std::size_t i = 0; i < months.size(); i += static_cast<std::size_t>(width)) {
            list<M> row;
            for (std::size_t k = i; k < std::min(i + static_cast<std::size_t>(width), months.size()); ++k) row.push_back(months[k]);
            out.push_back(row);
        }
        return out;
    }
    list<list<list<list<datetime::date>>>> yeardatescalendar(std::int64_t year, std::int64_t width = 3) const {
        std::vector<list<list<datetime::date>>> months;
        for (std::int64_t m = 1; m <= 12; ++m) months.push_back(monthdatescalendar(year, m));
        return rows(months, width);
    }
    list<list<list<Week2>>> yeardays2calendar(std::int64_t year, std::int64_t width = 3) const {
        std::vector<list<Week2>> months;
        for (std::int64_t m = 1; m <= 12; ++m) months.push_back(monthdays2calendar(year, m));
        return rows(months, width);
    }
    list<list<list<list<std::int64_t>>>> yeardayscalendar(std::int64_t year, std::int64_t width = 3) const {
        std::vector<list<list<std::int64_t>>> months;
        for (std::int64_t m = 1; m <= 12; ++m) months.push_back(monthdayscalendar(year, m));
        return rows(months, width);
    }
};

inline std::string repeat_newline(std::int64_t l) { return std::string(static_cast<std::size_t>(std::max<std::int64_t>(l, 0)), '\n'); }
inline std::string rstrip(const std::string& s) { return str_rstrip(s); }

class TextCalendar : public Calendar {
public:
    using Calendar::Calendar;
    std::string formatday(std::int64_t day, std::int64_t, std::int64_t width) const {
        std::string s;
        if (day != 0) s = day < 10 ? " " + std::to_string(day) : std::to_string(day);  // '%2i' % day
        return str_center(s, width);
    }
    std::string formatweek(const Week2& week, std::int64_t width) const {
        std::string out;
        bool first = true;
        for (const auto& [d, wd] : week) {
            if (!first) out += ' ';
            first = false;
            out += formatday(d, wd, width);
        }
        return out;
    }
    std::string formatweekday(std::int64_t day, std::int64_t width) const {
        std::string name = DAY_NAMES[mod(day, 7_i)];
        if (width < 9) name = name.substr(0, 3);
        return str_center(name.substr(0, static_cast<std::size_t>(std::max<std::int64_t>(width, 0))), width);
    }
    std::string formatweekheader(std::int64_t width) const {
        std::string out;
        std::int64_t f = getfirstweekday();
        for (std::int64_t i = f; i < f + 7; ++i) {
            if (i > f) out += ' ';
            out += formatweekday(i % 7, width);
        }
        return out;
    }
    std::string formatmonthname(std::int64_t year, std::int64_t month, std::int64_t width, bool withyear = true) const {
        validate_month(month);
        std::string s = MONTH_NAMES[month];
        if (withyear) s += " " + std::to_string(year);
        return str_center(s, width);
    }
    void prweek(const Week2& week, std::int64_t width) const { print("", "", formatweek(week, width)); }
    std::string formatmonth(std::int64_t year, std::int64_t month, std::int64_t w = 0, std::int64_t l = 0) const {
        w = std::max<std::int64_t>(2, w);
        l = std::max<std::int64_t>(1, l);
        std::string s = rstrip(formatmonthname(year, month, 7 * (w + 1) - 1)) + repeat_newline(l);
        s += rstrip(formatweekheader(w)) + repeat_newline(l);
        for (const auto& week : monthdays2calendar(year, month)) s += rstrip(formatweek(week, w)) + repeat_newline(l);
        return s;
    }
    void prmonth(std::int64_t year, std::int64_t month, std::int64_t w = 0, std::int64_t l = 0) const {
        print("", "", formatmonth(year, month, w, l));
    }
    static std::string formatstring(const std::vector<std::string>& cols, std::int64_t colwidth, std::int64_t spacing) {
        std::string out;
        for (std::size_t i = 0; i < cols.size(); ++i) {
            if (i > 0) out += std::string(static_cast<std::size_t>(std::max<std::int64_t>(spacing, 0)), ' ');
            out += str_center(cols[i], colwidth);
        }
        return out;
    }
    std::string formatyear(std::int64_t year, std::int64_t w = 2, std::int64_t l = 1, std::int64_t c = 6,
                           std::int64_t m = 3) const {
        w = std::max<std::int64_t>(2, w);
        l = std::max<std::int64_t>(1, l);
        c = std::max<std::int64_t>(2, c);
        std::int64_t colwidth = (w + 1) * 7 - 1;
        std::string v = rstrip(str_center(std::to_string(year), colwidth * m + c * (m - 1))) + repeat_newline(l);
        std::string header = formatweekheader(w);
        auto rows = yeardays2calendar(year, m);
        for (std::size_t i = 0; i < rows.size(); ++i) {
            const auto& row = rows[i];
            std::int64_t lo = m * static_cast<std::int64_t>(i) + 1, hi = std::min<std::int64_t>(m * (static_cast<std::int64_t>(i) + 1) + 1, 13);
            std::vector<std::string> names, headers;
            for (std::int64_t k = lo; k < hi; ++k) {
                names.push_back(formatmonthname(year, k, colwidth, false));
                headers.push_back(header);
            }
            v += repeat_newline(l) + rstrip(formatstring(names, colwidth, c)) + repeat_newline(l);
            v += rstrip(formatstring(headers, colwidth, c)) + repeat_newline(l);
            std::size_t height = 0;
            for (const auto& cal : row) height = std::max(height, cal.size());
            for (std::size_t j = 0; j < height; ++j) {
                std::vector<std::string> weeks;
                for (const auto& cal : row) weeks.push_back(j >= cal.size() ? std::string() : formatweek(cal[j], w));
                v += rstrip(formatstring(weeks, colwidth, c)) + repeat_newline(l);
            }
        }
        return v;
    }
    void pryear(std::int64_t year, std::int64_t w = 0, std::int64_t l = 0, std::int64_t c = 6, std::int64_t m = 3) const {
        print("", "", formatyear(year, w, l, c, m));
    }
};

class HTMLCalendar : public Calendar {
    static constexpr const char* CSS[] = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};

public:
    using Calendar::Calendar;
    std::string formatday(std::int64_t day, std::int64_t weekday) const {
        if (day == 0) return "<td class=\"noday\">&nbsp;</td>";
        return std::string("<td class=\"") + CSS[mod(weekday, 7_i)] + "\">" + std::to_string(day) + "</td>";
    }
    std::string formatweek(const Week2& week) const {
        std::string s;
        for (const auto& [d, wd] : week) s += formatday(d, wd);
        return "<tr>" + s + "</tr>";
    }
    std::string formatweekday(std::int64_t day) const {
        std::int64_t d = mod(day, 7_i);
        return std::string("<th class=\"") + CSS[d] + "\">" + std::string(DAY_NAMES[d], 3) + "</th>";
    }
    std::string formatweekheader() const {
        std::string s;
        std::int64_t f = getfirstweekday();
        for (std::int64_t i = f; i < f + 7; ++i) s += formatweekday(i % 7);
        return "<tr>" + s + "</tr>";
    }
    std::string formatmonthname(std::int64_t year, std::int64_t month, bool withyear = true) const {
        validate_month(month);
        std::string s = MONTH_NAMES[month];
        if (withyear) s += " " + std::to_string(year);
        return "<tr><th colspan=\"7\" class=\"month\">" + s + "</th></tr>";
    }
    std::string formatmonth(std::int64_t year, std::int64_t month, bool withyear = true) const {
        std::string v = "<table border=\"0\" cellpadding=\"0\" cellspacing=\"0\" class=\"month\">\n";
        v += formatmonthname(year, month, withyear) + "\n" + formatweekheader() + "\n";
        for (const auto& week : monthdays2calendar(year, month)) v += formatweek(week) + "\n";
        return v + "</table>\n";
    }
    std::string formatyear(std::int64_t year, std::int64_t width = 3) const {
        width = std::max<std::int64_t>(width, 1);
        std::string v = "<table border=\"0\" cellpadding=\"0\" cellspacing=\"0\" class=\"year\">\n";
        v += "<tr><th colspan=\"" + std::to_string(width) + "\" class=\"year\">" + std::to_string(year) + "</th></tr>";
        for (std::int64_t i = 1; i < 13; i += width) {
            v += "<tr>";
            for (std::int64_t m = i; m < std::min<std::int64_t>(i + width, 13); ++m)
                v += "<td>" + formatmonth(year, m, false) + "</td>";
            v += "</tr>";
        }
        return v + "</table>";
    }
    bytes formatyearpage(std::int64_t year, std::int64_t width = 3, std::optional<std::string> css = "calendar.css",
                         std::optional<std::string> encoding = std::nullopt) const {
        std::string enc = encoding.value_or("utf-8");
        std::string v = "<?xml version=\"1.0\" encoding=\"" + enc + "\"?>\n";
        v += "<!DOCTYPE html PUBLIC \"-//W3C//DTD XHTML 1.0 Strict//EN\" \"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd\">\n";
        v += "<html>\n<head>\n<meta http-equiv=\"Content-Type\" content=\"text/html; charset=" + enc + "\" />\n";
        if (css) v += "<link rel=\"stylesheet\" type=\"text/css\" href=\"" + *css + "\" />\n";
        v += "<title>Calendar for " + std::to_string(year) + "</title>\n</head>\n<body>\n";
        v += formatyear(year, width) + "</body>\n</html>\n";
        return bytes(v);
    }
};

// The module-level functions share one TextCalendar, as Python's do.
inline const TextCalendar& module_calendar() {
    static const TextCalendar c;
    return c;
}
inline std::int64_t firstweekday() { return module_calendar().getfirstweekday(); }
inline void setfirstweekday(std::int64_t f) {
    if (f < 0 || f > 6) raise<IllegalWeekdayError>("bad weekday number " + std::to_string(f) + "; must be 0 (Monday) to 6 (Sunday)");
    module_calendar().setfirstweekday(f);
}
inline list<list<std::int64_t>> monthcalendar(std::int64_t year, std::int64_t month) {
    return module_calendar().monthdayscalendar(year, month);
}
inline std::string weekheader(std::int64_t width) { return module_calendar().formatweekheader(width); }
inline std::string week(const Week2& w, std::int64_t width) { return module_calendar().formatweek(w, width); }
inline std::string month(std::int64_t year, std::int64_t m, std::int64_t w = 0, std::int64_t l = 0) {
    return module_calendar().formatmonth(year, m, w, l);
}
inline void prmonth(std::int64_t year, std::int64_t m, std::int64_t w = 0, std::int64_t l = 0) {
    module_calendar().prmonth(year, m, w, l);
}
inline std::string calendar(std::int64_t year, std::int64_t w = 2, std::int64_t l = 1, std::int64_t c = 6, std::int64_t m = 3) {
    return module_calendar().formatyear(year, w, l, c, m);
}
inline void prcal(std::int64_t year, std::int64_t w = 0, std::int64_t l = 0, std::int64_t c = 6, std::int64_t m = 3) {
    module_calendar().pryear(year, w, l, c, m);
}

}  // namespace sd::calendar
