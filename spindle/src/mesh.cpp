#include "mesh.h"

#include <algorithm>
#include <charconv>
#include <cstring>
#include <thread>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace spindle {

static unsigned workerCount() {
    unsigned n = std::thread::hardware_concurrency();
    return n == 0 ? 4 : std::min(n, 32u);
}

void parallelFor(size_t count, const std::function<void(size_t, size_t)>& fn, size_t minChunk) {
    if (count == 0) return;
    size_t workers = std::min<size_t>(workerCount(), (count + minChunk - 1) / minChunk);
    if (workers <= 1) {
        fn(0, count);
        return;
    }
    std::vector<std::thread> threads;
    size_t per = (count + workers - 1) / workers;
    for (size_t w = 0; w < workers; ++w) {
        size_t b = w * per, e = std::min(count, b + per);
        if (b >= e) break;
        threads.emplace_back([&fn, b, e] { fn(b, e); });
    }
    for (auto& t : threads) t.join();
}

// Sorts in independent chunks on worker threads, then merges pairs of chunks
// (also in parallel) until one sorted range remains.
template <class T, class Less>
static void parallelSort(std::vector<T>& v, Less less) {
    size_t n = v.size();
    size_t chunks = std::min<size_t>(workerCount(), std::max<size_t>(1, n / 65536));
    if (chunks <= 1) {
        std::sort(v.begin(), v.end(), less);
        return;
    }
    std::vector<size_t> bounds(chunks + 1);
    for (size_t i = 0; i <= chunks; ++i) bounds[i] = n * i / chunks;
    {
        std::vector<std::thread> threads;
        for (size_t i = 0; i < chunks; ++i)
            threads.emplace_back([&, i] { std::sort(v.begin() + bounds[i], v.begin() + bounds[i + 1], less); });
        for (auto& t : threads) t.join();
    }
    while (bounds.size() > 2) {
        std::vector<size_t> next;
        std::vector<std::thread> threads;
        for (size_t i = 0; i + 2 < bounds.size(); i += 2) {
            size_t b = bounds[i], m = bounds[i + 1], e = bounds[i + 2];
            threads.emplace_back([&v, b, m, e, less] {
                std::inplace_merge(v.begin() + b, v.begin() + m, v.begin() + e, less);
            });
            next.push_back(b);
        }
        if ((bounds.size() - 1) % 2 == 1) next.push_back(bounds[bounds.size() - 2]);
        next.push_back(bounds.back());
        for (auto& t : threads) t.join();
        bounds.swap(next);
    }
}

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

bool readWholeFile(const std::string& path, std::vector<uint8_t>& out, std::string& error) {
#ifdef _WIN32
    FILE* f = _wfopen(widen(path).c_str(), L"rb");
#else
    FILE* f = fopen(path.c_str(), "rb");
#endif
    if (!f) {
        error = "Could not open the file.";
        return false;
    }
#ifdef _WIN32
    _fseeki64(f, 0, SEEK_END);
    long long size = _ftelli64(f);
    _fseeki64(f, 0, SEEK_SET);
#else
    fseeko(f, 0, SEEK_END);
    long long size = ftello(f);
    fseeko(f, 0, SEEK_SET);
#endif
    if (size < 0) {
        fclose(f);
        error = "Could not read the file size.";
        return false;
    }
    try {
        out.resize((size_t)size);
    } catch (const std::bad_alloc&) {
        fclose(f);
        error = "Not enough memory to read the file.";
        return false;
    }
    size_t got = size ? fread(out.data(), 1, (size_t)size, f) : 0;
    fclose(f);
    if (got != (size_t)size) {
        error = "Could not read the whole file.";
        return false;
    }
    return true;
}

LoadResult loadStlFile(const std::string& path, Progress* progress) {
    std::vector<uint8_t> bytes;
    LoadResult r;
    if (!readWholeFile(path, bytes, r.error)) return r;
    return parseStl(bytes.data(), bytes.size(), progress);
}

bool isSupportedModelExtension(const std::string& path) {
    std::string e = extensionOf(path);
    return e == ".stl" || e == ".3mf";
}

LoadResult loadModelFile(const std::string& path, Progress* progress) {
    std::vector<uint8_t> bytes;
    LoadResult r;
    if (!readWholeFile(path, bytes, r.error)) return r;
    if (bytes.size() >= 4 && bytes[0] == 'P' && bytes[1] == 'K' && bytes[2] == 3 && bytes[3] == 4)
        return parse3mf(bytes.data(), bytes.size(), progress);
    return parseStl(bytes.data(), bytes.size(), progress);
}

