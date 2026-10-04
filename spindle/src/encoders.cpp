#include "encoders.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <codecapi.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfreadwrite.h>

#include <atomic>
#include <cstdio>
#include <cstring>

#define STB_IMAGE_WRITE_IMPLEMENTATION
#define STBIW_WINDOWS_UTF8
#include "../third_party/stb/stb_image_write.h"

#define MSF_GIF_IMPL
#include "../third_party/msf_gif/msf_gif.h"

#include "gpu.h"  // ComPtr

namespace spindle {

float automaticBitrateMbps(int width, int height, int fps) {
    // ~0.2 bits per pixel: 1080p30 is about 12 Mbps, 4K30 about 50 Mbps.
    double bps = 0.2 * (double)width * height * fps;
    return (float)std::min(100.0, std::max(2.0, bps / 1e6));
}

std::string pngFramePath(const std::string& base, int index) {
    std::string stem = base;
    if (toLower(extensionOf(stem)) == ".png") stem = stem.substr(0, stem.size() - 4);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "_%04d.png", index);
    return stem + buf;
}

static std::string tempPathFor(const std::string& path) {
    // Keep the extension so Media Foundation and ffmpeg still pick the container.
    std::string ext = extensionOf(path);
    std::string stem = path.substr(0, path.size() - ext.size());
    return stem + ".partial" + ext;
}

static bool moveIntoPlace(const std::string& from, const std::string& to, std::string& error) {
    if (!MoveFileExW(widen(from).c_str(), widen(to).c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_COPY_ALLOWED)) {
        error = "Could not move the finished file to " + to + " (is it open in another program?)";
        return false;
    }
    return true;
}

// ---------------------------------------------------------------------------
// MP4 / H.264 via Media Foundation
// ---------------------------------------------------------------------------

class Mp4Sink : public FrameSink {
public:
    ~Mp4Sink() override { abort(); }

    bool begin(const SinkConfig& cfg, std::string& error) override {
        cfg_ = cfg;
        if (cfg.width % 2 || cfg.height % 2) {
            error = "H.264 needs an even width and height.";
            return false;
        }
        if (cfg.width > 4096 || cfg.height > 4096) {
            error = "H.264 output is limited to 4096 pixels per side; use PNG for larger frames.";
            return false;
        }
        if (FAILED(MFStartup(MF_VERSION, MFSTARTUP_NOSOCKET))) {
            error = "Media Foundation is not available (Windows N editions need the Media Feature Pack).";
            return false;
        }
        mfStarted_ = true;
        temp_ = tempPathFor(cfg.path);
        DeleteFileW(widen(temp_).c_str());
        // Hardware encoders (e.g. Intel Quick Sync) first, then Microsoft's software encoder.
        std::string firstError;
        if (open(true, error)) return true;
        firstError = error;
        logf("H.264 with hardware transforms failed (%s); retrying in software", error.c_str());
        writer_ = nullptr;
        DeleteFileW(widen(temp_).c_str());
        if (open(false, error)) return true;
        return false;
    }

    bool write(int, const uint8_t* rgba, std::string& error) override {
        const int w = cfg_.width, h = cfg_.height;
        const DWORD size = (DWORD)(w * h * 3 / 2);
        ComPtr<IMFMediaBuffer> buffer;
        if (FAILED(MFCreateMemoryBuffer(size, buffer.put()))) {
            error = "Out of memory (video buffer).";
            return false;
        }
        BYTE* dst = nullptr;
        buffer->Lock(&dst, nullptr, nullptr);
        rgbaToNV12(rgba, dst, w, h);
        buffer->Unlock();
        buffer->SetCurrentLength(size);
        ComPtr<IMFSample> sample;
        MFCreateSample(sample.put());
        sample->AddBuffer(buffer.get());
        LONGLONG t0 = (LONGLONG)(frame_ * 10000000LL / cfg_.fps);
        LONGLONG t1 = (LONGLONG)((frame_ + 1) * 10000000LL / cfg_.fps);
        sample->SetSampleTime(t0);
        sample->SetSampleDuration(t1 - t0);
        HRESULT hr = writer_->WriteSample(stream_, sample.get());
        if (FAILED(hr)) {
            char b[96];
            std::snprintf(b, sizeof(b), "The H.264 encoder rejected frame %lld (hr=0x%08lx).", frame_, (unsigned long)hr);
            error = b;
            return false;
        }
        ++frame_;
        return true;
    }

