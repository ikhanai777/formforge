// Output sinks for rendered frames: MP4 (Media Foundation H.264), PNG sequence,
// GIF, and optional ffmpeg formats. Frames arrive as tightly packed RGBA8.
#pragma once

#include "scene.h"

#include <atomic>
#include <condition_variable>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace spindle {

struct SinkConfig {
    Format format = Format::MP4;
    std::string path;  // final output path (PNG: base name; frames get _0000 suffixes)
    int width = 0, height = 0;
    int fps = 30;
    int frameCount = 0;
    float bitrateMbps = 0;  // 0 = automatic
    bool alpha = false;      // frames carry meaningful straight alpha
    std::string ffmpegPath;  // for WebM / ProRes / HEVC
};

class FrameSink {
public:
    virtual ~FrameSink() = default;
    virtual bool begin(const SinkConfig& cfg, std::string& error) = 0;
    // Frames are written in order, except where parallel() allows otherwise.
    virtual bool write(int frameIndex, const uint8_t* rgba, std::string& error) = 0;
    virtual bool finish(std::string& error) = 0;  // finalises and moves the file into place
    virtual void abort() = 0;                     // discards a partial video (PNG frames are kept)
    virtual bool parallel() const { return false; }
};

std::unique_ptr<FrameSink> createSink(Format format);

// Path helpers.
std::string pngFramePath(const std::string& base, int index);
float automaticBitrateMbps(int width, int height, int fps);

// Runs a sink on worker threads behind a bounded queue, so the GPU never waits
// on the encoder for long and memory stays flat.
class EncoderQueue {
public:
    EncoderQueue(std::unique_ptr<FrameSink> sink, size_t capacity = 8);
    ~EncoderQueue();
    // Opens the sink. Media Foundation objects live in the multithreaded COM
    // apartment, so begin/write/finish all run on MTA threads, never on the
    // (possibly single-threaded) UI thread.
    bool begin(const SinkConfig& cfg, std::string& error);
    // Blocks while the queue is full. Returns false once an error has occurred.
    bool push(int frameIndex, std::vector<uint8_t>&& rgba);
    bool finish(std::string& error);
    void abort();
    bool failed(std::string* error = nullptr);
    int framesWritten() const { return written_; }

private:
    void worker();
    std::unique_ptr<FrameSink> sink_;
    size_t capacity_;
    std::mutex mutex_;
    std::condition_variable notEmpty_, notFull_;
    std::deque<std::pair<int, std::vector<uint8_t>>> queue_;
    std::vector<std::thread> threads_;
    bool closing_ = false;
    bool aborted_ = false;
    std::string error_;
    std::atomic<int> written_{0};
    int busy_ = 0;
};

}  // namespace spindle
