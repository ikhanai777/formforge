# Spindle: STL viewer and turntable renderer for Windows

Spec v1.0. Status: implemented as v0.1 in [`spindle/`](../spindle/); §16 lists where the build differs. Working name: **Spindle**.

A small Windows desktop app that opens any STL file, shows it in 3D with good
materials and lighting, and exports a 360° turntable video. One window, one
model, one export button. It does not edit, slice or convert anything.

It complements FormForge. `formforge render` makes quick numpy previews for the
critique loop. Spindle makes the presentation video for a finished part, and
its headless CLI mode (§9) lets a FormForge bundle produce one automatically.

---

## 1. The target machine, and what it rules out

Every technology choice below follows from this hardware, so the constraints
come first.

| Component | Detail | What it means for the design |
|---|---|---|
| OS | Windows 10 Pro 64-bit, build 19045 (22H2) | Win32 + Direct3D 11 + Media Foundation are all built in, so there is nothing to install. |
| CPU | i7-4910MQ, Haswell, 4C/8T, AVX2 | STL parsing and mesh welding are threaded across 8 workers. Software H.264 encoding at 1080p is fine; at 4K it is slow but acceptable for offline work. |
| RAM | 16 GB DDR3-1600 | Budget is 2 GB peak for a 10M-triangle model, which leaves plenty of headroom. |
| dGPU | Quadro K5100M, Kepler GK104, 8 GB GDDR5, driver 475.14 | D3D11 at feature level 11_0 (SM 5.0) is the safe target. There is **no hardware ray tracing**, OptiX needs Maxwell or newer, and current CUDA toolkits have dropped Kepler. The renderer is a **rasteriser**, and 8 GB of VRAM is generous. |
| iGPU | Intel HD 4600, 1 GB shared | This is the Optimus fallback. The app must run on it at reduced quality, and must make sure it is **not** picked by accident. |

**Rejected options, and why:**

- **CUDA, OptiX or other GPU path tracing.** Kepler (sm_30) has been dropped by
  CUDA 11+ and OptiX 7, and driver R470 is the last branch that supports this
  card. Building on any of them ties the app to toolchains that are already
  frozen.
- **Embedding Blender or Cycles.** It is hundreds of MB, and getting Kepler GPU
  rendering would mean pinning an old Blender release. Rendering on the CPU
  across 4 cores would take minutes per frame.
- **Vulkan, D3D12, wgpu or WebGPU.** These work on R470 but give no benefit for
  a single-object scene, and driver quirks on old hardware are more likely. D3D11
  is the most stable API this card has.
- **NVENC.** Kepler has first-generation NVENC, but current Video Codec SDKs and
  ffmpeg builds require newer drivers and hardware. Media Foundation is used
  instead (§8).
- **Electron, browser or three.js.** Not minimal, and WebGL on a 2014 Optimus
  laptop adds another layer of driver risk.

### 1.1 Forcing the NVIDIA GPU (Optimus)

On this laptop Windows will give the app the Intel GPU unless told otherwise.
All three of the following are required:

1. Export from the exe: `extern "C" __declspec(dllexport) DWORD NvOptimusEnablement = 1;`
   (plus `AmdPowerXpressRequestHighPerformance = 1` at no cost).
2. Enumerate adapters with `IDXGIFactory6::EnumAdapterByGpuPreference(HIGH_PERFORMANCE)`
   where available, otherwise use `EnumAdapters1` and pick the adapter with the most
   `DedicatedVideoMemory`.
3. Show the active adapter name and VRAM in the status bar, and add a
   *Settings → GPU* override. If the app falls back to the HD 4600, show a
   one-line warning.

### 1.2 Avoiding GPU timeouts (TDR)

Windows resets the GPU when a single submission runs longer than 2 s. An
offline frame with 64 accumulation samples on a 10M-triangle mesh could exceed
that on Kepler. So **each accumulation sample is its own submission**: call
`Flush()`, then wait on a query every N samples. No single draw call may cover
more than about 4M triangles; split larger index buffers into chunks.

---

