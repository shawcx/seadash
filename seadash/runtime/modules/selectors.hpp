// The `selectors` module: a selector watches sockets, files or file descriptors (F), each
// registered with data (D). DefaultSelector, PollSelector and SelectSelector are all this
// poll() selector, with Python's PollSelector behaviour: select() reports each ready file
// once, in the order they were registered. A Selector is a handle: copies share it (so its
// methods are const, like a list's).
#pragma once

#include "select.hpp"

namespace sd::selectors {

inline constexpr std::int64_t EVENT_READ = 1;
inline constexpr std::int64_t EVENT_WRITE = 2;

template <class D>
D no_data() {
    if constexpr (std::is_same_v<D, std::nullopt_t>) {
        return std::nullopt;
    } else {
        return D{};
    }
}

// Is `a` the very object `b` is? (Python's `is`, for finding a file that's been closed.)
template <class F>
bool same(const F& a, const F& b) {
    if constexpr (requires { a.is(b); }) {
        return a.is(b);  // a socket
    } else {
        return a == b;  // a file (a shared pointer), or an int
    }
}

// A registration: Python's SelectorKey(fileobj, fd, events, data) namedtuple.
template <class F, class D>
class SelectorKey {
    F fileobj_{};
    std::int64_t fd_ = -1;
    std::int64_t events_ = 0;
    D data_ = no_data<D>();

public:
    SelectorKey() = default;
    SelectorKey(F fileobj, std::int64_t fd, std::int64_t events, D data)
        : fileobj_(std::move(fileobj)), fd_(fd), events_(events), data_(std::move(data)) {}
    F fileobj() const { return fileobj_; }
    std::int64_t fd() const { return fd_; }
    std::int64_t events() const { return events_; }
    D data() const { return data_; }
    void set(std::int64_t events, D data) {
        events_ = events;
        data_ = std::move(data);
    }
    std::string sd_repr() const {
        return "SelectorKey(fileobj=" + repr(fileobj_) + ", fd=" + std::to_string(fd_) +
               ", events=" + std::to_string(events_) + ", data=" + repr(data_) + ")";
    }
};

template <class F, class D>
class Selector {
    using Key = SelectorKey<F, D>;
    struct State {
        std::vector<Key> keys;     // in registration order,
        std::vector<pollfd> fds;  // and what poll() watches for each
        bool closed = false;
    };
    std::shared_ptr<State> s_ = std::make_shared<State>();

