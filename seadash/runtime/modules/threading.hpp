// The `threading` module: real OS threads (no global lock). The checker makes this
// safe: threads only share values that are copied (sd::value_copy), or the thread-safe types below.
// All of them are handles: copying one shares the same underlying lock/queue/etc.
#pragma once

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <shared_mutex>
#include <thread>

namespace sd::threading {


// Python's Lock: may be released by a different thread than the one that acquired it
// (undefined behaviour for std::mutex). The state is one atomic: 0 = unlocked,
// 1 = locked, 2 = locked and someone may be waiting. An uncontended acquire/release is a
// single atomic operation; waiters briefly spin, then sleep on a condition variable.
class Lock {
    struct State {
        std::atomic<int> v{0};
        std::mutex mu;  // only for sleeping and waking
        std::condition_variable cv;
    };
    std::shared_ptr<State> s_ = std::make_shared<State>();

    bool try_lock() {
        int expected = 0;
        return s_->v.compare_exchange_strong(expected, 1, std::memory_order_acquire, std::memory_order_relaxed);
    }

public:
    bool acquire(bool blocking = true, double timeout = -1) {
        if (try_lock()) return true;
        if (!blocking) return false;
        // Critical sections are usually short: spin a little before going to sleep.
        for (int i = 0; i < 100; ++i) {
            if (s_->v.load(std::memory_order_relaxed) == 0 && try_lock()) return true;
            cpu_relax();
        }
        // Mark the lock contended (2) so release() knows to wake someone. Swapping in 2
        // and seeing 0 means we took it (conservatively still marked contended).
        auto take = [&] { return s_->v.exchange(2, std::memory_order_acquire) == 0; };
        std::unique_lock lk(s_->mu);
        if (timeout < 0) {
            s_->cv.wait(lk, take);
            return true;
        }
        return s_->cv.wait_for(lk, std::chrono::duration<double>(timeout), take);
    }
    void release() {
        int prev = s_->v.exchange(0, std::memory_order_release);
        if (prev == 0) raise("RuntimeError", "release unlocked lock");
        if (prev == 2) {
            // Taking the mutex orders this wake-up after a waiter's check-then-sleep.
            { std::lock_guard lk(s_->mu); }
            s_->cv.notify_one();
        }
    }
    bool locked() const { return s_->v.load(std::memory_order_relaxed) != 0; }
    std::string sd_repr() const { return locked() ? "<locked Lock>" : "<unlocked Lock>"; }
};

// A re-entrant lock: the owning thread may acquire it again.
class RLock {
    struct State {
        std::mutex mu;
        std::condition_variable cv;
        std::thread::id owner;
        std::int64_t count = 0;
    };
    std::shared_ptr<State> s_ = std::make_shared<State>();

public:
    bool acquire(bool blocking = true, double timeout = -1) {
        auto me = std::this_thread::get_id();
        std::unique_lock lk(s_->mu);
        auto free = [&] { return s_->count == 0 || s_->owner == me; };
        if (!free()) {
            if (!blocking) return false;
            if (timeout < 0) {
                s_->cv.wait(lk, free);
            } else if (!s_->cv.wait_for(lk, std::chrono::duration<double>(timeout), free)) {
                return false;
            }
        }
        s_->owner = me;
        ++s_->count;
        return true;
    }
    void release() {
        {
            std::lock_guard lk(s_->mu);
            if (s_->count == 0 || s_->owner != std::this_thread::get_id())
                raise("RuntimeError", "cannot release un-acquired lock");
            --s_->count;
        }
        s_->cv.notify_one();
    }
    std::string sd_repr() const { return "<RLock>"; }
};

// `with lock:` acquires, and releases on every way out of the block (RAII).
template <class L>
struct Held {
    L lock;
    explicit Held(L l) : lock(std::move(l)) { lock.acquire(); }
    Held(const Held&) = delete;
    ~Held() { lock.release(); }
};

class Event {
    struct State {
        std::mutex mu;
        std::condition_variable cv;
        bool flag = false;
    };
    std::shared_ptr<State> s_ = std::make_shared<State>();

public:
    void set() {
        {
            std::lock_guard lk(s_->mu);
            s_->flag = true;
        }
        s_->cv.notify_all();
    }
    void clear() {
        std::lock_guard lk(s_->mu);
        s_->flag = false;
    }
    bool is_set() const {
        std::lock_guard lk(s_->mu);
        return s_->flag;
    }
    bool wait(std::optional<double> timeout = std::nullopt) {
        std::unique_lock lk(s_->mu);
        if (!timeout) {
            s_->cv.wait(lk, [&] { return s_->flag; });
            return true;
        }
        return s_->cv.wait_for(lk, std::chrono::duration<double>(*timeout), [&] { return s_->flag; });
    }
    std::string sd_repr() const { return is_set() ? "<Event set>" : "<Event unset>"; }
};

// seadash: an int that any thread may update (lock-free).
class Atomic {
    std::shared_ptr<std::atomic<std::int64_t>> v_;

public:
    explicit Atomic(std::int64_t value = 0) : v_(std::make_shared<std::atomic<std::int64_t>>(value)) {}
    std::int64_t get() const { return v_->load(); }
    void set(std::int64_t value) { v_->store(value); }
    std::int64_t add(std::int64_t n = 1) { return v_->fetch_add(n) + n; }  // returns the new value
    std::int64_t sub(std::int64_t n = 1) { return v_->fetch_sub(n) - n; }
    bool compare_and_set(std::int64_t expected, std::int64_t value) { return v_->compare_exchange_strong(expected, value); }
    std::string sd_repr() const { return "Atomic(" + std::to_string(get()) + ")"; }
};

// seadash: a value only reachable while holding its lock: `with m as data:`.
template <class T>
class Mutex {
    struct State {
        std::mutex mu;
        T value;
    };
    std::shared_ptr<State> s_;

public:
    Mutex() : s_(std::make_shared<State>()) {}
    // Every way in and out copies (lists, dicts and sets are shared references otherwise).
    explicit Mutex(T value) : s_(std::make_shared<State>()) { s_->value = send(std::move(value)); }
    struct Guard {
        std::unique_lock<std::mutex> lk;
        T* v;
        T& value() { return *v; }
    };
    Guard lock() { return Guard{std::unique_lock(s_->mu), &s_->value}; }
    T get() const {
        std::lock_guard lk(s_->mu);
        return value_copy(s_->value);
    }
    void set(T value) {
        T copy = send(std::move(value));
        std::lock_guard lk(s_->mu);
        s_->value = std::move(copy);
    }
    std::string sd_repr() const { return "Mutex(" + repr(get()) + ")"; }
};

// seadash: a value many threads may read at once, or one may change:
// `with m.read() as data:` / `with m.write() as data:`.
template <class T>
struct ReadGuard {
    std::shared_lock<std::shared_mutex> lk;
    const T* v;
    const T& value() const { return *v; }
};
template <class T>
struct WriteGuard {
    std::unique_lock<std::shared_mutex> lk;
    T* v;
    T& value() { return *v; }
};

template <class T>
class RWMutex {
    struct State {
        std::shared_mutex mu;
        T value;
    };
    std::shared_ptr<State> s_;

public:
    RWMutex() : s_(std::make_shared<State>()) {}
    explicit RWMutex(T value) : s_(std::make_shared<State>()) { s_->value = send(std::move(value)); }
    ReadGuard<T> read() const { return {std::shared_lock(s_->mu), &s_->value}; }
    WriteGuard<T> write() const { return {std::unique_lock(s_->mu), &s_->value}; }
    T get() const {
        std::shared_lock lk(s_->mu);
        return value_copy(s_->value);
    }
    void set(T value) {
        T copy = send(std::move(value));
        std::unique_lock lk(s_->mu);
        s_->value = std::move(copy);
    }
    std::string sd_repr() const { return "RWMutex(" + repr(get()) + ")"; }
};

// Base class for `class Account(threading.Synchronized)`: every method holds this lock,
// so one instance can be shared between threads safely.
struct Synchronized : virtual object {
    mutable std::recursive_mutex sd_mutex;
    virtual ~Synchronized() = default;
    virtual std::string sd_repr() const { return "<Synchronized>"; }
    virtual std::string sd_class_name() const { return "Synchronized"; }
    virtual std::string sd_str() const { return sd_repr(); }
    virtual bool sd_truthy() const { return true; }
};

// ---- threads -------------------------------------------------------------------

struct ThreadState {
    std::function<void()> fn;
    std::string name;
    bool daemon = false;
    std::thread thread;
    std::mutex mu;
    std::condition_variable cv;
    bool started = false, done = false, joined = false;
};

inline std::mutex& registry_mutex() {
    static std::mutex m;
    return m;
}
inline std::vector<std::shared_ptr<ThreadState>>& registry() {
    static std::vector<std::shared_ptr<ThreadState>> threads;
    return threads;
}

inline void finish(const std::shared_ptr<ThreadState>& s) {
    // Called by whoever joins; exactly one caller does the OS-level join.
    std::unique_lock lk(s->mu);
    s->cv.wait(lk, [&] { return s->done; });
    if (!s->joined && !s->daemon) {
        s->joined = true;
        lk.unlock();
        s->thread.join();
    }
}

inline void join_all_threads() {
    // The program ends only when every non-daemon thread has finished, like Python.
    while (true) {
        std::vector<std::shared_ptr<ThreadState>> pending;
        {
            std::lock_guard lk(registry_mutex());
            pending.swap(registry());
        }
        if (pending.empty()) return;
        for (auto& s : pending) finish(s);
    }
}

inline void run_thread(const std::shared_ptr<ThreadState>& s) {
    thread_name() = s->name;  // (for logging's %(threadName)s)
    try {
        s->fn();
    } catch (const Thrown& t) {
        // Like Python: report it and end this thread; the rest of the program carries on.
        std::string msg = t.exc->sd_str();
        std::string text = "Exception in thread " + s->name + ":\n" + format_traceback(*t.exc) + t.exc->sd_type() +
                           (msg.empty() ? "" : ": " + msg) + "\n";
        std::fwrite(text.data(), 1, text.size(), stderr);
    } catch (const Exit&) {
        // sys.exit() in a thread just ends the thread
    }
    try {
        s->fn = nullptr;  // the target and its arguments go now, as Python's run() deletes them (a connection closes)
    } catch (...) {
    }
    {
        std::lock_guard lk(s->mu);
        s->done = true;
    }
    s->cv.notify_all();
}

class Thread {
    std::shared_ptr<ThreadState> s_;

