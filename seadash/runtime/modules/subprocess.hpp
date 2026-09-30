// The `subprocess` module: run programs, with Python's API and errors.
//
// Children are started with posix_spawnp. Our pipe ends are close-on-exec, so a
// child started by another thread at the same moment can't inherit them. Reading
// stdout/stderr and writing stdin happen together (poll), so a child producing lots
// of output can't deadlock against us, and a timeout kills the child, like Python.
#pragma once

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <spawn.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <thread>

extern char** environ;

namespace sd::subprocess {

inline constexpr std::int64_t PIPE = -1, STDOUT = -2, DEVNULL = -3;

struct SubprocessError : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "subprocess.SubprocessError"; }
};

struct Args;
std::string shown(const Args& a);  // how Python's messages show a command

struct CalledProcessError : SubprocessError {
    std::int64_t returncode = 0;
    list<std::string> cmd;
    std::optional<std::string> output, stdout, stderr;
    CalledProcessError(std::int64_t code, const Args& args, std::optional<std::string> out, std::optional<std::string> err);
    static std::string describe(std::int64_t code, const std::string& shown_cmd) {
        std::string what = "Command '" + shown_cmd + "'";
        if (code < 0) {
            const char* name = sigabbrev_np(static_cast<int>(-code));
            return what + " died with <Signals.SIG" + (name ? name : std::to_string(-code)) + ": " + std::to_string(-code) + ">.";
        }
        return what + " returned non-zero exit status " + std::to_string(code) + ".";
    }
    std::string sd_type() const override { return "subprocess.CalledProcessError"; }
    std::string sd_repr() const override {
        return "CalledProcessError(" + std::to_string(returncode) + ", " + repr(cmd) + ")";
    }
};

struct TimeoutExpired : SubprocessError {
    list<std::string> cmd;
    double timeout = 0;
    std::optional<std::string> output, stdout, stderr;
    TimeoutExpired(const Args& args, double t, std::optional<std::string> out, std::optional<std::string> err);
    std::string sd_type() const override { return "subprocess.TimeoutExpired"; }
};

// A command: a list of arguments, or one string (a program name, or a shell command).
struct Args {
    list<std::string> list_;
    std::optional<std::string> str_;
    Args(list<std::string> a) : list_(std::move(a)) {}
    Args(std::string s) : str_(std::move(s)) {}
    list<std::string> argv(bool shell) const {
        if (shell) {
            list<std::string> out{"/bin/sh", "-c"};
            if (str_) {
                out.push_back(*str_);
            } else {
                out.insert(out.end(), list_.begin(), list_.end());
            }
            return out;
        }
        return str_ ? list<std::string>{*str_} : list_;
    }
    list<std::string> as_list() const { return str_ ? list<std::string>{*str_} : list_; }
    std::string sd_repr() const { return str_ ? repr(*str_) : repr(list_); }
};

inline std::string shown(const Args& a) { return a.str_ ? *a.str_ : repr(a.list_); }

inline CalledProcessError::CalledProcessError(std::int64_t code, const Args& args, std::optional<std::string> out,
                                              std::optional<std::string> err)
    : SubprocessError(describe(code, shown(args))), returncode(code), cmd(args.as_list()), output(out), stdout(out),
      stderr(std::move(err)) {}

inline TimeoutExpired::TimeoutExpired(const Args& args, double t, std::optional<std::string> out,
                                      std::optional<std::string> err)
    : SubprocessError("Command '" + shown(args) + "' timed out after " + float_repr(t) + " seconds"),
      cmd(args.as_list()), timeout(t), output(out), stdout(out), stderr(std::move(err)) {}

// Where a child's stdin/stdout/stderr goes: inherited, a pipe to us, /dev/null,
// the child's stdout (for stderr), or an open file.
struct Redirect {
    std::int64_t kind = 0;  // 0: inherit, PIPE, STDOUT, DEVNULL, or 1: `fd`
    int fd = -1;
    std::shared_ptr<FileBase> file;  // keeps the file open while in use
    Redirect() = default;
    explicit Redirect(std::int64_t k) : kind(k) {}
    template <class F>
    Redirect(const std::shared_ptr<F>& f) : kind(1), fd(::fileno(f->handle())), file(f) {
        std::fflush(f->handle());  // what we wrote so far comes before the child's output
    }
};

