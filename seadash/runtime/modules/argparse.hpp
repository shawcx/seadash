// The `argparse` module: command-line parsing with Python's behaviour, help layout
// and error messages. The checker knows every argument's type, so the parsed values
// are read back with typed accessors (Namespace::get<T>).
#pragma once

#include <sys/ioctl.h>
#include <unistd.h>

#include <variant>

#include "pathlib.hpp"

namespace sd::argparse {

// A parsed (or default) value. Lists hold one kind of item, given by `type=`.
struct Value {
    std::variant<std::monostate, bool, std::int64_t, double, std::string, list<std::int64_t>, list<double>,
                 list<std::string>>
        v;
    bool path = false;  // a Path (or list of them), shown as PosixPath('...')
    Value() = default;
    Value(std::nullopt_t) {}
    template <class T>
    Value(const std::optional<T>& o) {
        if (o) *this = Value(*o);
    }
    Value(bool b) : v(b) {}
    Value(std::int64_t i) : v(i) {}
    Value(double d) : v(d) {}
    Value(std::string s) : v(std::move(s)) {}
    Value(list<std::int64_t> l) : v(std::move(l)) {}
    Value(list<double> l) : v(std::move(l)) {}
    Value(list<std::string> l) : v(std::move(l)) {}
    Value(const pathlib::Path& p) : v(p.str()), path(true) {}
    bool is_none() const { return std::holds_alternative<std::monostate>(v); }
    std::string sd_repr() const {
        return std::visit([this](const auto& x) -> std::string {
            using X = std::decay_t<decltype(x)>;
            if constexpr (std::is_same_v<X, std::monostate>) {
                return "None";
            } else if constexpr (std::is_same_v<X, std::string>) {
                return path ? pathlib::Path(x).sd_repr() : repr(x);
            } else if constexpr (std::is_same_v<X, list<std::string>>) {
                if (!path) return repr(x);
                std::string out = "[";
                for (std::size_t i = 0; i < x.size(); ++i) out += (i ? ", " : "") + pathlib::Path(x[i]).sd_repr();
                return out + "]";
            } else {
                return repr(x);
            }
        }, v);
    }
    std::string sd_str() const {
        if (auto* s = std::get_if<std::string>(&v)) return *s;
        return sd_repr();
    }
    bool operator==(const Value& o) const { return v == o.v; }
};

enum Kind { STR, INT, FLOAT, PATH };
inline constexpr std::int64_t ONE = -1, OPTIONAL = -2, ANY = -3, SOME = -4;  // nargs None, '?', '*', '+'

struct Spec {
    list<std::string> flags;  // empty for a positional
    std::string dest;
    std::string action = "store";  // store, store_true, store_false, store_const, count, append, help, version
    std::int64_t nargs = ONE;       // ONE, OPTIONAL, ANY, SOME, or a count
    Kind kind = STR;
    Value default_;
    Value const_;
    list<Value> choices;
    bool required = false;
    std::optional<std::string> help;
    std::optional<std::string> metavar;
    std::optional<std::string> version;
    bool positional() const { return flags.empty(); }
};

class ArgumentParser;

class Namespace {
    std::vector<std::pair<std::string, Value>> values_;  // in the order the arguments were added
    friend class ArgumentParser;

public:
    Value& slot(const std::string& dest) {
        for (auto& [k, v] : values_)
            if (k == dest) return v;
        values_.emplace_back(dest, Value());
        return values_.back().second;
    }
    const Value& at(const std::string& dest) const {
        for (auto& [k, v] : values_)
            if (k == dest) return v;
        static const Value none;
        return none;
    }
    template <class T>
    T get(const std::string& dest) const {
        return convert<T>(at(dest));
    }
    template <class T>
    static T convert(const Value& value) {
        if constexpr (is_optional<T>::value) {
            if (value.is_none()) return std::nullopt;
            return convert<typename T::value_type>(value);
        } else if constexpr (std::is_same_v<T, bool>) {
            return std::get<bool>(value.v);
        } else if constexpr (std::is_same_v<T, std::int64_t>) {
            if (auto* b = std::get_if<bool>(&value.v)) return *b;
            return std::get<std::int64_t>(value.v);
        } else if constexpr (std::is_same_v<T, double>) {
            if (auto* i = std::get_if<std::int64_t>(&value.v)) return static_cast<double>(*i);
            return std::get<double>(value.v);
        } else if constexpr (std::is_same_v<T, std::string>) {
            return std::get<std::string>(value.v);
        } else if constexpr (std::is_same_v<T, pathlib::Path>) {
            return pathlib::Path(std::get<std::string>(value.v));
        } else {  // a list
            using E = typename T::value_type;
            T out;
            std::visit([&](const auto& items) {
                using L = std::decay_t<decltype(items)>;
                if constexpr (is_vector<L>::value) {
                    for (const auto& item : items) out.push_back(convert<E>(Value(item)));
                }
            }, value.v);
            return out;
        }
    }
    std::string sd_repr() const {
        std::string out = "Namespace(";
        for (std::size_t i = 0; i < values_.size(); ++i) out += (i ? ", " : "") + values_[i].first + "=" + values_[i].second.sd_repr();
        return out + ")";
    }
};

inline std::size_t terminal_width() {
    if (const char* c = std::getenv("COLUMNS")) {
        try {
            return static_cast<std::size_t>(std::max(std::stoi(c), 3));
        } catch (...) {
        }
    }
    winsize w{};
    if (::ioctl(STDOUT_FILENO, TIOCGWINSZ, &w) == 0 && w.ws_col > 0) return w.ws_col;
    return 80;
}

// Word-wraps `text` to `width`, like Python's textwrap.fill (whitespace collapsed).
inline list<std::string> wrap(const std::string& text, std::size_t width) {
    list<std::string> lines;
    std::string line;
    std::size_t i = 0;
    while (i < text.size()) {
        while (i < text.size() && std::isspace(static_cast<unsigned char>(text[i]))) ++i;
        std::size_t j = i;
        while (j < text.size() && !std::isspace(static_cast<unsigned char>(text[j]))) ++j;
        if (j == i) break;
        std::string word = text.substr(i, j - i);
        if (!line.empty() && line.size() + 1 + word.size() > width) {
            lines.push_back(line);
            line.clear();
        }
        line += (line.empty() ? "" : " ") + word;
        i = j;
    }
    if (!line.empty()) lines.push_back(line);
    return lines;
}

class ArgumentParser {
    struct Command {  // a subcommand from add_subparsers().add_parser(...)
        list<std::string> names;  // its name, then any aliases
        std::optional<std::string> help;
        std::shared_ptr<void> parser;  // an ArgumentParser's state (the class isn't complete here)
    };
    struct State {
        std::string prog;
        std::optional<std::string> usage, description, epilog;
        std::vector<Spec> actions;
        std::vector<Command> commands;
    };
    std::shared_ptr<State> s_;

