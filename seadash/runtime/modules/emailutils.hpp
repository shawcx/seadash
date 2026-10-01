// The `email.utils` module's dates: formatdate, format_datetime (RFC 2822 dates, as in
// HTTP's Date and Last-Modified headers) and parsedate_to_datetime / parsedate_tz /
// parsedate, a port of Python's email._parseaddr._parsedate_tz (forgiving, like it).
#pragma once

#include <algorithm>
#include <cctype>
#include <climits>

#include "datetime.hpp"

namespace sd::emailutils {

inline std::string format_datetime(const datetime::datetime& dt, bool usegmt = false) {
    static const char* days[] = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"};
    static const char* months[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    std::string zone;
    auto tz = dt.tzinfo();
    if (usegmt) {
        if (!tz || !(*tz == datetime::timezone::utc())) raise("ValueError", "usegmt option requires a UTC datetime");
        zone = "GMT";
    } else if (!tz) {
        zone = "-0000";
    } else {
        zone = dt.strftime("%z");
    }
    using datetime::pad;
    return std::string(days[dt.weekday()]) + ", " + pad(dt.day(), 2) + " " + months[dt.month() - 1] + " " +
           pad(dt.year(), 4) + " " + pad(dt.hour(), 2) + ":" + pad(dt.minute(), 2) + ":" + pad(dt.second(), 2) + " " + zone;
}

inline std::string formatdate(std::optional<double> timeval = std::nullopt, bool localtime = false, bool usegmt = false) {
    if (!timeval) {
        timespec t;
        ::clock_gettime(CLOCK_REALTIME, &t);
        timeval = static_cast<double>(t.tv_sec) + t.tv_nsec / 1e9;
    }
    auto dt = datetime::datetime::fromtimestamp(*timeval, datetime::timezone::utc());
    if (localtime) {
        dt = dt.astimezone();
        usegmt = false;
    } else if (!usegmt) {
        dt = datetime::datetime(dt.year(), dt.month(), dt.day(), dt.hour(), dt.minute(), dt.second(), dt.microsecond());
    }
    return format_datetime(dt, usegmt);
}

// ---- parsing -----------------------------------------------------------------------

namespace detail {

// An int() of a token: nullopt where Python's int() raises ValueError. Python's ints don't
// overflow, so `huge` marks values beyond 64 bits (Python fails later, converting them).
struct Int {
    __int128 value = 0;
    bool huge = false;
};
inline std::optional<Int> to_int(std::string_view s) {
    Int out;
    bool negative = false;
    if (!s.empty() && (s[0] == '+' || s[0] == '-')) negative = s[0] == '-', s.remove_prefix(1);
    if (s.empty() || s.front() == '_' || s.back() == '_') return std::nullopt;
    char prev = 0;
    for (char c : s) {
        if (c == '_') {
            if (prev == '_') return std::nullopt;
        } else if (c >= '0' && c <= '9') {
            if (!out.huge) out.value = out.value * 10 + (c - '0');
            if (out.value > static_cast<__int128>(INT64_MAX) * 1000000) out.huge = true;
        } else {
            return std::nullopt;
        }
        prev = c;
    }
    if (negative) out.value = -out.value;
    return out;
}

inline std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}
inline std::string upper(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    return s;
}

// The parsed fields: year, month, day, hour, minute, second, and the zone's offset in
// seconds (nullopt for -0000 or no known zone).
struct Parsed {
    Int fields[6];
    std::optional<Int> tz;
};

inline std::optional<Parsed> parse(const std::string& text) {
    static const std::vector<std::string> monthnames = {
        "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
        "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
        "december"};
    static const std::vector<std::string> daynames = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"};
    static const std::vector<std::pair<std::string, int>> timezones = {
        {"UT", 0}, {"UTC", 0}, {"GMT", 0}, {"Z", 0}, {"AST", -400}, {"ADT", -300}, {"EST", -500}, {"EDT", -400},
        {"CST", -600}, {"CDT", -500}, {"MST", -700}, {"MDT", -600}, {"PST", -800}, {"PDT", -700}};
    auto contains = [](const std::vector<std::string>& v, const std::string& s) {
        return std::find(v.begin(), v.end(), s) != v.end();
    };

    std::vector<std::string> data = str_split(text);
    if (data.empty()) return std::nullopt;
    if (data[0].back() == ',' || contains(daynames, lower(data[0]))) {
        data.erase(data.begin());  // a day name
    } else if (auto i = data[0].rfind(','); i != std::string::npos) {
        data[0] = data[0].substr(i + 1);
    }
    if (data.size() == 3) {  // an RFC 850 date: 06-Nov-94
        auto stuff = str_split(data[0], "-");
        if (stuff.size() == 3) {
            stuff.insert(stuff.end(), data.begin() + 1, data.end());
            data = std::move(stuff);
        }
    }
    if (data.size() == 4) {
        const std::string s = data[3];
        auto i = s.find('+');
        if (i == std::string::npos) i = s.find('-');
        if (i != std::string::npos && i > 0) {
            data[3] = s.substr(0, i);
            data.push_back(s.substr(i));
        } else {
            data.push_back("");  // no zone
        }
    }
    if (data.size() < 5) return std::nullopt;
    std::string dd = data[0], mm = data[1], yy = data[2], tm = data[3], tz = data[4];
    if (dd.empty() || mm.empty() || yy.empty()) return std::nullopt;
    mm = lower(mm);
    if (!contains(monthnames, mm)) {
        std::swap(dd, mm);
        mm = lower(mm);
        if (!contains(monthnames, mm)) return std::nullopt;
    }
    std::int64_t month = std::find(monthnames.begin(), monthnames.end(), mm) - monthnames.begin() + 1;
    if (month > 12) month -= 12;
    if (dd.back() == ',') dd.pop_back();
    if (auto i = yy.find(':'); i != std::string::npos && i > 0) std::swap(yy, tm);
    if (yy.back() == ',') {
        yy.pop_back();
        if (yy.empty()) return std::nullopt;
    }
    if (!std::isdigit(static_cast<unsigned char>(yy[0]))) std::swap(yy, tz);
    if (tm.back() == ',') tm.pop_back();
    std::vector<std::string> hms = str_split(tm, ":");
    if (hms.size() == 2) {
        hms.push_back("0");
    } else if (hms.size() == 1 && tm.find('.') != std::string::npos) {  // 01.08.47
        hms = str_split(tm, ".");
        if (hms.size() == 2) hms.push_back("0");
        else if (hms.size() != 3) return std::nullopt;
    } else if (hms.size() != 3) {
        return std::nullopt;
    }
    auto year = to_int(yy), day = to_int(dd), hour = to_int(hms[0]), minute = to_int(hms[1]), second = to_int(hms[2]);
    if (!year || !day || !hour || !minute || !second) return std::nullopt;
    if (!year->huge && year->value < 100) year->value += year->value > 68 ? 1900 : 2000;

    std::optional<Int> offset;
    tz = upper(tz);
    if (auto z = std::find_if(timezones.begin(), timezones.end(), [&](const auto& p) { return p.first == tz; });
        z != timezones.end()) {
        offset = Int{z->second, false};
    } else {
        offset = to_int(tz);
        if (offset && offset->value == 0 && tz.starts_with("-")) offset = std::nullopt;
    }
    if (offset && offset->value != 0 && !offset->huge) {  // -0500 -> -18000 seconds
        __int128 v = offset->value < 0 ? -offset->value : offset->value;
        v = v / 100 * 3600 + v % 100 * 60;
        offset->value = offset->value < 0 ? -v : v;
    }
    return Parsed{{*year, Int{month, false}, *day, *hour, *minute, *second}, offset};
}

inline std::string int_text(__int128 v) {
    if (v < 0) return "-" + int_text(-v);
    std::string out;
    do out.insert(out.begin(), static_cast<char>('0' + static_cast<int>(v % 10))), v /= 10;
    while (v);
    return out;
}

// A Python int as a C long, as Python's conversions check it.
inline std::int64_t as_long(const Int& v) {
    if (v.huge || v.value > INT64_MAX || v.value < INT64_MIN) raise("OverflowError", "Python int too large to convert to C long");
    return static_cast<std::int64_t>(v.value);
}

// timezone(timedelta(seconds=offset)), with Python's errors for offsets out of range.
inline datetime::timezone zone(const Int& offset) {
    if (offset.huge) raise("OverflowError", "Python int too large to convert to C int");
    __int128 days = offset.value / 86400, secs = offset.value % 86400;
    if (secs < 0) secs += 86400, days -= 1;
    if (days > INT_MAX || days < INT_MIN) raise("OverflowError", "Python int too large to convert to C int");
    if (days > 999999999 || days < -999999999)
        raise("OverflowError", "days=" + int_text(days) + "; must have magnitude <= 999999999");
    if (offset.value >= 86400 || offset.value <= -86400) {  // (timezone() would say this too, but the microseconds overflow)
        std::string parts = "days=" + int_text(days) + (secs ? ", seconds=" + int_text(secs) : "");
        raise("ValueError", "offset must be a timedelta strictly between -timedelta(hours=24) and timedelta(hours=24), not "
                            "datetime.timedelta(" + parts + ").");
    }
    if (offset.value == 0) return datetime::timezone::utc();  // (Python's timezone(timedelta(0)) is timezone.utc)
    return datetime::timezone(datetime::timedelta(0, static_cast<double>(offset.value)));
}

}  // namespace detail

using DateTuple = std::tuple<std::int64_t, std::int64_t, std::int64_t, std::int64_t, std::int64_t, std::int64_t,
                             std::int64_t, std::int64_t, std::int64_t>;
using DateTupleTz = std::tuple<std::int64_t, std::int64_t, std::int64_t, std::int64_t, std::int64_t, std::int64_t,
                               std::int64_t, std::int64_t, std::int64_t, std::int64_t>;

inline datetime::datetime parsedate_to_datetime(const std::string& data) {
    auto p = detail::parse(data);
    if (!p) raise("ValueError", "Invalid date value or format \"" + data + "\"");
    std::optional<datetime::timezone> tz;
    if (p->tz) tz = detail::zone(*p->tz);  // (Python makes the tzinfo argument first)
    std::int64_t f[6];
    for (int i = 0; i < 6; i++) {  // datetime()'s arguments are C ints
        f[i] = detail::as_long(p->fields[i]);
        if (f[i] > INT_MAX) raise("OverflowError", "signed integer is greater than maximum");
        if (f[i] < INT_MIN) raise("OverflowError", "signed integer is less than minimum");
    }
    return datetime::datetime(f[0], f[1], f[2], f[3], f[4], f[5], 0, tz);
}

inline std::optional<DateTupleTz> parsedate_tz(const std::string& data) {
    auto p = detail::parse(data);
    if (!p) return std::nullopt;
    auto f = [&](int i) { return detail::as_long(p->fields[i]); };
    return DateTupleTz{f(0), f(1), f(2), f(3), f(4), f(5), 0, 1, -1, p->tz ? detail::as_long(*p->tz) : 0};
}

inline std::optional<DateTuple> parsedate(const std::string& data) {
    auto t = parsedate_tz(data);
    if (!t) return std::nullopt;
    auto& v = *t;
    return DateTuple{std::get<0>(v), std::get<1>(v), std::get<2>(v), std::get<3>(v), std::get<4>(v),
                     std::get<5>(v), std::get<6>(v), std::get<7>(v), std::get<8>(v)};
}

}  // namespace sd::emailutils