    bool finish(std::string& error) override {
        if (!writer_) {
            error = "The video was not started.";
            return false;
        }
        HRESULT hr = writer_->Finalize();
        writer_ = nullptr;
        shutdownMF();
        if (FAILED(hr)) {
            error = "Finalising the MP4 failed.";
            DeleteFileW(widen(temp_).c_str());
            return false;
        }
        return moveIntoPlace(temp_, cfg_.path, error);
    }

    void abort() override {
        if (writer_) {
            writer_ = nullptr;
            DeleteFileW(widen(temp_).c_str());
        }
        shutdownMF();
    }

private:
    bool open(bool hardware, std::string& error) {
        ComPtr<IMFAttributes> attrs;
        MFCreateAttributes(attrs.put(), 4);
        attrs->SetUINT32(MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, hardware ? TRUE : FALSE);
        attrs->SetUINT32(MF_SINK_WRITER_DISABLE_THROTTLING, TRUE);
        attrs->SetGUID(MF_TRANSCODE_CONTAINERTYPE, MFTranscodeContainerType_MPEG4);
        HRESULT hr = MFCreateSinkWriterFromURL(widen(temp_).c_str(), nullptr, attrs.get(), writer_.put());
        if (FAILED(hr)) {
            error = "Could not create " + temp_ + " (check the folder is writable).";
            return false;
        }
        const UINT32 bitrate = (UINT32)((cfg_.bitrateMbps > 0 ? cfg_.bitrateMbps
                                                               : automaticBitrateMbps(cfg_.width, cfg_.height, cfg_.fps)) * 1e6);
        ComPtr<IMFMediaType> out;
        MFCreateMediaType(out.put());
        out->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
        out->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
        out->SetUINT32(MF_MT_AVG_BITRATE, bitrate);
        out->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        out->SetUINT32(MF_MT_MPEG2_PROFILE, eAVEncH264VProfile_High);
        MFSetAttributeSize(out.get(), MF_MT_FRAME_SIZE, (UINT32)cfg_.width, (UINT32)cfg_.height);
        MFSetAttributeRatio(out.get(), MF_MT_FRAME_RATE, (UINT32)cfg_.fps, 1);
        MFSetAttributeRatio(out.get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
        out->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
        out->SetUINT32(MF_MT_VIDEO_PRIMARIES, MFVideoPrimaries_BT709);
        out->SetUINT32(MF_MT_TRANSFER_FUNCTION, MFVideoTransFunc_709);
        out->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235);
        if (FAILED(hr = writer_->AddStream(out.get(), &stream_))) {
            error = describe("The H.264 encoder rejected the output format", hr);
            return false;
        }
        ComPtr<IMFMediaType> in;
        MFCreateMediaType(in.put());
        in->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
        in->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
        in->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        MFSetAttributeSize(in.get(), MF_MT_FRAME_SIZE, (UINT32)cfg_.width, (UINT32)cfg_.height);
        MFSetAttributeRatio(in.get(), MF_MT_FRAME_RATE, (UINT32)cfg_.fps, 1);
        MFSetAttributeRatio(in.get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
        in->SetUINT32(MF_MT_DEFAULT_STRIDE, (UINT32)cfg_.width);
        in->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
        in->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235);
        if (FAILED(hr = writer_->SetInputMediaType(stream_, in.get(), nullptr))) {
            error = describe("The H.264 encoder rejected the input format", hr);
            return false;
        }
        if (FAILED(hr = writer_->BeginWriting())) {
            error = describe("Could not start the H.264 encoder", hr);
            return false;
        }
        logf("MP4: %dx%d @ %d fps, %.1f Mbps, %s", cfg_.width, cfg_.height, cfg_.fps, bitrate / 1e6,
             hardware ? "hardware allowed" : "software");
        return true;
    }