    static short poll_events(std::int64_t events) {
        return static_cast<short>(((events & EVENT_READ) ? POLLIN : 0) | ((events & EVENT_WRITE) ? POLLOUT : 0));
    }
    static void check_events(std::int64_t events) {
        if (!events || (events & ~(EVENT_READ | EVENT_WRITE))) raise("ValueError", "Invalid events: " + repr(events));
    }
    // Python's _fileobj_to_fd: the descriptor, or ValueError.
    static std::int64_t fileobj_to_fd(const F& fileobj) {
        std::int64_t fd;
        if constexpr (std::is_integral_v<F>) {
            fd = fileobj;
        } else {
            try {
                fd = sd::select::fileno_of(fileobj);
            } catch (const Thrown& t) {
                if (!isinstance<ValueError>(t)) throw;
                raise("ValueError", "Invalid file object: " + repr(fileobj));  // (a closed file)
            }
        }
        if (fd < 0) raise("ValueError", "Invalid file descriptor: " + std::to_string(fd));
        return fd;
    }
    // Python's _fileobj_lookup: also finds a registered file that has since been closed.
    std::int64_t lookup(const F& fileobj) const {
        try {
            return fileobj_to_fd(fileobj);
        } catch (const Thrown& t) {
            if (isinstance<ValueError>(t)) {
                for (const auto& key : s_->keys)
                    if (same(key.fileobj(), fileobj)) return key.fd();
            }
            throw;
        }
    }
    std::ptrdiff_t index_of(std::int64_t fd) const {
        for (std::size_t i = 0; i < s_->keys.size(); ++i)
            if (s_->keys[i].fd() == fd) return static_cast<std::ptrdiff_t>(i);
        return -1;
    }
    [[noreturn]] static void not_registered(const F& fileobj) {
        raise("KeyError", repr_str(repr(fileobj) + " is not registered"));
    }

public:
    Key register_(const F& fileobj, std::int64_t events, D data) const {
        check_events(events);
        std::int64_t fd = lookup(fileobj);
        if (index_of(fd) >= 0) {
            raise("KeyError", repr_str(repr(fileobj) + " (FD " + std::to_string(fd) + ") is already registered"));
        }
        if (fd > INT_MAX) raise("OverflowError", "file descriptor is greater than maximum");
        Key key(fileobj, fd, events, std::move(data));
        s_->keys.push_back(key);
        s_->fds.push_back(pollfd{static_cast<int>(fd), poll_events(events), 0});
        return key;
    }
    Key unregister(const F& fileobj) const {
        std::ptrdiff_t i = index_of(lookup(fileobj));
        if (i < 0) not_registered(fileobj);
        Key key = s_->keys[static_cast<std::size_t>(i)];
        s_->keys.erase(s_->keys.begin() + i);
        s_->fds.erase(s_->fds.begin() + i);
        return key;
    }
    // A new events mask or data for a registered file (it keeps its place in the order).
    Key modify(const F& fileobj, std::int64_t events, D data) const {
        std::ptrdiff_t i = index_of(lookup(fileobj));
        if (i < 0) not_registered(fileobj);
        check_events(events);
        auto at = static_cast<std::size_t>(i);
        s_->keys[at].set(events, std::move(data));
        s_->fds[at].events = poll_events(events);
        return s_->keys[at];
    }
    // (key, events) for each file that's ready, waiting at most `timeout` seconds (None: until
    // one is; 0 or less: not at all).
    list<std::tuple<Key, std::int64_t>> select(std::optional<double> timeout = std::nullopt) const {
        if (timeout && std::isnan(*timeout)) raise("ValueError", "Invalid value NaN (not a number)");
        std::optional<double> seconds;
        if (timeout) seconds = *timeout <= 0 ? 0.0 : std::ceil(*timeout * 1e3) / 1e3;
        std::vector<pollfd> fds = s_->fds;
        sd::select::poll_fds(fds, sd::select::Deadline(seconds));
        list<std::tuple<Key, std::int64_t>> ready;
        for (const auto& p : fds) {
            if (!p.revents) continue;
            std::ptrdiff_t i = index_of(p.fd);
            if (i < 0) continue;
            const Key& key = s_->keys[static_cast<std::size_t>(i)];
            // Like PollSelector: anything but "readable" (hang-up, error...) counts as writable
            // too, and anything but "writable" as readable, so a closed peer wakes a reader.
            std::int64_t events = ((p.revents & ~POLLIN) ? EVENT_WRITE : 0) | ((p.revents & ~POLLOUT) ? EVENT_READ : 0);
            ready.vec().emplace_back(key, events & key.events());
        }
        return ready;
    }
    Key get_key(const F& fileobj) const {
        if (s_->closed) raise("RuntimeError", "Selector is closed");
        std::ptrdiff_t i = index_of(lookup(fileobj));
        if (i < 0) not_registered(fileobj);
        return s_->keys[static_cast<std::size_t>(i)];
    }
    // The registrations by file descriptor (a copy, where Python's is a live view; empty
    // once the selector is closed, where Python's is None).
    dict<std::int64_t, Key> get_map() const {
        dict<std::int64_t, Key> out;
        for (const auto& key : s_->keys) out[key.fd()] = key;
        return out;
    }
    void close() const {
        s_->keys.clear();
        s_->fds.clear();
        s_->closed = true;
    }
    std::string sd_repr() const { return "<selectors.PollSelector object>"; }
};

}  // namespace sd::selectors
