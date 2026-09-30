// Named time zones read from the system's zoneinfo files (TZif, RFC 8536), for standard
// libraries without <chrono>'s time zone database (Apple's libc++). The part of
// std::chrono's interface that datetime uses: locate_zone(key)->get_info(time).
#pragma once

#include <chrono>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>

namespace sd::datetime::zones {

inline const char* const ZONEINFO_DIR = "/usr/share/zoneinfo";

struct sys_info {
    std::chrono::seconds offset{0};
    std::string abbrev;
};
struct local_info {
    sys_info first;  // for a time that happens twice or never: the one before the change
};

class time_zone {
    struct Change {
        std::int64_t at;  // when (UTC seconds) the zone goes from `before` to `after`
        sys_info before, after;
        std::int64_t at_wall() const { return at + std::max(before.offset, after.offset).count(); }
    };
    // A day in the rule that ends a TZif file ("EST5EDT,M3.2.0,M11.1.0"), which says how
    // the zone carries on after its last listed change.
    struct RuleDay {
        char kind = 'M';  // 'M': month.week.weekday; 'J': day 1..365, never Feb 29; 'N': day 0..365
        int month = 1, week = 1, day = 0;
        std::int64_t time = 7200;
        std::int64_t local(int year) const {  // seconds since 1970 on the wall clock
            namespace ch = std::chrono;
            auto days = [](ch::year_month_day d) { return ch::sys_days(d).time_since_epoch().count(); };
            std::int64_t jan1 = days(ch::year(year) / 1 / 1), when;
            if (kind == 'M') {
                auto month_start = ch::sys_days(ch::year(year) / month / 1);
                std::int64_t first = month_start.time_since_epoch().count();
                std::int64_t in_month = days(ch::year(year) / month / ch::last) - first + 1;
                when = first + (day - static_cast<int>(ch::weekday(month_start).c_encoding()) + 7) % 7 + (week - 1) * 7;
                if (when >= first + in_month) when -= 7;  // week 5 is the last one
            } else if (kind == 'J') {
                when = jan1 + day - 1 + (ch::year(year).is_leap() && day >= 60 ? 1 : 0);
            } else {
                when = jan1 + day;
            }
            return when * 86400 + time;
        }
    };
    struct Rule {
        sys_info std;
        std::optional<sys_info> dst;
        RuleDay start, end;
    };

    std::vector<Change> changes_;
    sys_info initial_;  // before the first change
    std::optional<Rule> rule_;

    // The last change at or before t. When t is a wall-clock time, one in a repeated or
    // skipped hour counts as before the change.
    static const Change* last_change(const std::vector<Change>& changes, std::int64_t t, bool wall) {
        auto it = std::upper_bound(changes.begin(), changes.end(), t,
                                   [&](std::int64_t v, const Change& c) { return v < (wall ? c.at_wall() : c.at); });
        return it == changes.begin() ? nullptr : &*(it - 1);
    }
    sys_info find(std::int64_t t, bool wall) const {
        const Change* last = last_change(changes_, t, wall);
        if (last == nullptr && !changes_.empty()) return initial_;
        if (last != nullptr && (last != &changes_.back() || !rule_)) return last->after;
        if (!rule_) return initial_;
        // Past the listed changes: the rule's two changes a year, around t.
        const Rule& r = *rule_;
        if (!r.dst) return r.std;
        namespace ch = std::chrono;
        int year = static_cast<int>(ch::year_month_day(ch::floor<ch::days>(ch::sys_seconds(ch::seconds(t)))).year());
        std::vector<Change> yearly;
        for (int y = year - 1; y <= year + 1; ++y) {
            yearly.push_back({r.start.local(y) - r.std.offset.count(), r.std, *r.dst});
            yearly.push_back({r.end.local(y) - r.dst->offset.count(), *r.dst, r.std});
        }
        std::sort(yearly.begin(), yearly.end(), [](const Change& a, const Change& b) { return a.at < b.at; });
        const Change* c = last_change(yearly, t, wall);
        if (c != nullptr && (last == nullptr || c->at >= last->at)) return c->after;
        return last != nullptr ? last->after : r.std;
    }

