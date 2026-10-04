#include "exporter.h"

#include <cstring>
#include <thread>

namespace spindle {

// Concentric mapping of the unit square to the unit disk (even lens coverage).
static void squareToDisk(float u, float v, float& x, float& y) {
    float a = 2 * u - 1, b = 2 * v - 1;
    if (a == 0 && b == 0) {
        x = y = 0;
        return;
    }
    float r, phi;
    if (std::fabs(a) > std::fabs(b)) {
        r = a;
        phi = (kPi / 4) * (b / a);
    } else {
        r = b;
        phi = (kPi / 2) - (kPi / 4) * (a / b);
    }
    x = r * std::cos(phi);
    y = r * std::sin(phi);
}

SampleInput makeTurntableSample(const Scene& s, const vec3& center, float radius, int width, int height, double frame,
                                int sampleIndex, int totalSamples, float startAzimuthDeg) {
    const bool jitter = totalSamples > 1 && sampleIndex > 0;
    const int h = sampleIndex + 1;
    double t = frame;
    if (jitter && s.turntable.shutter > 0) t += clampf(s.turntable.shutter, 0, 1) * halton(h, 5);
    float aspect = (float)width / (float)std::max(1, height);
    TurntablePose p = turntablePose(s, center, radius, aspect, t, startAzimuthDeg);

    ViewRequest r;
    r.eye = p.eye;
    r.target = p.target;
    r.fovY = p.fovY;
    r.ortho = p.ortho;
    r.orthoHalfHeight = p.orthoHalfHeight;
    r.aspect = aspect;
    r.nearZ = p.nearZ;
    r.farZ = p.farZ;
    r.widthPx = width;
    r.heightPx = height;
    if (jitter) {
        r.jitterX = halton(h, 2) - 0.5f;
        r.jitterY = halton(h, 3) - 0.5f;
        if (s.camera.depthOfField && !p.ortho) {
            float lensRadius = s.camera.focalLength / (2.0f * std::max(0.7f, s.camera.fStop));
            float x, y;
            squareToDisk(halton(h, 7), halton(h, 11), x, y);
            r.lensX = x * lensRadius;
            r.lensY = y * lensRadius;
        }
    }
    SampleInput in;
    in.view = buildView(r);
    in.objectAngle = p.objectAngle;
    in.sampleIndex = sampleIndex;
    in.fullQuality = true;
    in.lightReferenceAzimuth = startAzimuthDeg;
    in.explode = explodeAmountAt(s, t);
    return in;
}

SampleInput makeOrbitSample(const Scene& s, const OrbitCamera& cam, float radius, int width, int height, int sampleIndex) {
    float aspect = (float)width / (float)std::max(1, height);
    ViewRequest r;
    r.eye = cam.eye();
    r.target = cam.target;
    r.fovY = fovYForFocalLength(s.camera.focalLength, aspect);
    r.ortho = s.camera.orthographic;
    r.orthoHalfHeight = cam.distance * std::tan(r.fovY * 0.5f);
    r.aspect = aspect;
    r.nearZ = std::max(cam.distance * 0.01f, cam.distance - radius * 3.0f);
    if (r.ortho) r.nearZ = cam.distance * 0.01f;
    r.farZ = cam.distance + radius * 80.0f;
    r.widthPx = width;
    r.heightPx = height;
    if (sampleIndex > 0) {
        r.jitterX = halton(sampleIndex + 1, 2) - 0.5f;
        r.jitterY = halton(sampleIndex + 1, 3) - 0.5f;
    }
    SampleInput in;
    in.view = buildView(r);
    in.sampleIndex = sampleIndex;
    in.lightReferenceAzimuth = cam.azimuth;
    in.explode = clampf(s.explode.manual, 0, 1);
    return in;
}

Exporter::~Exporter() { cancel(); }

bool Exporter::createStaging(Renderer& renderer, std::string& error) {
    for (auto& s : staging_) {
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = (UINT)rt_.width;
        d.Height = (UINT)rt_.height;
        d.MipLevels = 1;
        d.ArraySize = 1;
        d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        d.SampleDesc.Count = 1;
        d.Usage = D3D11_USAGE_STAGING;
        d.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        if (FAILED(renderer.device()->CreateTexture2D(&d, nullptr, s.put()))) {
            error = "Could not allocate readback buffers.";
            return false;
        }
    }
    return true;
}

bool Exporter::start(Renderer& renderer, const ExportJob& job, std::string& error) {
    cancel();
    queue_.reset();
    rt_.release();
    for (auto& st : staging_) st = nullptr;
    pending_.clear();
    frame_ = sample_ = nextToEncode_ = 0;
    active_ = succeeded_ = cancelled_ = previewReady_ = false;
    error_.clear();
    job_ = job;
    const Scene& s = job_.scene;
    const OutputSettings& o = s.output;
    if (!renderer.hasMesh()) {
        error = "Load an STL or 3MF file first.";
        return false;
    }
    total_ = s.turntable.frameCount();
    if (job_.frameLimit > 0) total_ = std::min(total_, job_.frameLimit);
    samples_ = qualitySamples(o.quality);
    center_ = renderer.sphereCenter();
    radius_ = renderer.framingRadius(s);
    const bool transparent = s.environment.background == Background::Transparent;
    // Formats without alpha get the transparent background composited over black.
    overBlack_ = !(transparent && formatSupportsAlpha(o.format));

    if (!renderer.ensureTargets(rt_, o.width, o.height, error)) return false;
    if (!createStaging(renderer, error)) return false;

    SinkConfig cfg;
    cfg.format = o.format;
    cfg.path = job_.outputPath;
    cfg.width = o.width;
    cfg.height = o.height;
    cfg.fps = s.turntable.fps;
    cfg.frameCount = total_;
    cfg.bitrateMbps = o.bitrateMbps;
    cfg.alpha = !overBlack_;
    cfg.ffmpegPath = job_.ffmpegPath;
    queue_.reset(new EncoderQueue(createSink(o.format)));
    if (!queue_->begin(cfg, error)) {
        queue_.reset();
        return false;
    }
    active_ = true;
    startTime_ = std::chrono::steady_clock::now();
    logf("Export: %d frames x %d samples at %dx%d -> %s", total_, samples_, o.width, o.height, job_.outputPath.c_str());
    // Keep the machine awake (not the display) for the length of the export.
    SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED);
    return true;
}

