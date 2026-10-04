// The interactive viewer: Win32 window, D3D11 swap chain, Dear ImGui panel,
// progressive-accumulation viewport, async loading and the export dialog.
#include "app.h"

#include "cli.h"
#include "exporter.h"
#include "gpu.h"
#include "renderer.h"

#include "imgui.h"
#include "imgui_impl_dx11.h"
#include "imgui_impl_win32.h"

#include <commdlg.h>
#include <dwmapi.h>
#include <shellapi.h>

#include <atomic>
#include <chrono>
#include <cstring>
#include <functional>
#include <thread>

#include "../third_party/nlohmann/json.hpp"
#include "../third_party/stb/stb_image_write.h"

extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam);

namespace spindle {

using json = nlohmann::json;
using Clock = std::chrono::steady_clock;

// ---------------------------------------------------------------------------
// Settings (per user, %APPDATA%\Spindle\settings.json)
// ---------------------------------------------------------------------------

struct Settings {
    std::vector<std::string> recentFiles;
    std::string gpu = "auto";
    std::string ffmpegPath;
    bool darkTheme = true;
    int paceMs = 0;
    int viewportSamples = 64;
    std::string lastExportDir;

    void load(const std::string& path) {
        std::string text;
        if (!readTextFile(path, text)) return;
        json j = json::parse(text, nullptr, false);
        if (!j.is_object()) return;
        if (j.contains("recentFiles") && j["recentFiles"].is_array())
            for (auto& f : j["recentFiles"])
                if (f.is_string()) recentFiles.push_back(f.get<std::string>());
        gpu = j.value("gpu", gpu);
        ffmpegPath = j.value("ffmpegPath", ffmpegPath);
        darkTheme = j.value("darkTheme", darkTheme);
        paceMs = std::max(0, std::min(5000, j.value("paceMs", paceMs)));
        viewportSamples = std::max(1, std::min(1024, j.value("viewportSamples", viewportSamples)));
        lastExportDir = j.value("lastExportDir", lastExportDir);
    }
    void save(const std::string& path) const {
        json j = {{"recentFiles", recentFiles}, {"gpu", gpu}, {"ffmpegPath", ffmpegPath}, {"darkTheme", darkTheme},
                  {"paceMs", paceMs}, {"viewportSamples", viewportSamples}, {"lastExportDir", lastExportDir}};
        writeTextFile(path, j.dump(2));
    }
    void addRecent(const std::string& file) {
        recentFiles.erase(std::remove(recentFiles.begin(), recentFiles.end(), file), recentFiles.end());
        recentFiles.insert(recentFiles.begin(), file);
        if (recentFiles.size() > 10) recentFiles.resize(10);
    }
};

// ---------------------------------------------------------------------------
// Small Win32 helpers
// ---------------------------------------------------------------------------

static std::string openFileDialog(HWND owner, const wchar_t* filter, const wchar_t* title) {
    wchar_t buf[4096] = L"";
    OPENFILENAMEW ofn = {};
    ofn.lStructSize = sizeof(ofn);
    ofn.hwndOwner = owner;
    ofn.lpstrFilter = filter;
    ofn.lpstrFile = buf;
    ofn.nMaxFile = 4096;
    ofn.lpstrTitle = title;
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_EXPLORER;
    return GetOpenFileNameW(&ofn) ? narrow(buf) : std::string();
}

static std::string saveFileDialog(HWND owner, const wchar_t* filter, const wchar_t* defExt, const std::string& suggested,
                                  const wchar_t* title) {
    wchar_t buf[4096] = L"";
    std::wstring s = widen(suggested);
    wcsncpy(buf, s.c_str(), 4095);
    OPENFILENAMEW ofn = {};
    ofn.lStructSize = sizeof(ofn);
    ofn.hwndOwner = owner;
    ofn.lpstrFilter = filter;
    ofn.lpstrFile = buf;
    ofn.nMaxFile = 4096;
    ofn.lpstrDefExt = defExt;
    ofn.lpstrTitle = title;
    ofn.Flags = OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_EXPLORER;
    return GetSaveFileNameW(&ofn) ? narrow(buf) : std::string();
}

static std::string stemOf(const std::string& path) {
    std::string n = fileNameOf(path);
    size_t d = n.find_last_of('.');
    return d == std::string::npos ? n : n.substr(0, d);
}

static std::string dirOf(const std::string& path) {
    size_t s = path.find_last_of("\\/");
    return s == std::string::npos ? std::string() : path.substr(0, s);
}

static std::string formatSeconds(double s) {
    if (s < 0) return "--";
    int t = (int)std::lround(s);
    char b[32];
    if (t >= 3600) std::snprintf(b, sizeof(b), "%dh %02dm", t / 3600, (t / 60) % 60);
    else if (t >= 60) std::snprintf(b, sizeof(b), "%dm %02ds", t / 60, t % 60);
    else std::snprintf(b, sizeof(b), "%ds", t);
    return b;
}

static std::string withThousands(size_t n) {
    std::string s = std::to_string(n);
    for (int i = (int)s.size() - 3; i > 0; i -= 3) s.insert((size_t)i, ",");
    return s;
}

static uint64_t fnv(const void* p, size_t n, uint64_t h = 1469598103934665603ull) {
    const uint8_t* b = (const uint8_t*)p;
    for (size_t i = 0; i < n; ++i) h = (h ^ b[i]) * 1099511628211ull;
    return h;
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

class App {
public:
    int run(const std::vector<std::string>& args);
    LRESULT wndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp);

private:
    // setup
    bool createWindow();
    bool createDevice(std::string& error);
    bool createSwapChain(std::string& error);
    void destroyDevice();
    void recoverDevice();
    void resizeBackBuffer(int w, int h);
    void setupImGuiStyle();
    void rebuildFonts();

    // frame
    void frame();
    void pollAsync();
    void handleViewportInput(const ImVec2& vpMin, const ImVec2& vpSize);
    void handleShortcuts();
    void renderViewport(int x, int y, int w, int h);
    void drawPanel(float x, float y, float w, float h);
    void drawStatusBar(float y, float w, float h);
    void drawExportDialog();
    void drawPopups();

    // panel sections
    void sectionModel();
    void sectionMaterial();
    void sectionEnvironment();
    void sectionCamera();
    void sectionTurntable();
    void sectionExplode();
    void sectionOutput();
    void sectionSettings();

    // actions
    void openModelDialog();
    void loadModel(const std::string& path);
    void reprocessModel();
    void setEnvironmentSource(const std::string& source);
    void chooseTexture(const std::string& path);
    void loadPreset();
    void savePreset();
    void startExport();
    void frameModel();
    void handleDroppedFile(const std::string& path);
    void showError(const std::string& message);
    void notify(const std::string& message);
    float startAzimuth() const;
    void joinWorkers();

    HWND hwnd_ = nullptr;
    int hardwareAdapterCount_ = 0;
    std::string gpuForSession_;  // --gpu on the command line overrides Settings > GPU once
    float dpiScale_ = 1.0f;
    bool fontsDirty_ = false;
    int clientW_ = 0, clientH_ = 0;
    bool minimized_ = false;
    bool quit_ = false;

    Gpu gpu_;
    ComPtr<IDXGISwapChain1> swap_;
    ComPtr<ID3D11RenderTargetView> backRTV_;
    Renderer renderer_;
    RenderTargets viewRT_;
    bool imguiDx11_ = false;

    Settings settings_;
    std::string settingsPath_;
    Scene scene_;

    // Model state (CPU copies are kept for re-processing and device recovery).
    std::string modelPath_;
    TriangleSoup soup_;
    Mesh mesh_;
    std::vector<std::string> modelWarnings_;
    std::thread modelWorker_;
    Progress modelProgress_;
    std::atomic<bool> modelReady_{false};
    bool modelBusy_ = false;
    bool modelIsReload_ = false;
    std::string modelBusyName_;
    LoadResult pendingLoad_;
    Mesh pendingMesh_;
    std::string pendingModelPath_;
    MeshOptions appliedMeshOptions_;

    // Environment state.
    EnvironmentImage env_;
    std::string envSource_;
    std::thread envWorker_;
    std::atomic<bool> envReady_{false};
    bool envBusy_ = false;
    EnvironmentImage pendingEnv_;
    std::string pendingEnvSource_, pendingEnvError_;
    std::vector<std::string> recentHdris_;

    // Texture state.
    Image8 texture_;
    std::string texturePath_;

    // Viewport.
    OrbitCamera cam_;
    int sampleIndex_ = 0;
    uint64_t lastHash_ = 0;
    bool interacting_ = false;
    double sampleMs_ = 0;  // smoothed cost of one sample
    bool panelVisible_ = true;
    bool preview_ = false;
    double previewFrame_ = 0;
    Clock::time_point lastFrameTime_ = Clock::now();
    double fps_ = 0;

    // Test hook: --screenshot FILE saves the window after N frames and exits.
    std::string screenshotPath_;
    int screenshotAfter_ = 0;
    int frameCount_ = 0;
    void saveScreenshot();

    // Export.
    Exporter exporter_;
    bool exportDialog_ = false;
    std::string lastOutput_;

    // Messages.
    std::string errorMessage_;
    bool errorPending_ = false;
    std::string toast_;
    Clock::time_point toastTime_;
};

static App* g_app = nullptr;

static LRESULT CALLBACK StaticWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (g_app) return g_app->wndProc(hwnd, msg, wp, lp);
    return DefWindowProcW(hwnd, msg, wp, lp);
}

