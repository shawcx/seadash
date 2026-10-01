// The `logging` module: loggers in a dotted hierarchy, handlers (stream, file, null),
// formatters with Python's %(field)s patterns, basicConfig, and %-style lazy messages
// (`log.info("x=%s", x)`: the text is only built when the record is emitted). One lock
// makes it safe to log from several threads.
#pragma once

#include <sys/time.h>
#include <unistd.h>

#include <functional>
#include <map>
#include <mutex>
#include <thread>

namespace sd::logging {

inline constexpr std::int64_t NOTSET = 0, DEBUG = 10, INFO = 20, WARNING = 30, ERROR = 40, CRITICAL = 50;
inline const std::string BASIC_FORMAT = "%(levelname)s:%(name)s:%(message)s";

inline std::int64_t& disabled_below() {  // logging.disable(level): this level and below are off
    static std::int64_t level = -1;
    return level;
}

inline std::recursive_mutex& lock() {
    static std::recursive_mutex m;
    return m;
}

inline std::string level_name(std::int64_t level) {
    switch (level) {
        case CRITICAL: return "CRITICAL";
        case ERROR: return "ERROR";
        case WARNING: return "WARNING";
        case INFO: return "INFO";
        case DEBUG: return "DEBUG";
        case NOTSET: return "NOTSET";
    }
    return "Level " + std::to_string(level);
}
inline std::int64_t level_of(std::int64_t level) { return level; }
inline std::int64_t level_of(const std::string& name) {
    for (std::int64_t l : {CRITICAL, ERROR, WARNING, INFO, DEBUG, NOTSET})
        if (level_name(l) == name) return l;
    if (name == "WARN") return WARNING;
    if (name == "FATAL") return CRITICAL;
    raise("ValueError", "Unknown level: " + repr_str(name));
}

// ---- %-formatting (Python's `format % args`) --------------------------------------------

struct Arg {  // one value, as the formatter may need it
    std::string s, r;
    std::optional<std::int64_t> i;
    std::optional<double> f;
};
template <class T>
Arg arg(const T& x) {
    if constexpr (is_optional<T>::value) {
        if (x) return arg(*x);
        return Arg{"None", "None", std::nullopt, std::nullopt};
    } else {
        Arg a{str(x), repr(x), std::nullopt, std::nullopt};
        if constexpr (std::is_same_v<T, bool> || std::is_same_v<T, Bool> || std::is_integral_v<T>) {
            a.i = static_cast<std::int64_t>(x);
            a.f = static_cast<double>(x);
        } else if constexpr (std::is_floating_point_v<T>) {
            a.f = x;
        }
        return a;
    }
}

struct FormatError {
    std::string kind, message;
};

inline std::string percent_format(const std::string& fmt, const std::vector<Arg>& args,
                                  const std::function<const Arg*(const std::string&)>& named) {
    std::string out;
    std::size_t next = 0;
    auto take = [&]() -> const Arg& {
        if (next >= args.size()) throw FormatError{"TypeError", "not enough arguments for format string"};
        return args[next++];
    };
    for (std::size_t i = 0; i < fmt.size(); ++i) {
        if (fmt[i] != '%') {
            out += fmt[i];
            continue;
        }
        std::size_t start = i++;
        if (i >= fmt.size()) throw FormatError{"ValueError", "incomplete format"};
        const Arg* value = nullptr;
        if (fmt[i] == '(') {
            std::size_t close = fmt.find(')', i);
            if (close == std::string::npos) throw FormatError{"ValueError", "incomplete format key"};
            std::string key = fmt.substr(i + 1, close - i - 1);
            value = named ? named(key) : nullptr;
            if (!value) throw FormatError{"KeyError", repr_str(key)};
            i = close + 1;
        }
        std::string flags;
        while (i < fmt.size() && std::string("#0- +").find(fmt[i]) != std::string::npos) flags += fmt[i++];
        auto number = [&]() -> std::optional<std::int64_t> {
            if (i < fmt.size() && fmt[i] == '*') {
                ++i;
                const Arg& a = take();
                if (!a.i) throw FormatError{"TypeError", "* wants int"};
                return *a.i;
            }
            std::size_t j = i;
            while (i < fmt.size() && std::isdigit(static_cast<unsigned char>(fmt[i]))) ++i;
            if (i == j) return std::nullopt;
            return std::stoll(fmt.substr(j, i - j));
        };
        std::optional<std::int64_t> width = number(), precision;
        if (i < fmt.size() && fmt[i] == '.') {
            ++i;
            precision = number().value_or(0);
        }
        while (i < fmt.size() && std::string("hlL").find(fmt[i]) != std::string::npos) ++i;
        if (i >= fmt.size()) throw FormatError{"ValueError", "incomplete format"};
        char conv = fmt[i];
        if (conv == '%' && fmt[i - 1] == '%' && i == start + 1) {
            out += '%';
            continue;
        }
        const Arg& a = value ? *value : take();
        bool left = flags.find('-') != std::string::npos, zero = flags.find('0') != std::string::npos && !left;
        auto pad = [&](std::string text, bool numeric) {
            std::size_t len = 0;
            for (unsigned char c : text) len += (c & 0xC0) != 0x80;
            if (!width || static_cast<std::int64_t>(len) >= *width) return text;
            std::size_t fill = static_cast<std::size_t>(*width) - len;
            if (left) return text + std::string(fill, ' ');
            if (numeric && zero) {
                std::size_t sign = (!text.empty() && (text[0] == '-' || text[0] == '+' || text[0] == ' ')) ? 1 : 0;
                if (text.compare(sign, 2, "0x") == 0 || text.compare(sign, 2, "0X") == 0 || text.compare(sign, 2, "0o") == 0) sign += 2;
                return text.substr(0, sign) + std::string(fill, '0') + text.substr(sign);
            }
            return std::string(fill, ' ') + text;
        };
        auto integer = [&]() -> std::int64_t {
            if (a.i) return *a.i;
            if (a.f) return static_cast<std::int64_t>(*a.f);
            throw FormatError{"TypeError", std::string("%") + conv + " format: a real number is required, not str"};
        };
        auto sign_of = [&](bool negative) {
            return negative ? std::string("-") : flags.find('+') != std::string::npos ? std::string("+")
                                               : flags.find(' ') != std::string::npos ? std::string(" ") : std::string();
        };
        switch (conv) {
            case 's': case 'r': case 'a': {
                std::string text = conv == 's' ? a.s : conv == 'r' ? a.r : ascii(a.r);
                if (precision) text = text.substr(0, std::min<std::size_t>(text.size(), *precision));
                out += pad(text, false);
                break;
            }
            case 'd': case 'i': case 'u': {
                std::int64_t v = integer();
                std::string digits = v < 0 ? std::to_string(v).substr(1) : std::to_string(v);
                out += pad(sign_of(v < 0) + digits, true);
                break;
            }
            case 'x': case 'X': case 'o': {
                std::int64_t v = integer();
                char buf[64];
                std::snprintf(buf, sizeof buf, conv == 'o' ? "%llo" : conv == 'x' ? "%llx" : "%llX",
                              static_cast<unsigned long long>(v < 0 ? -v : v));
                std::string prefix = flags.find('#') != std::string::npos ? (conv == 'o' ? "0o" : conv == 'x' ? "0x" : "0X") : "";
                out += pad(sign_of(v < 0) + prefix + buf, true);
                break;
            }
            case 'e': case 'E': case 'f': case 'F': case 'g': case 'G': {
                if (!a.f) throw FormatError{"TypeError", "must be real number, not str"};
                std::string spec = "%" + flags + (width ? std::to_string(*width) : "") + "." +
                                   std::to_string(precision.value_or(6)) + conv;
                char buf[512];
                std::snprintf(buf, sizeof buf, spec.c_str(), *a.f);
                out += buf;
                break;
            }
            case 'c': {
                if (a.i) {
                    std::string ch;
                    std::uint32_t cp = static_cast<std::uint32_t>(*a.i);
                    if (cp < 0x80) ch += static_cast<char>(cp);
                    else if (cp < 0x800) ch += static_cast<char>(0xC0 | cp >> 6), ch += static_cast<char>(0x80 | (cp & 0x3F));
                    else if (cp < 0x10000) ch += static_cast<char>(0xE0 | cp >> 12), ch += static_cast<char>(0x80 | ((cp >> 6) & 0x3F)), ch += static_cast<char>(0x80 | (cp & 0x3F));
                    else ch += static_cast<char>(0xF0 | cp >> 18), ch += static_cast<char>(0x80 | ((cp >> 12) & 0x3F)), ch += static_cast<char>(0x80 | ((cp >> 6) & 0x3F)), ch += static_cast<char>(0x80 | (cp & 0x3F));
                    out += pad(ch, false);
                } else {
                    out += pad(a.s, false);
                }
                break;
            }
            default: {
                char buf[96];
                std::snprintf(buf, sizeof buf, "unsupported format character '%c' (0x%x) at index %zu", conv,
                              static_cast<unsigned char>(conv), i);
                throw FormatError{"ValueError", buf};
            }
        }
    }
    if (next < args.size() && !named) throw FormatError{"TypeError", "not all arguments converted during string formatting"};
    return out;
}

// ---- records, formatters, handlers --------------------------------------------------

struct Site {  // where the logging call is in the source (filled in by the compiler)
    const char* pathname;
    std::int64_t lineno;
    const char* funcName;
};

inline double start_time() {
    static const double start = [] {
        timeval tv;
        ::gettimeofday(&tv, nullptr);
        return tv.tv_sec + tv.tv_usec / 1e6;
    }();
    return start;
}

struct Record {
    std::string name, message, levelname, pathname, filename, module, funcName, threadName, exc_text;
    std::int64_t levelno = 0, lineno = 0, thread = 0, process = 0;
    double created = 0, msecs = 0, relativeCreated = 0;
    std::string asctime;
};

class Formatter {
    struct State {
        std::string fmt;
        std::optional<std::string> datefmt;
    };
    std::shared_ptr<State> s_;

public:
    Formatter(std::optional<std::string> fmt = std::nullopt, std::optional<std::string> datefmt = std::nullopt,
              const std::string& style = "%")
        : s_(std::make_shared<State>()) {
        if (style != "%") raise("ValueError", "seadash's logging supports style='%' formats (use %(name)s fields)");
        s_->fmt = fmt.value_or("%(message)s");
        s_->datefmt = std::move(datefmt);
    }
    std::string format_time(const Record& r) const {
        auto secs = static_cast<std::time_t>(r.created);
        std::tm tm{};
        ::localtime_r(&secs, &tm);
        char buf[256];
        std::strftime(buf, sizeof buf, s_->datefmt ? s_->datefmt->c_str() : "%Y-%m-%d %H:%M:%S", &tm);
        std::string out = buf;
        if (!s_->datefmt) {
            char ms[8];
            std::snprintf(ms, sizeof ms, ",%03d", static_cast<int>(r.msecs));
            out += ms;
        }
        return out;
    }
    std::string format(Record r) const {
        if (s_->fmt.find("%(asctime)") != std::string::npos) r.asctime = format_time(r);
        std::map<std::string, Arg> fields = {
            {"name", arg(r.name)}, {"message", arg(r.message)}, {"levelname", arg(r.levelname)},
            {"levelno", arg(r.levelno)}, {"pathname", arg(r.pathname)}, {"filename", arg(r.filename)},
            {"module", arg(r.module)}, {"funcName", arg(r.funcName)}, {"lineno", arg(r.lineno)},
            {"threadName", arg(r.threadName)}, {"thread", arg(r.thread)}, {"process", arg(r.process)},
            {"processName", arg(std::string("MainProcess"))}, {"created", arg(r.created)},
            {"msecs", arg(r.msecs)}, {"relativeCreated", arg(r.relativeCreated)}, {"asctime", arg(r.asctime)},
        };
        std::string out = percent_format(s_->fmt, {}, [&](const std::string& key) -> const Arg* {
            auto it = fields.find(key);
            return it == fields.end() ? nullptr : &it->second;
        });
        if (!r.exc_text.empty()) out += "\n" + r.exc_text;
        return out;
    }
};

class Handler {
    struct State {
        std::int64_t level = NOTSET;
        std::optional<Formatter> formatter;
        std::shared_ptr<TextFile> stream;  // null: a NullHandler
        std::string kind = "StreamHandler";
        std::string path;
    };
    std::shared_ptr<State> s_;

public:
    Handler() : s_(std::make_shared<State>()) { s_->kind = "NullHandler"; }
    static Handler stream(std::optional<std::shared_ptr<TextFile>> f) {
        Handler h;
        h.s_->kind = "StreamHandler";
        h.s_->stream = f && *f ? *f : std_stream(2);
        return h;
    }
    static Handler file(const std::string& filename, const std::string& mode, std::optional<std::string>) {
        Handler h;
        h.s_->kind = "FileHandler";
        h.s_->stream = open_text(filename, mode);
        h.s_->path = filename;
        return h;
    }
    void setLevel(std::int64_t level) { s_->level = level; }
    void setFormatter(Formatter f) { s_->formatter = std::move(f); }
    std::int64_t level() const { return s_->level; }
    bool same(const Handler& o) const { return s_ == o.s_; }
    void emit(const Record& r) const {
        if (!s_->stream || r.levelno < s_->level) return;
        std::string text = (s_->formatter ? *s_->formatter : Formatter()).format(r) + "\n";
        std::fwrite(text.data(), 1, text.size(), s_->stream->handle());
        std::fflush(s_->stream->handle());
    }
    void flush() const {
        if (s_->stream) std::fflush(s_->stream->handle());
    }
    void close() {
        if (s_->stream && s_->kind == "FileHandler") s_->stream->close();
    }
    std::string sd_repr() const {
        return "<" + s_->kind + " " + (s_->path.empty() ? (s_->stream ? s_->stream->path : std::string()) : s_->path) +
               " (" + level_name(s_->level) + ")>";
    }
};

// ---- loggers ----------------------------------------------------------------------

class Logger {
public:
    struct State {
        std::string name;
        std::int64_t level = NOTSET;
        std::vector<Handler> handlers;
        bool propagate = true;
        State* parent = nullptr;
    };

private:
    State* s_ = nullptr;  // loggers live for the whole program, like Python's

