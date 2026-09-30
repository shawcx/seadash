// The `datetime` module: date, time, datetime, timedelta and fixed-offset timezones,
// with Python's arithmetic, formatting (str, repr, isoformat, strftime) and parsing
// (fromisoformat, strptime). All are small immutable values.
#pragma once

#include <sys/time.h>

#include <chrono>
#include <cmath>
#include <ctime>

namespace sd::datetime {

inline constexpr std::int64_t MINYEAR = 1, MAXYEAR = 9999;
inline constexpr std::int64_t US_PER_SEC = 1000000, SEC_PER_DAY = 86400;

[[noreturn]] inline void value_error(const std::string& msg) { raise("ValueError", msg); }

inline bool is_leap(std::int64_t y) { return y % 4 == 0 && (y % 100 != 0 || y % 400 == 0); }
inline std::int64_t days_in_month(std::int64_t y, std::int64_t m) {
    static const int days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    return m == 2 && is_leap(y) ? 29 : days[m - 1];
}
// Days since 1970-01-01 (Howard Hinnant's algorithm), and back.
inline std::int64_t days_from_civil(std::int64_t y, std::int64_t m, std::int64_t d) {
    y -= m <= 2;
    std::int64_t era = (y >= 0 ? y : y - 399) / 400;
    std::int64_t yoe = y - era * 400;
    std::int64_t doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    std::int64_t doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}
inline std::tuple<std::int64_t, std::int64_t, std::int64_t> civil_from_days(std::int64_t z) {
    z += 719468;
    std::int64_t era = (z >= 0 ? z : z - 146096) / 146097;
    std::int64_t doe = z - era * 146097;
    std::int64_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    std::int64_t y = yoe + era * 400;
    std::int64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    std::int64_t mp = (5 * doy + 2) / 153;
    std::int64_t d = doy - (153 * mp + 2) / 5 + 1;
    std::int64_t m = mp + (mp < 10 ? 3 : -9);
    return {y + (m <= 2), m, d};
}
inline constexpr std::int64_t EPOCH_ORDINAL = 719163;  // date(1970, 1, 1).toordinal()

inline std::string pad(std::int64_t v, int width) {
    std::string s = std::to_string(v < 0 ? -v : v);
    if (static_cast<int>(s.size()) < width) s.insert(0, static_cast<std::size_t>(width) - s.size(), '0');
    return (v < 0 ? "-" : "") + s;
}
inline std::int64_t floordiv(std::int64_t a, std::int64_t b) {
    std::int64_t q = a / b;
    return (a % b != 0 && ((a < 0) != (b < 0))) ? q - 1 : q;
}

// ---- timedelta ---------------------------------------------------------------------

class timedelta {
    std::int64_t days_ = 0, seconds_ = 0, us_ = 0;  // normalized: 0 <= seconds < 86400, 0 <= us < 1e6

public:
    static timedelta from_us(std::int64_t total) {
        timedelta t;
        t.days_ = floordiv(total, SEC_PER_DAY * US_PER_SEC);
        std::int64_t rest = total - t.days_ * SEC_PER_DAY * US_PER_SEC;
        t.seconds_ = rest / US_PER_SEC;
        t.us_ = rest % US_PER_SEC;
        if (std::abs(t.days_) > 999999999) raise("OverflowError", "days=" + std::to_string(t.days_) + "; must have magnitude <= 999999999");
        return t;
    }
    timedelta() = default;
    // timedelta(days, seconds, microseconds, milliseconds, minutes, hours, weeks), all may be fractional
    timedelta(double days, double seconds = 0, double microseconds = 0, double milliseconds = 0, double minutes = 0,
              double hours = 0, double weeks = 0) {
        long double total = (static_cast<long double>(weeks) * 7 + days) * SEC_PER_DAY * US_PER_SEC +
                            (static_cast<long double>(hours) * 3600 + static_cast<long double>(minutes) * 60 + seconds) * US_PER_SEC +
                            static_cast<long double>(milliseconds) * 1000 + microseconds;
        *this = from_us(static_cast<std::int64_t>(std::nearbyintl(total)));  // round half to even, like Python
    }
    std::int64_t total_us() const { return (days_ * SEC_PER_DAY + seconds_) * US_PER_SEC + us_; }
    std::int64_t days() const { return days_; }
    std::int64_t seconds() const { return seconds_; }
    std::int64_t microseconds() const { return us_; }
    double total_seconds() const { return static_cast<double>(total_us()) / US_PER_SEC; }

