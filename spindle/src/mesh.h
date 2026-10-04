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

// Raw triangles exactly as read from the file (after dropping invalid ones).
struct TriangleSoup {
    std::vector<vec3> positions;  // 3 per triangle
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

struct Vertex {
    float px, py, pz;
    float nx, ny, nz;
};
static_assert(sizeof(Vertex) == 24, "vertex layout is shared with the GPU input layout");

enum class UpAxis { Z = 0, Y = 1 };

struct MeshOptions {
    UpAxis up = UpAxis::Z;
    int quarterTurns[3] = {0, 0, 0};  // extra 90-degree rotations about X, Y, Z
    float creaseAngleDeg = 30.0f;     // 0 = fully faceted, 180 = fully smooth
};

struct Mesh {
    std::vector<Vertex> vertices;
    std::vector<uint32_t> indices;
    vec3 boundsMin, boundsMax;  // after placing on the ground and centring in XY
    vec3 sphereCenter;          // on the Z axis, at half height
    float sphereRadius = 1.0f;  // bounding sphere about sphereCenter: safe for any Z rotation
    size_t triangleCount() const { return indices.size() / 3; }
    vec3 size() const { return boundsMax - boundsMin; }
};

// Orients, places on the ground plane (min Z = 0, centred in XY), welds
// coincident vertices and computes angle-weighted normals split at creases.
Mesh processMesh(const TriangleSoup& soup, const MeshOptions& options, Progress* progress = nullptr);

// Runs fn(begin, end) over [0, count) split across hardware threads.
void parallelFor(size_t count, const std::function<void(size_t, size_t)>& fn, size_t minChunk = 4096);

}  // namespace spindle