struct Options {
    Redirect in, out, err;
    bool shell = false;
    bool text = false;
    std::optional<std::string> cwd = std::nullopt;
    std::optional<dict<std::string, std::string>> env = std::nullopt;
};

// Python decodes text output with universal newlines: \r\n and \r become \n.
inline std::string universal_newlines(std::string s) {
    std::string out;
    out.reserve(s.size());
    for (std::size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '\r') {
            out += '\n';
            if (i + 1 < s.size() && s[i + 1] == '\n') ++i;
        } else {
            out += s[i];
        }
    }
    return out;
}

struct Child {
    pid_t pid = -1;
    int in = -1, out = -1, err = -1;  // our ends of the pipes
};

inline void close_fd(int& fd) {
    if (fd >= 0) ::close(fd);
    fd = -1;
}

inline Child spawn(const Args& args, const Options& o) {
    list<std::string> argv = args.argv(o.shell);
    if (argv.empty()) raise("IndexError", "list index out of range");
    if (o.cwd) {
        struct stat st;
        if (::stat(o.cwd->c_str(), &st) != 0) raise_os(errno, *o.cwd);
        if (!S_ISDIR(st.st_mode)) raise_os(ENOTDIR, *o.cwd);
    }
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    Child c;
    std::vector<int> to_close;  // the child's ends, closed here once it has started
    auto fail = [&](int e) {
        for (int fd : to_close) ::close(fd);
        close_fd(c.in), close_fd(c.out), close_fd(c.err);
        posix_spawn_file_actions_destroy(&fa);
        raise_os(e, std::nullopt);
    };
    auto setup = [&](const Redirect& r, int target, bool reading, int& ours) {
        if (r.kind == PIPE) {
            int p[2];
            if (::pipe2(p, O_CLOEXEC) != 0) fail(errno);
            int child_end = reading ? p[0] : p[1];
            ours = reading ? p[1] : p[0];
            to_close.push_back(child_end);
            posix_spawn_file_actions_adddup2(&fa, child_end, target);
        } else if (r.kind == DEVNULL) {
            posix_spawn_file_actions_addopen(&fa, target, "/dev/null", reading ? O_RDONLY : O_WRONLY, 0);
        } else if (r.kind == STDOUT) {
            posix_spawn_file_actions_adddup2(&fa, STDOUT_FILENO, target);
        } else if (r.kind == 1) {
            posix_spawn_file_actions_adddup2(&fa, r.fd, target);
        }
    };
    setup(o.in, STDIN_FILENO, true, c.in);
    setup(o.out, STDOUT_FILENO, false, c.out);
    setup(o.err, STDERR_FILENO, false, c.err);
    if (o.cwd) posix_spawn_file_actions_addchdir_np(&fa, o.cwd->c_str());

    std::vector<char*> cargv;
    for (auto& a : argv) cargv.push_back(const_cast<char*>(a.c_str()));
    cargv.push_back(nullptr);
    std::vector<std::string> env_strings;
    std::vector<char*> cenv;
    if (o.env) {
        for (const auto& [k, v] : *o.env) env_strings.push_back(k + "=" + v);
        for (auto& s : env_strings) cenv.push_back(s.data());
        cenv.push_back(nullptr);
    }
    std::fflush(nullptr);  // our buffered output comes first
    int rc = posix_spawnp(&c.pid, cargv[0], &fa, nullptr, cargv.data(), o.env ? cenv.data() : environ);
    posix_spawn_file_actions_destroy(&fa);
    for (int fd : to_close) ::close(fd);
    if (rc != 0) {
        close_fd(c.in), close_fd(c.out), close_fd(c.err);
        raise_os(rc, argv[0]);  // e.g. FileNotFoundError: [Errno 2] No such file or directory: 'nosuch'
    }
    return c;
}

inline std::int64_t decode_status(int status) {
    if (WIFSIGNALED(status)) return -WTERMSIG(status);  // killed by a signal: -signum, like Python
    return WEXITSTATUS(status);
}

using Clock = std::chrono::steady_clock;

inline std::optional<Clock::time_point> deadline_after(std::optional<double> timeout) {
    if (!timeout) return std::nullopt;
    return Clock::now() + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(*timeout));
}

