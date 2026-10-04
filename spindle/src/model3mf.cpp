// 3MF loading: a ZIP (OPC) container holding XML model files.
//
// Supported: the 3MF core spec (meshes, components, build items with
// transforms, units) and the Production extension's `p:path`, which Bambu
// Studio, Orca and PrusaSlicer use to keep each object in its own .model file.
// Materials, colours and beam lattices are ignored: Spindle applies its own
// material to the whole model.
#include "mesh.h"

#include <cctype>
#include <charconv>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <string_view>
#include <unordered_map>

// stb_image's raw-deflate decoder (the implementation is compiled in environment.cpp).
#include "../third_party/stb/stb_image.h"

namespace spindle {

namespace {

// ---------------------------------------------------------------------------
// ZIP
// ---------------------------------------------------------------------------

uint16_t rd16(const uint8_t* p) { return (uint16_t)(p[0] | (p[1] << 8)); }
uint32_t rd32(const uint8_t* p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }

struct ZipEntry {
    std::string name;
    uint16_t method = 0;
    uint32_t compressedSize = 0, size = 0, localOffset = 0;
};

// OPC part names compare case-insensitively and may be written with a leading '/'.
std::string partKey(std::string_view name) {
    std::string out;
    size_t i = 0;
    while (i < name.size() && (name[i] == '/' || name[i] == '\\')) ++i;
    for (; i < name.size(); ++i) {
        char c = name[i];
        if (c == '%' && i + 2 < name.size()) {  // percent-encoded characters in relationship targets
            int v = 0;
            if (std::from_chars(name.data() + i + 1, name.data() + i + 3, v, 16).ec == std::errc()) {
                out.push_back((char)v);
                i += 2;
                continue;
            }
        }
        out.push_back(c == '\\' ? '/' : (char)std::tolower((unsigned char)c));
    }
    return out;
}

bool readZipDirectory(const uint8_t* data, size_t size, std::map<std::string, ZipEntry>& out, std::string& error) {
    if (size < 22) {
        error = "The 3MF file is too small to be a ZIP archive.";
        return false;
    }
    // The end-of-central-directory record sits in the last 64 KB + 22 bytes.
    size_t eocd = std::string::npos;
    size_t stop = size > 22 + 65535 ? size - 22 - 65535 : 0;
    for (size_t i = size - 22 + 1; i-- > stop;)
        if (rd32(data + i) == 0x06054b50u) {
            eocd = i;
            break;
        }
    if (eocd == std::string::npos) {
        error = "The 3MF file is damaged (no ZIP directory found).";
        return false;
    }
    uint16_t count = rd16(data + eocd + 10);
    uint32_t cdOffset = rd32(data + eocd + 16);
    if (cdOffset == 0xFFFFFFFFu || count == 0xFFFF) {
        error = "ZIP64 archives (3MF files over 4 GB) are not supported.";
        return false;
    }
    size_t p = cdOffset;
    for (uint16_t i = 0; i < count; ++i) {
        if (p + 46 > size || rd32(data + p) != 0x02014b50u) {
            error = "The 3MF file is damaged (bad ZIP directory entry).";
            return false;
        }
        ZipEntry e;
        e.method = rd16(data + p + 10);
        e.compressedSize = rd32(data + p + 20);
        e.size = rd32(data + p + 24);
        uint16_t nameLen = rd16(data + p + 28), extraLen = rd16(data + p + 30), commentLen = rd16(data + p + 32);
        e.localOffset = rd32(data + p + 42);
        if (p + 46 + nameLen > size) {
            error = "The 3MF file is damaged (truncated ZIP directory).";
            return false;
        }
        e.name.assign((const char*)data + p + 46, nameLen);
        out[partKey(e.name)] = e;
        p += 46 + (size_t)nameLen + extraLen + commentLen;
    }
    return true;
}

bool extract(const uint8_t* data, size_t size, const ZipEntry& e, std::string& out, std::string& error) {
    size_t p = e.localOffset;
    if (p + 30 > size || rd32(data + p) != 0x04034b50u) {
        error = "The 3MF file is damaged (bad entry " + e.name + ").";
        return false;
    }
    size_t start = p + 30 + rd16(data + p + 26) + rd16(data + p + 28);
    if (start + e.compressedSize > size) {
        error = "The 3MF file is truncated (" + e.name + ").";
        return false;
    }
    const uint8_t* src = data + start;
    if (e.method == 0) {
        out.assign((const char*)src, e.compressedSize);
        return true;
    }
    if (e.method != 8) {
        error = e.name + " uses an unsupported ZIP compression method (" + std::to_string(e.method) + ").";
        return false;
    }
    if (e.compressedSize > 0x7FFFFFFFu) {
        error = e.name + " is too large.";
        return false;
    }
    int len = 0;
    char* raw = stbi_zlib_decode_noheader_malloc((const char*)src, (int)e.compressedSize, &len);
    if (!raw) {
        error = "Could not decompress " + e.name + " (corrupt data or out of memory).";
        return false;
    }
    out.assign(raw, (size_t)len);
    std::free(raw);
    return true;
}

// ---------------------------------------------------------------------------
// Minimal XML scanner: tags and attributes only, no allocation per attribute.
// ---------------------------------------------------------------------------

std::string_view localName(std::string_view n) {
    size_t c = n.find(':');
    return c == std::string_view::npos ? n : n.substr(c + 1);
}

struct Tag {
    std::string_view name;  // local name (namespace prefix removed)
    bool closing = false, selfClosing = false;
    std::vector<std::pair<std::string_view, std::string_view>> attrs;  // local names, raw values
    std::string_view attr(std::string_view n) const {
        for (auto& a : attrs)
            if (a.first == n) return a.second;
        return {};
    }
};

class XmlScanner {
public:
    XmlScanner(const char* begin, const char* end) : p_(begin), begin_(begin), end_(end) {}
    float progress() const { return end_ > begin_ ? float(p_ - begin_) / float(end_ - begin_) : 1.0f; }

