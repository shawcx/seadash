// The `concurrent.futures` module: ThreadPoolExecutor and Future on real OS threads (no
// GIL). The checker has already verified that what the work shares is safe (the same
// rules as threading.Thread), so here it's a plain thread pool.
#pragma once

#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>

namespace sd::futures {

struct CancelledError : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "concurrent.futures.CancelledError"; }
};
struct InvalidStateError : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "concurrent.futures.InvalidStateError"; }
};

using Clock = std::chrono::steady_clock;
inline std::optional<Clock::time_point> deadline_after(std::optional<double> timeout) {
    if (!timeout) return std::nullopt;
    return Clock::now() + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(*timeout));
}

// A result that will be there later. Copies share it.
template <class T>
class Future {
    using Stored = std::conditional_t<std::is_void_v<T>, std::monostate, T>;
    enum class Status { pending, running, finished, cancelled };
    struct State {
        std::mutex mu;
        std::condition_variable cv;
        Status status = Status::pending;
        std::optional<Stored> value;
        std::shared_ptr<BaseException> error;
        std::vector<std::function<void(Future)>> callbacks;
    };
    std::shared_ptr<State> s_;

    void finish(std::unique_lock<std::mutex>& lk, Status status) {
        s_->status = status;
        auto callbacks = std::move(s_->callbacks);
        lk.unlock();
        s_->cv.notify_all();
        for (auto& cb : callbacks) run_callback(cb);
    }
    void run_callback(const std::function<void(Future)>& cb) {
        try {
            cb(*this);
        } catch (const Thrown& t) {  // like Python: logged, and the other callbacks still run
            std::string msg = "exception calling callback for " + sd_repr() + "\n" + t.exc->sd_type() +
                              (t.exc->message.empty() ? "" : ": " + t.exc->message) + "\n";
            std::fwrite(msg.data(), 1, msg.size(), stderr);
        }
    }
    bool wait_done(std::unique_lock<std::mutex>& lk, std::optional<double> timeout) const {
        auto ready = [&] { return s_->status == Status::finished || s_->status == Status::cancelled; };
        if (!timeout) {
            s_->cv.wait(lk, ready);
            return true;
        }
        return s_->cv.wait_for(lk, std::chrono::duration<double>(*timeout), ready);
    }

public:
    Future() : s_(std::make_shared<State>()) {}

    // ---- for the executor ----
    bool start() {  // false if it was cancelled first
        std::lock_guard lk(s_->mu);
        if (s_->status != Status::pending) return false;
        s_->status = Status::running;
        return true;
    }
    template <class F>
    void run(F&& work) {
        try {
            if constexpr (std::is_void_v<T>) {
                work();
                set(std::monostate{});
            } else {
                set(work());
            }
        } catch (const Thrown& t) {
            std::unique_lock lk(s_->mu);
            s_->error = t.exc;
            finish(lk, Status::finished);
        } catch (const Exit&) {  // sys.exit() in the work: the work just ends
            set(Stored{});
        }
    }
    void set(Stored v) {
        std::unique_lock lk(s_->mu);
        s_->value = std::move(v);
        finish(lk, Status::finished);
    }

    // ---- Python's API ----
    T result(std::optional<double> timeout = std::nullopt) const {
        std::unique_lock lk(s_->mu);
        if (!wait_done(lk, timeout)) throw Thrown{std::make_shared<TimeoutError>("")};
        if (s_->status == Status::cancelled) throw Thrown{std::make_shared<CancelledError>("")};
        if (s_->error) throw Thrown{s_->error};
        if constexpr (!std::is_void_v<T>) return *s_->value;
    }
    std::optional<std::shared_ptr<Exception>> exception(std::optional<double> timeout = std::nullopt) const {
        std::unique_lock lk(s_->mu);
        if (!wait_done(lk, timeout)) throw Thrown{std::make_shared<TimeoutError>("")};
        if (s_->status == Status::cancelled) throw Thrown{std::make_shared<CancelledError>("")};
        if (!s_->error) return std::nullopt;
        return std::dynamic_pointer_cast<Exception>(s_->error);
    }
    bool cancel() {
        std::unique_lock lk(s_->mu);
        if (s_->status == Status::running || s_->status == Status::finished) return false;
        if (s_->status == Status::cancelled) return true;
        finish(lk, Status::cancelled);
        return true;
    }
    bool cancelled() const {
        std::lock_guard lk(s_->mu);
        return s_->status == Status::cancelled;
    }
    bool running() const {
        std::lock_guard lk(s_->mu);
        return s_->status == Status::running;
    }
    bool done() const {
        std::lock_guard lk(s_->mu);
        return s_->status == Status::finished || s_->status == Status::cancelled;
    }
    void add_done_callback(std::function<void(Future)> cb) {
        std::unique_lock lk(s_->mu);
        if (s_->status == Status::finished || s_->status == Status::cancelled) {
            lk.unlock();
            run_callback(cb);
            return;
        }
        s_->callbacks.push_back(std::move(cb));
    }
    // Wait (up to the deadline) until it's finished or cancelled.
    bool wait_until(std::optional<Clock::time_point> deadline) const {
        std::unique_lock lk(s_->mu);
        auto ready = [&] { return s_->status == Status::finished || s_->status == Status::cancelled; };
        if (!deadline) {
            s_->cv.wait(lk, ready);
            return true;
        }
        return s_->cv.wait_until(lk, *deadline, ready);
    }
    bool failed() const {
        std::lock_guard lk(s_->mu);
        return s_->error != nullptr;
    }

