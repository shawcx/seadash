// The `zoneinfo` module: named time zones from the system's IANA database, as tzinfo
// values for datetime (daylight saving time and all).
#pragma once

#include "datetime.hpp"

namespace sd::zoneinfo {

struct ZoneInfoNotFoundError : KeyError {
    using KeyError::KeyError;
    std::string sd_type() const override { return "zoneinfo.ZoneInfoNotFoundError"; }
};

inline datetime::timezone ZoneInfo(const std::string& key) {
    try {
        return datetime::timezone::named(std::chrono::locate_zone(key), key);
    } catch (const std::runtime_error&) {
        auto e = std::make_shared<ZoneInfoNotFoundError>(repr_str("No time zone found with key " + key));
        e->from_lookup = true;  // shown like a KeyError's key: 'No time zone found ...'
        throw Thrown{e};
    }
}

inline std::set<std::string> available_timezones() {
    std::set<std::string> out;
    const auto& db = std::chrono::get_tzdb();
    for (const auto& z : db.zones) out.insert(std::string(z.name()));
    for (const auto& l : db.links) out.insert(std::string(l.name()));
    return out;
}

}  // namespace sd::zoneinfo
