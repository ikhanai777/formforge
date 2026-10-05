#include "gl_renderer.h"

#include "gl_shaders.h"

#include <cstring>

namespace spindle {

namespace {

// Unit assignment, identical to the HLSL texture registers.
enum Unit { U_SHADOW = 0, U_SPEC, U_BRDF, U_AO, U_REFL, U_USER, U_DEPTH, U_SRC, U_ACCUM, U_EQUIRECT, U_ENV, U_AORAW };
const char* kSamplerNames[12] = {"tShadow", "tSpec", "tBrdf",     "tAO",  "tRefl", "tUser",
                                 "tDepth",  "tSrc",  "tAccum",    "tEquirect", "tEnv", "tAORaw"};
enum Block { B_FRAME = 0, B_MATERIAL, B_BAKE, B_PARTS };

// Direct3D-style projection (depth 0..1) to OpenGL clip space (depth -1..1).
mat4 toGlClip(const mat4& m) {
    mat4 c;
    c.m[2][2] = 2;
    c.m[2][3] = -1;
    return c * m;
}

uint16_t floatToHalf(float f) {
    uint32_t x;
    std::memcpy(&x, &f, 4);
    uint32_t sign = (x >> 16) & 0x8000;
    int32_t exp = (int32_t)((x >> 23) & 0xFF) - 127 + 15;
    uint32_t mant = x & 0x7FFFFF;
    if (((x >> 23) & 0xFF) == 0xFF) return (uint16_t)(sign | 0x7C00 | (mant ? 0x200 : 0));
    if (exp >= 31) return (uint16_t)(sign | 0x7BFF);
    if (exp <= 0) {
        if (exp < -10) return (uint16_t)sign;
        mant |= 0x800000;
        uint32_t shift = (uint32_t)(14 - exp);
        uint32_t h = mant >> shift;
        if ((mant >> (shift - 1)) & 1) ++h;
        return (uint16_t)(sign | h);
    }
    uint32_t h = sign | ((uint32_t)exp << 10) | (mant >> 13);
    if (mant & 0x1000) ++h;
    return (uint16_t)h;
}

GLuint compile(GLenum type, const std::string& src, std::string& error) {
    GLuint s = glCreateShader(type);
    const char* p = src.c_str();
    glShaderSource(s, 1, &p, nullptr);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[4096];
        glGetShaderInfoLog(s, sizeof(log), nullptr, log);
        error = std::string(type == GL_VERTEX_SHADER ? "Vertex" : "Fragment") + " shader failed to compile:\n" + log;
        glDeleteShader(s);
        return 0;
    }
    return s;
}

GLuint makeTexture2D(GLenum internal, int w, int h, int levels = 1) {
    GLuint t;
    glGenTextures(1, &t);
    glBindTexture(GL_TEXTURE_2D, t);
    glTexStorage2D(GL_TEXTURE_2D, levels, internal, w, h);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, levels - 1);
    return t;
}

GLuint makeFbo(GLuint color, GLuint depthTex, GLuint depthRb = 0) {
    GLuint f;
    glGenFramebuffers(1, &f);
    glBindFramebuffer(GL_FRAMEBUFFER, f);
    if (color) {
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, color, 0);
        GLenum b = GL_COLOR_ATTACHMENT0;
        glDrawBuffers(1, &b);
    } else {
        GLenum b = GL_NONE;
        glDrawBuffers(1, &b);
        glReadBuffer(GL_NONE);
    }
    if (depthTex) glFramebufferTexture2D(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_TEXTURE_2D, depthTex, 0);
    if (depthRb) glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, depthRb);
    return f;
}

bool hasExtension(const char* name) {
    GLint n = 0;
    glGetIntegerv(GL_NUM_EXTENSIONS, &n);
    for (GLint i = 0; i < n; ++i) {
        const char* e = (const char*)glGetStringi(GL_EXTENSIONS, (GLuint)i);
        if (e && std::strcmp(e, name) == 0) return true;
    }
    return false;
}

}  // namespace

void GlTargets::release() {
    GLuint tex[] = {hdrTex, depthTex, aoRawTex, aoTex, reflTex, outTex, accumTex[0], accumTex[1]};
    for (GLuint t : tex)
        if (t) glDeleteTextures(1, &t);
    if (reflDepth) glDeleteRenderbuffers(1, &reflDepth);
    GLuint fbos[] = {fboDepth, fboHdr, fboAoRaw, fboAo, fboRefl, fboOut, fboAccum[0], fboAccum[1]};
    for (GLuint f : fbos)
        if (f) glDeleteFramebuffers(1, &f);
    *this = GlTargets();
}