    static std::string describe(const char* what, HRESULT hr) {
        char b[160];
        std::snprintf(b, sizeof(b), "%s (hr=0x%08lx).", what, (unsigned long)hr);
        return b;
    }

    // BT.709 limited range, 2x2 chroma averaging.
    static void rgbaToNV12(const uint8_t* rgba, uint8_t* dst, int w, int h) {
        uint8_t* yPlane = dst;
        uint8_t* uv = dst + (size_t)w * h;
        for (int y = 0; y < h; y += 2) {
            for (int x = 0; x < w; x += 2) {
                float sr = 0, sg = 0, sb = 0;
                for (int dy = 0; dy < 2; ++dy)
                    for (int dx = 0; dx < 2; ++dx) {
                        const uint8_t* p = rgba + ((size_t)(y + dy) * w + (x + dx)) * 4;
                        float r = p[0] / 255.0f, g = p[1] / 255.0f, b = p[2] / 255.0f;
                        float Y = 0.2126f * r + 0.7152f * g + 0.0722f * b;
                        yPlane[(size_t)(y + dy) * w + x + dx] = (uint8_t)(16.5f + 219.0f * Y);
                        sr += r;
                        sg += g;
                        sb += b;
                    }
                sr *= 0.25f;
                sg *= 0.25f;
                sb *= 0.25f;
                float Y = 0.2126f * sr + 0.7152f * sg + 0.0722f * sb;
                float cb = (sb - Y) / 1.8556f, cr = (sr - Y) / 1.5748f;
                uint8_t* c = uv + (size_t)(y / 2) * w + x;
                c[0] = (uint8_t)clampf(128.5f + 224.0f * cb, 0, 255);
                c[1] = (uint8_t)clampf(128.5f + 224.0f * cr, 0, 255);
            }
        }
    }

    void shutdownMF() {
        if (mfStarted_) {
            MFShutdown();
            mfStarted_ = false;
        }
    }

    SinkConfig cfg_;
    std::string temp_;
    ComPtr<IMFSinkWriter> writer_;
    DWORD stream_ = 0;
    long long frame_ = 0;
    bool mfStarted_ = false;
};

// ---------------------------------------------------------------------------
// PNG sequence
// ---------------------------------------------------------------------------

class PngSink : public FrameSink {
public:
    bool begin(const SinkConfig& cfg, std::string&) override {
        cfg_ = cfg;
        return true;
    }
    bool write(int index, const uint8_t* rgba, std::string& error) override {
        std::string path = pngFramePath(cfg_.path, index);
        int comp = cfg_.alpha ? 4 : 3;
        std::vector<uint8_t> rgb;
        const uint8_t* data = rgba;
        if (comp == 3) {
            rgb.resize((size_t)cfg_.width * cfg_.height * 3);
            for (size_t i = 0, n = (size_t)cfg_.width * cfg_.height; i < n; ++i) {
                rgb[i * 3] = rgba[i * 4];
                rgb[i * 3 + 1] = rgba[i * 4 + 1];
                rgb[i * 3 + 2] = rgba[i * 4 + 2];
            }
            data = rgb.data();
        }
        if (!stbi_write_png(path.c_str(), cfg_.width, cfg_.height, comp, data, cfg_.width * comp)) {
            error = "Could not write " + path;
            return false;
        }
        return true;
    }
    bool finish(std::string&) override { return true; }
    void abort() override {}  // partial sequences are kept on purpose
    bool parallel() const override { return true; }

private:
    SinkConfig cfg_;
};

// ---------------------------------------------------------------------------
// GIF
// ---------------------------------------------------------------------------

