// The `select` module: select() on lists of sockets, files or file descriptors, and poll
// objects. Both take an int, or anything with a fileno() (a socket, a file).
#pragma once

#include <poll.h>
#include <sys/select.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <climits>
#include <cmath>
#include <optional>
#include <tuple>
#include <vector>

namespace sd::select {

// The file descriptor of what select() or poll() is given (a closed socket's is -1).
template <class F>
std::int64_t fileno_of(const F& x) {
    if constexpr (std::is_integral_v<F>) {
        return x;
    } else if constexpr (is_shared<F>::value) {
        return x->fileno_();  // a file
    } else {
        return x.fileno();  // a socket
    }
}

template <class F>
int checked_fd(const F& x) {
    std::int64_t fd = fileno_of(x);
    if (fd < 0) raise("ValueError", "file descriptor cannot be a negative integer (" + std::to_string(fd) + ")");
    if (fd > INT_MAX) raise("OverflowError", "file descriptor is greater than maximum");
    return static_cast<int>(fd);
}

// When a wait that may be interrupted by a signal must end: retried calls (PEP 475) wait
// only for what's left. No deadline: wait forever.
class Deadline {
    std::optional<std::chrono::steady_clock::time_point> at_;

public:
    explicit Deadline(std::optional<double> seconds) {
        if (seconds && *seconds < 1e9) {  // (longer than 30 years: forever)
            at_ = std::chrono::steady_clock::now() +
                  std::chrono::duration_cast<std::chrono::steady_clock::duration>(std::chrono::duration<double>(*seconds));
        }
    }
    bool forever() const { return !at_; }
    // What's left, in microseconds rounded up (Python rounds timeouts up, to wait at least that long).
    std::int64_t micros_left() const {
        auto left = *at_ - std::chrono::steady_clock::now();
        if (left <= left.zero()) return 0;
        return std::chrono::ceil<std::chrono::microseconds>(left).count();
    }
    int millis_left() const {
        if (!at_) return -1;
        std::int64_t ms = (micros_left() + 999) / 1000;
        return static_cast<int>(std::min<std::int64_t>(ms, INT_MAX));
    }
};

// select.select(rlist, wlist, xlist, timeout): which of each list's items are ready, in the
// list's order (an item listed twice is reported twice), like Python's.
template <class R, class W, class X>
std::tuple<list<R>, list<W>, list<X>> select(const list<R>& rlist, const list<W>& wlist, const list<X>& xlist,
                                             std::optional<double> timeout = std::nullopt) {
    if (timeout && std::isnan(*timeout)) raise("ValueError", "Invalid value NaN (not a number)");
    if (timeout && *timeout < 0) raise("ValueError", "timeout must be non-negative");
    std::vector<int> rfds, wfds, xfds;
    int nfds = 0;
    auto gather = [&](const auto& objs, std::vector<int>& fds) {
        for (const auto& obj : objs.vec()) {
            int fd = checked_fd(obj);
            if (fd >= FD_SETSIZE) raise("ValueError", "filedescriptor out of range in select()");
            fds.push_back(fd);
            nfds = std::max(nfds, fd + 1);
        }
    };
    gather(rlist, rfds);
    gather(wlist, wfds);
    gather(xlist, xfds);
    Deadline deadline(timeout);
    fd_set rs, ws, xs;
    for (;;) {
        FD_ZERO(&rs);
        FD_ZERO(&ws);
        FD_ZERO(&xs);
        for (int fd : rfds) FD_SET(fd, &rs);
        for (int fd : wfds) FD_SET(fd, &ws);
        for (int fd : xfds) FD_SET(fd, &xs);
        timeval tv{};
        if (!deadline.forever()) {
            std::int64_t us = deadline.micros_left();
            tv.tv_sec = static_cast<decltype(tv.tv_sec)>(us / 1000000);
            tv.tv_usec = static_cast<decltype(tv.tv_usec)>(us % 1000000);
        }
        int rc = ::select(nfds, &rs, &ws, &xs, deadline.forever() ? nullptr : &tv);
        if (rc >= 0) break;
        if (errno != EINTR) raise_os(errno, std::nullopt);
    }
    auto ready = [](const auto& objs, const std::vector<int>& fds, fd_set& set) {
        std::remove_cvref_t<decltype(objs)> out;
        for (std::size_t i = 0; i < fds.size(); ++i)
            if (FD_ISSET(fds[i], &set)) out.push_back(objs.vec()[i]);
        return out;
    };
    return {ready(rlist, rfds, rs), ready(wlist, wfds, ws), ready(xlist, xfds, xs)};
}

// poll(fds) until something happens or the deadline passes, retrying after a signal.
inline void poll_fds(std::vector<pollfd>& fds, const Deadline& deadline) {
    for (;;) {
        int rc = ::poll(fds.data(), static_cast<nfds_t>(fds.size()), deadline.millis_left());
        if (rc >= 0) return;
        if (errno != EINTR) raise_os(errno, std::nullopt);
    }
}

// select.poll(): the descriptors it watches, in the order they were registered (Python's
// order too: it keeps them in a dict). Copies share them.
class Poll {
    std::shared_ptr<std::vector<pollfd>> fds_ = std::make_shared<std::vector<pollfd>>();

    pollfd* find(int fd) const {
        for (auto& p : *fds_)
            if (p.fd == fd) return &p;
        return nullptr;
    }

public:
    template <class F>
    void register_(const F& obj, std::int64_t eventmask) const {
        int fd = checked_fd(obj);
        if (pollfd* p = find(fd)) {
            p->events = static_cast<short>(eventmask);
        } else {
            fds_->push_back(pollfd{fd, static_cast<short>(eventmask), 0});
        }
    }
    template <class F>
    void modify(const F& obj, std::int64_t eventmask) const {
        pollfd* p = find(checked_fd(obj));
        if (!p) raise_os(ENOENT, std::nullopt);
        p->events = static_cast<short>(eventmask);
    }
    template <class F>
    void unregister(const F& obj) const {
        int fd = checked_fd(obj);
        auto it = std::find_if(fds_->begin(), fds_->end(), [&](const pollfd& p) { return p.fd == fd; });
        if (it == fds_->end()) raise("KeyError", std::to_string(fd));
        fds_->erase(it);
    }
    // poll(timeout): (fd, events) for each descriptor something happened to. The timeout is in
    // milliseconds; None or a negative one waits for as long as it takes.
    list<std::tuple<std::int64_t, std::int64_t>> poll(std::optional<double> timeout_ms = std::nullopt) const {
        if (timeout_ms && std::isnan(*timeout_ms)) raise("ValueError", "Invalid value NaN (not a number)");
        std::optional<double> seconds;
        if (timeout_ms && *timeout_ms >= 0) seconds = std::ceil(*timeout_ms) / 1000;
        std::vector<pollfd> fds = *fds_;
        poll_fds(fds, Deadline(seconds));
        list<std::tuple<std::int64_t, std::int64_t>> out;
        for (const auto& p : fds)
            if (p.revents) out.vec().emplace_back(p.fd, static_cast<std::int64_t>(static_cast<unsigned short>(p.revents)));
        return out;
    }
    std::string sd_repr() const { return "<select.poll object>"; }
};

}  // namespace sd::select
