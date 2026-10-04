// Shared helpers: small vector/matrix math, logging and string conversion.
// Everything in this header is platform-neutral so the loader, mesh processing
// and scene code can be unit-tested on any OS.
#pragma once

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <string>
#include <algorithm>

namespace spindle {

constexpr float kPi = 3.14159265358979323846f;
inline float radians(float deg) { return deg * kPi / 180.0f; }
inline float degrees(float rad) { return rad * 180.0f / kPi; }
inline float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

struct vec3 {
    float x = 0, y = 0, z = 0;
    vec3() = default;
    constexpr vec3(float x_, float y_, float z_) : x(x_), y(y_), z(z_) {}
    vec3 operator+(const vec3& o) const { return {x + o.x, y + o.y, z + o.z}; }
    vec3 operator-(const vec3& o) const { return {x - o.x, y - o.y, z - o.z}; }
    vec3 operator*(float s) const { return {x * s, y * s, z * s}; }
    vec3 operator/(float s) const { return {x / s, y / s, z / s}; }
    vec3 operator-() const { return {-x, -y, -z}; }
    vec3& operator+=(const vec3& o) { x += o.x; y += o.y; z += o.z; return *this; }
    vec3& operator-=(const vec3& o) { x -= o.x; y -= o.y; z -= o.z; return *this; }
    vec3& operator*=(float s) { x *= s; y *= s; z *= s; return *this; }
    float operator[](int i) const { return i == 0 ? x : (i == 1 ? y : z); }
};
inline float dot(const vec3& a, const vec3& b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline vec3 cross(const vec3& a, const vec3& b) {
    return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}
inline float length(const vec3& v) { return std::sqrt(dot(v, v)); }
inline vec3 normalize(const vec3& v) {
    float l = length(v);
    return l > 0 ? v / l : vec3(0, 0, 1);
}
inline vec3 vmin(const vec3& a, const vec3& b) { return {std::min(a.x, b.x), std::min(a.y, b.y), std::min(a.z, b.z)}; }
inline vec3 vmax(const vec3& a, const vec3& b) { return {std::max(a.x, b.x), std::max(a.y, b.y), std::max(a.z, b.z)}; }

// Row-major storage (m[row][col]), transforming column vectors: p' = M * p.
// HLSL declares the matching cbuffer members `row_major` and uses mul(M, v).
struct mat4 {
    float m[4][4] = {{1, 0, 0, 0}, {0, 1, 0, 0}, {0, 0, 1, 0}, {0, 0, 0, 1}};

    static mat4 identity() { return mat4(); }

    mat4 operator*(const mat4& b) const {
        mat4 r;
        for (int i = 0; i < 4; ++i)
            for (int j = 0; j < 4; ++j) {
                float s = 0;
                for (int k = 0; k < 4; ++k) s += m[i][k] * b.m[k][j];
                r.m[i][j] = s;
            }
        return r;
    }
    vec3 transformPoint(const vec3& p) const {
        float x = m[0][0] * p.x + m[0][1] * p.y + m[0][2] * p.z + m[0][3];
        float y = m[1][0] * p.x + m[1][1] * p.y + m[1][2] * p.z + m[1][3];
        float z = m[2][0] * p.x + m[2][1] * p.y + m[2][2] * p.z + m[2][3];
        float w = m[3][0] * p.x + m[3][1] * p.y + m[3][2] * p.z + m[3][3];
        return w != 0 && w != 1 ? vec3(x / w, y / w, z / w) : vec3(x, y, z);
    }
    vec3 transformDir(const vec3& d) const {
        return {m[0][0] * d.x + m[0][1] * d.y + m[0][2] * d.z,
                m[1][0] * d.x + m[1][1] * d.y + m[1][2] * d.z,
                m[2][0] * d.x + m[2][1] * d.y + m[2][2] * d.z};
    }

    static mat4 translation(const vec3& t) {
        mat4 r;
        r.m[0][3] = t.x; r.m[1][3] = t.y; r.m[2][3] = t.z;
        return r;
    }
    static mat4 scale(const vec3& s) {
        mat4 r;
        r.m[0][0] = s.x; r.m[1][1] = s.y; r.m[2][2] = s.z;
        return r;
    }
    static mat4 rotationZ(float a) {
        mat4 r;
        float c = std::cos(a), s = std::sin(a);
        r.m[0][0] = c; r.m[0][1] = -s;
        r.m[1][0] = s; r.m[1][1] = c;
        return r;
    }
    // Right-handed look-at view matrix (camera looks down -Z in view space).
    static mat4 lookAt(const vec3& eye, const vec3& target, const vec3& upHint) {
        vec3 f = normalize(target - eye);
        vec3 up = std::fabs(dot(f, normalize(upHint))) > 0.999f ? vec3(0, 1, 0) : upHint;
        vec3 s = normalize(cross(f, up));
        vec3 u = cross(s, f);
        mat4 r;
        r.m[0][0] = s.x;  r.m[0][1] = s.y;  r.m[0][2] = s.z;  r.m[0][3] = -dot(s, eye);
        r.m[1][0] = u.x;  r.m[1][1] = u.y;  r.m[1][2] = u.z;  r.m[1][3] = -dot(u, eye);
        r.m[2][0] = -f.x; r.m[2][1] = -f.y; r.m[2][2] = -f.z; r.m[2][3] = dot(f, eye);
        return r;
    }
    // Right-handed perspective, depth mapped to [0, 1].
    static mat4 perspective(float fovY, float aspect, float zn, float zf) {
        float f = 1.0f / std::tan(fovY * 0.5f);
        mat4 r;
        r.m[0][0] = f / aspect;
        r.m[1][1] = f;
        r.m[2][2] = zf / (zn - zf);
        r.m[2][3] = zn * zf / (zn - zf);
        r.m[3][2] = -1;
        r.m[3][3] = 0;
        return r;
    }
    // Right-handed orthographic, depth mapped to [0, 1].
    static mat4 orthographic(float halfW, float halfH, float zn, float zf) {
        mat4 r;
        r.m[0][0] = 1.0f / halfW;
        r.m[1][1] = 1.0f / halfH;
        r.m[2][2] = 1.0f / (zn - zf);
        r.m[2][3] = zn / (zn - zf);
        return r;
    }
    mat4 inverse() const;
    mat4 transposed() const {
        mat4 r;
        for (int i = 0; i < 4; ++i)
            for (int j = 0; j < 4; ++j) r.m[i][j] = m[j][i];
        return r;
    }
};

// ---- logging ---------------------------------------------------------------
void logInit(const std::string& path);
void logf(const char* fmt, ...)
#if defined(__GNUC__)
    __attribute__((format(printf, 1, 2)))
#endif
    ;

// ---- strings ---------------------------------------------------------------
std::string toLower(std::string s);
std::string fileNameOf(const std::string& path);
std::string extensionOf(const std::string& path);  // lower-case, includes the dot

#ifdef _WIN32
std::wstring widen(const std::string& utf8);
std::string narrow(const std::wstring& wide);
#endif

}  // namespace spindle