bool GlRenderer::buildProgram(Program& p, int vs, int fs, std::string& error) {
    GLuint v = compile(GL_VERTEX_SHADER, glslVertex((GlVertexShader)vs), error);
    if (!v) return false;
    GLuint f = compile(GL_FRAGMENT_SHADER, glslFragment((GlFragmentShader)fs), error);
    if (!f) {
        glDeleteShader(v);
        return false;
    }
    p.id = glCreateProgram();
    glAttachShader(p.id, v);
    glAttachShader(p.id, f);
    glLinkProgram(p.id);
    glDeleteShader(v);
    glDeleteShader(f);
    GLint ok = 0;
    glGetProgramiv(p.id, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[4096];
        glGetProgramInfoLog(p.id, sizeof(log), nullptr, log);
        error = std::string("Shader program failed to link:\n") + log;
        return false;
    }
    const char* blocks[] = {"FrameCB", "MaterialCB", "BakeCB", "PartsCB"};
    for (GLuint b = 0; b < 4; ++b) {
        GLuint idx = glGetUniformBlockIndex(p.id, blocks[b]);
        if (idx != GL_INVALID_INDEX) glUniformBlockBinding(p.id, idx, b);
    }
    glUseProgram(p.id);
    for (int u = 0; u < 12; ++u) {
        GLint loc = glGetUniformLocation(p.id, kSamplerNames[u]);
        if (loc >= 0) glUniform1i(loc, u);
    }
    return true;
}

