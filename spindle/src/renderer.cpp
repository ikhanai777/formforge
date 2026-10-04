#include "renderer.h"

#include <cstring>

namespace spindle {

// Mirrors cbuffer FrameCB in shaders.hlsl, member for member.
struct Renderer::FrameConstants {
    mat4 viewProj, view, proj, projInv, invViewProj, lightViewProj, world, mirror;
    float eye[4], forward[4], screen[4];
    float keyDir[4], keyColor[4], fillDir[4], fillColor[4], rimDir[4], rimColor[4];
    float env[4];
    float sh[9][4];
    float background[4], bgColor[4], bgColor2[4];
    float ground[4], floorColor[4];
    float ao[4], shadow[4], tone[4], model[4], pass[4];
};

// Mirrors cbuffer MaterialCB.
struct Renderer::MaterialConstants {
    float base[4], pbr[4], trans[4], transTint[4], layer[4], noise[4], pattern[4], patternColor[4], gradient[4],
        gradientRange[4], wire[4];
};

struct BakeConstants {
    float face[4];
};

static_assert(sizeof(mat4) == 64, "mat4 must be 16 packed floats");

static void set4(float* d, float x, float y, float z, float w) {
    d[0] = x;
    d[1] = y;
    d[2] = z;
    d[3] = w;
}
static void set4(float* d, const vec3& v, float w) { set4(d, v.x, v.y, v.z, w); }

static float srgbToLinear1(float c) {
    return c <= 0.04045f ? c / 12.92f : std::pow((c + 0.055f) / 1.055f, 2.4f);
}
vec3 srgbToLinear(const vec3& c) { return {srgbToLinear1(c.x), srgbToLinear1(c.y), srgbToLinear1(c.z)}; }

static uint16_t floatToHalf(float f) {
    uint32_t x;
    std::memcpy(&x, &f, 4);
    uint32_t sign = (x >> 16) & 0x8000;
    int32_t exp = (int32_t)((x >> 23) & 0xFF) - 127 + 15;
    uint32_t mant = x & 0x7FFFFF;
    if (((x >> 23) & 0xFF) == 0xFF) return (uint16_t)(sign | 0x7C00 | (mant ? 0x200 : 0));
    if (exp >= 31) return (uint16_t)(sign | 0x7BFF);  // clamp to the largest finite half
    if (exp <= 0) {
        if (exp < -10) return (uint16_t)sign;
        mant |= 0x800000;
        uint32_t shift = (uint32_t)(14 - exp);
        uint32_t h = mant >> shift;
        if ((mant >> (shift - 1)) & 1) ++h;
        return (uint16_t)(sign | h);
    }
    uint32_t h = sign | ((uint32_t)exp << 10) | (mant >> 13);
    if (mant & 0x1000) ++h;  // round to nearest
    return (uint16_t)h;
}

static vec3 dirFromAzEl(float azDeg, float elDeg) {
    float az = radians(azDeg), el = radians(elDeg);
    return {std::cos(el) * std::cos(az), std::cos(el) * std::sin(az), std::sin(el)};
}

Renderer::Renderer() : frame_(new FrameConstants()) {}
Renderer::~Renderer() { shutdown(); }

#define TRY(expr, what)                                  \
    do {                                                 \
        HRESULT hr_ = (expr);                            \
        if (FAILED(hr_)) {                               \
            char b_[160];                                \
            std::snprintf(b_, sizeof(b_), "%s failed (hr=0x%08lx)", what, (unsigned long)hr_); \
            error = b_;                                  \
            return false;                                \
        }                                                \
    } while (0)

bool Renderer::init(ID3D11Device* device, ID3D11DeviceContext* context, size_t vramMB, std::string& error) {
    device_ = device;
    ctx_ = context;
    shadowSize_ = vramMB >= 1500 ? 4096 : 2048;

    UINT support = 0;
    if (FAILED(device_->CheckFormatSupport(DXGI_FORMAT_R32G32B32A32_FLOAT, &support)) ||
        !(support & D3D11_FORMAT_SUPPORT_BLENDABLE) || !(support & D3D11_FORMAT_SUPPORT_RENDER_TARGET))
        accumFormat_ = DXGI_FORMAT_R16G16B16A16_FLOAT;

    if (!createShaders(error) || !createStates(error)) return false;

    auto makeCB = [&](UINT size, ComPtr<ID3D11Buffer>& out) {
        D3D11_BUFFER_DESC d = {};
        d.ByteWidth = (size + 15) & ~15u;
        d.Usage = D3D11_USAGE_DYNAMIC;
        d.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        d.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        return device_->CreateBuffer(&d, nullptr, out.put());
    };
    TRY(makeCB(sizeof(FrameConstants), cbFrame_), "Frame constant buffer");
    TRY(makeCB(sizeof(MaterialConstants), cbMaterial_), "Material constant buffer");
    TRY(makeCB(sizeof(BakeConstants), cbBake_), "Bake constant buffer");

    // Shadow map.
    {
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = d.Height = (UINT)shadowSize_;
        d.MipLevels = 1;
        d.ArraySize = 1;
        d.Format = DXGI_FORMAT_R32_TYPELESS;
        d.SampleDesc.Count = 1;
        d.BindFlags = D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE;
        TRY(device_->CreateTexture2D(&d, nullptr, shadowTex_.put()), "Shadow map");
        D3D11_DEPTH_STENCIL_VIEW_DESC dv = {};
        dv.Format = DXGI_FORMAT_D32_FLOAT;
        dv.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2D;
        TRY(device_->CreateDepthStencilView(shadowTex_.get(), &dv, shadowDSV_.put()), "Shadow DSV");
        D3D11_SHADER_RESOURCE_VIEW_DESC sv = {};
        sv.Format = DXGI_FORMAT_R32_FLOAT;
        sv.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        sv.Texture2D.MipLevels = 1;
        TRY(device_->CreateShaderResourceView(shadowTex_.get(), &sv, shadowSRV_.put()), "Shadow SRV");
    }
    // 1x1 white texture for unbound slots.
    {
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = d.Height = 1;
        d.MipLevels = 1;
        d.ArraySize = 1;
        d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
        d.SampleDesc.Count = 1;
        d.Usage = D3D11_USAGE_IMMUTABLE;
        d.BindFlags = D3D11_BIND_SHADER_RESOURCE;
        uint32_t white = 0xFFFFFFFFu;
        D3D11_SUBRESOURCE_DATA init = {&white, 4, 4};
        TRY(device_->CreateTexture2D(&d, &init, whiteTex_.put()), "White texture");
        TRY(device_->CreateShaderResourceView(whiteTex_.get(), nullptr, whiteSRV_.put()), "White SRV");
    }
    if (!createBrdfLut(error)) return false;

    // A neutral grey environment until a real one is set.
    EnvironmentImage grey;
    grey.width = 64;
    grey.height = 32;
    grey.rgba.assign(64 * 32 * 4, 0.5f);
    analyzeEnvironment(grey);
    return setEnvironment(grey, error);
}

void Renderer::shutdown() {
    if (ctx_) {
        ctx_->ClearState();
        ctx_->Flush();
    }
    vsMesh_ = nullptr; vsShadow_ = nullptr; vsGround_ = nullptr; vsFullscreen_ = nullptr; gsWire_ = nullptr;
    psModel_ = nullptr; psModelWire_ = nullptr; psGround_ = nullptr; psSSAO_ = nullptr; psAOBlur_ = nullptr;
    psTonemapAccum_ = nullptr; psResolve_ = nullptr; psBlit_ = nullptr; psEquirectToCube_ = nullptr;
    psPrefilter_ = nullptr; psBrdfLut_ = nullptr; meshLayout_ = nullptr;
    rsNoCull_ = nullptr; rsShadow_ = nullptr; dsLessWrite_ = nullptr; dsLessEqualNoWrite_ = nullptr; dsNone_ = nullptr;
    bsPremul_ = nullptr; bsAdditive_ = nullptr;
    sShadow_ = nullptr; sLinearClamp_ = nullptr; sLinearWrap_ = nullptr; sPointClamp_ = nullptr; sEquirect_ = nullptr;
    cbFrame_ = nullptr; cbMaterial_ = nullptr; cbBake_ = nullptr;
    vb_ = nullptr; ib_ = nullptr; indexCount_ = 0;
    equirectTex_ = nullptr; envCubeTex_ = nullptr; specCubeTex_ = nullptr; brdfTex_ = nullptr; shadowTex_ = nullptr;
    userTex_ = nullptr; whiteTex_ = nullptr;
    equirectSRV_ = nullptr; envCubeSRV_ = nullptr; specCubeSRV_ = nullptr; brdfSRV_ = nullptr; shadowSRV_ = nullptr;
    userSRV_ = nullptr; whiteSRV_ = nullptr; shadowDSV_ = nullptr;
    hasEnv_ = false;
    device_ = nullptr;
    ctx_ = nullptr;
}

bool Renderer::createShaders(std::string& error) {
    using Defines = std::vector<std::pair<std::string, std::string>>;
    std::vector<char> code;
    auto vs = [&](const char* entry, ComPtr<ID3D11VertexShader>& out, ComPtr<ID3D11InputLayout>* layout) {
        if (!compileShader(entry, "vs_5_0", {}, code, error)) return false;
        if (FAILED(device_->CreateVertexShader(code.data(), code.size(), nullptr, out.put()))) {
            error = std::string("CreateVertexShader ") + entry;
            return false;
        }
        if (layout) {
            D3D11_INPUT_ELEMENT_DESC el[] = {
                {"POSITION", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 0, D3D11_INPUT_PER_VERTEX_DATA, 0},
                {"NORMAL", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 12, D3D11_INPUT_PER_VERTEX_DATA, 0},
            };
            if (FAILED(device_->CreateInputLayout(el, 2, code.data(), code.size(), layout->put()))) {
                error = "CreateInputLayout";
                return false;
            }
        }
        return true;
    };
    auto ps = [&](const char* entry, ComPtr<ID3D11PixelShader>& out, const Defines& defs = {}) {
        if (!compileShader(entry, "ps_5_0", defs, code, error)) return false;
        if (FAILED(device_->CreatePixelShader(code.data(), code.size(), nullptr, out.put()))) {
            error = std::string("CreatePixelShader ") + entry;
            return false;
        }
        return true;
    };
    if (!vs("VS_Mesh", vsMesh_, &meshLayout_)) return false;
    if (!vs("VS_Shadow", vsShadow_, nullptr)) return false;
    if (!vs("VS_Ground", vsGround_, nullptr)) return false;
    if (!vs("VS_Fullscreen", vsFullscreen_, nullptr)) return false;
    if (!compileShader("GS_Wire", "gs_5_0", {}, code, error)) return false;
    if (FAILED(device_->CreateGeometryShader(code.data(), code.size(), nullptr, gsWire_.put()))) {
        error = "CreateGeometryShader GS_Wire";
        return false;
    }
    return ps("PS_Model", psModel_) && ps("PS_Model", psModelWire_, {{"WIREFRAME", "1"}}) && ps("PS_Ground", psGround_) &&
           ps("PS_SSAO", psSSAO_) && ps("PS_AOBlur", psAOBlur_) && ps("PS_TonemapAccum", psTonemapAccum_) &&
           ps("PS_Resolve", psResolve_) && ps("PS_Blit", psBlit_) && ps("PS_EquirectToCube", psEquirectToCube_) &&
           ps("PS_Prefilter", psPrefilter_) && ps("PS_BrdfLut", psBrdfLut_);
}

bool Renderer::createStates(std::string& error) {
    D3D11_RASTERIZER_DESC r = {};
    r.FillMode = D3D11_FILL_SOLID;
    r.CullMode = D3D11_CULL_NONE;  // STL winding is unreliable; the shader orients normals itself
    r.DepthClipEnable = TRUE;
    TRY(device_->CreateRasterizerState(&r, rsNoCull_.put()), "Rasterizer state");
    r.SlopeScaledDepthBias = 1.5f;
    r.DepthBiasClamp = 0.01f;
    TRY(device_->CreateRasterizerState(&r, rsShadow_.put()), "Shadow rasterizer state");

    D3D11_DEPTH_STENCIL_DESC ds = {};
    ds.DepthEnable = TRUE;
    ds.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL;
    ds.DepthFunc = D3D11_COMPARISON_LESS;
    TRY(device_->CreateDepthStencilState(&ds, dsLessWrite_.put()), "Depth state");
    ds.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO;
    ds.DepthFunc = D3D11_COMPARISON_LESS_EQUAL;
    TRY(device_->CreateDepthStencilState(&ds, dsLessEqualNoWrite_.put()), "Depth state");
    ds.DepthEnable = FALSE;
    TRY(device_->CreateDepthStencilState(&ds, dsNone_.put()), "Depth state");

    D3D11_BLEND_DESC b = {};
    b.RenderTarget[0].BlendEnable = TRUE;
    b.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE;
    b.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
    b.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
    b.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
    b.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
    b.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
    b.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
    TRY(device_->CreateBlendState(&b, bsPremul_.put()), "Blend state");
    b.RenderTarget[0].DestBlend = D3D11_BLEND_ONE;
    b.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_ONE;
    TRY(device_->CreateBlendState(&b, bsAdditive_.put()), "Blend state");

    D3D11_SAMPLER_DESC s = {};
    s.Filter = D3D11_FILTER_COMPARISON_MIN_MAG_LINEAR_MIP_POINT;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_BORDER;
    s.BorderColor[0] = s.BorderColor[1] = s.BorderColor[2] = s.BorderColor[3] = 1.0f;
    s.ComparisonFunc = D3D11_COMPARISON_LESS_EQUAL;
    s.MaxLOD = D3D11_FLOAT32_MAX;
    TRY(device_->CreateSamplerState(&s, sShadow_.put()), "Shadow sampler");
    s = D3D11_SAMPLER_DESC();
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
    s.ComparisonFunc = D3D11_COMPARISON_NEVER;
    s.MaxLOD = D3D11_FLOAT32_MAX;
    TRY(device_->CreateSamplerState(&s, sLinearClamp_.put()), "Sampler");
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
    TRY(device_->CreateSamplerState(&s, sPointClamp_.put()), "Sampler");
    s.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
    s.AddressU = D3D11_TEXTURE_ADDRESS_WRAP;
    TRY(device_->CreateSamplerState(&s, sEquirect_.put()), "Sampler");
    s.Filter = D3D11_FILTER_ANISOTROPIC;
    s.MaxAnisotropy = 8;
    s.AddressU = s.AddressV = s.AddressW = D3D11_TEXTURE_ADDRESS_WRAP;
    TRY(device_->CreateSamplerState(&s, sLinearWrap_.put()), "Sampler");
    return true;
}

static void fullscreenViewport(ID3D11DeviceContext* ctx, int w, int h) {
    D3D11_VIEWPORT vp = {0, 0, (float)w, (float)h, 0, 1};
    ctx->RSSetViewports(1, &vp);
}

bool Renderer::createBrdfLut(std::string& error) {
    const int N = 128;
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = d.Height = N;
    d.MipLevels = 1;
    d.ArraySize = 1;
    d.Format = DXGI_FORMAT_R16G16_FLOAT;
    d.SampleDesc.Count = 1;
    d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    TRY(device_->CreateTexture2D(&d, nullptr, brdfTex_.put()), "BRDF LUT");
    TRY(device_->CreateShaderResourceView(brdfTex_.get(), nullptr, brdfSRV_.put()), "BRDF LUT SRV");
    ComPtr<ID3D11RenderTargetView> rtv;
    TRY(device_->CreateRenderTargetView(brdfTex_.get(), nullptr, rtv.put()), "BRDF LUT RTV");
    ctx_->OMSetRenderTargets(1, rtv.addressOf(), nullptr);
    fullscreenViewport(ctx_, N, N);
    ctx_->IASetInputLayout(nullptr);
    ctx_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx_->RSSetState(rsNoCull_.get());
    ctx_->OMSetDepthStencilState(dsNone_.get(), 0);
    ctx_->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    ctx_->VSSetShader(vsFullscreen_.get(), nullptr, 0);
    ctx_->GSSetShader(nullptr, nullptr, 0);
    ctx_->PSSetShader(psBrdfLut_.get(), nullptr, 0);
    ctx_->Draw(3, 0);
    ctx_->OMSetRenderTargets(0, nullptr, nullptr);
    return true;
}

bool Renderer::setMesh(const Mesh& mesh, std::string& error) {
    clearMesh();
    if (mesh.indices.empty()) return true;
    D3D11_BUFFER_DESC d = {};
    d.Usage = D3D11_USAGE_IMMUTABLE;
    d.ByteWidth = (UINT)(mesh.vertices.size() * sizeof(Vertex));
    d.BindFlags = D3D11_BIND_VERTEX_BUFFER;
    D3D11_SUBRESOURCE_DATA init = {mesh.vertices.data(), 0, 0};
    TRY(device_->CreateBuffer(&d, &init, vb_.put()), "Vertex buffer (out of video memory?)");
    d.ByteWidth = (UINT)(mesh.indices.size() * sizeof(uint32_t));
    d.BindFlags = D3D11_BIND_INDEX_BUFFER;
    init.pSysMem = mesh.indices.data();
    TRY(device_->CreateBuffer(&d, &init, ib_.put()), "Index buffer (out of video memory?)");
    indexCount_ = (uint32_t)mesh.indices.size();
    sphereCenter_ = mesh.sphereCenter;
    sphereRadius_ = mesh.sphereRadius;
    meshMinZ_ = mesh.boundsMin.z;
    meshMaxZ_ = mesh.boundsMax.z;
    return true;
}

void Renderer::clearMesh() {
    vb_ = nullptr;
    ib_ = nullptr;
    indexCount_ = 0;
}

void Renderer::drawMesh() {
    if (!indexCount_) return;
    UINT stride = sizeof(Vertex), offset = 0;
    ctx_->IASetInputLayout(meshLayout_.get());
    ctx_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx_->IASetVertexBuffers(0, 1, vb_.addressOf(), &stride, &offset);
    ctx_->IASetIndexBuffer(ib_.get(), DXGI_FORMAT_R32_UINT, 0);
    // Chunks of at most 4M triangles keep each draw well under the 2 s GPU timeout.
    const uint32_t chunk = 4u * 1024u * 1024u * 3u;
    for (uint32_t first = 0; first < indexCount_; first += chunk)
        ctx_->DrawIndexed(std::min(chunk, indexCount_ - first), first, 0);
}

bool Renderer::setEnvironment(const EnvironmentImage& env, std::string& error) {
    // 1. Equirectangular source (half floats, full mip chain).
    std::vector<uint16_t> half((size_t)env.width * env.height * 4);
    for (size_t i = 0; i < half.size(); ++i) half[i] = floatToHalf(env.rgba[i]);
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = (UINT)env.width;
    d.Height = (UINT)env.height;
    d.MipLevels = 0;
    d.ArraySize = 1;
    d.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    d.SampleDesc.Count = 1;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
    d.MiscFlags = D3D11_RESOURCE_MISC_GENERATE_MIPS;
    ComPtr<ID3D11Texture2D> eqTex;
    ComPtr<ID3D11ShaderResourceView> eqSRV;
    TRY(device_->CreateTexture2D(&d, nullptr, eqTex.put()), "Environment texture");
    TRY(device_->CreateShaderResourceView(eqTex.get(), nullptr, eqSRV.put()), "Environment SRV");
    ctx_->UpdateSubresource(eqTex.get(), 0, nullptr, half.data(), (UINT)env.width * 8, 0);
    ctx_->GenerateMips(eqSRV.get());

    // 2. 512^2 cubemap with filtered mips (source for prefiltering and blurred backgrounds).
    const int cubeSize = 512;
    d = D3D11_TEXTURE2D_DESC();
    d.Width = d.Height = cubeSize;
    d.MipLevels = 7;  // 512 .. 8: blurrier lookups clamp to 8x8, which is plenty
    d.ArraySize = 6;
    d.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    d.SampleDesc.Count = 1;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
    d.MiscFlags = D3D11_RESOURCE_MISC_TEXTURECUBE;
    ComPtr<ID3D11Texture2D> cubeTex;
    ComPtr<ID3D11ShaderResourceView> cubeSRV;
    TRY(device_->CreateTexture2D(&d, nullptr, cubeTex.put()), "Environment cubemap");
    TRY(device_->CreateShaderResourceView(cubeTex.get(), nullptr, cubeSRV.put()), "Environment cubemap SRV");
    cubeTex->GetDesc(&d);
    int cubeMips = (int)d.MipLevels;

    // 3. GGX-prefiltered specular cubemap, roughness = mip / (mips - 1).
    const int specSize = 256, specMips = 6;
    D3D11_TEXTURE2D_DESC sd = d;
    sd.Width = sd.Height = specSize;
    sd.MipLevels = specMips;
    sd.MiscFlags = D3D11_RESOURCE_MISC_TEXTURECUBE;
    ComPtr<ID3D11Texture2D> specTex;
    ComPtr<ID3D11ShaderResourceView> specSRV;
    TRY(device_->CreateTexture2D(&sd, nullptr, specTex.put()), "Specular cubemap");
    TRY(device_->CreateShaderResourceView(specTex.get(), nullptr, specSRV.put()), "Specular cubemap SRV");

    unbindAll();
    ctx_->IASetInputLayout(nullptr);
    ctx_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx_->RSSetState(rsNoCull_.get());
    ctx_->OMSetDepthStencilState(dsNone_.get(), 0);
    ctx_->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    ctx_->VSSetShader(vsFullscreen_.get(), nullptr, 0);
    ctx_->GSSetShader(nullptr, nullptr, 0);
    ID3D11SamplerState* samplers[] = {sShadow_.get(), sLinearClamp_.get(), sLinearWrap_.get(), sPointClamp_.get(), sEquirect_.get()};
    ctx_->PSSetSamplers(0, 5, samplers);
    ctx_->PSSetConstantBuffers(2, 1, cbBake_.addressOf());

    auto bake = [&](float face, float roughness, float lodOrRes) {
        D3D11_MAPPED_SUBRESOURCE m;
        if (SUCCEEDED(ctx_->Map(cbBake_.get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
            BakeConstants b;
            set4(b.face, face, roughness, lodOrRes, 0);
            std::memcpy(m.pData, &b, sizeof(b));
            ctx_->Unmap(cbBake_.get(), 0);
        }
    };
    auto faceRTV = [&](ID3D11Texture2D* tex, int face, int mip, ComPtr<ID3D11RenderTargetView>& out) {
        D3D11_RENDER_TARGET_VIEW_DESC rv = {};
        rv.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        rv.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2DARRAY;
        rv.Texture2DArray.MipSlice = (UINT)mip;
        rv.Texture2DArray.FirstArraySlice = (UINT)face;
        rv.Texture2DArray.ArraySize = 1;
        return device_->CreateRenderTargetView(tex, &rv, out.put());
    };

    // Equirect -> every cube face and mip, each reading the equirect mip that
    // matches its texel density. (Filling the mips directly instead of using
    // GenerateMips on the cube: some drivers leave cube mips empty.)
    ctx_->PSSetShader(psEquirectToCube_.get(), nullptr, 0);
    ctx_->PSSetShaderResources(9, 1, eqSRV.addressOf());
    for (int mip = 0; mip < cubeMips; ++mip) {
        int size = std::max(1, cubeSize >> mip);
        float lod = std::max(0.0f, std::log2((float)env.width / (4.0f * size)));
        fullscreenViewport(ctx_, size, size);
        for (int face = 0; face < 6; ++face) {
            ComPtr<ID3D11RenderTargetView> rtv;
            TRY(faceRTV(cubeTex.get(), face, mip, rtv), "Cubemap RTV");
            bake((float)face, 0, lod);
            ctx_->OMSetRenderTargets(1, rtv.addressOf(), nullptr);
            ctx_->Draw(3, 0);
        }
    }
    ctx_->OMSetRenderTargets(0, nullptr, nullptr);

    ctx_->PSSetShader(psPrefilter_.get(), nullptr, 0);
    ctx_->PSSetShaderResources(10, 1, cubeSRV.addressOf());
    for (int mip = 0; mip < specMips; ++mip) {
        int size = specSize >> mip;
        fullscreenViewport(ctx_, size, size);
        float roughness = (float)mip / (float)(specMips - 1);
        for (int face = 0; face < 6; ++face) {
            ComPtr<ID3D11RenderTargetView> rtv;
            TRY(faceRTV(specTex.get(), face, mip, rtv), "Specular RTV");
            bake((float)face, roughness, (float)cubeSize);
            ctx_->OMSetRenderTargets(1, rtv.addressOf(), nullptr);
            ctx_->Draw(3, 0);
            ctx_->Flush();  // keep each submission short (GPU timeout)
        }
    }
    ctx_->OMSetRenderTargets(0, nullptr, nullptr);
    unbindAll();

    equirectTex_ = eqTex;
    equirectSRV_ = eqSRV;
    envCubeTex_ = cubeTex;
    envCubeSRV_ = cubeSRV;
    specCubeTex_ = specTex;
    specCubeSRV_ = specSRV;
    envMips_ = cubeMips;
    specMips_ = specMips;
    for (int i = 0; i < 9; ++i) sh_[i] = env.sh[i];
    envDominantDir_ = env.dominantDirection;
    envDominantColor_ = env.dominantColor;
    envDirectionality_ = env.directionality;
    hasEnv_ = true;
    return true;
}

bool Renderer::setUserTexture(const Image8* image, std::string& error) {
    userTex_ = nullptr;
    userSRV_ = nullptr;
    if (!image || image->width <= 0) return true;
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = (UINT)image->width;
    d.Height = (UINT)image->height;
    d.MipLevels = 0;
    d.ArraySize = 1;
    d.Format = DXGI_FORMAT_R8G8B8A8_UNORM_SRGB;
    d.SampleDesc.Count = 1;
    d.BindFlags = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
    d.MiscFlags = D3D11_RESOURCE_MISC_GENERATE_MIPS;
    TRY(device_->CreateTexture2D(&d, nullptr, userTex_.put()), "Texture");
    TRY(device_->CreateShaderResourceView(userTex_.get(), nullptr, userSRV_.put()), "Texture SRV");
    ctx_->UpdateSubresource(userTex_.get(), 0, nullptr, image->rgba.data(), (UINT)image->width * 4, 0);
    ctx_->GenerateMips(userSRV_.get());
    return true;
}

bool Renderer::ensureTargets(RenderTargets& rt, int width, int height, std::string& error) {
    width = std::max(1, width);
    height = std::max(1, height);
    if (rt.width == width && rt.height == height && rt.outTex) return true;
    rt.release();
    auto color = [&](DXGI_FORMAT fmt, ComPtr<ID3D11Texture2D>& tex, ComPtr<ID3D11RenderTargetView>& rtv,
                     ComPtr<ID3D11ShaderResourceView>& srv, const char* what) {
        D3D11_TEXTURE2D_DESC d = {};
        d.Width = (UINT)width;
        d.Height = (UINT)height;
        d.MipLevels = 1;
        d.ArraySize = 1;
        d.Format = fmt;
        d.SampleDesc.Count = 1;
        d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        TRY(device_->CreateTexture2D(&d, nullptr, tex.put()), what);
        TRY(device_->CreateRenderTargetView(tex.get(), nullptr, rtv.put()), what);
        TRY(device_->CreateShaderResourceView(tex.get(), nullptr, srv.put()), what);
        return true;
    };
    if (!color(DXGI_FORMAT_R16G16B16A16_FLOAT, rt.hdrTex, rt.hdrRTV, rt.hdrSRV, "HDR target") ||
        !color(DXGI_FORMAT_R8_UNORM, rt.aoRawTex, rt.aoRawRTV, rt.aoRawSRV, "AO target") ||
        !color(DXGI_FORMAT_R8_UNORM, rt.aoTex, rt.aoRTV, rt.aoSRV, "AO target") ||
        !color(accumFormat_, rt.accumTex, rt.accumRTV, rt.accumSRV, "Accumulation target") ||
        !color(DXGI_FORMAT_R8G8B8A8_UNORM, rt.outTex, rt.outRTV, rt.outSRV, "Output target")) {
        rt.release();
        return false;
    }
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = (UINT)width;
    d.Height = (UINT)height;
    d.MipLevels = 1;
    d.ArraySize = 1;
    d.Format = DXGI_FORMAT_R32_TYPELESS;
    d.SampleDesc.Count = 1;
    d.BindFlags = D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE;
    if (FAILED(device_->CreateTexture2D(&d, nullptr, rt.depthTex.put()))) {
        rt.release();
        error = "Depth target (out of video memory?)";
        return false;
    }
    D3D11_DEPTH_STENCIL_VIEW_DESC dv = {};
    dv.Format = DXGI_FORMAT_D32_FLOAT;
    dv.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2D;
    D3D11_SHADER_RESOURCE_VIEW_DESC sv = {};
    sv.Format = DXGI_FORMAT_R32_FLOAT;
    sv.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
    sv.Texture2D.MipLevels = 1;
    if (FAILED(device_->CreateDepthStencilView(rt.depthTex.get(), &dv, rt.depthDSV.put())) ||
        FAILED(device_->CreateShaderResourceView(rt.depthTex.get(), &sv, rt.depthSRV.put()))) {
        rt.release();
        error = "Depth views";
        return false;
    }
    rt.width = width;
    rt.height = height;
    return true;
}

// The reflection targets are only allocated once a reflective floor is used.
static bool ensureReflection(ID3D11Device* dev, RenderTargets& rt) {
    if (rt.reflTex) return true;
    D3D11_TEXTURE2D_DESC d = {};
    d.Width = (UINT)rt.width;
    d.Height = (UINT)rt.height;
    d.MipLevels = 6;
    d.ArraySize = 1;
    d.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
    d.SampleDesc.Count = 1;
    d.BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
    d.MiscFlags = D3D11_RESOURCE_MISC_GENERATE_MIPS;
    if (FAILED(dev->CreateTexture2D(&d, nullptr, rt.reflTex.put()))) return false;
    D3D11_RENDER_TARGET_VIEW_DESC rv = {};
    rv.Format = d.Format;
    rv.ViewDimension = D3D11_RTV_DIMENSION_TEXTURE2D;
    if (FAILED(dev->CreateRenderTargetView(rt.reflTex.get(), &rv, rt.reflRTV.put())) ||
        FAILED(dev->CreateShaderResourceView(rt.reflTex.get(), nullptr, rt.reflSRV.put())))
        return false;
    d.MipLevels = 1;
    d.MiscFlags = 0;
    d.Format = DXGI_FORMAT_D32_FLOAT;
    d.BindFlags = D3D11_BIND_DEPTH_STENCIL;
    if (FAILED(dev->CreateTexture2D(&d, nullptr, rt.reflDepthTex.put())) ||
        FAILED(dev->CreateDepthStencilView(rt.reflDepthTex.get(), nullptr, rt.reflDSV.put())))
        return false;
    return true;
}

void Renderer::unbindAll() {
    ID3D11ShaderResourceView* nulls[12] = {};
    ctx_->PSSetShaderResources(0, 12, nulls);
    ctx_->OMSetRenderTargets(0, nullptr, nullptr);
}

void Renderer::bindCommon() {
    ID3D11SamplerState* samplers[] = {sShadow_.get(), sLinearClamp_.get(), sLinearWrap_.get(), sPointClamp_.get(), sEquirect_.get()};
    ctx_->PSSetSamplers(0, 5, samplers);
    ID3D11Buffer* cbs[] = {cbFrame_.get(), cbMaterial_.get(), cbBake_.get()};
    ctx_->VSSetConstantBuffers(0, 3, cbs);
    ctx_->GSSetConstantBuffers(0, 3, cbs);
    ctx_->PSSetConstantBuffers(0, 3, cbs);
}

void Renderer::updateFrame(const FrameConstants& f) {
    D3D11_MAPPED_SUBRESOURCE m;
    if (SUCCEEDED(ctx_->Map(cbFrame_.get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
        std::memcpy(m.pData, &f, sizeof(f));
        ctx_->Unmap(cbFrame_.get(), 0);
    }
    *frame_ = f;
}

void Renderer::fillMaterial(MaterialConstants& c, const Scene& s) {
    const MaterialSettings& m = s.material;
    std::memset(&c, 0, sizeof(c));
    set4(c.base, srgbToLinear(m.baseColor), clampf(m.roughness, 0, 1));
    set4(c.pbr, clampf(m.metalness, 0, 1), clampf(m.clearcoat, 0, 1), clampf(m.clearcoatRoughness, 0, 1), clampf(m.sheen, 0, 1));
    set4(c.trans, clampf(m.transmission, 0, 1), std::max(1.0f, m.ior), 0, 0);
    set4(c.transTint, srgbToLinear(m.transmissionTint), 0);
    set4(c.layer, m.layerLines ? 1.0f : 0.0f, std::max(0.01f, m.layerHeight), clampf(m.layerDepth, 0, 1), 0);
    set4(c.noise, std::max(0.0f, m.noiseStrength), std::max(0.001f, m.noiseScale), std::max(0.001f, m.noiseStretch), 0);
    int pattern = (int)m.pattern;
    if (m.pattern == Pattern::Image && !userSRV_) pattern = 0;
    set4(c.pattern, (float)pattern, std::max(0.01f, m.patternScale), clampf(m.patternStrength, 0, 1),
         clampf(m.triplanarSharpness, 1, 32));
    set4(c.patternColor, srgbToLinear(m.patternColor), 0);
    set4(c.gradient, srgbToLinear(m.gradientColor), m.gradient ? 1.0f : 0.0f);
    set4(c.gradientRange, meshMinZ_, meshMaxZ_, 0, 0);
    set4(c.wire, srgbToLinear(m.wireColor), m.wireframe ? std::max(0.5f, m.wireWidth) : 0.0f);
}

void Renderer::fillFrame(FrameConstants& f, const RenderTargets& rt, const Scene& s, const SampleInput& in,
                         const mat4& world, const mat4& lightViewProj, const vec3& keyDir, float keyIntensity,
                         float envShadow) {
    const EnvironmentSettings& e = s.environment;
    f = FrameConstants();
    f.view = in.view.view;
    f.proj = in.view.proj;
    f.viewProj = in.view.proj * in.view.view;
    f.projInv = in.view.proj.inverse();
    f.invViewProj = f.viewProj.inverse();
    f.lightViewProj = lightViewProj;
    f.world = world;
    f.mirror = mat4::identity();
    set4(f.eye, in.view.eye, in.view.ortho ? 1.0f : 0.0f);
    set4(f.forward, in.view.forward, 0);
    set4(f.screen, (float)rt.width, (float)rt.height, 1.0f / rt.width, 1.0f / rt.height);

    vec3 keyColor = e.lights.enabled ? srgbToLinear(e.lights.keyColor) : envDominantColor_;
    set4(f.keyDir, keyDir, keyIntensity);
    set4(f.keyColor, keyColor, clampf(envShadow, 0, 1));
    if (e.lights.enabled) {
        // Fill and rim follow the key: fill low on the opposite side, rim behind.
        float az = in.lightReferenceAzimuth;
        set4(f.fillDir, dirFromAzEl(az - 60.0f, 15.0f), e.lights.intensity * e.lights.fill);
        set4(f.fillColor, vec3(0.92f, 0.95f, 1.0f), 0);
        set4(f.rimDir, dirFromAzEl(az + 160.0f, 25.0f), e.lights.intensity * e.lights.rim);
        set4(f.rimColor, vec3(1, 1, 1), 0);
    }
    set4(f.env, std::max(0.0f, e.intensity), radians(e.rotation), clampf(e.saturation, 0, 2), (float)(specMips_ - 1));
    for (int i = 0; i < 9; ++i) set4(f.sh[i], sh_[i], 0);

    set4(f.background, (float)(int)e.background, clampf(e.backgroundBlur, 0, 1),
         e.background == Background::Transparent ? 1.0f : 0.0f, (float)(envMips_ - 1));
    set4(f.bgColor, srgbToLinear(e.backgroundColor), 0);
    set4(f.bgColor2, srgbToLinear(e.backgroundColor2), 0);
    set4(f.ground, (float)(int)e.ground, clampf(e.floorRoughness, 0, 1), clampf(e.reflection, 0, 1),
         clampf(e.shadowStrength, 0, 1));
    set4(f.floorColor, srgbToLinear(e.floorColor), sphereRadius_ * 10.0f);
    bool ao = in.fullQuality && e.aoStrength > 0;
    set4(f.ao, clampf(e.aoStrength, 0, 1), std::max(1e-4f, e.aoRadius) * sphereRadius_, ao ? 1.0f : 0.0f,
         (float)in.sampleIndex);
    float texelWorld = 2.04f * sphereRadius_ / (float)shadowSize_;
    float pcf = in.sampleIndex > 0 && in.fullQuality ? 1.0f : 1.5f + e.shadowSoftness * 0.35f;
    set4(f.shadow, 1.0f / (float)shadowSize_, texelWorld * 1.5f, pcf, indexCount_ ? 1.0f : 0.0f);
    set4(f.tone, std::pow(2.0f, e.exposure), (float)(int)e.tonemap, 1.0f, 0.0f);
    set4(f.model, sphereCenter_, sphereRadius_);
    set4(f.pass, 0, 0, (float)in.sampleIndex, 0);
}

void Renderer::renderSample(RenderTargets& rt, const Scene& s, const SampleInput& in) {
    const EnvironmentSettings& e = s.environment;
    const bool mesh = indexCount_ > 0;
    const bool ground = e.ground != Ground::None;
    const mat4 world = mat4::rotationZ(in.objectAngle);

    // ---- key light (casts the shadow map) ----
    vec3 keyDir;
    float keyIntensity = 0, envShadow;
    if (e.lights.enabled) {
        keyDir = dirFromAzEl(in.lightReferenceAzimuth + e.lights.keyAzimuth, clampf(e.lights.keyElevation, 5.0f, 89.0f));
        keyIntensity = std::max(0.0f, e.lights.intensity);
        envShadow = e.shadowStrength * 0.35f;
    } else {
        // Shadow from the environment's dominant light, darkening IBL in proportion
        // to how directional the environment is.
        vec3 d = envDominantDir_;
        float r = radians(e.rotation);
        keyDir = {std::cos(r) * d.x - std::sin(r) * d.y, std::sin(r) * d.x + std::cos(r) * d.y, d.z};
        envShadow = e.shadowStrength * clampf(0.3f + envDirectionality_ * 1.6f, 0.0f, 1.0f);
    }
    // Soft shadows: jitter the light across a cone, one direction per sample.
    if (in.fullQuality && in.sampleIndex > 0 && e.shadowSoftness > 0) {
        vec3 helper = std::fabs(keyDir.z) < 0.99f ? vec3(0, 0, 1) : vec3(1, 0, 0);
        vec3 b1 = normalize(cross(helper, keyDir)), b2 = cross(keyDir, b1);
        float u = halton(in.sampleIndex + 11, 5), v = halton(in.sampleIndex + 11, 7);
        float rr = std::sqrt(u) * std::tan(radians(e.shadowSoftness * 0.5f));
        float phi = 2 * kPi * v;
        keyDir = normalize(keyDir + b1 * (rr * std::cos(phi)) + b2 * (rr * std::sin(phi)));
    }
    const vec3 c = sphereCenter_;
    const float R = sphereRadius_;
    mat4 lightView = mat4::lookAt(c + keyDir * (4.0f * R), c, vec3(0, 0, 1));
    mat4 lightProj = mat4::orthographic(R * 1.02f, R * 1.02f, R * 0.5f, R * 40.0f);
    mat4 lightViewProj = lightProj * lightView;

    FrameConstants f;
    fillFrame(f, rt, s, in, world, lightViewProj, keyDir, keyIntensity, envShadow);
    if (!mesh) f.shadow[3] = 0;
    MaterialConstants mc;
    fillMaterial(mc, s);
    {
        D3D11_MAPPED_SUBRESOURCE m;
        if (SUCCEEDED(ctx_->Map(cbMaterial_.get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &m))) {
            std::memcpy(m.pData, &mc, sizeof(mc));
            ctx_->Unmap(cbMaterial_.get(), 0);
        }
    }
    updateFrame(f);
    unbindAll();
    bindCommon();
    ctx_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx_->GSSetShader(nullptr, nullptr, 0);

    // ---- 1. shadow map ----
    if (mesh) {
        ctx_->ClearDepthStencilView(shadowDSV_.get(), D3D11_CLEAR_DEPTH, 1.0f, 0);
        ctx_->OMSetRenderTargets(0, nullptr, shadowDSV_.get());
        fullscreenViewport(ctx_, shadowSize_, shadowSize_);
        ctx_->RSSetState(rsShadow_.get());
        ctx_->OMSetDepthStencilState(dsLessWrite_.get(), 0);
        ctx_->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
        ctx_->VSSetShader(vsShadow_.get(), nullptr, 0);
        ctx_->PSSetShader(nullptr, nullptr, 0);
        drawMesh();
        ctx_->OMSetRenderTargets(0, nullptr, nullptr);
    }

    // ---- 2. depth pre-pass (model + ground) ----
    fullscreenViewport(ctx_, rt.width, rt.height);
    ctx_->RSSetState(rsNoCull_.get());
    ctx_->ClearDepthStencilView(rt.depthDSV.get(), D3D11_CLEAR_DEPTH, 1.0f, 0);
    ctx_->OMSetRenderTargets(0, nullptr, rt.depthDSV.get());
    ctx_->OMSetDepthStencilState(dsLessWrite_.get(), 0);
    ctx_->PSSetShader(nullptr, nullptr, 0);
    if (mesh) {
        ctx_->VSSetShader(vsMesh_.get(), nullptr, 0);
        drawMesh();
    }
    if (ground) {
        ctx_->IASetInputLayout(nullptr);
        ctx_->VSSetShader(vsGround_.get(), nullptr, 0);
        ctx_->Draw(6, 0);
    }
    ctx_->OMSetRenderTargets(0, nullptr, nullptr);

    // ---- 3. ambient occlusion ----
    const bool ao = f.ao[2] > 0.5f;
    if (ao) {
        ctx_->IASetInputLayout(nullptr);
        ctx_->OMSetDepthStencilState(dsNone_.get(), 0);
        ctx_->VSSetShader(vsFullscreen_.get(), nullptr, 0);
        ctx_->OMSetRenderTargets(1, rt.aoRawRTV.addressOf(), nullptr);
        ctx_->PSSetShaderResources(6, 1, rt.depthSRV.addressOf());
        ctx_->PSSetShader(psSSAO_.get(), nullptr, 0);
        ctx_->Draw(3, 0);
        ctx_->OMSetRenderTargets(1, rt.aoRTV.addressOf(), nullptr);
        ctx_->PSSetShaderResources(11, 1, rt.aoRawSRV.addressOf());
        ctx_->PSSetShader(psAOBlur_.get(), nullptr, 0);
        ctx_->Draw(3, 0);
        unbindAll();
    }

    ID3D11ShaderResourceView* lighting[6] = {mesh ? shadowSRV_.get() : whiteSRV_.get(), specCubeSRV_.get(), brdfSRV_.get(),
                                             ao ? rt.aoSRV.get() : whiteSRV_.get(), whiteSRV_.get(),
                                             userSRV_ ? userSRV_.get() : whiteSRV_.get()};

    // ---- 4. planar reflection (model mirrored in z = 0, shaded from the mirrored eye) ----
    bool reflection = e.ground == Ground::Reflective && mesh && e.reflection > 0 && ensureReflection(device_, rt);
    if (reflection) {
        FrameConstants rf = f;
        rf.mirror = mat4::scale(vec3(1, 1, -1));
        rf.eye[2] = -rf.eye[2];
        rf.forward[2] = -rf.forward[2];
        rf.ao[2] = 0;  // screen-space AO belongs to the direct view
        rf.pass[0] = 1;
        updateFrame(rf);
        const float clear[4] = {0, 0, 0, 0};
        ctx_->ClearRenderTargetView(rt.reflRTV.get(), clear);
        ctx_->ClearDepthStencilView(rt.reflDSV.get(), D3D11_CLEAR_DEPTH, 1.0f, 0);
        ctx_->OMSetRenderTargets(1, rt.reflRTV.addressOf(), rt.reflDSV.get());
        ctx_->OMSetDepthStencilState(dsLessWrite_.get(), 0);
        ctx_->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
        ID3D11ShaderResourceView* noAO[6] = {lighting[0], lighting[1], lighting[2], whiteSRV_.get(), whiteSRV_.get(), lighting[5]};
        ctx_->PSSetShaderResources(0, 6, noAO);
        ctx_->VSSetShader(vsMesh_.get(), nullptr, 0);
        ctx_->PSSetShader(psModel_.get(), nullptr, 0);
        drawMesh();
        unbindAll();
        ctx_->GenerateMips(rt.reflSRV.get());
        updateFrame(f);
        lighting[4] = rt.reflSRV.get();
    }

    // ---- 5. HDR scene: ground (premultiplied over nothing), then the model ----
    {
        const float clear[4] = {0, 0, 0, 0};
        ctx_->ClearRenderTargetView(rt.hdrRTV.get(), clear);
        ctx_->OMSetRenderTargets(1, rt.hdrRTV.addressOf(), rt.depthDSV.get());
        ctx_->OMSetDepthStencilState(dsLessEqualNoWrite_.get(), 0);
        ctx_->PSSetShaderResources(0, 6, lighting);
        if (ground) {
            ctx_->OMSetBlendState(bsPremul_.get(), nullptr, 0xFFFFFFFF);
            ctx_->IASetInputLayout(nullptr);
            ctx_->VSSetShader(vsGround_.get(), nullptr, 0);
            ctx_->PSSetShader(psGround_.get(), nullptr, 0);
            ctx_->Draw(6, 0);
        }
        if (mesh) {
            ctx_->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
            ctx_->VSSetShader(vsMesh_.get(), nullptr, 0);
            if (s.material.wireframe) {
                ctx_->GSSetShader(gsWire_.get(), nullptr, 0);
                ctx_->PSSetShader(psModelWire_.get(), nullptr, 0);
            } else {
                ctx_->PSSetShader(psModel_.get(), nullptr, 0);
            }
            drawMesh();
            ctx_->GSSetShader(nullptr, nullptr, 0);
        }
        unbindAll();
    }

    // ---- 6. tonemap this sample over the background and add it to the accumulation ----
    if (in.sampleIndex == 0) {
        const float zero[4] = {0, 0, 0, 0};
        ctx_->ClearRenderTargetView(rt.accumRTV.get(), zero);
    }
    ctx_->OMSetRenderTargets(1, rt.accumRTV.addressOf(), nullptr);
    ctx_->OMSetDepthStencilState(dsNone_.get(), 0);
    ctx_->OMSetBlendState(bsAdditive_.get(), nullptr, 0xFFFFFFFF);
    ctx_->IASetInputLayout(nullptr);
    ctx_->VSSetShader(vsFullscreen_.get(), nullptr, 0);
    ID3D11ShaderResourceView* src[] = {specCubeSRV_.get()};
    ctx_->PSSetShaderResources(1, 1, src);
    ctx_->PSSetShaderResources(7, 1, rt.hdrSRV.addressOf());
    ctx_->PSSetShaderResources(9, 1, equirectSRV_.addressOf());
    ctx_->PSSetShader(psTonemapAccum_.get(), nullptr, 0);
    ctx_->Draw(3, 0);
    ctx_->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    unbindAll();
}

void Renderer::resolve(RenderTargets& rt, int samples, bool overBlack) {
    FrameConstants f = *frame_;
    f.tone[2] = 1.0f / (float)std::max(1, samples);
    f.tone[3] = overBlack ? 1.0f : 0.0f;
    set4(f.screen, (float)rt.width, (float)rt.height, 1.0f / rt.width, 1.0f / rt.height);
    updateFrame(f);
    bindCommon();
    unbindAll();
    fullscreenViewport(ctx_, rt.width, rt.height);
    ctx_->OMSetRenderTargets(1, rt.outRTV.addressOf(), nullptr);
    ctx_->RSSetState(rsNoCull_.get());
    ctx_->OMSetDepthStencilState(dsNone_.get(), 0);
    ctx_->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    ctx_->IASetInputLayout(nullptr);
    ctx_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx_->VSSetShader(vsFullscreen_.get(), nullptr, 0);
    ctx_->GSSetShader(nullptr, nullptr, 0);
    ctx_->PSSetShaderResources(8, 1, rt.accumSRV.addressOf());
    ctx_->PSSetShader(psResolve_.get(), nullptr, 0);
    ctx_->Draw(3, 0);
    unbindAll();
}

void Renderer::blit(RenderTargets& rt, ID3D11RenderTargetView* target, const D3D11_VIEWPORT& vp, bool checkerboard) {
    FrameConstants f = *frame_;
    f.pass[1] = checkerboard ? 1.0f : 0.0f;
    updateFrame(f);
    bindCommon();
    ctx_->RSSetViewports(1, &vp);
    ctx_->OMSetRenderTargets(1, &target, nullptr);
    ctx_->RSSetState(rsNoCull_.get());
    ctx_->OMSetDepthStencilState(dsNone_.get(), 0);
    ctx_->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    ctx_->IASetInputLayout(nullptr);
    ctx_->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    ctx_->VSSetShader(vsFullscreen_.get(), nullptr, 0);
    ctx_->GSSetShader(nullptr, nullptr, 0);
    ctx_->PSSetShaderResources(7, 1, rt.outSRV.addressOf());
    ctx_->PSSetShader(psBlit_.get(), nullptr, 0);
    ctx_->Draw(3, 0);
    ID3D11ShaderResourceView* none = nullptr;
    ctx_->PSSetShaderResources(7, 1, &none);
}

}  // namespace spindle
