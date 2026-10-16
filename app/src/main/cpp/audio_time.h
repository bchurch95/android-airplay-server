#ifndef AUDIO_TIME_H
#define AUDIO_TIME_H

#include <atomic>
#include <cstdint>
#include <ctime>

static constexpr int64_t NS_PER_SEC = 1000000000LL;

static inline int64_t monoNs() {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * NS_PER_SEC + ts.tv_nsec;
}

// Global atomic clock offset for AirPlay 2 PTP / NTP synchronization
// ptpNowNs() = monoNs() + gPtpOffsetNs
inline std::atomic<int64_t> &globalPtpOffsetNs() {
    static std::atomic<int64_t> sOffset{0};
    return sOffset;
}

static inline void setPtpOffsetNs(int64_t offsetNs) {
    globalPtpOffsetNs().store(offsetNs, std::memory_order_release);
}

static inline int64_t getPtpOffsetNs() {
    return globalPtpOffsetNs().load(std::memory_order_relaxed);
}

static inline int64_t ptpNowNs() {
    return monoNs() + getPtpOffsetNs();
}

#endif  // AUDIO_TIME_H