bool GlRenderer::init(std::string& error) {
    const char* version = (const char*)glGetString(GL_VERSION);
    const char* renderer = (const char*)glGetString(GL_RENDERER);
    description_ = std::string(renderer ? renderer : "?") + " (" + (version ? version : "?") + ")";
    GLint major = 0, minor = 0;
    glGetIntegerv(GL_MAJOR_VERSION, &major);
    glGetIntegerv(GL_MINOR_VERSION, &minor);
    bool es32 = major > 3 || (major == 3 && minor >= 2);
    if (!es32 && !hasExtension("GL_EXT_color_buffer_half_float") && !hasExtension("GL_EXT_color_buffer_float")) {
        error = "This GPU cannot render to half-float textures (needs OpenGL ES 3.2 or EXT_color_buffer_half_float).";
        return false;
    }
    GLint maxTex = 2048;
    glGetIntegerv(GL_MAX_TEXTURE_SIZE, &maxTex);
    shadowSize_ = std::min(2048, (int)maxTex);
    ctx_.shadowMapSize = shadowSize_;

    struct {
        Program* p;
        GlVertexShader vs;
        GlFragmentShader fs;
    } programs[] = {
        {&pMesh_, GlVertexShader::Mesh, GlFragmentShader::Model},
        {&pMeshDepth_, GlVertexShader::Mesh, GlFragmentShader::Empty},
        {&pShadow_, GlVertexShader::Shadow, GlFragmentShader::Empty},
        {&pGround_, GlVertexShader::Ground, GlFragmentShader::Ground},
        {&pGroundDepth_, GlVertexShader::Ground, GlFragmentShader::Empty},
        {&pSsao_, GlVertexShader::Fullscreen, GlFragmentShader::Ssao},
        {&pAoBlur_, GlVertexShader::Fullscreen, GlFragmentShader::AoBlur},
        {&pTonemap_, GlVertexShader::Fullscreen, GlFragmentShader::TonemapAccumulate},
        {&pResolve_, GlVertexShader::Fullscreen, GlFragmentShader::Resolve},
        {&pBlit_, GlVertexShader::Fullscreen, GlFragmentShader::Blit},
        {&pEquirect_, GlVertexShader::Fullscreen, GlFragmentShader::EquirectToCube},
        {&pPrefilter_, GlVertexShader::Fullscreen, GlFragmentShader::Prefilter},
        {&pBrdf_, GlVertexShader::Fullscreen, GlFragmentShader::BrdfLut},
    };
    for (auto& pr : programs)
        if (!buildProgram(*pr.p, (int)pr.vs, (int)pr.fs, error)) return false;

    GLuint* ubos[] = {&uboFrame_, &uboMaterial_, &uboBake_, &uboParts_};
    GLsizeiptr sizes[] = {(GLsizeiptr)sizeof(FrameConstants), (GLsizeiptr)sizeof(MaterialConstants), 16,
                          (GLsizeiptr)(16 * kMaxParts)};
    for (int i = 0; i < 4; ++i) {
        glGenBuffers(1, ubos[i]);
        glBindBuffer(GL_UNIFORM_BUFFER, *ubos[i]);
        std::vector<uint8_t> zero((size_t)sizes[i], 0);
        glBufferData(GL_UNIFORM_BUFFER, sizes[i], zero.data(), GL_DYNAMIC_DRAW);
        glBindBufferBase(GL_UNIFORM_BUFFER, (GLuint)i, *ubos[i]);
    }

    // Samplers, one per unit, matching the D3D sampler states.
    glGenSamplers(12, samplers_);
    for (int u = 0; u < 12; ++u) {
        GLuint s = samplers_[u];
        GLenum wrap = GL_CLAMP_TO_EDGE;
        GLenum minF = GL_NEAREST, magF = GL_NEAREST;
        if (u == U_SHADOW) {
            minF = magF = GL_LINEAR;
            glSamplerParameteri(s, GL_TEXTURE_COMPARE_MODE, GL_COMPARE_REF_TO_TEXTURE);
            glSamplerParameteri(s, GL_TEXTURE_COMPARE_FUNC, GL_LEQUAL);
        } else if (u == U_SPEC || u == U_ENV || u == U_REFL || u == U_EQUIRECT || u == U_USER) {
            minF = GL_LINEAR_MIPMAP_LINEAR;
            magF = GL_LINEAR;
        } else if (u == U_BRDF) {
            minF = magF = GL_LINEAR;
        }
        if (u == U_USER) wrap = GL_REPEAT;
        glSamplerParameteri(s, GL_TEXTURE_MIN_FILTER, (GLint)minF);
        glSamplerParameteri(s, GL_TEXTURE_MAG_FILTER, (GLint)magF);
        glSamplerParameteri(s, GL_TEXTURE_WRAP_S, (GLint)(u == U_EQUIRECT ? GL_REPEAT : wrap));
        glSamplerParameteri(s, GL_TEXTURE_WRAP_T, (GLint)wrap);
        glSamplerParameteri(s, GL_TEXTURE_WRAP_R, (GLint)wrap);
        glBindSampler((GLuint)u, s);
    }
    glGenVertexArrays(1, &emptyVao_);

    // Shadow map.
    shadowTex_ = makeTexture2D(GL_DEPTH_COMPONENT24, shadowSize_, shadowSize_);
    shadowFbo_ = makeFbo(0, shadowTex_);
    glGenFramebuffers(1, &bakeFbo_);

    // 1x1 white fallbacks for unbound slots.
    {
        const uint8_t white[4] = {255, 255, 255, 255};
        whiteTex_ = makeTexture2D(GL_RGBA8, 1, 1);
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, white);
    }
    if (!createBrdfLut(error)) return false;

    EnvironmentImage grey;
    grey.width = 64;
    grey.height = 32;
    grey.rgba.assign(64 * 32 * 4, 0.5f);
    analyzeEnvironment(grey);
    return setEnvironment(grey, error);
}

void GlRenderer::forgetContext() {
    GlRenderer fresh;
    *this = fresh;
}

void GlRenderer::shutdown() {
    Program* ps[] = {&pMesh_, &pMeshDepth_, &pShadow_, &pGround_, &pGroundDepth_, &pSsao_, &pAoBlur_,
                     &pTonemap_, &pResolve_, &pBlit_, &pEquirect_, &pPrefilter_, &pBrdf_};
    for (Program* p : ps)
        if (p->id) glDeleteProgram(p->id);
    GLuint bufs[] = {uboFrame_, uboMaterial_, uboBake_, uboParts_, vbo_, ibo_};
    for (GLuint b : bufs)
        if (b) glDeleteBuffers(1, &b);
    if (samplers_[0]) glDeleteSamplers(12, samplers_);
    GLuint texs[] = {shadowTex_, equirectTex_, envCubeTex_, specCubeTex_, brdfTex_, userTex_, whiteTex_};
    for (GLuint t : texs)
        if (t) glDeleteTextures(1, &t);
    if (shadowFbo_) glDeleteFramebuffers(1, &shadowFbo_);
    if (bakeFbo_) glDeleteFramebuffers(1, &bakeFbo_);
    if (emptyVao_) glDeleteVertexArrays(1, &emptyVao_);
    if (meshVao_) glDeleteVertexArrays(1, &meshVao_);
    forgetContext();
}

