#ifndef TIMELINE_BUFFER_H
#define TIMELINE_BUFFER_H

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>

#include "audio_time.h"

/*
 * lock-free SPSC ring of int16 samples (interleaved frames); positions are
 * non-wrapping 64-bit counters so empty/full are unambiguous without spare slot
 */
class SpscRing {
public:
    explicit SpscRing(size_t capacitySamples)
        : mCapacity(capacitySamples), mBuf(new int16_t[capacitySamples]) {}

    size_t available() const {  // samples readable
        return mWrite.load(std::memory_order_acquire) - mRead.load(std::memory_order_acquire);
    }

    // producer: push up to n samples, drops what doesn't fit
    size_t write(const int16_t *src, size_t n) {
        const size_t w = mWrite.load(std::memory_order_relaxed);
        const size_t r = mRead.load(std::memory_order_acquire);
        const size_t freeSpace = mCapacity - (w - r);
        if (n > freeSpace) n = freeSpace;
        const size_t wi = w % mCapacity;
        const size_t first = (n < mCapacity - wi) ? n : (mCapacity - wi);
        memcpy(&mBuf[wi], src, first * sizeof(int16_t));
        if (n > first) memcpy(&mBuf[0], src + first, (n - first) * sizeof(int16_t));
        mWrite.store(w + n, std::memory_order_release);
        return n;
    }

    // consumer: pop up to n samples
    size_t read(int16_t *dst, size_t n) {
        const size_t r = mRead.load(std::memory_order_relaxed);
        const size_t w = mWrite.load(std::memory_order_acquire);
        const size_t avail = w - r;
        if (n > avail) n = avail;
        const size_t ri = r % mCapacity;
        const size_t first = (n < mCapacity - ri) ? n : (mCapacity - ri);
        memcpy(dst, &mBuf[ri], first * sizeof(int16_t));
        if (n > first) memcpy(dst + first, &mBuf[0], (n - first) * sizeof(int16_t));
        mRead.store(r + n, std::memory_order_release);
        return n;
    }

    // consumer: discard up to n oldest samples
    void skip(size_t n) {
        const size_t r = mRead.load(std::memory_order_relaxed);
        const size_t w = mWrite.load(std::memory_order_acquire);
        const size_t avail = w - r;
        if (n > avail) n = avail;
        mRead.store(r + n, std::memory_order_release);
    }

private:
    const size_t mCapacity;
    std::unique_ptr<int16_t[]> mBuf;
    std::atomic<size_t> mWrite{0};
    std::atomic<size_t> mRead{0};
};

/*
 * adaptive jitter cushion tuner, NetEQ-inspired but simplified: decaying histogram of
 * packet jitter, cushion = high percentile of it + 1 packet of margin.
 * observe() is producer-thread only; result published via atomic, consumer reads it live
 */
class DelayTracker {
public:
    // staticCushionMs > 0: fixed cushion, tuner disabled
    // staticCushionMs <= 0: tune between MIN_CUSHION_MS and MAX_CUSHION_MS
    DelayTracker(int sampleRate, int channels, int staticCushionMs, int percentilePct, bool lowLatency = false)
        : mSampleRate(sampleRate), mChannels(channels),
          mAdaptive(staticCushionMs <= 0),
          mFloor(msToSamples(sampleRate, channels, mAdaptive ? (lowLatency ? MIN_CUSHION_LOW_LATENCY_MS : MIN_CUSHION_MUSIC_MS) : staticCushionMs)),
          mCeil(msToSamples(sampleRate, channels, mAdaptive ? MAX_CUSHION_MS : staticCushionMs)),
          mPercentilePct(percentilePct),
          mTuned(mFloor) {}

