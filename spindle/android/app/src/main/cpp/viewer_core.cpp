#include "viewer_core.h"

#include "../../../../../third_party/nlohmann/json.hpp"
#include "../../../../../third_party/stb/stb_image_write.h"

#include <cstring>

namespace spindle {

using json = nlohmann::json;
using Clock = std::chrono::steady_clock;

namespace {

// Writes the single resolved frame to exactly `path` (PNG).
class StillSink : public GlFrameSink {
public:
    bool begin(const GlSinkConfig& cfg, std::string&) override {
        cfg_ = cfg;
        return true;
    }
    bool writeFrame(GlRenderer& r, GlTargets& rt, int, std::string& error) override {
        std::vector<uint8_t> px;
        r.readPixels(rt, px);
        if (!stbi_write_png(cfg_.path.c_str(), cfg_.width, cfg_.height, 4, px.data(), cfg_.width * 4)) {
            error = "Could not write the image.";
            return false;
        }
        return true;
    }
    bool finish(std::string&) override { return true; }
    void abort() override {}
    bool supportsAlpha() const override { return true; }

private:
    GlSinkConfig cfg_;
};

uint64_t fnv(const void* p, size_t n, uint64_t h = 1469598103934665603ull) {
    const uint8_t* b = (const uint8_t*)p;
    for (size_t i = 0; i < n; ++i) h = (h ^ b[i]) * 1099511628211ull;
    return h;
}

MeshOptions meshOptionsFor(const Scene& s) {
    MeshOptions o;
    o.up = s.model.up;
    o.creaseAngleDeg = s.model.creaseAngle;
    for (int i = 0; i < 3; ++i) o.quarterTurns[i] = s.model.quarterTurns[i];
    return o;
}

bool sameOptions(const MeshOptions& a, const MeshOptions& b) {
    return a.up == b.up && a.creaseAngleDeg == b.creaseAngleDeg && a.quarterTurns[0] == b.quarterTurns[0] &&
           a.quarterTurns[1] == b.quarterTurns[1] && a.quarterTurns[2] == b.quarterTurns[2];
}

}  // namespace

ViewerCore::ViewerCore() {
    // Phones: a little lighter than the desktop defaults.
    scene_.output.width = 1280;
    scene_.output.height = 720;
}

ViewerCore::~ViewerCore() {
    modelProgress_.cancel = true;
    if (modelWorker_.joinable()) modelWorker_.join();
    if (envWorker_.joinable()) envWorker_.join();
}

// ---------------------------------------------------------------------------
// [UI]
// ---------------------------------------------------------------------------

std::string ViewerCore::sceneJson() {
    std::lock_guard<std::mutex> lock(mutex_);
    return sceneToJson(scene_);
}

void ViewerCore::setSceneJson(const std::string& text) {
    std::lock_guard<std::mutex> lock(mutex_);
    Scene s = scene_;
    std::string err;
    if (sceneFromJson(text, s, err)) {
        scene_ = s;
        ++sceneVersion_;
    } else {
        pendingError_ = err;
    }
}

std::string ViewerCore::applyMaterialPreset(const std::string& name) {
    std::lock_guard<std::mutex> lock(mutex_);
    spindle::applyMaterialPreset(name, scene_.material);
    ++sceneVersion_;
    return sceneToJson(scene_);
}

void ViewerCore::loadModel(const std::string& path, const std::string& displayName) {
    // Stop any running load first. The worker takes mutex_ as it finishes, so
    // join it without holding the lock.
    std::thread previous;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        modelProgress_.cancel = true;
        previous = std::move(modelWorker_);
    }
    if (previous.joinable()) previous.join();

    std::lock_guard<std::mutex> lock(mutex_);
    modelProgress_.value = 0;
    modelProgress_.cancel = false;
    modelReady_ = false;
    modelBusy_ = true;
    pendingModelName_ = displayName;
    MeshOptions opts = meshOptionsFor(scene_);
    appliedMeshOptions_ = opts;
    haveAppliedOptions_ = true;
    modelWorker_ = std::thread([this, path, opts] {
        LoadResult lr = loadModelFile(path, &modelProgress_);
        Mesh m;
        if (lr.ok && !modelProgress_.cancel) m = processMesh(lr.soup, opts);
        std::lock_guard<std::mutex> lock(mutex_);
        pendingLoad_ = std::move(lr);
        pendingMesh_ = std::move(m);
        modelReady_ = true;
    });
}