static bool finite3(const vec3& v) { return std::isfinite(v.x) && std::isfinite(v.y) && std::isfinite(v.z); }

// Drops triangles with NaN/Inf coordinates or exactly zero area, in place.
static void dropInvalid(LoadResult& r) {
    auto& p = r.soup.positions;
    auto& st = r.soup.triStyle;
    auto& pt = r.soup.triPart;
    const size_t tris = p.size() / 3;
    if (st.size() != tris) st.clear();
    if (pt.size() != tris) pt.clear();
    size_t out = 0;
    for (size_t t = 0; t < tris; ++t) {
        const vec3 a = p[t * 3], b = p[t * 3 + 1], c = p[t * 3 + 2];
        if (!finite3(a) || !finite3(b) || !finite3(c)) {
            ++r.droppedNonFinite;
            continue;
        }
        vec3 n = cross(b - a, c - a);
        if (n.x == 0 && n.y == 0 && n.z == 0) {
            ++r.droppedDegenerate;
            continue;
        }
        p[out * 3] = a;
        p[out * 3 + 1] = b;
        p[out * 3 + 2] = c;
        if (!st.empty()) st[out] = st[t];
        if (!pt.empty()) pt[out] = pt[t];
        ++out;
    }
    p.resize(out * 3);
    if (!st.empty()) st.resize(out);
    if (!pt.empty()) pt.resize(out);
}

static bool parseBinary(const uint8_t* data, size_t size, LoadResult& r, Progress* progress) {
    uint32_t declared;
    std::memcpy(&declared, data + 80, 4);
    size_t available = (size - 84) / 50;
    size_t count = declared;
    if ((uint64_t)84 + (uint64_t)50 * declared > size) {
        count = available;
        r.warnings.push_back("The file is truncated: it declares " + std::to_string(declared) +
                             " triangles but only " + std::to_string(available) + " are complete. Loaded those.");
    } else if ((uint64_t)84 + (uint64_t)50 * declared < size) {
        r.warnings.push_back("The file has extra bytes after the last triangle; they were ignored.");
    }
    r.binary = true;
    r.soup.positions.resize(count * 3);
    std::atomic<size_t> done{0};
    parallelFor(count, [&](size_t b, size_t e) {
        for (size_t t = b; t < e; ++t) {
            const uint8_t* rec = data + 84 + t * 50 + 12;  // skip the stored facet normal
            float f[9];
            std::memcpy(f, rec, sizeof(f));
            r.soup.positions[t * 3 + 0] = {f[0], f[1], f[2]};
            r.soup.positions[t * 3 + 1] = {f[3], f[4], f[5]};
            r.soup.positions[t * 3 + 2] = {f[6], f[7], f[8]};
            if (progress && ((t - b) & 0xFFFF) == 0xFFFF) {
                size_t d = done.fetch_add(0x10000) + 0x10000;
                progress->value = 0.9f * float(d) / float(count);
            }
        }
    }, 65536);
    return true;
}