    // ptsNs: packet presentation time; arrivalNs: local arrival time; durNs: packet duration
    void observe(int64_t ptsNs, int64_t arrivalNs, int64_t durNs) {
        if (!mAdaptive) return;

        const int64_t raw = arrivalNs - ptsNs; // arrival latency
        // jitter = latency relative to min latency seen so far (mBaseNs)
        if (!mHaveBase) { mBaseNs = raw; mHaveBase = true; }
        else if (raw < mBaseNs) mBaseNs = raw;
        // creep mBaseNs up slowly: counters clock drift and one freak fast packet
        else mBaseNs += (raw - mBaseNs) >> BASE_CREEP_SHIFT;
        int64_t rel = raw - mBaseNs;
        if (rel < 0) rel = 0;

        int bin = (int)(rel / BUCKET_NS);
        if (bin >= NBUCKETS) bin = NBUCKETS - 1;

        // decay: each bin bleeds 1/2^FORGET_SHIFT per packet, current bin topped up by
        // ONE/2^FORGET_SHIFT, so total converges to ONE
        int64_t sum = 0;
        for (int i = 0; i < NBUCKETS; i++) {
            mHist[i] -= (mHist[i] + (1 << FORGET_SHIFT) - 1) >> FORGET_SHIFT;
            sum += mHist[i];
        }
        mHist[bin] += ONE >> FORGET_SHIFT;
        sum += ONE >> FORGET_SHIFT;

        // cushion = smallest delay whose cumulative histogram share reaches percentile
        const int64_t thresh = sum * mPercentilePct / 100;
        int64_t cum = 0; int b = 0;
        for (; b < NBUCKETS - 1; b++) { cum += mHist[b]; if (cum >= thresh) break; }
        // bin is lower bound on delay: take top edge + margin. margin must keep output
        // fed for one data callback and tide us over until next packet
        const int64_t marginNs = std::max(durNs, mOutputBufferNs.load(std::memory_order_relaxed));
        const int64_t targetNs = (int64_t)(b + 1) * BUCKET_NS + marginNs;
        size_t samples = (size_t)(targetNs * mSampleRate / NS_PER_SEC) * mChannels;
        if (samples < mFloor) samples = mFloor;
        if (samples > mCeil) samples = mCeil;
        mTuned.store(samples, std::memory_order_relaxed);
    }

    // reset clock base after discontinuity or clock shift/resync so it doesn't poison
    // jitter calculations; producer-thread only (same as observe())
    void reanchor() { mHaveBase = false; }

    // latest tuned cushion in samples; any thread
    size_t target() const { return mTuned.load(std::memory_order_relaxed); }

    void noteOutputBufferFrames(int frames) {
        mOutputBufferNs.store((int64_t)frames * NS_PER_SEC / mSampleRate, std::memory_order_relaxed);
    }

    // largest cushion tuner can reach, in samples
    size_t ceil() const { return mCeil; }

private:
    static size_t msToSamples(int sampleRate, int channels, int ms) {
        return (size_t)sampleRate * ms / 1000 * channels;
    }

    static constexpr int MIN_CUSHION_LOW_LATENCY_MS = 100; // ~2.5 Portal bursts
    static constexpr int MIN_CUSHION_MUSIC_MS = 250;       // standard AirPlay audio latency buffer
    static constexpr int MAX_CUSHION_MS = 400;           // Max adaptive jitter cushion (400ms)
    static constexpr int BUCKET_MS = 10;                 // histogram granularity
    static constexpr int NBUCKETS = MAX_CUSHION_MS / BUCKET_MS;
    static constexpr int64_t BUCKET_NS = (int64_t)BUCKET_MS * 1000000LL;
    static constexpr int FORGET_SHIFT = 8;               // ~1/256 decay per packet (~6s @ 23ms)
    static constexpr int BASE_CREEP_SHIFT = 10;          // mBaseNs creep speed
    static constexpr int64_t ONE = 1 << 20;              // histogram fixed-point unit

    const int mSampleRate;
    const int mChannels;
    const bool mAdaptive;                // false = fixed cushion (tuner disabled)
    const size_t mFloor;
    const size_t mCeil;
    const int mPercentilePct;            // arrival-delay percentile cushion targets
    std::atomic<size_t> mTuned;
    std::atomic<int64_t> mOutputBufferNs{0};  // output->producer: oboe output buffer duration
    int64_t mHist[NBUCKETS] = {};
    int64_t mBaseNs = 0;
    bool mHaveBase = false;
};

// cumulative diagnostic counters, bumped from producer/consumer, read from stats thread
class TimelineMetrics {
public:
    // packed: nests into AudioDebugData with fixed layout
    struct __attribute__((packed)) Debug {
        uint32_t trims, drops, silences, underruns;  // cumulative; 32-bit to avoid wrap
    };

    void countTrim()     { mTrims.fetch_add(1, std::memory_order_relaxed); }
    void countDrop()     { mDrops.fetch_add(1, std::memory_order_relaxed); }
    void countSilence()  { mSilences.fetch_add(1, std::memory_order_relaxed); }
    void countUnderrun() { mUnderruns.fetch_add(1, std::memory_order_relaxed); }