void ViewerCore::orbit(float dx, float dy) {
    std::lock_guard<std::mutex> lock(mutex_);
    ops_.orbitX += dx;
    ops_.orbitY += dy;
    preview_ = false;
}

void ViewerCore::zoom(float factor, float nx, float ny) {
    std::lock_guard<std::mutex> lock(mutex_);
    ops_.zoomFactor *= factor;
    ops_.zoomX = nx;
    ops_.zoomY = ny;
}

void ViewerCore::pan(float dx, float dy) {
    std::lock_guard<std::mutex> lock(mutex_);
    ops_.panX += dx;
    ops_.panY += dy;
}

void ViewerCore::frameModel() {
    std::lock_guard<std::mutex> lock(mutex_);
    ops_.frame = true;
}

void ViewerCore::viewPreset(int which) {
    std::lock_guard<std::mutex> lock(mutex_);
    ops_.preset = which;
}

void ViewerCore::setPreview(bool on) {
    std::lock_guard<std::mutex> lock(mutex_);
    preview_ = on;
    previewFrame_ = 0;
}

std::string ViewerCore::startExport(ExportKind kind, const std::string& path) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (mesh_.indices.empty() && !modelBusy_) return "Open a model first.";
    if (exportStatus_.active || exportRequest_.pending) return "A render is already running.";
    if (kind == ExportKind::MP4 && (scene_.output.width % 2 || scene_.output.height % 2))
        return "MP4 needs an even width and height.";
    if (kind == ExportKind::MP4 && !videoSinkFactory_) return "Video encoding is not available on this device.";
    exportRequest_ = {true, kind, path};
    exportFinished_ = false;
    exportError_.clear();
    return "";
}

void ViewerCore::cancelExport() {
    std::lock_guard<std::mutex> lock(mutex_);
    cancelRequested_ = true;
    exportRequest_.pending = false;
}

std::string ViewerCore::statusJson() {
    std::lock_guard<std::mutex> lock(mutex_);
    json j;
    j["gl"] = glDescription_;
    j["glError"] = glError_;
    j["loading"] = modelBusy_;
    j["loadProgress"] = modelProgress_.value.load();
    j["envLoading"] = envBusy_;
    j["environment"] = envSource_;
    j["preview"] = preview_;
    j["samples"] = sampleIndex_;
    j["maxSamples"] = maxSamples_;
    j["sceneVersion"] = sceneVersion_;
    if (!mesh_.indices.empty()) {
        vec3 s = mesh_.size();
        json parts = json::array();
        for (auto& p : mesh_.parts) parts.push_back(p.name);
        j["model"] = {{"name", modelName_}, {"triangles", mesh_.triangleCount()}, {"size", {s.x, s.y, s.z}},
                      {"parts", parts}, {"hasColors", mesh_.hasFileColors}, {"hasPbr", mesh_.hasFilePbr}};
    }
    j["warnings"] = warnings_;
    if (!pendingError_.empty()) {
        j["error"] = pendingError_;
        pendingError_.clear();
    }
    json ex = {{"active", exportStatus_.active || exportRequest_.pending},
               {"progress", exportStatus_.progress},
               {"frame", exportStatus_.frame},
               {"total", exportStatus_.total},
               {"elapsed", exportStatus_.elapsed},
               {"eta", exportStatus_.eta},
               {"kind", (int)exportKind_},
               {"path", exportPath_}};
    if (exportFinished_) {
        ex["finished"] = true;
        ex["ok"] = exportOk_;
        ex["error"] = exportError_;
        exportFinished_ = false;
    }
    j["export"] = ex;
    return j.dump();
}

// ---------------------------------------------------------------------------
// [GL]
// ---------------------------------------------------------------------------

void ViewerCore::onSurfaceCreated() {
    // A new context: every old GL name is gone.
    renderer_.forgetContext();
    viewRT_ = GlTargets();
    exporter_.onContextLost();
    std::string err;
    glReady_ = renderer_.init(err);
    std::lock_guard<std::mutex> lock(mutex_);
    glDescription_ = renderer_.deviceDescription();
    glError_ = glReady_ ? "" : err;
    meshDirty_ = !mesh_.indices.empty();
    envDirty_ = !env_.rgba.empty();
    textureDirty_ = !texture_.rgba.empty();
    lastHash_ = 0;
}