    std::string metavar(const Spec& a) const {
        if (a.metavar) return *a.metavar;
        if (a.action == "parsers") {
            list<std::string> names;
            for (const auto& c : s_->commands) names.insert(names.end(), c.names.begin(), c.names.end());
            return "{" + join(names, ",") + "}";
        }
        if (!a.choices.empty()) {
            std::string out = "{";
            for (std::size_t i = 0; i < a.choices.size(); ++i) out += (i ? "," : "") + a.choices[i].sd_str();
            return out + "}";
        }
        if (a.positional()) return a.dest;
        std::string upper = a.dest;
        for (auto& c : upper) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        return upper;
    }
    bool takes_values(const Spec& a) const { return a.action == "store" || a.action == "append"; }
    std::string args_text(const Spec& a) const {
        std::string m = metavar(a);
        if (a.action == "parsers") return m + " ...";
        switch (a.nargs) {
            case ONE: return m;
            case OPTIONAL: return "[" + m + "]";
            case ANY: return "[" + m + " ...]";
            case SOME: return m + " [" + m + " ...]";
            default: {
                std::string out;
                for (std::int64_t i = 0; i < a.nargs; ++i) out += (i ? " " : "") + m;
                return out;
            }
        }
    }
    // Usage parts: one per bracketed group or word, as Python splits them for wrapping.
    list<std::string> usage_parts(const Spec& a) const {
        if (a.positional()) {
            std::string text = args_text(a);
            list<std::string> parts;
            std::size_t i = 0;
            while (i < text.size()) {
                if (text[i] == ' ') {
                    ++i;
                    continue;
                }
                std::size_t j = i;
                if (text[i] == '[') {
                    int depth = 0;
                    for (; j < text.size(); ++j) {
                        depth += text[j] == '[' ? 1 : text[j] == ']' ? -1 : 0;
                        if (depth == 0) break;
                    }
                    ++j;
                } else {
                    while (j < text.size() && text[j] != ' ') ++j;
                }
                parts.push_back(text.substr(i, j - i));
                i = j;
            }
            return parts;
        }
        std::string text = a.flags[0] + (takes_values(a) ? " " + args_text(a) : "");
        if (!a.required) return {"[" + text + "]"};
        list<std::string> parts;
        for (std::size_t i = 0, j; i < text.size(); i = j + 1) {
            j = text.find(' ', i);
            if (j == std::string::npos) j = text.size();
            parts.push_back(text.substr(i, j - i));
        }
        return parts;
    }
    std::size_t width() const { return terminal_width() - 2; }

public:
    ArgumentParser(std::optional<std::string> prog = std::nullopt, std::optional<std::string> usage = std::nullopt,
                   std::optional<std::string> description = std::nullopt, std::optional<std::string> epilog = std::nullopt,
                   bool add_help = true)
        : s_(std::make_shared<State>()) {
        s_->prog = prog ? *prog : pathlib::Path(argv().empty() ? std::string("prog") : std::string(argv()[0])).name();
        s_->usage = std::move(usage);
        s_->description = std::move(description);
        s_->epilog = std::move(epilog);
        if (add_help) {
            Spec h;
            h.flags = {"-h", "--help"};
            h.dest = "help";
            h.action = "help";
            h.help = "show this help message and exit";
            s_->actions.push_back(h);
        }
    }