    std::string sd_repr() const {
        std::lock_guard lk(s_->mu);
        static const char* names[] = {"pending", "running", "finished", "cancelled"};
        std::string out = "<Future state=" + std::string(names[static_cast<int>(s_->status)]);
        if (s_->status == Status::finished) out += s_->error ? " raised " + s_->error->sd_type() : " returned";
        return out + ">";
    }
    bool operator==(const Future& o) const { return s_ == o.s_; }
    bool operator<(const Future& o) const { return s_ < o.s_; }
    const void* identity() const { return s_.get(); }
};

class ThreadPoolExecutor {
    struct State {
        std::mutex mu;
        std::condition_variable cv;
        std::deque<std::function<void()>> tasks;
        std::vector<std::thread> workers;
        std::size_t max_workers = 1, idle = 0;
        std::string prefix;
        bool shutting_down = false, joined = false;
        std::vector<std::function<void()>> cancel_pending;  // one per queued task, to cancel its future
    };
    std::shared_ptr<State> s_;

    static void work(std::shared_ptr<State> s) {  // (holds the pool alive while it runs)
        while (true) {
            std::function<void()> task;
            {
                std::unique_lock lk(s->mu);
                ++s->idle;
                s->cv.wait(lk, [&] { return !s->tasks.empty() || s->shutting_down; });
                --s->idle;
                if (s->tasks.empty()) return;  // shutting down, and nothing left to do
                task = std::move(s->tasks.front());
                s->tasks.pop_front();
                s->cancel_pending.erase(s->cancel_pending.begin());
            }
            task();
        }
    }
    static std::mutex& registry_mu() {
        static std::mutex m;
        return m;
    }
    static std::vector<std::weak_ptr<State>>& registry() {
        static std::vector<std::weak_ptr<State>> r;
        return r;
    }
    static void shutdown_state(State& s, bool wait, bool cancel_futures) {
        std::vector<std::function<void()>> cancels;
        {
            std::lock_guard lk(s.mu);
            s.shutting_down = true;
            if (cancel_futures) {
                cancels = std::move(s.cancel_pending);
                s.cancel_pending.clear();
                s.tasks.clear();
            }
        }
        s.cv.notify_all();
        for (auto& c : cancels) c();
        if (!wait) return;
        std::vector<std::thread> workers;
        {
            std::lock_guard lk(s.mu);
            if (s.joined) return;
            s.joined = true;
            workers = std::move(s.workers);
        }
        for (auto& w : workers)
            if (w.get_id() != std::this_thread::get_id()) w.join();
            else w.detach();
    }
    static void join_all() {  // like Python, the program waits for submitted work at exit
        std::vector<std::shared_ptr<State>> live;
        {
            std::lock_guard lk(registry_mu());
            for (auto& w : registry())
                if (auto s = w.lock()) live.push_back(s);
        }
        for (auto& s : live) shutdown_state(*s, true, false);
    }

public:
    ThreadPoolExecutor() = default;
    ThreadPoolExecutor(std::optional<std::int64_t> max_workers, std::string thread_name_prefix = "")
        : s_(std::make_shared<State>()) {
        if (max_workers && *max_workers <= 0) raise("ValueError", "max_workers must be greater than 0");
        std::size_t cpus = std::max(1u, std::thread::hardware_concurrency());
        s_->max_workers = max_workers ? static_cast<std::size_t>(*max_workers) : std::min<std::size_t>(32, cpus + 4);
        s_->prefix = std::move(thread_name_prefix);
        static const bool hooked = [] {
            exit_hooks().push_back(join_all);
            return true;
        }();
        (void)hooked;
        std::lock_guard lk(registry_mu());
        registry().push_back(s_);
    }
    ~ThreadPoolExecutor() = default;

