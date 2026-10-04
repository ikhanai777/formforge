#include "camera.h"

namespace spindle {

float halton(int index, int base) {
    float f = 1, r = 0;
    int i = index;
    while (i > 0) {
        f /= (float)base;
        r += f * (float)(i % base);
        i /= base;
    }
    return r;
}

View buildView(const ViewRequest& r) {
    View v;
    vec3 fwd = normalize(r.target - r.eye);
    vec3 right = normalize(cross(fwd, vec3(0, 0, 1)));
    if (length(cross(fwd, vec3(0, 0, 1))) < 1e-4f) right = vec3(1, 0, 0);
    vec3 up = cross(right, fwd);

    // Depth of field: move the eye across the lens but keep looking at the focus point.
    vec3 eye = r.eye + right * r.lensX + up * r.lensY;
    v.view = mat4::lookAt(eye, r.target, vec3(0, 0, 1));
    if (r.ortho)
        v.proj = mat4::orthographic(r.orthoHalfHeight * r.aspect, r.orthoHalfHeight, r.nearZ, r.farZ);
    else
        v.proj = mat4::perspective(r.fovY, r.aspect, r.nearZ, r.farZ);

    // Sub-pixel jitter: shift the clip-space x/y by a fraction of a pixel.
    float jx = 2.0f * r.jitterX / (float)std::max(1, r.widthPx);
    float jy = 2.0f * r.jitterY / (float)std::max(1, r.heightPx);
    if (r.ortho) {
        v.proj.m[0][3] += jx;
        v.proj.m[1][3] += jy;
    } else {
        // w = -z_view, so adding -j to the z column shifts x/w by +j.
        v.proj.m[0][2] -= jx;
        v.proj.m[1][2] -= jy;
    }
    v.eye = eye;
    v.forward = normalize(r.target - eye);
    v.ortho = r.ortho;
    v.nearZ = r.nearZ;
    v.farZ = r.farZ;
    return v;
}

float fovYForFocalLength(float focalMm, float aspect) {
    float shortFov = 2.0f * std::atan(12.0f / std::max(1.0f, focalMm));
    if (aspect >= 1.0f) return shortFov;
    return 2.0f * std::atan(std::tan(shortFov * 0.5f) / aspect);
}

float framingDistance(float radius, float fovY, float aspect, float fill) {
    float halfShort = aspect >= 1.0f ? fovY * 0.5f : std::atan(std::tan(fovY * 0.5f) * aspect);
    float angular = std::atan(clampf(fill, 0.05f, 1.0f) * std::tan(halfShort));
    return radius / std::sin(angular);
}

vec3 OrbitCamera::eye() const {
    float az = radians(azimuth), el = radians(elevation);
    return target + vec3(std::cos(el) * std::cos(az), std::cos(el) * std::sin(az), std::sin(el)) * distance;
}

void OrbitCamera::frame(const vec3& center, float radius, float fovY, float aspect, float fill) {
    target = center;
    distance = framingDistance(radius, fovY, aspect, fill);
}

void OrbitCamera::orbit(float dAz, float dEl) {
    azimuth = std::fmod(azimuth + dAz, 360.0f);
    elevation = clampf(elevation + dEl, -89.0f, 89.0f);
}

void OrbitCamera::pan(float dxPx, float dyPx, float viewportHeightPx, float fovY) {
    vec3 fwd = normalize(target - eye());
    vec3 right = normalize(cross(fwd, vec3(0, 0, 1)));
    vec3 up = cross(right, fwd);
    float worldPerPx = 2.0f * distance * std::tan(fovY * 0.5f) / std::max(1.0f, viewportHeightPx);
    target += right * (-dxPx * worldPerPx) + up * (dyPx * worldPerPx);
}

void OrbitCamera::zoom(float wheelSteps) {
    distance = std::max(1e-3f, distance * std::pow(0.88f, wheelSteps));
}

static float smoothstep01(float t) {
    t = clampf(t, 0, 1);
    return t * t * (3 - 2 * t);
}

float turntableAngleDeg(const TurntableSettings& t, double frame) {
    const int n = t.frameCount();
    const float total = 360.0f * (float)t.rotations;
    float a = 0;
    switch (t.easing) {
        case Easing::Linear:
            // Frame n would equal frame 0, so the loop never shows the same angle twice.
            a = total * (float)(frame / n);
            break;
        case Easing::EaseInOut:
            a = total * smoothstep01(n > 1 ? (float)(frame / (n - 1)) : 0.0f);
            break;
        case Easing::HoldThenSpin: {
            double hold = std::min<double>(n - 1, std::max(0.0f, t.holdSeconds) * t.fps);
            double spinFrames = std::max(1.0, n - hold);
            a = frame <= hold ? 0.0f : total * (float)((frame - hold) / spinFrames);
            break;
        }
        default:
            break;
    }
    return t.clockwise ? -a : a;
}

float explodeAmountAt(const Scene& s, double frame) {
    const ExplodeSettings& x = s.explode;
    if (!x.animate) return clampf(x.manual, 0, 1);
    float t = (float)std::fmod(frame / std::max(1, s.turntable.frameCount()), 1.0);
    float span = std::max(1e-4f, x.end - x.start);
    float u = clampf((t - x.start) / span, 0, 1);
    switch (x.timing) {
        case ExplodeTiming::ExplodeAndReturn:
            // Out and back in within [start, end]; 0 outside it, so the loop is seamless.
            return t < x.start || t > x.end ? 0.0f : 0.5f - 0.5f * std::cos(2.0f * kPi * u);
        case ExplodeTiming::ExplodeAndHold:
            return smoothstep01(u);
        case ExplodeTiming::Assemble:
            return 1.0f - smoothstep01(u);
        default:
            return 0.0f;
    }
}

float partExplodeAmount(float global, float order, float stagger) {
    float a = clampf(global * (1.0f + stagger) - order * stagger, 0.0f, 1.0f);
    return smoothstep01(a);
}

void explodeOffsets(const std::vector<ExplodePart>& parts, const vec3& center, float sceneRadius, float distance,
                    const std::vector<float>& amounts, std::vector<vec3>& out) {
    out.assign(parts.size(), vec3(0, 0, 0));
    if (parts.size() < 2) return;
    float lowest = 0;
    for (size_t i = 0; i < parts.size(); ++i) {
        vec3 d = parts[i].center - center;
        float len = length(d);
        // A part at the centre (a core or a shaft) rises instead of staying put.
        vec3 dir = len > sceneRadius * 0.02f ? d / len : vec3(0, 0, 1);
        // Travel grows with the part's distance from the centre, plus a margin so
        // parts near the middle still separate. distance = 1 opens a typical
        // assembly to roughly twice its size.
        out[i] = dir * (distance * 0.6f * (len + 0.25f * sceneRadius) * amounts[i]);
        lowest = std::min(lowest, parts[i].minZ + out[i].z);
    }
    // Lift the whole assembly so nothing sinks through the floor.
    for (auto& o : out) o.z -= lowest;
}

float explodedRadius(const std::vector<ExplodePart>& parts, const vec3& center, float sceneRadius, float distance) {
    std::vector<float> ones(parts.size(), 1.0f);
    std::vector<vec3> off;
    explodeOffsets(parts, center, sceneRadius, distance, ones, off);
    float r = sceneRadius;
    for (size_t i = 0; i < parts.size(); ++i) r = std::max(r, length(parts[i].center + off[i] - center) + parts[i].radius);
    return r;
}

TurntablePose turntablePose(const Scene& s, const vec3& center, float radius, float aspect, double frame,
                            float startAzimuthDeg) {
    TurntablePose p;
    const auto& tt = s.turntable;
    const auto& cam = s.camera;
    float angle = turntableAngleDeg(tt, frame);
    float loop = (float)(frame / tt.frameCount());
    float elevation = clampf(cam.elevation + tt.bob * std::sin(2.0f * kPi * loop), -85.0f, 85.0f);
    float azimuth = startAzimuthDeg;
    if (tt.mode == TurntableMode::RotateObject)
        p.objectAngle = radians(angle);
    else
        azimuth -= angle;  // orbiting the camera the other way looks like the same spin

    p.ortho = cam.orthographic;
    p.fovY = fovYForFocalLength(cam.focalLength, aspect);
    float dist;
    if (p.ortho) {
        float halfShort = radius / clampf(cam.fill, 0.05f, 1.0f);
        p.orthoHalfHeight = aspect >= 1.0f ? halfShort : halfShort / aspect;
        dist = radius * 4.0f;
    } else {
        dist = framingDistance(radius, p.fovY, aspect, cam.fill);
    }
    float az = radians(azimuth), el = radians(elevation);
    p.target = center;
    p.eye = center + vec3(std::cos(el) * std::cos(az), std::cos(el) * std::sin(az), std::sin(el)) * dist;
    p.focusDistance = dist;
    p.nearZ = std::max(radius * 0.01f, dist - radius * 3.0f);
    p.farZ = dist + radius * 80.0f;
    return p;
}

}  // namespace spindle
