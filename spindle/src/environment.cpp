#include "environment.h"

#include "mesh.h"
#include "scene.h"

#include <cstring>
#include <functional>

#define STB_IMAGE_IMPLEMENTATION
#define STBI_NO_PSD
#define STBI_NO_PIC
#define STBI_NO_PNM
#define STBI_NO_GIF
#ifdef _WIN32
#define STBI_WINDOWS_UTF8
#endif
#include "../third_party/stb/stb_image.h"

namespace spindle {

vec3 equirectDirection(float u, float v) {
    float phi = (0.5f - u) * 2.0f * kPi;
    float theta = v * kPi;
    float s = std::sin(theta);
    return {s * std::cos(phi), s * std::sin(phi), std::cos(theta)};
}

static float smoothstep(float a, float b, float x) {
    float t = clampf((x - a) / (b - a), 0.0f, 1.0f);
    return t * t * (3 - 2 * t);
}
static vec3 mix(const vec3& a, const vec3& b, float t) { return a * (1 - t) + b * t; }
static vec3 dirFromAzEl(float azDeg, float elDeg) {
    float az = radians(azDeg), el = radians(elDeg);
    return {std::cos(el) * std::cos(az), std::cos(el) * std::sin(az), std::sin(el)};
}

// A rectangular area light, `halfW` x `halfH` degrees, facing the origin.
static float softbox(const vec3& d, float azDeg, float elDeg, float halfW, float halfH, float edge = 0.08f) {
    vec3 f = dirFromAzEl(azDeg, elDeg);
    float fd = dot(d, f);
    if (fd <= 0.01f) return 0;
    vec3 r = normalize(cross(f, vec3(0, 0, 1)));
    if (length(cross(f, vec3(0, 0, 1))) < 1e-4f) r = vec3(1, 0, 0);
    vec3 u = cross(r, f);
    float x = std::fabs(dot(d, r) / fd) / std::tan(radians(halfW));
    float y = std::fabs(dot(d, u) / fd) / std::tan(radians(halfH));
    return (1 - smoothstep(1 - edge, 1 + edge, x)) * (1 - smoothstep(1 - edge, 1 + edge, y));
}

// A round light (sun) with a soft halo.
static float disc(const vec3& d, float azDeg, float elDeg, float radiusDeg) {
    float c = dot(d, dirFromAzEl(azDeg, elDeg));
    float ang = std::acos(clampf(c, -1, 1));
    return 1 - smoothstep(radians(radiusDeg) * 0.85f, radians(radiusDeg) * 1.15f, ang);
}
static float halo(const vec3& d, float azDeg, float elDeg, float power) {
    float c = std::max(0.0f, dot(d, dirFromAzEl(azDeg, elDeg)));
    return std::pow(c, power);
}

static float hash2(int x, int y) {
    uint32_t h = (uint32_t)x * 374761393u + (uint32_t)y * 668265263u;
    h = (h ^ (h >> 13)) * 1274126177u;
    return float((h ^ (h >> 16)) & 0xFFFFFF) / float(0xFFFFFF);
}

using EnvFn = std::function<vec3(const vec3&)>;

static EnvFn proceduralFunction(const std::string& lname) {
    if (lname == "studio softbox")
        return [](const vec3& d) {
            float h = d.z;
            vec3 c = h > 0 ? mix(vec3(0.050f, 0.050f, 0.055f), vec3(0.025f, 0.025f, 0.03f), h)
                           : vec3(0.035f, 0.034f, 0.033f) * (1 + h * 0.5f);
            c += vec3(1.0f, 0.97f, 0.92f) * (14.0f * softbox(d, 45, 35, 22, 16));   // key
            c += vec3(0.92f, 0.95f, 1.0f) * (3.5f * softbox(d, -65, 15, 28, 22));   // fill
            c += vec3(1.0f, 1.0f, 1.0f) * (6.0f * softbox(d, 180, 72, 40, 7));     // overhead strip
            c += vec3(1.0f, 1.0f, 1.0f) * (9.0f * softbox(d, 160, 20, 5, 30));     // rim strip
            return c;
        };
    if (lname == "studio high-key")
        return [](const vec3& d) {
            float h = d.z;
            vec3 c = h > 0 ? mix(vec3(0.85f, 0.85f, 0.86f), vec3(1.0f, 1.0f, 1.0f), h)
                           : vec3(0.70f, 0.70f, 0.70f) * (1 + h * 0.3f);
            c += vec3(1, 1, 1) * (5.0f * softbox(d, 40, 40, 35, 25));
            c += vec3(1, 1, 1) * (2.5f * softbox(d, -120, 30, 35, 25));
            return c;
        };
    if (lname == "studio dark")
        return [](const vec3& d) {
            vec3 c = vec3(0.006f, 0.006f, 0.007f);
            c += vec3(1.0f, 0.98f, 0.95f) * (18.0f * softbox(d, 120, 10, 4, 35));
            c += vec3(0.95f, 0.98f, 1.0f) * (18.0f * softbox(d, -120, 10, 4, 35));
            c += vec3(1, 1, 1) * (4.0f * softbox(d, 0, 60, 18, 10));
            return c;
        };
    if (lname == "overcast")
        return [](const vec3& d) {
            float h = d.z;
            if (h >= 0) return mix(vec3(0.80f, 0.82f, 0.85f), vec3(1.15f, 1.17f, 1.2f), std::pow(h, 0.6f));
            return mix(vec3(0.55f, 0.56f, 0.57f), vec3(0.22f, 0.21f, 0.19f), smoothstep(0.0f, 0.15f, -h));
        };
    if (lname == "daylight")
        return [](const vec3& d) {
            float h = d.z;
            vec3 c;
            if (h >= 0)
                c = mix(vec3(0.75f, 0.85f, 1.0f) * 1.3f, vec3(0.22f, 0.42f, 0.95f), std::pow(h, 0.5f));
            else
                c = mix(vec3(0.6f, 0.62f, 0.62f), vec3(0.16f, 0.17f, 0.13f), smoothstep(0.0f, 0.1f, -h));
            c += vec3(1.0f, 0.95f, 0.85f) * (600.0f * disc(d, 120, 50, 2.5f));
            c += vec3(1.0f, 0.95f, 0.85f) * (1.5f * halo(d, 120, 50, 64));
            return c;
        };
    if (lname == "sunset")
        return [](const vec3& d) {
            float h = d.z;
            vec3 c;
            if (h >= 0) {
                c = mix(vec3(1.5f, 0.62f, 0.25f), vec3(0.12f, 0.2f, 0.5f), std::pow(h, 0.45f));
                c += vec3(1.2f, 0.45f, 0.15f) * (1.8f * halo(d, -30, 5, 8));
            } else {
                c = mix(vec3(0.35f, 0.18f, 0.1f), vec3(0.04f, 0.035f, 0.03f), smoothstep(0.0f, 0.08f, -h));
            }
            c += vec3(1.0f, 0.55f, 0.25f) * (220.0f * disc(d, -30, 5, 3.0f));
            return c;
        };
    if (lname == "warm interior")
        return [](const vec3& d) {
            float h = d.z;
            vec3 c;
            if (h > 0.55f) c = vec3(0.35f, 0.32f, 0.28f);            // ceiling
            else if (h > -0.35f) c = vec3(0.42f, 0.30f, 0.21f) * 0.6f;  // walls
            else c = vec3(0.30f, 0.19f, 0.11f) * 0.45f;              // wooden floor
            c += vec3(0.9f, 0.95f, 1.05f) * (9.0f * softbox(d, 90, 22, 30, 24, 0.03f));  // window
            c += vec3(1.0f, 0.75f, 0.45f) * (25.0f * disc(d, -100, 40, 4.0f));          // lamp
            c += vec3(1.0f, 0.7f, 0.4f) * (0.8f * halo(d, -100, 40, 6));
            return c;
        };
    if (lname == "night city")
        return [](const vec3& d) {
            float h = d.z;
            vec3 c = h >= 0 ? mix(vec3(0.12f, 0.06f, 0.05f), vec3(0.01f, 0.015f, 0.04f), std::pow(h, 0.35f))
                            : vec3(0.015f, 0.015f, 0.02f);
            // Lit windows in a band around the horizon.
            if (h > -0.05f && h < 0.25f) {
                float u = 0.5f - std::atan2(d.y, d.x) / (2 * kPi);
                int gx = (int)(u * 420), gy = (int)((h + 0.05f) * 160);
                float r = hash2(gx, gy);
                if (r > 0.82f) {
                    float t = hash2(gy, gx);
                    vec3 col = t < 0.6f ? vec3(1.0f, 0.75f, 0.4f) : (t < 0.85f ? vec3(0.6f, 0.8f, 1.0f) : vec3(1, 1, 1));
                    c += col * (2.0f + 4.0f * hash2(gx * 7, gy * 3));
                }
            }
            c += vec3(1.0f, 0.2f, 0.6f) * (8.0f * softbox(d, 200, 12, 25, 1.5f));  // neon
            c += vec3(0.2f, 0.9f, 1.0f) * (6.0f * softbox(d, 25, 6, 18, 1.2f));
            return c;
        };
    return nullptr;
}

bool makeProceduralEnvironment(const std::string& name, EnvironmentImage& out, int width) {
    EnvFn fn = proceduralFunction(toLower(name));
    if (!fn) return false;
    out.width = width;
    out.height = width / 2;
    out.rgba.assign((size_t)out.width * out.height * 4, 1.0f);
    const int W = out.width, H = out.height;
    parallelFor((size_t)H, [&](size_t y0, size_t y1) {
        for (size_t y = y0; y < y1; ++y)
            for (int x = 0; x < W; ++x) {
                // 2x2 supersampling keeps the softbox edges from aliasing.
                vec3 c(0, 0, 0);
                for (int s = 0; s < 4; ++s) {
                    float u = (x + 0.25f + 0.5f * (s & 1)) / W;
                    float v = (y + 0.25f + 0.5f * (s >> 1)) / H;
                    c += fn(equirectDirection(u, v));
                }
                c *= 0.25f;
                float* p = &out.rgba[((size_t)y * W + x) * 4];
                p[0] = c.x;
                p[1] = c.y;
                p[2] = c.z;
                p[3] = 1;
            }
    }, 8);
    analyzeEnvironment(out);
    return true;
}

static void halveEquirect(EnvironmentImage& e) {
    int W = e.width / 2, H = e.height / 2;
    std::vector<float> o((size_t)W * H * 4);
    for (int y = 0; y < H; ++y)
        for (int x = 0; x < W; ++x)
            for (int c = 0; c < 4; ++c) {
                auto at = [&](int xx, int yy) { return e.rgba[((size_t)yy * e.width + xx) * 4 + c]; };
                o[((size_t)y * W + x) * 4 + c] =
                    0.25f * (at(2 * x, 2 * y) + at(2 * x + 1, 2 * y) + at(2 * x, 2 * y + 1) + at(2 * x + 1, 2 * y + 1));
            }
    e.rgba.swap(o);
    e.width = W;
    e.height = H;
}

bool loadEnvironmentFile(const std::string& path, EnvironmentImage& out, std::string& error) {
    std::string ext = extensionOf(path);
    if (ext == ".exr") {
        error = "OpenEXR is not supported in this build. Convert the file to Radiance .hdr.";
        return false;
    }
    int w, h, comp;
    float* data = stbi_loadf(path.c_str(), &w, &h, &comp, 4);
    if (!data) {
        error = std::string("Could not read the image: ") + stbi_failure_reason();
        return false;
    }
    out.width = w;
    out.height = h;
    out.rgba.assign(data, data + (size_t)w * h * 4);
    stbi_image_free(data);
    for (size_t i = 0; i < out.rgba.size(); ++i) {
        float& v = out.rgba[i];
        if (!std::isfinite(v) || v < 0) v = 0;
        if ((i & 3) == 3) v = 1;
    }
    // Large HDRIs are reduced to 4K wide; the cubemap is 512 per face anyway.
    while (out.width > 4096 && out.height > 1) halveEquirect(out);
    analyzeEnvironment(out);
    return true;
}

void analyzeEnvironment(EnvironmentImage& env) {
    // Project onto 9 SH coefficients, sampling a reduced grid with solid-angle weights.
    const int W = std::min(env.width, 256), H = std::max(1, std::min(env.height, 128));
    double acc[9][3] = {};
    double lumWeighted[3] = {};
    double total = 0;
    for (int y = 0; y < H; ++y) {
        float v = (y + 0.5f) / H;
        float dOmega = (2 * kPi / W) * (kPi / H) * std::sin(v * kPi);
        int sy = std::min(env.height - 1, (int)(v * env.height));
        for (int x = 0; x < W; ++x) {
            float u = (x + 0.5f) / W;
            int sx = std::min(env.width - 1, (int)(u * env.width));
            const float* p = &env.rgba[((size_t)sy * env.width + sx) * 4];
            vec3 c = {p[0], p[1], p[2]};
            vec3 d = equirectDirection(u, v);
            float Y[9] = {0.282095f,
                          0.488603f * d.y,
                          0.488603f * d.z,
                          0.488603f * d.x,
                          1.092548f * d.x * d.y,
                          1.092548f * d.y * d.z,
                          0.315392f * (3 * d.z * d.z - 1),
                          1.092548f * d.x * d.z,
                          0.546274f * (d.x * d.x - d.y * d.y)};
            for (int i = 0; i < 9; ++i) {
                acc[i][0] += c.x * Y[i] * dOmega;
                acc[i][1] += c.y * Y[i] * dOmega;
                acc[i][2] += c.z * Y[i] * dOmega;
            }
            float lum = 0.2126f * c.x + 0.7152f * c.y + 0.0722f * c.z;
            lumWeighted[0] += lum * d.x * dOmega;
            lumWeighted[1] += lum * d.y * dOmega;
            lumWeighted[2] += lum * d.z * dOmega;
            total += lum * dOmega;
        }
    }
    // Convolve with the clamped cosine (A0 = pi, A1 = 2pi/3, A2 = pi/4) and divide by pi.
    const float band[9] = {1.0f, 2.0f / 3, 2.0f / 3, 2.0f / 3, 0.25f, 0.25f, 0.25f, 0.25f, 0.25f};
    for (int i = 0; i < 9; ++i)
        env.sh[i] = vec3((float)acc[i][0], (float)acc[i][1], (float)acc[i][2]) * band[i];

    vec3 dir((float)lumWeighted[0], (float)lumWeighted[1], (float)lumWeighted[2]);
    float mag = length(dir);
    // For a single point light |sum(L d)| / sum(L) = 1; for a uniform sphere it is 0.
    env.directionality = total > 0 ? clampf(mag / (float)total, 0.0f, 1.0f) : 0.0f;
    vec3 n = mag > 1e-8f ? dir / mag : vec3(0.4f, 0.3f, 0.86f);
    // Shadows from a light at or below the horizon would stretch to infinity.
    float el = clampf(std::asin(clampf(n.z, -1, 1)), radians(15.0f), radians(80.0f));
    float az = std::atan2(n.y, n.x);
    if (mag <= 1e-8f) az = radians(40.0f);
    env.dominantDirection = {std::cos(el) * std::cos(az), std::cos(el) * std::sin(az), std::sin(el)};

    // Average radiance in a 25-degree cone around that direction, for the key light colour.
    vec3 sum(0, 0, 0);
    float wsum = 0;
    float cosCone = std::cos(radians(25.0f));
    for (int y = 0; y < H; ++y)
        for (int x = 0; x < W; ++x) {
            float u = (x + 0.5f) / W, v = (y + 0.5f) / H;
            vec3 d = equirectDirection(u, v);
            if (dot(d, n) < cosCone) continue;
            int sx = std::min(env.width - 1, (int)(u * env.width)), sy = std::min(env.height - 1, (int)(v * env.height));
            const float* p = &env.rgba[((size_t)sy * env.width + sx) * 4];
            float w = std::sin(v * kPi);
            sum += vec3(p[0], p[1], p[2]) * w;
            wsum += w;
        }
    vec3 col = wsum > 0 ? sum / wsum : vec3(1, 1, 1);
    float m = std::max(col.x, std::max(col.y, col.z));
    env.dominantColor = m > 0 ? col / m : vec3(1, 1, 1);
}

bool loadImage8(const std::string& path, Image8& out, std::string& error) {
    int w, h, comp;
    unsigned char* data = stbi_load(path.c_str(), &w, &h, &comp, 4);
    if (!data) {
        error = std::string("Could not read the image: ") + stbi_failure_reason();
        return false;
    }
    out.width = w;
    out.height = h;
    out.rgba.assign(data, data + (size_t)w * h * 4);
    stbi_image_free(data);
    return true;
}

}  // namespace spindle
