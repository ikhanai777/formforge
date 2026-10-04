#include "scene.h"

#include "../third_party/nlohmann/json.hpp"

#include <cstdio>
#include <functional>
#include <map>

namespace spindle {

using json = nlohmann::json;

// ---- enum names ----------------------------------------------------------------

static const char* kPatternNames[] = {"None", "Image texture", "Wood", "Marble", "Carbon fibre", "Checker", "Granite"};
static const char* kBackgroundNames[] = {"Environment", "Solid colour", "Vertical gradient", "Radial gradient", "Transparent"};
static const char* kGroundNames[] = {"None", "Shadow catcher", "Floor", "Reflective floor"};
static const char* kTonemapNames[] = {"ACES", "AgX", "Linear"};
static const char* kModeNames[] = {"Rotate object", "Orbit camera"};
static const char* kEasingNames[] = {"Linear (seamless loop)", "Ease in-out", "Hold, then spin"};
static const char* kFormatNames[] = {"MP4 (H.264)", "PNG sequence", "GIF", "WebM VP9 (ffmpeg)", "ProRes 4444 (ffmpeg)", "MP4 H.265 (ffmpeg)"};
static const char* kFormatExt[] = {".mp4", ".png", ".gif", ".webm", ".mov", ".mp4"};
static const char* kQualityNames[] = {"Draft", "Standard", "High", "Ultra"};
static const int kQualitySamples[] = {1, 16, 64, 256};

const char* patternName(Pattern p) { return kPatternNames[(int)p]; }
const char* backgroundName(Background b) { return kBackgroundNames[(int)b]; }
const char* groundName(Ground g) { return kGroundNames[(int)g]; }
const char* tonemapName(Tonemap t) { return kTonemapNames[(int)t]; }
const char* turntableModeName(TurntableMode m) { return kModeNames[(int)m]; }
const char* easingName(Easing e) { return kEasingNames[(int)e]; }
const char* formatName(Format f) { return kFormatNames[(int)f]; }
const char* formatExtension(Format f) { return kFormatExt[(int)f]; }
bool formatNeedsFfmpeg(Format f) { return f == Format::WebM || f == Format::ProRes || f == Format::HEVC; }
bool formatSupportsAlpha(Format f) { return f == Format::PNG || f == Format::GIF || f == Format::WebM || f == Format::ProRes; }
const char* qualityName(Quality q) { return kQualityNames[(int)q]; }
static const char* kExplodeTimingNames[] = {"Explode, then reassemble", "Explode and hold", "Assemble from parts"};
const char* explodeTimingName(ExplodeTiming t) { return kExplodeTimingNames[(int)t]; }
int qualitySamples(Quality q) { return kQualitySamples[(int)q]; }

// Stable identifiers for the JSON files (independent of the display names).
static const char* kPatternIds[] = {"none", "image", "wood", "marble", "carbon", "checker", "granite"};
static const char* kBackgroundIds[] = {"environment", "solid", "gradient", "radial", "transparent"};
static const char* kGroundIds[] = {"none", "shadow_catcher", "floor", "reflective"};
static const char* kTonemapIds[] = {"aces", "agx", "linear"};
static const char* kModeIds[] = {"rotate_object", "orbit_camera"};
static const char* kEasingIds[] = {"linear", "ease_in_out", "hold"};
static const char* kFormatIds[] = {"mp4", "png", "gif", "webm", "prores", "hevc"};
static const char* kQualityIds[] = {"draft", "standard", "high", "ultra"};
static const char* kExplodeTimingIds[] = {"explode_return", "explode_hold", "assemble"};

template <class E, size_t N>
static void readEnum(const json& j, const char* key, E& out, const char* (&ids)[N]) {
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return;
    std::string v = toLower(it->get<std::string>());
    for (size_t i = 0; i < N; ++i)
        if (v == ids[i]) out = (E)i;
}

// ---- material presets -------------------------------------------------------

namespace {
struct MaterialPreset {
    const char* name;
    std::function<void(MaterialSettings&)> apply;
};

MaterialSettings base(const char* name) {
    MaterialSettings m;
    m.preset = name;
    m.layerLines = false;
    return m;
}

const std::vector<MaterialPreset>& presets() {
    static const std::vector<MaterialPreset> list = {
        {"Matte PLA", [](MaterialSettings& m) {
             m = base("Matte PLA");
             m.baseColor = {0.85f, 0.32f, 0.12f};
             m.roughness = 0.65f;
             m.layerLines = true;
         }},
        {"Glossy PETG", [](MaterialSettings& m) {
             m = base("Glossy PETG");
             m.baseColor = {0.08f, 0.35f, 0.75f};
             m.roughness = 0.25f;
             m.clearcoat = 0.3f;
             m.layerLines = true;
             m.layerDepth = 0.2f;
         }},
        {"Silk PLA", [](MaterialSettings& m) {
             m = base("Silk PLA");
             m.baseColor = {0.80f, 0.55f, 0.20f};
             m.roughness = 0.28f;
             m.metalness = 0.55f;
             m.sheen = 0.5f;
             m.layerLines = true;
             m.layerDepth = 0.25f;
         }},
        {"Grey resin (SLA)", [](MaterialSettings& m) {
             m = base("Grey resin (SLA)");
             m.baseColor = {0.45f, 0.46f, 0.47f};
             m.roughness = 0.4f;
         }},
        {"Clay", [](MaterialSettings& m) {
             m = base("Clay");
             m.baseColor = {0.72f, 0.45f, 0.32f};
             m.roughness = 0.9f;
             m.noiseStrength = 0.15f;
             m.noiseScale = 1.0f;
         }},
        {"Glazed ceramic", [](MaterialSettings& m) {
             m = base("Glazed ceramic");
             m.baseColor = {0.92f, 0.92f, 0.90f};
             m.roughness = 0.5f;
             m.clearcoat = 1.0f;
             m.clearcoatRoughness = 0.04f;
         }},
        {"Brushed aluminium", [](MaterialSettings& m) {
             m = base("Brushed aluminium");
             m.baseColor = {0.91f, 0.92f, 0.92f};
             m.metalness = 1.0f;
             m.roughness = 0.35f;
             m.noiseStrength = 0.25f;
             m.noiseScale = 0.15f;
             m.noiseStretch = 40.0f;
         }},
        {"Polished chrome", [](MaterialSettings& m) {
             m = base("Polished chrome");
             m.baseColor = {0.95f, 0.95f, 0.96f};
             m.metalness = 1.0f;
             m.roughness = 0.05f;
         }},
        {"Gold", [](MaterialSettings& m) {
             m = base("Gold");
             m.baseColor = {1.0f, 0.80f, 0.42f};
             m.metalness = 1.0f;
             m.roughness = 0.2f;
         }},
        {"Rubber", [](MaterialSettings& m) {
             m = base("Rubber");
             m.baseColor = {0.06f, 0.06f, 0.065f};
             m.roughness = 0.85f;
         }},
        {"Clear resin", [](MaterialSettings& m) {
             m = base("Clear resin");
             m.baseColor = {0.95f, 0.97f, 1.0f};
             m.roughness = 0.05f;
             m.transmission = 1.0f;
             m.ior = 1.5f;
             m.transmissionTint = {0.85f, 0.93f, 1.0f};
         }},
        {"Frosted resin", [](MaterialSettings& m) {
             m = base("Frosted resin");
             m.baseColor = {0.95f, 0.95f, 0.95f};
             m.roughness = 0.45f;
             m.transmission = 0.9f;
             m.ior = 1.5f;
             m.transmissionTint = {0.95f, 0.85f, 0.75f};
         }},
        {"Walnut", [](MaterialSettings& m) {
             m = base("Walnut");
             m.baseColor = {0.45f, 0.27f, 0.14f};
             m.patternColor = {0.22f, 0.11f, 0.05f};
             m.pattern = Pattern::Wood;
             m.patternScale = 12.0f;
             m.roughness = 0.55f;
             m.clearcoat = 0.25f;
         }},
        {"White marble", [](MaterialSettings& m) {
             m = base("White marble");
             m.baseColor = {0.93f, 0.92f, 0.90f};
             m.patternColor = {0.35f, 0.36f, 0.40f};
             m.pattern = Pattern::Marble;
             m.patternScale = 60.0f;
             m.roughness = 0.2f;
             m.clearcoat = 0.4f;
         }},
        {"Carbon fibre", [](MaterialSettings& m) {
             m = base("Carbon fibre");
             m.baseColor = {0.30f, 0.30f, 0.32f};
             m.patternColor = {0.03f, 0.03f, 0.035f};
             m.pattern = Pattern::Carbon;
             m.patternScale = 6.0f;
             m.roughness = 0.35f;
             m.clearcoat = 1.0f;
         }},
        {"Granite", [](MaterialSettings& m) {
             m = base("Granite");
             m.baseColor = {0.55f, 0.53f, 0.52f};
             m.patternColor = {0.12f, 0.11f, 0.11f};
             m.pattern = Pattern::Granite;
             m.patternScale = 4.0f;
             m.roughness = 0.45f;
         }},
    };
    return list;
}
}  // namespace

const std::vector<std::string>& materialPresetNames() {
    static std::vector<std::string> names = [] {
        std::vector<std::string> n;
        for (auto& p : presets()) n.push_back(p.name);
        return n;
    }();
    return names;
}

bool applyMaterialPreset(const std::string& name, MaterialSettings& m) {
    for (auto& p : presets())
        if (toLower(name) == toLower(p.name)) {
            bool wire = m.wireframe, fileColors = m.useFileColors, fileFinish = m.useFileFinish;
            p.apply(m);
            m.wireframe = wire;  // an overlay, not part of the look
            m.useFileColors = fileColors;
            m.useFileFinish = fileFinish;
            return true;
        }
    return false;
}

const std::vector<std::string>& proceduralEnvironmentNames() {
    static const std::vector<std::string> names = {
        "Studio Softbox", "Studio High-Key", "Studio Dark", "Overcast", "Daylight", "Sunset", "Warm Interior", "Night City",
    };
    return names;
}

bool isProceduralEnvironment(const std::string& source) {
    for (auto& n : proceduralEnvironmentNames())
        if (toLower(n) == toLower(source)) return true;
    return false;
}

const std::vector<ResolutionPreset>& resolutionPresets() {
    static const std::vector<ResolutionPreset> list = {
        {"720p (1280x720)", 1280, 720},       {"1080p (1920x1080)", 1920, 1080},
        {"1440p (2560x1440)", 2560, 1440},    {"4K UHD (3840x2160)", 3840, 2160},
        {"Square 1080 (1080x1080)", 1080, 1080}, {"Vertical 1080x1920", 1080, 1920},
        {"GIF square (600x600)", 600, 600},   {"GIF wide (800x450)", 800, 450},
    };
    return list;
}

// ---- JSON ------------------------------------------------------------------------

static json v3(const vec3& v) { return json::array({v.x, v.y, v.z}); }

static void read(const json& j, const char* k, float& v) {
    auto it = j.find(k);
    if (it != j.end() && it->is_number()) v = it->get<float>();
}
static void read(const json& j, const char* k, int& v) {
    auto it = j.find(k);
    if (it != j.end() && it->is_number()) v = (int)std::lround(it->get<double>());
}
static void read(const json& j, const char* k, bool& v) {
    auto it = j.find(k);
    if (it != j.end() && it->is_boolean()) v = it->get<bool>();
}
static void read(const json& j, const char* k, std::string& v) {
    auto it = j.find(k);
    if (it != j.end() && it->is_string()) v = it->get<std::string>();
}
static void read(const json& j, const char* k, vec3& v) {
    auto it = j.find(k);
    if (it != j.end() && it->is_array() && it->size() == 3 && (*it)[0].is_number())
        v = {(*it)[0].get<float>(), (*it)[1].get<float>(), (*it)[2].get<float>()};
}
static const json& sub(const json& j, const char* k) {
    static const json empty = json::object();
    auto it = j.find(k);
    return it != j.end() && it->is_object() ? *it : empty;
}

std::string sceneToJson(const Scene& s) {
    json j;
    j["version"] = 1;
    const auto& mo = s.model;
    j["model"] = {{"up", mo.up == UpAxis::Z ? "z" : "y"},
                  {"quarterTurns", {mo.quarterTurns[0], mo.quarterTurns[1], mo.quarterTurns[2]}},
                  {"creaseAngle", mo.creaseAngle}};
    const auto& m = s.material;
    j["material"] = {{"preset", m.preset},
                     {"useFileColors", m.useFileColors},
                     {"useFileFinish", m.useFileFinish},
                     {"baseColor", v3(m.baseColor)},
                     {"roughness", m.roughness},
                     {"metalness", m.metalness},
                     {"clearcoat", m.clearcoat},
                     {"clearcoatRoughness", m.clearcoatRoughness},
                     {"sheen", m.sheen},
                     {"transmission", m.transmission},
                     {"ior", m.ior},
                     {"transmissionTint", v3(m.transmissionTint)},
                     {"layerLines", {{"enabled", m.layerLines}, {"height", m.layerHeight}, {"depth", m.layerDepth}}},
                     {"noise", {{"strength", m.noiseStrength}, {"scale", m.noiseScale}, {"stretch", m.noiseStretch}}},
                     {"pattern", {{"type", kPatternIds[(int)m.pattern]},
                                  {"color", v3(m.patternColor)},
                                  {"scale", m.patternScale},
                                  {"strength", m.patternStrength},
                                  {"sharpness", m.triplanarSharpness},
                                  {"texture", m.texturePath}}},
                     {"gradient", {{"enabled", m.gradient}, {"color", v3(m.gradientColor)}}},
                     {"wireframe", {{"enabled", m.wireframe}, {"color", v3(m.wireColor)}, {"width", m.wireWidth}}}};
    const auto& e = s.environment;
    j["environment"] = {{"source", e.source},
                        {"rotation", e.rotation},
                        {"intensity", e.intensity},
                        {"saturation", e.saturation},
                        {"background", kBackgroundIds[(int)e.background]},
                        {"backgroundBlur", e.backgroundBlur},
                        {"backgroundColor", v3(e.backgroundColor)},
                        {"backgroundColor2", v3(e.backgroundColor2)},
                        {"ground", kGroundIds[(int)e.ground]},
                        {"floorColor", v3(e.floorColor)},
                        {"floorRoughness", e.floorRoughness},
                        {"reflection", e.reflection},
                        {"shadowStrength", e.shadowStrength},
                        {"shadowSoftness", e.shadowSoftness},
                        {"aoStrength", e.aoStrength},
                        {"aoRadius", e.aoRadius},
                        {"lights", {{"enabled", e.lights.enabled},
                                    {"intensity", e.lights.intensity},
                                    {"keyAzimuth", e.lights.keyAzimuth},
                                    {"keyElevation", e.lights.keyElevation},
                                    {"keyColor", v3(e.lights.keyColor)},
                                    {"fill", e.lights.fill},
                                    {"rim", e.lights.rim}}},
                        {"tonemap", kTonemapIds[(int)e.tonemap]},
                        {"exposure", e.exposure}};
    const auto& c = s.camera;
    j["camera"] = {{"focalLength", c.focalLength}, {"orthographic", c.orthographic}, {"elevation", c.elevation},
                   {"fill", c.fill}, {"depthOfField", c.depthOfField}, {"fStop", c.fStop}};
    const auto& t = s.turntable;
    j["turntable"] = {{"mode", kModeIds[(int)t.mode]}, {"seconds", t.seconds}, {"fps", t.fps},
                      {"rotations", t.rotations}, {"direction", t.clockwise ? "cw" : "ccw"},
                      {"easing", kEasingIds[(int)t.easing]}, {"holdSeconds", t.holdSeconds},
                      {"startFromViewport", t.startFromViewport}, {"startAngle", t.startAngle},
                      {"bob", t.bob}, {"shutter", t.shutter}};
    const auto& x = s.explode;
    j["explode"] = {{"animate", x.animate}, {"distance", x.distance}, {"stagger", x.stagger},
                    {"timing", kExplodeTimingIds[(int)x.timing]}, {"start", x.start}, {"end", x.end},
                    {"manual", x.manual}};
    const auto& o = s.output;
    j["output"] = {{"format", kFormatIds[(int)o.format]}, {"width", o.width}, {"height", o.height},
                   {"quality", kQualityIds[(int)o.quality]}, {"bitrateMbps", o.bitrateMbps}};
    return j.dump(2);
}

bool sceneFromJson(const std::string& text, Scene& s, std::string& error) {
    json j = json::parse(text, nullptr, false, true);
    if (j.is_discarded() || !j.is_object()) {
        error = "The preset is not valid JSON.";
        return false;
    }
    {
        const json& mo = sub(j, "model");
        std::string up;
        read(mo, "up", up);
        if (!up.empty()) s.model.up = toLower(up) == "y" ? UpAxis::Y : UpAxis::Z;
        auto it = mo.find("quarterTurns");
        if (it != mo.end() && it->is_array() && it->size() == 3)
            for (int i = 0; i < 3; ++i)
                if ((*it)[i].is_number()) s.model.quarterTurns[i] = (*it)[i].get<int>() & 3;
        read(mo, "creaseAngle", s.model.creaseAngle);
    }
    {
        const json& m = sub(j, "material");
        auto& d = s.material;
        // A preset name sets the defaults; explicit fields then override them.
        std::string preset;
        read(m, "preset", preset);
        if (!preset.empty() && !applyMaterialPreset(preset, d)) d.preset = preset;
        read(m, "useFileColors", d.useFileColors);
        read(m, "useFileFinish", d.useFileFinish);
        read(m, "baseColor", d.baseColor);
        read(m, "roughness", d.roughness);
        read(m, "metalness", d.metalness);
        read(m, "clearcoat", d.clearcoat);
        read(m, "clearcoatRoughness", d.clearcoatRoughness);
        read(m, "sheen", d.sheen);
        read(m, "transmission", d.transmission);
        read(m, "ior", d.ior);
        read(m, "transmissionTint", d.transmissionTint);
        const json& ll = sub(m, "layerLines");
        read(ll, "enabled", d.layerLines);
        read(ll, "height", d.layerHeight);
        read(ll, "depth", d.layerDepth);
        const json& n = sub(m, "noise");
        read(n, "strength", d.noiseStrength);
        read(n, "scale", d.noiseScale);
        read(n, "stretch", d.noiseStretch);
        const json& p = sub(m, "pattern");
        readEnum(p, "type", d.pattern, kPatternIds);
        read(p, "color", d.patternColor);
        read(p, "scale", d.patternScale);
        read(p, "strength", d.patternStrength);
        read(p, "sharpness", d.triplanarSharpness);
        read(p, "texture", d.texturePath);
        const json& g = sub(m, "gradient");
        read(g, "enabled", d.gradient);
        read(g, "color", d.gradientColor);
        const json& w = sub(m, "wireframe");
        read(w, "enabled", d.wireframe);
        read(w, "color", d.wireColor);
        read(w, "width", d.wireWidth);
    }
    {
        const json& e = sub(j, "environment");
        auto& d = s.environment;
        read(e, "source", d.source);
        read(e, "rotation", d.rotation);
        read(e, "intensity", d.intensity);
        read(e, "saturation", d.saturation);
        readEnum(e, "background", d.background, kBackgroundIds);
        read(e, "backgroundBlur", d.backgroundBlur);
        read(e, "backgroundColor", d.backgroundColor);
        read(e, "backgroundColor2", d.backgroundColor2);
        readEnum(e, "ground", d.ground, kGroundIds);
        read(e, "floorColor", d.floorColor);
        read(e, "floorRoughness", d.floorRoughness);
        read(e, "reflection", d.reflection);
        read(e, "shadowStrength", d.shadowStrength);
        read(e, "shadowSoftness", d.shadowSoftness);
        read(e, "aoStrength", d.aoStrength);
        read(e, "aoRadius", d.aoRadius);
        const json& l = sub(e, "lights");
        read(l, "enabled", d.lights.enabled);
        read(l, "intensity", d.lights.intensity);
        read(l, "keyAzimuth", d.lights.keyAzimuth);
        read(l, "keyElevation", d.lights.keyElevation);
        read(l, "keyColor", d.lights.keyColor);
        read(l, "fill", d.lights.fill);
        read(l, "rim", d.lights.rim);
        readEnum(e, "tonemap", d.tonemap, kTonemapIds);
        read(e, "exposure", d.exposure);
    }
    {
        const json& c = sub(j, "camera");
        auto& d = s.camera;
        read(c, "focalLength", d.focalLength);
        read(c, "orthographic", d.orthographic);
        read(c, "elevation", d.elevation);
        read(c, "fill", d.fill);
        read(c, "depthOfField", d.depthOfField);
        read(c, "fStop", d.fStop);
    }
    {
        const json& t = sub(j, "turntable");
        auto& d = s.turntable;
        readEnum(t, "mode", d.mode, kModeIds);
        read(t, "seconds", d.seconds);
        read(t, "fps", d.fps);
        read(t, "rotations", d.rotations);
        std::string dir;
        read(t, "direction", dir);
        if (!dir.empty()) d.clockwise = toLower(dir) == "cw";
        readEnum(t, "easing", d.easing, kEasingIds);
        read(t, "holdSeconds", d.holdSeconds);
        read(t, "startFromViewport", d.startFromViewport);
        read(t, "startAngle", d.startAngle);
        read(t, "bob", d.bob);
        read(t, "shutter", d.shutter);
    }
    {
        const json& x = sub(j, "explode");
        auto& d = s.explode;
        read(x, "animate", d.animate);
        read(x, "distance", d.distance);
        read(x, "stagger", d.stagger);
        readEnum(x, "timing", d.timing, kExplodeTimingIds);
        read(x, "start", d.start);
        read(x, "end", d.end);
        read(x, "manual", d.manual);
    }
    {
        const json& o = sub(j, "output");
        auto& d = s.output;
        readEnum(o, "format", d.format, kFormatIds);
        read(o, "width", d.width);
        read(o, "height", d.height);
        readEnum(o, "quality", d.quality, kQualityIds);
        read(o, "bitrateMbps", d.bitrateMbps);
    }
    // Clamp everything that could crash or hang a render.
    s.turntable.fps = std::max(1, std::min(s.turntable.fps, 120));
    s.turntable.seconds = clampf(s.turntable.seconds, 0.1f, 600.0f);
    s.turntable.rotations = std::max(1, std::min(s.turntable.rotations, 16));
    s.output.width = std::max(16, std::min(s.output.width, 8192));
    s.output.height = std::max(16, std::min(s.output.height, 8192));
    s.camera.fill = clampf(s.camera.fill, 0.05f, 1.0f);
    s.explode.distance = clampf(s.explode.distance, 0.0f, 10.0f);
    s.explode.stagger = clampf(s.explode.stagger, 0.0f, 1.0f);
    s.explode.start = clampf(s.explode.start, 0.0f, 1.0f);
    s.explode.end = clampf(s.explode.end, s.explode.start, 1.0f);
    s.explode.manual = clampf(s.explode.manual, 0.0f, 1.0f);
    s.camera.focalLength = clampf(s.camera.focalLength, 8.0f, 600.0f);
    s.camera.fStop = clampf(s.camera.fStop, 0.7f, 64.0f);
    s.model.creaseAngle = clampf(s.model.creaseAngle, 0.0f, 180.0f);
    s.material.layerHeight = std::max(0.01f, s.material.layerHeight);
    s.material.patternScale = std::max(0.01f, s.material.patternScale);
    s.material.noiseScale = std::max(0.001f, s.material.noiseScale);
    return true;
}

bool readTextFile(const std::string& path, std::string& out) {
#ifdef _WIN32
    FILE* f = _wfopen(widen(path).c_str(), L"rb");
#else
    FILE* f = fopen(path.c_str(), "rb");
#endif
    if (!f) return false;
    out.clear();
    char buf[65536];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) out.append(buf, n);
    fclose(f);
    return true;
}

bool writeTextFile(const std::string& path, const std::string& text) {
#ifdef _WIN32
    FILE* f = _wfopen(widen(path).c_str(), L"wb");
#else
    FILE* f = fopen(path.c_str(), "wb");
#endif
    if (!f) return false;
    bool ok = fwrite(text.data(), 1, text.size(), f) == text.size();
    return fclose(f) == 0 && ok;
}

bool saveSceneFile(const std::string& path, const Scene& s, std::string& error) {
    if (!writeTextFile(path, sceneToJson(s))) {
        error = "Could not write " + path;
        return false;
    }
    return true;
}

bool loadSceneFile(const std::string& path, Scene& s, std::string& error) {
    std::string text;
    if (!readTextFile(path, text)) {
        error = "Could not read " + path;
        return false;
    }
    return sceneFromJson(text, s, error);
}

}  // namespace spindle
