// Environment maps: procedural studio/outdoor HDR environments, .hdr loading,
// and the CPU-side lighting analysis (SH irradiance, dominant light direction).
// Platform-neutral.
#pragma once

#include "common.h"

#include <string>
#include <vector>

namespace spindle {

// Equirectangular, Z-up. Direction d maps to
//   u = 0.5 - atan2(d.y, d.x) / 2pi,  v = acos(d.z) / pi
// (u increases to the right when looking outwards, so text in HDRIs is not mirrored).
struct EnvironmentImage {
    int width = 0, height = 0;
    std::vector<float> rgba;  // linear radiance, 4 floats per pixel (alpha unused)

    // Diffuse lighting: 9 SH coefficients already convolved with the clamped
    // cosine lobe and divided by pi, so radiance = albedo * sum(c_i * Y_i(n)).
    vec3 sh[9];
    vec3 dominantDirection = {0.5f, 0.5f, 0.7f};  // unit, towards the light
    float directionality = 0.5f;                   // 0 = uniform, 1 = a single point light
    vec3 dominantColor = {1, 1, 1};               // average radiance near the dominant direction
};

bool makeProceduralEnvironment(const std::string& name, EnvironmentImage& out, int width = 1024);
bool loadEnvironmentFile(const std::string& path, EnvironmentImage& out, std::string& error);

// Fills sh/dominant* from the pixels. Called by both functions above.
void analyzeEnvironment(EnvironmentImage& env);

vec3 equirectDirection(float u, float v);

// 8-bit RGBA image (for user textures).
struct Image8 {
    int width = 0, height = 0;
    std::vector<uint8_t> rgba;
};
bool loadImage8(const std::string& path, Image8& out, std::string& error);

}  // namespace spindle