    static std::map<std::string, std::unique_ptr<State>>& registry() {
        static std::map<std::string, std::unique_ptr<State>> r;
        return r;
    }
    static State* root_state() {
        static State* root = [] {
            auto s = std::make_unique<State>();
            s->name = "root";
            s->level = WARNING;
            State* p = s.get();
            registry()[""] = std::move(s);
            return p;
        }();
        return root;
    }
    static State* nearest_parent(const std::string& name) {
        for (auto dot = name.rfind('.'); dot != std::string::npos; dot = name.rfind('.', dot - 1)) {
            auto it = registry().find(name.substr(0, dot));
            if (it != registry().end()) return it->second.get();
            if (dot == 0) break;
        }
        return root_state();
    }

public:
    Logger() : s_(root_state()) {}
    explicit Logger(State* s) : s_(s) {}
    static Logger get(std::optional<std::string> name) {
        std::lock_guard lk(lock());
        if (!name || name->empty() || *name == "root") return Logger(root_state());
        auto& reg = registry();
        auto it = reg.find(*name);
        if (it != reg.end()) return Logger(it->second.get());
        auto s = std::make_unique<State>();
        s->name = *name;
        s->parent = nearest_parent(*name);
        State* p = s.get();
        std::string prefix = *name + ".";
        for (auto& [other, st] : reg)  // existing children now hang under this logger
            if (other.rfind(prefix, 0) == 0 && st->parent && (st->parent == p->parent)) st->parent = p;
        reg[*name] = std::move(s);
        return Logger(p);
    }
    static Logger root() { return Logger(root_state()); }