    // any thread
    Debug debugInfo() const {
        Debug d{};
        d.trims = mTrims.load(std::memory_order_relaxed);
        d.drops = mDrops.load(std::memory_order_relaxed);
        d.silences = mSilences.load(std::memory_order_relaxed);
        d.underruns = mUnderruns.load(std::memory_order_relaxed);
        return d;
    }

private:
    std::atomic<uint32_t> mTrims{0};      // backlog exceeded cap and was trimmed
    std::atomic<uint32_t> mDrops{0};      // late packets dropped, pts slot already passed
    std::atomic<uint32_t> mSilences{0};   // gaps filled with silence
    std::atomic<uint32_t> mUnderruns{0};  // ring ran dry, output padded with silence
};

/*
 * Timeline-synchronized playout buffer: absorbs jitter, clock drift and clock resyncs,
 * syncs audio to its presentation-time tag.
 *
 * One concurrent reader + one concurrent writer OK; multiple readers or writers NOT safe.
 * samples = frames * channels
 */
class TimelineBuffer {
public:
    // cushion policy lives in mTracker; timeline reads mTracker.target() live
    TimelineBuffer(int sampleRate, int channels, int staticCushionMs, int percentilePct, bool lowLatency = false)
        : mSampleRate(sampleRate),
          mChannels(channels),
          mLowLatency(lowLatency),
          mTracker(sampleRate, channels, staticCushionMs, percentilePct, lowLatency),
          // ring can't be reallocated mid-stream: size for worst-case backlog, i.e. cap
          // of largest reachable cushion + one throttle window of overshoot (trims are
          // throttled), 2x slack
          mRing(std::max<size_t>((size_t)sampleRate * 8 * channels, 2 * capOf(mTracker.ceil()) + (size_t)sampleRate * TRIM_THROTTLE_MS / 1000 * channels)) {}

    void noteOutputBufferFrames(int frames) { mTracker.noteOutputBufferFrames(frames); }

    // producer: push samples
    void write(const int16_t *pcm, size_t samples, int64_t ptsNs) {
        if (samples == 0) return;
        // pts 0 = sender clock not NTP synced yet
        if (ptsNs == 0) {
            mRing.write(pcm, samples);
            return;
        }
        const int64_t durNs = samples * NS_PER_SEC / mChannels / mSampleRate;

        // If ring was empty or head pts was cleared, track presentation time at head
        if (mRing.available() == 0 || mHeadPtsNs.load(std::memory_order_relaxed) == 0) {
            mHeadPtsNs.store(ptsNs, std::memory_order_release);
        }

        // consumer underran since last write: it already played the gap as real-time
        // silence, re-anchor so we don't also insert it here
        if (mUnderran.exchange(false, std::memory_order_relaxed)) {
            mExpectedPtsNs = ptsNs;
        }
        const int64_t gapNs = ptsNs - mExpectedPtsNs;

        // large gap: possible discontinuity or clock shift. reanchor() must precede
        // observe() so the reset applies to this observation
        if (gapNs > MAX_GAP_NS || gapNs < -MAX_GAP_NS) {
            mTracker.reanchor();
            mHeadPtsNs.store(ptsNs, std::memory_order_release);
        }

        // no-op in static-cushion mode
        mTracker.observe(ptsNs, monoNs(), durNs);

        if (gapNs > MAX_GAP_NS || gapNs < -MAX_GAP_NS) {
            // discontinuity: write immediately. new sound after long silence resets
            // backlog to ideal size (less latency); on clock resync we keep the sound
        } else if (gapNs > SLACK_NS) {
            // sender left gap: reproduce as silence so timing is exact
            const size_t silenceFrames = (size_t)(gapNs * mSampleRate / NS_PER_SEC);
            writeSilenceFrames(silenceFrames);
            mMetrics.countSilence();
        } else if (gapNs < -SLACK_NS) {
            // slot already passed (late/overlap): drop
            mMetrics.countDrop();
            return;
        }
        mRing.write(pcm, samples);
        mExpectedPtsNs = ptsNs + durNs;
    }

    // thread-safe flush request from producer/control thread; executed on consumer thread
    void requestFlush() {
        mFlushRequested.store(true, std::memory_order_release);
    }