    [[noreturn]] static void invalid() { throw std::runtime_error("invalid time zone file"); }

    // ---- the rule: std offset [dst [offset] [,start[/time],end[/time]]] ----
    static std::string rule_name(const char*& p) {
        std::string name;
        if (*p == '<') {
            for (++p; *p && *p != '>'; ++p) name += *p;
            if (*p != '>') invalid();
            ++p;
        } else {
            while (std::isalpha(static_cast<unsigned char>(*p))) name += *p++;
        }
        if (name.empty()) invalid();
        return name;
    }
    static std::int64_t rule_number(const char*& p) {
        if (!std::isdigit(static_cast<unsigned char>(*p))) invalid();
        std::int64_t n = 0;
        while (std::isdigit(static_cast<unsigned char>(*p))) n = n * 10 + (*p++ - '0');
        return n;
    }
    static std::int64_t rule_time(const char*& p) {  // [+-]h[:mm[:ss]]
        int sign = *p == '-' ? -1 : 1;
        if (*p == '-' || *p == '+') ++p;
        std::int64_t secs = rule_number(p) * 3600;
        if (*p == ':') secs += rule_number(++p) * 60;
        if (*p == ':') secs += rule_number(++p);
        return sign * secs;
    }
    static RuleDay rule_day(const char*& p) {
        RuleDay d;
        if (*p == 'M') {
            d.month = static_cast<int>(rule_number(++p));
            if (*p != '.') invalid();
            d.week = static_cast<int>(rule_number(++p));
            if (*p != '.') invalid();
            d.day = static_cast<int>(rule_number(++p));
            if (d.month < 1 || d.month > 12 || d.week < 1 || d.week > 5 || d.day > 6) invalid();
        } else if (*p == 'J') {
            d.kind = 'J';
            d.day = static_cast<int>(rule_number(++p));
        } else {
            d.kind = 'N';
            d.day = static_cast<int>(rule_number(p));
        }
        if (*p == '/') d.time = rule_time(++p);
        return d;
    }
    static std::optional<Rule> parse_rule(const std::string& text) {
        if (text.empty()) return std::nullopt;
        const char* p = text.c_str();
        Rule r;
        r.std.abbrev = rule_name(p);
        r.std.offset = std::chrono::seconds(-rule_time(p));  // the rule counts west of UTC
        if (*p == '\0') return r;
        sys_info dst;
        dst.abbrev = rule_name(p);
        dst.offset = *p != ',' && *p != '\0' ? std::chrono::seconds(-rule_time(p)) : r.std.offset + std::chrono::hours(1);
        if (*p != ',') return r;  // no dates for the changes: standard time only
        r.start = rule_day(++p);
        if (*p != ',') invalid();
        r.end = rule_day(++p);
        if (*p != '\0') invalid();
        r.dst = dst;
        return r;
    }

public:
    explicit time_zone(const std::string& data) {
        auto byte = [&](std::size_t i) { return static_cast<unsigned char>(data[i]); };
        auto number = [&](std::size_t i, std::size_t size) {  // big-endian, signed
            std::uint64_t v = 0;
            for (std::size_t k = 0; k < size; ++k) v = (v << 8) | byte(i + k);
            return size == 4 ? static_cast<std::int64_t>(static_cast<std::int32_t>(v)) : static_cast<std::int64_t>(v);
        };
        std::size_t header = 0, time_size = 4;
        std::size_t isut = 0, isstd = 0, leaps = 0, times = 0, types = 0, chars = 0;
        auto read_header = [&] {
            if (data.size() < header + 44 || data.compare(header, 4, "TZif") != 0) invalid();
            std::size_t* counts[] = {&isut, &isstd, &leaps, &times, &types, &chars};
            for (std::size_t k = 0; k < 6; ++k) *counts[k] = static_cast<std::uint32_t>(number(header + 20 + 4 * k, 4));
        };
        auto body_size = [&] { return times * (time_size + 1) + types * 6 + chars + leaps * (time_size + 4) + isstd + isut; };
        read_header();
        if (byte(4) >= '2') {  // the same again with 64-bit times, which is the one to read
            header += 44 + body_size();
            time_size = 8;
            read_header();
        }
        if (data.size() < header + 44 + body_size() || types == 0) invalid();
        std::size_t time_at = header + 44, index_at = time_at + times * time_size;
        std::size_t type_at = index_at + times, char_at = type_at + types * 6;
        std::vector<sys_info> infos;
        std::optional<std::size_t> first_standard;
        for (std::size_t k = 0; k < types; ++k) {
            std::size_t abbrev = byte(type_at + k * 6 + 5);
            if (abbrev >= chars) invalid();
            std::size_t end = data.find('\0', char_at + abbrev);
            if (end == std::string::npos || end > char_at + chars) end = char_at + chars;
            infos.push_back({std::chrono::seconds(number(type_at + k * 6, 4)), data.substr(char_at + abbrev, end - char_at - abbrev)});
            if (!first_standard && byte(type_at + k * 6 + 4) == 0) first_standard = k;
        }
        initial_ = infos[first_standard.value_or(0)];
        for (std::size_t k = 0; k < times; ++k) {
            std::size_t type = byte(index_at + k);
            if (type >= types) invalid();
            changes_.push_back({number(time_at + k * time_size, time_size), k ? changes_.back().after : initial_, infos[type]});
        }
        if (time_size == 8) {
            std::size_t footer = header + 44 + body_size();
            std::size_t end = data.find('\n', footer + 1);
            if (footer < data.size() && data[footer] == '\n' && end != std::string::npos)
                rule_ = parse_rule(data.substr(footer + 1, end - footer - 1));
        }
    }
    time_zone(const time_zone&) = delete;

