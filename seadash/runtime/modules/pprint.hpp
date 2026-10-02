// The `pprint` module: Python's PrettyPrinter layout (a value on one line if it fits, else a
// container's items one per line under its bracket, long strings split into pieces), over
// seadash's values. Other objects (dataclasses included) print as their repr.
#pragma once

#include <algorithm>
#include <optional>
#include <string>
#include <vector>

#include "../seadash.hpp"

namespace sd::pprint {

struct Options {
    std::int64_t indent = 1;
    std::int64_t width = 80;
    std::optional<std::int64_t> depth;
    bool compact = false;
    bool sort_dicts = true;
    bool underscore_numbers = false;
};

template <class T> struct is_dictlike : std::false_type {};
template <class K, class V> struct is_dictlike<dict<K, V>> : std::true_type {};
template <class T> struct is_setlike : std::false_type {};
template <class T> struct is_setlike<set<T>> : std::true_type {};
template <class T> struct is_vtuple : std::false_type {};
template <class T> struct is_vtuple<vtuple<T>> : std::true_type {};
template <class T> struct is_vector : std::false_type {};  // (list(...) of an iterator, before it's stored)
template <class T> struct is_vector<std::vector<T>> : std::true_type {};

// Characters, not bytes: what a line's width counts.
inline std::int64_t width_of(const std::string& s) {
    std::int64_t n = 0;
    for (unsigned char c : s) n += (c & 0xC0) != 0x80;
    return n;
}

class Printer {
    Options o_;

public:
    explicit Printer(Options o) : o_(o) {
        if (o_.indent < 0) raise("ValueError", "indent must be >= 0");
        if (o_.depth && *o_.depth <= 0) raise("ValueError", "depth must be > 0");
        if (o_.width == 0) raise("ValueError", "width must be != 0");
    }

