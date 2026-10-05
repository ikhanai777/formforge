#include "frame_plan.h"

#include <cstring>

namespace spindle {

void set4(float* d, float x, float y, float z, float w) {
    d[0] = x;
    d[1] = y;
    d[2] = z;
    d[3] = w;
}
void set4(float* d, const vec3& v, float w) { set4(d, v.x, v.y, v.z, w); }

static float srgbToLinear1(float c) { return c <= 0.04045f ? c / 12.92f : std::pow((c + 0.055f) / 1.055f, 2.4f); }
vec3 srgbToLinear(const vec3& c) { return {srgbToLinear1(c.x), srgbToLinear1(c.y), srgbToLinear1(c.z)}; }

static vec3 dirFromAzEl(float azDeg, float elDeg) {
    float az = radians(azDeg), el = radians(elDeg);
    return {std::cos(el) * std::cos(az), std::cos(el) * std::sin(az), std::sin(el)};
}

float framingRadiusFor(const RenderContext& ctx, const Scene& s) {
    if (ctx.parts.size() < 2 || !(s.explode.animate || s.explode.manual > 0)) return ctx.sphereRadius;
    return explodedRadius(ctx.parts, ctx.sphereCenter, ctx.sphereRadius, s.explode.distance);
}

static void fillMaterial(MaterialConstants& c, const RenderContext& ctx, const Scene& s) {
    const MaterialSettings& m = s.material;
    std::memset(&c, 0, sizeof(c));
    set4(c.base, srgbToLinear(m.baseColor), clampf(m.roughness, 0, 1));
    set4(c.pbr, clampf(m.metalness, 0, 1), clampf(m.clearcoat, 0, 1), clampf(m.clearcoatRoughness, 0, 1),
         clampf(m.sheen, 0, 1));
    set4(c.trans, clampf(m.transmission, 0, 1), std::max(1.0f, m.ior), 0, 0);
    set4(c.transTint, srgbToLinear(m.transmissionTint), 0);
    set4(c.layer, m.layerLines ? 1.0f : 0.0f, std::max(0.01f, m.layerHeight), clampf(m.layerDepth, 0, 1), 0);
    set4(c.noise, std::max(0.0f, m.noiseStrength), std::max(0.001f, m.noiseScale), std::max(0.001f, m.noiseStretch), 0);
    int pattern = (int)m.pattern;
    if (m.pattern == Pattern::Image && !ctx.hasUserTexture) pattern = 0;
    set4(c.pattern, (float)pattern, std::max(0.01f, m.patternScale), clampf(m.patternStrength, 0, 1),
         clampf(m.triplanarSharpness, 1, 32));
    set4(c.patternColor, srgbToLinear(m.patternColor), 0);
    set4(c.gradient, srgbToLinear(m.gradientColor), m.gradient ? 1.0f : 0.0f);
    set4(c.gradientRange, ctx.minZ, ctx.maxZ, 0, 0);
    set4(c.wire, srgbToLinear(m.wireColor), m.wireframe ? std::max(0.5f, m.wireWidth) : 0.0f);
    set4(c.fileFlags, m.useFileColors ? 1.0f : 0.0f, m.useFileFinish ? 1.0f : 0.0f, 0, 0);
}

FramePlan planFrame(const RenderContext& ctx, const Scene& s, const SampleInput& in, int width, int height) {
    FramePlan plan;
    const EnvironmentSettings& e = s.environment;
    const bool mesh = ctx.hasMesh;

    // ---- explode: per-part offsets in the model's own frame ----
    plan.sceneRadius = ctx.sphereRadius;
    if (mesh && ctx.parts.size() > 1 && in.explode > 0) {
        std::vector<float> amounts(ctx.parts.size());
        for (size_t i = 0; i < ctx.parts.size(); ++i)
            amounts[i] = partExplodeAmount(in.explode, ctx.parts[i].order, s.explode.stagger);
        explodeOffsets(ctx.parts, ctx.sphereCenter, ctx.sphereRadius, s.explode.distance, amounts, plan.partOffsets);
        plan.sceneRadius = framingRadiusFor(ctx, s);
    }

    // ---- key light (casts the shadow map) ----
    vec3 keyDir;
    float keyIntensity = 0, envShadow;
    if (e.lights.enabled) {
        keyDir = dirFromAzEl(in.lightReferenceAzimuth + e.lights.keyAzimuth, clampf(e.lights.keyElevation, 5.0f, 89.0f));
        keyIntensity = std::max(0.0f, e.lights.intensity);
        envShadow = e.shadowStrength * 0.35f;
    } else {
        // Shadow from the environment's dominant light, darkening IBL in proportion
        // to how directional the environment is.
        vec3 d = ctx.envDominantDir;
        float r = radians(e.rotation);
        keyDir = {std::cos(r) * d.x - std::sin(r) * d.y, std::sin(r) * d.x + std::cos(r) * d.y, d.z};
        envShadow = e.shadowStrength * clampf(0.3f + ctx.envDirectionality * 1.6f, 0.0f, 1.0f);
    }
    // Soft shadows: jitter the light across a cone, one direction per sample.
    if (in.fullQuality && in.sampleIndex > 0 && e.shadowSoftness > 0) {
        vec3 helper = std::fabs(keyDir.z) < 0.99f ? vec3(0, 0, 1) : vec3(1, 0, 0);
        vec3 b1 = normalize(cross(helper, keyDir)), b2 = cross(keyDir, b1);
        float u = halton(in.sampleIndex + 11, 5), v = halton(in.sampleIndex + 11, 7);
        float rr = std::sqrt(u) * std::tan(radians(e.shadowSoftness * 0.5f));
        float phi = 2 * kPi * v;
        keyDir = normalize(keyDir + b1 * (rr * std::cos(phi)) + b2 * (rr * std::sin(phi)));
    }
    const vec3 c = ctx.sphereCenter;
    const float R = plan.sceneRadius;
    mat4 lightView = mat4::lookAt(c + keyDir * (4.0f * R), c, vec3(0, 0, 1));
    mat4 lightProj = mat4::orthographic(R * 1.02f, R * 1.02f, R * 0.5f, R * 40.0f);

    // ---- frame constants ----
    FrameConstants& f = plan.frame;
    f = FrameConstants();
    f.view = in.view.view;
    f.proj = in.view.proj;
    f.viewProj = in.view.proj * in.view.view;
    f.projInv = in.view.proj.inverse();
    f.invViewProj = f.viewProj.inverse();
    f.lightViewProj = lightProj * lightView;
    f.world = mat4::rotationZ(in.objectAngle);
    f.mirror = mat4::identity();
    set4(f.eye, in.view.eye, in.view.ortho ? 1.0f : 0.0f);
    set4(f.forward, in.view.forward, 0);
    set4(f.screen, (float)width, (float)height, 1.0f / width, 1.0f / height);

    vec3 keyColor = e.lights.enabled ? srgbToLinear(e.lights.keyColor) : ctx.envDominantColor;
    set4(f.keyDir, keyDir, keyIntensity);
    set4(f.keyColor, keyColor, clampf(envShadow, 0, 1));
    if (e.lights.enabled) {
        // Fill and rim follow the key: fill low on the opposite side, rim behind.
        float az = in.lightReferenceAzimuth;
        set4(f.fillDir, dirFromAzEl(az - 60.0f, 15.0f), e.lights.intensity * e.lights.fill);
        set4(f.fillColor, vec3(0.92f, 0.95f, 1.0f), 0);
        set4(f.rimDir, dirFromAzEl(az + 160.0f, 25.0f), e.lights.intensity * e.lights.rim);
        set4(f.rimColor, vec3(1, 1, 1), 0);
    }
    set4(f.env, std::max(0.0f, e.intensity), radians(e.rotation), clampf(e.saturation, 0, 2), (float)(ctx.specMips - 1));
    for (int i = 0; i < 9; ++i) set4(f.sh[i], ctx.sh[i], 0);

    set4(f.background, (float)(int)e.background, clampf(e.backgroundBlur, 0, 1),
         e.background == Background::Transparent ? 1.0f : 0.0f, (float)(ctx.envMips - 1));
    set4(f.bgColor, srgbToLinear(e.backgroundColor), 0);
    set4(f.bgColor2, srgbToLinear(e.backgroundColor2), 0);
    set4(f.ground, (float)(int)e.ground, clampf(e.floorRoughness, 0, 1), clampf(e.reflection, 0, 1),
         clampf(e.shadowStrength, 0, 1));
    set4(f.floorColor, srgbToLinear(e.floorColor), R * 10.0f);
    plan.aoEnabled = in.fullQuality && e.aoStrength > 0;
    set4(f.ao, clampf(e.aoStrength, 0, 1), std::max(1e-4f, e.aoRadius) * ctx.sphereRadius, plan.aoEnabled ? 1.0f : 0.0f,
         (float)in.sampleIndex);
    float texelWorld = 2.04f * R / (float)ctx.shadowMapSize;
    float pcf = in.sampleIndex > 0 && in.fullQuality ? 1.0f : 1.5f + e.shadowSoftness * 0.35f;
    plan.shadowEnabled = mesh;
    set4(f.shadow, 1.0f / (float)ctx.shadowMapSize, texelWorld * 1.5f, pcf, mesh ? 1.0f : 0.0f);
    set4(f.tone, std::pow(2.0f, e.exposure), (float)(int)e.tonemap, 1.0f, 0.0f);
    set4(f.model, ctx.sphereCenter, ctx.sphereRadius);
    set4(f.pass, 0, 0, (float)in.sampleIndex, 0);

    plan.groundEnabled = e.ground != Ground::None;
    plan.reflection = e.ground == Ground::Reflective && mesh && e.reflection > 0;
    fillMaterial(plan.material, ctx, s);
    return plan;
}

}  // namespace spindle
