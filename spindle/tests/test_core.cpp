// Unit tests for the platform-neutral core: STL parsing, mesh processing,
// scene JSON, turntable timing and environment analysis.
// Builds natively on any OS (no Direct3D), e.g.:
//   g++ -std=c++17 -O2 -pthread -I src tests/test_core.cpp src/{common,mesh,scene,camera,environment}.cpp
#include "../src/camera.h"
#include "../src/environment.h"
#include "../src/mesh.h"
#include "../src/scene.h"

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using namespace spindle;

static int g_failures = 0;
#define CHECK(cond)                                                          \
    do {                                                                     \
        if (!(cond)) {                                                       \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            ++g_failures;                                                    \
        }                                                                    \
    } while (0)

static const float kCube[12][9] = {
    // -Z
    {0, 0, 0, 1, 1, 0, 1, 0, 0}, {0, 0, 0, 0, 1, 0, 1, 1, 0},
    // +Z
    {0, 0, 1, 1, 0, 1, 1, 1, 1}, {0, 0, 1, 1, 1, 1, 0, 1, 1},
    // -Y
    {0, 0, 0, 1, 0, 0, 1, 0, 1}, {0, 0, 0, 1, 0, 1, 0, 0, 1},
    // +Y
    {0, 1, 0, 1, 1, 1, 1, 1, 0}, {0, 1, 0, 0, 1, 1, 1, 1, 1},
    // -X
    {0, 0, 0, 0, 0, 1, 0, 1, 1}, {0, 0, 0, 0, 1, 1, 0, 1, 0},
    // +X
    {1, 0, 0, 1, 1, 0, 1, 1, 1}, {1, 0, 0, 1, 1, 1, 1, 0, 1},
};

static std::vector<uint8_t> binaryStl(const float (*tris)[9], size_t n, const char* header = "binary") {
    std::vector<uint8_t> b(84 + 50 * n, 0);
    std::strncpy((char*)b.data(), header, 80);
    uint32_t count = (uint32_t)n;
    std::memcpy(&b[80], &count, 4);
    for (size_t t = 0; t < n; ++t) std::memcpy(&b[84 + t * 50 + 12], tris[t], 36);
    return b;
}

static std::string asciiStl(const float (*tris)[9], size_t n) {
    std::string s = "solid cube\n";
    char buf[256];
    for (size_t t = 0; t < n; ++t) {
        s += "  facet normal 0 0 0\n    outer loop\n";
        for (int v = 0; v < 3; ++v) {
            std::snprintf(buf, sizeof(buf), "      vertex %g %g %e\n", tris[t][v * 3], tris[t][v * 3 + 1], tris[t][v * 3 + 2]);
            s += buf;
        }
        s += "    endloop\n  endfacet\n";
    }
    s += "endsolid cube\n";
    return s;
}

static void testParsing() {
    auto bin = binaryStl(kCube, 12);
    LoadResult r = parseStl(bin.data(), bin.size());
    CHECK(r.ok && r.binary && r.soup.triangleCount() == 12);

    // Binary file whose header starts with "solid" must still load as binary.
    auto solidBin = binaryStl(kCube, 12, "solid exported by a careless CAD tool");
    r = parseStl(solidBin.data(), solidBin.size());
    CHECK(r.ok && r.binary && r.soup.triangleCount() == 12);

    std::string ascii = asciiStl(kCube, 12);
    r = parseStl((const uint8_t*)ascii.data(), ascii.size());
    CHECK(r.ok && !r.binary && r.soup.triangleCount() == 12);
    CHECK(r.soup.positions[4].y == 1 && r.soup.positions[5].x == 1 && r.soup.positions[6].z == 1);

    // Upper-case keywords and a leading '+'.
    std::string upper = "SOLID x\nFACET NORMAL 0 0 1\nOUTER LOOP\nVERTEX 0 0 0\nVERTEX +1 0 0\nVERTEX 0 1 0\nENDLOOP\nENDFACET\nENDSOLID\n";
    r = parseStl((const uint8_t*)upper.data(), upper.size());
    CHECK(r.ok && r.soup.triangleCount() == 1 && r.soup.positions[1].x == 1);

    // Zero triangles: a clear error, not a crash.
    auto empty = binaryStl(kCube, 0);
    r = parseStl(empty.data(), empty.size());
    CHECK(!r.ok && !r.error.empty());
    r = parseStl(nullptr, 0);
    CHECK(!r.ok);

    // Truncated: loads the complete triangles and warns.
    auto trunc = binaryStl(kCube, 12);
    trunc.resize(84 + 50 * 7 + 20);
    r = parseStl(trunc.data(), trunc.size());
    CHECK(r.ok && r.soup.triangleCount() == 7 && !r.warnings.empty());

    // NaN and zero-area triangles are dropped and counted.
    float bad[3][9] = {{0, 0, 0, 1, 0, 0, 0, 1, 0}, {0, 0, 0, 1, 0, 0, 2, 0, 0}, {0, 0, 0, NAN, 0, 0, 0, 1, 0}};
    auto badBin = binaryStl(bad, 3);
    r = parseStl(badBin.data(), badBin.size());
    CHECK(r.ok && r.soup.triangleCount() == 1 && r.droppedDegenerate == 1 && r.droppedNonFinite == 1);

    // Malformed ASCII number.
    std::string broken = "solid x\nfacet normal 0 0 1\nouter loop\nvertex 0 0 zz\nendloop\nendfacet\nendsolid\n";
    r = parseStl((const uint8_t*)broken.data(), broken.size());
    CHECK(!r.ok);
}

