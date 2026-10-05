// Headless check of the OpenGL ES renderer on a desktop (EGL + Mesa), so the
// Android renderer can be verified without a device:
//   gles_harness MODEL OUT.png [--preset P.json] [--res WxH] [--samples N]
//                [--explode A] [--gif OUT.gif FRAMES]
#include "../src/environment.h"
#include "../src/gles/gl_exporter.h"
#include "../src/gles/gl_renderer.h"

#include <EGL/egl.h>
#include <EGL/eglext.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "../third_party/stb/stb_image_write.h"

using namespace spindle;

static bool createContext() {
    EGLDisplay dpy = EGL_NO_DISPLAY;
    auto getPlatformDisplay = (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
    if (getPlatformDisplay) dpy = getPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, nullptr);
    if (dpy == EGL_NO_DISPLAY) dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    if (!eglInitialize(dpy, nullptr, nullptr)) return false;
    eglBindAPI(EGL_OPENGL_ES_API);
    const EGLint cfgAttrs[] = {EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_NONE};
    EGLConfig cfg;
    EGLint n = 0;
    if (!eglChooseConfig(dpy, cfgAttrs, &cfg, 1, &n) || n == 0) {
        const EGLint any[] = {EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_NONE};
        if (!eglChooseConfig(dpy, any, &cfg, 1, &n) || n == 0) return false;
    }
    const EGLint ctxAttrs[] = {EGL_CONTEXT_MAJOR_VERSION, 3, EGL_NONE};
    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctxAttrs);
    if (ctx == EGL_NO_CONTEXT) return false;
    return eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx);
}

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: gles_harness MODEL OUT.png [--preset P] [--res WxH] [--samples N] [--explode A] [--gif OUT FRAMES]\n");
        return 1;
    }
    Scene scene;
    int samples = 16;
    std::string gifPath;
    int gifFrames = 0;
    for (int i = 3; i < argc; ++i) {
        std::string a = argv[i];
        std::string err;
        if (a == "--preset" && i + 1 < argc) {
            if (!loadSceneFile(argv[++i], scene, err)) { std::fprintf(stderr, "%s\n", err.c_str()); return 1; }
        } else if (a == "--res" && i + 1 < argc) {
            std::sscanf(argv[++i], "%dx%d", &scene.output.width, &scene.output.height);
        } else if (a == "--samples" && i + 1 < argc) {
            samples = std::atoi(argv[++i]);
        } else if (a == "--explode" && i + 1 < argc) {
            scene.explode.manual = (float)std::atof(argv[++i]);
        } else if (a == "--gif" && i + 2 < argc) {
            gifPath = argv[++i];
            gifFrames = std::atoi(argv[++i]);
        }
    }
    if (!createContext()) {
        std::fprintf(stderr, "EGL context creation failed\n");
        return 3;
    }
    GlRenderer r;
    std::string err;
    if (!r.init(err)) {
        std::fprintf(stderr, "init: %s\n", err.c_str());
        return 3;
    }
    std::printf("GL: %s\n", r.deviceDescription().c_str());
    LoadResult lr = loadModelFile(argv[1]);
    if (!lr.ok) {
        std::fprintf(stderr, "load: %s\n", lr.error.c_str());
        return 2;
    }
    MeshOptions mo;
    mo.up = scene.model.up;
    mo.creaseAngleDeg = scene.model.creaseAngle;
    Mesh mesh = processMesh(lr.soup, mo);
    EnvironmentImage env;
    if (isProceduralEnvironment(scene.environment.source)) makeProceduralEnvironment(scene.environment.source, env);
    else if (!loadEnvironmentFile(scene.environment.source, env, err)) { std::fprintf(stderr, "%s\n", err.c_str()); return 2; }
    if (!r.setMesh(mesh, err) || !r.setEnvironment(env, err)) {
        std::fprintf(stderr, "upload: %s\n", err.c_str());
        return 3;
    }
    GlTargets rt;
    if (!r.ensureTargets(rt, scene.output.width, scene.output.height, err)) {
        std::fprintf(stderr, "targets: %s\n", err.c_str());
        return 3;
    }
    for (int i = 0; i < samples; ++i) {
        SampleInput in = makeTurntableSample(scene, r.sphereCenter(), r.framingRadius(scene), rt.width, rt.height, 0, i,
                                             samples, -60.0f);
        r.renderSample(rt, scene, in);
    }
    bool transparent = scene.environment.background == Background::Transparent;
    r.resolve(rt, !transparent);
    std::vector<uint8_t> px;
    r.readPixels(rt, px);
    GLenum glErr = glGetError();
    if (!stbi_write_png(argv[2], rt.width, rt.height, 4, px.data(), rt.width * 4)) return 4;
    std::printf("Wrote %s (%dx%d, %d samples, %d parts, glError 0x%x)\n", argv[2], rt.width, rt.height, samples,
                (int)mesh.parts.size(), glErr);

    if (!gifPath.empty()) {
        GlExportJob job;
        job.scene = scene;
        job.scene.output.quality = Quality::Draft;
        job.startAzimuth = -60.0f;
        job.frameLimit = gifFrames;
        GlSinkConfig cfg;
        cfg.path = gifPath;
        GlExporter ex;
        if (!ex.start(r, job, makeGifSink(), cfg, err)) {
            std::fprintf(stderr, "export: %s\n", err.c_str());
            return 4;
        }
        while (ex.step(r, 100.0)) {
        }
        if (!ex.succeeded()) {
            std::fprintf(stderr, "export: %s\n", ex.error().c_str());
            return 4;
        }
        std::printf("Wrote %s (%d frames)\n", gifPath.c_str(), ex.totalFrames());
    }
    return glErr == GL_NO_ERROR ? 0 : 5;
}