// Wait for the child to exit; nullopt if the deadline passes first.
inline std::optional<std::int64_t> wait_until(pid_t pid, std::optional<Clock::time_point> deadline) {
    int status = 0;
    if (!deadline) {
        while (::waitpid(pid, &status, 0) < 0) {
            if (errno != EINTR) raise_os(errno, std::nullopt);
        }
        return decode_status(status);
    }
    auto pause = std::chrono::microseconds(100);
    while (true) {
        pid_t r = ::waitpid(pid, &status, WNOHANG);
        if (r == pid) return decode_status(status);
        if (r < 0 && errno != EINTR) raise_os(errno, std::nullopt);
        if (Clock::now() >= *deadline) return std::nullopt;
        std::this_thread::sleep_for(pause);
        pause = std::min(pause * 2, std::chrono::microseconds(10000));
    }
}

// Write `input` to fd `in` while reading fds `out` and `err`, until all are done or the
// deadline passes (false). Closes the fds it finishes with.
inline bool exchange(int& in, const std::string& input, int& out, std::string& out_data, int& err, std::string& err_data,
                     std::optional<Clock::time_point> deadline) {
    // A child that exits without reading its input must not kill us with SIGPIPE:
    // block it on this thread while writing, and swallow one that's pending.
    sigset_t pipe_set, old_set;
    sigemptyset(&pipe_set);
    sigaddset(&pipe_set, SIGPIPE);
    pthread_sigmask(SIG_BLOCK, &pipe_set, &old_set);
    bool broken = false;
    std::size_t written = 0;
    if (in >= 0 && input.empty()) close_fd(in);
    bool finished = true;
    while (in >= 0 || out >= 0 || err >= 0) {
        pollfd fds[3];
        int n = 0;
        if (in >= 0) fds[n++] = {in, POLLOUT, 0};
        if (out >= 0) fds[n++] = {out, POLLIN, 0};
        if (err >= 0) fds[n++] = {err, POLLIN, 0};
        int wait_ms = -1;
        if (deadline) {
            auto left = std::chrono::duration_cast<std::chrono::milliseconds>(*deadline - Clock::now()).count();
            if (left <= 0) {
                finished = false;
                break;
            }
            wait_ms = static_cast<int>(std::min<long long>(left, 1000000));
        }
        int rc = ::poll(fds, n, wait_ms);
        if (rc < 0) {
            if (errno == EINTR) continue;
            raise_os(errno, std::nullopt);
        }
        for (int i = 0; i < n; ++i) {
            if (!fds[i].revents) continue;
            if (fds[i].fd == in) {
                ssize_t w = ::write(in, input.data() + written, std::min<std::size_t>(input.size() - written, 65536));
                if (w < 0 && errno == EPIPE) broken = true;
                if (w < 0) {
                    close_fd(in);  // the child stopped reading: that's fine, like Python
                } else if ((written += static_cast<std::size_t>(w)) == input.size()) {
                    close_fd(in);
                }
            } else {
                int& fd = fds[i].fd == out ? out : err;
                std::string& data = fds[i].fd == out ? out_data : err_data;
                char buf[65536];
                ssize_t r = ::read(fd, buf, sizeof buf);
                if (r > 0) {
                    data.append(buf, static_cast<std::size_t>(r));
                } else if (r == 0 || errno != EINTR) {
                    close_fd(fd);
                }
            }
        }
    }
    if (broken) {
        timespec zero{0, 0};
        sigtimedwait(&pipe_set, nullptr, &zero);
    }
    pthread_sigmask(SIG_SETMASK, &old_set, nullptr);
    return finished;
}

struct CompletedProcess {
    Args args{list<std::string>{}};
    std::int64_t returncode = 0;
    std::optional<std::string> out, err;
    bool text = false;

    void check_returncode() const {
        if (returncode != 0) throw Thrown{std::make_shared<CalledProcessError>(returncode, args, out, err)};
    }
    std::string stream_repr(const std::optional<std::string>& s) const { return text ? repr(*s) : repr(bytes(*s)); }
    std::string sd_repr() const {
        std::string r = "CompletedProcess(args=" + args.sd_repr() + ", returncode=" + std::to_string(returncode);
        if (out) r += ", stdout=" + stream_repr(out);
        if (err) r += ", stderr=" + stream_repr(err);
        return r + ")";
    }
};

