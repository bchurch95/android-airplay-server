#include "ptp_client.h"
#include "audio_time.h"

#include <arpa/inet.h>
#include <endian.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#include <algorithm>
#include <cstring>
#include <android/log.h>

#define TAG "AirPlayPTP"
#define LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, TAG, __VA_ARGS__)
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN,  TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

#pragma pack(push, 1)
struct PtpHeader {
    uint8_t  messageTypeAndTransport; // 4 bits transportSpecific, 4 bits messageType
    uint8_t  versionPTP;              // 0x02 for PTPv2
    uint16_t messageLength;           // Total length in octets
    uint8_t  domainNumber;
    uint8_t  reserved1;
    uint16_t flagField;
    int64_t  correctionField;         // scaled nanoseconds (ns * 65536)
    uint32_t reserved2;
    uint8_t  sourcePortIdentity[10];
    uint16_t sequenceId;
    uint8_t  controlField;
    int8_t   logMessageInterval;
};

struct PtpTimestamp {
    uint16_t seconds_msb;
    uint32_t seconds_lsb;
    uint32_t nanoseconds;
};
#pragma pack(pop)

static inline int64_t parsePtpTimestampNs(const PtpTimestamp &ts) {
    uint64_t secs = ((uint64_t)ntohs(ts.seconds_msb) << 32) | ntohl(ts.seconds_lsb);
    return (int64_t)secs * NS_PER_SEC + ntohl(ts.nanoseconds);
}

PtpClient::PtpClient() = default;

PtpClient::~PtpClient() {
    stop();
}

bool PtpClient::start(int eventPort, int generalPort) {
    if (mRunning.load()) return true;

    mEventFd = socket(AF_INET, SOCK_DGRAM, 0);
    mGeneralFd = socket(AF_INET, SOCK_DGRAM, 0);

    if (mEventFd < 0 || mGeneralFd < 0) {
        LOGE("Failed to create UDP sockets for PTP: %s", strerror(errno));
        stop();
        return false;
    }

    // Reuse address and port
    int opt = 1;
    setsockopt(mEventFd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    setsockopt(mGeneralFd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
#ifdef SO_REUSEPORT
    setsockopt(mEventFd, SOL_SOCKET, SO_REUSEPORT, &opt, sizeof(opt));
    setsockopt(mGeneralFd, SOL_SOCKET, SO_REUSEPORT, &opt, sizeof(opt));
#endif

    // Bind event port (319 or custom)
    struct sockaddr_in eventAddr{};
    eventAddr.sin_family = AF_INET;
    eventAddr.sin_addr.s_addr = INADDR_ANY;
    eventAddr.sin_port = htons(eventPort);

    if (bind(mEventFd, (struct sockaddr *)&eventAddr, sizeof(eventAddr)) < 0) {
        LOGW("Could not bind PTP event port %d: %s (will use ephemeral fallback if needed)",
             eventPort, strerror(errno));
    } else {
        LOGI("Bound PTP event port %d", eventPort);
    }

    // Bind general port (320 or custom)
    struct sockaddr_in genAddr{};
    genAddr.sin_family = AF_INET;
    genAddr.sin_addr.s_addr = INADDR_ANY;
    genAddr.sin_port = htons(generalPort);

    if (bind(mGeneralFd, (struct sockaddr *)&genAddr, sizeof(genAddr)) < 0) {
        LOGW("Could not bind PTP general port %d: %s", generalPort, strerror(errno));
    } else {
        LOGI("Bound PTP general port %d", generalPort);
    }

    mRunning.store(true);
    mEventThread = std::thread(&PtpClient::eventLoop, this);
    mGeneralThread = std::thread(&PtpClient::generalLoop, this);

    return true;
}

void PtpClient::stop() {
    if (!mRunning.exchange(false)) return;

    if (mEventFd >= 0) {
        close(mEventFd);
        mEventFd = -1;
    }
    if (mGeneralFd >= 0) {
        close(mGeneralFd);
        mGeneralFd = -1;
    }

    if (mEventThread.joinable()) mEventThread.join();
    if (mGeneralThread.joinable()) mGeneralThread.join();
}

void PtpClient::eventLoop() {
    uint8_t buffer[1024];
    struct pollfd pfd{};
    pfd.fd = mEventFd;
    pfd.events = POLLIN;

    while (mRunning.load(std::memory_order_relaxed)) {
        int pollRes = poll(&pfd, 1, 500);
        if (pollRes <= 0 || !(pfd.revents & POLLIN)) continue;

        struct sockaddr_in srcAddr{};
        socklen_t addrLen = sizeof(srcAddr);
        ssize_t len = recvfrom(mEventFd, buffer, sizeof(buffer), 0,
                               (struct sockaddr *)&srcAddr, &addrLen);
        if (len < (ssize_t)sizeof(PtpHeader)) continue;

        int64_t t2 = monoNs(); // local arrival timestamp
        auto *hdr = reinterpret_cast<PtpHeader *>(buffer);
        uint8_t msgType = hdr->messageTypeAndTransport & 0x0F;

        if (msgType == 0x0) { // PTP SYNC message
            if (len >= (ssize_t)(sizeof(PtpHeader) + sizeof(PtpTimestamp))) {
                auto *ts = reinterpret_cast<PtpTimestamp *>(buffer + sizeof(PtpHeader));
                int64_t t1 = parsePtpTimestampNs(*ts);
                int64_t diff = t1 - t2;

                // Update smoothed moving average offset
                mOffsetHistory[mHistoryIdx] = diff;
                mHistoryIdx = (mHistoryIdx + 1) % FILTER_SIZE;
                if (mHistoryCount < FILTER_SIZE) mHistoryCount++;

                int64_t sum = 0;
                for (int i = 0; i < mHistoryCount; ++i) sum += mOffsetHistory[i];
                int64_t smoothed = sum / mHistoryCount;

                mOffsetNs.store(smoothed, std::memory_order_release);
                mSynced.store(true, std::memory_order_release);
            }
        }
    }
}

void PtpClient::generalLoop() {
    uint8_t buffer[1024];
    struct pollfd pfd{};
    pfd.fd = mGeneralFd;
    pfd.events = POLLIN;

    while (mRunning.load(std::memory_order_relaxed)) {
        int pollRes = poll(&pfd, 1, 500);
        if (pollRes <= 0 || !(pfd.revents & POLLIN)) continue;

        struct sockaddr_in srcAddr{};
        socklen_t addrLen = sizeof(srcAddr);
        ssize_t len = recvfrom(mGeneralFd, buffer, sizeof(buffer), 0,
                               (struct sockaddr *)&srcAddr, &addrLen);
        if (len < (ssize_t)sizeof(PtpHeader)) continue;

        auto *hdr = reinterpret_cast<PtpHeader *>(buffer);
        uint8_t msgType = hdr->messageTypeAndTransport & 0x0F;

        if (msgType == 0x8) { // PTP FOLLOW_UP message
            if (len >= (ssize_t)(sizeof(PtpHeader) + sizeof(PtpTimestamp))) {
                auto *ts = reinterpret_cast<PtpTimestamp *>(buffer + sizeof(PtpHeader));
                int64_t preciseT1 = parsePtpTimestampNs(*ts);
                int64_t correction = (int64_t)(be64toh((uint64_t)hdr->correctionField) >> 16);
                preciseT1 += correction;
                // Refine offset with precise timestamp
            }
        }
    }
}
