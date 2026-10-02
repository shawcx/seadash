// The `signal` module: handlers written in seadash, the Signals enum, alarm, raise_signal...
//
// Python runs a handler on the main thread, between bytecodes. seadash has no interpreter loop
// to stop at, and a C signal handler may only do async-signal-safe things, so the C handler
// (`deliver`) just counts the signal and writes a byte to a pipe. A dispatcher thread, started
// with the first handler, reads the pipe and runs the program's handlers, one at a time and in
// signal-number order, like Python. A handler therefore runs alongside the rest of the program:
// the checker treats it as thread code (threads.py), so it only shares thread-safe state.
//
// raise_signal() and os.kill() of this process wait until the handler has run, as Python's do
// (they run pending handlers before returning), so a handler's effects are seen right after.
// An exception or sys.exit() escaping a handler ends the program: in Python it would be raised
// in the main thread, which can't be done to a running thread.
#pragma once

#include <fcntl.h>
#include <signal.h>
#include <string.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>

#include "enum.hpp"
#include "os.hpp"

namespace sd::signal {

// types.FrameType. Python passes a handler the frame it interrupted; seadash has none, so a
// handler's frame is always None.
struct Frame {
    std::string sd_repr() const { return "<frame>"; }
    bool operator==(const Frame&) const = default;
};

using Function = std::function<void(std::int64_t, std::optional<Frame>)>;

// What signal.signal() takes and gives: SIG_DFL, SIG_IGN, default_int_handler, a function, or
// None (a handler that wasn't set from seadash, as getsignal() reports it).
class Handler {
public:
    enum class Kind { None, Default, Ignore, DefaultInt, Function };

    Handler() = default;
    explicit Handler(Kind kind) : kind_(kind) {}
    template <class F>
    static Handler function(F f) {
        Handler h(Kind::Function);
        h.fn_ = std::make_shared<const Function>(std::move(f));
        return h;
    }

    Kind kind() const { return kind_; }
    const std::shared_ptr<const Function>& fn() const { return fn_; }

    // `is`: SIG_DFL is SIG_DFL; a function is the same handler if it came from the same signal() call.
    const void* identity() const {
        static const char tags[5] = {};
        return kind_ == Kind::Function ? static_cast<const void*>(fn_.get()) : &tags[static_cast<int>(kind_)];
    }
    bool operator==(const Handler& o) const { return kind_ == o.kind_ && fn_ == o.fn_; }

    // SIG_DFL and SIG_IGN are members of Python's IntEnum signal.Handlers.
    std::string sd_repr() const {
        switch (kind_) {
            case Kind::None: return "None";
            case Kind::Default: return "<Handlers.SIG_DFL: 0>";
            case Kind::Ignore: return "<Handlers.SIG_IGN: 1>";
            case Kind::DefaultInt: return "<built-in function default_int_handler>";
            case Kind::Function: break;
        }
        return "<function>";
    }
    std::string sd_str() const {
        if (kind_ == Kind::Default) return "0";
        if (kind_ == Kind::Ignore) return "1";
        return sd_repr();
    }

private:
    Kind kind_ = Kind::Default;
    std::shared_ptr<const Function> fn_;
};

inline Handler default_action() { return Handler(Handler::Kind::Default); }
inline Handler ignore() { return Handler(Handler::Kind::Ignore); }
inline Handler default_int_handler() { return Handler(Handler::Kind::DefaultInt); }

// ---- signal.Signals: an IntEnum like the ones codegen makes (see enum.hpp), but its values
// come from <signal.h>, so they're right on each platform. Keep in step with SIGNAL_NAMES in
// builtins.py: a member's index is its position there.

struct Signals {
    std::int64_t sd_index = 0;

