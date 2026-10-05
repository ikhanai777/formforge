#include "gl_exporter.h"

#include <cstdio>

#define STB_IMAGE_WRITE_IMPLEMENTATION
#include "../../third_party/stb/stb_image_write.h"

#define MSF_GIF_IMPL
#include "../../third_party/msf_gif/msf_gif.h"

namespace spindle {

std::string pngFramePathFor(const std::string& base, int index) {
    std::string stem = base;
    if (stem.size() > 4 && toLower(stem.substr(stem.size() - 4)) == ".png") stem = stem.substr(0, stem.size() - 4);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "_%04d.png", index);
    return stem + buf;
}

namespace {

std::string partialPath(const std::string& path) { return path + ".partial"; }

class GifSink : public GlFrameSink {
public:
    ~GifSink() override { abort(); }
    bool begin(const GlSinkConfig& cfg, std::string& error) override {
        cfg_ = cfg;
        file_ = std::fopen(partialPath(cfg.path).c_str(), "wb");
        if (!file_) {
            error = "Could not create " + cfg.path;
            return false;
        }
        msf_gif_alpha_threshold = cfg.alpha ? 128 : 0;
        msf_gif_bgra_flag = 0;
        if (!msf_gif_begin_to_file(&state_, cfg.width, cfg.height, (MsfGifFileWriteFunc)std::fwrite, file_)) {
            error = "Could not start the GIF encoder.";
            return false;
        }
        started_ = true;
        return true;
    }
    bool writeFrame(GlRenderer& r, GlTargets& rt, int index, std::string& error) override {
        r.readPixels(rt, pixels_);
        int delay = (int)std::lround(100.0 * (index + 1) / cfg_.fps) - (int)std::lround(100.0 * index / cfg_.fps);
        if (!msf_gif_frame_to_file(&state_, pixels_.data(), std::max(2, delay), 16, cfg_.width * 4)) {
            error = "The GIF encoder failed (out of memory?).";
            return false;
        }
        return true;
    }
    bool finish(std::string& error) override {
        bool ok = started_ && msf_gif_end_to_file(&state_);
        started_ = false;
        if (file_) std::fclose(file_);
        file_ = nullptr;
        if (!ok || std::rename(partialPath(cfg_.path).c_str(), cfg_.path.c_str()) != 0) {
            error = "Finalising the GIF failed.";
            std::remove(partialPath(cfg_.path).c_str());
            return false;
        }
        return true;
    }
    void abort() override {
        if (started_) msf_gif_end_to_file(&state_);
        started_ = false;
        if (file_) {
            std::fclose(file_);
            file_ = nullptr;
            std::remove(partialPath(cfg_.path).c_str());
        }
    }
    bool supportsAlpha() const override { return true; }

private:
    GlSinkConfig cfg_;
    FILE* file_ = nullptr;
    MsfGifState state_ = {};
    bool started_ = false;
    std::vector<uint8_t> pixels_;
};

class PngSink : public GlFrameSink {
public:
    bool begin(const GlSinkConfig& cfg, std::string&) override {
        cfg_ = cfg;
        return true;
    }
    bool writeFrame(GlRenderer& r, GlTargets& rt, int index, std::string& error) override {
        r.readPixels(rt, pixels_);
        std::string path = pngFramePathFor(cfg_.path, index);
        if (!stbi_write_png(path.c_str(), cfg_.width, cfg_.height, 4, pixels_.data(), cfg_.width * 4)) {
            error = "Could not write " + path;
            return false;
        }
        return true;
    }
    bool finish(std::string&) override { return true; }
    void abort() override {}
    bool supportsAlpha() const override { return true; }

private:
    GlSinkConfig cfg_;
    std::vector<uint8_t> pixels_;
};

}  // namespace

std::unique_ptr<GlFrameSink> makeGifSink() { return std::unique_ptr<GlFrameSink>(new GifSink()); }
std::unique_ptr<GlFrameSink> makePngSink() { return std::unique_ptr<GlFrameSink>(new PngSink()); }

GlExporter::~GlExporter() {
    if (active_ && sink_) sink_->abort();
}

bool GlExporter::start(GlRenderer& renderer, const GlExportJob& job, std::unique_ptr<GlFrameSink> sink,
                       const GlSinkConfig& cfgIn, std::string& error) {
    cancel();
    job_ = job;
    sink_ = std::move(sink);
    frame_ = sample_ = 0;
    active_ = succeeded_ = cancelled_ = previewReady_ = false;
    error_.clear();
    const Scene& s = job_.scene;
    if (!renderer.hasMesh()) {
        error = "Open a model first.";
        return false;
    }
    total_ = s.turntable.frameCount();
    if (job_.frameLimit > 0) total_ = std::min(total_, job_.frameLimit);
    samples_ = qualitySamples(s.output.quality);
    center_ = renderer.sphereCenter();
    radius_ = renderer.framingRadius(s);
    const bool transparent = s.environment.background == Background::Transparent;
    overBlack_ = !(transparent && sink_->supportsAlpha());
    if (!renderer.ensureTargets(rt_, s.output.width, s.output.height, error)) return false;
    GlSinkConfig cfg = cfgIn;
    cfg.width = s.output.width;
    cfg.height = s.output.height;
    cfg.fps = s.turntable.fps;
    cfg.frameCount = total_;
    cfg.alpha = !overBlack_;
    if (!sink_->begin(cfg, error)) {
        sink_.reset();
        return false;
    }
    active_ = true;
    startTime_ = std::chrono::steady_clock::now();
    return true;
}

bool GlExporter::step(GlRenderer& renderer, double budgetMs) {
    if (!active_) return false;
    auto begin = std::chrono::steady_clock::now();
    for (;;) {
        if (frame_ >= total_) {
            std::string err;
            bool ok = sink_->finish(err);
            active_ = false;
            endTime_ = std::chrono::steady_clock::now();
            if (ok) succeeded_ = true;
            else error_ = err;
            return false;
        }
        SampleInput in = makeTurntableSample(job_.scene, center_, radius_, rt_.width, rt_.height, frame_, sample_, samples_,
                                             job_.startAzimuth);
        renderer.renderSample(rt_, job_.scene, in);
        glFlush();
        if (++sample_ >= samples_) {
            renderer.resolve(rt_, overBlack_);
            std::string err;
            if (!sink_->writeFrame(renderer, rt_, frame_, err)) {
                fail(err);
                return false;
            }
            previewReady_ = true;
            ++frame_;
            sample_ = 0;
        }
        double spent = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - begin).count();
        if (spent >= budgetMs) return true;
    }
}

void GlExporter::fail(const std::string& message) {
    error_ = message;
    if (sink_) sink_->abort();
    active_ = false;
    endTime_ = std::chrono::steady_clock::now();
}

void GlExporter::cancel() {
    if (!active_) return;
    cancelled_ = true;
    fail("Cancelled.");
}

void GlExporter::onContextLost() {
    rt_ = GlTargets();  // the names died with the context
    if (active_) fail("The render was interrupted because the app went to the background.");
}

float GlExporter::progress() const {
    if (total_ <= 0) return 0;
    return clampf(((float)frame_ + (float)sample_ / samples_) / (float)total_, 0, 1);
}

double GlExporter::elapsedSeconds() const {
    auto end = active_ ? std::chrono::steady_clock::now() : endTime_;
    return std::chrono::duration<double>(end - startTime_).count();
}

double GlExporter::etaSeconds() const {
    float p = progress();
    if (p <= 0.001f) return -1;
    return elapsedSeconds() * (1.0 - p) / p;
}

}  // namespace spindle
