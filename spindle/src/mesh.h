// STL loading and mesh processing (welding, crease-aware normals, orientation).
// Platform-neutral: no Windows or GPU dependencies, so it is unit-tested natively.
#pragma once

#include "common.h"

#include <atomic>
#include <cstddef>
#include <functional>
#include <string>
#include <vector>

namespace spindle {

// Appearance read from the file for one triangle (3MF colours/materials).
struct FaceStyle {
    uint32_t rgba = 0;  // sRGB8, R in the low byte; alpha 0 = "no colour, use Spindle's material"
    uint8_t metalness = 0, roughness = 0;  // 0..255, valid when hasPbr
    bool hasPbr = false;
    bool operator==(const FaceStyle& o) const {
        return rgba == o.rgba && metalness == o.metalness && roughness == o.roughness && hasPbr == o.hasPbr;
    }
};

// Raw triangles exactly as read from the file (after dropping invalid ones).
struct TriangleSoup {
    std::vector<vec3> positions;  // 3 per triangle
    // Optional per-triangle data, either empty or one entry per triangle.
    std::vector<uint32_t> triStyle;  // index into styles (styles[0] is "no colour")
    std::vector<FaceStyle> styles;
    std::vector<uint32_t> triPart;  // index into partNames (assembly parts from the file)
    std::vector<std::string> partNames;
    size_t triangleCount() const { return positions.size() / 3; }
};

struct LoadResult {
    bool ok = false;
    std::string error;                  // set when !ok
    std::vector<std::string> warnings;  // non-fatal problems to show the user
    bool binary = false;
    size_t droppedNonFinite = 0;
    size_t droppedDegenerate = 0;
    TriangleSoup soup;
};

// Optional progress (0..1) and cancellation hooks for the loader.
struct Progress {
    std::atomic<float> value{0.0f};
    std::atomic<bool> cancel{false};
};

LoadResult loadStlFile(const std::string& path, Progress* progress = nullptr);
LoadResult parseStl(const uint8_t* data, size_t size, Progress* progress = nullptr);

// 3MF (model3mf.cpp): every printable build item, with its transforms, in mm.
LoadResult load3mfFile(const std::string& path, Progress* progress = nullptr);
LoadResult parse3mf(const uint8_t* data, size_t size, Progress* progress = nullptr);

// Picks STL or 3MF by content (a ZIP signature means 3MF), not by extension.
LoadResult loadModelFile(const std::string& path, Progress* progress = nullptr);
bool isSupportedModelExtension(const std::string& path);  // .stl / .3mf

bool readWholeFile(const std::string& path, std::vector<uint8_t>& out, std::string& error);
// Shared tail of every loader: drops NaN/zero-area triangles, adds warnings, sets ok.
void finishLoad(LoadResult& r, Progress* progress);

// GPU vertex. `color` is sRGB8 (alpha 0 = no file colour). `extra` packs
// part index (bits 0-11), has-PBR flag (bit 12), metalness (16-23), roughness (24-31).
struct Vertex {
    float px, py, pz;
    float nx, ny, nz;
    uint32_t color;
    uint32_t extra;
};
static_assert(sizeof(Vertex) == 32, "vertex layout is shared with the GPU input layout");

constexpr uint32_t kMaxParts = 1024;  // explode offsets live in a 16 KB constant buffer

enum class UpAxis { Z = 0, Y = 1 };

struct MeshOptions {
    UpAxis up = UpAxis::Z;
    int quarterTurns[3] = {0, 0, 0};  // extra 90-degree rotations about X, Y, Z
    float creaseAngleDeg = 30.0f;     // 0 = fully faceted, 180 = fully smooth
    // When the file defines no parts (STL, single-object 3MF): treat every
    // connected piece of the mesh as a part, so it can be exploded.
    bool splitConnectedPieces = true;
};

struct MeshPart {
    std::string name;
    vec3 center;         // bounding-box centre after placement
    float radius = 0;    // bounding sphere about center
    float minZ = 0;
    size_t triangles = 0;
    float order = 0;     // 0 = farthest from the assembly centre (leaves first) .. 1 = innermost
};

struct Mesh {
    std::vector<Vertex> vertices;
    std::vector<uint32_t> indices;
    vec3 boundsMin, boundsMax;  // after placing on the ground and centring in XY
    vec3 sphereCenter;          // on the Z axis, at half height
    float sphereRadius = 1.0f;  // bounding sphere about sphereCenter: safe for any Z rotation
    std::vector<MeshPart> parts;  // at least one
    bool hasFileColors = false;
    bool hasFilePbr = false;
    size_t triangleCount() const { return indices.size() / 3; }
    vec3 size() const { return boundsMax - boundsMin; }
};

// Orients, places on the ground plane (min Z = 0, centred in XY), welds
// coincident vertices and computes angle-weighted normals split at creases.
Mesh processMesh(const TriangleSoup& soup, const MeshOptions& options, Progress* progress = nullptr);

// Runs fn(begin, end) over [0, count) split across hardware threads.
void parallelFor(size_t count, const std::function<void(size_t, size_t)>& fn, size_t minChunk = 4096);

}  // namespace spindle
