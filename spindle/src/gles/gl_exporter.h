// Turntable export for the OpenGL ES renderer: renders every frame with S
// accumulated samples and hands the resolved frame to a sink (GIF, PNG
// sequence, or a platform video encoder such as Android MediaCodec).
#pragma once

#include "gl_renderer.h"

#include <chrono>
#include <memory>
#include <string>

namespace spindle {

struct GlSinkConfig {
    std::string path;  // GIF/MP4: the file; PNG: base name, frames get _0000 suffixes
    int width = 0, height = 0, fps = 30, frameCount = 0;
    float bitrateMbps = 0;
    bool alpha = false;  // frames carry straight alpha (transparent background)
};

class GlFrameSink {
public:
    virtual ~GlFrameSink() = default;
    virtual bool begin(const GlSinkConfig& cfg, std::string& error) = 0;
    // Called with rt.outTex holding frame `index`, the GL context current.
    virtual bool writeFrame(GlRenderer& renderer, GlTargets& rt, int index, std::string& error) = 0;
    virtual bool finish(std::string& error) = 0;
    virtual void abort() = 0;
    virtual bool supportsAlpha() const { return false; }
};

std::unique_ptr<GlFrameSink> makeGifSink();
std::unique_ptr<GlFrameSink> makePngSink();

struct GlExportJob {
    Scene scene;
    float startAzimuth = 0;
    int frameLimit = -1;
};

class GlExporter {
public:
    ~GlExporter();
    bool start(GlRenderer& renderer, const GlExportJob& job, std::unique_ptr<GlFrameSink> sink, const GlSinkConfig& cfg,
               std::string& error);
    // Renders for about budgetMs; returns true while the export is still running.
    bool step(GlRenderer& renderer, double budgetMs);
    void cancel();

    bool active() const { return active_; }
    bool succeeded() const { return succeeded_; }
    bool cancelled() const { return cancelled_; }
    const std::string& error() const { return error_; }
    int totalFrames() const { return total_; }
    int framesDone() const { return frame_; }
    float progress() const;
    double elapsedSeconds() const;
    double etaSeconds() const;
    GlTargets& targets() { return rt_; }
    bool hasPreview() const { return previewReady_; }
    // The GL context was lost: the export fails (Android tears contexts down on pause).
    void onContextLost();

private:
    void fail(const std::string& message);

    GlExportJob job_;
    GlTargets rt_;
    std::unique_ptr<GlFrameSink> sink_;
    int total_ = 0, samples_ = 1, frame_ = 0, sample_ = 0;
    bool overBlack_ = false;
    bool active_ = false, succeeded_ = false, cancelled_ = false, previewReady_ = false;
    std::string error_;
    vec3 center_;
    float radius_ = 1;
    std::chrono::steady_clock::time_point startTime_, endTime_;
};

std::string pngFramePathFor(const std::string& base, int index);

}  // namespace spindle