static inline bool isSpace(uint8_t c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\f' || c == '\v'; }

static inline bool matchWordCI(const uint8_t* p, const uint8_t* end, const char* word) {
    size_t n = std::strlen(word);
    if ((size_t)(end - p) < n) return false;
    for (size_t i = 0; i < n; ++i)
        if (std::tolower(p[i]) != word[i]) return false;
    return true;
}

// Finds the end of the next "endfacet" at or after p, or `end`.
static const uint8_t* nextFacetBoundary(const uint8_t* p, const uint8_t* end) {
    for (; p < end; ++p)
        if ((*p == 'e' || *p == 'E') && matchWordCI(p, end, "endfacet")) return p + 8;
    return end;
}

static const uint8_t* parseFloat(const uint8_t* p, const uint8_t* end, float& out, bool& ok) {
    while (p < end && isSpace(*p)) ++p;
    if (p < end && *p == '+') ++p;
    auto res = std::from_chars((const char*)p, (const char*)end, out);
    if (res.ec != std::errc()) {
        ok = false;
        return p;
    }
    return (const uint8_t*)res.ptr;
}

// Parses every "vertex x y z" in [b, e). Returns false on a malformed number.
static bool parseAsciiRange(const uint8_t* b, const uint8_t* e, std::vector<vec3>& out) {
    const uint8_t* p = b;
    while (p < e) {
        if ((*p == 'v' || *p == 'V') && (p == b || isSpace(p[-1])) && matchWordCI(p, e, "vertex") &&
            p + 6 < e && isSpace(p[6])) {
            p += 6;
            vec3 v;
            bool ok = true;
            p = parseFloat(p, e, v.x, ok);
            p = parseFloat(p, e, v.y, ok);
            p = parseFloat(p, e, v.z, ok);
            if (!ok) return false;
            out.push_back(v);
        } else {
            ++p;
        }
    }
    return true;
}

static bool parseAscii(const uint8_t* data, size_t size, LoadResult& r, Progress* progress) {
    const uint8_t* end = data + size;
    size_t parts = std::max<size_t>(1, std::min<size_t>(workerCount(), size / (1 << 20)));
    std::vector<const uint8_t*> cuts{data};
    for (size_t i = 1; i < parts; ++i) {
        const uint8_t* c = nextFacetBoundary(data + size * i / parts, end);
        if (c > cuts.back()) cuts.push_back(c);
    }
    cuts.push_back(end);
    size_t n = cuts.size() - 1;
    std::vector<std::vector<vec3>> chunks(n);
    std::vector<char> ok(n, 1);
    std::vector<std::thread> threads;
    std::atomic<size_t> finished{0};
    for (size_t i = 0; i < n; ++i)
        threads.emplace_back([&, i] {
            ok[i] = parseAsciiRange(cuts[i], cuts[i + 1], chunks[i]);
            if (progress) progress->value = 0.9f * float(++finished) / float(n);
        });
    for (auto& t : threads) t.join();
    for (size_t i = 0; i < n; ++i)
        if (!ok[i]) {
            r.error = "The ASCII STL contains a malformed vertex line.";
            return false;
        }
    size_t total = 0;
    for (auto& c : chunks) total += c.size();
    if (total % 3 != 0) r.warnings.push_back("A facet does not have exactly 3 vertices; the incomplete triangle was ignored.");
    r.soup.positions.reserve(total);
    for (auto& c : chunks) r.soup.positions.insert(r.soup.positions.end(), c.begin(), c.end());
    r.soup.positions.resize(total - total % 3);
    r.binary = false;
    return true;
}

static bool startsWithSolid(const uint8_t* data, size_t size) {
    size_t i = 0;
    while (i < size && isSpace(data[i])) ++i;
    return matchWordCI(data + i, data + size, "solid");
}

LoadResult parseStl(const uint8_t* data, size_t size, Progress* progress) {
    LoadResult r;
    if (size == 0) {
        r.error = "The file is empty.";
        return r;
    }
    bool solid = startsWithSolid(data, size);
    bool exactBinary = false;
    if (size >= 84) {
        uint32_t declared;
        std::memcpy(&declared, data + 80, 4);
        exactBinary = (uint64_t)84 + (uint64_t)50 * declared == size;
    }

    // The size check comes first: many binary files start with "solid" too.
    if (exactBinary) {
        parseBinary(data, size, r, progress);
    } else if (solid) {
        if (!parseAscii(data, size, r, progress)) return r;
        if (r.soup.positions.empty() && size >= 84) {
            // "solid" header but no ASCII content: treat as a damaged binary file.
            r = LoadResult();
            parseBinary(data, size, r, progress);
        }
    } else if (size >= 84) {
        parseBinary(data, size, r, progress);
    } else {
        r.error = "The file is too small to be an STL.";
        return r;
    }
    if (progress && progress->cancel) {
        r.error = "Cancelled.";
        return r;
    }

    finishLoad(r, progress);
    return r;
}

void finishLoad(LoadResult& r, Progress* progress) {
    dropInvalid(r);
    if (r.droppedNonFinite)
        r.warnings.push_back("Dropped " + std::to_string(r.droppedNonFinite) + " triangles with invalid (NaN/Inf) coordinates.");
    if (r.droppedDegenerate)
        r.warnings.push_back("Dropped " + std::to_string(r.droppedDegenerate) + " zero-area triangles.");
    if (r.soup.positions.empty()) {
        r.error = "The file contains no triangles.";
        return;
    }
    if (progress) progress->value = 1.0f;
    r.ok = true;
}

// ---------------------------------------------------------------------------
// Processing
// ---------------------------------------------------------------------------

static vec3 orient(vec3 p, const MeshOptions& o) {
    if (o.up == UpAxis::Y) p = {p.x, -p.z, p.y};  // +90 degrees about X: Y-up becomes Z-up
    for (int k = 0; k < (o.quarterTurns[0] & 3); ++k) p = {p.x, -p.z, p.y};
    for (int k = 0; k < (o.quarterTurns[1] & 3); ++k) p = {p.z, p.y, -p.x};
    for (int k = 0; k < (o.quarterTurns[2] & 3); ++k) p = {-p.y, p.x, p.z};
    return p;
}

struct CornerKey {
    uint64_t key;
    uint32_t corner;
};

Mesh processMesh(const TriangleSoup& soup, const MeshOptions& options, Progress* progress) {
    Mesh mesh;
    const size_t corners = soup.positions.size();
    const size_t tris = corners / 3;
    if (tris == 0) return mesh;

    // 1. Orient, then place on the ground and centre in XY.
    std::vector<vec3> pos(corners);
    parallelFor(corners, [&](size_t b, size_t e) {
        for (size_t i = b; i < e; ++i) pos[i] = orient(soup.positions[i], options);
    });
    vec3 lo = pos[0], hi = pos[0];
    for (const vec3& p : pos) {
        lo = vmin(lo, p);
        hi = vmax(hi, p);
    }
    vec3 shift = {-(lo.x + hi.x) * 0.5f, -(lo.y + hi.y) * 0.5f, -lo.z};
    parallelFor(corners, [&](size_t b, size_t e) {
        for (size_t i = b; i < e; ++i) pos[i] += shift;
    });
    lo += shift;
    hi += shift;
    mesh.boundsMin = lo;
    mesh.boundsMax = hi;
    if (progress) progress->value = 0.1f;

    // 2. Face normals (unit) and the interior angle at each corner.
    std::vector<vec3> faceN(tris);
    std::vector<float> cornerAngle(corners);
    parallelFor(tris, [&](size_t b, size_t e) {
        for (size_t t = b; t < e; ++t) {
            const vec3 &a = pos[t * 3], &bb = pos[t * 3 + 1], &c = pos[t * 3 + 2];
            faceN[t] = normalize(cross(bb - a, c - a));
            const vec3* v[3] = {&a, &bb, &c};
            for (int k = 0; k < 3; ++k) {
                vec3 e1 = normalize(*v[(k + 1) % 3] - *v[k]);
                vec3 e2 = normalize(*v[(k + 2) % 3] - *v[k]);
                cornerAngle[t * 3 + k] = std::acos(clampf(dot(e1, e2), -1.0f, 1.0f));
            }
        }
    });
    if (progress) progress->value = 0.25f;

    // 3. Weld: quantise to a 2^21 grid over the bounding box and sort corners by cell.
    std::vector<CornerKey> keys(corners);
    const float kLevels = float((1u << 21) - 1);
    vec3 extent = vmax(hi - lo, vec3(1e-12f, 1e-12f, 1e-12f));
    parallelFor(corners, [&](size_t b, size_t e) {
        for (size_t i = b; i < e; ++i) {
            vec3 q = pos[i] - lo;
            uint64_t qx = (uint64_t)std::llround(clampf(q.x / extent.x, 0, 1) * kLevels);
            uint64_t qy = (uint64_t)std::llround(clampf(q.y / extent.y, 0, 1) * kLevels);
            uint64_t qz = (uint64_t)std::llround(clampf(q.z / extent.z, 0, 1) * kLevels);
            keys[i] = {(qx << 42) | (qy << 21) | qz, (uint32_t)i};
        }
    });
    parallelSort(keys, [](const CornerKey& a, const CornerKey& b) {
        return a.key < b.key || (a.key == b.key && a.corner < b.corner);
    });
    if (progress) progress->value = 0.6f;

    // 4. Runs of corners that share a welded position.
    std::vector<size_t> runStart;
    runStart.reserve(corners / 4);
    for (size_t i = 0; i < corners; ++i)
        if (i == 0 || keys[i].key != keys[i - 1].key) runStart.push_back(i);
    runStart.push_back(corners);
    const size_t runs = runStart.size() - 1;

    // 5. Parts: from the file when it defines several, otherwise the connected
    //    pieces of the mesh (union-find over triangles that share a position).
    std::vector<uint32_t> triPart(tris, 0);
    std::vector<std::string> partNames;
    if (soup.triPart.size() == tris && soup.partNames.size() > 1) {
        triPart = soup.triPart;
        partNames = soup.partNames;
    } else if (options.splitConnectedPieces) {
        std::vector<uint32_t> parent(tris);
        for (size_t t = 0; t < tris; ++t) parent[t] = (uint32_t)t;
        auto find = [&](uint32_t x) {
            while (parent[x] != x) x = parent[x] = parent[parent[x]];
            return x;
        };
        for (size_t r = 0; r < runs; ++r) {
            uint32_t first = find(keys[runStart[r]].corner / 3);
            for (size_t i = runStart[r] + 1; i < runStart[r + 1]; ++i) {
                uint32_t o = find(keys[i].corner / 3);
                if (o != first) parent[o] = first;
            }
        }
        std::vector<uint32_t> label(tris, UINT32_MAX);
        uint32_t next = 0;
        for (size_t t = 0; t < tris; ++t) {
            uint32_t root = find((uint32_t)t);
            if (label[root] == UINT32_MAX) label[root] = next++;
            triPart[t] = label[root];
        }
        for (uint32_t i = 0; i < next; ++i) partNames.push_back("Piece " + std::to_string(i + 1));
    }
    if (partNames.empty()) partNames.push_back("Model");
    // Keep the kMaxParts - 1 largest parts separate; the rest move together.
    if (partNames.size() > kMaxParts) {
        std::vector<size_t> count(partNames.size(), 0);
        for (uint32_t p : triPart) ++count[p];
        std::vector<uint32_t> byCount(partNames.size());
        for (size_t i = 0; i < byCount.size(); ++i) byCount[i] = (uint32_t)i;
        std::sort(byCount.begin(), byCount.end(), [&](uint32_t a, uint32_t b) { return count[a] > count[b]; });
        std::vector<uint32_t> remap(partNames.size(), kMaxParts - 1);
        std::vector<std::string> names;
        for (uint32_t i = 0; i < kMaxParts - 1; ++i) {
            remap[byCount[i]] = i;
            names.push_back(partNames[byCount[i]]);
        }
        names.push_back("Other pieces (" + std::to_string(partNames.size() - (kMaxParts - 1)) + ")");
        for (auto& p : triPart) p = remap[p];
        partNames.swap(names);
    }

    // 6. Within each run, cluster corners of the same part whose faces lie within
    //    the crease angle of a cluster's seed face (one smooth normal each), then
    //    split clusters by file style so colours stay sharp at their boundaries.
    const float cosCrease = std::cos(radians(clampf(options.creaseAngleDeg, 0.0f, 180.0f))) - 1e-4f;
    const bool styled = soup.triStyle.size() == tris;
    // A run of k corners has at most k clusters/vertices, so entry j of run r is
    // stored at slot runStart[r] + j of the per-corner arrays below.
    std::vector<uint32_t> cornerVertex(corners);  // vertex index within its run
    std::vector<uint32_t> runVertexCount(runs);
    std::vector<vec3> slotNormal(corners);
    std::vector<uint32_t> slotStyle(corners), slotPart(corners);
    parallelFor(runs, [&](size_t b, size_t e) {
        std::vector<vec3> seedFace, sum;
        std::vector<uint32_t> seedPart, cornerCluster;
        std::vector<std::pair<uint32_t, uint32_t>> verts;  // (cluster, style)
        for (size_t r = b; r < e; ++r) {
            seedFace.clear();
            sum.clear();
            seedPart.clear();
            cornerCluster.clear();
            verts.clear();
            for (size_t i = runStart[r]; i < runStart[r + 1]; ++i) {
                uint32_t c = keys[i].corner;
                const vec3& fn = faceN[c / 3];
                uint32_t part = triPart[c / 3];
                size_t found = seedFace.size();
                for (size_t s = 0; s < seedFace.size(); ++s)
                    if (seedPart[s] == part && dot(seedFace[s], fn) >= cosCrease) {
                        found = s;
                        break;
                    }
                if (found == seedFace.size()) {
                    seedFace.push_back(fn);
                    seedPart.push_back(part);
                    sum.push_back(vec3(0, 0, 0));
                }
                sum[found] += fn * cornerAngle[c];
                cornerCluster.push_back((uint32_t)found);
            }
            for (size_t i = runStart[r]; i < runStart[r + 1]; ++i) {
                uint32_t c = keys[i].corner;
                std::pair<uint32_t, uint32_t> key(cornerCluster[i - runStart[r]], styled ? soup.triStyle[c / 3] : 0u);
                size_t v = 0;
                while (v < verts.size() && verts[v] != key) ++v;
                if (v == verts.size()) verts.push_back(key);
                cornerVertex[i] = (uint32_t)v;
            }
            runVertexCount[r] = (uint32_t)verts.size();
            for (size_t v = 0; v < verts.size(); ++v) {
                uint32_t cl = verts[v].first;
                slotNormal[runStart[r] + v] = length(sum[cl]) > 0 ? normalize(sum[cl]) : seedFace[cl];
                slotStyle[runStart[r] + v] = verts[v].second;
                slotPart[runStart[r] + v] = seedPart[cl];
            }
        }
    });
    if (progress) progress->value = 0.8f;

    // 7. Assign global vertex ids (prefix sum) and emit.
    std::vector<uint32_t> runBase(runs + 1, 0);
    for (size_t r = 0; r < runs; ++r) runBase[r + 1] = runBase[r] + runVertexCount[r];
    mesh.vertices.resize(runBase[runs]);
    mesh.indices.resize(corners);
    parallelFor(runs, [&](size_t b, size_t e) {
        for (size_t r = b; r < e; ++r) {
            const vec3& p = pos[keys[runStart[r]].corner];
            for (uint32_t v = 0; v < runVertexCount[r]; ++v) {
                const size_t slot = runStart[r] + v;
                const vec3& n = slotNormal[slot];
                Vertex& out = mesh.vertices[runBase[r] + v];
                out = {p.x, p.y, p.z, n.x, n.y, n.z, 0u, slotPart[slot] & 0xFFFu};
                if (styled) {
                    const FaceStyle& fs = soup.styles[slotStyle[slot]];
                    out.color = fs.rgba;
                    if (fs.hasPbr) out.extra |= (1u << 12) | ((uint32_t)fs.metalness << 16) | ((uint32_t)fs.roughness << 24);
                }
            }
            for (size_t i = runStart[r]; i < runStart[r + 1]; ++i)
                mesh.indices[keys[i].corner] = runBase[r] + cornerVertex[i];
        }
    });
    if (styled)
        for (const FaceStyle& fs : soup.styles) {
            if ((fs.rgba >> 24) != 0) mesh.hasFileColors = true;
            if (fs.hasPbr) mesh.hasFilePbr = true;
        }

    // 6. Bounding sphere about the turntable axis, so no rotation can clip it.
    mesh.sphereCenter = {0, 0, (lo.z + hi.z) * 0.5f};
    float r2 = 0;
    for (const Vertex& v : mesh.vertices) {
        vec3 d = vec3(v.px, v.py, v.pz) - mesh.sphereCenter;
        r2 = std::max(r2, dot(d, d));
    }
    mesh.sphereRadius = std::max(std::sqrt(r2), 1e-6f);

    // 8. Per-part bounds, and the order parts leave in when exploding (outermost first).
    mesh.parts.resize(partNames.size());
    std::vector<vec3> plo(partNames.size(), vec3(1e30f, 1e30f, 1e30f)), phi(partNames.size(), vec3(-1e30f, -1e30f, -1e30f));
    for (size_t t = 0; t < tris; ++t) {
        uint32_t p = triPart[t];
        ++mesh.parts[p].triangles;
        for (int k = 0; k < 3; ++k) {
            plo[p] = vmin(plo[p], pos[t * 3 + k]);
            phi[p] = vmax(phi[p], pos[t * 3 + k]);
        }
    }
    for (size_t p = 0; p < mesh.parts.size(); ++p) {
        MeshPart& mp = mesh.parts[p];
        mp.name = partNames[p];
        if (!mp.triangles) {
            mp.center = mesh.sphereCenter;
            continue;
        }
        mp.center = (plo[p] + phi[p]) * 0.5f;
        mp.radius = length(phi[p] - plo[p]) * 0.5f;
        mp.minZ = plo[p].z;
    }
    std::vector<uint32_t> rank(mesh.parts.size());
    for (size_t i = 0; i < rank.size(); ++i) rank[i] = (uint32_t)i;
    std::sort(rank.begin(), rank.end(), [&](uint32_t a, uint32_t b) {
        return length(mesh.parts[a].center - mesh.sphereCenter) > length(mesh.parts[b].center - mesh.sphereCenter);
    });
    for (size_t i = 0; i < rank.size(); ++i)
        mesh.parts[rank[i]].order = rank.size() > 1 ? (float)i / (float)(rank.size() - 1) : 0.0f;
    if (progress) progress->value = 1.0f;
    return mesh;
}

}  // namespace spindle
