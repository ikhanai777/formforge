# Spindle for Android

The same renderer as the Windows app, ported to OpenGL ES 3.0. It opens STL and
3MF files (with 3MF colours and materials), lets you change the material,
environment and lighting, explodes assemblies, and exports turntable videos
(MP4), GIFs and PNG stills to your gallery.

## Installing

1. On your phone, open
   **https://github.com/ikhanai777/formforge/releases/tag/spindle-android-latest**
   and tap **Spindle.apk**.
2. Open the downloaded file. Android asks you to allow installs from your browser
   or file manager the first time. Allow it, then tap **Install**.
3. If Play Protect warns that the app is unknown, tap **More details → Install
   anyway**. It warns because the app is not from the Play Store.

You need Android 8.0 or newer and a GPU with OpenGL ES 3.0. Every phone from the
last eight years or so has both. The APK runs on 64-bit and 32-bit ARM phones and
on x86_64 emulators.

The release is rebuilt on every push that touches `spindle/`. Installing a newer
build over an older one keeps your settings, because every build is signed with
the same key (see below).

## Using it

- **Open a model:** use the **Model** tab, or share an `.stl`/`.3mf` from a file
  manager or browser to Spindle.
- **Viewport gestures:**
  - Drag with one finger to orbit.
  - Pinch to zoom.
  - Drag with two fingers to pan.
  - Double-tap to frame the model.
- **Tabs:**
  - **Look:** material preset, colour, roughness, metal and texture. 3MF files
    keep their own colours unless you turn *Colours from file* off.
  - **Scene:** environment (built-in or your own `.hdr`), background, floor,
    shadows and reflections.
  - **Spin:** duration, frame rate, direction, camera height and quality.
  - **Explode:** slider for the viewport, or animate during the turntable.
  - **Export:** MP4, GIF or PNG. Files go to *Movies/Spindle* or
    *Pictures/Spindle* and appear in your gallery. When an export finishes,
    Spindle offers *Share* and *Open*.

## Signing key

The APK is signed with `keystore/spindle-sideload.p12` (password
`spindle-sideload`). The key is committed on purpose. It only identifies this
sideloaded app, so that each build installs as an update over the last one. It
grants no access to anything. Don't use it for a Play Store release.

## Building it yourself

You need JDK 17 and the Android SDK with NDK (any r26+) and CMake 3.22.1.

```sh
cd spindle/android
./gradlew assembleRelease
# app/build/outputs/apk/release/app-release.apk
```

## How it is built and tested

- **Shared code:** the native code (`app/src/main/cpp`) builds on the shared
  Spindle core (`../src`): loaders, mesh processing, scenes, environments and
  sampling. It adds the GL ES renderer in `../src/gles`, which uses the same
  frame plan as the Direct3D renderer.
- **Desktop check:** the GL ES renderer and the viewer core are first run on
  desktop Mesa (`tests/gles_harness.cpp`, `tests/viewer_core_test.cpp`). Their
  stills match the Direct3D renderer to within one level per channel on
  average.
- **CI build:** `.github/workflows/spindle-android.yml` builds the APK and
  publishes it to the release above.
- **Emulator test:** CI then installs the APK on an Android 10 emulator, loads
  a 3MF assembly, and exports a still, an exploding MP4 and a GIF
  (`ci/emulator-test.sh`). The emulator's output is uploaded as the
  *android-emulator-output* artifact.

## Known limitations

- **Export resolution:** the MP4 size is limited by the phone's H.264 encoder.
  1920×1080 works almost everywhere. If an export fails, Spindle says so; try a
  lower size or a GIF.
- **Export speed:** Final quality accumulates many samples per frame, so long
  4K exports can take a while on a phone. Keep the app in front while it
  exports. If Android takes the GL context away, the export is cancelled and
  partial files are removed.