    void add_argument(Spec spec) { s_->actions.push_back(std::move(spec)); }

    std::string format_usage() const {
        std::string prefix = "usage: ";
        if (s_->usage) {  // a custom usage string, with %(prog)s filled in
            std::string u = *s_->usage;
            for (std::size_t at; (at = u.find("%(prog)s")) != std::string::npos;) u.replace(at, 8, s_->prog);
            return prefix + u + "\n";
        }
        list<std::string> opt_parts, pos_parts;
        for (const auto& a : s_->actions) {
            auto parts = usage_parts(a);
            (a.positional() ? pos_parts : opt_parts).insert((a.positional() ? pos_parts : opt_parts).end(), parts.begin(), parts.end());
        }
        std::string all = s_->prog;
        for (auto& p : opt_parts) all += " " + p;
        for (auto& p : pos_parts) all += " " + p;
        std::size_t text_width = width();
        if (prefix.size() + all.size() <= text_width) return prefix + all + "\n";
        // Too long: wrap, optionals and positionals each starting a new line (Python's layout).
        auto get_lines = [&](const list<std::string>& parts, const std::string& indent, const std::string* first_prefix) {
            list<std::string> lines, line;
            std::size_t line_len = first_prefix ? first_prefix->size() - 1 : indent.size() - 1;
            for (const auto& part : parts) {
                if (line_len + 1 + part.size() > text_width && !line.empty()) {
                    lines.push_back(indent + join(line, " "));
                    line.clear();
                    line_len = indent.size() - 1;
                }
                line.push_back(part);
                line_len += part.size() + 1;
            }
            if (!line.empty()) lines.push_back(indent + join(line, " "));
            if (first_prefix && !lines.empty()) lines[0] = lines[0].substr(indent.size());
            return lines;
        };
        list<std::string> lines;
        if (prefix.size() + s_->prog.size() <= 0.75 * text_width) {
            std::string indent(prefix.size() + s_->prog.size() + 1, ' ');
            if (!opt_parts.empty()) {
                list<std::string> first = {s_->prog};
                first.insert(first.end(), opt_parts.begin(), opt_parts.end());
                lines = get_lines(first, indent, &prefix);
                auto more = get_lines(pos_parts, indent, nullptr);
                lines.insert(lines.end(), more.begin(), more.end());
            } else {
                list<std::string> first = {s_->prog};
                first.insert(first.end(), pos_parts.begin(), pos_parts.end());
                lines = get_lines(first, indent, &prefix);
            }
        } else {
            std::string indent(prefix.size(), ' ');
            list<std::string> parts = opt_parts;
            parts.insert(parts.end(), pos_parts.begin(), pos_parts.end());
            lines = get_lines(parts, indent, nullptr);
            lines.insert(lines.begin(), s_->prog);
        }
        return prefix + join(lines, "\n") + "\n";
    }
    static std::string join(const list<std::string>& parts, const std::string& sep) {
        std::string out;
        for (std::size_t i = 0; i < parts.size(); ++i) out += (i ? sep : "") + parts[i];
        return out;
    }

