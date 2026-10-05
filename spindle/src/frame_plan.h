// Everything a renderer needs for one accumulation sample, computed on the CPU:
// shader constants (laid out to match FrameCB / MaterialCB in the shaders),
// the key light and shadow frustum, and per-part explode offsets.
// Platform-neutral: shared by the Direct3D 11 and OpenGL ES renderers.
#pragma once

#include "camera.h"
#include "mesh.h"
#include "sampling.h"
#include "scene.h"

#include <vector>

namespace spindle {

// Mirrors cbuffer FrameCB (HLSL) / uniform block FrameCB (GLSL), member for member.
struct FrameConstants {
    mat4 viewProj, view, proj, projInv, invViewProj, lightViewProj, world, mirror;
    float eye[4], forward[4], screen[4];
    float keyDir[4], keyColor[4], fillDir[4], fillColor[4], rimDir[4], rimColor[4];
    float env[4];
    float sh[9][4];
    float background[4], bgColor[4], bgColor2[4];
    float ground[4], floorColor[4];
    float ao[4], shadow[4], tone[4], model[4], pass[4];
};

// Mirrors MaterialCB.
struct MaterialConstants {
    float base[4], pbr[4], trans[4], transTint[4], layer[4], noise[4], pattern[4], patternColor[4], gradient[4],
        gradientRange[4], wire[4], fileFlags[4];
};

// What the renderer knows about the loaded model and environment.
struct RenderContext {
    bool hasMesh = false;
    bool hasUserTexture = false;
    vec3 sphereCenter = {0, 0, 50};
    float sphereRadius = 50;
    float minZ = 0, maxZ = 100;
    std::vector<ExplodePart> parts;
    vec3 sh[9];
    vec3 envDominantDir = {0.5f, 0.5f, 0.7f};
    vec3 envDominantColor = {1, 1, 1};
    float envDirectionality = 0.5f;
    int specMips = 6, envMips = 7;
    int shadowMapSize = 4096;
};

struct FramePlan {
    FrameConstants frame;
    MaterialConstants material;
    std::vector<vec3> partOffsets;  // empty = assembled
    float sceneRadius = 1;          // radius used for shadows and the floor this sample
    bool aoEnabled = false;
    bool shadowEnabled = false;
    bool groundEnabled = false;
    bool reflection = false;
};

// Radius to frame the camera on (covers the fully exploded assembly when the scene explodes).
float framingRadiusFor(const RenderContext& ctx, const Scene& scene);

FramePlan planFrame(const RenderContext& ctx, const Scene& scene, const SampleInput& in, int width, int height);

void set4(float* d, float x, float y, float z, float w);
void set4(float* d, const vec3& v, float w);
vec3 srgbToLinear(const vec3& c);

}  // namespace spindle
