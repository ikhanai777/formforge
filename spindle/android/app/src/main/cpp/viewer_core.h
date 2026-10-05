// The Android viewer's native core, independent of Android APIs so it can also
// be exercised on a desktop GL ES context (tests/gles_harness.cpp).
//
// Threading: methods marked [UI] may be called from any thread (they take a
// lock and queue work); methods marked [GL] must run on the thread that owns
// the GL context (GLSurfaceView's render thread).
#pragma once

#include "camera.h"
#include "environment.h"
#include "gles/gl_exporter.h"
#include "gles/gl_renderer.h"
#include "mesh.h"
#include "scene.h"

#include <atomic>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace spindle {

enum class ExportKind { MP4 = 0, GIF, Still };

class ViewerCore {
public:
    using VideoSinkFactory = std::function<std::unique_ptr<GlFrameSink>()>;

    ViewerCore();
    ~ViewerCore();
    void setVideoSinkFactory(VideoSinkFactory f) { videoSinkFactory_ = std::move(f); }

    // ---- [UI] ----
    std::string sceneJson();
    void setSceneJson(const std::string& json);
    std::string applyMaterialPreset(const std::string& name);  // returns the new scene JSON
    void loadModel(const std::string& path, const std::string& displayName);
    void orbit(float dxPx, float dyPx);
    void zoom(float factor, float ndcX, float ndcY);
    void pan(float dxPx, float dyPx);
    void frameModel();
    void viewPreset(int which);  // 0 front, 1 side, 2 top, 3 default
    void setPreview(bool on);
    // Queues an export of the current scene; the GL thread starts it. Returns an
    // error message, or "" when queued.
    std::string startExport(ExportKind kind, const std::string& path);
    void cancelExport();
    std::string statusJson();

    // ---- [GL] ----
    void onSurfaceCreated();  // (re)creates every GL resource from the CPU copies
    void onSurfaceChanged(int width, int height);
    // Renders one frame into the default framebuffer. Returns true if another
    // frame is wanted soon (accumulating, preview, export or loading).
    bool onDrawFrame();

private:
    struct CameraOps {
        float orbitX = 0, orbitY = 0, panX = 0, panY = 0, zoomFactor = 1, zoomX = 0, zoomY = 0;
        bool frame = false;
        int preset = -1;
    };
    struct ExportRequest {
        bool pending = false;
        ExportKind kind = ExportKind::MP4;
        std::string path;
    };

    void pollWorkers();
    void startEnvironmentLoad(const std::string& source);
    void applyCameraOps(const CameraOps& ops, float aspect);
    void startPendingExport(const Scene& scene, const ExportRequest& req);
    void frameCameraLocked(float aspect);

    std::mutex mutex_;
    Scene scene_;
    uint64_t sceneVersion_ = 1;
    CameraOps ops_;
    ExportRequest exportRequest_;
    bool cancelRequested_ = false;
    bool preview_ = false;
    std::string pendingError_;  // one-shot message for the UI

    // Model state (CPU copies survive GL context loss).
    std::thread modelWorker_;
    Progress modelProgress_;
    std::atomic<bool> modelReady_{false};
    bool modelBusy_ = false;
    std::string modelName_, pendingModelName_;
    LoadResult pendingLoad_;
    Mesh pendingMesh_;
    Mesh mesh_;
    std::vector<std::string> warnings_;
    bool meshDirty_ = false;  // needs (re)upload
    MeshOptions appliedMeshOptions_;
    bool haveAppliedOptions_ = false;
    TriangleSoup soup_;

    // Environment.
    std::thread envWorker_;
    std::atomic<bool> envReady_{false};
    bool envBusy_ = false;
    EnvironmentImage env_, pendingEnv_;
    std::string envSource_, pendingEnvSource_, pendingEnvError_;
    bool envDirty_ = false;

    // Texture.
    Image8 texture_;
    std::string texturePath_;
    bool textureDirty_ = false;

    // GL thread state.
    GlRenderer renderer_;
    bool glReady_ = false;
    std::string glError_;
    GlTargets viewRT_;
    int surfaceW_ = 1, surfaceH_ = 1;
    OrbitCamera cam_;
    bool cameraFramed_ = false;
    int sampleIndex_ = 0;
    uint64_t lastHash_ = 0;
    int maxSamples_ = 48;
    double previewFrame_ = 0;
    std::chrono::steady_clock::time_point lastFrame_ = std::chrono::steady_clock::now();
    GlExporter exporter_;
    ExportKind exportKind_ = ExportKind::MP4;
    std::string exportPath_;
    struct ExportStatus {
        bool active = false;
        float progress = 0;
        int frame = 0, total = 0;
        double elapsed = 0, eta = -1;
    } exportStatus_;  // snapshot taken on the GL thread, read by the UI
    bool exportFinished_ = false;  // reported once to the UI
    bool exportOk_ = false;
    std::string exportError_;
    VideoSinkFactory videoSinkFactory_;
    std::string glDescription_;
};

}  // namespace spindle