    std::string expand_help(const Spec& a) const {
        std::string text = *a.help, out;
        for (std::size_t i = 0; i < text.size(); ++i) {
            if (text.compare(i, 11, "%(default)s") == 0) {
                out += a.default_.sd_str(), i += 10;
            } else if (text.compare(i, 8, "%(prog)s") == 0) {
                out += s_->prog, i += 7;
            } else if (text.compare(i, 2, "%%") == 0) {
                out += '%', ++i;
            } else {
                out += text[i];
            }
        }
        return out;
    }
    static std::string command_invocation(const Command& c) {  // "remove (rm)"
        std::string out = c.names[0];
        if (c.names.size() > 1) {
            list<std::string> aliases(c.names.begin() + 1, c.names.end());
            out += " (" + join(aliases, ", ") + ")";
        }
        return out;
    }
    std::string invocation(const Spec& a) const {
        if (a.positional()) return metavar(a);
        if (!takes_values(a)) return join(a.flags, ", ");
        list<std::string> parts;
        for (const auto& f : a.flags) parts.push_back(f + " " + args_text(a));
        return join(parts, ", ");
    }
    std::string format_help() const {
        std::size_t text_width = width();
        std::string out = format_usage();
        if (s_->description) {
            out += "\n";
            for (auto& line : wrap(*s_->description, text_width)) out += line + "\n";
        }
        std::size_t max_help_position = std::min<std::size_t>(24, std::max<std::size_t>(text_width - 20, 4));
        std::size_t longest = 0;
        for (const auto& a : s_->actions) longest = std::max(longest, invocation(a).size() + 2);
        for (const auto& c : s_->commands)
            if (c.help) longest = std::max(longest, command_invocation(c).size() + 4);
        std::size_t help_position = std::min(longest + 2, max_help_position);
        std::size_t help_width = std::max<std::size_t>(text_width - help_position, 11);
        auto entry = [&](const std::string& head, const std::optional<std::string>& help, std::size_t indent) {
            std::string pad(indent, ' ');
            std::size_t action_width = help_position - indent - 2;
            if (!help) {
                out += pad + head + "\n";
                return;
            }
            auto lines = wrap(*help, help_width);
            if (head.size() <= action_width) {
                out += pad + head + std::string(action_width - head.size() + 2, ' ') + (lines.empty() ? "" : lines[0]) + "\n";
                for (std::size_t i = 1; i < lines.size(); ++i) out += std::string(help_position, ' ') + lines[i] + "\n";
            } else {
                out += pad + head + "\n";
                for (auto& line : lines) out += std::string(help_position, ' ') + line + "\n";
            }
        };
        auto section = [&](const char* title, bool positional) {
            bool any = false;
            for (const auto& a : s_->actions) any = any || a.positional() == positional;
            if (!any) return;
            out += std::string("\n") + title + ":\n";
            for (const auto& a : s_->actions) {
                if (a.positional() != positional) continue;
                entry(invocation(a), a.help ? std::optional(expand_help(a)) : std::nullopt, 2);
                if (a.action == "parsers") {
                    for (const auto& c : s_->commands)
                        if (c.help) entry(command_invocation(c), c.help, 4);
                }
            }
        };
        section("positional arguments", true);
        section("options", false);
        if (s_->epilog) {
            out += "\n";
            for (auto& line : wrap(*s_->epilog, text_width)) out += line + "\n";
        }
        return out;
    }
    void print_help() const {
        std::string h = format_help();
        std::fwrite(h.data(), 1, h.size(), stdout);
    }
    void print_usage() const {
        std::string u = format_usage();
        std::fwrite(u.data(), 1, u.size(), stdout);
    }
    [[noreturn]] void exit(std::int64_t status = 0, std::optional<std::string> message = std::nullopt) const {
        if (message) std::fwrite(message->data(), 1, message->size(), stderr);
        std::fflush(stdout);
        throw Exit{static_cast<int>(status)};
    }
    [[noreturn]] void error(const std::string& message) const {
        std::fflush(stdout);
        std::string u = format_usage();
        std::fwrite(u.data(), 1, u.size(), stderr);
        exit(2, s_->prog + ": error: " + message + "\n");
    }

private:
    std::string name_of(const Spec& a) const {
        if (a.positional() && a.dest.empty()) return metavar(a);
        if (a.positional()) return a.metavar ? *a.metavar : a.dest;
        return join(a.flags, "/");
    }
    Value convert_one(const Spec& a, const std::string& text) const {
        Value v;
        switch (a.kind) {
            case STR: v = Value(text); break;
            case PATH: v = Value(pathlib::Path(text)); break;
            case INT: {
                std::size_t used = 0;
                bool ok = false;
                try {
                    std::string t = text;
                    while (!t.empty() && std::isspace(static_cast<unsigned char>(t.back()))) t.pop_back();
                    long long n = std::stoll(t, &used, 10);
                    ok = used == t.size();
                    v = Value(static_cast<std::int64_t>(n));
                } catch (...) {
                }
                if (!ok) error("argument " + name_of(a) + ": invalid int value: " + repr_str(text));
                break;
            }
            case FLOAT: {
                std::size_t used = 0;
                bool ok = false;
                try {
                    double d = std::stod(text, &used);
                    ok = used == text.size();
                    v = Value(d);
                } catch (...) {
                }
                if (!ok) error("argument " + name_of(a) + ": invalid float value: " + repr_str(text));
                break;
            }
        }
        if (!a.choices.empty() && std::find(a.choices.begin(), a.choices.end(), v) == a.choices.end()) {
            std::string options;
            for (std::size_t i = 0; i < a.choices.size(); ++i) options += (i ? ", " : "") + a.choices[i].sd_repr();
            error("argument " + name_of(a) + ": invalid choice: " + v.sd_repr() + " (choose from " + options + ")");
        }
        return v;
    }
    Value convert_many(const Spec& a, const list<std::string>& texts) const {
        switch (a.kind) {
            case INT: {
                list<std::int64_t> out;
                for (auto& t : texts) out.push_back(std::get<std::int64_t>(convert_one(a, t).v));
                return Value(out);
            }
            case FLOAT: {
                list<double> out;
                for (auto& t : texts) out.push_back(std::get<double>(convert_one(a, t).v));
                return Value(out);
            }
            default: {
                list<std::string> out;
                for (auto& t : texts) out.push_back(std::get<std::string>(convert_one(a, t).v));
                Value v(out);
                v.path = a.kind == PATH;
                return v;
            }
        }
    }
    // Store what an action received (its strings, already split off).
    void apply(const Spec& a, const list<std::string>& texts, Namespace& ns) const {
        Value& slot = ns.slot(a.dest);
        if (a.action == "store_true") {
            slot = Value(true);
        } else if (a.action == "store_false") {
            slot = Value(false);
        } else if (a.action == "store_const") {
            slot = a.const_;
        } else if (a.action == "count") {
            slot = Value(static_cast<std::int64_t>(slot.is_none() ? 1 : std::get<std::int64_t>(slot.v) + 1));
        } else if (a.action == "help") {
            print_help();
            exit(0);
        } else if (a.action == "version") {
            std::string v = a.version.value_or("") + "\n";
            std::fwrite(v.data(), 1, v.size(), stdout);
            exit(0);
        } else if (a.action == "append") {
            Value item = a.nargs == ONE ? convert_one(a, texts[0]) : convert_many(a, texts);
            if (slot.is_none()) slot = convert_many(a, {});
            std::visit([&](auto& items) {
                using L = std::decay_t<decltype(items)>;
                if constexpr (is_vector<L>::value) {
                    using E = typename L::value_type;
                    if (auto* one = std::get_if<E>(&item.v)) items.push_back(*one);
                }
            }, slot.v);
        } else if (a.nargs == ONE) {
            slot = convert_one(a, texts[0]);
        } else if (a.nargs == OPTIONAL) {
            slot = texts.empty() ? (a.positional() ? a.default_ : a.const_) : convert_one(a, texts[0]);
        } else if (texts.empty() && a.nargs == ANY && a.positional() && !a.default_.is_none()) {
            slot = a.default_;
        } else {
            slot = convert_many(a, texts);
        }
    }
    static std::pair<std::int64_t, std::int64_t> arity(const Spec& a) {  // (min, max) strings; max -1: any
        if (a.action == "parsers") return {1, 1};
        if (a.action != "store" && a.action != "append") return {0, 0};
        switch (a.nargs) {
            case ONE: return {1, 1};
            case OPTIONAL: return {0, 1};
            case ANY: return {0, -1};
            case SOME: return {1, -1};
            default: return {a.nargs, a.nargs};
        }
    }

public:
    Namespace parse_args(std::optional<list<std::string>> args = std::nullopt) const {
        list<std::string> input;
        if (args) {
            input = *args;
        } else {
            for (std::size_t i = 1; i < argv().size(); ++i) input.push_back(argv()[i]);
        }
        Namespace ns;
        list<std::string> extras;
        parse_known(input, ns, extras);
        if (!extras.empty()) error("unrecognized arguments: " + join(extras, " "));
        return ns;
    }