static TriangleSoup cubeSoup() {
    TriangleSoup s;
    for (auto& t : kCube)
        for (int v = 0; v < 3; ++v) s.positions.push_back({t[v * 3] * 10, t[v * 3 + 1] * 10, t[v * 3 + 2] * 10 + 5});
    return s;
}

static void testProcessing() {
    TriangleSoup soup = cubeSoup();
    MeshOptions o;
    o.creaseAngleDeg = 30;
    Mesh m = processMesh(soup, o);
    CHECK(m.triangleCount() == 12);
    CHECK(m.vertices.size() == 24);  // each corner split into 3 flat faces
    CHECK(std::fabs(m.boundsMin.z) < 1e-6f && std::fabs(m.boundsMax.z - 10) < 1e-5f);
    CHECK(std::fabs(m.boundsMin.x + 5) < 1e-5f && std::fabs(m.boundsMax.x - 5) < 1e-5f);
    // Flat normals are axis-aligned.
    for (auto& v : m.vertices) {
        float ax = std::fabs(v.nx) + std::fabs(v.ny) + std::fabs(v.nz);
        CHECK(std::fabs(ax - 1) < 1e-4f);
    }
    // Bounding sphere about (0,0,5) reaches the corners.
    CHECK(std::fabs(m.sphereRadius - std::sqrt(75.0f)) < 1e-3f);

    o.creaseAngleDeg = 180;
    m = processMesh(soup, o);
    CHECK(m.vertices.size() == 8);  // fully smooth: one vertex per corner
    for (auto& v : m.vertices) {
        vec3 p(v.px, v.py, v.pz - 5), n(v.nx, v.ny, v.nz);
        CHECK(dot(normalize(p), n) > 0.99f);  // corner normals point diagonally out
    }

    // Y-up: the cube's original Y extent becomes Z.
    TriangleSoup tall;
    for (auto& t : kCube)
        for (int v = 0; v < 3; ++v) tall.positions.push_back({t[v * 3], t[v * 3 + 1] * 30, t[v * 3 + 2]});
    o.up = UpAxis::Y;
    m = processMesh(tall, o);
    CHECK(std::fabs(m.size().z - 30) < 1e-4f && std::fabs(m.size().y - 1) < 1e-4f);

    // Every index is in range.
    for (uint32_t i : m.indices) CHECK(i < m.vertices.size());

    // A larger mesh exercises the parallel sort and merge paths.
    TriangleSoup big;
    const int N = 300;
    for (int y = 0; y < N; ++y)
        for (int x = 0; x < N; ++x) {
            vec3 a(x, y, 0), b(x + 1, y, 0), c(x + 1, y + 1, 0), d(x, y + 1, 0);
            auto h = [](vec3 p) { p.z = std::sin(p.x * 0.1f) * std::cos(p.y * 0.1f) * 5; return p; };
            big.positions.insert(big.positions.end(), {h(a), h(b), h(c), h(a), h(c), h(d)});
        }
    o = MeshOptions();
    o.creaseAngleDeg = 60;
    m = processMesh(big, o);
    CHECK(m.vertices.size() == (size_t)(N + 1) * (N + 1));
    CHECK(m.triangleCount() == (size_t)N * N * 2);
}

