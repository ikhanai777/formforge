// OpenGL ES 3.0 renderer: the same passes as the Direct3D 11 renderer
// (renderer.cpp), used by the Android app. Runs on any GLES 3.0 context with
// half-float render targets (EXT_color_buffer_half_float / _float, or ES 3.2).
#pragma once

#include "../environment.h"
#include "../frame_plan.h"
#include "../mesh.h"
#include "../sampling.h"
#include "../scene.h"

#include <GLES3/gl3.h>

#include <string>
#include <vector>

namespace spindle {

// Per-size render targets. The viewport and an export each own one set.
struct GlTargets {
    int width = 0, height = 0;
    GLuint hdrTex = 0, depthTex = 0, aoRawTex = 0, aoTex = 0, reflTex = 0, reflDepth = 0, outTex = 0;
    GLuint accumTex[2] = {0, 0};
    GLuint fboDepth = 0, fboHdr = 0, fboAoRaw = 0, fboAo = 0, fboRefl = 0, fboOut = 0;
    GLuint fboAccum[2] = {0, 0};
    int accumCurrent = 0;  // accumTex[accumCurrent] holds the running average
    void release();
};

class GlRenderer {
public:
    bool init(std::string& error);
    void shutdown();  // call with the context current
    // The context was lost (Android pauses): forget GL names without deleting them.
    void forgetContext();
    const std::string& deviceDescription() const { return description_; }

    bool setMesh(const Mesh& mesh, std::string& error);
    void clearMesh();
    bool hasMesh() const { return indexCount_ > 0; }
    vec3 sphereCenter() const { return ctx_.sphereCenter; }
    float sphereRadius() const { return ctx_.sphereRadius; }
    float framingRadius(const Scene& scene) const { return framingRadiusFor(ctx_, scene); }
    size_t partCount() const { return ctx_.parts.size(); }

    bool setEnvironment(const EnvironmentImage& env, std::string& error);
    bool setUserTexture(const Image8* image, std::string& error);

    bool ensureTargets(GlTargets& rt, int width, int height, std::string& error);
    void renderSample(GlTargets& rt, const Scene& scene, const SampleInput& in);
    // Writes the running average into rt.outTex (RGBA8, sRGB-encoded; straight
    // alpha, or composited over black when overBlack).
    void resolve(GlTargets& rt, bool overBlack);
    // Draws rt.outTex into framebuffer `fbo` (0 = the window) at the given rectangle.
    void blit(GlTargets& rt, GLuint fbo, int x, int y, int w, int h, bool checkerboard);
    // Reads rt.outTex back as top-down RGBA8 rows.
    void readPixels(GlTargets& rt, std::vector<uint8_t>& rgba);

private:
    struct Program {
        GLuint id = 0;
    };
    bool buildProgram(Program& p, int vs, int fs, std::string& error);
    void uploadFrame(const FrameConstants& f);
    void drawMesh();
    void fullscreen();
    void bindTexture(int unit, GLenum target, GLuint tex);
    bool createBrdfLut(std::string& error);

    std::string description_;
    Program pMesh_, pMeshDepth_, pShadow_, pGround_, pGroundDepth_, pSsao_, pAoBlur_, pTonemap_, pResolve_, pBlit_,
        pEquirect_, pPrefilter_, pBrdf_;
    GLuint uboFrame_ = 0, uboMaterial_ = 0, uboBake_ = 0, uboParts_ = 0;
    GLuint samplers_[12] = {};
    GLuint emptyVao_ = 0, meshVao_ = 0, vbo_ = 0, ibo_ = 0;
    uint32_t indexCount_ = 0;
    GLuint shadowTex_ = 0, shadowFbo_ = 0, bakeFbo_ = 0;
    GLuint equirectTex_ = 0, envCubeTex_ = 0, specCubeTex_ = 0, brdfTex_ = 0, userTex_ = 0, whiteTex_ = 0;
    int shadowSize_ = 2048;
    RenderContext ctx_;
    FrameConstants lastFrame_;
};

}  // namespace spindle