## 2. Scope

### In scope (v1)
- Open binary and ASCII STL by drag and drop, *File → Open*, "Open with", or a command-line argument.
- Real-time PBR viewport with orbit, pan and zoom.
- Material presets, plus editable base colour, roughness, metalness and clearcoat.
- Textures that work without UVs (triplanar), plus procedural surface detail.
- Environments: HDRI image-based lighting, procedural studio setups, ground with shadow or reflection, and background options.
- Turntable export: MP4 (H.264), PNG sequence (with optional alpha) and GIF.
- Scene presets saved and loaded as JSON.
- Headless CLI rendering for batch and pipeline use.

### Out of scope (v1)
- Mesh editing, repair, boolean operations or slicing.
- Multiple objects or assemblies. (OBJ/3MF/PLY import is a v2 candidate; the loader interface allows for it.)
- True refraction or caustics. Glass is approximated (§5.4).
- Path tracing, keyframe animation beyond the turntable, audio.
- macOS and Linux. Windows 7 and 8 are not tested.

---

## 3. Technology stack

| Concern | Choice |
|---|---|
| Language | C++17, MSVC (VS 2022 toolset v143), x64 only |
| Windowing | Raw Win32 (`CreateWindowEx`), per-monitor DPI aware v2 |
| Graphics | Direct3D 11, feature level 11_0 required, HLSL SM 5.0, shaders precompiled with `fxc` into the exe |
| UI | Dear ImGui (docking branch not needed) with the Win32 + DX11 backends |
| Image I/O | stb_image (PNG/JPG/HDR), stb_image_write (PNG), tinyexr (EXR, optional) |
| Video | Media Foundation `IMFSinkWriter` → H.264 / MP4 |
| GIF | Built-in encoder (msf_gif or similar single-header library) with per-frame palette and dithering |
| JSON | nlohmann/json |
| Math | DirectXMath |
| Build | CMake ≥ 3.24, vcpkg manifest mode, static CRT (`/MT`) |

**Distribution:** a portable folder containing `Spindle.exe` (under 10 MB) and
`assets/` (HDRIs and textures, about 60 MB). There is no installer, registry
setup or VC++ redistributable. File association is opt-in from Settings and is
written to `HKCU` only.

---

## 4. STL loading

### 4.1 Parsing
- **Format detection:** do not trust the `solid` header, because many binary STLs
  start with it. A file is binary if `84 + 50 × triCount == fileSize`. Otherwise
  try ASCII; if that fails, report a clear error.
- **Binary:** memory-map the file (`CreateFileMapping`) and parse in parallel
  across `std::thread::hardware_concurrency()` workers, each taking a contiguous
  triangle range.
- **ASCII:** use a hand-written tokenizer with `std::from_chars`, not iostreams.
  Split work by `facet` boundaries across threads.
- Ignore stored facet normals and recompute them from winding, because exporters
  often write zeros or wrong values. Read the colour bits in the attribute word
  (VisCAM/Materialise conventions) and offer them as an optional "use vertex
  colours" toggle.
- Load on a worker thread, with a progress bar and a cancel button. The UI stays responsive.

### 4.2 Processing
1. **Weld vertices:** quantise positions to 1e-5 × bounding-box diagonal, then do a
   parallel radix sort and dedupe. This gives an indexed mesh.
2. **Normals:** angle-weighted vertex normals with a crease angle (default 30°,
   adjustable 0–180°). Vertices are split along creases, so flat CAD faces stay
   flat and organic surfaces stay smooth. There is also a "flat shading" toggle.
3. **Orientation:** default is **Z-up**, the 3D-printing convention, with a
   Y-up toggle and rotate ±90° buttons for each axis. Place the model on the
   ground (min Z = 0) and centre it in XY.
4. **Units:** STL has no units. Assume mm and show the bounding box dimensions
   (`140.0 × 70.0 × 128.0 mm`) in the status bar.
5. **Bad meshes:** inverted or inconsistent winding is common. Render back faces
   with the normal flipped (two-sided lighting) so they never show black, and
   provide a "flip normals" action.
