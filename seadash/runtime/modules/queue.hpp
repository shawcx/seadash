// The `queue` module: a thread-safe FIFO queue. Items are copied in and out, so any
// value can cross between threads through a Queue.
#pragma once

#include <condition_variable>
#include <deque>
#include <mutex>

namespace sd::queue {

struct Empty : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "queue.Empty"; }
};
struct Full : Exception {
    using Exception::Exception;
    std::string sd_type() const override { return "queue.Full"; }
};

template <class T>
class Queue {
    struct State {
        std::mutex mu;
        std::condition_variable not_empty, not_full, all_done;
        std::deque<T> items;
        std::int64_t maxsize = 0;
        std::int64_t unfinished = 0;
    };
    std::shared_ptr<State> s_;

    template <class Pred>
    static bool wait(std::condition_variable& cv, std::unique_lock<std::mutex>& lk, bool block,
                     std::optional<double> timeout, Pred ready) {
        if (ready()) return true;
        if (!block) return false;
        if (!timeout) {
            cv.wait(lk, ready);
            return true;
        }
        if (*timeout < 0) raise("ValueError", "'timeout' must be a non-negative number");
        return cv.wait_for(lk, std::chrono::duration<double>(*timeout), ready);
    }

public:
    explicit Queue(std::int64_t maxsize = 0) : s_(std::make_shared<State>()) { s_->maxsize = maxsize; }

    void put(T item, bool block = true, std::optional<double> timeout = std::nullopt) {
        std::unique_lock lk(s_->mu);
        auto has_room = [&] { return s_->maxsize <= 0 || static_cast<std::int64_t>(s_->items.size()) < s_->maxsize; };
        if (!wait(s_->not_full, lk, block, timeout, has_room)) throw Thrown{std::make_shared<Full>("")};
        s_->items.push_back(std::move(item));
        ++s_->unfinished;
        lk.unlock();
        s_->not_empty.notify_one();
    }
    T get(bool block = true, std::optional<double> timeout = std::nullopt) {
        std::unique_lock lk(s_->mu);
        if (!wait(s_->not_empty, lk, block, timeout, [&] { return !s_->items.empty(); }))
            throw Thrown{std::make_shared<Empty>("")};
        T item = std::move(s_->items.front());
        s_->items.pop_front();
        lk.unlock();
        s_->not_full.notify_one();
        return item;
    }
    void put_nowait(T item) { put(std::move(item), false); }
    T get_nowait() { return get(false); }

    bool empty() const {
        std::lock_guard lk(s_->mu);
        return s_->items.empty();
    }
    bool full() const {
        std::lock_guard lk(s_->mu);
        return s_->maxsize > 0 && static_cast<std::int64_t>(s_->items.size()) >= s_->maxsize;
    }
    std::int64_t qsize() const {
        std::lock_guard lk(s_->mu);
        return static_cast<std::int64_t>(s_->items.size());
    }
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