void GlRenderer::bindTexture(int unit, GLenum target, GLuint tex) {
    glActiveTexture(GL_TEXTURE0 + (GLenum)unit);
    glBindTexture(target, tex);
}

void GlRenderer::uploadFrame(const FrameConstants& f) {
    glBindBuffer(GL_UNIFORM_BUFFER, uboFrame_);
    glBufferSubData(GL_UNIFORM_BUFFER, 0, sizeof(f), &f);
    lastFrame_ = f;
}

void GlRenderer::fullscreen() {
    glBindVertexArray(emptyVao_);
    glDrawArrays(GL_TRIANGLES, 0, 3);
}

bool GlRenderer::createBrdfLut(std::string&) {
    const int N = 128;
    brdfTex_ = makeTexture2D(GL_RG16F, N, N);
    glBindFramebuffer(GL_FRAMEBUFFER, bakeFbo_);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, brdfTex_, 0);
    FrameConstants f = FrameConstants();
    set4(f.screen, (float)N, (float)N, 1.0f / N, 1.0f / N);
    uploadFrame(f);
    glViewport(0, 0, N, N);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_BLEND);
    glDisable(GL_CULL_FACE);
    glUseProgram(pBrdf_.id);
    fullscreen();
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    return true;
}

bool GlRenderer::setMesh(const Mesh& mesh, std::string& error) {
    clearMesh();
    ctx_.parts.clear();
    if (mesh.indices.empty()) return true;
    glGenVertexArrays(1, &meshVao_);
    glBindVertexArray(meshVao_);
    glGenBuffers(1, &vbo_);
    glBindBuffer(GL_ARRAY_BUFFER, vbo_);
    glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)(mesh.vertices.size() * sizeof(Vertex)), mesh.vertices.data(), GL_STATIC_DRAW);
    glGenBuffers(1, &ibo_);
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ibo_);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, (GLsizeiptr)(mesh.indices.size() * 4), mesh.indices.data(), GL_STATIC_DRAW);
    if (glGetError() == GL_OUT_OF_MEMORY) {
        error = "Not enough graphics memory for this model.";
        clearMesh();
        return false;
    }
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, sizeof(Vertex), (void*)0);
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, sizeof(Vertex), (void*)12);
    glEnableVertexAttribArray(2);
    glVertexAttribPointer(2, 4, GL_UNSIGNED_BYTE, GL_TRUE, sizeof(Vertex), (void*)24);
    glEnableVertexAttribArray(3);
    glVertexAttribIPointer(3, 1, GL_UNSIGNED_INT, sizeof(Vertex), (void*)28);
    glBindVertexArray(0);
    indexCount_ = (uint32_t)mesh.indices.size();
    ctx_.hasMesh = true;
    ctx_.sphereCenter = mesh.sphereCenter;
    ctx_.sphereRadius = mesh.sphereRadius;
    ctx_.minZ = mesh.boundsMin.z;
    ctx_.maxZ = mesh.boundsMax.z;
    for (const MeshPart& p : mesh.parts) ctx_.parts.push_back({p.center, p.radius, p.minZ, p.order});
    return true;
}

void GlRenderer::clearMesh() {
    if (vbo_) glDeleteBuffers(1, &vbo_);
    if (ibo_) glDeleteBuffers(1, &ibo_);
    if (meshVao_) glDeleteVertexArrays(1, &meshVao_);
    vbo_ = ibo_ = meshVao_ = 0;
    indexCount_ = 0;
    ctx_.hasMesh = false;
}

void GlRenderer::drawMesh() {
    if (!indexCount_) return;
    glBindVertexArray(meshVao_);
    glDrawElements(GL_TRIANGLES, (GLsizei)indexCount_, GL_UNSIGNED_INT, nullptr);
}

