// The `enum` module: Enum, IntEnum, StrEnum, Flag and IntFlag.
//
// The checker works out every member's value when compiling, and codegen makes each enum
// a small value struct: an index into its class's table of members (sd_index), or for a
// flag the bits themselves (sd_bits). The table holds what's the same for every member:
// the class name, the members' names and values, and for flags which members are single
// bits. Here are the parts that don't depend on the enum: lookups, repr and str.
#pragma once

namespace sd::enums {

template <class V>
struct Table {
    std::string cls;                  // "Color"
    std::vector<std::string> names;   // the distinct members, in definition order (aliases aren't here)
    std::vector<V> values;
    std::vector<std::pair<std::string, std::int64_t>> by_name;  // every name, aliases too -> its member
    // Flags only:
    std::vector<std::int64_t> canonical;  // the single-bit members, in definition order
    std::int64_t mask = 0;                // every member's bits
    bool keep = false;                    // IntFlag: any int is a value (Flag: only the members' bits)
};

// Color(value): the member with that value.
template <class V, class X>
std::int64_t lookup(const Table<V>& t, const X& value) {
    for (std::size_t i = 0; i < t.values.size(); ++i)
        if (t.values[i] == value) return static_cast<std::int64_t>(i);
    raise("ValueError", repr(value) + " is not a valid " + t.cls);
}

// Color["RED"]: the member with that name (or alias).
template <class V>
std::int64_t lookup_name(const Table<V>& t, const std::string& name) {
    for (const auto& [n, i] : t.by_name)
        if (n == name) return i;
    raise("KeyError", repr(name));
}

template <class V>
std::string repr(const Table<V>& t, std::int64_t i) {
    return "<" + t.cls + "." + t.names[i] + ": " + sd::repr(t.values[i]) + ">";
}

// ---- flags ------------------------------------------------------------------

inline std::int64_t bit_length(std::int64_t v) {
    std::int64_t n = 0;
    for (auto u = static_cast<std::uint64_t>(v); u; u >>= 1) ++n;
    return n;
}

// All the bits an IntFlag's `~` works within: every bit up to its highest member's.
template <class V>
std::int64_t all_bits(const Table<V>& t) {
    return t.mask ? static_cast<std::int64_t>((std::uint64_t(1) << bit_length(t.mask)) - 1) : 0;
}

// Python's enum._bin: "0b0 0111", sign-extended to max_bits digits.
inline std::string bin_digits(std::int64_t num, std::int64_t max_bits) {
    std::string digits;
    for (auto u = static_cast<std::uint64_t>(num); u; u >>= 1) digits.insert(digits.begin(), u & 1 ? '1' : '0');
    std::string s = "0b0" + digits;  // (num >= 0: bin(num + ceiling) with its leading 1 made 0)
    std::string sign = s.substr(0, 3), rest = s.substr(3);
    if (static_cast<std::int64_t>(rest.size()) < max_bits)
        rest = (std::string(max_bits, sign.back()) + rest).substr(rest.size());
    return sign + " " + rest;
}

// Flag(value): the value's bits, checked (Flag) or kept as they are (IntFlag).
template <class V>
std::int64_t flag_value(const Table<V>& t, std::int64_t value) {
    if (value < 0) {
        std::int64_t top = std::max<std::int64_t>(all_bits(t) + 1, std::int64_t(1) << bit_length(~value));
        value += top;
    }
    if (!t.keep && (value & ~t.mask)) {
        std::int64_t max_bits = std::max(bit_length(value), bit_length(t.mask));
        raise("ValueError", "<flag '" + t.cls + "'> invalid value " + std::to_string(value) + "\n    given " +
                                bin_digits(value, max_bits) + "\n  allowed " + bin_digits(t.mask, max_bits));
    }
    return value;
}

// The single-bit members a flag value holds, in definition order (indexes into the table).
template <class V>
std::vector<std::int64_t> flag_members(const Table<V>& t, std::int64_t bits) {
    std::vector<std::int64_t> out;
    for (std::int64_t i : t.canonical)
        if ((bits & t.values[i]) == t.values[i]) out.push_back(i);
    return out;
}

// A flag value's name: a member's own name, or its members' names joined with '|' (and
// an IntFlag's unnamed bits as a number), or None for no members at all.
template <class V>
std::optional<std::string> flag_name(const Table<V>& t, std::int64_t bits) {
    for (std::size_t i = 0; i < t.values.size(); ++i)
        if (t.values[i] == bits) return t.names[i];
    std::string out;
    std::int64_t known = 0;
    for (std::int64_t i : flag_members(t, bits)) {
        out += (out.empty() ? "" : "|") + t.names[i];
        known |= t.values[i];
    }
    if (out.empty()) return std::nullopt;
    if (std::int64_t unknown = bits & ~known) out += "|" + std::to_string(unknown);
    return out;
}

template <class V>
std::string flag_repr(const Table<V>& t, std::int64_t bits) {
    auto name = flag_name(t, bits);
    return "<" + t.cls + (name ? "." + *name : "") + ": " + std::to_string(bits) + ">";
}

template <class V>
std::string flag_str(const Table<V>& t, std::int64_t bits) {
    if (t.keep) return std::to_string(bits);  // IntFlag: str() is the number
    auto name = flag_name(t, bits);
    return name ? t.cls + "." + *name : t.cls + "(" + std::to_string(bits) + ")";
}

template <class V>
std::int64_t flag_invert(const Table<V>& t, std::int64_t bits) {
    return (t.keep ? all_bits(t) : t.mask) & ~bits;
}

// `member in flags`: are all of member's bits set?
template <class F>
bool flag_contains(const F& flags, const F& member) {
    return (flags.sd_bits & member.sd_bits) == member.sd_bits;
}

}  // namespace sd::enums