    sys_info get_info(std::chrono::sys_seconds t) const { return find(t.time_since_epoch().count(), false); }
    local_info get_info(std::chrono::local_seconds t) const { return {find(t.time_since_epoch().count(), true)}; }
};

// The zone with this key ("Europe/Paris"); the same pointer each time. Throws
// std::runtime_error if there's no such zone.
inline const time_zone* locate_zone(const std::string& key) {
    static std::mutex mu;
    static std::map<std::string, std::unique_ptr<time_zone>> loaded;
    std::lock_guard lock(mu);
    if (auto it = loaded.find(key); it != loaded.end()) return it->second.get();
    std::filesystem::path relative(key);
    bool plain = !key.empty() && relative.is_relative();
    for (const auto& part : relative) plain = plain && part != ".." && part != ".";
    std::ifstream file;
    if (plain && std::filesystem::is_regular_file(ZONEINFO_DIR / relative)) file.open(ZONEINFO_DIR / relative, std::ios::binary);
    if (!file) throw std::runtime_error("no time zone " + key);
    std::string data((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
    return loaded.emplace(key, std::make_unique<time_zone>(data)).first->second.get();
}

// Every zone's key, as Python finds them: the TZif files under the zoneinfo directory.
inline std::set<std::string> zone_names() {
    namespace fs = std::filesystem;
    std::set<std::string> out;
    std::error_code ec;
    fs::path root = fs::canonical(ZONEINFO_DIR, ec);
    if (ec) return out;
    for (auto it = fs::recursive_directory_iterator(root, ec); !ec && it != fs::recursive_directory_iterator(); it.increment(ec)) {
        std::string key = it->path().lexically_relative(root).generic_string();
        if (key == "posix" || key == "right") {  // copies of the same zones
            it.disable_recursion_pending();
            continue;
        }
        if (key == "posixrules" || !it->is_regular_file(ec)) continue;
        char magic[4] = {};
        if (std::ifstream(it->path(), std::ios::binary).read(magic, 4) && std::string_view(magic, 4) == "TZif") out.insert(key);
    }
    return out;
}

}  // namespace sd::datetime::zones
