// What one accumulation sample renders: the camera (with its sub-pixel, lens and
// shutter jitter), the turntable angle and the explode amount. Platform-neutral,
// shared by the Direct3D (Windows) and OpenGL ES (Android) renderers.
#pragma once

#include "camera.h"
#include "scene.h"

namespace spindle {

struct SampleInput {
    View view;
    float objectAngle = 0;  // radians about Z
    int sampleIndex = 0;    // 0 = first sample (clears the accumulation buffer, no jitter)
    bool fullQuality = true;  // false: skip AO and soft-shadow jitter (camera moving)
    // Light-rig azimuths are relative to this (degrees): the viewport passes its
    // camera azimuth, an export passes the fixed start azimuth, so in orbit-camera
    // mode the lights stay put relative to the model.
    float lightReferenceAzimuth = 0;
    float explode = 0;  // global explode amount 0..1 (see explodeAmountAt)
};

// Builds sample `sampleIndex` of `totalSamples` for turntable time `frame`
// (shared by the export and the viewport's live preview).
SampleInput makeTurntableSample(const Scene& scene, const vec3& center, float radius, int width, int height,
                                double frame, int sampleIndex, int totalSamples, float startAzimuthDeg);

// Builds sample `sampleIndex` for the interactive orbit camera.
SampleInput makeOrbitSample(const Scene& scene, const OrbitCamera& cam, float radius, int width, int height,
                            int sampleIndex);

}  // namespace spindle