void ViewerCore::onSurfaceChanged(int width, int height) {
    surfaceW_ = std::max(1, width);
    surfaceH_ = std::max(1, height);
    lastHash_ = 0;
}

void ViewerCore::startEnvironmentLoad(const std::string& source) {
    envBusy_ = true;
    envReady_ = false;
    pendingEnvSource_ = source;
    if (envWorker_.joinable()) envWorker_.join();
    envWorker_ = std::thread([this, source] {
        EnvironmentImage e;
        std::string err;
        if (isProceduralEnvironment(source)) makeProceduralEnvironment(source, e, 512);
        else loadEnvironmentFile(source, e, err);
        std::lock_guard<std::mutex> lock(mutex_);
        pendingEnv_ = std::move(e);
        pendingEnvError_ = err;
        envReady_ = true;
    });
}

// Called with mutex_ held.
void ViewerCore::pollWorkers() {
    if (modelBusy_ && modelReady_) {
        modelWorker_.join();
        modelBusy_ = false;
        if (!pendingLoad_.ok) {
            if (!modelProgress_.cancel) pendingError_ = pendingModelName_ + ": " + pendingLoad_.error;
        } else {
            bool firstLoad = pendingLoad_.soup.positions.size() > 0;
            mesh_ = std::move(pendingMesh_);
            if (firstLoad && !pendingModelName_.empty()) {
                soup_ = std::move(pendingLoad_.soup);
                modelName_ = pendingModelName_;
                warnings_ = pendingLoad_.warnings;
                cameraFramed_ = false;
            }
            meshDirty_ = true;
        }
        pendingLoad_ = LoadResult();
        pendingMesh_ = Mesh();
    }
    // Orientation or smoothing changed: re-process the kept triangles.
    if (!modelBusy_ && !soup_.positions.empty() && haveAppliedOptions_ &&
        !sameOptions(appliedMeshOptions_, meshOptionsFor(scene_))) {
        MeshOptions opts = meshOptionsFor(scene_);
        appliedMeshOptions_ = opts;
        modelBusy_ = true;
        modelReady_ = false;
        pendingModelName_.clear();
        if (modelWorker_.joinable()) modelWorker_.join();
        modelWorker_ = std::thread([this, opts] {
            Mesh m = processMesh(soup_, opts);
            std::lock_guard<std::mutex> lock(mutex_);
            pendingLoad_ = LoadResult();
            pendingLoad_.ok = true;
            pendingMesh_ = std::move(m);
            modelReady_ = true;
        });
    }
    if (envBusy_ && envReady_) {
        envWorker_.join();
        envBusy_ = false;
        if (!pendingEnvError_.empty()) {
            pendingError_ = pendingEnvError_;
            scene_.environment.source = envSource_;
        } else {
            env_ = std::move(pendingEnv_);
            envSource_ = pendingEnvSource_;
            envDirty_ = true;
        }
    }
    if (!envBusy_ && scene_.environment.source != envSource_) startEnvironmentLoad(scene_.environment.source);
    if (scene_.material.pattern == Pattern::Image && scene_.material.texturePath != texturePath_) {
        texturePath_ = scene_.material.texturePath;
        std::string err;
        Image8 img;
        if (loadImage8(texturePath_, img, err)) {
            texture_ = std::move(img);
            textureDirty_ = true;
        } else {
            pendingError_ = err;
        }
    }
}

void ViewerCore::frameCameraLocked(float aspect) {
    cam_.elevation = scene_.camera.elevation;
    cam_.frame(renderer_.sphereCenter(), renderer_.framingRadius(scene_), fovYForFocalLength(scene_.camera.focalLength, aspect),
               aspect, scene_.camera.fill);
}