bool App::createWindow() {
    HINSTANCE inst = GetModuleHandleW(nullptr);
    WNDCLASSEXW wc = {};
    wc.cbSize = sizeof(wc);
    wc.style = CS_HREDRAW | CS_VREDRAW | CS_DBLCLKS;
    wc.lpfnWndProc = StaticWndProc;
    wc.hInstance = inst;
    wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
    wc.hIcon = LoadIconW(inst, MAKEINTRESOURCEW(1));
    wc.lpszClassName = L"SpindleWindow";
    RegisterClassExW(&wc);

    // Size the window to ~80% of the work area of the primary monitor.
    RECT work;
    SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0);
    int ww = std::min(1600, (int)((work.right - work.left) * 0.85f));
    int wh = std::min(1000, (int)((work.bottom - work.top) * 0.85f));
    int x = work.left + ((work.right - work.left) - ww) / 2;
    int y = work.top + ((work.bottom - work.top) - wh) / 2;
    hwnd_ = CreateWindowExW(WS_EX_ACCEPTFILES, wc.lpszClassName, L"Spindle", WS_OVERLAPPEDWINDOW, x, y, ww, wh, nullptr,
                            nullptr, inst, nullptr);
    if (!hwnd_) return false;
    dpiScale_ = ImGui_ImplWin32_GetDpiScaleForHwnd(hwnd_);
    BOOL dark = settings_.darkTheme ? TRUE : FALSE;
    DwmSetWindowAttribute(hwnd_, 20 /* DWMWA_USE_IMMERSIVE_DARK_MODE */, &dark, sizeof(dark));
    return true;
}

bool App::createDevice(std::string& error) {
    if (!createGpu(gpu_, gpuForSession_.empty() ? settings_.gpu : gpuForSession_, error)) return false;
    if (!createSwapChain(error)) return false;
    if (!renderer_.init(gpu_.device.get(), gpu_.context.get(), gpu_.adapter.dedicatedVideoMemoryMB, error)) return false;
    imguiDx11_ = ImGui_ImplDX11_Init(gpu_.device.get(), gpu_.context.get());
    return true;
}

bool App::createSwapChain(std::string& error) {
    RECT rc;
    GetClientRect(hwnd_, &rc);
    clientW_ = std::max(1L, rc.right - rc.left);
    clientH_ = std::max(1L, rc.bottom - rc.top);
    DXGI_SWAP_CHAIN_DESC1 d = {};
    d.Width = (UINT)clientW_;
    d.Height = (UINT)clientH_;
    d.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    d.SampleDesc.Count = 1;
    d.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    d.BufferCount = 2;
    d.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    HRESULT hr = gpu_.factory->CreateSwapChainForHwnd(gpu_.device.get(), hwnd_, &d, nullptr, nullptr, swap_.put());
    if (FAILED(hr)) {
        // Older drivers / remote sessions: fall back to the blt model.
        d.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
        d.BufferCount = 1;
        hr = gpu_.factory->CreateSwapChainForHwnd(gpu_.device.get(), hwnd_, &d, nullptr, nullptr, swap_.put());
    }
    if (FAILED(hr)) {
        error = "Could not create the swap chain.";
        return false;
    }
    gpu_.factory->MakeWindowAssociation(hwnd_, DXGI_MWA_NO_ALT_ENTER);
    ComPtr<ID3D11Texture2D> back;
    swap_->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)back.put());
    gpu_.device->CreateRenderTargetView(back.get(), nullptr, backRTV_.put());
    return true;
}

void App::resizeBackBuffer(int w, int h) {
    if (!swap_ || w <= 0 || h <= 0) return;
    clientW_ = w;
    clientH_ = h;
    backRTV_ = nullptr;
    gpu_.context->OMSetRenderTargets(0, nullptr, nullptr);
    swap_->ResizeBuffers(0, (UINT)w, (UINT)h, DXGI_FORMAT_UNKNOWN, 0);
    ComPtr<ID3D11Texture2D> back;
    swap_->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)back.put());
    gpu_.device->CreateRenderTargetView(back.get(), nullptr, backRTV_.put());
}

void App::destroyDevice() {
    if (imguiDx11_) ImGui_ImplDX11_Shutdown();
    imguiDx11_ = false;
    exporter_.onDeviceLost();
    viewRT_.release();
    renderer_.shutdown();
    backRTV_ = nullptr;
    swap_ = nullptr;
    gpu_ = Gpu();
}

// Driver reset or GPU removed: rebuild everything from the CPU-side copies.
void App::recoverDevice() {
    HRESULT reason = gpu_.device ? gpu_.device->GetDeviceRemovedReason() : E_FAIL;
    logf("Device lost (reason 0x%08lx); recreating", (unsigned long)reason);
    destroyDevice();
    std::string err;
    for (int attempt = 0; attempt < 10; ++attempt) {
        if (createDevice(err)) break;
        destroyDevice();
        Sleep(500);
    }
    if (!renderer_.device()) {
        MessageBoxW(hwnd_, widen("The graphics device could not be recreated:\n" + err).c_str(), L"Spindle", MB_ICONERROR);
        quit_ = true;
        return;
    }
    renderer_.setEnvironment(env_, err);
    if (!mesh_.indices.empty()) renderer_.setMesh(mesh_, err);
    if (!texture_.rgba.empty()) renderer_.setUserTexture(&texture_, err);
    if (!exporter_.onDeviceRestored(renderer_, err)) showError("The export could not resume: " + err);
    sampleIndex_ = 0;
    lastHash_ = 0;
    notify("Recovered from a graphics driver reset.");
}

void App::setupImGuiStyle() {
    ImGuiStyle& st = ImGui::GetStyle();
    st = ImGuiStyle();
    if (settings_.darkTheme) ImGui::StyleColorsDark(&st);
    else ImGui::StyleColorsLight(&st);
    st.WindowRounding = 0;
    st.FrameRounding = 4;
    st.GrabRounding = 4;
    st.PopupRounding = 4;
    st.ScrollbarRounding = 6;
    st.WindowBorderSize = 0;
    st.FramePadding = ImVec2(6, 4);
    st.ItemSpacing = ImVec2(8, 6);
    if (settings_.darkTheme) {
        ImVec4* c = st.Colors;
        c[ImGuiCol_WindowBg] = ImVec4(0.11f, 0.12f, 0.14f, 1.0f);
        c[ImGuiCol_Header] = ImVec4(0.20f, 0.22f, 0.26f, 1.0f);
        c[ImGuiCol_HeaderHovered] = ImVec4(0.26f, 0.29f, 0.34f, 1.0f);
        c[ImGuiCol_HeaderActive] = ImVec4(0.30f, 0.34f, 0.40f, 1.0f);
        c[ImGuiCol_Button] = ImVec4(0.22f, 0.25f, 0.30f, 1.0f);
        c[ImGuiCol_ButtonHovered] = ImVec4(0.29f, 0.33f, 0.40f, 1.0f);
        c[ImGuiCol_FrameBg] = ImVec4(0.17f, 0.18f, 0.21f, 1.0f);
        c[ImGuiCol_CheckMark] = ImVec4(0.95f, 0.55f, 0.20f, 1.0f);
        c[ImGuiCol_SliderGrab] = ImVec4(0.95f, 0.55f, 0.20f, 1.0f);
        c[ImGuiCol_SliderGrabActive] = ImVec4(1.0f, 0.65f, 0.30f, 1.0f);
        c[ImGuiCol_PlotHistogram] = ImVec4(0.95f, 0.55f, 0.20f, 1.0f);
    }
    st.ScaleAllSizes(dpiScale_);
}

void App::rebuildFonts() {
    ImGuiIO& io = ImGui::GetIO();
    io.Fonts->Clear();
    float size = std::round(15.0f * dpiScale_);
    wchar_t windir[MAX_PATH];
    GetWindowsDirectoryW(windir, MAX_PATH);
    std::string font = narrow(windir) + "\\Fonts\\segoeui.ttf";
    ImFont* f = nullptr;
    if (GetFileAttributesW(widen(font).c_str()) != INVALID_FILE_ATTRIBUTES)
        f = io.Fonts->AddFontFromFileTTF(font.c_str(), size);
    if (!f) {
        ImFontConfig cfg;
        cfg.SizePixels = std::round(13.0f * dpiScale_);
        io.Fonts->AddFontDefault(&cfg);
    }
    if (imguiDx11_) ImGui_ImplDX11_InvalidateDeviceObjects();  // re-uploaded on the next NewFrame
    setupImGuiStyle();
}

// ---------------------------------------------------------------------------
// Async work
// ---------------------------------------------------------------------------

void App::joinWorkers() {
    modelProgress_.cancel = true;
    if (modelWorker_.joinable()) modelWorker_.join();
    if (envWorker_.joinable()) envWorker_.join();
}

void App::loadModel(const std::string& path) {
    if (modelBusy_) {
        modelProgress_.cancel = true;
        if (modelWorker_.joinable()) modelWorker_.join();
    }
    modelProgress_.value = 0;
    modelProgress_.cancel = false;
    modelReady_ = false;
    modelBusy_ = true;
    modelIsReload_ = false;
    modelBusyName_ = fileNameOf(path);
    pendingModelPath_ = path;
    MeshOptions opts;
    opts.up = scene_.model.up;
    opts.creaseAngleDeg = scene_.model.creaseAngle;
    for (int i = 0; i < 3; ++i) opts.quarterTurns[i] = scene_.model.quarterTurns[i];
    appliedMeshOptions_ = opts;
    if (modelWorker_.joinable()) modelWorker_.join();
    modelWorker_ = std::thread([this, path, opts] {
        pendingLoad_ = loadModelFile(path, &modelProgress_);
        if (pendingLoad_.ok && !modelProgress_.cancel) pendingMesh_ = processMesh(pendingLoad_.soup, opts);
        modelReady_ = true;
        PostMessageW(hwnd_, WM_NULL, 0, 0);  // wake the message loop
    });
}