    friend timedelta operator+(const timedelta& a, const timedelta& b) { return from_us(a.total_us() + b.total_us()); }
    friend timedelta operator-(const timedelta& a, const timedelta& b) { return from_us(a.total_us() - b.total_us()); }
    timedelta operator-() const { return from_us(-total_us()); }
    timedelta operator+() const { return *this; }
    friend timedelta operator*(const timedelta& a, std::int64_t n) { return from_us(a.total_us() * n); }
    friend timedelta operator*(std::int64_t n, const timedelta& a) { return a * n; }
    friend timedelta operator*(const timedelta& a, double f) {
        return from_us(static_cast<std::int64_t>(std::nearbyintl(static_cast<long double>(a.total_us()) * f)));
    }
    friend timedelta operator*(double f, const timedelta& a) { return a * f; }
    friend double operator/(const timedelta& a, const timedelta& b) {
        if (b.total_us() == 0) raise("ZeroDivisionError", "division by zero");
        return static_cast<double>(a.total_us()) / static_cast<double>(b.total_us());
    }
    friend timedelta operator/(const timedelta& a, std::int64_t n) {
        if (n == 0) raise("ZeroDivisionError", "division by zero");
        return from_us(static_cast<std::int64_t>(std::nearbyintl(static_cast<long double>(a.total_us()) / n)));
    }
    friend timedelta operator/(const timedelta& a, double f) {
        if (f == 0) raise("ZeroDivisionError", "division by zero");
        return from_us(static_cast<std::int64_t>(std::nearbyintl(static_cast<long double>(a.total_us()) / f)));
    }
    auto operator<=>(const timedelta& o) const { return total_us() <=> o.total_us(); }
    bool operator==(const timedelta& o) const { return total_us() == o.total_us(); }
    bool sd_truthy() const { return total_us() != 0; }

    std::string sd_str() const {
        std::string out;
        if (days_) out = std::to_string(days_) + (std::abs(days_) == 1 ? " day, " : " days, ");
        out += std::to_string(seconds_ / 3600) + ":" + pad(seconds_ / 60 % 60, 2) + ":" + pad(seconds_ % 60, 2);
        if (us_) out += "." + pad(us_, 6);
        return out;
    }
    std::string sd_repr() const {
        std::string parts;
        auto add = [&](const char* name, std::int64_t v) {
            if (v) parts += std::string(parts.empty() ? "" : ", ") + name + "=" + std::to_string(v);
        };
        add("days", days_), add("seconds", seconds_), add("microseconds", us_);
        return "datetime.timedelta(" + (parts.empty() ? "0" : parts) + ")";
    }
};

inline std::int64_t floordiv(const timedelta& a, const timedelta& b) {
    if (b.total_us() == 0) raise("ZeroDivisionError", "integer division or modulo by zero");
    return floordiv(a.total_us(), b.total_us());
}
inline timedelta floordiv(const timedelta& a, std::int64_t n) {
    if (n == 0) raise("ZeroDivisionError", "integer division or modulo by zero");
    return timedelta::from_us(floordiv(a.total_us(), n));
}
inline timedelta mod(const timedelta& a, const timedelta& b) {
    if (b.total_us() == 0) raise("ZeroDivisionError", "integer division or modulo by zero");
    return timedelta::from_us(a.total_us() - floordiv(a, b) * b.total_us());
}
inline timedelta abs(const timedelta& t) { return t.total_us() < 0 ? -t : t; }

// ---- timezone: a fixed offset from UTC, or a named zone (zoneinfo.ZoneInfo) --------------
//
// A named zone's offset depends on the moment: offsets are asked for at a wall-clock time
// (the fields of a datetime) or at a UTC instant. For a wall time that happens twice or
// not at all (when clocks change), the offset in effect before the change is used, like
// Python's fold=0.

inline std::string offset_text(std::int64_t us, bool colon = true) {  // "+05:30" / "+0530"
    std::string sign = us < 0 ? "-" : "+";
    us = std::abs(us);
    std::int64_t secs = us / US_PER_SEC;
    std::string out = sign + pad(secs / 3600, 2) + (colon ? ":" : "") + pad(secs / 60 % 60, 2);
    if (secs % 60 || us % US_PER_SEC) out += (colon ? ":" : "") + pad(secs % 60, 2);
    if (us % US_PER_SEC) out += "." + pad(us % US_PER_SEC, 6);
    return out;
}

class timezone {
    std::int64_t offset_us_ = 0;
    std::optional<std::string> name_;
    bool utc_ = false;
    const std::chrono::time_zone* zone_ = nullptr;  // a named zone (its key is name_)

    std::chrono::sys_info info_utc(std::int64_t utc_us) const {
        return zone_->get_info(std::chrono::sys_seconds(std::chrono::seconds(floordiv(utc_us, US_PER_SEC))));
    }
    std::chrono::sys_info info_wall(std::int64_t wall_us) const {
        auto local = std::chrono::local_seconds(std::chrono::seconds(floordiv(wall_us, US_PER_SEC)));
        return zone_->get_info(local).first;  // unique, or (ambiguous / skipped) the one before the change
    }

public:
    timezone() : utc_(true) {}
    explicit timezone(const timedelta& offset, std::optional<std::string> name = std::nullopt)
        : offset_us_(offset.total_us()), name_(std::move(name)) {
        if (std::abs(offset_us_) >= SEC_PER_DAY * US_PER_SEC)
            value_error("offset must be a timedelta strictly between -timedelta(hours=24) and timedelta(hours=24), not " + offset.sd_repr() + ".");
    }
    static timezone utc() { return timezone(); }
    static timezone named(const std::chrono::time_zone* zone, std::string key) {
        timezone tz;
        tz.utc_ = false;
        tz.zone_ = zone;
        tz.name_ = std::move(key);
        return tz;
    }
    bool is_named() const { return zone_ != nullptr; }
    const std::string& key() const { return *name_; }