    std::string name() const { return s_->name; }
    std::int64_t level() const { return s_->level; }
    bool propagate() const { return s_->propagate; }
    std::optional<Logger> parent() const { return s_->parent ? std::optional(Logger(s_->parent)) : std::nullopt; }
    list<Handler> handlers() const {
        std::lock_guard lk(lock());
        return list<Handler>(s_->handlers.begin(), s_->handlers.end());
    }
    template <class L>
    void setLevel(const L& level) {
        std::lock_guard lk(lock());
        s_->level = level_of(level);
    }
    void addHandler(Handler h) {
        std::lock_guard lk(lock());
        for (auto& existing : s_->handlers)
            if (existing.same(h)) return;
        s_->handlers.push_back(std::move(h));
    }
    void removeHandler(const Handler& h) {
        std::lock_guard lk(lock());
        std::erase_if(s_->handlers, [&](const Handler& x) { return x.same(h); });
    }
    bool hasHandlers() const {
        std::lock_guard lk(lock());
        for (State* s = s_; s; s = s->propagate ? s->parent : nullptr)
            if (!s->handlers.empty()) return true;
        return false;
    }
    std::int64_t getEffectiveLevel() const {
        std::lock_guard lk(lock());
        for (State* s = s_; s; s = s->parent)
            if (s->level != NOTSET) return s->level;
        return NOTSET;
    }
    bool isEnabledFor(std::int64_t level) const { return level > disabled_below() && level >= getEffectiveLevel(); }

