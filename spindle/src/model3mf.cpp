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
    std::string name;
    std::vector<vec3> vertices;
    std::vector<uint32_t> triangles;  // 3 indices each
    std::vector<uint32_t> triStyle;   // per triangle: loader style (0 = none); empty if no properties
    std::vector<uint8_t> triPaint;    // per triangle: painted filament (0 = not painted); empty if none
    std::vector<Component> components;
    bool support = false;
};

struct BuildItem {
    std::string path;
    long long objectId = 0;
    Xform xform;
};

// A property group: basematerials, colorgroup, compositematerials or
// multiproperties, each resolved to one FaceStyle per index.
struct PropGroup {
    std::vector<FaceStyle> entries;
    bool texture = false;  // texture2dgroup: not supported, renders as "no colour"
};

struct ModelFile {
    std::unordered_map<long long, Object> objects;
    std::unordered_map<long long, PropGroup> groups;
    std::unordered_map<long long, std::vector<std::pair<float, float>>> pbr;  // id -> (metalness, roughness)
    std::vector<BuildItem> items;
    size_t nonPrintable = 0;
};

// "#RRGGBB" or "#RRGGBBAA" -> sRGB8 packed with R in the low byte (alpha forced opaque).
bool parseColor(std::string_view s, uint32_t& rgba) {
    while (!s.empty() && s.front() == ' ') s.remove_prefix(1);
    if (s.size() < 7 || s[0] != '#') return false;
    unsigned v = 0;
    if (std::from_chars(s.data() + 1, s.data() + 7, v, 16).ec != std::errc()) return false;
    rgba = ((v >> 16) & 255) | (((v >> 8) & 255) << 8) | ((v & 255) << 16) | 0xFF000000u;
    return true;
}

FaceStyle mixStyles(const FaceStyle* s, const float* w, int n) {
    float rgb[3] = {0, 0, 0}, metal = 0, rough = 0, total = 0;
    bool pbr = false;
    for (int i = 0; i < n; ++i) {
        if ((s[i].rgba >> 24) == 0 || w[i] <= 0) continue;
        for (int c = 0; c < 3; ++c) rgb[c] += w[i] * (float)((s[i].rgba >> (8 * c)) & 255);
        metal += w[i] * s[i].metalness;
        rough += w[i] * s[i].roughness;
        pbr |= s[i].hasPbr;
        total += w[i];
    }
    FaceStyle out;
    if (total <= 0) return out;
    for (int c = 0; c < 3; ++c) out.rgba |= (uint32_t)std::lround(rgb[c] / total) << (8 * c);
    out.rgba |= 0xFF000000u;
    out.hasPbr = pbr;
    out.metalness = (uint8_t)std::lround(metal / total);
    out.roughness = (uint8_t)std::lround(rough / total);
    return out;
}

std::vector<float> parseFloats(std::string_view s) {
    std::vector<float> out;
    while (!s.empty()) {
        while (!s.empty() && (s.front() == ' ' || s.front() == '\t' || s.front() == '\n' || s.front() == '\r')) s.remove_prefix(1);
        size_t e = 0;
        while (e < s.size() && s[e] != ' ' && s[e] != '\t' && s[e] != '\n' && s[e] != '\r') ++e;
        if (e == 0) break;
        float f;
        if (parseFloat(s.substr(0, e), f)) out.push_back(f);
        s.remove_prefix(e);
    }
    return out;
}