void ViewerCore::applyCameraOps(const CameraOps& ops, float aspect) {
    float fovY = fovYForFocalLength(scene_.camera.focalLength, aspect);
    if (ops.orbitX != 0 || ops.orbitY != 0) cam_.orbit(-ops.orbitX * 0.3f, ops.orbitY * 0.3f);
    if (ops.panX != 0 || ops.panY != 0) cam_.pan(ops.panX, ops.panY, (float)surfaceH_, fovY);
    if (ops.zoomFactor != 1) {
        float before = cam_.distance;
        cam_.distance = std::max(1e-3f, cam_.distance / ops.zoomFactor);
        // Zoom towards the pinch centre on the plane through the target.
        float k = 1.0f - cam_.distance / before;
        vec3 fwd = normalize(cam_.target - cam_.eye());
        vec3 right = normalize(cross(fwd, vec3(0, 0, 1)));
        vec3 up = cross(right, fwd);
        float halfH = before * std::tan(fovY * 0.5f), halfW = halfH * aspect;
        cam_.target += (right * (ops.zoomX * halfW) + up * (ops.zoomY * halfH)) * k;
    }
    if (ops.preset == 0) { cam_.azimuth = -90; cam_.elevation = 0; }
    if (ops.preset == 1) { cam_.azimuth = 0; cam_.elevation = 0; }
    if (ops.preset == 2) { cam_.azimuth = -90; cam_.elevation = 89; }
    if (ops.preset == 3) { cam_.azimuth = -60; cam_.elevation = scene_.camera.elevation; }
    if (ops.frame || ops.preset >= 0) frameCameraLocked(aspect);
}

void ViewerCore::startPendingExport(const Scene& scene, const ExportRequest& req) {
    GlExportJob job;
    job.scene = scene;
    job.startAzimuth = scene.turntable.startFromViewport ? cam_.azimuth : scene.turntable.startAngle;
    std::unique_ptr<GlFrameSink> sink;
    if (req.kind == ExportKind::MP4) sink = videoSinkFactory_();
    else if (req.kind == ExportKind::GIF) sink = makeGifSink();
    else {
        sink.reset(new StillSink());
        job.frameLimit = 1;
        // A still is the first turntable frame; never animate it away.
        job.scene.explode.animate = false;
    }
    GlSinkConfig cfg;
    cfg.path = req.path;
    cfg.bitrateMbps = scene.output.bitrateMbps;
    std::string err;
    exportKind_ = req.kind;
    exportPath_ = req.path;
    if (!exporter_.start(renderer_, job, std::move(sink), cfg, err)) {
        std::lock_guard<std::mutex> lock(mutex_);
        exportFinished_ = true;
        exportOk_ = false;
        exportError_ = err;
    }
}

