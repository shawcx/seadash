// The `string` module: character-set constants, capwords, and Template ($-substitution).
#pragma once

namespace sd::stringmod {

inline const std::string ascii_lowercase = "abcdefghijklmnopqrstuvwxyz";
inline const std::string ascii_uppercase = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
inline const std::string ascii_letters = ascii_lowercase + ascii_uppercase;
inline const std::string digits = "0123456789";
inline const std::string hexdigits = "0123456789abcdefABCDEF";
inline const std::string octdigits = "01234567";
inline const std::string punctuation = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~";
inline const std::string whitespace = " \t\n\r\x0b\x0c";
inline const std::string printable = digits + ascii_letters + punctuation + whitespace;

inline std::string capwords(const std::string& s, std::optional<std::string> sep = std::nullopt) {
    list<std::string> words = sep ? str_split(s, *sep) : str_split(s);
    std::string out;
    for (std::size_t i = 0; i < words.size(); ++i) out += (i ? sep.value_or(" ") : "") + str_capitalize(words[i]);
    return out;
}

// Any mapping's values as text, for Template.substitute(mapping).
template <class M>
dict<std::string, std::string> stringify(const M& mapping) {
    dict<std::string, std::string> out;
    for (const auto& [k, v] : mapping) out[k] = str(v);
    return out;
}

class Template {
    std::string template_;

    static bool id_start(char c) { return std::isalpha(static_cast<unsigned char>(c)) || c == '_'; }
    static bool id_char(char c) { return std::isalnum(static_cast<unsigned char>(c)) || c == '_'; }

    // Walk the template: text, $$, $name, ${name}, or an invalid $.
    template <class F>
    std::string render(F&& lookup, bool safe) const {
        const std::string& t = template_;
        std::string out;
        for (std::size_t i = 0; i < t.size();) {
            if (t[i] != '$') {
                out += t[i++];
                continue;
            }
            std::size_t start = i;
            if (i + 1 < t.size() && t[i + 1] == '$') {
                out += '$';
                i += 2;
                continue;
            }
            std::string name;
            std::size_t end = i + 1;
            if (end < t.size() && id_start(t[end])) {
                while (end < t.size() && id_char(t[end])) ++end;
                name = t.substr(i + 1, end - i - 1);
            } else if (end < t.size() && t[end] == '{' && end + 1 < t.size() && id_start(t[end + 1])) {
                std::size_t close = end + 1;
                while (close < t.size() && id_char(t[close])) ++close;
                if (close < t.size() && t[close] == '}') {
                    name = t.substr(end + 1, close - end - 1);
                    end = close + 1;
                }
            }
            if (name.empty()) {  // `$` not followed by a name
                if (!safe) invalid(start);
                out += '$';
                i += 1;
                continue;
            }
            if (auto value = lookup(name)) {
                out += *value;
            } else if (safe) {
                out += t.substr(start, end - start);
            } else {
                auto e = std::make_shared<KeyError>(repr_str(name));
                e->from_lookup = true;
                throw Thrown{e};
            }
            i = end;
        }
        return out;
    }
    [[noreturn]] void invalid(std::size_t at) const {  // (Python counts from just after the $)
        std::size_t i = at + 1, line = 1 + std::count(template_.begin(), template_.begin() + at, '\n');
        std::size_t newline = template_.rfind('\n', at);
        std::size_t col = newline == std::string::npos ? i : i - (newline + 1);
        raise("ValueError", "Invalid placeholder in string: line " + std::to_string(line) + ", col " + std::to_string(col));
    }

public:
    Template() = default;
    explicit Template(std::string t) : template_(std::move(t)) {}
    std::string substitute(const dict<std::string, std::string>& keywords,
                           const std::optional<dict<std::string, std::string>>& mapping, bool safe = false) const {
        return render([&](const std::string& name) -> std::optional<std::string> {
            if (const std::string* v = keywords.find(name)) return *v;  // keywords win over the mapping
            if (mapping)
                if (const std::string* v = mapping->find(name)) return *v;
            return std::nullopt;
        }, safe);
    }
    list<std::string> get_identifiers() const {
        list<std::string> names;
        render([&](const std::string& name) -> std::optional<std::string> {
            if (std::find(names.begin(), names.end(), name) == names.end()) names.push_back(name);
            return "";
        }, true);
        return names;
    }
    bool is_valid() const {
        try {
            render([](const std::string&) -> std::optional<std::string> { return ""; }, false);
            return true;
        } catch (const Thrown&) {
            return false;
        }
    }
    std::string get_template() const { return template_; }
    std::string sd_repr() const { return "<string.Template object>"; }
};

}  // namespace sd::stringmod
