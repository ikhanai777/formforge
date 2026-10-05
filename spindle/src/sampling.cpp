#include "sampling.h"

namespace spindle {

// Concentric mapping of the unit square to the unit disk (even lens coverage).
static void squareToDisk(float u, float v, float& x, float& y) {
    float a = 2 * u - 1, b = 2 * v - 1;
    if (a == 0 && b == 0) {
        x = y = 0;
        return;
    }
    float r, phi;
    if (std::fabs(a) > std::fabs(b)) {
        r = a;
        phi = (kPi / 4) * (b / a);
    } else {
        r = b;
        phi = (kPi / 2) - (kPi / 4) * (a / b);
    }
    x = r * std::cos(phi);
    y = r * std::sin(phi);
}

SampleInput makeTurntableSample(const Scene& s, const vec3& center, float radius, int width, int height, double frame,
                                int sampleIndex, int totalSamples, float startAzimuthDeg) {
    const bool jitter = totalSamples > 1 && sampleIndex > 0;
    const int h = sampleIndex + 1;
    double t = frame;
    if (jitter && s.turntable.shutter > 0) t += clampf(s.turntable.shutter, 0, 1) * halton(h, 5);
    float aspect = (float)width / (float)std::max(1, height);
    TurntablePose p = turntablePose(s, center, radius, aspect, t, startAzimuthDeg);

    ViewRequest r;
    r.eye = p.eye;
    r.target = p.target;
    r.fovY = p.fovY;
    r.ortho = p.ortho;
    r.orthoHalfHeight = p.orthoHalfHeight;
    r.aspect = aspect;
    r.nearZ = p.nearZ;
    r.farZ = p.farZ;
    r.widthPx = width;
    r.heightPx = height;
    if (jitter) {
        r.jitterX = halton(h, 2) - 0.5f;
        r.jitterY = halton(h, 3) - 0.5f;
        if (s.camera.depthOfField && !p.ortho) {
            float lensRadius = s.camera.focalLength / (2.0f * std::max(0.7f, s.camera.fStop));
            float x, y;
            squareToDisk(halton(h, 7), halton(h, 11), x, y);
            r.lensX = x * lensRadius;
            r.lensY = y * lensRadius;
        }
    }
    SampleInput in;
    in.view = buildView(r);
    in.objectAngle = p.objectAngle;
    in.sampleIndex = sampleIndex;
    in.fullQuality = true;
    in.lightReferenceAzimuth = startAzimuthDeg;
    in.explode = explodeAmountAt(s, t);
    return in;
}

SampleInput makeOrbitSample(const Scene& s, const OrbitCamera& cam, float radius, int width, int height, int sampleIndex) {
    float aspect = (float)width / (float)std::max(1, height);
    ViewRequest r;
    r.eye = cam.eye();
    r.target = cam.target;
    r.fovY = fovYForFocalLength(s.camera.focalLength, aspect);
    r.ortho = s.camera.orthographic;
    r.orthoHalfHeight = cam.distance * std::tan(r.fovY * 0.5f);
    r.aspect = aspect;
    r.nearZ = std::max(cam.distance * 0.01f, cam.distance - radius * 3.0f);
    if (r.ortho) r.nearZ = cam.distance * 0.01f;
    r.farZ = cam.distance + radius * 80.0f;
    r.widthPx = width;
    r.heightPx = height;
    if (sampleIndex > 0) {
        r.jitterX = halton(sampleIndex + 1, 2) - 0.5f;
        r.jitterY = halton(sampleIndex + 1, 3) - 0.5f;
    }
    SampleInput in;
    in.view = buildView(r);
    in.sampleIndex = sampleIndex;
    in.lightReferenceAzimuth = cam.azimuth;
    in.explode = clampf(s.explode.manual, 0, 1);
    return in;
}

}  // namespace spindle
