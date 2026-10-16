#ifndef PTP_CLIENT_H
#define PTP_CLIENT_H

#include <atomic>
#include <cstdint>
#include <memory>
#include <thread>
#include <string>

/**
 * IEEE 1588-2008 (PTPv2) Clock Synchronizer for AirPlay 2 Multi-Room Audio.
 * 
 * Listens on UDP port 319 (Event messages: Sync, Follow_Up, Delay_Req) and
 * UDP port 320 (General messages: Announce, Delay_Resp).
 * 
 * Tracks the master clock offset so the local audio engine can translate
 * presentation timestamps (PTS) into local DAC presentation time with
 * microsecond accuracy.
 */
class PtpClient {
public:
    PtpClient();
    ~PtpClient();

    bool start(int eventPort = 319, int generalPort = 320);
    void stop();

    bool isRunning() const { return mRunning.load(std::memory_order_relaxed); }
    bool isSynchronized() const { return mSynced.load(std::memory_order_relaxed); }

    // Master-to-slave offset in nanoseconds: local_ptp_time = local_mono_time + offset
    int64_t getOffsetNs() const { return mOffsetNs.load(std::memory_order_relaxed); }
    int64_t getRoundTripDelayNs() const { return mRoundTripNs.load(std::memory_order_relaxed); }

    // Convert local monotonic timestamp (monoNs()) to master PTP timestamp
    int64_t toPtpNs(int64_t monoNs) const {
        return monoNs + mOffsetNs.load(std::memory_order_relaxed);
    }

    // Convert master PTP presentation timestamp to local monotonic clock
    int64_t toMonoNs(int64_t ptpNs) const {
        return ptpNs - mOffsetNs.load(std::memory_order_relaxed);
    }

private:
    void eventLoop();
    void generalLoop();

    std::atomic<bool> mRunning{false};
    std::atomic<bool> mSynced{false};
    std::atomic<int64_t> mOffsetNs{0};
    std::atomic<int64_t> mRoundTripNs{0};

    int mEventFd{-1};
    int mGeneralFd{-1};

    std::thread mEventThread;
    std::thread mGeneralThread;

    // Moving average filter for clock offset smoothing
    static constexpr int FILTER_SIZE = 8;
    int64_t mOffsetHistory[FILTER_SIZE]{0};
    int mHistoryIdx{0};
    int mHistoryCount{0};
};

#endif // PTP_CLIENT_H