static void testScene() {
    Scene s;
    s.material.baseColor = {0.1f, 0.2f, 0.3f};
    s.environment.ground = Ground::Reflective;
    s.turntable.clockwise = true;
    s.output.format = Format::GIF;
    s.model.up = UpAxis::Y;
    std::string json = sceneToJson(s);
    Scene t;
    std::string err;
    CHECK(sceneFromJson(json, t, err));
    CHECK(std::fabs(t.material.baseColor.y - 0.2f) < 1e-6f);
    CHECK(t.environment.ground == Ground::Reflective);
    CHECK(t.turntable.clockwise);
    CHECK(t.output.format == Format::GIF);
    CHECK(t.model.up == UpAxis::Y);
    CHECK(sceneToJson(t) == json);

    // Partial file: unknown keys ignored, missing keys keep defaults, preset applied first.
    Scene p;
    CHECK(sceneFromJson(R"({"material":{"preset":"Gold","roughness":0.5},"bogus":1,"output":{"width":99999}})", p, err));
    CHECK(p.material.metalness == 1.0f && p.material.roughness == 0.5f);
    CHECK(p.output.width == 8192);
    CHECK(!sceneFromJson("{not json", p, err));

    for (auto& name : materialPresetNames()) {
        MaterialSettings m;
        CHECK(applyMaterialPreset(name, m));
        CHECK(m.preset == name);
    }
}

static void testTurntable() {
    TurntableSettings t;
    t.seconds = 4;
    t.fps = 30;
    CHECK(t.frameCount() == 120);
    CHECK(turntableAngleDeg(t, 0) == 0);
    CHECK(std::fabs(turntableAngleDeg(t, 120) - 360) < 1e-4f);  // frame N == frame 0: seamless
    CHECK(turntableAngleDeg(t, 119) < 360);
    t.clockwise = true;
    CHECK(turntableAngleDeg(t, 30) < 0);
    t.clockwise = false;
    t.easing = Easing::EaseInOut;
    CHECK(std::fabs(turntableAngleDeg(t, 119) - 360) < 1e-3f);
    t.easing = Easing::HoldThenSpin;
    t.holdSeconds = 1;
    CHECK(turntableAngleDeg(t, 29) == 0 && turntableAngleDeg(t, 31) > 0);

    // Auto-fit: the bounding sphere's angular size is the requested fill.
    Scene s;
    float aspect = 16.0f / 9.0f;
    TurntablePose p = turntablePose(s, {0, 0, 50}, 50, aspect, 0, 0);
    float d = length(p.eye - p.target);
    float angular = std::asin(50 / d);
    CHECK(std::fabs(std::tan(angular) - s.camera.fill * std::tan(p.fovY / 2)) < 1e-4f);

    // Jittered projection shifts NDC by the requested pixel fraction.
    ViewRequest r;
    r.eye = {0, -100, 0};
    r.target = {0, 0, 0};
    r.aspect = 1;
    r.widthPx = r.heightPx = 100;
    r.jitterX = 0.5f;
    View v = buildView(r);
    mat4 vp = v.proj * v.view;
    vec3 ndc = vp.transformPoint({0, 0, 0});
    CHECK(std::fabs(ndc.x - 0.01f) < 1e-5f && std::fabs(ndc.y) < 1e-5f);
    // The target projects to the screen centre and lies inside the depth range.
    CHECK(ndc.z > 0 && ndc.z < 1);
    // Inverse is an inverse.
    mat4 id = vp * vp.inverse();
    CHECK(std::fabs(id.m[0][0] - 1) < 1e-4f && std::fabs(id.m[2][3]) < 1e-3f);
}

static void testEnvironment() {
    for (auto& name : proceduralEnvironmentNames()) {
        EnvironmentImage e;
        CHECK(makeProceduralEnvironment(name, e, 256));
        CHECK(e.width == 256 && e.height == 128);
        CHECK(e.sh[0].x > 0);
        CHECK(e.dominantDirection.z > 0.2f);  // light comes from above the horizon
    }
    EnvironmentImage key;
    makeProceduralEnvironment("Studio Softbox", key, 256);
    // The key softbox is at azimuth 45: the dominant direction should lean that way.
    CHECK(key.dominantDirection.x > 0 && key.dominantDirection.y > 0);
    EnvironmentImage overcast;
    makeProceduralEnvironment("Overcast", overcast, 256);
    CHECK(overcast.directionality < key.directionality);
    // Irradiance of a uniform white sphere is 1 for every normal.
    EnvironmentImage white;
    white.width = 64;
    white.height = 32;
    white.rgba.assign(64 * 32 * 4, 1.0f);
    analyzeEnvironment(white);
    CHECK(std::fabs(white.sh[0].x * 0.282095f - 1.0f) < 0.02f);
    CHECK(white.directionality < 0.02f);
    // Equirect mapping round trip: +Z is the top row.
    vec3 up = equirectDirection(0.5f, 0.0f);
    CHECK(up.z > 0.999f);
}

int main() {
    testParsing();
    testProcessing();
    testScene();
    testTurntable();
    testEnvironment();
    if (g_failures) {
        std::printf("%d check(s) failed\n", g_failures);
        return 1;
    }
    std::printf("all core tests passed\n");
    return 0;
}