bool ViewerCore::onDrawFrame() {
    auto now = Clock::now();
    double dt = std::chrono::duration<double>(now - lastFrame_).count();
    lastFrame_ = now;

    Scene scene;
    CameraOps ops;
    ExportRequest req;
    bool preview, cancel, uploadMesh, uploadEnv, uploadTex;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        pollWorkers();
        scene = scene_;
        ops = ops_;
        ops_ = CameraOps();
        req = exportRequest_;
        exportRequest_.pending = false;
        preview = preview_;
        cancel = cancelRequested_;
        cancelRequested_ = false;
        uploadMesh = meshDirty_;
        uploadEnv = envDirty_;
        uploadTex = textureDirty_;
        meshDirty_ = envDirty_ = textureDirty_ = false;
    }

    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glViewport(0, 0, surfaceW_, surfaceH_);
    glClearColor(0.07f, 0.075f, 0.085f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    if (!glReady_) return false;

    std::string err;
    if (uploadEnv && !renderer_.setEnvironment(env_, err)) {
        std::lock_guard<std::mutex> lock(mutex_);
        pendingError_ = err;
    }
    if (uploadTex) renderer_.setUserTexture(&texture_, err);
    if (uploadEnv || uploadTex) lastHash_ = 0;
    if (uploadMesh) {
        if (!renderer_.setMesh(mesh_, err)) {
            std::lock_guard<std::mutex> lock(mutex_);
            pendingError_ = err;
        }
        lastHash_ = 0;
    }

    const float aspect = (float)surfaceW_ / (float)surfaceH_;
    if (renderer_.hasMesh() && !cameraFramed_) {
        cam_.azimuth = -60.0f;
        cam_.elevation = scene.camera.elevation;
        cam_.frame(renderer_.sphereCenter(), renderer_.framingRadius(scene), fovYForFocalLength(scene.camera.focalLength, aspect),
                   aspect, scene.camera.fill);
        cameraFramed_ = true;
    }
    applyCameraOps(ops, aspect);

    // ---- export ----
    if (cancel) exporter_.cancel();
    if (req.pending && !renderer_.hasMesh()) {
        // The model is still loading: keep the request until it arrives.
        std::lock_guard<std::mutex> lock(mutex_);
        if (!exportRequest_.pending) exportRequest_ = req;
    } else if (req.pending) {
        startPendingExport(scene, req);
    }
    bool wasActive = exporter_.active();
    if (exporter_.active()) exporter_.step(renderer_, 40.0);
    {
        std::lock_guard<std::mutex> lock(mutex_);
        exportStatus_ = {exporter_.active(), exporter_.progress(), exporter_.framesDone(), exporter_.totalFrames(),
                         exporter_.elapsedSeconds(), exporter_.etaSeconds()};
    }
    if (wasActive && !exporter_.active()) {
        std::lock_guard<std::mutex> lock(mutex_);
        exportFinished_ = true;
        exportOk_ = exporter_.succeeded();
        exportError_ = exporter_.error();
    }
    if (exporter_.active() || (exporter_.hasPreview() && wasActive)) {
        GlTargets& ert = exporter_.targets();
        if (ert.outTex) {
            float s = std::min((float)surfaceW_ / ert.width, (float)surfaceH_ / ert.height);
            int dw = (int)(ert.width * s), dh = (int)(ert.height * s);
            renderer_.blit(ert, 0, (surfaceW_ - dw) / 2, (surfaceH_ - dh) / 2, dw, dh,
                           scene.environment.background == Background::Transparent);
        }
        return true;
    }

    // ---- viewport ----
    int rw = surfaceW_, rh = surfaceH_;
    int vx = 0, vy = 0;
    if (preview) {
        previewFrame_ = std::fmod(previewFrame_ + dt * scene.turntable.fps, (double)scene.turntable.frameCount());
        float exportAspect = (float)scene.output.width / (float)scene.output.height;
        if (aspect > exportAspect) rw = (int)(surfaceH_ * exportAspect);
        else rh = (int)(surfaceW_ / exportAspect);
        vx = (surfaceW_ - rw) / 2;
        vy = (surfaceH_ - rh) / 2;
    }
    if (!renderer_.ensureTargets(viewRT_, rw, rh, err)) {
        std::lock_guard<std::mutex> lock(mutex_);
        pendingError_ = err;
        return false;
    }
    std::string sj = sceneToJson(scene);
    uint64_t h = fnv(sj.data(), sj.size());
    float camState[7] = {cam_.azimuth, cam_.elevation, cam_.distance, cam_.target.x, cam_.target.y, cam_.target.z,
                         (float)previewFrame_};
    h = fnv(camState, sizeof(camState), h);
    int dims[3] = {rw, rh, preview ? 1 : 0};
    h = fnv(dims, sizeof(dims), h);
    if (h != lastHash_) {
        lastHash_ = h;
        sampleIndex_ = 0;
    }
    const int maxSamples = preview ? 1 : maxSamples_;
    const bool moving = ops.orbitX != 0 || ops.orbitY != 0 || ops.panX != 0 || ops.panY != 0 || ops.zoomFactor != 1;
    auto start = Clock::now();
    while (sampleIndex_ < maxSamples) {
        SampleInput in;
        float radius = renderer_.framingRadius(scene);
        float startAz = scene.turntable.startFromViewport ? cam_.azimuth : scene.turntable.startAngle;
        if (preview) in = makeTurntableSample(scene, renderer_.sphereCenter(), radius, rw, rh, previewFrame_, 0, 1, startAz);
        else in = makeOrbitSample(scene, cam_, radius, rw, rh, sampleIndex_);
        in.fullQuality = !moving;
        renderer_.renderSample(viewRT_, scene, in);
        ++sampleIndex_;
        if (std::chrono::duration<double, std::milli>(Clock::now() - start).count() > 12.0 || moving) break;
    }
    renderer_.resolve(viewRT_, false);
    renderer_.blit(viewRT_, 0, vx, vy, rw, rh, scene.environment.background == Background::Transparent);

    std::lock_guard<std::mutex> lock(mutex_);
    return preview || sampleIndex_ < maxSamples || modelBusy_ || envBusy_;
}

}  // namespace spindle