    // Parse `input` into `ns`; strings nothing wanted go to `extras` (a subcommand's
    // leftovers are reported by the top parser, like Python).
    void parse_known(const list<std::string>& input, Namespace& ns, list<std::string>& extras) const {
        for (const auto& a : s_->actions) {  // every dest exists, in order, starting from its default
            if (a.action == "help" || a.action == "version" || a.dest.empty()) continue;
            Value& slot = ns.slot(a.dest);
            if (a.action == "store_true") {
                slot = a.default_.is_none() ? Value(false) : a.default_;
            } else if (a.action == "store_false") {
                slot = a.default_.is_none() ? Value(true) : a.default_;
            } else {
                slot = a.default_;
            }
        }
        // Classify: 'O' for an option, 'A' for an argument ('--' makes the rest arguments).
        std::string pattern;
        list<std::string> strings;
        bool rest_are_args = false;
        for (const auto& s : input) {
            if (!rest_are_args && s == "--") {
                rest_are_args = true;
                continue;
            }
            strings.push_back(s);
            bool option = !rest_are_args && s.size() > 1 && s[0] == '-' &&
                          !(std::isdigit(static_cast<unsigned char>(s[1])) || (s[1] == '.' && s.size() > 2));
            pattern += option ? 'O' : 'A';
        }
        std::vector<const Spec*> positionals;
        for (const auto& a : s_->actions)
            if (a.positional()) positionals.push_back(&a);
        std::size_t next_positional = 0;
        std::vector<const Spec*> seen;

        // Give the run of arguments starting at `start` to as many positionals as can take
        // them (each greedily, leaving the minimum the following ones need), like Python.
        auto consume_positionals = [&](std::size_t start) {
            std::size_t run = 0;
            while (start + run < pattern.size() && pattern[start + run] == 'A') ++run;
            std::size_t count = 0;
            std::int64_t need = 0;
            for (std::size_t i = next_positional; i < positionals.size(); ++i) {
                need += arity(*positionals[i]).first;
                if (need > static_cast<std::int64_t>(run)) break;
                ++count;
            }
            std::size_t pos = start;
            for (std::size_t k = 0; k < count; ++k) {
                const Spec& a = *positionals[next_positional + k];
                std::int64_t later_min = 0;
                for (std::size_t j = k + 1; j < count; ++j) later_min += arity(*positionals[next_positional + j]).first;
                std::int64_t available = static_cast<std::int64_t>(start + run - pos) - later_min;
                auto [lo, hi] = arity(a);
                std::int64_t take = hi < 0 ? available : std::min(hi, available);
                take = std::max(take, lo);
                list<std::string> texts(strings.begin() + pos, strings.begin() + pos + take);
                seen.push_back(&a);
                if (a.action == "parsers") {  // the subcommand takes its name and everything after it
                    list<std::string> rest(strings.begin() + pos + 1, strings.end());
                    run_command(a, strings[pos], rest, ns, extras);
                    next_positional = positionals.size();
                    return strings.size();
                }
                apply(a, texts, ns);
                pos += take;
            }
            next_positional += count;
            return pos;
        };

        auto find_option = [&](const std::string& flag, const std::string& shown) -> const Spec* {
            for (const auto& a : s_->actions)
                for (const auto& f : a.flags)
                    if (f == flag) return &a;
            if (flag.rfind("--", 0) != 0) return nullptr;
            std::vector<std::pair<const Spec*, std::string>> matches;  // unique prefixes of long options
            for (const auto& a : s_->actions)
                for (const auto& f : a.flags)
                    if (f.rfind("--", 0) == 0 && f.rfind(flag, 0) == 0) matches.emplace_back(&a, f);
            if (matches.size() > 1) {
                std::string names;
                for (std::size_t i = 0; i < matches.size(); ++i) names += (i ? ", " : "") + matches[i].second;
                error("ambiguous option: " + shown + " could match " + names);
            }
            return matches.empty() ? nullptr : matches[0].first;
        };

        std::size_t i = 0;
        while (i < strings.size()) {
            if (pattern[i] == 'A') {
                std::size_t after = consume_positionals(i);
                if (after == i) {  // no positional wants it
                    extras.push_back(strings[i]);
                    ++i;
                } else {
                    i = after;
                }
                continue;
            }
            std::string arg = strings[i];
            std::optional<std::string> explicit_value;
            const Spec* action = nullptr;
            auto eq = arg.find('=');
            if (arg.rfind("--", 0) == 0 && eq != std::string::npos) {
                action = find_option(arg.substr(0, eq), arg);
                if (action) explicit_value = arg.substr(eq + 1);
            }
            if (!action) action = find_option(arg, arg);
            if (!action && arg.rfind("--", 0) != 0 && arg.size() > 2) {  // -n5, -vv
                action = find_option(arg.substr(0, 2), arg);
                if (action) explicit_value = arg.substr(2);
            }
            if (!action) {
                extras.push_back(arg);
                ++i;
                continue;
            }
            ++i;
            seen.push_back(action);
            auto [lo, hi] = arity(*action);
            if (hi == 0) {  // a flag; with -vv, the rest are more short flags
                if (explicit_value && arg.rfind("--", 0) == 0)
                    error("argument " + name_of(*action) + ": ignored explicit argument " + repr_str(*explicit_value));
                apply(*action, {}, ns);
                std::string more = explicit_value && arg.rfind("--", 0) != 0 ? *explicit_value : "";
                while (!more.empty()) {
                    const Spec* next = find_option("-" + more.substr(0, 1), arg);
                    if (!next) error("argument " + name_of(*action) + ": ignored explicit argument " + repr_str(more));
                    seen.push_back(next);
                    auto [nlo, nhi] = arity(*next);
                    if (nhi == 0) {
                        apply(*next, {}, ns);
                        more = more.substr(1);
                    } else {
                        apply(*next, {more.substr(1)}, ns);
                        more.clear();
                    }
                }
                continue;
            }
            list<std::string> texts;
            if (explicit_value) {
                texts.push_back(*explicit_value);
            } else {
                while (i < strings.size() && pattern[i] == 'A' && (hi < 0 || static_cast<std::int64_t>(texts.size()) < hi))
                    texts.push_back(strings[i++]);
            }
            if (static_cast<std::int64_t>(texts.size()) < lo) {
                std::string what = action->nargs == ONE ? "expected one argument"
                                   : action->nargs == SOME ? "expected at least one argument"
                                   : "expected " + std::to_string(lo) + " arguments";
                error("argument " + name_of(*action) + ": " + what);
            }
            apply(*action, texts, ns);
        }
        consume_positionals(strings.size());  // positionals that can be empty get their defaults

        list<std::string> missing;
        for (const auto& a : s_->actions) {
            bool given = std::find(seen.begin(), seen.end(), &a) != seen.end();
            bool needed = a.action == "parsers" ? a.required : a.required || (a.positional() && arity(a).first > 0);
            if (!given && needed) missing.push_back(name_of(a));
        }
        if (!missing.empty()) error("the following arguments are required: " + join(missing, ", "));
    }

