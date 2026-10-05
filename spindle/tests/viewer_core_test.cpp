// Desktop test of the Android viewer core (android/app/src/main/cpp/viewer_core.*)
// on an EGL pbuffer: load a model asynchronously, accumulate the viewport,
// change the scene through JSON, export a GIF and a still, and check status.
//   viewer_core_test MODEL OUTDIR
#include "../android/app/src/main/cpp/viewer_core.h"

#include <EGL/egl.h>
#include <EGL/eglext.h>

#include <chrono>
#include <cstdio>
#include <thread>

#include "../third_party/nlohmann/json.hpp"

using namespace spindle;
using json = nlohmann::json;

static int g_fail = 0;
#define CHECK(c)                                                         \
    do {                                                                 \
        if (!(c)) {                                                      \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #c);     \
            ++g_fail;                                                    \
        }                                                                \
    } while (0)

int main(int argc, char** argv) {
    if (argc < 3) return 1;
    std::string out = argv[2];
    EGLDisplay dpy = EGL_NO_DISPLAY;
    auto getPlatformDisplay = (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
    if (getPlatformDisplay) dpy = getPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, nullptr);
    if (dpy == EGL_NO_DISPLAY) dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    eglInitialize(dpy, nullptr, nullptr);
    eglBindAPI(EGL_OPENGL_ES_API);
    const EGLint cfgAttrs[] = {EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
                               EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_NONE};
    EGLConfig cfg;
    EGLint n = 0;
    if (!eglChooseConfig(dpy, cfgAttrs, &cfg, 1, &n) || !n) { std::printf("no EGL config\n"); return 3; }
    const EGLint pb[] = {EGL_WIDTH, 540, EGL_HEIGHT, 960, EGL_NONE};  // a portrait phone
    EGLSurface surf = eglCreatePbufferSurface(dpy, cfg, pb);
    const EGLint ctxAttrs[] = {EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE};
    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctxAttrs);
    eglMakeCurrent(dpy, surf, surf, ctx);

    ViewerCore core;
    core.onSurfaceCreated();
    core.onSurfaceChanged(540, 960);
    json st = json::parse(core.statusJson());
    std::printf("GL: %s\n", st["gl"].get<std::string>().c_str());
    CHECK(st["glError"].get<std::string>().empty());

    // The UI thread loads; the GL thread keeps drawing until the model is up.
    std::thread ui([&] { core.loadModel(argv[1], "model"); });
    ui.join();
    int frames = 0;
    auto t0 = std::chrono::steady_clock::now();
    while (frames < 2000) {
        bool more = core.onDrawFrame();
        ++frames;
        st = json::parse(core.statusJson());
        if (st.contains("model") && !more) break;
    }
    double secs = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    CHECK(st.contains("model"));
    std::printf("loaded and converged in %d frames (%.1fs), samples %d, parts %d\n", frames, secs,
                st["samples"].get<int>(), (int)st["model"]["parts"].size());
    CHECK(st["samples"].get<int>() == st["maxSamples"].get<int>());

    // Scene edits arrive as JSON from the UI.
    json scene = json::parse(core.sceneJson());
    scene["environment"]["source"] = "Sunset";
    scene["environment"]["background"] = "environment";
    scene["explode"]["manual"] = 0.7;
    scene["output"]["width"] = 320;
    scene["output"]["height"] = 320;
    scene["output"]["quality"] = "draft";
    scene["turntable"]["seconds"] = 1.0;
    scene["turntable"]["fps"] = 12;
    core.setSceneJson(scene.dump());
    std::string preset = core.applyMaterialPreset("Gold");
    CHECK(json::parse(preset)["material"]["metalness"].get<float>() == 1.0f);
    for (int i = 0; i < 200 && core.onDrawFrame(); ++i) {
    }
    core.orbit(40, -10);
    core.zoom(1.3f, 0.1f, 0.0f);
    core.onDrawFrame();

    // GIF export, then a still.
    CHECK(core.startExport(ExportKind::GIF, out + "/core.gif").empty());
    CHECK(!core.startExport(ExportKind::GIF, out + "/again.gif").empty());  // one at a time
    bool finished = false;
    for (int i = 0; i < 5000 && !finished; ++i) {
        core.onDrawFrame();
        st = json::parse(core.statusJson());
        if (st["export"].contains("finished")) {
            finished = true;
            CHECK(st["export"]["ok"].get<bool>());
            std::printf("gif: %d frames, ok=%d %s\n", st["export"]["total"].get<int>(), (int)st["export"]["ok"].get<bool>(),
                        st["export"]["error"].get<std::string>().c_str());
        }
    }
    CHECK(finished);
    CHECK(core.startExport(ExportKind::Still, out + "/core_still.png").empty());
    finished = false;
    for (int i = 0; i < 5000 && !finished; ++i) {
        core.onDrawFrame();
        st = json::parse(core.statusJson());
        if (st["export"].contains("finished")) {
            finished = true;
            CHECK(st["export"]["ok"].get<bool>());
        }
    }
    CHECK(finished);
    // MP4 needs the Android encoder: without a factory it is refused cleanly.
    CHECK(!core.startExport(ExportKind::MP4, out + "/x.mp4").empty());

    // A lost context (Android pause) is rebuilt from the CPU copies.
    core.onSurfaceCreated();
    core.onSurfaceChanged(540, 960);
    for (int i = 0; i < 400 && core.onDrawFrame(); ++i) {
    }
    st = json::parse(core.statusJson());
    CHECK(st.contains("model") && st["samples"].get<int>() == st["maxSamples"].get<int>());

    // Save what the "screen" shows.
    std::vector<uint8_t> px(540 * 960 * 4);
    glReadPixels(0, 0, 540, 960, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
    FILE* f = std::fopen((out + "/core_screen.rgba").c_str(), "wb");
    if (f) {
        std::fwrite(px.data(), 1, px.size(), f);
        std::fclose(f);
    }
    std::printf(g_fail ? "%d failure(s)\n" : "viewer core test passed\n", g_fail);
    return g_fail ? 1 : 0;
}