bool GlRenderer::setEnvironment(const EnvironmentImage& env, std::string& error) {
    // 1. Equirect with a CPU-built mip chain (half-float mip generation is not
    //    guaranteed on every GLES driver).
    int levels = 1;
    for (int w = env.width, h = env.height; w > 1 || h > 1; w = std::max(1, w / 2), h = std::max(1, h / 2)) ++levels;
    GLuint eq = makeTexture2D(GL_RGBA16F, env.width, env.height, levels);
    {
        std::vector<float> cur = env.rgba;
        int w = env.width, h = env.height;
        std::vector<uint16_t> half;
        for (int l = 0; l < levels; ++l) {
            half.resize(cur.size());
            for (size_t i = 0; i < cur.size(); ++i) half[i] = floatToHalf(cur[i]);
            glTexSubImage2D(GL_TEXTURE_2D, l, 0, 0, w, h, GL_RGBA, GL_HALF_FLOAT, half.data());
            int nw = std::max(1, w / 2), nh = std::max(1, h / 2);
            std::vector<float> next((size_t)nw * nh * 4);
            for (int y = 0; y < nh; ++y)
                for (int x = 0; x < nw; ++x)
                    for (int c = 0; c < 4; ++c) {
                        auto at = [&](int xx, int yy) {
                            return cur[((size_t)std::min(yy, h - 1) * w + std::min(xx, w - 1)) * 4 + c];
                        };
                        next[((size_t)y * nw + x) * 4 + c] =
                            0.25f * (at(2 * x, 2 * y) + at(2 * x + 1, 2 * y) + at(2 * x, 2 * y + 1) + at(2 * x + 1, 2 * y + 1));
                    }
            cur.swap(next);
            w = nw;
            h = nh;
        }
    }

    const int cubeSize = 512, cubeMips = 7, specSize = 256, specMips = 6;
    GLuint cube, spec;
    glGenTextures(1, &cube);
    glBindTexture(GL_TEXTURE_CUBE_MAP, cube);
    glTexStorage2D(GL_TEXTURE_CUBE_MAP, cubeMips, GL_RGBA16F, cubeSize, cubeSize);
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_MAX_LEVEL, cubeMips - 1);
    glGenTextures(1, &spec);
    glBindTexture(GL_TEXTURE_CUBE_MAP, spec);
    glTexStorage2D(GL_TEXTURE_CUBE_MAP, specMips, GL_RGBA16F, specSize, specSize);
    glTexParameteri(GL_TEXTURE_CUBE_MAP, GL_TEXTURE_MAX_LEVEL, specMips - 1);

    glBindFramebuffer(GL_FRAMEBUFFER, bakeFbo_);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_BLEND);
    glDisable(GL_CULL_FACE);
    auto bake = [&](float face, float roughness, float lodOrRes, int size) {
        float b[4] = {face, roughness, lodOrRes, 0};
        glBindBuffer(GL_UNIFORM_BUFFER, uboBake_);
        glBufferSubData(GL_UNIFORM_BUFFER, 0, 16, b);
        FrameConstants f = FrameConstants();
        set4(f.screen, (float)size, (float)size, 1.0f / size, 1.0f / size);
        uploadFrame(f);
    };

    // 2. Cube mips straight from the matching equirect mip.
    glUseProgram(pEquirect_.id);
    bindTexture(U_EQUIRECT, GL_TEXTURE_2D, eq);
    for (int mip = 0; mip < cubeMips; ++mip) {
        int size = std::max(1, cubeSize >> mip);
        float lod = std::max(0.0f, std::log2((float)env.width / (4.0f * size)));
        glViewport(0, 0, size, size);
        for (int face = 0; face < 6; ++face) {
            glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_CUBE_MAP_POSITIVE_X + (GLenum)face, cube, mip);
            bake((float)face, 0, lod, size);
            fullscreen();
        }
    }
    // 3. GGX-prefiltered specular cube.
    glUseProgram(pPrefilter_.id);
    bindTexture(U_ENV, GL_TEXTURE_CUBE_MAP, cube);
    for (int mip = 0; mip < specMips; ++mip) {
        int size = specSize >> mip;
        glViewport(0, 0, size, size);
        for (int face = 0; face < 6; ++face) {
            glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_CUBE_MAP_POSITIVE_X + (GLenum)face, spec, mip);
            bake((float)face, (float)mip / (float)(specMips - 1), (float)cubeSize, size);
            fullscreen();
            glFlush();
        }
    }
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 0, 0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    if (glGetError() == GL_OUT_OF_MEMORY) {
        error = "Not enough graphics memory for the environment.";
        return false;
    }

    GLuint old[] = {equirectTex_, envCubeTex_, specCubeTex_};
    for (GLuint t : old)
        if (t) glDeleteTextures(1, &t);
    equirectTex_ = eq;
    envCubeTex_ = cube;
    specCubeTex_ = spec;
    ctx_.specMips = specMips;
    ctx_.envMips = cubeMips;
    for (int i = 0; i < 9; ++i) ctx_.sh[i] = env.sh[i];
    ctx_.envDominantDir = env.dominantDirection;
    ctx_.envDominantColor = env.dominantColor;
    ctx_.envDirectionality = env.directionality;
    return true;
}