bool Exporter::readbackOldest(Renderer& renderer) {
    if (pending_.empty()) return true;
    auto [frame, slot] = pending_.front();
    pending_.pop_front();
    D3D11_MAPPED_SUBRESOURCE m;
    HRESULT hr = renderer.context()->Map(staging_[slot].get(), 0, D3D11_MAP_READ, 0, &m);
    if (FAILED(hr)) {
        fail("Reading the frame back from the GPU failed.");
        return false;
    }
    const size_t row = (size_t)rt_.width * 4;
    std::vector<uint8_t> pixels(row * rt_.height);
    for (int y = 0; y < rt_.height; ++y) std::memcpy(&pixels[row * y], (const uint8_t*)m.pData + (size_t)m.RowPitch * y, row);
    renderer.context()->Unmap(staging_[slot].get(), 0);
    if (!queue_->push(frame, std::move(pixels))) {
        std::string err;
        queue_->failed(&err);
        fail(err.empty() ? "The encoder stopped." : err);
        return false;
    }
    nextToEncode_ = frame + 1;
    return true;
}

bool Exporter::step(Renderer& renderer, double budgetMs) {
    if (!active_) return false;
    auto begin = std::chrono::steady_clock::now();
    for (;;) {
        std::string err;
        if (queue_->failed(&err)) {
            fail(err);
            return false;
        }
        if (frame_ >= total_) {
            while (!pending_.empty())
                if (!readbackOldest(renderer)) return false;
            finishEncoding();
            return false;
        }
        SampleInput in = makeTurntableSample(job_.scene, center_, radius_, rt_.width, rt_.height, frame_, sample_, samples_,
                                             job_.startAzimuth);
        renderer.renderSample(rt_, job_.scene, in);
        renderer.context()->Flush();  // one short submission per sample: no GPU timeouts
        if (++sample_ >= samples_) {
            renderer.resolve(rt_, samples_, overBlack_);
            int slot = frame_ % 3;
            renderer.context()->CopyResource(staging_[slot].get(), rt_.outTex.get());
            pending_.emplace_back(frame_, slot);
            previewReady_ = true;
            // Two frames of latency: the GPU has long finished the oldest copy.
            if (pending_.size() >= 3 && !readbackOldest(renderer)) return false;
            ++frame_;
            sample_ = 0;
            if (job_.paceMs > 0) std::this_thread::sleep_for(std::chrono::milliseconds(job_.paceMs));
        }
        double spent = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - begin).count();
        if (spent >= budgetMs) return true;
    }
}

void Exporter::finishEncoding() {
    std::string err;
    bool ok = queue_->finish(err);
    active_ = false;
    endTime_ = std::chrono::steady_clock::now();
    SetThreadExecutionState(ES_CONTINUOUS);
    if (!ok) {
        error_ = err;
        logf("Export failed: %s", err.c_str());
        return;
    }
    succeeded_ = true;
    logf("Export finished in %.1f s", elapsedSeconds());
}

void Exporter::fail(const std::string& message) {
    error_ = message;
    logf("Export failed: %s", message.c_str());
    if (queue_) queue_->abort();
    active_ = false;
    endTime_ = std::chrono::steady_clock::now();
    SetThreadExecutionState(ES_CONTINUOUS);
}

void Exporter::cancel() {
    if (!active_) return;
    cancelled_ = true;
    fail("Cancelled.");
}

double Exporter::elapsedSeconds() const {
    auto end = active_ ? std::chrono::steady_clock::now() : endTime_;
    return std::chrono::duration<double>(end - startTime_).count();
}

float Exporter::progress() const {
    if (total_ <= 0) return 0;
    return clampf(((float)frame_ + (float)sample_ / samples_) / (float)total_, 0, 1);
}

double Exporter::etaSeconds() const {
    float p = progress();
    if (p <= 0.001f) return -1;
    return elapsedSeconds() * (1.0 - p) / p;
}

void Exporter::onDeviceLost() {
    for (auto& s : staging_) s = nullptr;
    pending_.clear();
    rt_.release();
    previewReady_ = false;
}

bool Exporter::onDeviceRestored(Renderer& renderer, std::string& error) {
    if (!active_) return true;
    const OutputSettings& o = job_.scene.output;
    if (!renderer.ensureTargets(rt_, o.width, o.height, error) || !createStaging(renderer, error)) {
        fail(error);
        return false;
    }
    // Frames that were still on the GPU are rendered again; encoded ones are kept.
    frame_ = nextToEncode_;
    sample_ = 0;
    logf("Export resumed at frame %d after a device reset", frame_);
    return true;
}

}  // namespace spindle
