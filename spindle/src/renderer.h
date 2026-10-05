// Direct3D 11 renderer: PBR + IBL rasteriser with accumulation-based anti-aliasing,
// soft shadows, depth of field and motion blur (see shaders.hlsl for the passes).
#pragma once

#include "camera.h"
#include "environment.h"
#include "frame_plan.h"
#include "gpu.h"
#include "mesh.h"
#include "sampling.h"
#include "scene.h"

#include <memory>

namespace spindle {

// Per-size render targets. The viewport and an export each own one set.
struct RenderTargets {
    int width = 0, height = 0;
    ComPtr<ID3D11Texture2D> hdrTex, depthTex, aoRawTex, aoTex, reflTex, reflDepthTex, accumTex, outTex;
    ComPtr<ID3D11RenderTargetView> hdrRTV, aoRawRTV, aoRTV, reflRTV, accumRTV, outRTV;
    ComPtr<ID3D11ShaderResourceView> hdrSRV, depthSRV, aoRawSRV, aoSRV, reflSRV, accumSRV, outSRV;
    ComPtr<ID3D11DepthStencilView> depthDSV, reflDSV;
    void release() { *this = RenderTargets(); }
};


class Renderer {
public:
    Renderer();
    ~Renderer();
    Renderer(const Renderer&) = delete;
    Renderer& operator=(const Renderer&) = delete;

    bool init(ID3D11Device* device, ID3D11DeviceContext* context, size_t vramMB, std::string& error);
    void shutdown();

    bool setMesh(const Mesh& mesh, std::string& error);
    void clearMesh();
    bool hasMesh() const { return indexCount_ > 0; }
    vec3 sphereCenter() const { return sphereCenter_; }
    float sphereRadius() const { return sphereRadius_; }
    // Radius to frame the camera on: grows to cover the exploded assembly when
    // the scene explodes, so parts never leave the frame mid-animation.
    float framingRadius(const Scene& scene) const;
    size_t partCount() const { return parts_.size(); }

    bool setEnvironment(const EnvironmentImage& env, std::string& error);
    bool setUserTexture(const Image8* image, std::string& error);  // nullptr clears

    bool ensureTargets(RenderTargets& rt, int width, int height, std::string& error);
    void renderSample(RenderTargets& rt, const Scene& scene, const SampleInput& in);
    // Writes the average of `samples` accumulated samples into rt.outTex (RGBA8, sRGB).
    void resolve(RenderTargets& rt, int samples, bool overBlack);
    // Draws rt.outTex into the given render target region.
    void blit(RenderTargets& rt, ID3D11RenderTargetView* target, const D3D11_VIEWPORT& vp, bool checkerboard);

    ID3D11Device* device() const { return device_; }
    ID3D11DeviceContext* context() const { return ctx_; }

private:
    std::unique_ptr<FrameConstants> frame_;  // last uploaded frame constants
    RenderContext renderContext() const;

    bool createShaders(std::string& error);
    bool createStates(std::string& error);
    bool createBrdfLut(std::string& error);
    void updateFrame(const FrameConstants& f);
    void drawMesh();
    void bindCommon();
    void unbindAll();

    ID3D11Device* device_ = nullptr;
    ID3D11DeviceContext* ctx_ = nullptr;

    ComPtr<ID3D11VertexShader> vsMesh_, vsShadow_, vsGround_, vsFullscreen_;
    ComPtr<ID3D11GeometryShader> gsWire_;
    ComPtr<ID3D11PixelShader> psModel_, psModelWire_, psGround_, psSSAO_, psAOBlur_, psTonemapAccum_, psResolve_, psBlit_,
        psEquirectToCube_, psPrefilter_, psBrdfLut_;
    ComPtr<ID3D11InputLayout> meshLayout_;

    ComPtr<ID3D11RasterizerState> rsNoCull_, rsShadow_;
    ComPtr<ID3D11DepthStencilState> dsLessWrite_, dsLessEqualNoWrite_, dsNone_;
    ComPtr<ID3D11BlendState> bsPremul_, bsAdditive_;
    ComPtr<ID3D11SamplerState> sShadow_, sLinearClamp_, sLinearWrap_, sPointClamp_, sEquirect_;
    ComPtr<ID3D11Buffer> cbFrame_, cbMaterial_, cbBake_, cbParts_;
    std::vector<ExplodePart> parts_;
    float curRadius_ = 1;  // scene radius for the sample being rendered (shadows, floor)

    ComPtr<ID3D11Buffer> vb_, ib_;
    uint32_t indexCount_ = 0;
    vec3 sphereCenter_ = {0, 0, 0};
    float sphereRadius_ = 1;
    float meshMinZ_ = 0, meshMaxZ_ = 1;

    ComPtr<ID3D11Texture2D> equirectTex_, envCubeTex_, specCubeTex_, brdfTex_, shadowTex_, userTex_, whiteTex_;
    ComPtr<ID3D11ShaderResourceView> equirectSRV_, envCubeSRV_, specCubeSRV_, brdfSRV_, shadowSRV_, userSRV_, whiteSRV_;
    ComPtr<ID3D11DepthStencilView> shadowDSV_;
    int shadowSize_ = 4096;
    int envMips_ = 1, specMips_ = 1;
    vec3 sh_[9];
    vec3 envDominantDir_ = {0.5f, 0.5f, 0.7f};
    vec3 envDominantColor_ = {1, 1, 1};
    float envDirectionality_ = 0.5f;
    bool hasEnv_ = false;
    DXGI_FORMAT accumFormat_ = DXGI_FORMAT_R32G32B32A32_FLOAT;
};


}  // namespace spindle