// Multi-material painting from Bambu Studio / OrcaSlicer (`paint_color`) and
// PrusaSlicer (`slic3rpe:mmu_segmentation`): a hex string, read from the end,
// encoding a subdivision tree of the triangle whose leaves carry a filament
// state (0 = unpainted, k = filament k). Returns the state covering the
// largest area, approximating each child as an equal share of its parent.
int decodePaint(std::string_view hex) {
    std::vector<uint8_t> nib;
    nib.reserve(hex.size());
    for (size_t i = hex.size(); i-- > 0;) {
        char c = hex[i];
        int v = c >= '0' && c <= '9' ? c - '0' : (c >= 'A' && c <= 'F' ? c - 'A' + 10 : (c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1));
        if (v >= 0) nib.push_back((uint8_t)v);
    }
    float weight[32] = {};
    size_t pos = 0;
    int budget = 100000;  // guards against malformed input
    std::vector<std::pair<float, int>> stack{{1.0f, 0}};
    // Iterative pre-order walk: each entry is (area share, unused).
    while (!stack.empty() && pos < nib.size() && budget-- > 0) {
        float w = stack.back().first;
        stack.pop_back();
        uint8_t code = nib[pos++];
        int splitSides = code & 3;
        if (splitSides) {
            int children = splitSides + 1;
            for (int c = 0; c < children; ++c) stack.push_back({w / children, 0});
        } else {
            int state = code >> 2;
            if (state == 3) state = pos < nib.size() ? nib[pos++] + 3 : 3;
            if (state < 32) weight[state] += w;
        }
    }
    int best = 0;
    for (int i = 1; i < 32; ++i)
        if (weight[i] > weight[best]) best = i;
    return best;
}

// Slicer project data that colours objects by filament.
struct SlicerInfo {
    std::vector<uint32_t> filamentColors;                          // filament k -> colour of index k-1
    std::map<long long, int> objectExtruder;                       // root object id -> filament
    std::map<long long, std::string> objectName;
    std::map<std::pair<long long, long long>, int> partExtruder;   // (root object, part id) -> filament
    std::map<std::pair<long long, long long>, std::string> partName;
    std::map<std::pair<long long, long long>, bool> partIsModifier;
    std::map<long long, std::vector<long long>> partOrder;         // root object -> part ids in file order
    struct Volume {
        size_t first = 0, last = 0;
        int extruder = 0;
        std::string name;
        bool modifier = false;
    };
    std::map<long long, std::vector<Volume>> volumes;  // PrusaSlicer: triangle ranges per object
    bool any() const { return !filamentColors.empty() || !objectExtruder.empty() || !volumes.empty(); }
};

// Every "#RRGGBB" after `key` up to the end of the JSON array or INI line.
std::vector<uint32_t> colorsAfterKey(const std::string& text, const char* key) {
    std::vector<uint32_t> out;
    size_t k = text.find(key);
    if (k == std::string::npos) return out;
    size_t p = k + std::strlen(key);
    bool json = false;
    for (size_t q = p; q < text.size() && q < p + 8; ++q)
        if (text[q] == '[') json = true;
    for (; p < text.size(); ++p) {
        char c = text[p];
        if ((json && c == ']') || (!json && c == '\n')) break;
        if (c == '#') {
            uint32_t rgba;
            if (parseColor(std::string_view(text).substr(p, 9), rgba)) out.push_back(rgba);
        }
    }
    return out;
}

class Loader {
public:
    Loader(const uint8_t* data, size_t size, Progress* progress, LoadResult& result)
        : data_(data), size_(size), progress_(progress), r_(result) {
        styles_.push_back(FaceStyle());  // index 0: no colour
    }