    ThreadState& state() const {
        if (!s_) raise("RuntimeError", "thread was not created with threading.Thread(...)");
        return *s_;
    }

public:
    Thread() = default;
    Thread(std::function<void()> fn, std::optional<std::string> name, const std::string& target_name, bool daemon)
        : s_(std::make_shared<ThreadState>()) {
        static std::atomic<std::int64_t> counter{0};
        s_->fn = std::move(fn);
        s_->daemon = daemon;
        s_->name = name ? *name : "Thread-" + std::to_string(++counter) + " (" + target_name + ")";
    }
    void start() {
        ThreadState& s = state();
        {
            std::lock_guard lk(s.mu);
            if (s.started) raise("RuntimeError", "threads can only be started once");
            s.started = true;
        }
        auto shared = s_;
        s.thread = std::thread([shared] { run_thread(shared); });
        if (s.daemon) {
            s.thread.detach();
        } else {
            std::lock_guard lk(registry_mutex());
            registry().push_back(s_);
        }
    }
    void join(std::optional<double> timeout = std::nullopt) {
        ThreadState& s = state();
        if (!s.started) raise("RuntimeError", "cannot join thread before it is started");
        if (s.thread.get_id() == std::this_thread::get_id()) raise("RuntimeError", "cannot join current thread");
        if (timeout) {
            std::unique_lock lk(s.mu);
            if (!s.cv.wait_for(lk, std::chrono::duration<double>(*timeout), [&] { return s.done; })) return;
        }
        finish(s_);
    }
    bool is_alive() const {
        ThreadState& s = state();
        std::lock_guard lk(s.mu);
        return s.started && !s.done;
    }
    std::string name() const { return state().name; }
    bool daemon() const { return state().daemon; }
    std::string sd_repr() const { return "<Thread(" + name() + ", " + (is_alive() ? "started" : "stopped") + ")>"; }
};

inline const bool registered = [] {
    exit_hooks().push_back(join_all_threads);
    return true;
}();

}  // namespace sd::threading