    // Log: nothing is formatted unless the level is enabled.
    template <class M, class... A>
    void log(const Site& site, std::int64_t level, bool exc_info, const M& msg, const A&... args) const {
        std::lock_guard lk(lock());
        if (!isEnabledFor(level)) return;
        Record r;
        r.name = s_->name;
        r.levelno = level;
        r.levelname = level_name(level);
        r.pathname = site.pathname;
        std::string path = site.pathname;
        r.filename = path.substr(path.rfind('/') == std::string::npos ? 0 : path.rfind('/') + 1);
        r.module = r.filename.substr(0, r.filename.rfind('.'));
        r.lineno = site.lineno;
        r.funcName = site.funcName;
        r.threadName = thread_name();
        r.thread = static_cast<std::int64_t>(std::hash<std::thread::id>()(std::this_thread::get_id()) & 0x7fffffffffff);
        r.process = ::getpid();
        timeval tv;
        ::gettimeofday(&tv, nullptr);
        r.created = tv.tv_sec + tv.tv_usec / 1e6;
        r.msecs = static_cast<double>(tv.tv_usec / 1000);
        r.relativeCreated = (r.created - start_time()) * 1000;
        std::string text = str(msg);
        if constexpr (sizeof...(A) > 0) {
            try {
                text = percent_format(text, std::vector<Arg>{arg(args)...}, nullptr);
            } catch (const FormatError& e) {  // like Python: report it, and carry on
                std::vector<Arg> shown{arg(args)...};
                std::string list;
                for (std::size_t i = 0; i < shown.size(); ++i) list += (i ? ", " : "") + shown[i].r;
                std::string report = "--- Logging error ---\n" + e.kind + ": " + e.message + "\nMessage: " + repr(text) +
                                     "\nArguments: (" + list + (shown.size() == 1 ? ",)" : ")") + "\n";
                std::fwrite(report.data(), 1, report.size(), stderr);
                return;
            }
        }
        r.message = text;
        if (exc_info) {
            if (auto current = std::current_exception()) {
                try {
                    std::rethrow_exception(current);
                } catch (const Thrown& t) {
                    r.exc_text = t.exc->sd_type() + (t.exc->sd_str().empty() ? "" : ": " + t.exc->sd_str());
                } catch (...) {
                }
            } else {
                r.exc_text = "NoneType: None";
            }
        }
        handle(r);
    }
    void handle(const Record& r) const {
        bool handled = false;
        for (State* s = s_; s; s = s->propagate ? s->parent : nullptr) {
            for (const auto& h : s->handlers) {
                handled = true;
                h.emit(r);
            }
        }
        if (!handled && r.levelno >= WARNING) {  // Python's last resort: the bare message on stderr
            std::string text = r.message + (r.exc_text.empty() ? "" : "\n" + r.exc_text) + "\n";
            std::fwrite(text.data(), 1, text.size(), stderr);
        }
    }
    std::string sd_repr() const {
        return "<" + std::string(s_->parent ? "Logger" : "RootLogger") + " " + s_->name + " (" +
               level_name(getEffectiveLevel()) + ")>";
    }
};

inline Logger getLogger(std::optional<std::string> name = std::nullopt) { return Logger::get(name); }

template <class L>
void basicConfig(std::optional<L> level, std::optional<std::string> format, std::optional<std::string> datefmt,
                 const std::string& style, std::optional<std::string> filename, const std::string& filemode,
                 std::shared_ptr<TextFile> stream, std::optional<list<Handler>> handlers, bool force,
                 std::optional<std::string> encoding) {
    std::lock_guard lk(lock());
    Logger root = Logger::root();
    if (root.hasHandlers()) {
        if (!force) return;
        for (auto& h : root.handlers()) {
            root.removeHandler(h);
            h.close();
        }
    }
    Formatter f(format ? *format : BASIC_FORMAT, datefmt, style);
    list<Handler> added;
    if (handlers) {
        added = *handlers;
    } else if (filename) {
        added.push_back(Handler::file(*filename, filemode, encoding));
    } else {
        added.push_back(Handler::stream(stream));
    }
    for (auto& h : added) {
        h.setFormatter(f);
        root.addHandler(h);
    }
    if (level) root.setLevel(*level);
}
inline void basicConfig() {
    basicConfig<std::int64_t>(std::nullopt, std::nullopt, std::nullopt, "%", std::nullopt, "a", nullptr, std::nullopt,
                              false, std::nullopt);
}

// logging.info(...) and friends: on the root logger, setting it up first if needed.
template <class M, class... A>
void root_log(const Site& site, std::int64_t level, bool exc_info, const M& msg, const A&... args) {
    {
        std::lock_guard lk(lock());
        if (Logger::root().handlers().empty()) basicConfig();
    }
    Logger::root().log(site, level, exc_info, msg, args...);
}

inline void disable(std::int64_t level = CRITICAL) {
    std::lock_guard lk(lock());
    disabled_below() = level;
}

}  // namespace sd::logging