bool GlRenderer::setUserTexture(const Image8* image, std::string&) {
    if (userTex_) glDeleteTextures(1, &userTex_);
    userTex_ = 0;
    ctx_.hasUserTexture = false;
    if (!image || image->width <= 0) return true;
    int levels = 1;
    for (int w = image->width, h = image->height; w > 1 || h > 1; w = std::max(1, w / 2), h = std::max(1, h / 2)) ++levels;
    userTex_ = makeTexture2D(GL_SRGB8_ALPHA8, image->width, image->height, levels);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, image->width, image->height, GL_RGBA, GL_UNSIGNED_BYTE, image->rgba.data());
    glGenerateMipmap(GL_TEXTURE_2D);
    ctx_.hasUserTexture = true;
    return true;
}

bool GlRenderer::ensureTargets(GlTargets& rt, int width, int height, std::string& error) {
    width = std::max(1, width);
    height = std::max(1, height);
    if (rt.width == width && rt.height == height && rt.outTex) return true;
    rt.release();
    rt.hdrTex = makeTexture2D(GL_RGBA16F, width, height);
    rt.depthTex = makeTexture2D(GL_DEPTH_COMPONENT24, width, height);
    rt.aoRawTex = makeTexture2D(GL_R8, width, height);
    rt.aoTex = makeTexture2D(GL_R8, width, height);
    rt.accumTex[0] = makeTexture2D(GL_RGBA16F, width, height);
    rt.accumTex[1] = makeTexture2D(GL_RGBA16F, width, height);
    rt.outTex = makeTexture2D(GL_RGBA8, width, height);
    rt.fboDepth = makeFbo(0, rt.depthTex);
    rt.fboHdr = makeFbo(rt.hdrTex, rt.depthTex);
    rt.fboAoRaw = makeFbo(rt.aoRawTex, 0);
    rt.fboAo = makeFbo(rt.aoTex, 0);
    rt.fboAccum[0] = makeFbo(rt.accumTex[0], 0);
    rt.fboAccum[1] = makeFbo(rt.accumTex[1], 0);
    rt.fboOut = makeFbo(rt.outTex, 0);
    GLenum status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
    glBindFramebuffer(GL_FRAMEBUFFER, rt.fboHdr);
    GLenum hdrStatus = glCheckFramebufferStatus(GL_FRAMEBUFFER);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    if (status != GL_FRAMEBUFFER_COMPLETE || hdrStatus != GL_FRAMEBUFFER_COMPLETE || glGetError() == GL_OUT_OF_MEMORY) {
        rt.release();
        error = "Could not create render targets (out of graphics memory?)";
        return false;
    }
    rt.width = width;
    rt.height = height;
    return true;
}