    std::int64_t offset_at_wall(std::int64_t wall_us) const {
        if (!zone_) return offset_us_;
        return std::chrono::duration_cast<std::chrono::seconds>(info_wall(wall_us).offset).count() * US_PER_SEC;
    }
    std::int64_t offset_at_utc(std::int64_t utc_us) const {
        if (!zone_) return offset_us_;
        return std::chrono::duration_cast<std::chrono::seconds>(info_utc(utc_us).offset).count() * US_PER_SEC;
    }
    std::string name_at_wall(std::int64_t wall_us) const {
        if (zone_) return info_wall(wall_us).abbrev;
        return tzname();
    }

    // As a fixed offset (a named zone at 1970-01-01, which is only used by time objects).
    timedelta offset() const { return timedelta::from_us(offset_at_wall(0)); }
    std::string tzname() const {
        if (zone_) return name_at_wall(0);
        if (name_) return *name_;
        if (utc_ || offset_us_ == 0) return "UTC";
        return "UTC" + offset_text(offset_us_);
    }
    std::string sd_str() const { return zone_ ? *name_ : tzname(); }
    std::string sd_repr() const {
        if (zone_) return "zoneinfo.ZoneInfo(key=" + repr_str(*name_) + ")";
        if (utc_) return "datetime.timezone.utc";
        return "datetime.timezone(" + offset().sd_repr() + (name_ ? ", " + repr_str(*name_) : "") + ")";
    }
    bool operator==(const timezone& o) const {
        if (zone_ || o.zone_) return zone_ == o.zone_;
        return offset_us_ == o.offset_us_;
    }
};

// ---- date ----------------------------------------------------------------------------

inline void check_date(std::int64_t y, std::int64_t m, std::int64_t d) {
    if (y < MINYEAR || y > MAXYEAR) value_error("year " + std::to_string(y) + " is out of range");
    if (m < 1 || m > 12) value_error("month must be in 1..12");
    if (d < 1 || d > days_in_month(y, m)) value_error("day is out of range for month");
}

std::string format_time(const std::string& fmt, std::int64_t y, std::int64_t mo, std::int64_t d, std::int64_t h,
                        std::int64_t mi, std::int64_t s, std::int64_t us,
                        const std::optional<std::pair<std::int64_t, std::string>>& tz);

class date {
protected:
    std::int64_t y_ = 1, m_ = 1, d_ = 1;

public:
    date() = default;
    date(std::int64_t year, std::int64_t month, std::int64_t day) : y_(year), m_(month), d_(day) {
        check_date(year, month, day);
    }
    static date fromordinal(std::int64_t n) {
        if (n < 1) value_error("ordinal must be >= 1");
        auto [y, m, d] = civil_from_days(n - EPOCH_ORDINAL);
        return date(y, m, d);
    }
    static date today();
    static date fromtimestamp(double ts);
    static date fromisoformat(const std::string& s);

    std::int64_t year() const { return y_; }
    std::int64_t month() const { return m_; }
    std::int64_t day() const { return d_; }
    std::int64_t toordinal() const { return days_from_civil(y_, m_, d_) + EPOCH_ORDINAL; }
    std::int64_t weekday() const { return (toordinal() + 6) % 7; }
    std::int64_t isoweekday() const { return weekday() + 1; }
    date replace(std::optional<std::int64_t> year, std::optional<std::int64_t> month, std::optional<std::int64_t> day) const {
        return date(year.value_or(y_), month.value_or(m_), day.value_or(d_));
    }
    std::string isoformat() const { return pad(y_, 4) + "-" + pad(m_, 2) + "-" + pad(d_, 2); }
    std::string strftime(const std::string& fmt) const { return format_time(fmt, y_, m_, d_, 0, 0, 0, 0, std::nullopt); }
    std::string ctime() const { return strftime("%a %b %e %H:%M:%S %Y"); }
    std::string sd_str() const { return isoformat(); }
    std::string sd_repr() const {
        return "datetime.date(" + std::to_string(y_) + ", " + std::to_string(m_) + ", " + std::to_string(d_) + ")";
    }
    auto operator<=>(const date& o) const { return toordinal() <=> o.toordinal(); }
    bool operator==(const date& o) const { return toordinal() == o.toordinal(); }

