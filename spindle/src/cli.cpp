#include "cli.h"

#include "exporter.h"
#include "gpu.h"
#include "renderer.h"

#include <shlobj.h>

#include <cstdio>
#include <cstring>

#include "../third_party/stb/stb_image_write.h"

namespace spindle {

static std::string knownFolder(REFKNOWNFOLDERID id) {
    PWSTR path = nullptr;
    std::string out;
    if (SUCCEEDED(SHGetKnownFolderPath(id, KF_FLAG_CREATE, nullptr, &path))) out = narrow(path);
    CoTaskMemFree(path);
    return out;
}

std::string appDataDir() {
    std::string dir = knownFolder(FOLDERID_RoamingAppData);
    if (dir.empty()) dir = exeDir();
    dir += "\\Spindle";
    CreateDirectoryW(widen(dir).c_str(), nullptr);
    return dir;
}

std::string localCacheDir() {
    std::string dir = knownFolder(FOLDERID_LocalAppData);
    if (dir.empty()) return {};
    dir += "\\Spindle";
    CreateDirectoryW(widen(dir).c_str(), nullptr);
    dir += "\\shadercache";
    CreateDirectoryW(widen(dir).c_str(), nullptr);
    return dir;
}

std::string exeDir() {
    wchar_t buf[MAX_PATH];
    DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
    std::string p = narrow(std::wstring(buf, n));
    size_t s = p.find_last_of("\\/");
    return s == std::string::npos ? "." : p.substr(0, s);
}

bool isCliInvocation(const std::vector<std::string>& args) {
    if (args.empty()) return false;
    const std::string& a = args[0];
    return a == "render" || a == "still" || a == "--list-gpus" || a == "--help" || a == "-h" || a == "/?" ||
           a == "--version";
}

static void usage() {
    std::printf(
        "Spindle - STL viewer and turntable renderer\n"
        "\n"
        "Usage:\n"
        "  Spindle [model.stl]                         open the viewer\n"
        "  Spindle render model.stl -o out.mp4 [opts]  render a turntable without a window\n"
        "  Spindle still  model.stl -o out.png [opts]  render one image\n"
        "  Spindle --list-gpus\n"
        "\n"
        "Options:\n"
        "  --preset FILE        scene preset (.json) saved from the viewer\n"
        "  --material NAME      e.g. \"Matte PLA\", \"Gold\", \"Clear resin\"\n"
        "  --color RRGGBB       base colour (hex)\n"
        "  --env NAME|FILE      procedural environment name or an .hdr file\n"
        "  --background MODE    environment|solid|gradient|radial|transparent\n"
        "  --transparent        same as --background transparent\n"
        "  --ground MODE        none|shadow|floor|reflective\n"
        "  --res WxH            output size (default 1920x1080)\n"
        "  --fps N  --seconds S --quality draft|standard|high|ultra\n"
        "  --format FMT         mp4|png|gif|webm|prores|hevc (default: from -o extension)\n"
        "  --elevation DEG      camera elevation    --angle DEG  camera azimuth of frame 0\n"
        "  --up z|y             model up axis       --lights     enable the three-point rig\n"
        "  --frames N           render only the first N frames (testing)\n"
        "  --gpu auto|N|NAME|warp\n"
        "  --ffmpeg PATH        ffmpeg.exe for webm/prores/hevc\n"
        "\n"
        "Exit codes: 0 ok, 1 usage, 2 load error, 3 GPU error, 4 encode error.\n");
}

static bool parseHexColor(const std::string& s, vec3& out) {
    std::string h = s;
    if (!h.empty() && h[0] == '#') h = h.substr(1);
    if (h.size() != 6) return false;
    unsigned v = 0;
    if (std::sscanf(h.c_str(), "%x", &v) != 1) return false;
    out = {((v >> 16) & 255) / 255.0f, ((v >> 8) & 255) / 255.0f, (v & 255) / 255.0f};
    return true;
}

static Format formatFromPath(const std::string& path) {
    std::string e = extensionOf(path);
    if (e == ".gif") return Format::GIF;
    if (e == ".png") return Format::PNG;
    if (e == ".webm") return Format::WebM;
    if (e == ".mov") return Format::ProRes;
    return Format::MP4;
}

struct CliContext {
    Gpu gpu;
    Renderer renderer;
    Scene scene;
};

// Loads model, environment and texture for `scene` into the renderer.
static int prepare(CliContext& c, const std::string& stlPath, const std::string& gpuPref) {
    std::string err;
    if (!createGpu(c.gpu, gpuPref, err)) {
        std::fprintf(stderr, "error: %s\n", err.c_str());
        return 3;
    }
    std::printf("GPU: %s\n", c.gpu.adapter.name.c_str());
    setShaderCacheDir(localCacheDir());
    if (!c.renderer.init(c.gpu.device.get(), c.gpu.context.get(), c.gpu.adapter.dedicatedVideoMemoryMB, err)) {
        std::fprintf(stderr, "error: %s\n", err.c_str());
        return 3;
    }

    LoadResult lr = loadStlFile(stlPath);
    if (!lr.ok) {
        std::fprintf(stderr, "error: %s: %s\n", stlPath.c_str(), lr.error.c_str());
        return 2;
    }
    for (auto& w : lr.warnings) std::printf("warning: %s\n", w.c_str());
    MeshOptions mo;
    mo.up = c.scene.model.up;
    mo.creaseAngleDeg = c.scene.model.creaseAngle;
    for (int i = 0; i < 3; ++i) mo.quarterTurns[i] = c.scene.model.quarterTurns[i];
    Mesh mesh = processMesh(lr.soup, mo);
    vec3 sz = mesh.size();
    std::printf("Model: %llu triangles, %.1f x %.1f x %.1f mm\n", (unsigned long long)mesh.triangleCount(), sz.x, sz.y, sz.z);
    if (!c.renderer.setMesh(mesh, err)) {
        std::fprintf(stderr, "error: %s\n", err.c_str());
        return 3;
    }

    EnvironmentImage env;
    const std::string& src = c.scene.environment.source;
    if (isProceduralEnvironment(src)) {
        makeProceduralEnvironment(src, env);
    } else if (!loadEnvironmentFile(src, env, err)) {
        std::fprintf(stderr, "error: environment %s: %s\n", src.c_str(), err.c_str());
        return 2;
    }
    if (!c.renderer.setEnvironment(env, err)) {
        std::fprintf(stderr, "error: %s\n", err.c_str());
        return 3;
    }
    if (c.scene.material.pattern == Pattern::Image && !c.scene.material.texturePath.empty()) {
        Image8 img;
        if (!loadImage8(c.scene.material.texturePath, img, err)) {
            std::fprintf(stderr, "error: texture: %s\n", err.c_str());
            return 2;
        }
        c.renderer.setUserTexture(&img, err);
    }
    return 0;
}

int runCli(const std::vector<std::string>& args) {
    if (args.empty() || args[0] == "--help" || args[0] == "-h" || args[0] == "/?") {
        usage();
        return args.empty() ? 1 : 0;
    }
    if (args[0] == "--version") {
        std::printf("Spindle %s\n", SPINDLE_VERSION);
        return 0;
    }
    if (args[0] == "--list-gpus") {
        for (auto& a : listAdapters())
            std::printf("%d: %s (%llu MB dedicated%s)\n", a.index, a.name.c_str(), (unsigned long long)a.dedicatedVideoMemoryMB,
                        a.software ? ", software" : "");
        return 0;
    }

    const bool still = args[0] == "still";
    std::string stl, out, presetPath, gpuPref = "auto", ffmpeg;
    int frameLimit = -1;
    float angle = -60.0f;
    bool formatSet = false;
    CliContext ctx;
    Scene& s = ctx.scene;
    std::vector<std::pair<std::string, std::string>> overrides;  // applied after the preset

    for (size_t i = 1; i < args.size(); ++i) {
        const std::string& a = args[i];
        auto next = [&](std::string& v) {
            if (i + 1 >= args.size()) {
                std::fprintf(stderr, "error: %s needs a value\n", a.c_str());
                return false;
            }
            v = args[++i];
            return true;
        };
        std::string v;
        if (a == "-o" || a == "--output") {
            if (!next(out)) return 1;
        } else if (a == "--preset") {
            if (!next(presetPath)) return 1;
        } else if (a == "--gpu") {
            if (!next(gpuPref)) return 1;
        } else if (a == "--ffmpeg") {
            if (!next(ffmpeg)) return 1;
        } else if (a == "--transparent" || a == "--lights") {
            overrides.push_back({a, ""});
        } else if (a.size() > 2 && a[0] == '-' && a[1] == '-') {
            if (!next(v)) return 1;
            overrides.push_back({a, v});
        } else if (stl.empty()) {
            stl = a;
        } else {
            std::fprintf(stderr, "error: unexpected argument '%s'\n", a.c_str());
            return 1;
        }
    }
    if (stl.empty() || out.empty()) {
        usage();
        return 1;
    }
    if (!presetPath.empty()) {
        std::string err;
        if (!loadSceneFile(presetPath, s, err)) {
            std::fprintf(stderr, "error: %s\n", err.c_str());
            return 2;
        }
    }
    for (auto& [k, v] : overrides) {
        bool ok = true;
        if (k == "--material") ok = applyMaterialPreset(v, s.material);
        else if (k == "--color") ok = parseHexColor(v, s.material.baseColor);
        else if (k == "--env") s.environment.source = v;
        else if (k == "--transparent") s.environment.background = Background::Transparent;
        else if (k == "--lights") s.environment.lights.enabled = true;
        else if (k == "--background") {
            const char* ids[] = {"environment", "solid", "gradient", "radial", "transparent"};
            ok = false;
            for (int b = 0; b < 5; ++b)
                if (v == ids[b]) { s.environment.background = (Background)b; ok = true; }
        } else if (k == "--ground") {
            const char* ids[] = {"none", "shadow", "floor", "reflective"};
            ok = false;
            for (int g = 0; g < 4; ++g)
                if (v == ids[g]) { s.environment.ground = (Ground)g; ok = true; }
        } else if (k == "--res") ok = std::sscanf(v.c_str(), "%dx%d", &s.output.width, &s.output.height) == 2;
        else if (k == "--fps") s.turntable.fps = std::atoi(v.c_str());
        else if (k == "--seconds") s.turntable.seconds = (float)std::atof(v.c_str());
        else if (k == "--quality") {
            ok = false;
            for (int q = 0; q < (int)Quality::Count; ++q)
                if (toLower(v) == toLower(qualityName((Quality)q))) { s.output.quality = (Quality)q; ok = true; }
        } else if (k == "--format") {
            const char* ids[] = {"mp4", "png", "gif", "webm", "prores", "hevc"};
            ok = false;
            for (int f = 0; f < 6; ++f)
                if (v == ids[f]) { s.output.format = (Format)f; ok = formatSet = true; }
        } else if (k == "--elevation") s.camera.elevation = (float)std::atof(v.c_str());
        else if (k == "--angle") angle = (float)std::atof(v.c_str());
        else if (k == "--up") s.model.up = toLower(v) == "y" ? UpAxis::Y : UpAxis::Z;
        else if (k == "--frames") frameLimit = std::atoi(v.c_str());
        else {
            std::fprintf(stderr, "error: unknown option %s\n", k.c_str());
            return 1;
        }
        if (!ok) {
            std::fprintf(stderr, "error: invalid value for %s: %s\n", k.c_str(), v.c_str());
            return 1;
        }
    }
    // Re-validate after the overrides.
    {
        std::string err;
        sceneFromJson(sceneToJson(s), s, err);
    }
    if (!formatSet && !still) s.output.format = formatFromPath(out);
    if (s.output.format == Format::MP4 && (s.output.width % 2 || s.output.height % 2)) {
        std::fprintf(stderr, "error: MP4 needs an even width and height\n");
        return 1;
    }

    int rc = prepare(ctx, stl, gpuPref);
    if (rc) return rc;
    Renderer& r = ctx.renderer;

    if (still) {
        RenderTargets rt;
        std::string err;
        if (!r.ensureTargets(rt, s.output.width, s.output.height, err)) {
            std::fprintf(stderr, "error: %s\n", err.c_str());
            return 3;
        }
        int samples = qualitySamples(s.output.quality);
        for (int i = 0; i < samples; ++i) {
            SampleInput in = makeTurntableSample(s, r.sphereCenter(), r.sphereRadius(), rt.width, rt.height, 0, i, samples, angle);
            r.renderSample(rt, s, in);
            r.context()->Flush();
        }
        bool transparent = s.environment.background == Background::Transparent;
        r.resolve(rt, samples, !transparent);
        D3D11_TEXTURE2D_DESC d;
        rt.outTex->GetDesc(&d);
        d.Usage = D3D11_USAGE_STAGING;
        d.BindFlags = 0;
        d.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
        ComPtr<ID3D11Texture2D> staging;
        if (FAILED(r.device()->CreateTexture2D(&d, nullptr, staging.put()))) return 3;
        r.context()->CopyResource(staging.get(), rt.outTex.get());
        D3D11_MAPPED_SUBRESOURCE m;
        if (FAILED(r.context()->Map(staging.get(), 0, D3D11_MAP_READ, 0, &m))) return 3;
        std::vector<uint8_t> px((size_t)rt.width * rt.height * 4);
        for (int y = 0; y < rt.height; ++y)
            std::memcpy(&px[(size_t)y * rt.width * 4], (uint8_t*)m.pData + (size_t)y * m.RowPitch, (size_t)rt.width * 4);
        r.context()->Unmap(staging.get(), 0);
        if (!stbi_write_png(out.c_str(), rt.width, rt.height, 4, px.data(), rt.width * 4)) {
            std::fprintf(stderr, "error: could not write %s\n", out.c_str());
            return 4;
        }
        std::printf("Wrote %s (%dx%d, %d samples)\n", out.c_str(), rt.width, rt.height, samples);
        return 0;
    }

    ExportJob job;
    job.scene = s;
    job.outputPath = out;
    job.startAzimuth = angle;
    job.ffmpegPath = ffmpeg;
    job.frameLimit = frameLimit;
    Exporter ex;
    std::string err;
    if (!ex.start(r, job, err)) {
        std::fprintf(stderr, "error: %s\n", err.c_str());
        return 4;
    }
    std::printf("Rendering %d frames x %d samples at %dx%d\n", ex.totalFrames(), ex.samplesPerFrame(), s.output.width,
                s.output.height);
    int lastDecile = -1;
    while (ex.step(r, 250.0)) {
        int decile = (int)(ex.progress() * 10);
        if (decile != lastDecile) {
            lastDecile = decile;
            std::printf("%3d%%  frame %d/%d  elapsed %.0fs\n", decile * 10, ex.framesRendered(), ex.totalFrames(),
                        ex.elapsedSeconds());
            std::fflush(stdout);
        }
    }
    if (!ex.succeeded()) {
        std::fprintf(stderr, "error: %s\n", ex.error().c_str());
        return 4;
    }
    std::printf("100%%  done in %.1fs -> %s\n", ex.elapsedSeconds(),
                s.output.format == Format::PNG ? pngFramePath(out, 0).c_str() : out.c_str());
    return 0;
}

}  // namespace spindle
