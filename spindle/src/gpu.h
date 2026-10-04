// Direct3D 11 device creation with Optimus-aware adapter selection, plus
// runtime shader compilation (d3dcompiler_47.dll ships with Windows 10).
#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>

#include <string>
#include <vector>

namespace spindle {

// Minimal COM smart pointer (avoids a WRL dependency so MinGW builds work too).
template <class T>
class ComPtr {
public:
    ComPtr() = default;
    ComPtr(std::nullptr_t) {}
    ComPtr(const ComPtr& o) : p_(o.p_) { if (p_) p_->AddRef(); }
    ComPtr(ComPtr&& o) noexcept : p_(o.p_) { o.p_ = nullptr; }
    ~ComPtr() { reset(); }
    ComPtr& operator=(const ComPtr& o) {
        if (this != &o) {
            reset();
            p_ = o.p_;
            if (p_) p_->AddRef();
        }
        return *this;
    }
    ComPtr& operator=(ComPtr&& o) noexcept {
        if (this != &o) {
            reset();
            p_ = o.p_;
            o.p_ = nullptr;
        }
        return *this;
    }
    ComPtr& operator=(std::nullptr_t) { reset(); return *this; }
    void reset() {
        if (p_) {
            p_->Release();
            p_ = nullptr;
        }
    }
    T* get() const { return p_; }
    T* operator->() const { return p_; }
    T** put() { reset(); return &p_; }       // for out-parameters
    T* const* addressOf() const { return &p_; }  // for array-of-one bindings
    explicit operator bool() const { return p_ != nullptr; }
    template <class U>
    HRESULT as(ComPtr<U>& out) const { return p_->QueryInterface(__uuidof(U), (void**)out.put()); }

private:
    T* p_ = nullptr;
};

struct AdapterInfo {
    int index = -1;
    std::string name;
    size_t dedicatedVideoMemoryMB = 0;
    size_t sharedSystemMemoryMB = 0;
    unsigned vendorId = 0;
    bool software = false;
    std::string driverVersion;
};

std::vector<AdapterInfo> listAdapters();

struct Gpu {
    ComPtr<ID3D11Device> device;
    ComPtr<ID3D11DeviceContext> context;
    ComPtr<IDXGIFactory2> factory;
    AdapterInfo adapter;
    D3D_FEATURE_LEVEL featureLevel = D3D_FEATURE_LEVEL_11_0;
    bool warp = false;
};

// preference: "auto" (high-performance GPU), an adapter index, a case-insensitive
// substring of the adapter name, or "warp" (CPU rasteriser, for testing).
bool createGpu(Gpu& gpu, const std::string& preference, std::string& error);

// Compiles HLSL from the embedded shader source. Results are cached on disk
// (keyed by a hash of the source, entry point, profile and defines).
bool compileShader(const char* entry, const char* profile, const std::vector<std::pair<std::string, std::string>>& defines,
                   std::vector<char>& bytecode, std::string& error);
void setShaderCacheDir(const std::string& dir);

// The embedded shaders.hlsl (generated at build time).
extern const unsigned char kShaderSource[];
extern const unsigned long long kShaderSourceSize;

}  // namespace spindle
