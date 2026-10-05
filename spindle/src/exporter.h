// Turntable export: renders every frame with S accumulated samples, reads it
// back through a small staging ring, and feeds the encoder queue.
#pragma once

#include "encoders.h"
#include "renderer.h"

#include <chrono>
#include <deque>
#include <memory>

namespace spindle {

struct ExportJob {
    Scene scene;
    std::string outputPath;
    float startAzimuth = 0;  // camera azimuth of frame 0, degrees
    std::string ffmpegPath;
    int paceMs = 0;  // optional pause between frames (thermal pacing)
    int frameLimit = -1;  // > 0: stop after this many frames of the full loop (testing)
};

class Exporter {
public:
    ~Exporter();
    bool start(Renderer& renderer, const ExportJob& job, std::string& error);
    // Renders for about `budgetMs`. Returns true while the export is still running.
    bool step(Renderer& renderer, double budgetMs);
    void cancel();

    bool active() const { return active_; }
    bool succeeded() const { return succeeded_; }
    bool cancelled() const { return cancelled_; }
    const std::string& error() const { return error_; }
    int totalFrames() const { return total_; }
    int framesRendered() const { return frame_; }
    int framesEncoded() const { return queue_ ? queue_->framesWritten() : 0; }
    int samplesPerFrame() const { return samples_; }
    double elapsedSeconds() const;
    double etaSeconds() const;
    float progress() const;
    RenderTargets& targets() { return rt_; }
    bool hasPreview() const { return previewReady_; }
    const ExportJob& job() const { return job_; }

    void onDeviceLost();
    bool onDeviceRestored(Renderer& renderer, std::string& error);

private:
    bool createStaging(Renderer& renderer, std::string& error);
    bool readbackOldest(Renderer& renderer);
    void fail(const std::string& message);
    void finishEncoding();

    ExportJob job_;
    RenderTargets rt_;
    std::unique_ptr<EncoderQueue> queue_;
    ComPtr<ID3D11Texture2D> staging_[3];
    std::deque<std::pair<int, int>> pending_;  // (frame index, staging slot)
    int total_ = 0, samples_ = 1, frame_ = 0, sample_ = 0, nextToEncode_ = 0;
    bool overBlack_ = false;
    bool active_ = false, succeeded_ = false, cancelled_ = false, previewReady_ = false;
    std::string error_;
    std::chrono::steady_clock::time_point startTime_, endTime_;
    vec3 center_;
    float radius_ = 1;
};

}  // namespace spindle
