// The `queue` module: a thread-safe FIFO queue. Items are copied in and out, so any
// value can cross between threads through a Queue.
//
// Handing an item over is usually quicker than a trip through the kernel, so:
//  - the deque is guarded by a spinlock (held only for one push or pop);
//  - a thread that finds the queue empty (or full) spins briefly, watching an atomic
//    count, before it sleeps on a condition variable;
//  - put/get only wake a sleeper when no other waiter is spinning (a spinner will take
//    the next item anyway); a thread that takes an item while more remain wakes the
//    next sleeper itself, so no item is left waiting while someone sleeps.
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>

namespace sd::queue {

struct Empty : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "queue.Empty"; }
};
struct Full : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "queue.Full"; }
};

// Guards the deque: held only for a push or pop, so waiting threads spin (yielding
// if the holder seems to have been descheduled) rather than sleep in the kernel.
class SpinLock {
    std::atomic<bool> held_{false};

public:
    void lock() {
        for (int i = 0;; ++i) {
            if (!held_.load(std::memory_order_relaxed) && !held_.exchange(true, std::memory_order_acquire)) return;
            if (i < 100) {
                cpu_relax();
            } else {
                std::this_thread::yield();
            }
        }
    }
    void unlock() { held_.store(false, std::memory_order_release); }
};

// The threads waiting for one condition (an item to get, or room to put). Waiters
// spin, then sleep; `ready` is checked again after announcing ourselves asleep, and
// the notifier updates the queue before reading `spinning`/`asleep` (all seq_cst), so a
// wake-up can't be missed.
struct Waiters {
    alignas(64) std::atomic<int> spinning{0};
    std::atomic<int> asleep{0};
    std::mutex mu;
    std::condition_variable cv;
    static constexpr int SPINS = 2000;

    // Wait until ready() may be true; false if the deadline passed first.
    template <class Ready>
    bool wait(Ready ready, std::optional<std::chrono::steady_clock::time_point> deadline) {
        spinning.fetch_add(1);
        bool got = false;
        for (int i = 0; i < SPINS && !(got = ready()); ++i) cpu_relax();
        spinning.fetch_sub(1);
        if (got) return true;
        std::unique_lock lk(mu);
        asleep.fetch_add(1);
        bool ok = true;
        if (!ready()) {
            if (deadline) {
                ok = cv.wait_until(lk, *deadline) == std::cv_status::no_timeout;
            } else {
                cv.wait(lk);
            }
        }
        asleep.fetch_sub(1);
        return ok || ready();
    }
    void wake_if_needed() {
        if (spinning.load() == 0 && asleep.load() > 0) {
            { std::lock_guard lk(mu); }  // a sleeper that saw not-ready is now inside wait()
            cv.notify_one();
        }
    }
};

template <class T>
class Queue {
    using Clock = std::chrono::steady_clock;

    struct State {
        alignas(64) SpinLock mu;  // guards items/unfinished
        alignas(64) std::atomic<std::int64_t> count{0};  // items.size(), readable without the lock
        std::deque<T> items;
        std::int64_t maxsize = 0;
        std::int64_t unfinished = 0;
        Waiters getters, putters;
        std::condition_variable_any all_done;
    };
    std::shared_ptr<State> s_;

    static std::optional<Clock::time_point> deadline(std::optional<double> timeout) {
        if (!timeout) return std::nullopt;
        if (*timeout < 0) raise("ValueError", "'timeout' must be a non-negative number");
        return Clock::now() + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(*timeout));
    }
    bool try_put(T& item) {
        std::int64_t size;
        {
            std::lock_guard lk(s_->mu);
            size = static_cast<std::int64_t>(s_->items.size());
            if (s_->maxsize > 0 && size >= s_->maxsize) return false;
            s_->items.push_back(std::move(item));
            ++s_->unfinished;
            s_->count.store(++size);
        }
        s_->getters.wake_if_needed();
        if (s_->maxsize > 0 && size < s_->maxsize) s_->putters.wake_if_needed();  // pass the baton
        return true;
    }
    bool has_room() const { return s_->maxsize <= 0 || s_->count.load() < s_->maxsize; }
    bool has_items() const { return s_->count.load() > 0; }

public:
    explicit Queue(std::int64_t maxsize = 0) : s_(std::make_shared<State>()) { s_->maxsize = maxsize; }

    void put(T value, bool block = true, std::optional<double> timeout = std::nullopt) {
        T item = send(std::move(value));  // the receiver gets its own (moved when the sender is done with it)
        std::optional<std::optional<Clock::time_point>> until;
        while (!try_put(item)) {
            if (!block) throw Thrown{std::make_shared<Full>("")};
            if (!until) until = deadline(timeout);
            if (!s_->putters.wait([&] { return has_room(); }, *until)) throw Thrown{std::make_shared<Full>("")};
        }
    }
    T get(bool block = true, std::optional<double> timeout = std::nullopt) {
        std::optional<std::optional<Clock::time_point>> until;
        while (true) {
            std::optional<T> item;
            std::int64_t left = 0;
            {
                std::lock_guard lk(s_->mu);
                if (!s_->items.empty()) {
                    item.emplace(std::move(s_->items.front()));
                    s_->items.pop_front();
                    left = static_cast<std::int64_t>(s_->items.size());
                    s_->count.store(left);
                }
            }
            if (item) {
                if (s_->maxsize > 0) s_->putters.wake_if_needed();
                if (left > 0) s_->getters.wake_if_needed();  // pass the baton
                return std::move(*item);
            }
            if (!block) throw Thrown{std::make_shared<Empty>("")};
            if (!until) until = deadline(timeout);
            if (!s_->getters.wait([&] { return has_items(); }, *until)) throw Thrown{std::make_shared<Empty>("")};
        }
    }
    void put_nowait(T item) { put(std::move(item), false); }
    T get_nowait() { return get(false); }

    bool empty() const { return qsize() == 0; }
    bool full() const { return s_->maxsize > 0 && qsize() >= s_->maxsize; }
    std::int64_t qsize() const { return s_->count.load(); }
    void task_done() {
        std::lock_guard lk(s_->mu);
        if (s_->unfinished <= 0) raise("ValueError", "task_done() called too many times");
        if (--s_->unfinished == 0) s_->all_done.notify_all();
    }
    void join() {
        std::unique_lock lk(s_->mu);
        s_->all_done.wait(lk, [&] { return s_->unfinished == 0; });
    }
    std::string sd_repr() const { return "<Queue qsize=" + std::to_string(qsize()) + ">"; }
};

}  // namespace sd::queue
