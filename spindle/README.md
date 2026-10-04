# Spindle

A small Windows app that opens any STL file, shows it with good materials and
lighting, and renders a 360° turntable video. It has one window, one model and
one export button.

It implements [`docs/turntable-viewer-spec.md`](../docs/turntable-viewer-spec.md).
It is tuned for a 2014 mobile workstation (Quadro K5100M, Kepler, 8 GB; i7-4910MQ;
Windows 10), and runs on any Direct3D 11 GPU, including integrated graphics.

## Getting it

**Download:** every push that touches `spindle/` builds on GitHub Actions. Open
the latest *Spindle (Windows)* run and download the **Spindle-windows-x64**
artifact. Unzip it anywhere and run `Spindle.exe`. Nothing needs installing: the
C++ runtime is linked in, and the shader compiler and video encoder ship with
Windows 10.

**Build it yourself** (Visual Studio 2022 with the C++ workload, and CMake 3.20+):

```bat
cd spindle
cmake -S . -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Release
build\Release\Spindle.exe
```

All dependencies are vendored in `third_party/`, so the build needs no network.

## Using it

Drop an `.stl` onto the window, or press **Ctrl+O**. Then pick a material and a
lighting setup, check **Turntable → Preview** (Space), and press **Render
turntable…** (Ctrl+R).

| Input | Action |
|---|---|
| Left drag | Orbit |
| Right or middle drag | Pan |
| Wheel | Zoom towards the cursor |
| Double-click / `F` | Frame the model |
| `Space` | Turntable preview at export speed, letterboxed to the export size |
| `1` `3` `7` | Front, side and top views |
| `Tab` | Hide or show the panel |
| `Ctrl+O` / `Ctrl+R` / `Ctrl+S` | Open STL / render / save scene preset |

You can also drop other files onto the window:
- an `.hdr` sets the lighting environment;
- a `.png` or `.jpg` becomes a triplanar texture;
- a `.json` scene preset applies all its settings.

When the camera stops, the viewport keeps refining the image (64 samples by
default), so the still view matches the final render.

### What's in the box

- **Materials:** 16 presets, including Matte PLA, Glossy PETG, Silk PLA, grey
  resin, clay, glazed ceramic, brushed aluminium, chrome, gold, rubber, clear and
  frosted resin, walnut, marble, carbon fibre and granite. Every parameter is
  editable.
- **Surface detail:** *FDM layer lines* (layer height and depth), noise bump,
  height gradient, wireframe overlay.
- **Textures:** STL has no UVs, so images are applied triplanar in object space.
  They stay attached to the model while it spins. There are also procedural
  wood, marble, carbon fibre, checker and granite patterns.
- **Environments:** 8 built-in procedural environments (Studio Softbox,
  High-Key, Dark, Overcast, Daylight, Sunset, Warm Interior, Night City), or any
  Radiance `.hdr` file. A three-point light rig can be added on top.
- **Backgrounds:** environment (with blur), solid colour, vertical or radial
  gradient, and transparent.
- **Ground:** shadow catcher, floor, reflective floor, or none. Shadows soften
  with the *Shadow softness* setting, and there is ambient occlusion.
- **Output:** MP4 (H.264), PNG sequence (with alpha) and GIF. With ffmpeg, also
  WebM VP9, ProRes 4444 and H.265.
- **Quality:** Draft, Standard, High and Ultra average 1, 16, 64 and 256 jittered
  samples per frame. This gives the anti-aliasing, soft shadows, depth of field
  and motion blur.

Scene presets for common looks are in `presets/`.

## Command line

`Spindle.com` is the console build of the same program. From a command prompt,
`Spindle …` runs it instead of the `.exe`, so the shell waits for it and shows its
output.

```bat
Spindle render part.stl -o part.mp4 --preset presets\studio-product.json
Spindle render part.stl -o spin.gif --material "Silk PLA" --res 600x600 --seconds 4 --transparent
Spindle render part.stl -o frames\part.png --quality high          (PNG sequence)
Spindle still  part.stl -o hero.png --material Gold --env Sunset --background environment
Spindle --list-gpus
Spindle --help
```

The command line and the viewer use the same renderer and preset files, so a
preset saved in the viewer renders the same way in a batch.

Exit codes: 0 ok, 1 usage, 2 load error, 3 GPU error, 4 encode error.

**FormForge:** to give a generated part a turntable, run
`Spindle render bundle\model.stl --preset presets\formforge.json -o bundle\turntable.mp4`.

## Files it writes

- `%APPDATA%\Spindle\settings.json`: recent files, GPU choice, ffmpeg path, theme.
- `%APPDATA%\Spindle\log.txt`: adapter, driver, timings and errors (attach it to bug reports).
- `%LOCALAPPDATA%\Spindle\shadercache\`: compiled shaders, so later starts are fast.

## How it is built

| | |
|---|---|
| `src/mesh.*` | STL parsing (binary/ASCII detection by size, parallel parse), welding, crease-aware normals |
| `src/environment.*` | Procedural environments, `.hdr` loading, SH irradiance and dominant-light extraction |
| `src/scene.*` | Scene settings, material presets, JSON presets |
| `src/camera.*` | Orbit camera, turntable path, seamless-loop timing, jitter |
| `src/shaders.hlsl` | Every GPU pass (embedded into the exe at build time) |
| `src/renderer.*` | D3D11 passes: shadow map, depth, SSAO, planar reflection, PBR, tonemap and accumulate, resolve |
| `src/exporter.*` | Turntable driver: accumulation, readback ring, encoder queue |
| `src/encoders.*` | Media Foundation H.264, PNG, GIF, ffmpeg pipe |
| `src/app.cpp` | Win32 window, ImGui panel, viewport, async loading, device-loss recovery |
| `src/cli.*` | `render` / `still` / `--list-gpus` |

The platform-neutral core (`mesh`, `scene`, `camera`, `environment`) has unit
tests in `tests/test_core.cpp`. They build and run on Linux too:
`cmake -S . -B build && cmake --build build && ctest --test-dir build`.

During development the Windows build was also cross-compiled with MinGW
(`cmake/mingw-w64.cmake`) and run under Wine. That used Mesa's software OpenGL
and Microsoft's `d3dcompiler_47.dll`, to check the shaders, the GUI and the
GIF/PNG exports. CI covers the rest on real Windows: MSVC build, WARP renders,
Media Foundation MP4 and a GUI screenshot.

## Known limitations

- Glass and clear resin are approximate: they refract the environment only, with
  no caustics and no view of the floor through the object.
- OpenEXR environments are not supported; convert them to `.hdr`.
- Triplanar textures drive colour only (no normal maps). STL vertex colours are ignored.
- MP4 output is limited to 4096 px per side (an H.264 limit); use PNG for larger frames.
- The app has not yet been run on the target K5100M laptop. The performance
  targets in the spec are estimates until it has.
