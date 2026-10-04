#include "gpu.h"

#include "common.h"

#include <d3dcompiler.h>
#include <dxgi1_6.h>

#include <cstdio>
#include <cstring>

// Ask Optimus / PowerXpress laptops to run this process on the discrete GPU.
// These must be exported from the .exe itself; a DLL export is ignored.
extern "C" {
__declspec(dllexport) DWORD NvOptimusEnablement = 1;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}

namespace spindle {

static std::string driverVersionOf(IDXGIAdapter1* adapter) {
    LARGE_INTEGER v;
    if (FAILED(adapter->CheckInterfaceSupport(__uuidof(IDXGIDevice), &v))) return {};
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%u.%u.%u.%u", HIWORD(v.HighPart), LOWORD(v.HighPart), HIWORD(v.LowPart),
                  LOWORD(v.LowPart));
    return buf;
}

static AdapterInfo describe(IDXGIAdapter1* a, int index) {
    DXGI_ADAPTER_DESC1 d;
    a->GetDesc1(&d);
    AdapterInfo info;
    info.index = index;
    info.name = narrow(d.Description);
    info.dedicatedVideoMemoryMB = d.DedicatedVideoMemory >> 20;
    info.sharedSystemMemoryMB = d.SharedSystemMemory >> 20;
    info.vendorId = d.VendorId;
    info.software = (d.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0 || (d.VendorId == 0x1414 && d.DeviceId == 0x8c);
    info.driverVersion = driverVersionOf(a);
    return info;
}

std::vector<AdapterInfo> listAdapters() {
    std::vector<AdapterInfo> out;
    ComPtr<IDXGIFactory1> factory;
    if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)factory.put()))) return out;
    ComPtr<IDXGIAdapter1> a;
    for (UINT i = 0; factory->EnumAdapters1(i, a.put()) != DXGI_ERROR_NOT_FOUND; ++i) out.push_back(describe(a.get(), (int)i));
    return out;
}

// Picks the adapter: explicit preference first, then the OS high-performance
// choice (Windows 10 1803+), then the hardware adapter with the most VRAM.
static ComPtr<IDXGIAdapter1> pickAdapter(IDXGIFactory1* factory, const std::string& pref, std::string& why) {
    std::vector<ComPtr<IDXGIAdapter1>> all;
    {
        ComPtr<IDXGIAdapter1> a;
        for (UINT i = 0; factory->EnumAdapters1(i, a.put()) != DXGI_ERROR_NOT_FOUND; ++i) all.push_back(a);
    }
    std::string p = toLower(pref);
    if (!p.empty() && p != "auto") {
        char* end = nullptr;
        long idx = std::strtol(p.c_str(), &end, 10);
        if (end && *end == 0 && idx >= 0 && (size_t)idx < all.size()) {
            why = "selected by index";
            return all[idx];
        }
        for (auto& a : all)
            if (toLower(describe(a.get(), 0).name).find(p) != std::string::npos) {
                why = "selected by name";
                return a;
            }
        logf("GPU preference '%s' matched nothing; using automatic selection", pref.c_str());
    }

    ComPtr<IDXGIFactory6> f6;
    if (SUCCEEDED(factory->QueryInterface(__uuidof(IDXGIFactory6), (void**)f6.put()))) {
        ComPtr<IDXGIAdapter1> a;
        if (SUCCEEDED(f6->EnumAdapterByGpuPreference(0, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE, __uuidof(IDXGIAdapter1),
                                                     (void**)a.put())) &&
            !describe(a.get(), 0).software) {
            why = "high-performance preference";
            return a;
        }
    }
    ComPtr<IDXGIAdapter1> best;
    size_t bestMem = 0;
    for (auto& a : all) {
        AdapterInfo d = describe(a.get(), 0);
        if (d.software) continue;
        if (!best || d.dedicatedVideoMemoryMB > bestMem) {
            best = a;
            bestMem = d.dedicatedVideoMemoryMB;
        }
    }
    why = "most dedicated video memory";
    return best;
}

bool createGpu(Gpu& gpu, const std::string& preference, std::string& error) {
    gpu = Gpu();
    ComPtr<IDXGIFactory1> factory1;
    if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)factory1.put()))) {
        error = "Could not create a DXGI factory.";
        return false;
    }
    factory1.as(gpu.factory);

    const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};
    UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
#ifdef SPINDLE_DEBUG_LAYER
    flags |= D3D11_CREATE_DEVICE_DEBUG;