void GlRenderer::renderSample(GlTargets& rt, const Scene& s, const SampleInput& in) {
    FramePlan plan = planFrame(ctx_, s, in, rt.width, rt.height);
    const bool mesh = indexCount_ > 0;
    FrameConstants f = plan.frame;
    // Convert every projection to GL clip space.
    f.proj = toGlClip(f.proj);
    f.viewProj = toGlClip(f.viewProj);
    f.projInv = f.proj.inverse();
    f.invViewProj = f.viewProj.inverse();
    f.lightViewProj = toGlClip(f.lightViewProj);
    // Running average: this sample's weight in the accumulation.
    f.tone[2] = 1.0f / (float)(in.sampleIndex + 1);

    {
        std::vector<float> parts(4 * kMaxParts, 0.0f);
        for (size_t i = 0; i < plan.partOffsets.size() && i < kMaxParts; ++i) set4(&parts[i * 4], plan.partOffsets[i], 0);
        glBindBuffer(GL_UNIFORM_BUFFER, uboParts_);
        glBufferSubData(GL_UNIFORM_BUFFER, 0, (GLsizeiptr)(parts.size() * 4), parts.data());
        glBindBuffer(GL_UNIFORM_BUFFER, uboMaterial_);
        glBufferSubData(GL_UNIFORM_BUFFER, 0, sizeof(plan.material), &plan.material);
    }
    uploadFrame(f);
    glDisable(GL_CULL_FACE);
    glDisable(GL_BLEND);
    glDisable(GL_SCISSOR_TEST);

    // 1. Shadow map.
    if (mesh) {
        glBindFramebuffer(GL_FRAMEBUFFER, shadowFbo_);
        glViewport(0, 0, shadowSize_, shadowSize_);
        glEnable(GL_DEPTH_TEST);
        glDepthMask(GL_TRUE);
        glDepthFunc(GL_LESS);
        glClearDepthf(1.0f);
        glClear(GL_DEPTH_BUFFER_BIT);
        glEnable(GL_POLYGON_OFFSET_FILL);
        glPolygonOffset(1.5f, 2.0f);
        glUseProgram(pShadow_.id);
        drawMesh();
        glDisable(GL_POLYGON_OFFSET_FILL);
    }

    // 2. Depth pre-pass (model + ground).
    glBindFramebuffer(GL_FRAMEBUFFER, rt.fboDepth);
    glViewport(0, 0, rt.width, rt.height);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_TRUE);
    glDepthFunc(GL_LESS);
    glClear(GL_DEPTH_BUFFER_BIT);
    if (mesh) {
        glUseProgram(pMeshDepth_.id);
        drawMesh();
    }
    if (plan.groundEnabled) {
        glUseProgram(pGroundDepth_.id);
        glBindVertexArray(emptyVao_);
        glDrawArrays(GL_TRIANGLES, 0, 6);
    }

    // 3. Ambient occlusion.
    glDisable(GL_DEPTH_TEST);
    if (plan.aoEnabled) {
        bindTexture(U_DEPTH, GL_TEXTURE_2D, rt.depthTex);
        glBindFramebuffer(GL_FRAMEBUFFER, rt.fboAoRaw);
        glUseProgram(pSsao_.id);
        fullscreen();
        glBindFramebuffer(GL_FRAMEBUFFER, rt.fboAo);
        bindTexture(U_AORAW, GL_TEXTURE_2D, rt.aoRawTex);
        glUseProgram(pAoBlur_.id);
        fullscreen();
        // The depth texture is attached to the HDR target below: never leave it bound too.
        bindTexture(U_DEPTH, GL_TEXTURE_2D, 0);
        bindTexture(U_AORAW, GL_TEXTURE_2D, 0);
    }

    bindTexture(U_SHADOW, GL_TEXTURE_2D, shadowTex_);
    bindTexture(U_SPEC, GL_TEXTURE_CUBE_MAP, specCubeTex_);
    bindTexture(U_BRDF, GL_TEXTURE_2D, brdfTex_);
    bindTexture(U_AO, GL_TEXTURE_2D, plan.aoEnabled ? rt.aoTex : whiteTex_);
    bindTexture(U_REFL, GL_TEXTURE_2D, whiteTex_);
    bindTexture(U_USER, GL_TEXTURE_2D, userTex_ ? userTex_ : whiteTex_);
    bindTexture(U_EQUIRECT, GL_TEXTURE_2D, equirectTex_);

    // 4. Planar reflection (model mirrored in z = 0, shaded from the mirrored eye).
    if (plan.reflection) {
        if (!rt.reflTex) {
            rt.reflTex = makeTexture2D(GL_RGBA16F, rt.width, rt.height, 6);
            glGenRenderbuffers(1, &rt.reflDepth);
            glBindRenderbuffer(GL_RENDERBUFFER, rt.reflDepth);
            glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH_COMPONENT24, rt.width, rt.height);
            rt.fboRefl = makeFbo(rt.reflTex, 0, rt.reflDepth);
        }
        FrameConstants rf = f;
        rf.mirror = mat4::scale(vec3(1, 1, -1));
        rf.eye[2] = -rf.eye[2];
        rf.forward[2] = -rf.forward[2];
        rf.ao[2] = 0;
        rf.pass[0] = 1;
        uploadFrame(rf);
        bindTexture(U_AO, GL_TEXTURE_2D, whiteTex_);
        glBindFramebuffer(GL_FRAMEBUFFER, rt.fboRefl);
        glClearColor(0, 0, 0, 0);
        glEnable(GL_DEPTH_TEST);
        glDepthMask(GL_TRUE);
        glDepthFunc(GL_LESS);
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
        glUseProgram(pMesh_.id);
        drawMesh();
        bindTexture(U_REFL, GL_TEXTURE_2D, rt.reflTex);  // leaves U_REFL active for the mip build
        glGenerateMipmap(GL_TEXTURE_2D);
        uploadFrame(f);
        bindTexture(U_AO, GL_TEXTURE_2D, plan.aoEnabled ? rt.aoTex : whiteTex_);
    }

    // 5. HDR scene: ground (premultiplied over nothing), then the model.
    glBindFramebuffer(GL_FRAMEBUFFER, rt.fboHdr);
    glViewport(0, 0, rt.width, rt.height);
    glClearColor(0, 0, 0, 0);
    glClear(GL_COLOR_BUFFER_BIT);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_FALSE);
    glDepthFunc(GL_LEQUAL);
    if (plan.groundEnabled) {
        glEnable(GL_BLEND);
        glBlendFuncSeparate(GL_ONE, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
        glUseProgram(pGround_.id);
        glBindVertexArray(emptyVao_);
        glDrawArrays(GL_TRIANGLES, 0, 6);
        glDisable(GL_BLEND);
    }
    if (mesh) {
        glUseProgram(pMesh_.id);
        drawMesh();
    }
    glDepthMask(GL_TRUE);
    glDisable(GL_DEPTH_TEST);

    // 6. Tonemap over the background and fold into the running average (ping-pong).
    int prev = rt.accumCurrent, next = 1 - rt.accumCurrent;
    if (in.sampleIndex == 0) {
        glBindFramebuffer(GL_FRAMEBUFFER, rt.fboAccum[prev]);
        glClearColor(0, 0, 0, 0);
        glClear(GL_COLOR_BUFFER_BIT);
    }
    glBindFramebuffer(GL_FRAMEBUFFER, rt.fboAccum[next]);
    bindTexture(U_SRC, GL_TEXTURE_2D, rt.hdrTex);
    bindTexture(U_ACCUM, GL_TEXTURE_2D, rt.accumTex[prev]);
    glUseProgram(pTonemap_.id);
    fullscreen();
    rt.accumCurrent = next;
    bindTexture(U_SRC, GL_TEXTURE_2D, 0);
    bindTexture(U_ACCUM, GL_TEXTURE_2D, 0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

void GlRenderer::resolve(GlTargets& rt, bool overBlack) {
    FrameConstants f = lastFrame_;
    f.tone[3] = overBlack ? 1.0f : 0.0f;
    set4(f.screen, (float)rt.width, (float)rt.height, 1.0f / rt.width, 1.0f / rt.height);
    uploadFrame(f);
    glBindFramebuffer(GL_FRAMEBUFFER, rt.fboOut);
    glViewport(0, 0, rt.width, rt.height);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_BLEND);
    bindTexture(U_ACCUM, GL_TEXTURE_2D, rt.accumTex[rt.accumCurrent]);
    glUseProgram(pResolve_.id);
    fullscreen();
    bindTexture(U_ACCUM, GL_TEXTURE_2D, 0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
}

void GlRenderer::blit(GlTargets& rt, GLuint fbo, int x, int y, int w, int h, bool checkerboard) {
    FrameConstants f = lastFrame_;
    f.pass[1] = checkerboard ? 1.0f : 0.0f;
    uploadFrame(f);
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glViewport(x, y, w, h);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_BLEND);
    bindTexture(U_SRC, GL_TEXTURE_2D, rt.outTex);
    glBindSampler(U_SRC, samplers_[U_BRDF]);  // linear: the target may be scaled
    glUseProgram(pBlit_.id);
    fullscreen();
    glBindSampler(U_SRC, samplers_[U_SRC]);
    bindTexture(U_SRC, GL_TEXTURE_2D, 0);
}

void GlRenderer::readPixels(GlTargets& rt, std::vector<uint8_t>& rgba) {
    const size_t row = (size_t)rt.width * 4;
    std::vector<uint8_t> raw(row * rt.height);
    glBindFramebuffer(GL_FRAMEBUFFER, rt.fboOut);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glReadPixels(0, 0, rt.width, rt.height, GL_RGBA, GL_UNSIGNED_BYTE, raw.data());
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    rgba.resize(raw.size());
    for (int y = 0; y < rt.height; ++y)  // GL rows are bottom-up
        std::memcpy(&rgba[row * y], &raw[row * (rt.height - 1 - y)], row);
}

}  // namespace spindle