void App::reprocessModel() {
    if (soup_.positions.empty() || modelBusy_) return;
    MeshOptions opts;
    opts.up = scene_.model.up;
    opts.creaseAngleDeg = scene_.model.creaseAngle;
    for (int i = 0; i < 3; ++i) opts.quarterTurns[i] = scene_.model.quarterTurns[i];
    appliedMeshOptions_ = opts;
    modelBusy_ = true;
    modelIsReload_ = true;
    modelReady_ = false;
    modelBusyName_ = fileNameOf(modelPath_);
    if (modelWorker_.joinable()) modelWorker_.join();
    modelWorker_ = std::thread([this, opts] {
        pendingLoad_ = LoadResult();
        pendingLoad_.ok = true;
        pendingMesh_ = processMesh(soup_, opts);
        modelReady_ = true;
        PostMessageW(hwnd_, WM_NULL, 0, 0);
    });
}

void App::setEnvironmentSource(const std::string& source) {
    if (envBusy_) return;
    envBusy_ = true;
    envReady_ = false;
    pendingEnvSource_ = source;
    if (envWorker_.joinable()) envWorker_.join();
    envWorker_ = std::thread([this, source] {
        pendingEnvError_.clear();
        pendingEnv_ = EnvironmentImage();
        if (isProceduralEnvironment(source)) makeProceduralEnvironment(source, pendingEnv_);
        else loadEnvironmentFile(source, pendingEnv_, pendingEnvError_);
        envReady_ = true;
        PostMessageW(hwnd_, WM_NULL, 0, 0);
    });
}

void App::pollAsync() {
    if (modelBusy_ && modelReady_) {
        modelWorker_.join();
        modelBusy_ = false;
        if (!pendingLoad_.ok) {
            if (!modelProgress_.cancel) showError(fileNameOf(pendingModelPath_) + ": " + pendingLoad_.error);
        } else {
            std::string err;
            if (!renderer_.setMesh(pendingMesh_, err)) {
                showError("Could not upload the model to the GPU: " + err);
            } else {
                bool first = mesh_.indices.empty() || !modelIsReload_;
                mesh_ = std::move(pendingMesh_);
                if (!modelIsReload_) {
                    soup_ = std::move(pendingLoad_.soup);
                    modelPath_ = pendingModelPath_;
                    modelWarnings_ = pendingLoad_.warnings;
                    settings_.addRecent(modelPath_);
                    settings_.save(settingsPath_);
                    SetWindowTextW(hwnd_, widen("Spindle - " + fileNameOf(modelPath_)).c_str());
                }
                if (first) frameModel();
            }
        }
        pendingLoad_ = LoadResult();
        pendingMesh_ = Mesh();
        lastHash_ = 0;
    }
    if (envBusy_ && envReady_) {
        envWorker_.join();
        envBusy_ = false;
        if (!pendingEnvError_.empty()) {
            showError(pendingEnvError_);
            scene_.environment.source = envSource_;  // keep the previous one
        } else {
            std::string err;
            if (renderer_.setEnvironment(pendingEnv_, err)) {
                env_ = std::move(pendingEnv_);
                envSource_ = pendingEnvSource_;
                scene_.environment.source = envSource_;
                if (!isProceduralEnvironment(envSource_)) {
                    recentHdris_.erase(std::remove(recentHdris_.begin(), recentHdris_.end(), envSource_), recentHdris_.end());
                    recentHdris_.insert(recentHdris_.begin(), envSource_);
                }
            } else {
                showError("Could not build the environment lighting: " + err);
            }
        }
        lastHash_ = 0;
    }
    // A preset or the UI changed the environment source: load it.
    if (!envBusy_ && scene_.environment.source != envSource_) setEnvironmentSource(scene_.environment.source);
    // Texture path changed (preset load or picker).
    if (scene_.material.pattern == Pattern::Image && scene_.material.texturePath != texturePath_) {
        chooseTexture(scene_.material.texturePath);
    }
}