    template <class R, class F, class... Args>
    Future<R> submit(F fn, Args... args) {
        Future<R> fut;
        {
            std::lock_guard lk(s_->mu);
            if (s_->shutting_down) raise("RuntimeError", "cannot schedule new futures after shutdown");
            s_->tasks.push_back([fut, fn = std::move(fn), args = std::make_tuple(std::move(args)...)]() mutable {
                if (!fut.start()) return;  // cancelled while queued
                fut.run([&]() -> R { return std::apply(fn, args); });
            });
            s_->cancel_pending.push_back([fut]() mutable { fut.cancel(); });
            // A new thread only when no worker is free, up to max_workers (like Python).
            if (s_->idle < s_->tasks.size() && s_->workers.size() < s_->max_workers)
                s_->workers.emplace_back(work, s_);
        }
        s_->cv.notify_one();
        return fut;
    }

    // map(fn, *iterables): all submitted now; results come back in order, as they're read.
    template <class R, class F, class... Its>
    Generator<R> map(F fn, std::optional<double> timeout, Its... its) {
        std::vector<Future<R>> futures;
        for (auto&& args : zip_lazy<std::tuple<elem_t<decltype(iter(its))>...>>(its...)) {
            futures.push_back(std::apply([&](const auto&... a) { return submit<R>(fn, a...); }, args));
        }
        return results<R>(std::move(futures), deadline_after(timeout));
    }
    template <class R>
    static Generator<R> results(std::vector<Future<R>> futures, std::optional<Clock::time_point> deadline) {
        for (auto& f : futures) {
            if (!f.wait_until(deadline)) throw Thrown{std::make_shared<TimeoutError>("")};
            co_yield f.result();
        }
    }

    void shutdown(bool wait = true, bool cancel_futures = false) { shutdown_state(*s_, wait, cancel_futures); }
    std::string sd_repr() const { return "<concurrent.futures.thread.ThreadPoolExecutor object>"; }
};

// as_completed(futures): each future as it finishes (those already done first).
template <class T, class It>
Generator<Future<T>> as_completed(It items, std::optional<double> timeout) {
    std::vector<Future<T>> fs;
    for (auto&& f : iter(items))
        if (std::find(fs.begin(), fs.end(), f) == fs.end()) fs.push_back(f);
    struct Shared {
        std::mutex mu;
        std::condition_variable cv;
        std::deque<Future<T>> finished;
    };
    auto shared = std::make_shared<Shared>();
    for (auto& f : fs) {
        f.add_done_callback([shared](Future<T> done) {
            {
                std::lock_guard lk(shared->mu);
                shared->finished.push_back(done);
            }
            shared->cv.notify_one();
        });
    }
    auto deadline = deadline_after(timeout);
    for (std::size_t yielded = 0; yielded < fs.size(); ++yielded) {
        Future<T> next;
        {
            std::unique_lock lk(shared->mu);
            auto ready = [&] { return !shared->finished.empty(); };
            bool ok = deadline ? shared->cv.wait_until(lk, *deadline, ready) : (shared->cv.wait(lk, ready), true);
            if (!ok)
                throw Thrown{std::make_shared<TimeoutError>(std::to_string(fs.size() - yielded) + " (of " +
                                                            std::to_string(fs.size()) + ") futures unfinished")};
            next = shared->finished.front();
            shared->finished.pop_front();
        }
        co_yield next;
    }
}

// wait(futures, timeout, return_when): (done, not_done).
template <class T, class It>
std::tuple<std::set<Future<T>>, std::set<Future<T>>> wait(It items, std::optional<double> timeout,
                                                          const std::string& return_when) {
    std::set<Future<T>> all;
    for (auto&& f : iter(items)) all.insert(f);
    auto deadline = deadline_after(timeout);
    auto split = [&] {
        std::set<Future<T>> done, pending;
        for (auto& f : all) (f.done() ? done : pending).insert(f);
        return std::make_tuple(done, pending);
    };
    while (true) {
        auto [done, pending] = split();
        bool enough = pending.empty() || (return_when == "FIRST_COMPLETED" && !done.empty()) ||
                      (return_when == "FIRST_EXCEPTION" && std::any_of(done.begin(), done.end(), [](auto& f) { return f.failed(); }));
        if (enough || (deadline && Clock::now() >= *deadline)) return {done, pending};
        // Sleep until something finishes (or the deadline): wait on the first pending future briefly.
        auto step = Clock::now() + std::chrono::milliseconds(5);
        pending.begin()->wait_until(deadline ? std::min(step, *deadline) : step);
    }
}

}  // namespace sd::futures

template <class T>
struct std::hash<sd::futures::Future<T>> {
    std::size_t operator()(const sd::futures::Future<T>& f) const { return std::hash<const void*>()(f.identity()); }
};
