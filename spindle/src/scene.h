// Scene description shared by the GUI, the CLI and the preset files.
#pragma once

#include "common.h"
#include "mesh.h"

#include <string>
#include <vector>

namespace spindle {

enum class Pattern { None = 0, Image, Wood, Marble, Carbon, Checker, Granite, Count };
enum class Background { Environment = 0, Solid, Gradient, Radial, Transparent, Count };
enum class Ground { None = 0, ShadowCatcher, Floor, Reflective, Count };
enum class Tonemap { ACES = 0, AgX, Linear, Count };
enum class TurntableMode { RotateObject = 0, OrbitCamera, Count };
enum class Easing { Linear = 0, EaseInOut, HoldThenSpin, Count };
enum class Format { MP4 = 0, PNG, GIF, WebM, ProRes, HEVC, Count };
enum class Quality { Draft = 0, Standard, High, Ultra, Count };

const char* patternName(Pattern p);
const char* backgroundName(Background b);
const char* groundName(Ground g);
const char* tonemapName(Tonemap t);
const char* turntableModeName(TurntableMode m);
const char* easingName(Easing e);
const char* formatName(Format f);
const char* formatExtension(Format f);
bool formatNeedsFfmpeg(Format f);
bool formatSupportsAlpha(Format f);
const char* qualityName(Quality q);
int qualitySamples(Quality q);

struct ModelSettings {
    UpAxis up = UpAxis::Z;
    int quarterTurns[3] = {0, 0, 0};
    float creaseAngle = 30.0f;
};

struct MaterialSettings {
    std::string preset = "Matte PLA";
    vec3 baseColor = {0.85f, 0.32f, 0.12f};
    float roughness = 0.65f;
    float metalness = 0.0f;
    float clearcoat = 0.0f;
    float clearcoatRoughness = 0.08f;
    float sheen = 0.0f;
    float transmission = 0.0f;  // approximate: environment refraction only
    float ior = 1.5f;
    vec3 transmissionTint = {1, 1, 1};

    bool layerLines = true;
    float layerHeight = 0.2f;  // mm
    float layerDepth = 0.35f;  // 0..1 relative bump strength

    float noiseStrength = 0.0f;
    float noiseScale = 2.0f;    // mm
    float noiseStretch = 1.0f;  // >1 stretches the noise along X (brushed metal)

    Pattern pattern = Pattern::None;
    vec3 patternColor = {0.25f, 0.13f, 0.05f};
    float patternScale = 20.0f;  // mm per repeat
    float patternStrength = 1.0f;
    float triplanarSharpness = 4.0f;
    std::string texturePath;     // used when pattern == Image

    bool gradient = false;
    vec3 gradientColor = {0.1f, 0.3f, 0.9f};

    bool wireframe = false;
    vec3 wireColor = {0.05f, 0.05f, 0.05f};
    float wireWidth = 1.0f;  // pixels
};

struct LightRig {
    bool enabled = false;
    float intensity = 3.0f;
    float keyAzimuth = 45.0f;    // degrees, measured from +X towards +Y
    float keyElevation = 45.0f;  // degrees above the horizon
    vec3 keyColor = {1.0f, 0.97f, 0.92f};
    float fill = 0.35f;  // relative to key
    float rim = 0.6f;    // relative to key
};

struct EnvironmentSettings {
    std::string source = "Studio Softbox";  // a procedural environment name or a .hdr/.exr path
    float rotation = 0.0f;                  // degrees
    float intensity = 1.0f;
    float saturation = 1.0f;

    Background background = Background::Solid;
    float backgroundBlur = 0.4f;  // 0..1, environment background only
    vec3 backgroundColor = {0.93f, 0.93f, 0.94f};
    vec3 backgroundColor2 = {0.62f, 0.64f, 0.68f};

    Ground ground = Ground::ShadowCatcher;
    vec3 floorColor = {0.85f, 0.85f, 0.86f};
    float floorRoughness = 0.6f;
    float reflection = 0.35f;
    float shadowStrength = 0.75f;
    float shadowSoftness = 6.0f;  // degrees of light cone (offline soft shadows)
    float aoStrength = 0.8f;
    float aoRadius = 0.12f;  // fraction of the model's bounding radius

    LightRig lights;

    Tonemap tonemap = Tonemap::ACES;
    float exposure = 0.0f;  // EV
};

struct CameraSettings {
    float focalLength = 50.0f;  // mm, 35 mm-equivalent on the short side
    bool orthographic = false;
    float elevation = 20.0f;    // degrees
    float fill = 0.8f;          // fraction of the short frame side covered by the bounding sphere
    bool depthOfField = false;
    float fStop = 2.8f;
};

struct TurntableSettings {
    TurntableMode mode = TurntableMode::RotateObject;
    float seconds = 8.0f;
    int fps = 30;
    int rotations = 1;
    bool clockwise = false;
    Easing easing = Easing::Linear;
    float holdSeconds = 1.0f;
    bool startFromViewport = true;
    float startAngle = 0.0f;  // degrees; used when !startFromViewport
    float bob = 0.0f;         // degrees of vertical camera bob over the loop
    float shutter = 0.0f;     // 0..1 of a frame interval; > 0 enables motion blur

    int frameCount() const { return std::max(1, (int)std::lround(seconds * fps)); }
};

struct OutputSettings {
    Format format = Format::MP4;
    int width = 1920;
    int height = 1080;
    Quality quality = Quality::Standard;
    float bitrateMbps = 0.0f;  // 0 = automatic
};

struct Scene {
    ModelSettings model;
    MaterialSettings material;
    EnvironmentSettings environment;
    CameraSettings camera;
    TurntableSettings turntable;
    OutputSettings output;
};

// Material presets ---------------------------------------------------------
const std::vector<std::string>& materialPresetNames();
bool applyMaterialPreset(const std::string& name, MaterialSettings& m);  // false if unknown

// Procedural environments -----------------------------------------------------
const std::vector<std::string>& proceduralEnvironmentNames();
bool isProceduralEnvironment(const std::string& source);

// Resolution presets -------------------------------------------------------
struct ResolutionPreset {
    const char* name;
    int width, height;
};
const std::vector<ResolutionPreset>& resolutionPresets();

// JSON ------------------------------------------------------------------------
std::string sceneToJson(const Scene& s);
// Unknown keys are ignored and missing keys keep the value already in `s`.
bool sceneFromJson(const std::string& text, Scene& s, std::string& error);
bool saveSceneFile(const std::string& path, const Scene& s, std::string& error);
bool loadSceneFile(const std::string& path, Scene& s, std::string& error);

bool readTextFile(const std::string& path, std::string& out);
bool writeTextFile(const std::string& path, const std::string& text);

}  // namespace spindle