6. Upload to the GPU as one immutable vertex buffer (position + normal, 24 bytes)
   and a 32-bit index buffer, chunked as described in §1.2.

### 4.3 Performance targets (on the target machine, from SSD)

| Model | Load to first frame | Peak RAM | Viewport at 1080p |
|---|---|---|---|
| 100k triangles | < 0.3 s | < 300 MB | 60 fps |
| 1M triangles | < 1.5 s | < 600 MB | 60 fps |
| 5M triangles | < 6 s | < 1.5 GB | ≥ 30 fps |
| 10M triangles | < 12 s | < 2.0 GB | ≥ 15 fps (interactive quality drops automatically, §6.3) |

---

## 5. Materials and textures

### 5.1 Shading model
Metallic-roughness PBR: GGX specular, Smith-correlated visibility, Schlick
Fresnel and Lambert diffuse. On top of that there is an optional **clearcoat**
layer (a second GGX lobe) and an optional **sheen** term for fabric-like and
silk-PLA looks. Lighting is computed in linear space, rendered into an RGBA16F
target, then tonemapped (ACES fitted by default, AgX and Linear available) with
exposure control. Output is sRGB.

### 5.2 Presets (shipped)
Each preset is a JSON file in `assets/materials/`, and users can save their own.

| Preset | Notes |
|---|---|
| Matte PLA | Roughness 0.65, optional layer lines |
| Glossy PETG | Roughness 0.25, clearcoat 0.3 |
| Silk PLA | Low roughness, sheen, anisotropy faked with stretched normal noise |
| Grey resin (SLA) | Roughness 0.4, no layer lines |
| Clay / unfired | Roughness 0.9, faint noise bump |
| Ceramic glazed | White base, clearcoat 1.0 |
| Brushed aluminium | Metallic, directional normal-noise |
| Polished chrome | Metallic, roughness 0.05 |
| Gold | Metallic, base (1.0, 0.77, 0.34) |
| Rubber | Roughness 0.85, dark |
| Frosted / clear resin | Approximate transmission (§5.4) |
| Wireframe overlay | Can be combined with any preset; drawn with geometry-shader barycentrics |

### 5.3 Textures without UVs
STL has no UV coordinates, so every texture is applied in **object space**. This
means it rotates with the model on the turntable and does not swim.

- **Triplanar mapping** with blend sharpness, scale (in mm) and rotation. It is
  used for base colour, roughness and normal maps (normal maps use a
  whiteout-blended triplanar).
- **Bundled texture sets** (CC0, 1K, BC7-compressed DDS at build time): wood,
  marble, concrete, carbon fibre, knurled metal, leather, fabric, hammered metal.
- **User textures:** drop a PNG or JPG onto the Texture slot.
- **Procedural detail** (shader only, no texture memory):
  - *FDM layer lines*: a normal perturbation that is periodic in world Z, with
    layer height (default 0.2 mm) and depth parameters. This is the feature most
    likely to make a print render read as a print.
  - *Noise bump*: value or FBM noise with scale and strength.
  - *Height gradient*: base colour interpolated along Z between two colours.

### 5.4 Transparency (approximate)
"Clear resin" and "glass" use a two-pass approach:

1. Render back faces to get thickness.
2. Render front faces with Fresnel reflection of the environment, plus a
   refracted environment lookup offset by the normal, with Beer-Lambert tint by
   thickness.

There are no inter-reflections or caustics. This limitation is documented in the UI tooltip.

---

## 6. Environments and lighting

### 6.1 Image-based lighting
- Load an equirectangular `.hdr` or `.exr` (up to 8K; downsampled to 4K internally).
- Precompute on load, on the GPU, in under 1 s for a 4K HDRI:
  a 512² RGBA16F cubemap, a GGX-prefiltered specular mip chain (split-sum),
  9-coefficient spherical-harmonics irradiance, and a shared 2-channel 256² BRDF LUT
  (generated once and cached).
- Controls: rotation (yaw), intensity, background visibility, background blur (by sampling a prefiltered mip), and saturation.