    friend date operator+(const date& a, const timedelta& t) {
        std::int64_t n = a.toordinal() + t.days();
        if (n < 1 || n > date(MAXYEAR, 12, 31).toordinal()) raise("OverflowError", "date value out of range");
        return fromordinal(n);
    }
    friend date operator+(const timedelta& t, const date& a) { return a + t; }
    friend date operator-(const date& a, const timedelta& t) { return a + (-t); }
    friend timedelta operator-(const date& a, const date& b) {
        return timedelta(static_cast<double>(a.toordinal() - b.toordinal()));
    }
};

// ---- time ----------------------------------------------------------------------------

inline void check_time(std::int64_t h, std::int64_t mi, std::int64_t s, std::int64_t us) {
    if (h < 0 || h > 23) value_error("hour must be in 0..23");
    if (mi < 0 || mi > 59) value_error("minute must be in 0..59");
    if (s < 0 || s > 59) value_error("second must be in 0..59");
    if (us < 0 || us > 999999) value_error("microsecond must be in 0..999999");
}

inline std::string time_iso(std::int64_t h, std::int64_t mi, std::int64_t s, std::int64_t us, const std::string& timespec) {
    std::string spec = timespec;
    if (spec == "auto") spec = us ? "microseconds" : "seconds";
    if (spec == "hours") return pad(h, 2);
    if (spec == "minutes") return pad(h, 2) + ":" + pad(mi, 2);
    std::string out = pad(h, 2) + ":" + pad(mi, 2) + ":" + pad(s, 2);
    if (spec == "seconds") return out;
    if (spec == "milliseconds") return out + "." + pad(us / 1000, 3);
    if (spec == "microseconds") return out + "." + pad(us, 6);
    value_error("Unknown timespec value");
}

class time {
    std::int64_t h_ = 0, mi_ = 0, s_ = 0, us_ = 0;
    std::optional<timezone> tz_;

public:
    time() = default;
    time(std::int64_t hour, std::int64_t minute = 0, std::int64_t second = 0, std::int64_t microsecond = 0,
         std::optional<timezone> tzinfo = std::nullopt)
        : h_(hour), mi_(minute), s_(second), us_(microsecond), tz_(std::move(tzinfo)) {
        check_time(hour, minute, second, microsecond);
    }
    static time fromisoformat(const std::string& s);
    std::int64_t hour() const { return h_; }
    std::int64_t minute() const { return mi_; }
    std::int64_t second() const { return s_; }
    std::int64_t microsecond() const { return us_; }
    std::optional<timezone> tzinfo() const { return tz_; }
    time replace(std::optional<std::int64_t> hour, std::optional<std::int64_t> minute, std::optional<std::int64_t> second,
                 std::optional<std::int64_t> microsecond) const {
        return time(hour.value_or(h_), minute.value_or(mi_), second.value_or(s_), microsecond.value_or(us_), tz_);
    }
    std::string isoformat(const std::string& timespec = "auto") const {
        return time_iso(h_, mi_, s_, us_, timespec) + (tz_ ? offset_text(tz_->offset_at_wall(0)) : "");
    }
    std::string strftime(const std::string& fmt) const {
        return format_time(fmt, 1900, 1, 1, h_, mi_, s_, us_, tz_ ? std::optional(std::pair(tz_->offset_at_wall(0), tz_->tzname())) : std::nullopt);
    }
    std::string sd_str() const { return isoformat(); }
    std::string sd_repr() const {
        std::string out = "datetime.time(" + std::to_string(h_) + ", " + std::to_string(mi_);
        if (s_ || us_) out += ", " + std::to_string(s_);
        if (us_) out += ", " + std::to_string(us_);
        if (tz_) out += ", tzinfo=" + tz_->sd_repr();
        return out + ")";
    }
    std::int64_t key() const { return ((h_ * 60 + mi_) * 60 + s_) * US_PER_SEC + us_ - (tz_ ? tz_->offset_at_wall(0) : 0); }
    auto operator<=>(const time& o) const { return key() <=> o.key(); }
    bool operator==(const time& o) const { return key() == o.key(); }
};

// ---- datetime ------------------------------------------------------------------------

class datetime : public date {
    std::int64_t h_ = 0, mi_ = 0, s_ = 0, us_ = 0;
    std::optional<timezone> tz_;