    // Advances to the next element tag. Returns false at the end or on malformed input.
    bool next(Tag& t) {
        for (;;) {
            const char* lt = (const char*)std::memchr(p_, '<', (size_t)(end_ - p_));
            if (!lt) return false;
            p_ = lt + 1;
            if (p_ >= end_) return false;
            if (*p_ == '?' || *p_ == '!') {
                const char* stop = "?>";
                if (startsWith("!--")) stop = "-->";
                else if (startsWith("![CDATA[")) stop = "]]>";
                else if (*p_ == '!') stop = ">";
                const char* q = find(stop);
                if (!q) return false;
                p_ = q + std::strlen(stop);
                continue;
            }
            t.attrs.clear();
            t.closing = *p_ == '/';
            t.selfClosing = false;
            if (t.closing) ++p_;
            const char* n0 = p_;
            while (p_ < end_ && !isSpace(*p_) && *p_ != '>' && *p_ != '/') ++p_;
            t.name = localName(std::string_view(n0, (size_t)(p_ - n0)));
            for (;;) {
                while (p_ < end_ && isSpace(*p_)) ++p_;
                if (p_ >= end_) return false;
                if (*p_ == '>') {
                    ++p_;
                    return true;
                }
                if (*p_ == '/') {
                    t.selfClosing = true;
                    ++p_;
                    continue;
                }
                const char* a0 = p_;
                while (p_ < end_ && *p_ != '=' && !isSpace(*p_) && *p_ != '>') ++p_;
                std::string_view an(a0, (size_t)(p_ - a0));
                while (p_ < end_ && isSpace(*p_)) ++p_;
                if (p_ >= end_ || *p_ != '=') continue;  // attribute without a value: ignore
                ++p_;
                while (p_ < end_ && isSpace(*p_)) ++p_;
                if (p_ >= end_ || (*p_ != '"' && *p_ != '\'')) return false;
                char quote = *p_++;
                const char* v0 = p_;
                const char* v1 = (const char*)std::memchr(p_, quote, (size_t)(end_ - p_));
                if (!v1) return false;
                p_ = v1 + 1;
                t.attrs.emplace_back(localName(an), std::string_view(v0, (size_t)(v1 - v0)));
            }
        }
    }

private:
    static bool isSpace(char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }
    bool startsWith(const char* s) const {
        size_t n = std::strlen(s);
        return (size_t)(end_ - p_) >= n && std::memcmp(p_, s, n) == 0;
    }
    const char* find(const char* s) const {
        size_t n = std::strlen(s);
        for (const char* q = p_; q + n <= end_; ++q)
            if (std::memcmp(q, s, n) == 0) return q;
        return nullptr;
    }
    const char* p_;
    const char* begin_;
    const char* end_;
};

bool parseFloat(std::string_view s, float& out) {
    while (!s.empty() && (s.front() == ' ' || s.front() == '+')) s.remove_prefix(1);
    auto r = std::from_chars(s.data(), s.data() + s.size(), out);
    return r.ec == std::errc() && std::isfinite(out);
}

bool parseInt(std::string_view s, long long& out) {
    while (!s.empty() && (s.front() == ' ' || s.front() == '+')) s.remove_prefix(1);
    return std::from_chars(s.data(), s.data() + s.size(), out).ec == std::errc();
}

std::string decodeEntities(std::string_view s) {
    std::string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '&') {
            size_t semi = s.find(';', i);
            if (semi != std::string_view::npos) {
                std::string_view ent = s.substr(i + 1, semi - i - 1);
                char c = 0;
                if (ent == "amp") c = '&';
                else if (ent == "lt") c = '<';
                else if (ent == "gt") c = '>';
                else if (ent == "quot") c = '"';
                else if (ent == "apos") c = '\'';
                if (c) {
                    out.push_back(c);
                    i = semi;
                    continue;
                }
            }
        }
        out.push_back(s[i]);
    }
    return out;
}