    static const sd::enums::Table<std::int64_t>& sd_table() {
        static const sd::enums::Table<std::int64_t> t{
            "Signals",
            {"SIGHUP", "SIGINT", "SIGQUIT", "SIGILL", "SIGTRAP", "SIGABRT", "SIGBUS", "SIGFPE", "SIGKILL", "SIGUSR1",
             "SIGSEGV", "SIGUSR2", "SIGPIPE", "SIGALRM", "SIGTERM", "SIGCHLD", "SIGCONT", "SIGSTOP", "SIGTSTP",
             "SIGTTIN", "SIGTTOU", "SIGURG", "SIGXCPU", "SIGXFSZ", "SIGVTALRM", "SIGPROF", "SIGWINCH", "SIGIO",
             "SIGSYS"},
            {SIGHUP, SIGINT, SIGQUIT, SIGILL, SIGTRAP, SIGABRT, SIGBUS, SIGFPE, SIGKILL, SIGUSR1, SIGSEGV, SIGUSR2,
             SIGPIPE, SIGALRM, SIGTERM, SIGCHLD, SIGCONT, SIGSTOP, SIGTSTP, SIGTTIN, SIGTTOU, SIGURG, SIGXCPU,
             SIGXFSZ, SIGVTALRM, SIGPROF, SIGWINCH, SIGIO, SIGSYS},
            {},
            {},
            0,
            false,
        };
        return t;
    }
    static const std::vector<std::pair<std::string, std::int64_t>>& names() {
        static const auto by_name = [] {
            std::vector<std::pair<std::string, std::int64_t>> out;
            const auto& t = sd_table();
            for (std::size_t i = 0; i < t.names.size(); ++i) out.emplace_back(t.names[i], static_cast<std::int64_t>(i));
            out.emplace_back("SIGIOT", 5);  // another name for SIGABRT
            return out;
        }();
        return by_name;
    }

    static Signals sd_at(std::int64_t i) {
        Signals x;
        x.sd_index = i;
        return x;
    }
    static Signals sd_by_name(const std::string& n) {
        for (const auto& [name, i] : names())
            if (name == n) return sd_at(i);
        raise("KeyError", repr(n));
    }
    const std::string& sd_name() const { return sd_table().names[sd_index]; }
    const std::int64_t& sd_value() const { return sd_table().values[sd_index]; }
    static Signals sd_lookup(const std::int64_t& v) { return sd_at(sd::enums::lookup(sd_table(), v)); }
    // In order of their numbers, as Python lists them (which differs between platforms).
    static sd::list<Signals> sd_members() {
        sd::list<Signals> out;
        for (std::int64_t i = 0; i < static_cast<std::int64_t>(sd_table().values.size()); ++i) out.push_back(sd_at(i));
        std::stable_sort(out.vec().begin(), out.vec().end());
        return out;
    }
    std::string sd_repr() const { return sd::enums::repr(sd_table(), sd_index); }
    std::string sd_str() const { return sd::str(sd_value()); }
    bool sd_truthy() const { return true; }  // (no signal is 0)
    friend std::string format_value(const Signals& x, std::string_view spec) { return sd::format_any(x.sd_value(), spec); }
    bool operator==(const Signals&) const = default;
    operator const std::int64_t&() const { return sd_value(); }
    bool operator<(const Signals& o) const { return sd_value() < o.sd_value(); }
};

// ---- the dispatcher -------------------------------------------------------------

// What the C handler touches: lock-free, and constant-initialized (no constructor to wait for).
static_assert(std::atomic<std::uint64_t>::is_always_lock_free && std::atomic<int>::is_always_lock_free);
inline std::atomic<std::uint64_t> received[NSIG];  // signals the C handler has seen, per number
inline std::atomic<int> wake_fd{-1};               // the pipe's write end

extern "C" inline void deliver(int sig) {
    int saved = errno;
    received[sig].fetch_add(1, std::memory_order_release);
    char byte = static_cast<char>(sig);
    ssize_t n = ::write(wake_fd.load(std::memory_order_relaxed), &byte, 1);  // (a full pipe is fine: it's counted)
    (void)n;
    errno = saved;
}

// Python's default_int_handler on a signal other than SIGINT: end the program as an uncaught
// KeyboardInterrupt does, by SIGINT.
extern "C" inline void interrupt(int) {
    ::signal(SIGINT, SIG_DFL);
    ::kill(::getpid(), SIGINT);
}

inline const std::thread::id main_thread = std::this_thread::get_id();  // (static initialization runs on it)

struct State {
    std::mutex mu;  // the handlers
    Handler handlers[NSIG];
    bool set[NSIG] = {};  // set from seadash (otherwise sigaction says what's there)
    std::thread::id dispatcher;
    bool started = false;

    std::mutex done_mu;  // what the waiters below wait for
    std::condition_variable done_cv;
    std::uint64_t completed[NSIG] = {};  // received[sig] as of the end of its last handler run
    bool active[NSIG] = {};              // a seadash function is sig's handler
    std::uint64_t runs = 0;              // handlers run, for pause()
    bool stopped = false;                // the program is ending: no more handlers