**Bundled HDRIs** (CC0 from Poly Haven, 2K): a small studio, a large softbox
studio, an overcast outdoor scene, a sunset, a neutral warehouse interior, and a
night city scene.

### 6.2 Procedural studio (no HDRI needed)
- **Three-point light rig:** key, fill and rim. Each is a spherical area light
  with colour, intensity, direction and size. It can be used alone or added on
  top of an HDRI.
- **Backdrops:** solid colour, vertical or radial gradient, an infinite sweep
  (cyclorama floor that curves into the wall), or **transparent** (alpha
  channel, for PNG and transparent-GIF export).

### 6.3 Ground and shadows
- **Ground modes:** none, *shadow catcher* (invisible floor that only receives
  shadow and AO, composited over the backdrop), solid floor with material, and
  *reflective floor* (planar reflection pass with roughness blur, strength
  adjustable).
- **Shadows** come from the key light, or from the dominant direction in the
  HDRI (found by importance sampling its luminance on load). Use a 4096² shadow
  map fitted tightly to the model's bounding sphere, which is cheap because the
  scene is one object. Soft edges come from PCSS in the viewport and from
  jittered light positions during offline accumulation (§7.3).
- **Ambient occlusion:** GTAO at half resolution in the viewport and full
  resolution with accumulation in offline renders. Contact shadow on the ground
  plane comes from an AO term computed against the model's depth.
- **Adaptive quality:** if the viewport frame time goes over 33 ms, it turns off
  AO and soft shadows while the camera is moving, then restores them when the
  camera stops.

---

## 7. Turntable renderer

### 7.1 Motion settings
| Setting | Values (default in bold) |
|---|---|
| Mode | **Rotate object** (lighting stays fixed, so highlights move like a real turntable) / Orbit camera (lighting stays fixed relative to the model) |
| Duration | 2–60 s (**8 s**) |
| Frame rate | 24 / 25 / **30** / 50 / 60 |
| Rotations | 1–4 (**1**), clockwise or counter-clockwise |
| Start angle | 0–359° (**the current viewport angle**) |
| Easing | **Linear** (loops seamlessly) / ease in-out / hold at start N s |
| Camera elevation | -30° to 80° (**20°**), with optional vertical bob ±N° over the loop |
| Lens | Focal length 18–200 mm equivalent (**50 mm**), or orthographic |
| Framing | Auto-fit: the bounding sphere fills N% (**80%**) of the shorter frame side across *all* angles, so nothing is clipped mid-spin |

**Seamless loop rule:** frame `i` of `N` uses angle `start + 360° × rotations × i / N`.
The last frame is never equal to the first, so a looping MP4 or GIF has no stutter.