// ---------------------------------------------------------------------------
// 3MF model
// ---------------------------------------------------------------------------

// 3MF transforms act on row vectors: p' = [x y z 1] * M, with M given as
// "m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32" (last row = translation).
struct Xform {
    float m[12] = {1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0};
    vec3 apply(const vec3& p) const {
        return {p.x * m[0] + p.y * m[3] + p.z * m[6] + m[9], p.x * m[1] + p.y * m[4] + p.z * m[7] + m[10],
                p.x * m[2] + p.y * m[5] + p.z * m[8] + m[11]};
    }
    // The transform that applies `first`, then `then`.
    static Xform chain(const Xform& first, const Xform& then) {
        Xform r;
        for (int i = 0; i < 4; ++i)  // rows of first (row 3 = translation)
            for (int j = 0; j < 3; ++j) {
                float v = first.m[i * 3 + 0] * then.m[0 * 3 + j] + first.m[i * 3 + 1] * then.m[1 * 3 + j] +
                          first.m[i * 3 + 2] * then.m[2 * 3 + j];
                if (i == 3) v += then.m[9 + j];
                r.m[i * 3 + j] = v;
            }
        return r;
    }
};

bool parseXform(std::string_view s, Xform& out) {
    Xform x;
    for (int i = 0; i < 12; ++i) {
        while (!s.empty() && (s.front() == ' ' || s.front() == '\t' || s.front() == '\n' || s.front() == '\r')) s.remove_prefix(1);
        size_t end = 0;
        while (end < s.size() && s[end] != ' ' && s[end] != '\t' && s[end] != '\n' && s[end] != '\r') ++end;
        if (!parseFloat(s.substr(0, end), x.m[i])) return false;
        s.remove_prefix(end);
    }
    out = x;
    return true;
}

float unitScale(std::string_view unit) {
    if (unit == "micron") return 0.001f;
    if (unit == "centimeter") return 10.0f;
    if (unit == "inch") return 25.4f;
    if (unit == "foot") return 304.8f;
    if (unit == "meter") return 1000.0f;
    return 1.0f;  // millimeter (the default)
}

struct Component {
    std::string path;  // part key of the model file, empty = same file
    long long objectId = 0;
    Xform xform;
};

struct Object {
    std::vector<vec3> vertices;
    std::vector<uint32_t> triangles;  // 3 indices each
    std::vector<Component> components;
    bool support = false;
};

struct BuildItem {
    std::string path;
    long long objectId = 0;
    Xform xform;
};

struct ModelFile {
    std::unordered_map<long long, Object> objects;
    std::vector<BuildItem> items;
    size_t nonPrintable = 0;
};

class Loader {
public:
    Loader(const uint8_t* data, size_t size, Progress* progress, LoadResult& result)
        : data_(data), size_(size), progress_(progress), r_(result) {}