    void run() {
        if (!readZipDirectory(data_, size_, zip_, r_.error)) return;
        std::string root = findRootModel();
        if (root.empty()) {
            r_.error = "This 3MF has no 3D model part (3D/3dmodel.model).";
            return;
        }
        readSlicerInfo();
        ModelFile* main = file(root, true);
        if (!main) return;
        if (main->items.empty()) {
            r_.warnings.push_back("The 3MF has no build items; showing every mesh object it contains.");
            for (auto& [id, obj] : main->objects)
                if (!obj.vertices.empty()) append(root, id, Xform(), 0, id, -1, -1);
        }
        for (size_t i = 0; i < main->items.size(); ++i) {
            const BuildItem& item = main->items[i];
            append(item.path.empty() ? root : item.path, item.objectId, item.xform, 0, item.objectId, -1, -1);
            if (cancelled()) return;
        }
        r_.soup.styles = styles_;
        if (styles_.size() == 1) r_.soup.triStyle.clear();
        if (main->nonPrintable)
            r_.warnings.push_back("Skipped " + std::to_string(main->nonPrintable) + " build item(s) marked as not printable.");
        if (badIndices_)
            r_.warnings.push_back("Dropped " + std::to_string(badIndices_) + " triangles with out-of-range vertex indices.");
        if (skippedSupports_)
            r_.warnings.push_back("Left out " + std::to_string(skippedSupports_) + " support object(s).");
        if (skippedModifiers_)
            r_.warnings.push_back("Left out " + std::to_string(skippedModifiers_) +
                                  " modifier / negative / support-blocker part(s) (slicer settings, not geometry).");
        if (textures_)
            r_.warnings.push_back("Texture-mapped colours are not supported; those triangles use Spindle's material.");
    }

private:
    bool cancelled() const { return progress_ && progress_->cancel; }

    bool readPart(const char* key, std::string& out) {
        auto it = zip_.find(key);
        std::string err;
        return it != zip_.end() && extract(data_, size_, it->second, out, err);
    }

    // Filament colours and per-object / per-part filament assignments written by
    // Bambu Studio / OrcaSlicer (model_settings.config, project_settings.config)
    // and PrusaSlicer (Slic3r_PE_model.config, Slic3r_PE.config).
    void readSlicerInfo() {
        std::string text;
        if (readPart("metadata/project_settings.config", text)) slicer_.filamentColors = colorsAfterKey(text, "\"filament_colour\"");
        if (slicer_.filamentColors.empty() && readPart("metadata/slic3r_pe.config", text)) {
            std::vector<uint32_t> extruder = colorsAfterKey(text, "; extruder_colour =");
            std::vector<uint32_t> filament = colorsAfterKey(text, "; filament_colour =");
            slicer_.filamentColors = extruder.size() >= filament.size() && !extruder.empty() ? extruder : filament;
        }
        if (readPart("metadata/model_settings.config", text)) {
            XmlScanner sc(text.data(), text.data() + text.size());
            Tag t;
            long long obj = -1, part = -1;
            while (sc.next(t)) {
                if (t.closing) {
                    if (t.name == "part") part = -1;
                    else if (t.name == "object") obj = -1;
                    continue;
                }
                long long id;
                if (t.name == "object" && parseInt(t.attr("id"), id)) {
                    obj = id;
                } else if (t.name == "part" && obj >= 0 && parseInt(t.attr("id"), id)) {
                    part = id;
                    slicer_.partOrder[obj].push_back(id);
                    std::string_view sub = t.attr("subtype");
                    slicer_.partIsModifier[{obj, id}] = !sub.empty() && sub != "normal_part";
                    if (t.selfClosing) part = -1;
                } else if (t.name == "metadata" && obj >= 0) {
                    std::string_view key = t.attr("key"), value = t.attr("value");
                    long long n;
                    if (key == "extruder" && parseInt(value, n)) {
                        if (part >= 0) slicer_.partExtruder[{obj, part}] = (int)n;
                        else slicer_.objectExtruder[obj] = (int)n;
                    } else if (key == "name") {
                        if (part >= 0) slicer_.partName[{obj, part}] = decodeEntities(value);
                        else slicer_.objectName[obj] = decodeEntities(value);
                    }
                }
            }
        }
        if (readPart("metadata/slic3r_pe_model.config", text)) {
            XmlScanner sc(text.data(), text.data() + text.size());
            Tag t;
            long long obj = -1;
            SlicerInfo::Volume* vol = nullptr;
            while (sc.next(t)) {
                if (t.closing) {
                    if (t.name == "volume") vol = nullptr;
                    else if (t.name == "object") obj = -1;
                    continue;
                }
                long long id, a, b;
                if (t.name == "object" && parseInt(t.attr("id"), id)) {
                    obj = id;
                } else if (t.name == "volume" && obj >= 0 && parseInt(t.attr("firstid"), a) && parseInt(t.attr("lastid"), b)) {
                    slicer_.volumes[obj].push_back({(size_t)a, (size_t)b, 0, "", false});
                    vol = t.selfClosing ? nullptr : &slicer_.volumes[obj].back();
                } else if (t.name == "metadata" && obj >= 0) {
                    std::string_view key = t.attr("key"), value = t.attr("value");
                    long long n;
                    if (vol) {
                        if (key == "extruder" && parseInt(value, n)) vol->extruder = (int)n;
                        else if (key == "name") vol->name = decodeEntities(value);
                        else if (key == "volume_type") vol->modifier = value != "ModelPart";
                        else if (key == "modifier") vol->modifier = value == "1";
                    } else if (key == "extruder" && parseInt(value, n)) {
                        slicer_.objectExtruder[obj] = (int)n;
                    } else if (key == "name") {
                        slicer_.objectName[obj] = decodeEntities(value);
                    }
                }
            }
        }
    }

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