void App::chooseTexture(const std::string& path) {
    texturePath_ = path;
    std::string err;
    Image8 img;
    if (path.empty()) {
        texture_ = Image8();
        renderer_.setUserTexture(nullptr, err);
        return;
    }
    if (!loadImage8(path, img, err)) {
        showError(fileNameOf(path) + ": " + err);
        return;
    }
    texture_ = std::move(img);
    if (!renderer_.setUserTexture(&texture_, err)) showError(err);
    scene_.material.texturePath = path;
    scene_.material.pattern = Pattern::Image;
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

void App::showError(const std::string& message) {
    logf("Error: %s", message.c_str());
    errorMessage_ = message;
    errorPending_ = true;
}

void App::notify(const std::string& message) {
    toast_ = message;
    toastTime_ = Clock::now();
}

void App::openModelDialog() {
    std::string p = openFileDialog(hwnd_, L"3D models (*.stl;*.3mf)\0*.stl;*.3mf\0STL (*.stl)\0*.stl\0"
                                          L"3MF (*.3mf)\0*.3mf\0All files\0*.*\0", L"Open model");
    if (!p.empty()) loadModel(p);
}

void App::loadPreset() {
    std::string p = openFileDialog(hwnd_, L"Spindle scene presets (*.json)\0*.json\0All files\0*.*\0", L"Load scene preset");
    if (p.empty()) return;
    Scene s = scene_;
    std::string err;
    if (!loadSceneFile(p, s, err)) return showError(err);
    bool reprocess = s.model.up != scene_.model.up || s.model.creaseAngle != scene_.model.creaseAngle ||
                     std::memcmp(s.model.quarterTurns, scene_.model.quarterTurns, sizeof(s.model.quarterTurns)) != 0;
    scene_ = s;
    if (reprocess) reprocessModel();
    notify("Loaded preset " + fileNameOf(p));
}

void App::savePreset() {
    std::string p = saveFileDialog(hwnd_, L"Spindle scene presets (*.json)\0*.json\0", L"json",
                                   appDataDir() + "\\preset.json", L"Save scene preset");
    if (p.empty()) return;
    std::string err;
    if (!saveSceneFile(p, scene_, err)) return showError(err);
    notify("Saved preset " + fileNameOf(p));
}

void App::handleDroppedFile(const std::string& path) {
    std::string ext = extensionOf(path);
    if (isSupportedModelExtension(path)) loadModel(path);
    else if (ext == ".hdr" || ext == ".exr") scene_.environment.source = path;
    else if (ext == ".json") {
        Scene s = scene_;
        std::string err;
        if (loadSceneFile(path, s, err)) {
            scene_ = s;
            reprocessModel();
            notify("Loaded preset " + fileNameOf(path));
        } else {
            showError(err);
        }
    } else if (ext == ".png" || ext == ".jpg" || ext == ".jpeg" || ext == ".bmp" || ext == ".tga") {
        chooseTexture(path);
    } else {
        showError("Unsupported file type: " + fileNameOf(path));
    }
}

void App::frameModel() {
    float aspect = (float)std::max(1, viewRT_.width) / (float)std::max(1, viewRT_.height);
    if (viewRT_.width == 0) aspect = 1.5f;
    cam_.frame(renderer_.sphereCenter(), renderer_.framingRadius(scene_), fovYForFocalLength(scene_.camera.focalLength, aspect),
               aspect, scene_.camera.fill);
}

float App::startAzimuth() const { return scene_.turntable.startFromViewport ? cam_.azimuth : scene_.turntable.startAngle; }

void App::startExport() {
    if (!renderer_.hasMesh()) return showError("Open an STL or 3MF file first.");
    if (exporter_.active()) return;
    Format f = scene_.output.format;
    if (formatNeedsFfmpeg(f) && settings_.ffmpegPath.empty()) {
        wchar_t found[MAX_PATH];
        if (!SearchPathW(nullptr, L"ffmpeg.exe", nullptr, MAX_PATH, found, nullptr))
            return showError("This format needs ffmpeg.exe. Install ffmpeg or set its path under Settings.");
    }
    if (f == Format::MP4 && (scene_.output.width % 2 || scene_.output.height % 2))
        return showError("MP4 needs an even width and height.");

    std::string name = (modelPath_.empty() ? "turntable" : stemOf(modelPath_)) + "_turntable" + formatExtension(f);
    std::string dir = !settings_.lastExportDir.empty() ? settings_.lastExportDir : dirOf(modelPath_);
    // Filter strings are "description\0pattern\0\0".
    auto makeFilter = [](const wchar_t* desc, const wchar_t* pattern) {
        std::wstring f = desc;
        f.push_back(L'\0');
        f += pattern;
        f.push_back(L'\0');
        f.push_back(L'\0');
        return f;
    };
    std::wstring filter;
    const wchar_t* defExt = L"mp4";
    switch (f) {
        case Format::PNG: filter = makeFilter(L"PNG sequence (*.png)", L"*.png"); defExt = L"png"; break;
        case Format::GIF: filter = makeFilter(L"GIF animation (*.gif)", L"*.gif"); defExt = L"gif"; break;
        case Format::WebM: filter = makeFilter(L"WebM video (*.webm)", L"*.webm"); defExt = L"webm"; break;
        case Format::ProRes: filter = makeFilter(L"QuickTime ProRes (*.mov)", L"*.mov"); defExt = L"mov"; break;
        default: filter = makeFilter(L"MP4 video (*.mp4)", L"*.mp4"); defExt = L"mp4"; break;
    }
    std::string out = saveFileDialog(hwnd_, filter.c_str(), defExt, dir.empty() ? name : dir + "\\" + name, L"Render turntable to");
    if (out.empty()) return;
    settings_.lastExportDir = dirOf(out);
    settings_.save(settingsPath_);

    ExportJob job;
    job.scene = scene_;
    job.outputPath = out;
    job.startAzimuth = startAzimuth();
    job.ffmpegPath = settings_.ffmpegPath;
    job.paceMs = settings_.paceMs;
    std::string err;
    preview_ = false;
    if (!exporter_.start(renderer_, job, err)) return showError(err);
    lastOutput_ = f == Format::PNG ? pngFramePath(out, 0) : out;
    exportDialog_ = true;
}

// ---------------------------------------------------------------------------
// Window procedure
// ---------------------------------------------------------------------------

LRESULT App::wndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (ImGui::GetCurrentContext() && ImGui_ImplWin32_WndProcHandler(hwnd, msg, wp, lp)) return 1;
    switch (msg) {
        case WM_SIZE:
            minimized_ = wp == SIZE_MINIMIZED;
            if (!minimized_) resizeBackBuffer(LOWORD(lp), HIWORD(lp));
            return 0;
        case WM_GETMINMAXINFO: {
            auto* mmi = (MINMAXINFO*)lp;
            mmi->ptMinTrackSize.x = (LONG)(720 * dpiScale_);
            mmi->ptMinTrackSize.y = (LONG)(480 * dpiScale_);
            return 0;
        }
        case WM_DPICHANGED: {
            dpiScale_ = HIWORD(wp) / 96.0f;
            fontsDirty_ = true;
            const RECT* r = (const RECT*)lp;
            SetWindowPos(hwnd, nullptr, r->left, r->top, r->right - r->left, r->bottom - r->top, SWP_NOZORDER | SWP_NOACTIVATE);
            return 0;
        }
        case WM_DROPFILES: {
            HDROP drop = (HDROP)wp;
            UINT n = DragQueryFileW(drop, 0xFFFFFFFF, nullptr, 0);
            for (UINT i = 0; i < n; ++i) {
                wchar_t path[MAX_PATH * 4];
                DragQueryFileW(drop, i, path, MAX_PATH * 4);
                handleDroppedFile(narrow(path));
            }
            DragFinish(drop);
            return 0;
        }
        case WM_CLOSE:
            if (exporter_.active() &&
                MessageBoxW(hwnd, L"A render is in progress. Cancel it and quit?", L"Spindle", MB_YESNO | MB_ICONQUESTION) != IDYES)
                return 0;
            quit_ = true;
            return 0;
        case WM_DESTROY:
            PostQuitMessage(0);
            return 0;
        default:
            break;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

// ---------------------------------------------------------------------------
// Frame
// ---------------------------------------------------------------------------

void App::handleShortcuts() {
    ImGuiIO& io = ImGui::GetIO();
    if (io.WantTextInput) return;
    if (io.KeyCtrl) {
        if (ImGui::IsKeyPressed(ImGuiKey_O, false)) openModelDialog();
        if (ImGui::IsKeyPressed(ImGuiKey_R, false)) startExport();
        if (ImGui::IsKeyPressed(ImGuiKey_S, false)) savePreset();
        return;
    }
    if (ImGui::IsKeyPressed(ImGuiKey_F, false)) frameModel();
    if (ImGui::IsKeyPressed(ImGuiKey_Tab, false)) panelVisible_ = !panelVisible_;
    if (ImGui::IsKeyPressed(ImGuiKey_Space, false)) {
        preview_ = !preview_;
        previewFrame_ = 0;
    }
    if (ImGui::IsKeyPressed(ImGuiKey_1, false)) { cam_.azimuth = -90; cam_.elevation = 0; }
    if (ImGui::IsKeyPressed(ImGuiKey_3, false)) { cam_.azimuth = 0; cam_.elevation = 0; }
    if (ImGui::IsKeyPressed(ImGuiKey_7, false)) { cam_.azimuth = -90; cam_.elevation = 89; }
}

void App::handleViewportInput(const ImVec2& vpMin, const ImVec2& vpSize) {
    ImGuiIO& io = ImGui::GetIO();
    interacting_ = false;
    if (io.WantCaptureMouse) return;
    ImVec2 m = io.MousePos;
    bool inside = m.x >= vpMin.x && m.y >= vpMin.y && m.x < vpMin.x + vpSize.x && m.y < vpMin.y + vpSize.y;
    float fovY = fovYForFocalLength(scene_.camera.focalLength, vpSize.x / std::max(1.0f, vpSize.y));

    bool orbiting = ImGui::IsMouseDragging(ImGuiMouseButton_Left, 0) && inside;
    bool panning = (ImGui::IsMouseDragging(ImGuiMouseButton_Right, 0) || ImGui::IsMouseDragging(ImGuiMouseButton_Middle, 0)) && inside;
    if (orbiting) {
        cam_.orbit(-io.MouseDelta.x * 0.35f, io.MouseDelta.y * 0.35f);
        preview_ = false;
    }
    if (panning) cam_.pan(io.MouseDelta.x, io.MouseDelta.y, vpSize.y, fovY);
    if (inside && io.MouseWheel != 0) {
        // Zoom towards the point under the cursor on the plane through the target.
        float before = cam_.distance;
        cam_.zoom(io.MouseWheel);
        float k = 1.0f - cam_.distance / before;
        vec3 fwd = normalize(cam_.target - cam_.eye());
        vec3 right = normalize(cross(fwd, vec3(0, 0, 1)));
        vec3 up = cross(right, fwd);
        float nx = ((m.x - vpMin.x) / vpSize.x) * 2 - 1, ny = 1 - ((m.y - vpMin.y) / vpSize.y) * 2;
        float halfH = before * std::tan(fovY * 0.5f), halfW = halfH * vpSize.x / vpSize.y;
        cam_.target += (right * (nx * halfW) + up * (ny * halfH)) * k;
    }
    if (inside && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) frameModel();
    interacting_ = orbiting || panning || io.MouseWheel != 0;
}

void App::renderViewport(int x, int y, int w, int h) {
    if (w < 8 || h < 8) return;
    ID3D11DeviceContext* ctx = gpu_.context.get();
    D3D11_VIEWPORT vp = {(float)x, (float)y, (float)w, (float)h, 0, 1};

    // During an export the viewport shows the frames being rendered.
    if (exporter_.active() || (exportDialog_ && exporter_.hasPreview())) {
        RenderTargets& ert = exporter_.targets();
        if (ert.outSRV) {
            float s = std::min((float)w / ert.width, (float)h / ert.height);
            float dw = ert.width * s, dh = ert.height * s;
            D3D11_VIEWPORT evp = {x + (w - dw) * 0.5f, y + (h - dh) * 0.5f, dw, dh, 0, 1};
            renderer_.blit(ert, backRTV_.get(), evp,
                           scene_.environment.background == Background::Transparent);
        }
        return;
    }

    int rw = w, rh = h;
    if (preview_) {
        // Letterbox to the export aspect ratio: what you see is what you export.
        float aspect = (float)scene_.output.width / (float)scene_.output.height;
        if ((float)w / h > aspect) rw = (int)(h * aspect);
        else rh = (int)(w / aspect);
        vp = {(float)(x + (w - rw) / 2), (float)(y + (h - rh) / 2), (float)rw, (float)rh, 0, 1};
    }
    std::string err;
    if (!renderer_.ensureTargets(viewRT_, rw, rh, err)) {
        showError(err);
        return;
    }

    // Anything that changes the image restarts the accumulation.
    std::string sj = sceneToJson(scene_);
    uint64_t h64 = fnv(sj.data(), sj.size());
    float camState[7] = {cam_.azimuth, cam_.elevation, cam_.distance, cam_.target.x, cam_.target.y, cam_.target.z,
                         (float)previewFrame_};
    h64 = fnv(camState, sizeof(camState), h64);
    int dims[3] = {rw, rh, preview_ ? 1 : 0};
    h64 = fnv(dims, sizeof(dims), h64);
    if (h64 != lastHash_) {
        lastHash_ = h64;
        sampleIndex_ = 0;
    }

    const int maxSamples = preview_ ? 1 : settings_.viewportSamples;
    // Drop AO and soft shadows while moving on a slow GPU (adaptive quality).
    bool full = !(interacting_ && sampleMs_ > 33.0);
    auto start = Clock::now();
    while (sampleIndex_ < maxSamples) {
        SampleInput in;
        if (preview_)
            in = makeTurntableSample(scene_, renderer_.sphereCenter(), renderer_.framingRadius(scene_), rw, rh, previewFrame_, 0, 1,
                                     startAzimuth());
        else
            in = makeOrbitSample(scene_, cam_, renderer_.framingRadius(scene_), rw, rh, sampleIndex_);
        in.fullQuality = full;
        auto t0 = Clock::now();
        renderer_.renderSample(viewRT_, scene_, in);
        ctx->Flush();
        double ms = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
        sampleMs_ = sampleMs_ * 0.9 + ms * 0.1;
        ++sampleIndex_;
        renderer_.resolve(viewRT_, sampleIndex_, false);
        // Keep the UI at interactive rates: refine for at most ~12 ms per frame.
        if (std::chrono::duration<double, std::milli>(Clock::now() - start).count() > 12.0 || sampleIndex_ == 1) break;
    }
    renderer_.blit(viewRT_, backRTV_.get(), vp, scene_.environment.background == Background::Transparent);
}

void App::frame() {
    if (fontsDirty_) {
        fontsDirty_ = false;
        rebuildFonts();
    }
    auto now = Clock::now();
    double dt = std::chrono::duration<double>(now - lastFrameTime_).count();
    lastFrameTime_ = now;
    fps_ = fps_ * 0.95 + (dt > 0 ? 1.0 / dt : 0) * 0.05;

    pollAsync();
    if (exporter_.active()) {
        exporter_.step(renderer_, 30.0);
        if (!exporter_.active() && !exporter_.succeeded() && !exporter_.cancelled()) showError(exporter_.error());
    }
    if (preview_) {
        previewFrame_ += dt * scene_.turntable.fps;
        previewFrame_ = std::fmod(previewFrame_, (double)scene_.turntable.frameCount());
    }

    ImGui_ImplDX11_NewFrame();
    ImGui_ImplWin32_NewFrame();
    ImGui::NewFrame();

    const float W = (float)clientW_, H = (float)clientH_;
    const float statusH = std::round(26 * dpiScale_);
    const float panelW = panelVisible_ ? std::round(370 * dpiScale_) : 0.0f;
    ImVec2 vpMin(0, 0), vpSize(W - panelW, H - statusH);

    handleShortcuts();
    handleViewportInput(vpMin, vpSize);
    if (panelVisible_) drawPanel(W - panelW, 0, panelW, H - statusH);
    drawStatusBar(H - statusH, W, statusH);
    drawExportDialog();
    drawPopups();

    // Hints over an empty viewport.
    ImDrawList* fg = ImGui::GetForegroundDrawList();
    if (!renderer_.hasMesh() && !modelBusy_) {
        const char* hint = "Drop an STL or 3MF file here, or press Ctrl+O";
        ImVec2 ts = ImGui::CalcTextSize(hint);
        fg->AddText(ImVec2(vpSize.x * 0.5f - ts.x * 0.5f, vpSize.y * 0.5f - ts.y * 0.5f), IM_COL32(220, 220, 225, 220), hint);
    }
    if (modelBusy_) {
        char b[256];
        std::snprintf(b, sizeof(b), "Loading %s ... %d%%", modelBusyName_.c_str(), (int)(modelProgress_.value * 100));
        ImVec2 ts = ImGui::CalcTextSize(b);
        fg->AddRectFilled(ImVec2(vpSize.x * 0.5f - ts.x * 0.5f - 12, vpSize.y * 0.5f - ts.y - 8),
                          ImVec2(vpSize.x * 0.5f + ts.x * 0.5f + 12, vpSize.y * 0.5f + 8), IM_COL32(20, 22, 26, 220), 6);
        fg->AddText(ImVec2(vpSize.x * 0.5f - ts.x * 0.5f, vpSize.y * 0.5f - ts.y * 0.5f - 4), IM_COL32(240, 240, 240, 255), b);
    }
    if (!toast_.empty() && std::chrono::duration<double>(Clock::now() - toastTime_).count() < 3.0) {
        ImVec2 ts = ImGui::CalcTextSize(toast_.c_str());
        ImVec2 p(16 * dpiScale_, vpSize.y - ts.y - 24 * dpiScale_);
        fg->AddRectFilled(ImVec2(p.x - 8, p.y - 6), ImVec2(p.x + ts.x + 8, p.y + ts.y + 6), IM_COL32(20, 22, 26, 230), 6);
        fg->AddText(p, IM_COL32(240, 240, 240, 255), toast_.c_str());
    }
    if (preview_) {
        const char* t = "Turntable preview - Space to stop";
        fg->AddText(ImVec2(16 * dpiScale_, 12 * dpiScale_), IM_COL32(240, 240, 240, 200), t);
    }

    ImGui::Render();

    const float clear[4] = {0.07f, 0.075f, 0.085f, 1.0f};
    gpu_.context->ClearRenderTargetView(backRTV_.get(), clear);
    renderViewport(0, 0, (int)vpSize.x, (int)vpSize.y);
    gpu_.context->OMSetRenderTargets(1, backRTV_.addressOf(), nullptr);
    D3D11_VIEWPORT full = {0, 0, W, H, 0, 1};
    gpu_.context->RSSetViewports(1, &full);
    ImGui_ImplDX11_RenderDrawData(ImGui::GetDrawData());

    if (!screenshotPath_.empty() && ++frameCount_ >= screenshotAfter_ && !modelBusy_ && !envBusy_) {
        saveScreenshot();
        quit_ = true;
    }
    HRESULT hr = swap_->Present(1, 0);
    if (hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET) recoverDevice();
}

// ---------------------------------------------------------------------------
// Panel
// ---------------------------------------------------------------------------

static bool colorEdit(const char* label, vec3& c) {
    float v[3] = {c.x, c.y, c.z};
    bool changed = ImGui::ColorEdit3(label, v, ImGuiColorEditFlags_NoInputs | ImGuiColorEditFlags_PickerHueWheel);
    if (changed) c = {v[0], v[1], v[2]};
    return changed;
}

template <class E>
static bool enumCombo(const char* label, E& value, int count, const char* (*name)(E)) {
    bool changed = false;
    if (ImGui::BeginCombo(label, name(value))) {
        for (int i = 0; i < count; ++i) {
            bool sel = (int)value == i;
            if (ImGui::Selectable(name((E)i), sel)) {
                value = (E)i;
                changed = true;
            }
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    return changed;
}

static void helpMarker(const char* text) {
    ImGui::SameLine();
    ImGui::TextDisabled("(?)");
    if (ImGui::BeginItemTooltip()) {
        ImGui::PushTextWrapPos(ImGui::GetFontSize() * 28.0f);
        ImGui::TextUnformatted(text);
        ImGui::PopTextWrapPos();
        ImGui::EndTooltip();
    }
}

void App::drawPanel(float x, float y, float w, float h) {
    ImGui::SetNextWindowPos(ImVec2(x, y));
    ImGui::SetNextWindowSize(ImVec2(w, h));
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoMove |
                             ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoBringToFrontOnFocus;
    ImGui::Begin("##panel", nullptr, flags);
    ImGui::PushItemWidth(-ImGui::GetFontSize() * 8.5f);

    float bw = (ImGui::GetContentRegionAvail().x - ImGui::GetStyle().ItemSpacing.x * 2) / 3;
    if (ImGui::Button("Open model...", ImVec2(bw, 0))) openModelDialog();
    ImGui::SameLine();
    if (ImGui::Button("Load preset", ImVec2(bw, 0))) loadPreset();
    ImGui::SameLine();
    if (ImGui::Button("Save preset", ImVec2(bw, 0))) savePreset();
    if (!settings_.recentFiles.empty()) {
        if (ImGui::BeginCombo("##recent", "Recent files", ImGuiComboFlags_HeightLarge)) {
            for (auto& f : settings_.recentFiles)
                if (ImGui::Selectable(f.c_str())) {
                    std::string path = f;  // the list may change inside loadModel
                    loadModel(path);
                    break;
                }
            ImGui::EndCombo();
        }
    }
    ImGui::Spacing();

    sectionModel();
    sectionMaterial();
    sectionEnvironment();
    sectionCamera();
    sectionTurntable();
    sectionExplode();
    sectionOutput();
    sectionSettings();

    ImGui::PopItemWidth();
    ImGui::End();
}

void App::sectionModel() {
    if (!ImGui::CollapsingHeader("Model", ImGuiTreeNodeFlags_DefaultOpen)) return;
    if (mesh_.indices.empty()) {
        ImGui::TextDisabled("No model loaded");
    } else {
        ImGui::TextUnformatted(fileNameOf(modelPath_).c_str());
        vec3 s = mesh_.size();
        ImGui::TextDisabled("%s triangles   %.1f x %.1f x %.1f mm", withThousands(mesh_.triangleCount()).c_str(), s.x, s.y, s.z);
    }
    auto& mo = scene_.model;
    int up = (int)mo.up;
    bool changed = false;
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted("Up axis");
    ImGui::SameLine();
    changed |= ImGui::RadioButton("Z (printing)", &up, 0);
    ImGui::SameLine();
    changed |= ImGui::RadioButton("Y", &up, 1);
    mo.up = (UpAxis)up;
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted("Rotate 90");
    const char* axes[] = {"X", "Y", "Z"};
    for (int i = 0; i < 3; ++i) {
        ImGui::SameLine();
        if (ImGui::Button(axes[i])) {
            mo.quarterTurns[i] = (mo.quarterTurns[i] + 1) & 3;
            changed = true;
        }
    }
    ImGui::SameLine();
    if (ImGui::Button("Reset")) {
        mo.quarterTurns[0] = mo.quarterTurns[1] = mo.quarterTurns[2] = 0;
        changed = true;
    }
    ImGui::SliderFloat("Smoothing angle", &mo.creaseAngle, 0.0f, 180.0f, "%.0f deg");
    if (ImGui::IsItemDeactivatedAfterEdit()) changed = true;
    helpMarker("Edges sharper than this stay crisp; softer ones are smoothed. 0 = faceted, 180 = fully smooth.");
    if (changed) reprocessModel();
    if (mesh_.parts.size() > 1 && ImGui::TreeNode("##parts", "%d parts", (int)mesh_.parts.size())) {
        int shown = 0;
        for (const MeshPart& p : mesh_.parts) {
            if (++shown > 200) {
                ImGui::TextDisabled("... and %d more", (int)mesh_.parts.size() - 200);
                break;
            }
            ImGui::BulletText("%s", p.name.c_str());
            ImGui::SameLine();
            ImGui::TextDisabled("%s tris", withThousands(p.triangles).c_str());
        }
        ImGui::TreePop();
    }
    ImGui::PushTextWrapPos(0.0f);
    for (auto& w : modelWarnings_) ImGui::TextColored(ImVec4(1.0f, 0.75f, 0.3f, 1.0f), "! %s", w.c_str());
    ImGui::PopTextWrapPos();
    ImGui::Spacing();
}

void App::sectionMaterial() {
    if (!ImGui::CollapsingHeader("Material", ImGuiTreeNodeFlags_DefaultOpen)) return;
    auto& m = scene_.material;
    if (ImGui::BeginCombo("Preset", m.preset.c_str(), ImGuiComboFlags_HeightLarge)) {
        for (auto& name : materialPresetNames())
            if (ImGui::Selectable(name.c_str(), name == m.preset)) applyMaterialPreset(name, m);
        ImGui::EndCombo();
    }
    if (mesh_.hasFileColors) {
        ImGui::Checkbox("Colours from file", &m.useFileColors);
        helpMarker("Use the colours stored in the 3MF (materials, colour groups, slicer filaments and painting). "
                   "Parts without a colour use the colour below.");
    }
    if (mesh_.hasFilePbr) ImGui::Checkbox("Metal / roughness from file", &m.useFileFinish);
    colorEdit(mesh_.hasFileColors && m.useFileColors ? "Colour (unpainted parts)" : "Colour", m.baseColor);
    ImGui::SliderFloat("Roughness", &m.roughness, 0.0f, 1.0f);
    ImGui::SliderFloat("Metalness", &m.metalness, 0.0f, 1.0f);
    ImGui::SliderFloat("Clearcoat", &m.clearcoat, 0.0f, 1.0f);
    if (m.clearcoat > 0) ImGui::SliderFloat("Coat roughness", &m.clearcoatRoughness, 0.0f, 1.0f);
    ImGui::SliderFloat("Sheen", &m.sheen, 0.0f, 1.0f);
    ImGui::SliderFloat("Transmission", &m.transmission, 0.0f, 1.0f);
    helpMarker("Approximate glass/resin: refracts the environment only, no caustics or inter-reflections.");
    if (m.transmission > 0) {
        ImGui::SliderFloat("IOR", &m.ior, 1.0f, 2.5f);
        colorEdit("Tint", m.transmissionTint);
    }
    if (ImGui::TreeNodeEx("Surface detail", ImGuiTreeNodeFlags_DefaultOpen)) {
        ImGui::Checkbox("FDM layer lines", &m.layerLines);
        if (m.layerLines) {
            ImGui::SliderFloat("Layer height", &m.layerHeight, 0.04f, 0.6f, "%.2f mm");
            ImGui::SliderFloat("Layer depth", &m.layerDepth, 0.0f, 1.0f);
        }
        ImGui::SliderFloat("Noise bump", &m.noiseStrength, 0.0f, 1.0f);
        if (m.noiseStrength > 0) {
            ImGui::SliderFloat("Noise scale", &m.noiseScale, 0.05f, 20.0f, "%.2f mm", ImGuiSliderFlags_Logarithmic);
            ImGui::SliderFloat("Noise stretch", &m.noiseStretch, 1.0f, 100.0f, "%.0fx", ImGuiSliderFlags_Logarithmic);
        }
        ImGui::TreePop();
    }
    if (ImGui::TreeNodeEx("Pattern / texture", ImGuiTreeNodeFlags_DefaultOpen)) {
        Pattern before = m.pattern;
        enumCombo("Pattern", m.pattern, (int)Pattern::Count, patternName);
        if (m.pattern == Pattern::Image && before != Pattern::Image && texturePath_.empty()) {
            std::string p = openFileDialog(hwnd_, L"Images (*.png;*.jpg;*.jpeg;*.bmp;*.tga)\0*.png;*.jpg;*.jpeg;*.bmp;*.tga\0",
                                           L"Choose a texture");
            if (p.empty()) m.pattern = before;
            else chooseTexture(p);
        }
        if (m.pattern == Pattern::Image) {
            ImGui::TextDisabled("%s", texturePath_.empty() ? "(no image)" : fileNameOf(texturePath_).c_str());
            ImGui::SameLine();
            if (ImGui::SmallButton("Change...")) {
                std::string p = openFileDialog(hwnd_, L"Images (*.png;*.jpg;*.jpeg;*.bmp;*.tga)\0*.png;*.jpg;*.jpeg;*.bmp;*.tga\0",
                                               L"Choose a texture");
                if (!p.empty()) chooseTexture(p);
            }
        }
        if (m.pattern != Pattern::None) {
            if (m.pattern != Pattern::Image) colorEdit("Pattern colour", m.patternColor);
            ImGui::SliderFloat("Scale", &m.patternScale, 0.5f, 200.0f, "%.1f mm", ImGuiSliderFlags_Logarithmic);
            ImGui::SliderFloat("Strength", &m.patternStrength, 0.0f, 1.0f);
            if (m.pattern == Pattern::Image || m.pattern == Pattern::Carbon)
                ImGui::SliderFloat("Blend sharpness", &m.triplanarSharpness, 1.0f, 16.0f);
        }
        ImGui::Checkbox("Height gradient", &m.gradient);
        if (m.gradient) {
            ImGui::SameLine();
            colorEdit("##grad", m.gradientColor);
        }
        ImGui::Checkbox("Wireframe overlay", &m.wireframe);
        if (m.wireframe) {
            ImGui::SameLine();
            colorEdit("##wire", m.wireColor);
            ImGui::SliderFloat("Line width", &m.wireWidth, 0.5f, 4.0f, "%.1f px");
        }
        ImGui::TreePop();
    }
    ImGui::Spacing();
}

void App::sectionEnvironment() {
    if (!ImGui::CollapsingHeader("Environment", ImGuiTreeNodeFlags_DefaultOpen)) return;
    auto& e = scene_.environment;
    std::string label = isProceduralEnvironment(envSource_) ? envSource_ : fileNameOf(envSource_);
    if (envBusy_) label += " (loading...)";
    if (ImGui::BeginCombo("Lighting", label.c_str(), ImGuiComboFlags_HeightLarge)) {
        for (auto& n : proceduralEnvironmentNames())
            if (ImGui::Selectable(n.c_str(), n == envSource_)) e.source = n;
        ImGui::Separator();
        for (auto& p : recentHdris_)
            if (ImGui::Selectable(fileNameOf(p).c_str(), p == envSource_)) e.source = p;
        if (ImGui::Selectable("Load HDRI (.hdr)...")) {
            std::string p = openFileDialog(hwnd_, L"Radiance HDR (*.hdr)\0*.hdr\0All files\0*.*\0", L"Load HDRI");
            if (!p.empty()) e.source = p;
        }
        ImGui::EndCombo();
    }
    ImGui::SliderFloat("Rotation", &e.rotation, -180.0f, 180.0f, "%.0f deg");
    ImGui::SliderFloat("Intensity", &e.intensity, 0.0f, 4.0f);
    ImGui::SliderFloat("Saturation", &e.saturation, 0.0f, 2.0f);

    enumCombo("Background", e.background, (int)Background::Count, backgroundName);
    if (e.background == Background::Environment) ImGui::SliderFloat("Blur", &e.backgroundBlur, 0.0f, 1.0f);
    if (e.background == Background::Solid) colorEdit("Background colour", e.backgroundColor);
    if (e.background == Background::Gradient || e.background == Background::Radial) {
        colorEdit(e.background == Background::Gradient ? "Top" : "Centre", e.backgroundColor);
        ImGui::SameLine();
        colorEdit(e.background == Background::Gradient ? "Bottom" : "Edge", e.backgroundColor2);
    }

    enumCombo("Ground", e.ground, (int)Ground::Count, groundName);
    if (e.ground == Ground::Floor || e.ground == Ground::Reflective) {
        colorEdit("Floor colour", e.floorColor);
        ImGui::SliderFloat("Floor roughness", &e.floorRoughness, 0.0f, 1.0f);
    }
    if (e.ground == Ground::Reflective) ImGui::SliderFloat("Reflection", &e.reflection, 0.0f, 1.0f);
    ImGui::SliderFloat("Shadow", &e.shadowStrength, 0.0f, 1.0f);
    ImGui::SliderFloat("Shadow softness", &e.shadowSoftness, 0.0f, 30.0f, "%.0f deg");
    ImGui::SliderFloat("Ambient occlusion", &e.aoStrength, 0.0f, 1.0f);
    if (e.aoStrength > 0) ImGui::SliderFloat("AO radius", &e.aoRadius, 0.02f, 0.5f);

    if (ImGui::TreeNodeEx("Light rig", 0)) {
        ImGui::Checkbox("Three-point lights", &e.lights.enabled);
        helpMarker("Key, fill and rim lights on top of the environment. Angles are relative to the camera; the key casts the shadow.");
        if (e.lights.enabled) {
            ImGui::SliderFloat("Light intensity", &e.lights.intensity, 0.0f, 10.0f);
            ImGui::SliderFloat("Key azimuth", &e.lights.keyAzimuth, -180.0f, 180.0f, "%.0f deg");
            ImGui::SliderFloat("Key elevation", &e.lights.keyElevation, 5.0f, 89.0f, "%.0f deg");
            colorEdit("Key colour", e.lights.keyColor);
            ImGui::SliderFloat("Fill", &e.lights.fill, 0.0f, 1.0f);
            ImGui::SliderFloat("Rim", &e.lights.rim, 0.0f, 2.0f);
        }
        ImGui::TreePop();
    }
    enumCombo("Tonemap", e.tonemap, (int)Tonemap::Count, tonemapName);
    ImGui::SliderFloat("Exposure", &e.exposure, -4.0f, 4.0f, "%+.1f EV");
    ImGui::Spacing();
}

void App::sectionCamera() {
    if (!ImGui::CollapsingHeader("Camera")) return;
    auto& c = scene_.camera;
    ImGui::Checkbox("Orthographic", &c.orthographic);
    if (!c.orthographic) ImGui::SliderFloat("Focal length", &c.focalLength, 18.0f, 200.0f, "%.0f mm");
    ImGui::SliderFloat("Elevation", &c.elevation, -30.0f, 80.0f, "%.0f deg");
    ImGui::SliderFloat("Fill frame", &c.fill, 0.3f, 1.0f, "%.2f");
    helpMarker("How much of the frame's short side the model's bounding sphere covers. 0.8 leaves a margin; nothing is clipped at any angle.");
    if (!c.orthographic) {
        ImGui::Checkbox("Depth of field", &c.depthOfField);
        if (c.depthOfField) ImGui::SliderFloat("f-stop", &c.fStop, 0.7f, 22.0f, "f/%.1f", ImGuiSliderFlags_Logarithmic);
    }
    if (ImGui::Button("Frame model (F)")) frameModel();
    ImGui::SameLine();
    if (ImGui::Button("Match viewport to turntable")) {
        cam_.elevation = c.elevation;
        frameModel();
    }
    ImGui::Spacing();
}

void App::sectionTurntable() {
    if (!ImGui::CollapsingHeader("Turntable", ImGuiTreeNodeFlags_DefaultOpen)) return;
    auto& t = scene_.turntable;
    enumCombo("Motion", t.mode, (int)TurntableMode::Count, turntableModeName);
    ImGui::SliderFloat("Duration", &t.seconds, 2.0f, 60.0f, "%.1f s");
    static const int fpsValues[] = {24, 25, 30, 50, 60};
    char fpsLabel[16];
    std::snprintf(fpsLabel, sizeof(fpsLabel), "%d fps", t.fps);
    if (ImGui::BeginCombo("Frame rate", fpsLabel)) {
        for (int v : fpsValues) {
            char b[16];
            std::snprintf(b, sizeof(b), "%d fps", v);
            if (ImGui::Selectable(b, v == t.fps)) t.fps = v;
        }
        ImGui::EndCombo();
    }
    ImGui::SliderInt("Rotations", &t.rotations, 1, 4);
    int dir = t.clockwise ? 1 : 0;
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted("Direction");
    ImGui::SameLine();
    ImGui::RadioButton("Counter-clockwise", &dir, 0);
    ImGui::SameLine();
    ImGui::RadioButton("Clockwise", &dir, 1);
    t.clockwise = dir == 1;
    enumCombo("Easing", t.easing, (int)Easing::Count, easingName);
    if (t.easing == Easing::HoldThenSpin) ImGui::SliderFloat("Hold", &t.holdSeconds, 0.0f, 5.0f, "%.1f s");
    ImGui::Checkbox("Start from current view", &t.startFromViewport);
    if (!t.startFromViewport) ImGui::SliderFloat("Start angle", &t.startAngle, -180.0f, 180.0f, "%.0f deg");
    ImGui::SliderFloat("Vertical bob", &t.bob, 0.0f, 20.0f, "%.0f deg");
    ImGui::SliderFloat("Motion blur", &t.shutter, 0.0f, 1.0f, "%.2f");
    helpMarker("Shutter as a fraction of the frame interval. Needs Standard quality or higher (blur comes from accumulated samples).");
    if (ImGui::Button(preview_ ? "Stop preview (Space)" : "Preview (Space)", ImVec2(-1, 0))) {
        preview_ = !preview_;
        previewFrame_ = 0;
    }
    ImGui::Spacing();
}

void App::sectionExplode() {
    if (!ImGui::CollapsingHeader("Exploded view", ImGuiTreeNodeFlags_DefaultOpen)) return;
    auto& x = scene_.explode;
    if (mesh_.parts.size() < 2) {
        ImGui::TextDisabled("This model is one piece: nothing to explode.");
        ImGui::Spacing();
        return;
    }
    ImGui::TextDisabled("%d parts, outermost leave first", (int)mesh_.parts.size());
    ImGui::SliderFloat("Explode (view)", &x.manual, 0.0f, 1.0f);
    helpMarker("Pulls the parts apart in the viewport and in stills. The turntable animation below is separate.");
    ImGui::SliderFloat("Distance", &x.distance, 0.1f, 4.0f, "%.2fx");
    ImGui::SliderFloat("Stagger", &x.stagger, 0.0f, 1.0f);
    helpMarker("0: all parts move together. 1: parts move one after another, so the assembly opens up slowly.");
    ImGui::Checkbox("Animate during the turntable", &x.animate);
    if (x.animate) {
        enumCombo("Timing", x.timing, (int)ExplodeTiming::Count, explodeTimingName);
        float a = x.start * 100.0f, b = x.end * 100.0f;
        if (ImGui::DragFloatRange2("Between", &a, &b, 0.5f, 0.0f, 100.0f, "%.0f%%", "%.0f%% of the spin")) {
            x.start = clampf(a / 100.0f, 0, 1);
            x.end = clampf(std::max(b, a + 1.0f) / 100.0f, 0, 1);
        }
        ImGui::TextDisabled("Press Space to preview.");
    }
    ImGui::Spacing();
}

void App::sectionOutput() {
    if (!ImGui::CollapsingHeader("Output", ImGuiTreeNodeFlags_DefaultOpen)) return;
    auto& o = scene_.output;
    enumCombo("Format", o.format, (int)Format::Count, formatName);
    char res[64];
    std::snprintf(res, sizeof(res), "%d x %d", o.width, o.height);
    if (ImGui::BeginCombo("Resolution", res)) {
        for (auto& r : resolutionPresets())
            if (ImGui::Selectable(r.name, r.width == o.width && r.height == o.height)) {
                o.width = r.width;
                o.height = r.height;
            }
        ImGui::EndCombo();
    }
    int wh[2] = {o.width, o.height};
    if (ImGui::InputInt2("Custom size", wh)) {
        o.width = std::max(16, std::min(8192, wh[0]));
        o.height = std::max(16, std::min(8192, wh[1]));
    }
    enumCombo("Quality", o.quality, (int)Quality::Count, qualityName);
    helpMarker("Samples per frame: Draft 1, Standard 16, High 64, Ultra 256. More samples = smoother edges, softer shadows, real depth of field and motion blur.");
    if (o.format == Format::MP4) {
        bool autoRate = o.bitrateMbps <= 0;
        if (ImGui::Checkbox("Automatic bitrate", &autoRate)) o.bitrateMbps = autoRate ? 0 : automaticBitrateMbps(o.width, o.height, scene_.turntable.fps);
        if (!autoRate) ImGui::SliderFloat("Bitrate", &o.bitrateMbps, 1.0f, 100.0f, "%.0f Mbps");
    }
    if (scene_.environment.background == Background::Transparent && !formatSupportsAlpha(o.format))
        ImGui::TextColored(ImVec4(1.0f, 0.75f, 0.3f, 1.0f), "This format has no alpha: transparent areas become black.");
    if (o.format == Format::MP4 && (o.width % 2 || o.height % 2))
        ImGui::TextColored(ImVec4(1.0f, 0.5f, 0.4f, 1.0f), "MP4 needs an even width and height.");
    int frames = scene_.turntable.frameCount();
    ImGui::TextDisabled("%d frames x %d samples", frames, qualitySamples(o.quality));
    ImGui::Spacing();
    bool can = renderer_.hasMesh() && !exporter_.active();
    if (!can) ImGui::BeginDisabled();
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0.85f, 0.45f, 0.15f, 1.0f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0.95f, 0.55f, 0.22f, 1.0f));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(1.0f, 0.62f, 0.3f, 1.0f));
    if (ImGui::Button("Render turntable...  (Ctrl+R)", ImVec2(-1, ImGui::GetFrameHeight() * 1.6f))) startExport();
    ImGui::PopStyleColor(3);
    if (!can) ImGui::EndDisabled();
    ImGui::Spacing();
}

void App::sectionSettings() {
    if (!ImGui::CollapsingHeader("Settings")) return;
    static std::vector<AdapterInfo> adapters = listAdapters();
    std::string current = settings_.gpu == "auto" ? "Automatic (high performance)" : settings_.gpu;
    if (ImGui::BeginCombo("GPU", current.c_str())) {
        if (ImGui::Selectable("Automatic (high performance)", settings_.gpu == "auto")) settings_.gpu = "auto";
        for (auto& a : adapters) {
            if (a.software) continue;
            if (ImGui::Selectable(a.name.c_str(), settings_.gpu == a.name)) settings_.gpu = a.name;
        }
        ImGui::EndCombo();
    }
    helpMarker("Takes effect the next time Spindle starts.");
    char buf[1024];
    std::snprintf(buf, sizeof(buf), "%s", settings_.ffmpegPath.c_str());
    if (ImGui::InputText("ffmpeg.exe", buf, sizeof(buf))) settings_.ffmpegPath = buf;
    helpMarker("Optional. Enables WebM, ProRes and H.265 output. Leave empty to use ffmpeg from PATH.");
    ImGui::SliderInt("Viewport samples", &settings_.viewportSamples, 1, 256);
    ImGui::SliderInt("Pause between frames", &settings_.paceMs, 0, 1000, "%d ms");
    helpMarker("Lets a hot laptop cool down during long exports.");
    if (ImGui::Checkbox("Dark theme", &settings_.darkTheme)) {
        setupImGuiStyle();
        BOOL dark = settings_.darkTheme ? TRUE : FALSE;
        DwmSetWindowAttribute(hwnd_, 20, &dark, sizeof(dark));
    }
    if (ImGui::Button("Open log folder")) ShellExecuteW(nullptr, L"open", widen(appDataDir()).c_str(), nullptr, nullptr, SW_SHOWNORMAL);
    ImGui::SameLine();
    if (ImGui::Button("Save settings")) settings_.save(settingsPath_);
    ImGui::Spacing();
}

void App::drawStatusBar(float y, float w, float h) {
    ImGui::SetNextWindowPos(ImVec2(0, y));
    ImGui::SetNextWindowSize(ImVec2(w, h));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * dpiScale_, 4 * dpiScale_));
    ImGui::Begin("##status", nullptr,
                 ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoMove |
                     ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoSavedSettings);
    std::string s;
    if (!mesh_.indices.empty()) {
        vec3 sz = mesh_.size();
        char b[256];
        std::snprintf(b, sizeof(b), "%s  |  %s tris  |  %.1f x %.1f x %.1f mm  |  ", fileNameOf(modelPath_).c_str(),
                      withThousands(mesh_.triangleCount()).c_str(), sz.x, sz.y, sz.z);
        s += b;
    }
    char b[256];
    std::snprintf(b, sizeof(b), "%s (%llu MB)  |  %.0f fps  |  %d/%d samples", gpu_.adapter.name.c_str(),
                  (unsigned long long)gpu_.adapter.dedicatedVideoMemoryMB, fps_, std::min(sampleIndex_, settings_.viewportSamples),
                  preview_ ? 1 : settings_.viewportSamples);
    s += b;
    ImGui::TextUnformatted(s.c_str());
    // Warn when Optimus put us on the integrated GPU.
    if (gpu_.adapter.vendorId == 0x8086 && hardwareAdapterCount_ > 1) {
        ImGui::SameLine();
        ImGui::TextColored(ImVec4(1.0f, 0.7f, 0.3f, 1.0f), "  Running on the integrated GPU - see Settings > GPU");
    }
    ImGui::End();
    ImGui::PopStyleVar();
}