    void run() {
        if (!readZipDirectory(data_, size_, zip_, r_.error)) return;
        std::string root = findRootModel();
        if (root.empty()) {
            r_.error = "This 3MF has no 3D model part (3D/3dmodel.model).";
            return;
        }
        rootKey_ = root;
        ModelFile* main = file(root, true);
        if (!main) return;
        if (main->items.empty()) {
            r_.warnings.push_back("The 3MF has no build items; showing every mesh object it contains.");
            for (auto& [id, obj] : main->objects)
                if (!obj.vertices.empty()) append(root, id, Xform(), 0);
        }
        for (size_t i = 0; i < main->items.size(); ++i) {
            const BuildItem& item = main->items[i];
            append(item.path.empty() ? root : item.path, item.objectId, item.xform, 0);
            if (cancelled()) return;
        }
        if (main->nonPrintable)
            r_.warnings.push_back("Skipped " + std::to_string(main->nonPrintable) + " build item(s) marked as not printable.");
        if (main->items.size() > 1)
            r_.warnings.push_back(std::to_string(main->items.size()) +
                                  " objects in the build are shown together at their positions on the plate.");
        if (badIndices_)
            r_.warnings.push_back("Dropped " + std::to_string(badIndices_) + " triangles with out-of-range vertex indices.");
        if (skippedSupports_)
            r_.warnings.push_back("Left out " + std::to_string(skippedSupports_) + " support object(s).");
    }

private:
    bool cancelled() const { return progress_ && progress_->cancel; }

    // The root model is the target of the 3dmodel relationship in _rels/.rels.
    std::string findRootModel() {
        auto rels = zip_.find("_rels/.rels");
        if (rels != zip_.end()) {
            std::string xml, err;
            if (extract(data_, size_, rels->second, xml, err)) {
                XmlScanner sc(xml.data(), xml.data() + xml.size());
                Tag t;
                while (sc.next(t))
                    if (t.name == "Relationship" && t.attr("Type").find("3dmodel") != std::string_view::npos) {
                        std::string key = partKey(decodeEntities(t.attr("Target")));
                        if (zip_.count(key)) return key;
                    }
            }
        }
        if (zip_.count("3d/3dmodel.model")) return "3d/3dmodel.model";
        for (auto& [key, e] : zip_)
            if (key.size() > 6 && key.compare(key.size() - 6, 6, ".model") == 0) return key;
        return {};
    }

    ModelFile* file(const std::string& key, bool isRoot = false) {
        auto it = files_.find(key);
        if (it != files_.end()) return it->second.get();
        auto z = zip_.find(key);
        if (z == zip_.end()) {
            if (isRoot) r_.error = "The 3MF is missing its model part " + key + ".";
            else r_.warnings.push_back("The 3MF refers to a missing part " + key + ".");
            files_[key] = nullptr;
            return nullptr;
        }
        std::string xml;
        std::string err;
        if (!extract(data_, size_, z->second, xml, err)) {
            if (isRoot) r_.error = err;
            else r_.warnings.push_back(err);
            files_[key] = nullptr;
            return nullptr;
        }
        auto mf = std::make_unique<ModelFile>();
        if (!parseModel(xml, *mf, isRoot)) {
            if (isRoot && r_.error.empty()) r_.error = "The 3D model XML in the 3MF is malformed.";
            else if (!isRoot) r_.warnings.push_back("The model part " + key + " is malformed; it was skipped.");
            files_[key] = nullptr;
            return nullptr;
        }
        ModelFile* raw = mf.get();
        files_[key] = std::move(mf);
        return raw;
    }