class GifSink : public FrameSink {
public:
    ~GifSink() override { abort(); }
    bool begin(const SinkConfig& cfg, std::string& error) override {
        cfg_ = cfg;
        temp_ = tempPathFor(cfg.path);
        file_ = _wfopen(widen(temp_).c_str(), L"wb");
        if (!file_) {
            error = "Could not create " + temp_;
            return false;
        }
        msf_gif_alpha_threshold = cfg.alpha ? 128 : 0;
        msf_gif_bgra_flag = 0;
        if (!msf_gif_begin_to_file(&state_, cfg.width, cfg.height, (MsfGifFileWriteFunc)fwrite, file_)) {
            error = "Could not start the GIF encoder.";
            return false;
        }
        started_ = true;
        return true;
    }
    bool write(int index, const uint8_t* rgba, std::string& error) override {
        // GIF delays are in centiseconds; distribute the rounding so the loop length is exact.
        int delay = (int)std::lround(100.0 * (index + 1) / cfg_.fps) - (int)std::lround(100.0 * index / cfg_.fps);
        if (!msf_gif_frame_to_file(&state_, const_cast<uint8_t*>(rgba), std::max(2, delay), 16, cfg_.width * 4)) {
            error = "The GIF encoder failed (out of memory?).";
            return false;
        }
        return true;
    }
    bool finish(std::string& error) override {
        bool ok = started_ && msf_gif_end_to_file(&state_);
        started_ = false;
        if (file_) fclose(file_);
        file_ = nullptr;
        if (!ok) {
            error = "Finalising the GIF failed.";
            DeleteFileW(widen(temp_).c_str());
            return false;
        }
        return moveIntoPlace(temp_, cfg_.path, error);
    }
    void abort() override {
        if (started_) msf_gif_end_to_file(&state_);
        started_ = false;
        if (file_) {
            fclose(file_);
            file_ = nullptr;
            DeleteFileW(widen(temp_).c_str());
        }
    }

private:
    SinkConfig cfg_;
    std::string temp_;
    FILE* file_ = nullptr;
    MsfGifState state_ = {};
    bool started_ = false;
};

// ---------------------------------------------------------------------------
// ffmpeg (optional): raw RGBA frames through a pipe
// ---------------------------------------------------------------------------

class FfmpegSink : public FrameSink {
public:
    explicit FfmpegSink(Format f) : format_(f) {}
    ~FfmpegSink() override { abort(); }

    bool begin(const SinkConfig& cfg, std::string& error) override {
        cfg_ = cfg;
        temp_ = tempPathFor(cfg.path);
        std::string exe = cfg.ffmpegPath.empty() ? "ffmpeg" : cfg.ffmpegPath;
        char head[256];
        std::snprintf(head, sizeof(head), " -hide_banner -loglevel error -y -f rawvideo -pix_fmt rgba -s %dx%d -r %d -i - ",
                      cfg.width, cfg.height, cfg.fps);
        std::string codec;
        if (format_ == Format::WebM)
            codec = std::string("-c:v libvpx-vp9 -b:v 0 -crf 28 -row-mt 1 -pix_fmt ") + (cfg.alpha ? "yuva420p" : "yuv420p");
        else if (format_ == Format::ProRes)
            codec = std::string("-c:v prores_ks -profile:v 4 -pix_fmt ") + (cfg.alpha ? "yuva444p10le" : "yuv444p10le");
        else
            codec = "-c:v libx265 -crf 20 -preset medium -tag:v hvc1 -pix_fmt yuv420p";
        std::string cmd = "\"\"" + exe + "\"" + head + codec + " \"" + temp_ + "\"\"";
        pipe_ = _wpopen(widen(cmd).c_str(), L"wb");
        if (!pipe_) {
            error = "Could not start ffmpeg. Install it or set its path in Settings.";
            return false;
        }
        return true;
    }
    bool write(int, const uint8_t* rgba, std::string& error) override {
        size_t n = (size_t)cfg_.width * cfg_.height * 4;
        if (fwrite(rgba, 1, n, pipe_) != n) {
            error = "ffmpeg stopped accepting frames (see the log for its error).";
            return false;
        }
        return true;
    }
    bool finish(std::string& error) override {
        int code = pipe_ ? _pclose(pipe_) : -1;
        pipe_ = nullptr;
        if (code != 0) {
            error = "ffmpeg failed (exit code " + std::to_string(code) + "). Is it a build with this codec?";
            DeleteFileW(widen(temp_).c_str());
            return false;
        }
        return moveIntoPlace(temp_, cfg_.path, error);
    }
    void abort() override {
        if (pipe_) {
            _pclose(pipe_);
            pipe_ = nullptr;
            DeleteFileW(widen(temp_).c_str());
        }
    }

private:
    Format format_;
    SinkConfig cfg_;
    std::string temp_;
    FILE* pipe_ = nullptr;
};