    std::mutex run_mu;  // held while a handler runs
};

// Never destroyed: the dispatcher may still be waiting on the pipe when the program ends.
inline State& state() {
    static State* s = new State();
    return *s;
}

inline void check_range(std::int64_t signum) {
    if (signum < 1 || signum >= NSIG) raise("ValueError", "signal number out of range");
}

// An exception or sys.exit() escaping a handler ends the whole program (Python raises it in the
// main thread, which ends the program unless something catches it there).
[[noreturn]] inline void end_program(int code, const std::string& message) {
    std::fflush(stdout);
    if (!message.empty()) std::fwrite(message.data(), 1, message.size(), stderr);
    std::fflush(nullptr);
    std::_Exit(code);
}

// Runs sig's handler, if it's still a function of the program's; says whether it did.
inline bool run_handler(int sig) {
    State& s = state();
    std::shared_ptr<const Function> fn;
    {
        std::lock_guard lk(s.mu);
        if (s.handlers[sig].kind() == Handler::Kind::Function) fn = s.handlers[sig].fn();
    }
    if (!fn) return false;  // (changed since the signal came)
    std::lock_guard running(s.run_mu);
    {
        std::lock_guard lk(s.done_mu);
        if (s.stopped) return false;
    }
    try {
        (*fn)(sig, std::nullopt);
    } catch (const Exit& e) {
        end_program(e.code, "");
    } catch (const Thrown& t) {
        std::string msg = t.exc->sd_str();
        end_program(1, t.exc->sd_type() + (msg.empty() ? "" : ": " + msg) + "\n");
    }
    return true;
}

inline void dispatch(int wake_r) {
    State& s = state();
    std::uint64_t dispatched[NSIG] = {};
    char buf[64];
    while (true) {
        if (::read(wake_r, buf, sizeof buf) < 0 && errno != EINTR) return;
        for (int sig = 1; sig < NSIG; ++sig) {  // (Python runs pending handlers in this order too)
            std::uint64_t r = received[sig].load(std::memory_order_acquire);
            if (r == dispatched[sig]) continue;
            dispatched[sig] = r;
            bool ran = run_handler(sig);
            {
                std::lock_guard lk(s.done_mu);
                s.completed[sig] = r;
                s.runs += ran;
            }
            s.done_cv.notify_all();
        }
    }
}

// At the end of the program (after it has waited for its threads, which handlers may still be
// helping): wait for a running handler, and run no more.
inline void stop() {
    State& s = state();
    std::lock_guard running(s.run_mu);
    {
        std::lock_guard lk(s.done_mu);
        s.stopped = true;
    }
    s.done_cv.notify_all();
}

inline void start_dispatcher(State& s) {  // (s.mu held)
    if (s.started) return;
    int fds[2];
    if (::pipe(fds) != 0) raise_os(errno, std::nullopt);
    for (int fd : fds) ::fcntl(fd, F_SETFD, FD_CLOEXEC);
    ::fcntl(fds[1], F_SETFL, ::fcntl(fds[1], F_GETFL) | O_NONBLOCK);  // the C handler must never block
    wake_fd.store(fds[1], std::memory_order_relaxed);
    std::thread t([r = fds[0]] { dispatch(r); });
    s.dispatcher = t.get_id();
    t.detach();
    s.started = true;
    exit_hooks().push_back(stop);  // (after threading's, which waits for the program's threads)
}

// What sig's handler is: what seadash set, or else what sigaction says (SIGINT's default is
// Python's default_int_handler).
inline Handler current(State& s, int sig) {  // (s.mu held)
    if (s.set[sig]) return s.handlers[sig];
    struct sigaction old {};
    if (::sigaction(sig, nullptr, &old) != 0) raise_os(errno, std::nullopt);
    if (old.sa_handler == SIG_IGN) return ignore();
    if (old.sa_handler == SIG_DFL) return sig == SIGINT ? default_int_handler() : default_action();
    return Handler(Handler::Kind::None);
}

inline Handler getsignal(std::int64_t signalnum) {
    check_range(signalnum);
    State& s = state();
    std::lock_guard lk(s.mu);
    return current(s, static_cast<int>(signalnum));
}

inline Handler signal(std::int64_t signalnum, const Handler& handler) {
    check_range(signalnum);
    int sig = static_cast<int>(signalnum);
    State& s = state();
    std::unique_lock lk(s.mu);
    auto me = std::this_thread::get_id();
    if (me != main_thread && !(s.started && me == s.dispatcher))  // (a handler runs where Python's main thread would)
        raise("ValueError", "signal only works in main thread of the main interpreter");
    struct sigaction sa {};
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = SA_RESTART;  // (what the program is doing carries on: it doesn't see EINTR)
    switch (handler.kind()) {
        case Handler::Kind::None:
            raise("TypeError", "signal handler must be signal.SIG_IGN, signal.SIG_DFL, or a callable object");
        case Handler::Kind::Default:
            sa.sa_handler = SIG_DFL;
            break;
        case Handler::Kind::Ignore:
            sa.sa_handler = SIG_IGN;
            break;
        case Handler::Kind::DefaultInt:  // (SIGINT's default action ends the program as KeyboardInterrupt would)
            sa.sa_handler = sig == SIGINT ? SIG_DFL : interrupt;
            break;
        case Handler::Kind::Function:
            start_dispatcher(s);
            sa.sa_handler = deliver;
            break;
    }
    Handler old = current(s, sig);
    if (::sigaction(sig, &sa, nullptr) != 0) raise_os(errno, std::nullopt);
    s.handlers[sig] = handler;
    s.set[sig] = true;
    {
        std::lock_guard done(s.done_mu);
        s.active[sig] = handler.kind() == Handler::Kind::Function;
    }
    lk.unlock();
    s.done_cv.notify_all();  // (raise_signal() waiting for a handler that's gone needn't wait)
    return old;
}

// Sends sig to this process with send(), then waits for its handler to run, if it has one of
// seadash's (not on the dispatcher itself: it runs after the handler that sent it).
template <class Send>
void send_and_wait(int sig, Send send) {
    State& s = state();
    std::uint64_t before = received[sig].load(std::memory_order_acquire);
    send();
    bool started;
    std::thread::id dispatcher;
    {
        std::lock_guard lk(s.mu);
        started = s.started;
        dispatcher = s.dispatcher;
    }
    if (!started || std::this_thread::get_id() == dispatcher) return;
    // (Once the signal has been seen, the dispatcher will get to it, whatever its handler is by
    // then; until it has, a handler that's gone may mean it never comes.)
    std::unique_lock lk(s.done_mu);
    s.done_cv.wait(lk, [&] {
        return s.completed[sig] > before || s.stopped ||
               (!s.active[sig] && received[sig].load(std::memory_order_acquire) == before);
    });
}

// (Like Python's, it leaves checking the number to the system: 0 does nothing, NSIG fails.)
inline void raise_signal(std::int64_t signalnum) {
    int sig = static_cast<int>(signalnum);
    auto send = [&] {
        if (::raise(sig) != 0) raise_os(errno, std::nullopt);
    };
    if (signalnum < 1 || signalnum >= NSIG) return send();
    send_and_wait(sig, send);
}

// os.kill(os.getpid(), sig)
inline void kill_self(std::int64_t sig) {
    auto send = [&] {
        if (::kill(::getpid(), static_cast<int>(sig)) != 0) raise_os(errno, std::nullopt);
    };
    if (sig < 1 || sig >= NSIG) return send();  // (0 only checks the process exists; others fail)
    send_and_wait(static_cast<int>(sig), send);
}
inline const bool kill_hooked = (sd::os::kill_self = kill_self, true);

// Waits until a handler has run (rather than for the signal itself, which was seen on whichever
// thread the system chose).
inline void pause() {
    State& s = state();
    std::unique_lock lk(s.done_mu);
    std::uint64_t runs = s.runs;
    s.done_cv.wait(lk, [&] { return s.runs != runs; });
}

inline std::int64_t alarm(std::int64_t seconds) { return ::alarm(static_cast<unsigned>(seconds)); }

inline std::optional<std::string> strsignal(std::int64_t signalnum) {
    check_range(signalnum);
    static std::mutex m;  // (strsignal may use a static buffer)
    std::lock_guard lk(m);
    errno = 0;
    const char* text = ::strsignal(static_cast<int>(signalnum));
    if (errno != 0 || text == nullptr || std::strstr(text, "Unknown signal") != nullptr) return std::nullopt;
    return std::string(text);
}

inline sd::set<std::int64_t> valid_signals() {
    sigset_t all;
    sigfillset(&all);
    sd::set<std::int64_t> out;
    for (int sig = 1; sig < NSIG; ++sig)
        if (sigismember(&all, sig) == 1) out.insert(sig);
    return out;
}

}  // namespace sd::signal

template <>
struct std::hash<sd::signal::Signals> {
    std::size_t operator()(const sd::signal::Signals& x) const { return std::hash<std::int64_t>{}(x.sd_index); }
};