#endif
    auto create = [&](IDXGIAdapter* adapter, D3D_DRIVER_TYPE type) {
        HRESULT hr = D3D11CreateDevice(adapter, type, nullptr, flags, levels, 2, D3D11_SDK_VERSION, gpu.device.put(),
                                       &gpu.featureLevel, gpu.context.put());
        if (hr == E_INVALIDARG)  // Windows without 11_1 runtime support
            hr = D3D11CreateDevice(adapter, type, nullptr, flags, levels + 1, 1, D3D11_SDK_VERSION, gpu.device.put(),
                                   &gpu.featureLevel, gpu.context.put());
        return hr;
    };

    bool wantWarp = toLower(preference) == "warp";
    if (!wantWarp) {
        std::string why;
        ComPtr<IDXGIAdapter1> adapter = pickAdapter(factory1.get(), preference, why);
        if (adapter && SUCCEEDED(create(adapter.get(), D3D_DRIVER_TYPE_UNKNOWN))) {
            UINT count = 0;
            for (ComPtr<IDXGIAdapter1> a; factory1->EnumAdapters1(count, a.put()) != DXGI_ERROR_NOT_FOUND; ++count) {
                DXGI_ADAPTER_DESC1 d1, d2;
                a->GetDesc1(&d1);
                adapter->GetDesc1(&d2);
                if (d1.AdapterLuid.LowPart == d2.AdapterLuid.LowPart && d1.AdapterLuid.HighPart == d2.AdapterLuid.HighPart) break;
            }
            gpu.adapter = describe(adapter.get(), (int)count);
            logf("GPU: %s (%llu MB VRAM, driver %s, %s)", gpu.adapter.name.c_str(),
                 (unsigned long long)gpu.adapter.dedicatedVideoMemoryMB,
                 gpu.adapter.driverVersion.c_str(), why.c_str());
        } else {
            logf("Hardware device creation failed; falling back to WARP");
        }
    }
    if (!gpu.device) {
        if (FAILED(create(nullptr, D3D_DRIVER_TYPE_WARP))) {
            error = "Could not create a Direct3D 11 device (feature level 11_0 is required).";
            return false;
        }
        gpu.warp = true;
        gpu.adapter.name = "Microsoft Basic Render Driver (WARP, CPU)";
        gpu.adapter.software = true;
    }
    // The device's own factory is the one that must create swap chains.
    ComPtr<IDXGIDevice> dxgiDevice;
    ComPtr<IDXGIAdapter> dxgiAdapter;
    if (SUCCEEDED(gpu.device.as(dxgiDevice)) && SUCCEEDED(dxgiDevice->GetAdapter(dxgiAdapter.put()))) {
        ComPtr<IDXGIFactory2> f2;
        if (SUCCEEDED(dxgiAdapter->GetParent(__uuidof(IDXGIFactory2), (void**)f2.put()))) gpu.factory = f2;
        // Shorter queue: keeps the UI responsive during heavy accumulation.
        ComPtr<IDXGIDevice1> d1;
        if (SUCCEEDED(gpu.device.as(d1))) d1->SetMaximumFrameLatency(2);
    }
    logf("Feature level %x", (unsigned)gpu.featureLevel);
    return true;
}

// ---- shader compilation -------------------------------------------------------

static std::string g_cacheDir;
void setShaderCacheDir(const std::string& dir) { g_cacheDir = dir; }

static uint64_t fnv1a(const void* data, size_t n, uint64_t h = 1469598103934665603ull) {
    const uint8_t* p = (const uint8_t*)data;
    for (size_t i = 0; i < n; ++i) h = (h ^ p[i]) * 1099511628211ull;
    return h;
}

typedef HRESULT(WINAPI* PFN_D3DCompile)(LPCVOID, SIZE_T, LPCSTR, const D3D_SHADER_MACRO*, ID3DInclude*, LPCSTR, LPCSTR,
                                        UINT, UINT, ID3DBlob**, ID3DBlob**);

bool compileShader(const char* entry, const char* profile, const std::vector<std::pair<std::string, std::string>>& defines,
                   std::vector<char>& bytecode, std::string& error) {
    uint64_t h = fnv1a(kShaderSource, (size_t)kShaderSourceSize);
    h = fnv1a(entry, std::strlen(entry), h);
    h = fnv1a(profile, std::strlen(profile), h);
    for (auto& d : defines) {
        h = fnv1a(d.first.data(), d.first.size(), h);
        h = fnv1a(d.second.data(), d.second.size(), h);
    }
    char name[64];
    std::snprintf(name, sizeof(name), "%016llx.cso", (unsigned long long)h);
    std::string cachePath = g_cacheDir.empty() ? std::string() : g_cacheDir + "\\" + name;

    if (!cachePath.empty()) {
        FILE* f = _wfopen(widen(cachePath).c_str(), L"rb");
        if (f) {
            fseek(f, 0, SEEK_END);
            long n = ftell(f);
            fseek(f, 0, SEEK_SET);
            bytecode.resize(n > 0 ? (size_t)n : 0);
            bool ok = n > 0 && fread(bytecode.data(), 1, (size_t)n, f) == (size_t)n;
            fclose(f);
            if (ok) return true;
        }
    }

    static PFN_D3DCompile compile = [] {
        HMODULE m = LoadLibraryW(L"d3dcompiler_47.dll");
        return m ? (PFN_D3DCompile)(void*)GetProcAddress(m, "D3DCompile") : nullptr;
    }();
    if (!compile) {
        error = "d3dcompiler_47.dll is missing (it ships with Windows 10).";
        return false;
    }
    std::vector<D3D_SHADER_MACRO> macros;
    for (auto& d : defines) macros.push_back({d.first.c_str(), d.second.c_str()});
    macros.push_back({nullptr, nullptr});
    ComPtr<ID3DBlob> code, errors;
    HRESULT hr = compile(kShaderSource, (SIZE_T)kShaderSourceSize, "shaders.hlsl", macros.data(), nullptr, entry, profile,
                         D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, code.put(), errors.put());
    if (FAILED(hr)) {
        error = std::string("Shader ") + entry + " failed to compile";
        if (errors) error += ":\n" + std::string((const char*)errors->GetBufferPointer(), errors->GetBufferSize());
        return false;
    }
    const char* p = (const char*)code->GetBufferPointer();
    bytecode.assign(p, p + code->GetBufferSize());
    if (!cachePath.empty()) {
        FILE* f = _wfopen(widen(cachePath).c_str(), L"wb");
        if (f) {
            fwrite(bytecode.data(), 1, bytecode.size(), f);
            fclose(f);
        }
    }
    return true;
}

}  // namespace spindle