    // add_subparsers(): subcommands are one more positional argument.
    ArgumentParser add_subparsers(Spec spec) {  // (returns this parser: add_parser() goes on it)
        spec.action = "parsers";
        s_->actions.push_back(std::move(spec));
        return *this;
    }
    ArgumentParser add_parser(const std::string& name, std::optional<std::string> help, list<std::string> aliases,
                              std::optional<std::string> description) {
        ArgumentParser child(s_->prog + " " + name, std::nullopt, std::move(description));
        list<std::string> names{name};
        names.insert(names.end(), aliases.begin(), aliases.end());
        s_->commands.push_back(Command{names, std::move(help), child.s_});
        return child;
    }

private:
    explicit ArgumentParser(std::shared_ptr<State> s) : s_(std::move(s)) {}
    void run_command(const Spec& a, const std::string& name, const list<std::string>& rest, Namespace& ns,
                     list<std::string>& extras) const {
        for (const auto& c : s_->commands) {
            if (std::find(c.names.begin(), c.names.end(), name) == c.names.end()) continue;
            ns.slot(a.dest) = Value(name);
            ArgumentParser(std::static_pointer_cast<State>(c.parser)).parse_known(rest, ns, extras);
            return;
        }
        list<std::string> names;
        for (const auto& c : s_->commands)
            for (const auto& n : c.names) names.push_back(repr_str(n));
        error("argument " + name_of(a) + ": invalid choice: " + repr_str(name) + " (choose from " + join(names, ", ") + ")");
    }

public:
    std::string sd_repr() const {
        return "ArgumentParser(prog=" + repr_str(s_->prog) + ", usage=None, description=" +
               (s_->description ? repr_str(*s_->description) : "None") +
               ", formatter_class=<class 'argparse.HelpFormatter'>, conflict_handler='error', add_help=True)";
    }
};

}  // namespace sd::argparse