    // Microseconds since the epoch, reading the fields as if they were UTC.
    std::int64_t wall_us() const {
        return ((days_from_civil(y_, m_, d_) * SEC_PER_DAY + h_ * 3600 + mi_ * 60 + s_) * US_PER_SEC) + us_;
    }
    static datetime from_wall_us(std::int64_t us, std::optional<timezone> tz) {
        std::int64_t days = floordiv(us, SEC_PER_DAY * US_PER_SEC);
        std::int64_t rest = us - days * SEC_PER_DAY * US_PER_SEC;
        auto [y, m, d] = civil_from_days(days);
        if (y < MINYEAR || y > MAXYEAR) raise("OverflowError", "date value out of range");
        std::int64_t secs = rest / US_PER_SEC;
        return datetime(y, m, d, secs / 3600, secs / 60 % 60, secs % 60, rest % US_PER_SEC, std::move(tz));
    }
    static std::int64_t local_offset_us(std::time_t t, std::string* name = nullptr) {
        std::tm tm{};
        ::localtime_r(&t, &tm);
        if (name) *name = tm.tm_zone ? tm.tm_zone : "";
        return static_cast<std::int64_t>(tm.tm_gmtoff) * US_PER_SEC;
    }
    // The real instant, for aware datetimes (naive ones are taken as local time).
    std::int64_t utc_us() const {
        if (tz_) return wall_us() - tz_->offset_at_wall(wall_us());
        std::tm tm{};
        tm.tm_year = static_cast<int>(y_ - 1900), tm.tm_mon = static_cast<int>(m_ - 1), tm.tm_mday = static_cast<int>(d_);
        tm.tm_hour = static_cast<int>(h_), tm.tm_min = static_cast<int>(mi_), tm.tm_sec = static_cast<int>(s_);
        tm.tm_isdst = -1;
        return static_cast<std::int64_t>(std::mktime(&tm)) * US_PER_SEC + us_;
    }

public:
    datetime() = default;
    datetime(std::int64_t year, std::int64_t month, std::int64_t day, std::int64_t hour = 0, std::int64_t minute = 0,
             std::int64_t second = 0, std::int64_t microsecond = 0, std::optional<timezone> tzinfo = std::nullopt)
        : date(year, month, day), h_(hour), mi_(minute), s_(second), us_(microsecond), tz_(std::move(tzinfo)) {
        check_time(hour, minute, second, microsecond);
    }
    static datetime fromtimestamp(double ts, std::optional<timezone> tz = std::nullopt) {
        auto us = static_cast<std::int64_t>(std::nearbyint(ts * US_PER_SEC));
        if (tz) return from_wall_us(us + tz->offset_at_utc(us), tz);
        return from_wall_us(us + local_offset_us(static_cast<std::time_t>(floordiv(us, US_PER_SEC))), std::nullopt);
    }
    static datetime utcfromtimestamp(double ts) {
        return from_wall_us(static_cast<std::int64_t>(std::nearbyint(ts * US_PER_SEC)), std::nullopt);
    }
    static datetime now(std::optional<timezone> tz = std::nullopt) {
        timespec t;
        ::clock_gettime(CLOCK_REALTIME, &t);
        return fromtimestamp(static_cast<double>(t.tv_sec) + t.tv_nsec / 1e9, std::move(tz));
    }
    static datetime today() { return now(); }
    static datetime utcnow() {
        timespec t;
        ::clock_gettime(CLOCK_REALTIME, &t);
        return utcfromtimestamp(static_cast<double>(t.tv_sec) + t.tv_nsec / 1e9);
    }
    static datetime combine(const date& d, const time& t, std::optional<timezone> tz = std::nullopt) {
        return datetime(d.year(), d.month(), d.day(), t.hour(), t.minute(), t.second(), t.microsecond(),
                        tz ? tz : t.tzinfo());
    }
    static datetime fromisoformat(const std::string& s);
    static datetime strptime(const std::string& s, const std::string& fmt);

    std::int64_t hour() const { return h_; }
    std::int64_t minute() const { return mi_; }
    std::int64_t second() const { return s_; }
    std::int64_t microsecond() const { return us_; }
    std::optional<timezone> tzinfo() const { return tz_; }
    date to_date() const { return date(y_, m_, d_); }
    time to_time() const { return time(h_, mi_, s_, us_); }
    double timestamp() const { return static_cast<double>(utc_us()) / US_PER_SEC; }
    std::optional<timedelta> utcoffset() const {
        return tz_ ? std::optional(timedelta::from_us(tz_->offset_at_wall(wall_us()))) : std::nullopt;
    }
    std::optional<std::string> tzname() const { return tz_ ? std::optional(tz_->name_at_wall(wall_us())) : std::nullopt; }
    datetime astimezone(std::optional<timezone> tz = std::nullopt) const {
        std::int64_t utc = utc_us();
        if (!tz) {  // the local time zone, as a fixed offset with its abbreviation
            std::string name;
            std::int64_t off = local_offset_us(static_cast<std::time_t>(floordiv(utc, US_PER_SEC)), &name);
            tz = timezone(timedelta::from_us(off), name);
        }
        return from_wall_us(utc + tz->offset_at_utc(utc), tz);
    }
    datetime replace(std::optional<std::int64_t> year, std::optional<std::int64_t> month, std::optional<std::int64_t> day,
                     std::optional<std::int64_t> hour, std::optional<std::int64_t> minute, std::optional<std::int64_t> second,
                     std::optional<std::int64_t> microsecond, std::optional<std::optional<timezone>> tzinfo) const {
        return datetime(year.value_or(y_), month.value_or(m_), day.value_or(d_), hour.value_or(h_), minute.value_or(mi_),
                        second.value_or(s_), microsecond.value_or(us_), tzinfo ? *tzinfo : tz_);
    }
    std::string isoformat(const std::string& sep = "T", const std::string& timespec = "auto") const {
        return date::isoformat() + sep + time_iso(h_, mi_, s_, us_, timespec) +
               (tz_ ? offset_text(tz_->offset_at_wall(wall_us())) : "");
    }
    std::string strftime(const std::string& fmt) const {
        auto zone = tz_ ? std::optional(std::pair(tz_->offset_at_wall(wall_us()), tz_->name_at_wall(wall_us()))) : std::nullopt;
        return format_time(fmt, y_, m_, d_, h_, mi_, s_, us_, zone);
    }
    std::string ctime() const { return strftime("%a %b %e %H:%M:%S %Y"); }
    std::string sd_str() const { return isoformat(" "); }
    std::string sd_repr() const {
        std::string out = "datetime.datetime(" + std::to_string(y_) + ", " + std::to_string(m_) + ", " + std::to_string(d_) +
                          ", " + std::to_string(h_) + ", " + std::to_string(mi_);
        if (s_ || us_) out += ", " + std::to_string(s_);
        if (us_) out += ", " + std::to_string(us_);
        if (tz_) out += ", tzinfo=" + tz_->sd_repr();
        return out + ")";
    }