// Run to completion, collecting what was piped. On timeout, kill the child, then
// raise TimeoutExpired.
inline CompletedProcess run(const Args& args, const Options& o, const std::optional<std::string>& input,
                            std::optional<double> timeout, bool check) {
    Options opts = o;
    if (input) opts.in = Redirect(PIPE);
    Child c = spawn(args, opts);
    std::string out_data, err_data;
    auto deadline = deadline_after(timeout);
    bool done = exchange(c.in, input.value_or(""), c.out, out_data, c.err, err_data, deadline);
    std::optional<std::int64_t> code = done ? wait_until(c.pid, deadline) : std::nullopt;
    auto finish = [&](std::string& s) { return o.text ? universal_newlines(std::move(s)) : s; };
    std::optional<std::string> out, err;
    if (o.out.kind == PIPE) out = finish(out_data);
    if (o.err.kind == PIPE) err = finish(err_data);
    if (!code) {
        ::kill(c.pid, SIGKILL);
        wait_until(c.pid, std::nullopt);
        close_fd(c.in), close_fd(c.out), close_fd(c.err);
        throw Thrown{std::make_shared<TimeoutExpired>(args, *timeout, out, err)};
    }
    CompletedProcess result{args, *code, out, err, o.text};
    if (check) result.check_returncode();
    return result;
}

inline std::string check_output(const Args& args, Options o, const std::optional<std::string>& input,
                                std::optional<double> timeout) {
    o.out = Redirect(PIPE);
    return *run(args, o, input, timeout, true).out;
}

inline std::int64_t call(const Args& args, const Options& o, std::optional<double> timeout) {
    return run(args, o, std::nullopt, timeout, false).returncode;
}

inline std::int64_t check_call(const Args& args, const Options& o, std::optional<double> timeout) {
    return run(args, o, std::nullopt, timeout, true).returncode;
}

inline std::tuple<std::int64_t, std::string> getstatusoutput(const std::string& cmd) {
    Options o;
    o.shell = true;
    o.text = true;
    o.out = Redirect(PIPE);
    o.err = Redirect(STDOUT);
    auto r = run(Args(cmd), o, std::nullopt, std::nullopt, false);
    std::string text = *r.out;
    if (!text.empty() && text.back() == '\n') text.pop_back();
    return {r.returncode, text};
}

inline std::string getoutput(const std::string& cmd) { return std::get<1>(getstatusoutput(cmd)); }

// A running child. Copies share it (like a Python object reference).
class Popen {
    struct State {
        Args args{list<std::string>{}};
        Options options;
        Child child;
        std::optional<std::int64_t> returncode;
        std::shared_ptr<TextFile> text_in, text_out, text_err;
        std::shared_ptr<BinaryFile> bin_in, bin_out, bin_err;
        std::mutex mu;
    };
    std::shared_ptr<State> s_;

    template <class F>
    static std::shared_ptr<F> wrap(int& fd, const char* mode) {
        if (fd < 0) return nullptr;
        std::FILE* f = ::fdopen(fd, mode);
        fd = -1;  // the FILE owns it now
        return std::make_shared<F>(f, "<pipe>", mode);
    }
    static int take_fd(const std::shared_ptr<TextFile>& t, const std::shared_ptr<BinaryFile>& b) {
        FileBase* f = t ? static_cast<FileBase*>(t.get()) : static_cast<FileBase*>(b.get());
        if (!f || !f->fp) return -1;
        std::fflush(f->fp);
        int fd = ::dup(::fileno(f->fp));
        f->close();
        return fd;
    }

public:
    Popen() = default;
    Popen(const Args& args, const Options& o) : s_(std::make_shared<State>()) {
        s_->args = args;
        s_->options = o;
        s_->child = spawn(args, o);
        if (o.text) {
            s_->text_in = wrap<TextFile>(s_->child.in, "w");
            s_->text_out = wrap<TextFile>(s_->child.out, "r");
            s_->text_err = wrap<TextFile>(s_->child.err, "r");
        } else {
            s_->bin_in = wrap<BinaryFile>(s_->child.in, "wb");
            s_->bin_out = wrap<BinaryFile>(s_->child.out, "rb");
            s_->bin_err = wrap<BinaryFile>(s_->child.err, "rb");
        }
    }