    bool parseModel(const std::string& xml, ModelFile& mf, bool reportProgress) {
        XmlScanner sc(xml.data(), xml.data() + xml.size());
        Tag t;
        float scale = 1.0f;
        Object* obj = nullptr;
        bool inBuild = false;
        size_t tick = 0;
        bool sawModel = false;
        while (sc.next(t)) {
            if (++tick % 65536 == 0) {
                if (cancelled()) return false;
                if (reportProgress && progress_) progress_->value = 0.05f + 0.75f * sc.progress();
            }
            const std::string_view n = t.name;
            if (t.closing) {
                if (n == "object") obj = nullptr;
                else if (n == "build") inBuild = false;
                continue;
            }
            if (n == "vertex" && obj) {
                vec3 v;
                if (!parseFloat(t.attr("x"), v.x) || !parseFloat(t.attr("y"), v.y) || !parseFloat(t.attr("z"), v.z)) {
                    v = {NAN, NAN, NAN};  // keeps indices aligned; the triangle is dropped later
                }
                obj->vertices.push_back(v * scale);
            } else if (n == "triangle" && obj) {
                long long a, b, c;
                if (parseInt(t.attr("v1"), a) && parseInt(t.attr("v2"), b) && parseInt(t.attr("v3"), c) && a >= 0 && b >= 0 &&
                    c >= 0 && a <= 0xFFFFFFFFll && b <= 0xFFFFFFFFll && c <= 0xFFFFFFFFll) {
                    obj->triangles.push_back((uint32_t)a);
                    obj->triangles.push_back((uint32_t)b);
                    obj->triangles.push_back((uint32_t)c);
                } else {
                    ++badIndices_;
                }
            } else if (n == "object") {
                long long id;
                if (!parseInt(t.attr("id"), id)) {
                    obj = nullptr;
                    continue;
                }
                obj = &mf.objects[id];
                obj->support = t.attr("type") == "support";
            } else if (n == "component" && obj) {
                Component c;
                if (!parseInt(t.attr("objectid"), c.objectId)) continue;
                readXform(t, scale, c.xform);
                std::string_view path = t.attr("path");
                if (!path.empty()) c.path = partKey(decodeEntities(path));
                obj->components.push_back(std::move(c));
            } else if (n == "item" && inBuild) {
                BuildItem item;
                if (!parseInt(t.attr("objectid"), item.objectId)) continue;
                if (t.attr("printable") == "0") {
                    ++mf.nonPrintable;
                    continue;
                }
                readXform(t, scale, item.xform);
                std::string_view path = t.attr("path");
                if (!path.empty()) item.path = partKey(decodeEntities(path));
                mf.items.push_back(std::move(item));
            } else if (n == "build") {
                inBuild = !t.selfClosing;
            } else if (n == "model") {
                sawModel = true;
                scale = unitScale(t.attr("unit"));
            }
        }
        return sawModel;
    }

    void readXform(const Tag& t, float scale, Xform& out) {
        std::string_view s = t.attr("transform");
        if (s.empty()) return;
        if (!parseXform(s, out)) {
            r_.warnings.push_back("Ignored a malformed transform.");
            out = Xform();
            return;
        }
        // Translations are in the file's unit; vertices were already converted to mm.
        out.m[9] *= scale;
        out.m[10] *= scale;
        out.m[11] *= scale;
    }

    void append(const std::string& fileKey, long long id, const Xform& xform, int depth) {
        if (depth > 32) {
            r_.warnings.push_back("Component nesting is too deep (a cycle?); part of the model was skipped.");
            return;
        }
        ModelFile* f = file(fileKey);
        if (!f) return;
        auto it = f->objects.find(id);
        if (it == f->objects.end()) {
            r_.warnings.push_back("The build refers to a missing object (id " + std::to_string(id) + ").");
            return;
        }
        const Object& o = it->second;
        if (o.support) {
            ++skippedSupports_;
            return;
        }
        auto& out = r_.soup.positions;
        const size_t nv = o.vertices.size();
        for (size_t i = 0; i + 2 < o.triangles.size(); i += 3) {
            uint32_t a = o.triangles[i], b = o.triangles[i + 1], c = o.triangles[i + 2];
            if (a >= nv || b >= nv || c >= nv) {
                ++badIndices_;
                continue;
            }
            out.push_back(xform.apply(o.vertices[a]));
            out.push_back(xform.apply(o.vertices[b]));
            out.push_back(xform.apply(o.vertices[c]));
        }
        for (const Component& c : o.components)
            append(c.path.empty() ? fileKey : c.path, c.objectId, Xform::chain(c.xform, xform), depth + 1);
    }

    const uint8_t* data_;
    size_t size_;
    Progress* progress_;
    LoadResult& r_;
    std::map<std::string, ZipEntry> zip_;
    std::map<std::string, std::unique_ptr<ModelFile>> files_;
    std::string rootKey_;
    size_t badIndices_ = 0;
    size_t skippedSupports_ = 0;
};

}  // namespace

LoadResult parse3mf(const uint8_t* data, size_t size, Progress* progress) {
    LoadResult r;
    Loader(data, size, progress, r).run();
    if (!r.error.empty()) return r;
    if (progress && progress->cancel) {
        r.error = "Cancelled.";
        return r;
    }
    finishLoad(r, progress);
    return r;
}

LoadResult load3mfFile(const std::string& path, Progress* progress) {
    std::vector<uint8_t> bytes;
    LoadResult r;
    if (!readWholeFile(path, bytes, r.error)) return r;
    return parse3mf(bytes.data(), bytes.size(), progress);
}

}  // namespace spindle
