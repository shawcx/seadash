// The `io` module: io.StringIO and io.BytesIO, file objects over a string in memory. They are
// the TextFile and BinaryFile that open() returns (overriding its virtual I/O primitives), so
// print(file=), csv, json.dump, logging and functions that take a TextIO accept them.
// Positions count bytes, as seadash's strings do (Python's StringIO counts characters).
// Like any file object, one isn't shared between threads (threads.py), so there's no lock.
#pragma once

#include <format>
#include <type_traits>

namespace sd::io {

template <class Base>
struct Memory final : Base {
    static constexpr bool text = std::is_same_v<Base, TextFile>;
    std::string buf;
    std::size_t pos = 0;
    bool open = true;
    // StringIO's newline=: None translates \r\n and \r to \n as they're written; "" ends a
    // line at \n, \r or \r\n; "\r" and "\r\n" are what a written \n becomes, and end a line.
    bool universal_write = false, universal_lines = false;
    std::string ending = "\n";

    Memory() : Base(nullptr, "", "") {
        if constexpr (text) this->translate_newlines = false;  // (readline_raw knows the line endings)
    }

    // (StringIO says this without the full stop, BytesIO with it, as in Python.)
    void need_open() const {
        if (!open) raise("ValueError", text ? "I/O operation on closed file" : "I/O operation on closed file.");
    }
    std::string translated(const std::string& s) const {
        if (universal_write) return universal(s);
        if (ending == "\n" || s.find('\n') == std::string::npos) return s;
        std::string out;
        for (char c : s) {
            if (c == '\n') out += ending;
            else out += c;
        }
        return out;
    }

    void check_open() const override {  // (before iterating: Python says it with the full stop)
        if (!open) raise("ValueError", "I/O operation on closed file.");
    }
    std::string read_raw(std::int64_t n) override {
        need_open();
        std::size_t avail = pos < buf.size() ? buf.size() - pos : 0;
        std::size_t k = n < 0 ? avail : std::min(avail, static_cast<std::size_t>(n));
        std::string out = k ? buf.substr(pos, k) : std::string();
        pos += k;
        return out;
    }
    std::string readline_raw() override {
        need_open();
        if (pos >= buf.size()) return "";
        std::size_t end;
        if (universal_lines) {
            std::size_t i = buf.find_first_of("\r\n", pos);
            end = i == std::string::npos ? buf.size() : i + (buf[i] == '\r' && i + 1 < buf.size() && buf[i + 1] == '\n' ? 2 : 1);
        } else {
            std::size_t i = buf.find(ending, pos);
            end = i == std::string::npos ? buf.size() : i + ending.size();
        }
        std::string out = buf.substr(pos, end - pos);
        pos = end;
        return out;
    }
    std::int64_t write_raw(const std::string& s) override {
        need_open();
        bool translate = false;
        if constexpr (text) translate = universal_write || ending != "\n";
        if (translate) write_at(translated(s));
        else write_at(s);
        return static_cast<std::int64_t>(s.size());  // (the length as given, as in Python)
    }
    void write_at(const std::string& data) {
        if (data.empty()) return;  // (not even the padding)
        if (pos > buf.size()) buf.resize(pos, '\0');  // written past the end: the gap is NULs
        buf.replace(pos, std::min(data.size(), buf.size() - pos), data);
        pos += data.size();
    }
    std::int64_t seek(std::int64_t offset, std::int64_t whence = 0) override {
        need_open();
        if constexpr (text) {
            if (whence < 0 || whence > 2)
                raise("ValueError", "Invalid whence (" + std::to_string(whence) + ", should be 0, 1 or 2)");
            if (offset < 0 && whence == 0) raise("ValueError", "Negative seek position " + std::to_string(offset));
            if (whence != 0 && offset != 0) raise("OSError", "Can't do nonzero cur-relative seeks");
        } else {
            if (offset < 0 && whence == 0) raise("ValueError", "negative seek value " + std::to_string(offset));
            if (whence < 0 || whence > 2)
                raise("ValueError", "invalid whence (" + std::to_string(whence) + ", should be 0, 1 or 2)");
        }
        std::int64_t base = whence == 0 ? 0 : whence == 1 ? static_cast<std::int64_t>(pos) : static_cast<std::int64_t>(buf.size());
        pos = static_cast<std::size_t>(std::max<std::int64_t>(0, base + offset));
        return static_cast<std::int64_t>(pos);
    }
    std::int64_t tell() override {
        need_open();
        return static_cast<std::int64_t>(pos);
    }
    std::int64_t truncate(std::optional<std::int64_t> size = std::nullopt) override {  // (the position stays)
        need_open();
        std::int64_t n = size ? *size : static_cast<std::int64_t>(pos);
        if (n < 0) raise("ValueError", std::string(text ? "Negative" : "negative") + " size value " + std::to_string(n));
        if (static_cast<std::size_t>(n) < buf.size()) buf.resize(static_cast<std::size_t>(n));
        return n;
    }
    void close() override {
        open = false;
        buf = std::string();
    }
    void flush() override {
        if constexpr (!text) need_open();  // (a closed StringIO's flush() does nothing)
    }
    bool is_closed() const override { return !open; }
    bool readable() const override { return need_open(), true; }
    bool writable() const override { return need_open(), true; }
    bool seekable() const override { return need_open(), true; }
    bool isatty() const override { return need_open(), false; }
    std::string value() const {
        need_open();
        return buf;
    }
    [[noreturn]] void no_attribute(const char* name) const {
        raise("AttributeError", std::string("'_io.") + (text ? "StringIO" : "BytesIO") + "' object has no attribute '" + name + "'");
    }
    std::string get_name() const override { no_attribute("name"); }
    std::string get_mode() const override { no_attribute("mode"); }
    std::int64_t fileno_() const override { raise("OSError", "fileno"); }  // (Python's io.UnsupportedOperation)
    std::string sd_repr() const override {
        return std::format("<_io.{} object at {}>", text ? "StringIO" : "BytesIO", static_cast<const void*>(this));
    }
};

using StringIO = Memory<TextFile>;
using BytesIO = Memory<BinaryFile>;

inline std::shared_ptr<TextFile> string_io(const std::optional<std::string>& initial_value,
                                           const std::optional<std::string>& newline) {
    if (newline && *newline != "" && *newline != "\n" && *newline != "\r" && *newline != "\r\n")
        raise("ValueError", "illegal newline value: " + repr_str(*newline));
    auto f = std::make_shared<StringIO>();
    f->universal_write = !newline;
    f->universal_lines = newline && newline->empty();
    if (newline && (*newline == "\r" || *newline == "\r\n")) f->ending = *newline;
    if (initial_value) f->buf = f->translated(*initial_value);
    return f;
}

inline std::shared_ptr<BinaryFile> bytes_io(const bytes& initial_bytes) {
    auto f = std::make_shared<BytesIO>();
    f->buf = initial_bytes.data;
    return f;
}

// sio.getvalue(): the checker only allows it on a StringIO / BytesIO.
inline std::string getvalue(const std::shared_ptr<TextFile>& f) { return static_cast<StringIO&>(*f).value(); }
inline bytes getvalue(const std::shared_ptr<BinaryFile>& f) { return bytes(static_cast<BytesIO&>(*f).value()); }

}  // namespace sd::io
