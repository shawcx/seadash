// The `tempfile` module: temporary directories that clean themselves up.
#pragma once

#include <random>

#include "shutil.hpp"

namespace sd::tempfile {

inline std::string gettempdir() {
    for (const char* var : {"TMPDIR", "TEMP", "TMP"}) {
        if (const char* v = std::getenv(var); v && *v && pathlib::Path(std::string(v)).is_dir()) return v;
    }
    return "/tmp";
}

// A new, private (0700) directory named prefix + 8 random characters + suffix.
inline std::string mkdtemp(const std::string& suffix = "", std::optional<std::string> prefix = std::nullopt,
                           std::optional<std::string> dir = std::nullopt) {
    static const char chars[] = "abcdefghijklmnopqrstuvwxyz0123456789_";
    thread_local std::mt19937_64 rng{std::random_device{}()};
    std::string base = dir ? *dir : gettempdir();
    for (int attempt = 0; attempt < 10000; ++attempt) {
        std::string name = prefix.value_or("tmp");
        for (int i = 0; i < 8; ++i) name += chars[rng() % (sizeof chars - 1)];
        std::string path = (pathlib::Path(base) / (name + suffix)).str();
        if (::mkdir(path.c_str(), 0700) == 0) return pathlib::Path(path).absolute().str();
        if (errno != EEXIST) raise_os(errno, path);
    }
    raise_os(EEXIST, base);
}

// `with tempfile.TemporaryDirectory() as tmp:` -- removed (with its contents) at the end,
// or when the last copy of the object goes away.
class TemporaryDirectory {
    struct State {
        std::string name;
        bool removed = false;
        ~State() {
            if (!removed) shutil::remove_tree(pathlib::Path(name), true);
        }
    };
    std::shared_ptr<State> s_;

public:
    TemporaryDirectory(const std::string& suffix = "", std::optional<std::string> prefix = std::nullopt,
                       std::optional<std::string> dir = std::nullopt)
        : s_(std::make_shared<State>()) {
        s_->name = mkdtemp(suffix, prefix, dir);
    }
    std::string name() const { return s_->name; }
    void cleanup() {
        if (!s_->removed) shutil::remove_tree(pathlib::Path(s_->name), true);
        s_->removed = true;
    }
    std::string sd_repr() const { return "<TemporaryDirectory " + repr_str(s_->name) + ">"; }
};

}  // namespace sd::tempfile