    uint32_t internStyle(const FaceStyle& fs) {
        if ((fs.rgba >> 24) == 0 && !fs.hasPbr) return 0;
        uint64_t key = (uint64_t)fs.rgba | ((uint64_t)fs.metalness << 32) | ((uint64_t)fs.roughness << 40) |
                       ((uint64_t)fs.hasPbr << 48);
        auto it = styleIndex_.find(key);
        if (it != styleIndex_.end()) return it->second;
        uint32_t id = (uint32_t)styles_.size();
        styles_.push_back(fs);
        styleIndex_[key] = id;
        return id;
    }

    // Looks up entry `index` of property group `pid` in file `mf`.
    const FaceStyle* property(ModelFile& mf, long long pid, long long index) {
        auto g = mf.groups.find(pid);
        if (g == mf.groups.end()) return nullptr;
        if (g->second.texture) {
            textures_ = true;
            return nullptr;
        }
        if (index < 0 || index >= (long long)g->second.entries.size()) return nullptr;
        return &g->second.entries[(size_t)index];
    }

    bool parseModel(const std::string& xml, ModelFile& mf, bool reportProgress) {
        XmlScanner sc(xml.data(), xml.data() + xml.size());
        Tag t;
        float scale = 1.0f;
        Object* obj = nullptr;
        long long objPid = -1, objPindex = 0;
        PropGroup* group = nullptr;
        std::vector<std::pair<float, float>>* pbrList = nullptr;
        const std::vector<std::pair<float, float>>* groupPbr = nullptr;
        // compositematerials: the base group and its index list; multiproperties: layer groups
        long long compositeBase = -1;
        std::vector<float> compositeIndices;
        std::vector<long long> multiLayers;
        bool inBuild = false, sawModel = false;
        size_t tick = 0;

        auto applyGroupPbr = [&](FaceStyle& fs, size_t index) {
            if (groupPbr && index < groupPbr->size()) {
                fs.hasPbr = true;
                fs.metalness = (uint8_t)std::lround(clampf((*groupPbr)[index].first, 0, 1) * 255);
                fs.roughness = (uint8_t)std::lround(clampf((*groupPbr)[index].second, 0, 1) * 255);
            }
        };

        while (sc.next(t)) {
            if (++tick % 65536 == 0) {
                if (cancelled()) return false;
                if (reportProgress && progress_) progress_->value = 0.05f + 0.75f * sc.progress();
            }
            const std::string_view n = t.name;
            if (t.closing) {
                if (n == "object") obj = nullptr;
                else if (n == "build") inBuild = false;
                else if (n == "basematerials" || n == "colorgroup" || n == "compositematerials" || n == "multiproperties") group = nullptr;
                else if (n == "pbmetallicdisplayproperties" || n == "pbspeculardisplayproperties") pbrList = nullptr;
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
                if (!(parseInt(t.attr("v1"), a) && parseInt(t.attr("v2"), b) && parseInt(t.attr("v3"), c) && a >= 0 && b >= 0 &&
                      c >= 0 && a <= 0xFFFFFFFFll && b <= 0xFFFFFFFFll && c <= 0xFFFFFFFFll)) {
                    ++badIndices_;
                    continue;
                }
                const size_t triIndex = obj->triangles.size() / 3;
                obj->triangles.push_back((uint32_t)a);
                obj->triangles.push_back((uint32_t)b);
                obj->triangles.push_back((uint32_t)c);
                // Triangle properties: pid + p1 (p2/p3 for per-vertex values, averaged here).
                long long pid = objPid, p1 = objPindex, p2 = -1, p3 = -1;
                parseInt(t.attr("pid"), pid);
                bool hasP1 = parseInt(t.attr("p1"), p1);
                if (!hasP1 && pid != objPid) p1 = 0;
                if (!parseInt(t.attr("p2"), p2)) p2 = p1;
                if (!parseInt(t.attr("p3"), p3)) p3 = p1;
                uint32_t style = 0;
                if (pid >= 0) {
                    const FaceStyle* e[3] = {property(mf, pid, p1), property(mf, pid, p2), property(mf, pid, p3)};
                    if (e[0] && (p2 == p1 || !e[1]) && (p3 == p1 || !e[2])) {
                        style = internStyle(*e[0]);
                    } else if (e[0] || e[1] || e[2]) {
                        FaceStyle s3[3];
                        float w[3];
                        for (int k = 0; k < 3; ++k) {
                            s3[k] = e[k] ? *e[k] : FaceStyle();
                            w[k] = e[k] ? 1.0f : 0.0f;
                        }
                        style = internStyle(mixStyles(s3, w, 3));
                    }
                }
                if (style) {
                    if (obj->triStyle.size() < triIndex) obj->triStyle.resize(triIndex, 0);
                    obj->triStyle.push_back(style);
                }
                std::string_view paint = t.attr("paint_color");
                if (paint.empty()) paint = t.attr("mmu_segmentation");
                if (!paint.empty()) {
                    int state = decodePaint(paint);
                    if (state > 0) {
                        if (obj->triPaint.size() < triIndex) obj->triPaint.resize(triIndex, 0);
                        obj->triPaint.push_back((uint8_t)std::min(state, 255));
                    }
                }
            } else if (n == "object") {
                long long id;
                if (!parseInt(t.attr("id"), id)) {
                    obj = nullptr;
                    continue;
                }
                obj = &mf.objects[id];
                obj->support = t.attr("type") == "support";
                obj->name = decodeEntities(t.attr("name"));
                objPid = -1;
                objPindex = 0;
                parseInt(t.attr("pid"), objPid);
                parseInt(t.attr("pindex"), objPindex);
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
            // ---- materials and colours ----
            else if (n == "pbmetallicdisplayproperties" || n == "pbspeculardisplayproperties") {
                long long id;
                pbrList = parseInt(t.attr("id"), id) && !t.selfClosing ? &mf.pbr[id] : nullptr;
            } else if (n == "pbmetallic" && pbrList) {
                float m = 0, r = 0.5f;
                parseFloat(t.attr("metallicness"), m);
                parseFloat(t.attr("roughness"), r);
                pbrList->push_back({m, r});
            } else if (n == "pbspecular" && pbrList) {
                float g = 0.5f;
                parseFloat(t.attr("glossiness"), g);
                pbrList->push_back({0.0f, 1.0f - g});
            } else if (n == "basematerials" || n == "colorgroup") {
                long long id, dp;
                group = parseInt(t.attr("id"), id) && !t.selfClosing ? &mf.groups[id] : nullptr;
                groupPbr = nullptr;
                if (group && parseInt(t.attr("displaypropertiesid"), dp)) {
                    auto it = mf.pbr.find(dp);
                    if (it != mf.pbr.end()) groupPbr = &it->second;
                }
            } else if ((n == "base" || n == "color") && group) {
                FaceStyle fs;
                parseColor(t.attr(n == "base" ? "displaycolor" : "color"), fs.rgba);
                applyGroupPbr(fs, group->entries.size());
                group->entries.push_back(fs);
            } else if (n == "texture2dgroup") {
                long long id;
                if (parseInt(t.attr("id"), id)) mf.groups[id].texture = true;
            } else if (n == "compositematerials") {
                long long id;
                group = parseInt(t.attr("id"), id) && !t.selfClosing ? &mf.groups[id] : nullptr;
                compositeBase = -1;
                parseInt(t.attr("matid"), compositeBase);
                compositeIndices = parseFloats(t.attr("matindices"));
            } else if (n == "composite" && group) {
                // A weighted mix of base materials.
                std::vector<float> values = parseFloats(t.attr("values"));
                std::vector<FaceStyle> parts;
                std::vector<float> weights;
                for (size_t k = 0; k < values.size() && k < compositeIndices.size(); ++k)
                    if (const FaceStyle* fs = property(mf, compositeBase, (long long)compositeIndices[k])) {
                        parts.push_back(*fs);
                        weights.push_back(values[k]);
                    }
                group->entries.push_back(parts.empty() ? FaceStyle() : mixStyles(parts.data(), weights.data(), (int)parts.size()));
            } else if (n == "multiproperties") {
                long long id;
                group = parseInt(t.attr("id"), id) && !t.selfClosing ? &mf.groups[id] : nullptr;
                multiLayers.clear();
                for (float f : parseFloats(t.attr("pids"))) multiLayers.push_back((long long)f);
            } else if (n == "multi" && group) {
                // Layers are blended in the file; use the first layer that has a colour.
                std::vector<float> idx = parseFloats(t.attr("pindices"));
                FaceStyle chosen;
                for (size_t k = 0; k < multiLayers.size(); ++k) {
                    const FaceStyle* fs = property(mf, multiLayers[k], k < idx.size() ? (long long)idx[k] : 0);
                    if (fs && (fs->rgba >> 24)) {
                        chosen = *fs;
                        break;
                    }
                }
                group->entries.push_back(chosen);
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

    uint32_t filamentStyle(int filament) {
        if (filament <= 0 || filament > (int)slicer_.filamentColors.size()) return 0;
        FaceStyle fs;
        fs.rgba = slicer_.filamentColors[(size_t)filament - 1];
        return internStyle(fs);
    }

    uint32_t newPart(const std::string& name) {
        r_.soup.partNames.push_back(name.empty() ? "Part " + std::to_string(r_.soup.partNames.size() + 1) : name);
        return (uint32_t)r_.soup.partNames.size() - 1;
    }

    // Appends object `id` of `fileKey`. rootId is the build item's object (the key
    // for slicer metadata); partId/partIndex identify the component below it.
    void append(const std::string& fileKey, long long id, const Xform& xform, int depth, long long rootId, long long partId,
                long long partIndex) {
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
        Object& o = it->second;
        if (o.support) {
            ++skippedSupports_;
            return;
        }

        // Slicer metadata for this part: modifiers are settings volumes, not geometry.
        std::pair<long long, long long> pkey(rootId, partId);
        if (depth == 1 && slicer_.partExtruder.count(pkey) == 0 && slicer_.partIsModifier.count(pkey) == 0) {
            // Some writers number parts by position rather than by object id.
            auto order = slicer_.partOrder.find(rootId);
            if (order != slicer_.partOrder.end() && partIndex >= 0 && partIndex < (long long)order->second.size())
                pkey.second = order->second[(size_t)partIndex];
        }
        if (depth >= 1 && slicer_.partIsModifier.count(pkey) && slicer_.partIsModifier[pkey]) {
            ++skippedModifiers_;
            return;
        }
        int filament = 0;
        if (slicer_.partExtruder.count(pkey)) filament = slicer_.partExtruder[pkey];
        else if (slicer_.objectExtruder.count(rootId)) filament = slicer_.objectExtruder[rootId];
        else if (!slicer_.filamentColors.empty()) filament = 1;

        if (!o.triangles.empty()) {
            std::string name = slicer_.partName.count(pkey) ? slicer_.partName[pkey] : std::string();
            if (name.empty() && depth == 0 && slicer_.objectName.count(rootId)) name = slicer_.objectName[rootId];
            if (name.empty()) name = o.name;
            emit(o, xform, filament, name, depth == 0 ? slicer_.volumes.find(rootId) : slicer_.volumes.end());
        }
        long long index = 0;
        for (const Component& c : o.components) {
            long long pid = depth == 0 ? c.objectId : partId;
            long long pidx = depth == 0 ? index : partIndex;
            append(c.path.empty() ? fileKey : c.path, c.objectId, Xform::chain(c.xform, xform), depth + 1, rootId, pid, pidx);
            ++index;
        }
    }

    void emit(const Object& o, const Xform& xform, int filament, const std::string& name,
              std::map<long long, std::vector<SlicerInfo::Volume>>::const_iterator volumes) {
        const bool hasVolumes = volumes != slicer_.volumes.end() && !volumes->second.empty();
        uint32_t part = hasVolumes ? 0 : newPart(name);
        std::vector<uint32_t> volumePart;
        if (hasVolumes)
            for (auto& v : volumes->second)
                volumePart.push_back(v.modifier ? UINT32_MAX : newPart(v.name.empty() ? name : v.name));

        auto& out = r_.soup.positions;
        auto& triStyle = r_.soup.triStyle;
        auto& triPart = r_.soup.triPart;
        // Keep the per-triangle arrays the same length as the triangle count.
        triStyle.resize(out.size() / 3, 0);
        triPart.resize(out.size() / 3, 0);
        const size_t nv = o.vertices.size();
        const uint32_t defaultStyle = filamentStyle(filament);
        for (size_t tri = 0; tri * 3 + 2 < o.triangles.size(); ++tri) {
            uint32_t a = o.triangles[tri * 3], b = o.triangles[tri * 3 + 1], c = o.triangles[tri * 3 + 2];
            if (a >= nv || b >= nv || c >= nv) {
                ++badIndices_;
                continue;
            }
            uint32_t triPartIndex = part;
            uint32_t style = defaultStyle;
            if (hasVolumes) {
                triPartIndex = UINT32_MAX;
                const auto& vols = volumes->second;
                for (size_t v = 0; v < vols.size(); ++v)
                    if (tri >= vols[v].first && tri <= vols[v].last) {
                        triPartIndex = volumePart[v];
                        if (vols[v].extruder > 0) style = filamentStyle(vols[v].extruder);
                        break;
                    }
                if (triPartIndex == UINT32_MAX) {  // inside a modifier volume, or not covered
                    continue;
                }
            }
            if (tri < o.triPaint.size() && o.triPaint[tri] > 0) {
                uint32_t painted = filamentStyle(o.triPaint[tri]);
                if (painted) style = painted;
            }
            if (tri < o.triStyle.size() && o.triStyle[tri] != 0) style = o.triStyle[tri];
            out.push_back(xform.apply(o.vertices[a]));
            out.push_back(xform.apply(o.vertices[b]));
            out.push_back(xform.apply(o.vertices[c]));
            triStyle.push_back(style);
            triPart.push_back(triPartIndex);
        }
        if (hasVolumes)
            for (uint32_t vp : volumePart)
                if (vp == UINT32_MAX) ++skippedModifiers_;
    }

    const uint8_t* data_;
    size_t size_;
    Progress* progress_;
    LoadResult& r_;
    std::map<std::string, ZipEntry> zip_;
    std::map<std::string, std::unique_ptr<ModelFile>> files_;
    SlicerInfo slicer_;
    std::vector<FaceStyle> styles_;
    std::unordered_map<uint64_t, uint32_t> styleIndex_;
    size_t badIndices_ = 0;
    size_t skippedSupports_ = 0;
    size_t skippedModifiers_ = 0;
    bool textures_ = false;
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