    // consumer: pop frames*channels samples into out, silence-padded on underrun
    void read(int16_t *out, int32_t frames, int64_t hardwareLatencyNs = 0) {
        if (mFlushRequested.exchange(false, std::memory_order_acquire)) {
            mRing.skip(mRing.available());
            mHeadPtsNs.store(0, std::memory_order_release);
            mPriming = true;
            mPlaying = false;
            mPrimeSilenceFrames = 0;
            mAboveCapSinceNs = 0;
            mUnderran.store(true, std::memory_order_relaxed);
        }
        const size_t need = (size_t)frames * mChannels;
        const size_t tuned = mTracker.target();
        const int64_t now = monoNs();

        int64_t headPts = mHeadPtsNs.load(std::memory_order_acquire);

        // Playout time gating (AirPlay 2 presentation timestamps)
        if (headPts > 0 && !mPlaying) {
            const int64_t playoutNowNs = now + hardwareLatencyNs;
            const int64_t leadNs = headPts - playoutNowNs;
            const int64_t burstNs = (int64_t)frames * NS_PER_SEC / mSampleRate;

            if (leadNs > burstNs) {
                // Scheduled in future (> 1 callback buffer ahead): output silence while waiting
                memset(out, 0, need * sizeof(int16_t));
                return;
            } else if (leadNs > 0) {
                // Initial presentation alignment: pad leadSilenceFrames at the start of the first burst
                size_t leadSilenceFrames = (size_t)(leadNs * mSampleRate / NS_PER_SEC);
                if (leadSilenceFrames > (size_t)frames) leadSilenceFrames = (size_t)frames;
                const size_t silenceSamples = leadSilenceFrames * mChannels;
                memset(out, 0, silenceSamples * sizeof(int16_t));
                const size_t audioFrames = (size_t)frames - leadSilenceFrames;
                const size_t audioNeed = audioFrames * mChannels;
                const size_t got = mRing.read(out + silenceSamples, audioNeed);
                if (got < audioNeed) {
                    memset(out + silenceSamples + got, 0, (audioNeed - got) * sizeof(int16_t));
                }
                headPts += (int64_t)audioFrames * NS_PER_SEC / mSampleRate;
                mHeadPtsNs.store(headPts, std::memory_order_relaxed);
                mPlaying = true;
                mPriming = false;
                return;
            } else {
                // Initial presentation time has arrived (leadNs <= 0): start continuous playback
                mPlaying = true;
                mPriming = false;
            }
        }

        // Catastrophic desync check during playback (> 500ms behind): flush and re-anchor
        if (mPlaying && headPts > 0) {
            const int64_t playoutNowNs = now + hardwareLatencyNs;
            const int64_t lagNs = playoutNowNs - headPts;
            if (lagNs > 500000000LL) {
                mRing.skip(mRing.available());
                mHeadPtsNs.store(0, std::memory_order_release);
                mPlaying = false;
                mPriming = true;
                memset(out, 0, need * sizeof(int16_t));
                return;
            }
        }

        const size_t avail = mRing.available();
        if (avail > capOf(tuned)) {
            if (mAboveCapSinceNs == 0) mAboveCapSinceNs = now;
        } else {
            mAboveCapSinceNs = 0;
        }
        if (mAboveCapSinceNs != 0 && now - mAboveCapSinceNs >= TRIM_SUSTAIN_NS
                && now - mLastTrimBlockNs >= TRIM_THROTTLE_NS) {
            size_t trimmed = avail - tuned;
            mRing.skip(trimmed);
            if (headPts > 0) {
                headPts += (int64_t)(trimmed / mChannels) * NS_PER_SEC / mSampleRate;
                mHeadPtsNs.store(headPts, std::memory_order_relaxed);
            }
            mMetrics.countTrim();
            mLastTrimBlockNs = now;
            mAboveCapSinceNs = 0;
        }

        // prebuffer: hold output until cushion fills (for unscheduled audio where headPts == 0)
        if (mPriming && headPts == 0) {
            const size_t buffered = mRing.available();
            if (buffered == 0) mPrimeSilenceFrames = 0;
            else mPrimeSilenceFrames += (uint32_t)frames;
            const uint32_t starveFrames = (uint32_t)(2 * tuned / mChannels);
            const bool starved = buffered > 0 && mPrimeSilenceFrames >= starveFrames;
            if (buffered == 0 || (buffered < tuned && !starved)) {
                memset(out, 0, need * sizeof(int16_t));
                return;
            }
            mPriming = false;
            mPrimeSilenceFrames = 0;
        }

        const size_t got = mRing.read(out, need);
        if (got < need) {
            memset(out + got, 0, (need - got) * sizeof(int16_t));
            mPriming = true;
            mPlaying = false;
            mLastTrimBlockNs = now;  // hold off trims while rebuilding
            mUnderran.store(true, std::memory_order_relaxed);  // producer re-anchors
            mHeadPtsNs.store(0, std::memory_order_release);
            mMetrics.countUnderrun();
        } else if (headPts > 0) {
            headPts += (int64_t)frames * NS_PER_SEC / mSampleRate;
            mHeadPtsNs.store(headPts, std::memory_order_relaxed);
        }
    }