### 7.2 Output settings
| Setting | Values |
|---|---|
| Resolution | 720p, **1080p**, 1440p, 4K UHD, square 1080×1080, vertical 1080×1920, custom (up to 4096 on either side for H.264, 8192 for PNG) |
| Format | **MP4 (H.264)**, PNG sequence (8-bit, optional alpha), GIF |
| Quality | Draft (1 sample), **Standard (16 samples)**, High (64), Ultra (256) |
| Bitrate (MP4) | Auto from resolution (1080p30 → 12 Mbps), or manual |
| Extras | Depth of field (focus on the model's centre by default), motion blur (shutter 0–360°) |

### 7.3 Offline accumulation (how a rasteriser gets near-offline quality)
Each output frame is the average of *S* subframes rendered into an RGBA32F
accumulation buffer. Each subframe jitters:
- The **subpixel offset** (Halton 2,3), which gives anti-aliasing better than MSAA, with no shimmer.
- The **light position** across the area light disk, which gives physically
  plausible soft shadows.
- The **lens position** across the aperture, which gives true depth of field.
- The **time** within the shutter interval, which gives true motion blur.
- The **GTAO rotation**, which removes AO noise.

This uses only plain D3D11 and works the same on the K5100M and the HD 4600.
At *S* = 16 it removes the visible gap between viewport and "final render"
without needing any ray tracing.

### 7.4 Pipeline
```
render thread (GPU)        readback (3-deep staging ring)     encoder thread
  accumulate S subframes →  CopyResource → Map when ready  →  MF SinkWriter / PNG / GIF
  tonemap → RGBA8 target     (never Map the texture just written)   bounded queue (8 frames)
```
- The offscreen render target size does not depend on the window size.
- The UI shows a progress bar, the current frame preview, elapsed time and ETA.
  **Cancel** stops cleanly and keeps the partial PNG sequence.
- Write output to a temp file and rename it when finished, so a crash never leaves a half-written MP4 under the final name.
- The app calls `SetThreadExecutionState(ES_SYSTEM_REQUIRED)` during export so the laptop does not go to sleep.

### 7.5 Render time targets (K5100M, 8 s at 30 fps = 240 frames, 1M-triangle model)
| Preset | 1080p | 4K |
|---|---|---|
| Draft | < 30 s | < 1.5 min |
| Standard (16) | < 2 min | < 6 min |
| High (64) | < 6 min | < 20 min |

At 4K the software H.264 encoder is likely to be the bottleneck, not the GPU.
That is acceptable. The ETA must reflect whichever stage is slower.

---

## 8. Video encoding

- **MP4:** use `MFCreateSinkWriterFromURL` with an H.264 output type (High
  profile, level chosen from resolution and fps), and `MF_SINK_WRITER_DISABLE_THROTTLING`.
  Set `MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS = TRUE`, so Intel Quick Sync on
  the HD 4600 is used automatically when its driver exposes it. Otherwise
  Microsoft's software encoder is used. Both are part of Windows 10, so there are
  no extra dependencies. Input is NV12, converted from RGBA on the GPU in a compute
  shader (BT.709, limited range) to keep that work off the CPU.
- **PNG sequence:** `name_0000.png …`, compressed on 4 worker threads. Alpha is
  kept when the backdrop is transparent.
- **GIF:** at most 800 px wide by default, 15–30 fps, a per-frame 256-colour
  palette with ordered or Floyd-Steinberg dithering, and an infinite loop flag.
  Transparent backdrop uses 1-bit alpha.
- **Optional ffmpeg:** if `ffmpeg.exe` is found on PATH or set in Settings, show
  extra formats (WebM VP9, ProRes 4444 with alpha, H.265). It is fed raw frames
  through a pipe. This is never required.

---

## 9. CLI (headless) mode

```
Spindle.exe model.stl                                   # open in the viewer
Spindle.exe render model.stl -o out.mp4 [options]       # no window
  --preset  studio_matte.json     scene preset (material + env + camera + output)
  --material "Silk PLA"           --hdri sunset.hdr      --res 1920x1080
  --fps 30  --seconds 8           --quality standard     --transparent
  --elevation 20  --up z          --format mp4|png|gif
```
- Exit codes: 0 means success, 2 means a load error, 3 means a GPU or device
  error, 4 means an encode error. The CLI prints progress lines to stdout in
  plain text, one line per 10%.
- It uses the same renderer and the same scene JSON as the GUI, so a preset
  saved in the GUI renders identically in a batch run.
- This gives the FormForge integration point: a bundle step can call
  `Spindle.exe render bundle/model.stl --preset formforge.json -o bundle/turntable.mp4`.

---

## 10. UI

Minimal by design: the viewport fills the window and a single collapsible panel
sits on the right.

```
┌──────────────────────────────────────────────┬─────────────────┐
│                                              │ ▸ Model         │
│                                              │ ▾ Material      │
│                 3D viewport                  │   [Matte PLA ▾] │
│          (drop an STL anywhere here)         │   colour ■      │
│                                              │   roughness ──● │
│                                              │ ▸ Environment   │
│                                              │ ▸ Camera        │
│                                              │ ▸ Turntable     │
│                                              │ [ Render video ]│
├──────────────────────────────────────────────┴─────────────────┤
│ planter.stl · 1.11M tris · 140×70×128 mm · Quadro K5100M · 60fps│
└────────────────────────────────────────────────────────────────┘
```

- **Viewport controls:** left-drag orbits, right-drag or middle-drag pans, the
  wheel zooms toward the cursor, `F` frames the model, `Space` plays or pauses a
  live turntable preview, `1/3/7` give front, side and top views, `Tab` hides the
  panel, `Ctrl+O` opens, `Ctrl+R` renders, and `Ctrl+S` saves the scene preset.
- **Live preview:** the Turntable section has a *Preview* toggle that spins the
  viewport at the export speed, framed and letterboxed to the export aspect
  ratio, so what you see is what you will export.
- Light and dark UI themes, DPI scaling, and the last 10 files.
- Settings are stored in `%APPDATA%\Spindle\settings.json` and user presets in
  `%APPDATA%\Spindle\presets\`.

---

## 11. Scene preset format

```json
{
  "version": 1,
  "model":    { "up": "z", "creaseAngle": 30, "flatShading": false },
  "material": { "preset": "Matte PLA", "baseColor": [0.85, 0.32, 0.12],
                "roughness": 0.65, "metalness": 0.0, "clearcoat": 0.0,
                "layerLines": { "enabled": true, "height": 0.2, "depth": 0.3 },
                "texture": null },
  "environment": { "hdri": "studio_small_2k.hdr", "rotation": 120, "intensity": 1.0,
                   "background": "sweep", "backgroundColor": [0.94, 0.94, 0.94],
                   "ground": "shadow_catcher", "lights": "three_point" },
  "camera":   { "focalLength": 50, "elevation": 20, "fill": 0.8, "dof": null },
  "turntable":{ "mode": "rotate_object", "seconds": 8, "fps": 30, "rotations": 1,
                "direction": "ccw", "easing": "linear" },
  "output":   { "format": "mp4", "width": 1920, "height": 1080,
                "quality": "standard", "transparent": false }
}
```
Unknown keys are ignored, missing keys get default values, and `version` controls migration.

---

## 12. Robustness

- **Device lost** (`DXGI_ERROR_DEVICE_REMOVED`, for example after a driver
  reset): recreate the device and all GPU resources from the CPU-side mesh and
  textures. During an export, resume from the last completed frame.
- **Out of VRAM:** for the HD 4600 or huge models, lower the shadow map size,
  disable the reflection pass, then fall back to drawing in chunks.
- **Malformed STL:** NaN or Inf vertices and zero-area triangles are dropped,
  and a count is shown. Truncated binary files load the complete triangles and
  show a warning.
- **Thermals:** this is a 2014 laptop, so long 4K exports may throttle. An
  optional *pace* setting adds a sleep between frames. The app does not attempt
  power management beyond that.
- **Logging:** `%APPDATA%\Spindle\log.txt` records the adapter, driver version,
  feature level, load timings and export stats, to help with bug reports.

---

## 13. Acceptance criteria

1. Starting `Spindle.exe` on the target machine shows **Quadro K5100M** in the
   status bar, without any change in the NVIDIA Control Panel.
2. All of these load correctly: binary STL, ASCII STL, binary with a `solid`
   header, a 0-triangle file (error message, no crash), a truncated file
   (warning), and a mesh with inverted winding (renders correctly lit).
3. A 1M-triangle STL meets every row of the §4.3 and §7.5 targets.
4. A default 8 s / 30 fps / 1080p MP4 plays in the Windows Films & TV app, VLC
   and a browser, and loops with no visible jump at the seam.
5. A transparent-background PNG sequence has correct (premultiplied-free,
   straight) alpha around the model and the shadow catcher.
6. Rendering the same preset from the GUI and from the CLI gives pixel-identical
   PNG frames at the same quality setting (deterministic jitter sequence).
7. Forcing the HD 4600 in Settings still produces a correct, slower export.
8. A 30-minute 4K High export finishes with no TDR, and memory use stays flat.
9. The portable folder runs on a clean Windows 10 22H2 install without
   installing anything.

---

## 14. Milestones

| # | Deliverable | Rough effort |
|---|---|---|
| M1 | Win32 + D3D11 + ImGui shell, Optimus selection, STL loader with welding and normals, orbit viewport with simple lighting | 1 week |
| M2 | PBR, IBL precompute, HDRI loading, tonemapping, material presets, ground modes and shadows | 1.5 weeks |
| M3 | Triplanar textures, layer lines, procedural detail, GTAO, reflective floor, approximate transparency | 1 week |
| M4 | Offline accumulation, turntable camera, readback ring, MP4/PNG/GIF export, progress and cancel | 1.5 weeks |
| M5 | CLI mode, presets, device-lost recovery, packaging, acceptance tests on the target machine | 1 week |

---

## 15. Open questions

- Should the app ship a small *sample library* of STLs so it opens with
  something to show? (Proposal: one FormForge-generated part.)
- Should v2 accept 3MF and use its colour and material data? 3MF is the format
  FormForge already exports, so this is cheap to add and is the most likely request.
- Is a watermark or text overlay (part name and dimensions) wanted on exported
  videos? It is trivial to add in the tonemap pass.

---

## 16. Implementation notes (v0.1)

The build in `spindle/` follows this spec except for the points below. Each one
was a deliberate trade-off, not an omission found later.

| Spec | v0.1 | Why |
|---|---|---|
| Shaders precompiled with `fxc` (§3) | Compiled at first start by `d3dcompiler_47.dll` (part of Windows 10), then cached in `%LOCALAPPDATA%\Spindle\shadercache` | No Windows SDK step in the build; first start costs about 1–2 s once |
| Bundled Poly Haven HDRIs and texture sets (§5.3, §6.1) | 8 procedural environments and 5 procedural patterns generated at runtime; users can load any `.hdr` or image | The app stays one small exe with no asset folder |
| OpenEXR input (§6.1) | `.hdr` only | Avoids another dependency |
| GTAO (§6.3) | Hemisphere SSAO with a depth-aware blur, rotated per accumulation sample | Simpler; with accumulation the noise averages out |
| Triplanar normal maps (§5.3) | Triplanar colour only | Procedural bumps (layer lines, noise) cover the main need |
| STL vertex colours (§4.1) | Ignored | Rarely present; would need a second vertex stream |
| "Flip normals" action (§4.2) | Not needed | The shader orients every normal towards the viewer using the geometric normal, so inverted or inconsistent winding always shades correctly |
| Back-face thickness pass for clear resin (§5.4) | Single pass with a fixed tint | Same visual class of approximation, one pass cheaper |
| NV12 conversion in a compute shader (§8) | On the encoder thread (CPU) | Keeps the GPU path simple; the CPU has headroom while the GPU renders |
| `Spindle.exe render …` from a console (§9) | `Spindle.com` (console build) sits next to `Spindle.exe`, so `Spindle render …` in cmd/PowerShell resolves to it and the shell waits for it | A GUI-subsystem exe returns to the prompt immediately |
| Opt-in file association (§3) | Not implemented | "Open with" and drag-and-drop cover it |
| Environment cubemap mips via `GenerateMips` | Each cube mip is rendered straight from the matching equirect mip | Some drivers leave generated cube mips empty |
| Light rig angles | Relative to the camera's start azimuth | The rig frames the subject the same way whatever the start angle; in orbit-camera mode it stays fixed to the model |

**What has been verified, and how**

- *Core* (loader, welding, normals, presets, turntable timing, environments):
  unit tests, run natively and under Wine.
- *Renderer, GUI, GIF and PNG export*: cross-compiled with MinGW and run under
  Wine (wined3d on Mesa llvmpipe), with Microsoft's real `d3dcompiler_47.dll`.
  Images were inspected by eye.
- *MSVC build, Media Foundation MP4, WARP renders, GUI screenshot*: GitHub
  Actions on `windows-2022` (`.github/workflows/spindle.yml`).
- *Not yet verified*: performance on the target K5100M laptop (the targets in
  §4.3 and §7.5 are still estimates), Intel Quick Sync encoding, and recovery
  from a real driver reset.