    // The items of a dict, sorted by key when sort_dicts is on (and keys can be ordered).
    template <class K, class V>
    std::vector<std::pair<K, V>> items(const dict<K, V>& d) const {
        std::vector<std::pair<K, V>> out;
        for (const auto& [k, v] : d) out.emplace_back(k, v);
        if constexpr (requires(const K& a, const K& b) { a < b; }) {
            if (o_.sort_dicts) std::stable_sort(out.begin(), out.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
        }
        return out;
    }
    template <class T>
    static std::vector<T> sorted_set(const set<T>& s) {
        std::vector<T> out(s.begin(), s.end());
        if constexpr (requires(const T& a, const T& b) { a < b; }) std::stable_sort(out.begin(), out.end());
        return out;
    }

    // One line: Python's repr, but with dicts sorted, depth honored and underscores in ints.
    template <class T>
    std::string safe_repr(const T& x, std::int64_t level) const {
        if constexpr (std::is_same_v<T, std::int64_t>) {
            if (!o_.underscore_numbers) return std::to_string(x);
            std::string digits = std::to_string(x < 0 ? -static_cast<unsigned long long>(x) : static_cast<unsigned long long>(x));
            std::string out;
            for (std::size_t i = 0; i < digits.size(); ++i) {
                if (i > 0 && (digits.size() - i) % 3 == 0) out += '_';
                out += digits[i];
            }
            return (x < 0 ? "-" : "") + out;
        } else if constexpr (is_optional<T>::value) {
            return x ? safe_repr(*x, level) : std::string("None");
        } else if constexpr (is_dictlike<T>::value) {
            if (x.empty()) return "{}";
            if (o_.depth && level >= *o_.depth) return "{...}";
            std::string out = "{";
            bool first = true;
            for (const auto& [k, v] : items(x)) {
                if (!first) out += ", ";
                first = false;
                out += safe_repr(k, level + 1) + ": " + safe_repr(v, level + 1);
            }
            return out + "}";
        } else if constexpr (is_list<T>::value || is_vtuple<T>::value || is_vector<T>::value) {
            const auto& xs = [&]() -> const auto& { if constexpr (is_vtuple<T>::value) return x.items; else return x; }();
            bool tuple = is_vtuple<T>::value;
            if (xs.empty()) return tuple ? "()" : "[]";
            std::string open = tuple ? "(" : "[", close = tuple ? (xs.size() == 1 ? ",)" : ")") : "]";
            if (o_.depth && level >= *o_.depth) return open + "..." + close;
            std::string out = open;
            for (std::size_t i = 0; i < xs.size(); ++i) out += (i ? ", " : "") + safe_repr(xs[i], level + 1);
            return out + close;
        } else if constexpr (is_tuple<T>::value) {
            constexpr std::size_t n = std::tuple_size_v<T>;
            if constexpr (n == 0) {
                return "()";
            } else {
                if (o_.depth && level >= *o_.depth) return n == 1 ? "(...,)" : "(...)";
                std::string out = "(";
                std::apply([&](const auto&... e) {
                    std::size_t i = 0;
                    ((out += (i++ ? ", " : "") + safe_repr(e, level + 1)), ...);
                }, x);
                return out + (n == 1 ? ",)" : ")");
            }
        } else {
            return repr(x);
        }
    }

    // Python's _format: x at column `indent`, with `allowance` columns kept for what follows it.
    template <class T>
    void format(const T& x, std::string& out, std::int64_t indent, std::int64_t allowance, std::int64_t level) const {
        std::string rep = safe_repr(x, level);
        if (width_of(rep) <= o_.width - indent - allowance) {
            out += rep;
            return;
        }
        level += 1;
        if constexpr (is_optional<T>::value) {
            if (x) format(*x, out, indent, allowance, level - 1);
            else out += rep;
        } else if constexpr (std::is_same_v<T, std::string>) {
            format_str(x, out, indent, allowance, level);
        } else if constexpr (is_dictlike<T>::value) {
            out += '{';
            if (o_.indent > 1) out += std::string(static_cast<std::size_t>(o_.indent - 1), ' ');
            if (!x.empty()) format_dict_items(items(x), out, indent, allowance + 1, level);
            out += '}';
        } else if constexpr (is_list<T>::value || is_vector<T>::value) {
            out += '[';
            format_items(std::vector<typename T::value_type>(x.begin(), x.end()), out, indent, allowance + 1, level);
            out += ']';
        } else if constexpr (is_vtuple<T>::value) {
            std::string end = x.items.size() == 1 ? ",)" : ")";
            out += '(';
            format_items(std::vector<typename decltype(x.items)::value_type>(x.items.begin(), x.items.end()), out, indent,
                         allowance + width_of(end), level);
            out += end;
        } else if constexpr (is_setlike<T>::value) {
            if (x.empty()) {
                out += rep;
                return;
            }
            out += '{';
            format_items(sorted_set(x), out, indent, allowance + 1, level);
            out += '}';
        } else if constexpr (is_tuple<T>::value) {
            format_tuple(x, out, indent, allowance, level);
        } else {
            out += rep;
        }
    }

    template <class Items>
    void format_dict_items(const Items& items, std::string& out, std::int64_t indent, std::int64_t allowance,
                           std::int64_t level) const {
        indent += o_.indent;
        std::string delimnl = ",\n" + std::string(static_cast<std::size_t>(indent), ' ');
        for (std::size_t i = 0; i < items.size(); ++i) {
            bool last = i + 1 == items.size();
            std::string rep = safe_repr(items[i].first, level);
            out += rep + ": ";
            format(items[i].second, out, indent + width_of(rep) + 2, last ? allowance : 1, level);
            if (!last) out += delimnl;
        }
    }

    template <class T>
    void format_items(const std::vector<T>& items, std::string& out, std::int64_t indent, std::int64_t allowance,
                      std::int64_t level) const {
        indent += o_.indent;
        if (o_.indent > 1) out += std::string(static_cast<std::size_t>(o_.indent - 1), ' ');
        std::string delimnl = ",\n" + std::string(static_cast<std::size_t>(indent), ' '), delim;
        std::int64_t max_width = o_.width - indent + 1, width = max_width;
        for (std::size_t i = 0; i < items.size(); ++i) {
            bool last = i + 1 == items.size();
            if (last) {
                max_width -= allowance;
                width -= allowance;
            }
            if (o_.compact) {
                std::string rep = safe_repr(items[i], level);
                std::int64_t w = width_of(rep) + 2;
                if (width < w) {
                    width = max_width;
                    if (!delim.empty()) delim = delimnl;
                }
                if (width >= w) {
                    width -= w;
                    out += delim;
                    delim = ", ";
                    out += rep;
                    continue;
                }
            }
            out += delim;
            delim = delimnl;
            format(items[i], out, indent, last ? allowance : 1, level);
        }
    }

    template <class... Ts>
    void format_tuple(const std::tuple<Ts...>& x, std::string& out, std::int64_t indent, std::int64_t allowance,
                      std::int64_t level) const {
        std::string end = sizeof...(Ts) == 1 ? ",)" : ")";
        out += '(';
        indent += o_.indent;
        if (o_.indent > 1) out += std::string(static_cast<std::size_t>(o_.indent - 1), ' ');
        std::string delimnl = ",\n" + std::string(static_cast<std::size_t>(indent), ' ');
        std::size_t i = 0;
        std::apply([&](const auto&... e) {
            ((out += (i++ ? delimnl : std::string()),
              format(e, out, indent, i == sizeof...(Ts) ? allowance + width_of(end) : 1, level)), ...);
        }, x);
        out += end;
    }

    // Python's _pprint_str: a long string as pieces that fit, broken after whitespace (and at
    // newlines); at the top level they're wrapped in parentheses, so the result is a valid expression.
    void format_str(const std::string& s, std::string& out, std::int64_t indent, std::int64_t allowance,
                    std::int64_t level) const {
        if (s.empty()) {
            out += repr(s);
            return;
        }
        std::vector<std::string> lines;
        for (std::size_t start = 0; start < s.size();) {
            std::size_t nl = s.find('\n', start);
            std::size_t end = nl == std::string::npos ? s.size() : nl + 1;
            lines.push_back(s.substr(start, end - start));
            start = end;
        }
        if (level == 1) {
            indent += 1;
            allowance += 1;
        }
        std::int64_t max_width1 = o_.width - indent, max_width = max_width1;
        std::vector<std::string> chunks;
        std::string rep;
        for (std::size_t i = 0; i < lines.size(); ++i) {
            const std::string& line = lines[i];
            rep = repr(line);
            if (i + 1 == lines.size()) max_width1 -= allowance;
            if (width_of(rep) <= max_width1) {
                chunks.push_back(rep);
                continue;
            }
            std::vector<std::string> parts;  // runs of non-space then space
            for (std::size_t k = 0; k < line.size();) {
                std::size_t j = k;
                while (j < line.size() && !std::isspace(static_cast<unsigned char>(line[j]))) ++j;
                while (j < line.size() && std::isspace(static_cast<unsigned char>(line[j]))) ++j;
                parts.push_back(line.substr(k, j - k));
                k = j;
            }
            std::int64_t max_width2 = max_width;
            std::string current;
            for (std::size_t j = 0; j < parts.size(); ++j) {
                std::string candidate = current + parts[j];
                if (j + 1 == parts.size() && i + 1 == lines.size()) max_width2 -= allowance;
                if (width_of(repr(candidate)) > max_width2) {
                    if (!current.empty()) chunks.push_back(repr(current));
                    current = parts[j];
                } else {
                    current = candidate;
                }
            }
            if (!current.empty()) chunks.push_back(repr(current));
        }
        if (chunks.size() == 1) {
            out += rep;
            return;
        }
        if (level == 1) out += '(';
        for (std::size_t i = 0; i < chunks.size(); ++i) {
            if (i > 0) out += "\n" + std::string(static_cast<std::size_t>(indent), ' ');
            out += chunks[i];
        }
        if (level == 1) out += ')';
    }

    template <class T>
    std::string pformat(const T& x) const {
        std::string out;
        format(x, out, 0, 0, 0);
        return out;
    }
};

template <class T>
std::string pformat(const T& x, std::int64_t indent, std::int64_t width, std::optional<std::int64_t> depth, bool compact,
                    bool sort_dicts, bool underscore_numbers) {
    return Printer({indent, width, depth, compact, sort_dicts, underscore_numbers}).pformat(x);
}
template <class T, class Stream>
void pprint(const T& x, const Stream& stream, std::int64_t indent, std::int64_t width, std::optional<std::int64_t> depth,
            bool compact, bool sort_dicts, bool underscore_numbers) {
    std::string text = pformat(x, indent, width, depth, compact, sort_dicts, underscore_numbers);
    if constexpr (is_optional<Stream>::value) {
        if (stream) sd::print_to(*stream, " ", "\n", text);
        else sd::print(" ", "\n", text);
    } else {
        sd::print_to(stream, " ", "\n", text);
    }
}
template <class T>
std::string saferepr(const T& x) {
    return Printer({}).safe_repr(x, 0);
}

// isreadable(x): its repr is a Python literal (numbers, strings, bytes, None and containers of
// them; not objects). isrecursive(x): seadash's values can't contain themselves.
template <class T>
bool isreadable(const T& x) {
    if constexpr (std::is_arithmetic_v<T> || std::is_same_v<T, Bool> || std::is_same_v<T, std::string> ||
                  std::is_same_v<T, bytes>) {
        return true;
    } else if constexpr (is_optional<T>::value) {
        return !x || isreadable(*x);
    } else if constexpr (is_dictlike<T>::value) {
        for (const auto& [k, v] : x)
            if (!isreadable(k) || !isreadable(v)) return false;
        return true;
    } else if constexpr (is_list<T>::value || is_vector<T>::value || is_setlike<T>::value) {
        for (const auto& e : x)
            if (!isreadable(e)) return false;
        return true;
    } else if constexpr (is_vtuple<T>::value) {
        return isreadable(x.items);
    } else if constexpr (is_tuple<T>::value) {
        return std::apply([](const auto&... e) { return (isreadable(e) && ...); }, x);
    } else {
        return false;
    }
}
template <class T>
bool isrecursive(const T&) {
    return false;
}

// pprint.PrettyPrinter(...): the options, kept for its pformat()/pprint().
struct PrettyPrinter {
    Options options;
    std::optional<std::shared_ptr<TextFile>> stream;
    template <class T>
    std::string pformat(const T& x) const { return Printer(options).pformat(x); }
    template <class T>
    void pprint(const T& x) const {
        std::string text = pformat(x);
        if (stream) sd::print_to(*stream, " ", "\n", text);
        else sd::print(" ", "\n", text);
    }
    template <class T>
    bool isreadable(const T& x) const { return sd::pprint::isreadable(x); }
    template <class T>
    bool isrecursive(const T& x) const { return sd::pprint::isrecursive(x); }
};
inline PrettyPrinter make_printer(std::int64_t indent, std::int64_t width, std::optional<std::int64_t> depth,
                                  std::optional<std::shared_ptr<TextFile>> stream, bool compact, bool sort_dicts,
                                  bool underscore_numbers) {
    Printer check({indent, width, depth, compact, sort_dicts, underscore_numbers});  // (its errors)
    return PrettyPrinter{{indent, width, depth, compact, sort_dicts, underscore_numbers}, std::move(stream)};
}

}  // namespace sd::pprint