void App::drawExportDialog() {
    if (!exportDialog_) return;
    ImGui::OpenPopup("Rendering turntable");
    ImVec2 center = ImGui::GetMainViewport()->GetCenter();
    ImGui::SetNextWindowPos(center, ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    ImGui::SetNextWindowSize(ImVec2(460 * dpiScale_, 0));
    if (!ImGui::BeginPopupModal("Rendering turntable", nullptr, ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoMove)) return;
    const ExportJob& job = exporter_.job();
    ImGui::TextWrapped("%s", job.outputPath.c_str());
    ImGui::TextDisabled("%d x %d, %s, %d frames x %d samples", job.scene.output.width, job.scene.output.height,
                        formatName(job.scene.output.format), exporter_.totalFrames(), exporter_.samplesPerFrame());
    if (exporter_.active()) {
        char overlay[64];
        std::snprintf(overlay, sizeof(overlay), "frame %d / %d", exporter_.framesRendered(), exporter_.totalFrames());
        ImGui::ProgressBar(exporter_.progress(), ImVec2(-1, 0), overlay);
        ImGui::Text("Elapsed %s   Remaining %s   Encoded %d", formatSeconds(exporter_.elapsedSeconds()).c_str(),
                    formatSeconds(exporter_.etaSeconds()).c_str(), exporter_.framesEncoded());
        if (ImGui::Button("Cancel", ImVec2(-1, 0))) exporter_.cancel();
    } else if (exporter_.succeeded()) {
        ImGui::TextColored(ImVec4(0.5f, 0.9f, 0.5f, 1.0f), "Done in %s.", formatSeconds(exporter_.elapsedSeconds()).c_str());
        float bw = (ImGui::GetContentRegionAvail().x - ImGui::GetStyle().ItemSpacing.x * 2) / 3;
        if (ImGui::Button("Play", ImVec2(bw, 0)))
            ShellExecuteW(nullptr, L"open", widen(lastOutput_).c_str(), nullptr, nullptr, SW_SHOWNORMAL);
        ImGui::SameLine();
        if (ImGui::Button("Show in folder", ImVec2(bw, 0))) {
            std::wstring args = L"/select,\"" + widen(lastOutput_) + L"\"";
            ShellExecuteW(nullptr, L"open", L"explorer.exe", args.c_str(), nullptr, SW_SHOWNORMAL);
        }
        ImGui::SameLine();
        if (ImGui::Button("Close", ImVec2(bw, 0))) {
            exportDialog_ = false;
            ImGui::CloseCurrentPopup();
        }
    } else {
        if (exporter_.cancelled()) {
            ImGui::TextUnformatted(job.scene.output.format == Format::PNG ? "Cancelled. Frames already written were kept."
                                                                         : "Cancelled.");
        } else {
            ImGui::TextColored(ImVec4(1.0f, 0.5f, 0.4f, 1.0f), "Failed: %s", exporter_.error().c_str());
        }
        if (ImGui::Button("Close", ImVec2(-1, 0))) {
            exportDialog_ = false;
            ImGui::CloseCurrentPopup();
        }
    }
    ImGui::EndPopup();
}

void App::drawPopups() {
    if (errorPending_) {
        ImGui::OpenPopup("Spindle##error");
        errorPending_ = false;
    }
    ImVec2 center = ImGui::GetMainViewport()->GetCenter();
    ImGui::SetNextWindowPos(center, ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    if (ImGui::BeginPopupModal("Spindle##error", nullptr, ImGuiWindowFlags_AlwaysAutoResize)) {
        ImGui::PushTextWrapPos(ImGui::GetFontSize() * 30.0f);
        ImGui::TextUnformatted(errorMessage_.c_str());
        ImGui::PopTextWrapPos();
        if (ImGui::Button("OK", ImVec2(120 * dpiScale_, 0))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }
}

void App::saveScreenshot() {
    ComPtr<ID3D11Texture2D> back;
    if (FAILED(swap_->GetBuffer(0, __uuidof(ID3D11Texture2D), (void**)back.put()))) return;
    D3D11_TEXTURE2D_DESC d;
    back->GetDesc(&d);
    d.Usage = D3D11_USAGE_STAGING;
    d.BindFlags = 0;
    d.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    d.MiscFlags = 0;
    ComPtr<ID3D11Texture2D> staging;
    if (FAILED(gpu_.device->CreateTexture2D(&d, nullptr, staging.put()))) return;
    gpu_.context->CopyResource(staging.get(), back.get());
    D3D11_MAPPED_SUBRESOURCE m;
    if (FAILED(gpu_.context->Map(staging.get(), 0, D3D11_MAP_READ, 0, &m))) return;
    std::vector<uint8_t> px((size_t)d.Width * d.Height * 4);
    for (UINT y = 0; y < d.Height; ++y) {
        std::memcpy(&px[(size_t)y * d.Width * 4], (uint8_t*)m.pData + (size_t)y * m.RowPitch, (size_t)d.Width * 4);
        for (UINT x = 0; x < d.Width; ++x) px[((size_t)y * d.Width + x) * 4 + 3] = 255;
    }
    gpu_.context->Unmap(staging.get(), 0);
    stbi_write_png(screenshotPath_.c_str(), (int)d.Width, (int)d.Height, 4, px.data(), (int)d.Width * 4);
    logf("Screenshot written to %s", screenshotPath_.c_str());
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

int App::run(const std::vector<std::string>& args) {
    g_app = this;
    settingsPath_ = appDataDir() + "\\settings.json";
    settings_.load(settingsPath_);
    setShaderCacheDir(localCacheDir());
    ImGui_ImplWin32_EnableDpiAwareness();
    if (!createWindow()) return 3;

    IMGUI_CHECKVERSION();
    ImGui::CreateContext();
    ImGuiIO& io = ImGui::GetIO();
    io.IniFilename = nullptr;
    io.ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
    ImGui_ImplWin32_Init(hwnd_);

    for (size_t i = 0; i + 1 < args.size(); ++i)
        if (args[i] == "--gpu") gpuForSession_ = args[i + 1];  // not saved to settings
    for (auto& a : listAdapters())
        if (!a.software) ++hardwareAdapterCount_;
    std::string err;
    if (!createDevice(err)) {
        MessageBoxW(hwnd_, widen("Spindle needs a Direct3D 11 GPU (feature level 11_0).\n\n" + err).c_str(), L"Spindle",
                    MB_ICONERROR);
        return 3;
    }
    rebuildFonts();
    env_ = EnvironmentImage();
    setEnvironmentSource(scene_.environment.source);
    for (size_t i = 0; i < args.size(); ++i) {
        if (args[i] == "--screenshot" && i + 1 < args.size()) {
            screenshotPath_ = args[++i];
            screenshotAfter_ = 90;
        } else if (args[i] == "--screenshot-after" && i + 1 < args.size()) {
            screenshotAfter_ = std::max(1, std::atoi(args[++i].c_str()));
        } else if (args[i] == "--gpu" && i + 1 < args.size()) {
            ++i;  // handled before device creation
        } else if (args[i] == "--preset" && i + 1 < args.size()) {
            std::string presetErr;
            if (!loadSceneFile(args[++i], scene_, presetErr)) showError(presetErr);
        } else if (!args[i].empty() && args[i][0] != '-') {
            handleDroppedFile(args[i]);
        }
    }

    ShowWindow(hwnd_, SW_SHOWDEFAULT);
    UpdateWindow(hwnd_);

    while (!quit_) {
        MSG msg;
        bool toastVisible = !toast_.empty() && std::chrono::duration<double>(Clock::now() - toastTime_).count() < 3.0;
        bool idle = !exporter_.active() && !preview_ && !modelBusy_ && !envBusy_ && !interacting_ && !toastVisible &&
                    sampleIndex_ >= settings_.viewportSamples;
        // When the image has converged and nothing is running, sleep until input
        // arrives (keeps a laptop cool) but wake periodically for hover effects.
        if (idle || minimized_) MsgWaitForMultipleObjects(0, nullptr, FALSE, minimized_ ? 250 : 100, QS_ALLINPUT);
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) quit_ = true;
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        if (quit_) break;
        if (minimized_ && !exporter_.active()) continue;
        if (minimized_) {
            exporter_.step(renderer_, 200.0);  // keep exporting while minimised
            continue;
        }
        frame();
    }

    exporter_.cancel();
    joinWorkers();
    settings_.save(settingsPath_);
    ImGui_ImplDX11_Shutdown();
    imguiDx11_ = false;
    ImGui_ImplWin32_Shutdown();
    ImGui::DestroyContext();
    viewRT_.release();
    renderer_.shutdown();
    DestroyWindow(hwnd_);
    g_app = nullptr;
    return 0;
}

int runGui(const std::vector<std::string>& args) {
    App app;
    return app.run(args);
}

}  // namespace spindle