    // Naive and aware datetimes can't be ordered (they're never equal), like Python.
    std::partial_ordering operator<=>(const datetime& o) const {
        if (tz_.has_value() != o.tz_.has_value()) raise("TypeError", "can't compare offset-naive and offset-aware datetimes");
        return tz_ && !(*tz_ == *o.tz_) ? utc_us() <=> o.utc_us() : wall_us() <=> o.wall_us();
    }
    bool operator==(const datetime& o) const {
        if (tz_.has_value() != o.tz_.has_value()) return false;
        return tz_ ? utc_us() == o.utc_us() : wall_us() == o.wall_us();
    }
    std::int64_t hash_key() const { return tz_ ? utc_us() : wall_us(); }

    friend datetime operator+(const datetime& a, const timedelta& t) { return from_wall_us(a.wall_us() + t.total_us(), a.tz_); }
    friend datetime operator+(const timedelta& t, const datetime& a) { return a + t; }
    friend datetime operator-(const datetime& a, const timedelta& t) { return a + (-t); }
    friend timedelta operator-(const datetime& a, const datetime& b) {
        if (a.tz_.has_value() != b.tz_.has_value()) raise("TypeError", "can't subtract offset-naive and offset-aware datetimes");
        bool same_zone = !a.tz_ || *a.tz_ == *b.tz_;  // (Python: the same tzinfo means wall-clock arithmetic)
        return timedelta::from_us(same_zone ? a.wall_us() - b.wall_us() : a.utc_us() - b.utc_us());
    }
};

inline date date::today() { return datetime::now().to_date(); }
inline date date::fromtimestamp(double ts) { return datetime::fromtimestamp(ts).to_date(); }

// ---- strftime ------------------------------------------------------------------------

// The C library formats most directives; %f, %z and %Z depend on Python's own values.
inline std::string format_time(const std::string& fmt, std::int64_t y, std::int64_t mo, std::int64_t d, std::int64_t h,
                               std::int64_t mi, std::int64_t s, std::int64_t us,
                               const std::optional<std::pair<std::int64_t, std::string>>& tz) {
    std::tm tm{};
    tm.tm_year = static_cast<int>(y - 1900), tm.tm_mon = static_cast<int>(mo - 1), tm.tm_mday = static_cast<int>(d);
    tm.tm_hour = static_cast<int>(h), tm.tm_min = static_cast<int>(mi), tm.tm_sec = static_cast<int>(s);
    std::int64_t days = days_from_civil(y, mo, d);
    tm.tm_wday = static_cast<int>(((days % 7) + 11) % 7);  // 1970-01-01 was a Thursday
    tm.tm_yday = static_cast<int>(days - days_from_civil(y, 1, 1));
    tm.tm_isdst = -1;
    std::string expanded;
    for (std::size_t i = 0; i < fmt.size(); ++i) {
        if (fmt[i] != '%' || i + 1 == fmt.size()) {
            expanded += fmt[i];
            continue;
        }
        char c = fmt[++i];
        if (c == 'f') {
            expanded += pad(us, 6);
        } else if (c == 'z') {
            if (tz) expanded += offset_text(tz->first, false);
        } else if (c == 'Z') {
            if (tz) {
                for (char ch : tz->second) expanded += ch == '%' ? std::string("%%") : std::string(1, ch);
            }
        } else {
            expanded += '%';
            expanded += c;
        }
    }
    if (expanded.empty()) return "";
    std::string out(expanded.size() * 4 + 64, '\0');
    while (true) {
        std::size_t n = std::strftime(out.data(), out.size(), expanded.c_str(), &tm);
        if (n > 0 || out.size() > expanded.size() * 64 + 4096) {
            out.resize(n);
            return out;
        }
        out.resize(out.size() * 2);
    }
}

// ---- parsing -------------------------------------------------------------------------

inline const char* const MONTHS[] = {"january", "february", "march", "april", "may", "june", "july",
                                     "august", "september", "october", "november", "december"};
inline const char* const WEEKDAYS[] = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"};

// datetime.strptime: Python's directives, parsed without the C library (which lacks %f
// and Python's %z forms).
inline datetime datetime::strptime(const std::string& s, const std::string& fmt) {
    auto mismatch = [&]() -> datetime {
        value_error("time data " + repr_str(s) + " does not match format " + repr_str(fmt));
    };
    std::int64_t year = 1900, month = 1, day = 1, hour = 0, minute = 0, second = 0, us = 0, yday = -1;
    std::optional<std::int64_t> hour12, pm;
    std::optional<timezone> tz;
    std::size_t pos = 0;
    auto number = [&](int max_digits, std::int64_t lo, std::int64_t hi, std::int64_t& out) {
        std::size_t start = pos;
        std::int64_t v = 0;
        while (pos < s.size() && pos - start < static_cast<std::size_t>(max_digits) && std::isdigit(static_cast<unsigned char>(s[pos])))
            v = v * 10 + (s[pos++] - '0');
        if (pos == start || v < lo || v > hi) return false;
        out = v;
        return true;
    };
    auto word = [&](const char* const* names, int count, std::int64_t& out) {
        for (int i = 0; i < count; ++i) {
            std::string full = names[i];
            for (std::size_t len : {full.size(), std::size_t{3}}) {
                if (pos + len <= s.size() && strncasecmp(s.c_str() + pos, full.c_str(), len) == 0) {
                    pos += len;
                    out = i;
                    return true;
                }
            }
        }
        return false;
    };
    for (std::size_t i = 0; i < fmt.size(); ++i) {
        char c = fmt[i];
        if (std::isspace(static_cast<unsigned char>(c))) {  // whitespace matches any amount of whitespace
            while (pos < s.size() && std::isspace(static_cast<unsigned char>(s[pos]))) ++pos;
            continue;
        }
        if (c != '%' || i + 1 == fmt.size()) {
            if (pos >= s.size() || s[pos] != c) return mismatch();
            ++pos;
            continue;
        }
        char dir = fmt[++i];
        std::int64_t v = 0;
        bool ok = true;
        switch (dir) {
            case 'Y': ok = number(4, 0, 9999, year); break;
            case 'y': ok = number(2, 0, 99, v), year = v < 69 ? 2000 + v : 1900 + v; break;
            case 'm': ok = number(2, 1, 12, month); break;
            case 'd': ok = number(2, 1, 31, day); break;
            case 'H': ok = number(2, 0, 23, hour); break;
            case 'I': ok = number(2, 1, 12, v), hour12 = v; break;
            case 'M': ok = number(2, 0, 59, minute); break;
            case 'S': ok = number(2, 0, 61, second); break;
            case 'j': ok = number(3, 1, 366, yday); break;
            case 'f': {
                std::size_t start = pos;
                ok = number(6, 0, 999999, us);
                for (std::size_t k = pos - start; ok && k < 6; ++k) us *= 10;  // ".25" is 250000 microseconds
                break;
            }
            case 'b': case 'B': case 'h': ok = word(MONTHS, 12, v), month = v + 1; break;
            case 'a': case 'A': ok = word(WEEKDAYS, 7, v); break;
            case 'p':
                if (pos + 2 <= s.size() && strncasecmp(s.c_str() + pos, "AM", 2) == 0) {
                    pm = 0, pos += 2;
                } else if (pos + 2 <= s.size() && strncasecmp(s.c_str() + pos, "PM", 2) == 0) {
                    pm = 1, pos += 2;
                } else {
                    ok = false;
                }
                break;
            case 'z': {
                if (pos < s.size() && (s[pos] == 'Z' || s[pos] == 'z')) {
                    ++pos;
                    tz = timezone(timedelta(0));
                    break;
                }
                if (pos >= s.size() || (s[pos] != '+' && s[pos] != '-')) {
                    ok = false;
                    break;
                }
                int sign = s[pos++] == '-' ? -1 : 1;
                std::int64_t hh = 0, mm = 0, ss = 0;
                ok = number(2, 0, 23, hh);
                if (ok && pos < s.size() && s[pos] == ':') ++pos;
                ok = ok && number(2, 0, 59, mm);
                if (ok && pos < s.size() && (s[pos] == ':' || std::isdigit(static_cast<unsigned char>(s[pos])))) {
                    if (s[pos] == ':') ++pos;
                    ok = number(2, 0, 59, ss);
                }
                if (ok) tz = timezone(timedelta(0, static_cast<double>(sign * (hh * 3600 + mm * 60 + ss))));
                break;
            }
            case 'Z':
                if (pos + 3 <= s.size() && (s.compare(pos, 3, "UTC") == 0 || s.compare(pos, 3, "GMT") == 0)) {
                    pos += 3;
                } else {
                    ok = false;
                }
                break;
            case '%':
                ok = pos < s.size() && s[pos] == '%';
                if (ok) ++pos;
                break;
            default:
                value_error("'" + std::string(1, dir) + "' is a bad directive in format '%" + std::string(1, dir) + "'");
        }
        if (!ok) return mismatch();
    }
    if (pos < s.size()) value_error("unconverted data remains: " + s.substr(pos));
    if (hour12) hour = *hour12 % 12 + (pm.value_or(0) ? 12 : 0);
    if (yday > 0) {
        auto [yy, mm, dd] = civil_from_days(days_from_civil(year, 1, 1) + yday - 1);
        month = mm, day = dd;
        (void)yy;
    }
    return datetime(year, month, day, hour, minute, second, us, tz);
}

// ISO 8601 as Python 3.11+ reads it: YYYY-MM-DD, then optionally a separator and
// HH[:MM[:SS[.ffffff]]] with an optional Z or +HH:MM[:SS] offset.
inline bool parse_iso_date(const std::string& s, std::size_t& pos, std::int64_t& y, std::int64_t& m, std::int64_t& d) {
    auto digits = [&](int n, std::int64_t& out) {
        if (pos + n > s.size()) return false;
        out = 0;
        for (int i = 0; i < n; ++i) {
            if (!std::isdigit(static_cast<unsigned char>(s[pos + i]))) return false;
            out = out * 10 + (s[pos + i] - '0');
        }
        pos += n;
        return true;
    };
    if (!digits(4, y)) return false;
    bool dashes = pos < s.size() && s[pos] == '-';
    if (dashes) ++pos;
    if (!digits(2, m)) return false;
    if (dashes && (pos >= s.size() || s[pos++] != '-')) return false;
    return digits(2, d);
}

inline bool parse_iso_time(const std::string& s, std::size_t& pos, std::int64_t& h, std::int64_t& mi, std::int64_t& sec,
                           std::int64_t& us, std::optional<timezone>& tz) {
    auto digits = [&](int n, std::int64_t& out) {
        if (pos + n > s.size()) return false;
        out = 0;
        for (int i = 0; i < n; ++i) {
            if (!std::isdigit(static_cast<unsigned char>(s[pos + i]))) return false;
            out = out * 10 + (s[pos + i] - '0');
        }
        pos += n;
        return true;
    };
    mi = sec = us = 0;
    if (!digits(2, h)) return false;
    auto more = [&] { return pos < s.size() && s[pos] != '+' && s[pos] != '-' && s[pos] != 'Z'; };
    if (more()) {
        if (s[pos] == ':') ++pos;
        if (!digits(2, mi)) return false;
    }
    if (more()) {
        if (s[pos] == ':') ++pos;
        if (!digits(2, sec)) return false;
    }
    if (pos < s.size() && (s[pos] == '.' || s[pos] == ',')) {
        ++pos;
        std::size_t start = pos;
        while (pos < s.size() && std::isdigit(static_cast<unsigned char>(s[pos]))) ++pos;
        std::string frac = s.substr(start, pos - start);
        if (frac.empty()) return false;
        frac = (frac + "000000").substr(0, 6);
        us = std::stoll(frac);
    }
    if (pos < s.size() && s[pos] == 'Z') {
        ++pos;
        tz = timezone::utc();
    } else if (pos < s.size() && (s[pos] == '+' || s[pos] == '-')) {
        int sign = s[pos++] == '-' ? -1 : 1;
        std::int64_t oh = 0, om = 0, os = 0;
        if (!digits(2, oh)) return false;
        if (pos < s.size() && s[pos] == ':') ++pos;
        if (pos < s.size() && !digits(2, om)) return false;
        if (pos < s.size() && s[pos] == ':') {
            ++pos;
            if (!digits(2, os)) return false;
        }
        tz = timezone(timedelta(0, static_cast<double>(sign * (oh * 3600 + om * 60 + os))));
    }
    return pos == s.size();
}

inline date date::fromisoformat(const std::string& s) {
    std::size_t pos = 0;
    std::int64_t y, m, d;
    if (!parse_iso_date(s, pos, y, m, d) || pos != s.size()) value_error("Invalid isoformat string: " + repr_str(s));
    return date(y, m, d);
}
inline time time::fromisoformat(const std::string& s) {
    std::size_t pos = s.size() && s[0] == 'T' ? 1 : 0;
    std::int64_t h, mi, sec, us;
    std::optional<timezone> tz;
    if (!parse_iso_time(s, pos, h, mi, sec, us, tz)) value_error("Invalid isoformat string: " + repr_str(s));
    return time(h, mi, sec, us, tz);
}
inline datetime datetime::fromisoformat(const std::string& s) {
    std::size_t pos = 0;
    std::int64_t y, m, d, h = 0, mi = 0, sec = 0, us = 0;
    std::optional<timezone> tz;
    bool ok = parse_iso_date(s, pos, y, m, d);
    if (ok && pos < s.size()) {
        ++pos;  // any one-character separator, as Python allows
        ok = parse_iso_time(s, pos, h, mi, sec, us, tz);
    }
    if (!ok) value_error("Invalid isoformat string: " + repr_str(s));
    return datetime(y, m, d, h, mi, sec, us, tz);
}

}  // namespace sd::datetime

template <>
struct std::hash<sd::datetime::date> {
    std::size_t operator()(const sd::datetime::date& d) const { return std::hash<std::int64_t>()(d.toordinal()); }
};
template <>
struct std::hash<sd::datetime::datetime> {
    std::size_t operator()(const sd::datetime::datetime& d) const { return std::hash<std::int64_t>()(d.hash_key()); }
};
template <>
struct std::hash<sd::datetime::time> {
    std::size_t operator()(const sd::datetime::time& t) const { return std::hash<std::int64_t>()(t.key()); }
};
template <>
struct std::hash<sd::datetime::timedelta> {
    std::size_t operator()(const sd::datetime::timedelta& t) const { return std::hash<std::int64_t>()(t.total_us()); }
};