std::unique_ptr<FrameSink> createSink(Format format) {
    switch (format) {
        case Format::MP4: return std::unique_ptr<FrameSink>(new Mp4Sink());
        case Format::PNG: return std::unique_ptr<FrameSink>(new PngSink());
        case Format::GIF: return std::unique_ptr<FrameSink>(new GifSink());
        default: return std::unique_ptr<FrameSink>(new FfmpegSink(format));
    }
}

// ---------------------------------------------------------------------------
// Queue
// ---------------------------------------------------------------------------

// Runs fn on a fresh thread that has joined the multithreaded COM apartment.
template <class F>
static void runInMTA(F fn) {
    std::thread t([&] {
        HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        fn();
        if (SUCCEEDED(hr)) CoUninitialize();
    });
    t.join();
}

EncoderQueue::EncoderQueue(std::unique_ptr<FrameSink> sink, size_t capacity) : sink_(std::move(sink)), capacity_(capacity) {}

bool EncoderQueue::begin(const SinkConfig& cfg, std::string& error) {
    bool ok = false;
    runInMTA([&] { ok = sink_->begin(cfg, error); });
    if (!ok) {
        closing_ = true;
        return false;
    }
    int workers = sink_->parallel() ? 4 : 1;
    for (int i = 0; i < workers; ++i) threads_.emplace_back([this] { worker(); });
    return true;
}

EncoderQueue::~EncoderQueue() { abort(); }

void EncoderQueue::worker() {
    HRESULT com = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    struct ComExit {
        HRESULT hr;
        ~ComExit() { if (SUCCEEDED(hr)) CoUninitialize(); }
    } comExit{com};
    for (;;) {
        std::pair<int, std::vector<uint8_t>> item;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            notEmpty_.wait(lock, [this] { return closing_ || !queue_.empty(); });
            if (queue_.empty()) return;
            item = std::move(queue_.front());
            queue_.pop_front();
            ++busy_;
        }
        notFull_.notify_all();
        std::string err;
        bool ok = aborted_ || sink_->write(item.first, item.second.data(), err);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            --busy_;
            if (!ok && error_.empty()) error_ = err;
        }
        if (ok) ++written_;
        notFull_.notify_all();
    }
}

bool EncoderQueue::push(int frameIndex, std::vector<uint8_t>&& rgba) {
    std::unique_lock<std::mutex> lock(mutex_);
    notFull_.wait(lock, [this] { return queue_.size() < capacity_ || !error_.empty() || aborted_; });
    if (!error_.empty() || aborted_) return false;
    queue_.emplace_back(frameIndex, std::move(rgba));
    lock.unlock();
    notEmpty_.notify_one();
    return true;
}

bool EncoderQueue::finish(std::string& error) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        closing_ = true;
    }
    notEmpty_.notify_all();
    for (auto& t : threads_) t.join();
    threads_.clear();
    if (!error_.empty()) {
        runInMTA([&] { sink_->abort(); });
        error = error_;
        return false;
    }
    bool ok = false;
    runInMTA([&] { ok = sink_->finish(error); });
    return ok;
}

void EncoderQueue::abort() {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (threads_.empty() && closing_) return;
        aborted_ = true;
        closing_ = true;
        queue_.clear();
    }
    notEmpty_.notify_all();
    notFull_.notify_all();
    for (auto& t : threads_) t.join();
    threads_.clear();
    runInMTA([&] { sink_->abort(); });
}

bool EncoderQueue::failed(std::string* error) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (error) *error = error_;
    return !error_.empty();
}

}  // namespace spindle
