// Camera math for the orbit viewport and the turntable path. Platform-neutral.
#pragma once

#include "common.h"
#include "scene.h"

namespace spindle {

// Low-discrepancy sequence used for every per-sample jitter.
float halton(int index, int base);

struct View {
    mat4 view, proj;
    vec3 eye;
    vec3 forward;
    bool ortho = false;
    float nearZ = 0.1f, farZ = 1000.0f;
};

struct ViewRequest {
    vec3 eye, target;
    float fovY = 0.7f;           // perspective only
    float orthoHalfHeight = 1;   // orthographic only
    bool ortho = false;
    float aspect = 1;
    float nearZ = 0.1f, farZ = 1000.0f;
    // Accumulation jitter. pixelJitter is in pixels; lens offset in world units.
    float jitterX = 0, jitterY = 0;
    int widthPx = 1, heightPx = 1;
    float lensX = 0, lensY = 0;
};
View buildView(const ViewRequest& r);

// Vertical field of view for a 35 mm-equivalent focal length applied to the
// shorter side of the frame (24 mm), so portrait and landscape frame alike.
float fovYForFocalLength(float focalMm, float aspect);

// Interactive orbit camera for the viewport.
struct OrbitCamera {
    float azimuth = -60.0f;   // degrees around Z, from +X
    float elevation = 20.0f;  // degrees above the horizon
    float distance = 300.0f;
    vec3 target = {0, 0, 50};

    vec3 eye() const;
    void frame(const vec3& sphereCenter, float radius, float fovY, float aspect, float fill = 0.8f);
    void orbit(float dAzimuthDeg, float dElevationDeg);
    void pan(float dxPx, float dyPx, float viewportHeightPx, float fovY);
    void zoom(float wheelSteps);
};

// One sample of the turntable: model rotation and camera, at time `t` frames.
struct TurntablePose {
    float objectAngle = 0;  // radians about Z, applied to the model
    vec3 eye, target;
    float fovY = 0.7f, orthoHalfHeight = 1;
    bool ortho = false;
    float nearZ = 0.1f, farZ = 1000.0f;
    float focusDistance = 1;
};

// Fractional frame index `frame` (subframe time added for motion blur).
// `startAzimuthDeg` is the camera azimuth at frame 0.
TurntablePose turntablePose(const Scene& scene, const vec3& sphereCenter, float sphereRadius, float aspect,
                            double frame, float startAzimuthDeg);

// The angle (degrees) the turntable has advanced at fractional frame `frame`.
float turntableAngleDeg(const TurntableSettings& t, double frame);

// Distance at which a sphere of `radius` fills `fill` of the short frame side.
float framingDistance(float radius, float fovY, float aspect, float fill);

}  // namespace spindle