    // call after discontinuity or clock shift/resync; producer-side, must not run
    // concurrently with write()
    void reanchorTracker() { mTracker.reanchor(); }

    // rebuild prebuffer cushion before resuming, e.g. after output stream restart;
    // must not run concurrently with read()
    void reprime() { mPriming = true; }

    // drop buffered audio + rebuild cushion, so resume after pause doesn't play stale
    // tail; only while output callback is stopped (skip() is consumer-side)
    void flushAndReprime() {
        mRing.skip(mRing.available());
        mHeadPtsNs.store(0, std::memory_order_release);
        mPlaying = false;
        mPriming = true;
    }

    // debug snapshot: backlog + tuned cushion (ms) + counters; reads only atomics, any thread
    struct __attribute__((packed)) Debug {
        uint16_t backlogMs;         // bounded by ring (few seconds)
        uint16_t tunedCushionMs;    // bounded by MAX_CUSHION_MS
        TimelineMetrics::Debug metrics;
    };
    Debug debugInfo() const {
        Debug d{};
        d.backlogMs = (uint16_t)(mRing.available() / mChannels * 1000 / mSampleRate);
        d.tunedCushionMs = (uint16_t)(mTracker.target() / mChannels * 1000 / mSampleRate);
        d.metrics = mMetrics.debugInfo();
        return d;
    }

private:

    // latency ceiling for cushion: 2x, but never below TRIM_FLOOR_MS so tiny cushion
    // still gets slack before a trim (trim costs a glitch)
    size_t capOf(size_t cushionSamples) const {
        if (!mLowLatency || mExpectedPtsNs == 0) {
            // In music streaming, sender buffers 1-2s ahead; never trim valid buffered audio under 3500ms
            return (size_t)mSampleRate * 3500 / 1000 * mChannels;
        }
        return std::max(cushionSamples * 2, (size_t)mSampleRate * TRIM_FLOOR_MS / 1000 * mChannels);
    }

    void writeSilenceFrames(size_t frames) {
        int16_t zeros[512] = {0};  // 256 stereo frames per chunk
        size_t samples = frames * mChannels;
        while (samples > 0) {
            const size_t n = std::min<size_t>(samples, 512);
            mRing.write(zeros, n);
            samples -= n;
        }
    }

    static constexpr int64_t SLACK_NS = 10'000'000LL;    // ignore <10ms gaps (NTP shift, rounding)
    static constexpr int64_t MAX_GAP_NS = 750'000'000LL; // >750ms = discontinuity, re-anchor
    static constexpr int TRIM_FLOOR_MS = 160;             // min trim point even for tiny cushion
    static constexpr int TRIM_THROTTLE_MS = 2000;        // min spacing between trims, and after underrun
    static constexpr int64_t TRIM_THROTTLE_NS = (int64_t)TRIM_THROTTLE_MS * 1'000'000LL;
    static constexpr int TRIM_SUSTAIN_MS = 2000;         // backlog must exceed cap this long before trim
    static constexpr int64_t TRIM_SUSTAIN_NS = (int64_t)TRIM_SUSTAIN_MS * 1'000'000LL;

    const int mSampleRate;
    const int mChannels;
    const bool mLowLatency;
    DelayTracker mTracker;               // owns cushion policy; read live each read()
    SpscRing mRing;
    TimelineMetrics mMetrics;
    bool mPriming = true;                // stream start: buffer output to build cushion
    bool mPlaying = false;               // active playout state after initial presentation gating
    uint32_t mPrimeSilenceFrames = 0;    // consumer-only: silence frames output while priming with partial data
    int64_t mLastTrimBlockNs = 0;        // consumer-only: last trim/underrun time (trim throttle)
    int64_t mAboveCapSinceNs = 0;        // consumer-only: when backlog first exceeded cap (0 = under)
    std::atomic<bool> mUnderran{false};  // consumer->producer: underrun happened
    std::atomic<bool> mFlushRequested{false}; // producer->consumer: flush requested
    std::atomic<int64_t> mHeadPtsNs{0};        // presentation time at head of ring buffer

    int64_t mExpectedPtsNs = 0;          // presentation time expected at write head
};

#endif  // TIMELINE_BUFFER_H