    std::shared_ptr<TextFile> stdin_text() const { return s_->text_in; }
    std::shared_ptr<TextFile> stdout_text() const { return s_->text_out; }
    std::shared_ptr<TextFile> stderr_text() const { return s_->text_err; }
    std::shared_ptr<BinaryFile> stdin_binary() const { return s_->bin_in; }
    std::shared_ptr<BinaryFile> stdout_binary() const { return s_->bin_out; }
    std::shared_ptr<BinaryFile> stderr_binary() const { return s_->bin_err; }
    std::int64_t pid() const { return s_->child.pid; }
    std::optional<std::int64_t> returncode() const {
        std::lock_guard lk(s_->mu);
        return s_->returncode;
    }
    list<std::string> args_list() const { return s_->args.as_list(); }
    const Args& args_value() const { return s_->args; }

    std::optional<std::int64_t> poll() {
        std::lock_guard lk(s_->mu);
        if (!s_->returncode) {
            int status = 0;
            if (::waitpid(s_->child.pid, &status, WNOHANG) == s_->child.pid) s_->returncode = decode_status(status);
        }
        return s_->returncode;
    }
    std::int64_t wait(std::optional<double> timeout = std::nullopt) {
        if (auto rc = returncode()) return *rc;
        auto code = wait_until(s_->child.pid, deadline_after(timeout));
        if (!code) throw Thrown{std::make_shared<TimeoutExpired>(s_->args, *timeout, std::nullopt, std::nullopt)};
        std::lock_guard lk(s_->mu);
        s_->returncode = code;
        return *code;
    }
    // Send input, read all output, wait for the child. On timeout the child keeps
    // running (call kill()), like Python.
    std::tuple<std::optional<std::string>, std::optional<std::string>> communicate(
        const std::optional<std::string>& input = std::nullopt, std::optional<double> timeout = std::nullopt) {
        int in = take_fd(s_->text_in, s_->bin_in);
        int out = take_fd(s_->text_out, s_->bin_out);
        int err = take_fd(s_->text_err, s_->bin_err);
        std::string out_data, err_data;
        auto deadline = deadline_after(timeout);
        bool done = exchange(in, input.value_or(""), out, out_data, err, err_data, deadline);
        close_fd(in), close_fd(out), close_fd(err);
        if (!done) throw Thrown{std::make_shared<TimeoutExpired>(s_->args, *timeout, std::nullopt, std::nullopt)};
        wait(timeout ? std::optional<double>(std::max(0.0, std::chrono::duration<double>(*deadline - Clock::now()).count())) : std::nullopt);
        auto finish = [&](std::string& s) { return s_->options.text ? universal_newlines(std::move(s)) : s; };
        std::optional<std::string> o, e;
        if (s_->options.out.kind == PIPE) o = finish(out_data);
        if (s_->options.err.kind == PIPE) e = finish(err_data);
        return {o, e};
    }
    void send_signal(std::int64_t sig) {
        if (!returncode()) ::kill(s_->child.pid, static_cast<int>(sig));
    }
    void terminate() { send_signal(SIGTERM); }
    void kill() { send_signal(SIGKILL); }
    // `with Popen(...) as p:` closes the pipes and waits for the child at the end.
    void sd_exit() {
        for (FileBase* f : {static_cast<FileBase*>(s_->text_out.get()), static_cast<FileBase*>(s_->text_err.get()),
                            static_cast<FileBase*>(s_->bin_out.get()), static_cast<FileBase*>(s_->bin_err.get()),
                            static_cast<FileBase*>(s_->text_in.get()), static_cast<FileBase*>(s_->bin_in.get())}) {
            if (f) f->close();
        }
        wait();
    }
    std::string sd_repr() const {
        auto rc = returncode();
        return "<Popen: returncode: " + (rc ? std::to_string(*rc) : std::string("None")) + " args: " + s_->args.sd_repr() + ">";
    }
};

// A captured stream as the type the checker gave it: str, bytes, or their optionals.
template <class T>
T as(const std::optional<std::string>& s) {
    if constexpr (std::is_same_v<T, std::string>) {
        return s.value_or("");
    } else if constexpr (std::is_same_v<T, bytes>) {
        return bytes(s.value_or(""));
    } else if constexpr (std::is_same_v<T, std::optional<std::string>>) {
        return s;
    } else {
        return s ? T(bytes(*s)) : T(std::nullopt);
    }
}

}  // namespace sd::subprocess
