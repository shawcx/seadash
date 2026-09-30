// The `time` module: clocks and sleep.
#pragma once

#include <chrono>
#include <thread>

namespace sd::time {

inline double perf_counter() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}
inline double monotonic() { return perf_counter(); }
inline double time() {
    using namespace std::chrono;
    return duration<double>(system_clock::now().time_since_epoch()).count();
}
inline void sleep(double seconds) {
    if (seconds < 0) raise("ValueError", "sleep length must be non-negative");
    std::this_thread::sleep_for(std::chrono::duration<double>(seconds));
}

}  // namespace sd::time
