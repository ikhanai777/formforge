// GLSL ES 3.00 port of shaders.hlsl. Same passes, same constants, with these
// OpenGL conventions:
//   * Uniform blocks are std140 + row_major, so `M * v` matches HLSL mul(M, v).
//   * Projection matrices are converted to GL clip space ([-1, 1] depth) on the
//     C++ side, so stored depth values match the Direct3D renderer.
//   * Screen-space lookups use gl_FragCoord (origin bottom-left) throughout.
#include "gl_shaders.h"

namespace spindle {
namespace {

const char* kVersion = "#version 300 es\n";

const char* kBlocks = R"GLSL(
precision highp float;
precision highp int;

layout(std140, row_major) uniform FrameCB
{
    mat4 gViewProj;
    mat4 gView;
    mat4 gProj;
    mat4 gProjInv;
    mat4 gInvViewProj;
    mat4 gLightViewProj;
    mat4 gWorld;
    mat4 gMirror;
    vec4 gEye;
    vec4 gForward;
    vec4 gScreen;
    vec4 gKeyDir;
    vec4 gKeyColor;
    vec4 gFillDir;
    vec4 gFillColor;
    vec4 gRimDir;
    vec4 gRimColor;
    vec4 gEnv;
    vec4 gSH[9];
    vec4 gBackground;
    vec4 gBgColor;
    vec4 gBgColor2;
    vec4 gGround;
    vec4 gFloorColor;
    vec4 gAO;
    vec4 gShadow;
    vec4 gTone;
    vec4 gModel;
    vec4 gPass;
};

layout(std140) uniform MaterialCB
{
    vec4 gBase;
    vec4 gPbr;
    vec4 gTrans;
    vec4 gTransTint;
    vec4 gLayer;
    vec4 gNoise;
    vec4 gPattern;
    vec4 gPatternColor;
    vec4 gGradient;
    vec4 gGradientRange;
    vec4 gWire;
    vec4 gFileFlags;
};

layout(std140) uniform BakeCB
{
    vec4 gFace;
};

const float PI = 3.14159265359;
)GLSL";

const char* kPartsBlock = R"GLSL(
layout(std140) uniform PartsCB
{
    vec4 gPartOffset[1024];
};
)GLSL";

// ---------------------------------------------------------------------------
// Vertex shaders
// ---------------------------------------------------------------------------

const char* kVsMesh = R"GLSL(
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNrm;
layout(location = 2) in vec4 aColor;   // sRGB file colour, a = has colour
layout(location = 3) in uint aExtra;   // part (0-11), has PBR (12), metal (16-23), rough (24-31)

out vec3 vWpos;
out vec3 vWnrm;
out vec3 vOpos;
out vec3 vOnrm;
flat out vec4 vColor;
flat out vec3 vPbr;

vec3 srgbToLinearV(vec3 c)
{
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(vec3(0.04045), c));
}

void main()
{
    vec3 p = aPos + gPartOffset[aExtra & 0xFFFu].xyz;
    vec4 w = gWorld * vec4(p, 1.0);
    gl_Position = gViewProj * (gMirror * w);
    vWpos = w.xyz;
    vWnrm = mat3(gWorld) * aNrm;
    vOpos = aPos;  // unexploded: textures and layer lines travel with the part
    vOnrm = aNrm;
    vColor = vec4(srgbToLinearV(aColor.rgb), aColor.a);
    vPbr = vec3(float((aExtra >> 16u) & 255u) / 255.0, float((aExtra >> 24u) & 255u) / 255.0, float((aExtra >> 12u) & 1u));
}
)GLSL";

const char* kVsShadow = R"GLSL(
layout(location = 0) in vec3 aPos;
layout(location = 3) in uint aExtra;
void main()
{
    vec3 p = aPos + gPartOffset[aExtra & 0xFFFu].xyz;
    gl_Position = gLightViewProj * (gWorld * vec4(p, 1.0));
}
)GLSL";

const char* kVsGround = R"GLSL(
out vec3 vWpos;
void main()
{
    vec2 corners[6] = vec2[6](vec2(-1, -1), vec2(1, -1), vec2(1, 1), vec2(-1, -1), vec2(1, 1), vec2(-1, 1));
    vec3 p = vec3(corners[gl_VertexID] * gFloorColor.w * 1.02, 0.0);
    gl_Position = gViewProj * vec4(p, 1.0);
    vWpos = p;
}
)GLSL";

const char* kVsFullscreen = R"GLSL(
out vec2 vUV;
void main()
{
    vec2 uv = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
    vUV = uv;  // (0,0) bottom-left
}
)GLSL";

// ---------------------------------------------------------------------------
// Fragment helpers
// ---------------------------------------------------------------------------

const char* kFsCommon = R"GLSL(
precision highp sampler2D;
precision highp samplerCube;
precision highp sampler2DShadow;

uniform sampler2DShadow tShadow;   // unit 0
uniform samplerCube tSpec;         // 1
uniform sampler2D tBrdf;           // 2
uniform sampler2D tAO;             // 3
uniform sampler2D tRefl;           // 4
uniform sampler2D tUser;           // 5
uniform sampler2D tDepth;          // 6
uniform sampler2D tSrc;            // 7
uniform sampler2D tAccum;          // 8
uniform sampler2D tEquirect;       // 9
uniform samplerCube tEnv;          // 10
uniform sampler2D tAORaw;          // 11

vec3 rotZ(vec3 d, float a)
{
    float c = cos(a), s = sin(a);
    return vec3(c * d.x - s * d.y, s * d.x + c * d.y, d.z);
}

vec3 envSpace(vec3 d) { return rotZ(d, -gEnv.y); }

float luminance(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }

vec3 adjustSaturation(vec3 c, float s) { return max(vec3(0.0), mix(vec3(luminance(c)), c, s)); }

vec2 equirectUV(vec3 d)
{
    float u = 0.5 - atan(d.y, d.x) / (2.0 * PI);
    float v = acos(clamp(d.z, -1.0, 1.0)) / PI;
    return vec2(u, v);
}

vec3 shIrradiance(vec3 n)
{
    vec3 r = gSH[0].rgb * 0.282095
           + gSH[1].rgb * (0.488603 * n.y)
           + gSH[2].rgb * (0.488603 * n.z)
           + gSH[3].rgb * (0.488603 * n.x)
           + gSH[4].rgb * (1.092548 * n.x * n.y)
           + gSH[5].rgb * (1.092548 * n.y * n.z)
           + gSH[6].rgb * (0.315392 * (3.0 * n.z * n.z - 1.0))
           + gSH[7].rgb * (1.092548 * n.x * n.z)
           + gSH[8].rgb * (0.546274 * (n.x * n.x - n.y * n.y));
    return max(r, vec3(0.0));
}

vec3 envDiffuse(vec3 n) { return adjustSaturation(shIrradiance(envSpace(n)), gEnv.z) * gEnv.x; }

vec3 envSpecular(vec3 r, float roughness)
{
    vec3 c = textureLod(tSpec, envSpace(r), clamp(roughness, 0.0, 1.0) * gEnv.w).rgb;
    return adjustSaturation(c, gEnv.z) * gEnv.x;
}

float interleavedGradientNoise(vec2 p)
{
    return fract(52.9829189 * fract(dot(p, vec2(0.06711056, 0.00583715))));
}

float D_GGX(float NoH, float a)
{
    float a2 = a * a;
    float d = NoH * NoH * (a2 - 1.0) + 1.0;
    return a2 / (PI * d * d + 1e-7);
}

float V_SmithGGXCorrelated(float NoV, float NoL, float a)
{
    float a2 = a * a;
    float gv = NoL * sqrt(NoV * NoV * (1.0 - a2) + a2);
    float gl = NoV * sqrt(NoL * NoL * (1.0 - a2) + a2);
    return 0.5 / (gv + gl + 1e-6);
}

vec3 F_Schlick(vec3 f0, float VoH) { return f0 + (1.0 - f0) * pow(1.0 - VoH, 5.0); }

vec3 directLight(vec3 L, vec3 radiance, vec3 N, vec3 V, vec3 diffColor, vec3 f0, float roughness)
{
    float NoL = clamp(dot(N, L), 0.0, 1.0);
    if (NoL <= 0.0) return vec3(0.0);
    float a = max(roughness * roughness, 0.01);
    vec3 H = normalize(L + V);
    float NoH = clamp(dot(N, H), 0.0, 1.0);
    float VoH = clamp(dot(V, H), 0.0, 1.0);
    float NoV = max(dot(N, V), 1e-4);
    vec3 F = F_Schlick(f0, VoH);
    vec3 spec = D_GGX(NoH, a) * V_SmithGGXCorrelated(NoV, NoL, a) * F;
    vec3 diff = diffColor / PI * (1.0 - F);
    return (diff + spec) * radiance * NoL;
}

const vec2 kPoisson[12] = vec2[12](
    vec2(-0.326, -0.406), vec2(-0.840, -0.074), vec2(-0.696, 0.457), vec2(-0.203, 0.621),
    vec2(0.962, -0.195), vec2(0.473, -0.480), vec2(0.519, 0.767), vec2(0.185, -0.893),
    vec2(0.507, 0.064), vec2(0.896, 0.412), vec2(-0.322, -0.933), vec2(-0.792, -0.598));

float shadowFactor(vec3 P, vec3 Ng, vec2 pixel)
{
    if (gShadow.w < 0.5) return 1.0;
    vec3 Po = P + Ng * gShadow.y;
    vec4 lp = gLightViewProj * vec4(Po, 1.0);
    vec3 ndc = lp.xyz / lp.w;
    vec2 uv = ndc.xy * 0.5 + 0.5;
    float z = ndc.z * 0.5 + 0.5;
    if (any(lessThan(uv, vec2(0.0))) || any(greaterThan(uv, vec2(1.0))) || z > 1.0) return 1.0;
    z -= 0.0004;
    float angle = interleavedGradientNoise(pixel + gPass.z * 7.0) * 2.0 * PI;
    vec2 cs = vec2(cos(angle), sin(angle));
    float r = gShadow.z * gShadow.x;
    float sum = 0.0;
    for (int k = 0; k < 12; ++k)
    {
        vec2 o = vec2(kPoisson[k].x * cs.x - kPoisson[k].y * cs.y, kPoisson[k].x * cs.y + kPoisson[k].y * cs.x);
        sum += texture(tShadow, vec3(uv + o * r, z));
    }
    return sum / 12.0;
}

float aoAt(vec2 pixel)
{
    if (gAO.z < 0.5) return 1.0;
    return mix(1.0, texelFetch(tAO, ivec2(pixel), 0).r, gAO.x);
}

float hash13(vec3 p)
{
    p = fract(p * 0.1031);
    p += dot(p, p.zyx + 31.32);
    return fract((p.x + p.y) * p.z);
}

float vnoise(vec3 p)
{
    vec3 i = floor(p);
    vec3 f = fract(p);
    vec3 u = f * f * (3.0 - 2.0 * f);
    float n000 = hash13(i);
    float n100 = hash13(i + vec3(1, 0, 0));
    float n010 = hash13(i + vec3(0, 1, 0));
    float n110 = hash13(i + vec3(1, 1, 0));
    float n001 = hash13(i + vec3(0, 0, 1));
    float n101 = hash13(i + vec3(1, 0, 1));
    float n011 = hash13(i + vec3(0, 1, 1));
    float n111 = hash13(i + vec3(1, 1, 1));
    return mix(mix(mix(n000, n100, u.x), mix(n010, n110, u.x), u.y),
               mix(mix(n001, n101, u.x), mix(n011, n111, u.x), u.y), u.z);
}

float fbm(vec3 p)
{
    float s = 0.0, a = 0.5;
    for (int k = 0; k < 4; ++k)
    {
        s += a * vnoise(p);
        p = p * 2.03 + vec3(17.1, 9.7, 3.3);
        a *= 0.5;
    }
    return s / 0.9375;
}

vec3 triplanarWeights(vec3 n, float sharpness)
{
    vec3 w = pow(abs(n) + 1e-4, vec3(sharpness));
    return w / (w.x + w.y + w.z);
}

vec3 triplanarTexture(vec3 p, vec3 dpx, vec3 dpy, vec3 w)
{
    vec3 x = textureGrad(tUser, p.yz, dpx.yz, dpy.yz).rgb;
    vec3 y = textureGrad(tUser, p.xz, dpx.xz, dpy.xz).rgb;
    vec3 z = textureGrad(tUser, p.xy, dpx.xy, dpy.xy).rgb;
    return x * w.x + y * w.y + z * w.z;
}

float carbon2(vec2 uv)
{
    vec2 c = floor(uv);
    vec2 f = fract(uv);
    bool horizontal = mod(c.x + c.y + 1024.0, 4.0) < 2.0;
    float across = horizontal ? f.y : f.x;
    float along = horizontal ? f.x : f.y;
    float bundle = sin(across * PI);
    float fibres = 0.5 + 0.5 * sin(across * PI * 22.0);
    float shade = horizontal ? 1.0 : 0.65;
    return clamp(bundle * mix(0.8, 1.0, fibres) * shade * (0.85 + 0.15 * sin(along * PI)), 0.0, 1.0);
}

vec3 srgbToLinear(vec3 c)
{
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(vec3(0.04045), c));
}

vec3 linearToSrgb(vec3 c)
{
    c = clamp(c, 0.0, 1.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(vec3(0.0031308), c));
}
)GLSL";

// ---------------------------------------------------------------------------
// Fragment shaders
// ---------------------------------------------------------------------------

const char* kFsEmpty = R"GLSL(
void main() {}
)GLSL";

const char* kFsModel = R"GLSL(
in vec3 vWpos;
in vec3 vWnrm;
in vec3 vOpos;
in vec3 vOnrm;
flat in vec4 vColor;
flat in vec3 vPbr;
out vec4 fragColor;

void main()
{
    vec3 P = vWpos;
    vec3 V = gEye.w > 0.5 ? -gForward.xyz : normalize(gEye.xyz - P);

    // Orient to the visible side with the geometric normal (inverted winding safe).
    vec3 Ng = normalize(cross(dFdx(P), dFdy(P)));
    if (dot(Ng, V) < 0.0) Ng = -Ng;
    vec3 No = normalize(vOnrm);
    vec3 Nw = normalize(vWnrm);
    if (dot(Nw, Ng) < 0.0) { Nw = -Nw; No = -No; }

    vec3 op = vOpos;
    vec3 dpx = dFdx(op), dpy = dFdy(op);
    float footprint = max(length(dpx), length(dpy));
    float fwz = abs(dpx.z) + abs(dpy.z);

    vec3 albedo = gBase.rgb;
    float roughness = gBase.w;
    float metal = gPbr.x;
    if (gFileFlags.x > 0.5 && vColor.a > 0.5) albedo = vColor.rgb;
    if (gFileFlags.y > 0.5 && vPbr.z > 0.5)
    {
        metal = vPbr.x;
        roughness = vPbr.y;
    }

    if (gGradient.w > 0.5)
    {
        float t = clamp((op.z - gGradientRange.x) / max(1e-4, gGradientRange.y - gGradientRange.x), 0.0, 1.0);
        albedo = mix(albedo, gGradient.rgb, t);
    }

    int pattern = int(gPattern.x + 0.5);
    float scale = max(gPattern.y, 1e-3);
    float strength = gPattern.z;
    vec3 q = op / scale;
    vec3 tw = triplanarWeights(No, gPattern.w);
    if (pattern == 1)
    {
        vec3 t = triplanarTexture(q, dpx / scale, dpy / scale, tw);
        albedo = mix(albedo, t, strength);
    }
    else if (pattern == 2)
    {
        vec3 warp = vec3(fbm(q * 0.7), fbm(q * 0.7 + 5.2), 0.0) - 0.5;
        float r = length(q.xy + warp.xy * 1.2);
        float ring = fract(r + fbm(q * vec3(3.0, 3.0, 0.3)) * 0.25);
        float late = smoothstep(0.55, 0.8, ring) * (1.0 - smoothstep(0.9, 1.0, ring));
        float grain = vnoise(q * vec3(40.0, 40.0, 1.5));
        float t = clamp(late * 0.85 + grain * 0.2, 0.0, 1.0);
        albedo = mix(albedo, gPatternColor.rgb, t * strength);
    }
    else if (pattern == 3)
    {
        float v = sin((q.x + q.y * 0.6 + q.z * 0.3) * 2.0 * PI * 1.5 + fbm(q * 3.0) * 7.0);
        float vein = pow(1.0 - abs(v), 8.0);
        float cloud = fbm(q * 9.0);
        float t = clamp(vein * 0.9 + (cloud - 0.5) * 0.15, 0.0, 1.0);
        albedo = mix(albedo, gPatternColor.rgb, t * strength);
    }
    else if (pattern == 4)
    {
        float t = carbon2(q.yz) * tw.x + carbon2(q.xz) * tw.y + carbon2(q.xy) * tw.z;
        albedo = mix(gPatternColor.rgb, albedo, mix(1.0, t, strength));
        roughness = mix(roughness, roughness * (1.3 - 0.6 * t), strength);
    }
    else if (pattern == 5)
    {
        vec3 c = floor(q + 1e-3);
        float parity = mod(abs(c.x + c.y + c.z), 2.0);
        albedo = mix(albedo, gPatternColor.rgb, parity * strength);
    }
    else if (pattern == 6)
    {
        float n1 = vnoise(q * 4.0), n2 = vnoise(q * 9.0 + 5.0), n3 = vnoise(q * 17.0 + 11.0);
        float dark = smoothstep(0.55, 0.62, n1 * 0.6 + n2 * 0.4);
        float speck = smoothstep(0.80, 0.84, n3);
        albedo = mix(albedo, gPatternColor.rgb, dark * strength);
        albedo = mix(albedo, vec3(0.85), speck * 0.6 * strength);
    }

    vec3 g = vec3(0.0);
    if (gLayer.x > 0.5)
    {
        float H = max(gLayer.y, 1e-3);
        float t = fract(op.z / H);
        float fade = clamp(2.0 - 2.0 * fwz / H, 0.0, 1.0);
        g.z += gLayer.z * 1.5 * cos(PI * t) * fade;
    }
    if (gNoise.x > 0.0)
    {
        float s = max(gNoise.y, 1e-3);
        vec3 stretch = vec3(max(gNoise.z, 1e-3), 1.0, 1.0);
        vec3 nq = op / (s * stretch);
        const float e = 0.05;
        float n0 = fbm(nq);
        vec3 d = vec3(fbm(nq + vec3(e, 0, 0)), fbm(nq + vec3(0, e, 0)), fbm(nq + vec3(0, 0, e))) - n0;
        vec3 grad = d / e / (s * stretch);
        float fade = clamp(2.0 - 2.0 * footprint / s, 0.0, 1.0);
        g += grad * gNoise.x * s * 0.6 * fade;
    }
    vec3 Nb = normalize(No - (g - No * dot(No, g)));
    vec3 N = normalize(mat3(gWorld) * Nb);
    if (dot(N, V) < 0.0) N = normalize(N - V * (dot(N, V) - 1e-3));

    roughness = clamp(roughness, 0.02, 1.0);
    float NoV = max(dot(N, V), 1e-4);
    vec3 R = reflect(-V, N);
    vec3 f0 = mix(vec3(0.04), albedo, metal);
    vec3 diffColor = albedo * (1.0 - metal);
    vec2 ab = textureLod(tBrdf, vec2(NoV, roughness), 0.0).rg;
    vec3 specWeight = f0 * ab.x + ab.y;

    float ao = aoAt(gl_FragCoord.xy);
    float specAO = clamp(pow(NoV + ao, exp2(-16.0 * roughness - 1.0)) - 1.0 + ao, 0.0, 1.0);
    float sh = shadowFactor(P, Ng, gl_FragCoord.xy);
    float facing = clamp(dot(Ng, gKeyDir.xyz) * 2.0, 0.0, 1.0);
    float envVis = 1.0 - gKeyColor.w * (1.0 - sh) * facing;

    vec3 diffuse = envDiffuse(N) * diffColor * ao;
    vec3 specular = envSpecular(R, roughness) * specWeight * specAO;

    if (gTrans.x > 0.0)
    {
        vec3 T = refract(-V, N, 1.0 / max(gTrans.y, 1.0));
        if (dot(T, T) < 1e-4) T = R;
        vec3 through = envSpecular(T, clamp(roughness + 0.15, 0.0, 1.0)) * gTransTint.rgb;
        diffuse = mix(diffuse, through * (1.0 - specWeight), gTrans.x);
    }

    vec3 color = (diffuse + specular) * envVis;
    color += directLight(gKeyDir.xyz, gKeyColor.rgb * gKeyDir.w, N, V, diffColor, f0, roughness) * sh;
    color += directLight(gFillDir.xyz, gFillColor.rgb * gFillDir.w, N, V, diffColor, f0, roughness) * ao;
    color += directLight(gRimDir.xyz, gRimColor.rgb * gRimDir.w, N, V, diffColor, f0, roughness);

    if (gPbr.w > 0.0)
        color += gPbr.w * pow(1.0 - NoV, 3.0) * envDiffuse(N) * mix(albedo, vec3(1.0), 0.4) * ao;

    if (gPbr.y > 0.0)
    {
        float ccr = clamp(gPbr.z, 0.02, 1.0);
        float NoVc = max(dot(Nw, V), 1e-4);
        vec2 abc = textureLod(tBrdf, vec2(NoVc, ccr), 0.0).rg;
        float Fc = (0.04 + 0.96 * pow(1.0 - NoVc, 5.0)) * gPbr.y;
        vec3 Rc = reflect(-V, Nw);
        vec3 cc = envSpecular(Rc, ccr) * (0.04 * abc.x + abc.y) * gPbr.y * specAO * envVis;
        cc += directLight(gKeyDir.xyz, gKeyColor.rgb * gKeyDir.w, Nw, V, vec3(0.0), vec3(0.04), ccr) * gPbr.y * sh;
        color = color * (1.0 - Fc) + cc;
    }
    fragColor = vec4(max(color, vec3(0.0)), 1.0);
}
)GLSL";

const char* kFsGround = R"GLSL(
in vec3 vWpos;
out vec4 fragColor;

void main()
{
    vec3 P = vWpos;
    vec3 N = vec3(0, 0, 1);
    float fadeR = gFloorColor.w;
    float fade = 1.0 - smoothstep(fadeR * 0.25, fadeR, length(P.xy));
    float sh = shadowFactor(P, N, gl_FragCoord.xy);
    float ao = aoAt(gl_FragCoord.xy);
    int mode = int(gGround.x + 0.5);

    if (mode == 1)
    {
        float lit = (1.0 - (1.0 - sh) * gGround.w) * ao;
        fragColor = vec4(0.0, 0.0, 0.0, clamp(1.0 - lit, 0.0, 1.0) * fade);
        return;
    }

    vec3 V = gEye.w > 0.5 ? -gForward.xyz : normalize(gEye.xyz - P);
    float NoV = max(V.z, 1e-4);
    float rough = clamp(gGround.y, 0.02, 1.0);
    float envVis = 1.0 - gGround.w * (1.0 - sh);
    vec3 albedo = gFloorColor.rgb;
    vec2 ab = textureLod(tBrdf, vec2(NoV, rough), 0.0).rg;
    vec3 specW = vec3(0.04 * ab.x + ab.y);
    vec3 col = envDiffuse(N) * albedo * ao * envVis + envSpecular(reflect(-V, N), rough) * specW * ao * envVis;
    col += directLight(gKeyDir.xyz, gKeyColor.rgb * gKeyDir.w, N, V, albedo, vec3(0.04), rough) * sh;
    col += directLight(gFillDir.xyz, gFillColor.rgb * gFillDir.w, N, V, albedo, vec3(0.04), rough) * ao;

    if (mode == 3)
    {
        vec2 uv = gl_FragCoord.xy * gScreen.zw;
        vec4 r = textureLod(tRefl, uv, rough * 5.0);
        float k = gGround.z * mix(0.35, 1.0, pow(1.0 - NoV, 2.0));
        col = col * (1.0 - k * r.a) + r.rgb * k;
    }
    fragColor = vec4(col * fade, fade);
}
)GLSL";

const char* kFsSsao = R"GLSL(
out vec4 fragColor;

vec3 viewPosition(vec2 uv, float depth)
{
    vec4 v = gProjInv * vec4(uv * 2.0 - 1.0, depth * 2.0 - 1.0, 1.0);
    return v.xyz / v.w;
}

float depthAt(ivec2 p)
{
    ivec2 size = textureSize(tDepth, 0);
    return texelFetch(tDepth, clamp(p, ivec2(0), size - 1), 0).r;
}

void main()
{
    ivec2 px = ivec2(gl_FragCoord.xy);
    float d = depthAt(px);
    if (d >= 1.0) { fragColor = vec4(1.0); return; }
    vec2 uv = (vec2(px) + 0.5) * gScreen.zw;
    vec3 P = viewPosition(uv, d);

    vec3 Pr = viewPosition(uv + vec2(gScreen.z, 0.0), depthAt(px + ivec2(1, 0)));
    vec3 Pl = viewPosition(uv - vec2(gScreen.z, 0.0), depthAt(px - ivec2(1, 0)));
    vec3 Pu = viewPosition(uv + vec2(0.0, gScreen.w), depthAt(px + ivec2(0, 1)));
    vec3 Pd = viewPosition(uv - vec2(0.0, gScreen.w), depthAt(px - ivec2(0, 1)));
    vec3 dx = abs(Pr.z - P.z) < abs(P.z - Pl.z) ? Pr - P : P - Pl;
    vec3 dy = abs(Pu.z - P.z) < abs(P.z - Pd.z) ? Pu - P : P - Pd;
    vec3 N = normalize(cross(dx, dy));
    vec3 toEye = gEye.w > 0.5 ? vec3(0, 0, 1) : normalize(-P);
    if (dot(N, toEye) < 0.0) N = -N;

    float angle = interleavedGradientNoise(vec2(px) + gAO.w * 5.588238) * 2.0 * PI;
    vec3 helper = abs(N.z) < 0.9 ? vec3(0, 0, 1) : vec3(1, 0, 0);
    vec3 T = normalize(cross(helper, N));
    vec3 B = cross(N, T);
    float ca = cos(angle), sa = sin(angle);
    vec3 T2 = T * ca + B * sa;
    vec3 B2 = B * ca - T * sa;

    float R = gAO.y;
    float occlusion = 0.0;
    const int COUNT = 12;
    for (int k = 0; k < COUNT; ++k)
    {
        float u = (float(k) + 0.5) / float(COUNT);
        float phi = float(k) * 2.39996323;
        float r = sqrt(u);
        vec3 dir = vec3(r * cos(phi), r * sin(phi), sqrt(1.0 - u));
        float len = mix(0.15, 1.0, float(k * k) / float(COUNT * COUNT));
        vec3 S = P + (T2 * dir.x + B2 * dir.y + N * dir.z) * (R * len);
        vec4 c = gProj * vec4(S, 1.0);
        vec2 suv = c.xy / c.w * 0.5 + 0.5;
        if (any(lessThan(suv, vec2(0.0))) || any(greaterThan(suv, vec2(1.0)))) continue;
        float sd = depthAt(ivec2(suv * gScreen.xy));
        vec3 Sp = viewPosition(suv, sd);
        float range = smoothstep(0.0, 1.0, R / max(abs(P.z - Sp.z), 1e-5));
        occlusion += (Sp.z >= S.z + 0.02 * R ? 1.0 : 0.0) * range;
    }
    fragColor = vec4(clamp(1.0 - occlusion / float(COUNT), 0.0, 1.0));
}
)GLSL";

const char* kFsAoBlur = R"GLSL(
out vec4 fragColor;

vec3 viewPosition(vec2 uv, float depth)
{
    vec4 v = gProjInv * vec4(uv * 2.0 - 1.0, depth * 2.0 - 1.0, 1.0);
    return v.xyz / v.w;
}

void main()
{
    ivec2 px = ivec2(gl_FragCoord.xy);
    ivec2 size = textureSize(tDepth, 0);
    float d0 = texelFetch(tDepth, px, 0).r;
    if (d0 >= 1.0) { fragColor = vec4(1.0); return; }
    float z0 = viewPosition((vec2(px) + 0.5) * gScreen.zw, d0).z;
    float sum = 0.0, wsum = 0.0;
    for (int y = -2; y <= 1; ++y)
    for (int x = -2; x <= 1; ++x)
    {
        ivec2 p = clamp(px + ivec2(x, y), ivec2(0), size - 1);
        float d = texelFetch(tDepth, p, 0).r;
        float z = viewPosition((vec2(p) + 0.5) * gScreen.zw, d).z;
        float w = abs(z - z0) < gAO.y * 0.25 ? 1.0 : 0.0;
        sum += texelFetch(tAORaw, p, 0).r * w;
        wsum += w;
    }
    fragColor = vec4(wsum > 0.0 ? sum / wsum : 1.0);
}
)GLSL";

const char* kFsTonemap = R"GLSL(
in vec2 vUV;
out vec4 fragColor;

vec3 tonemapACES(vec3 x)
{
    x *= 0.6;
    return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
}

vec3 agxContrast(vec3 x)
{
    vec3 x2 = x * x;
    vec3 x4 = x2 * x2;
    return 15.5 * x4 * x2 - 40.14 * x4 * x + 31.96 * x4 - 6.868 * x2 * x + 0.4298 * x2 + 0.1191 * x - 0.00232;
}

vec3 tonemapAgX(vec3 c)
{
    // mat3(...) takes columns: inMat * c equals HLSL mul(c, inMat) with the same literals.
    const mat3 inMat = mat3(0.842479062253094, 0.0423282422610123, 0.0423756549057051,
                            0.0784335999999992, 0.878468636469772, 0.0784336,
                            0.0792237451477643, 0.0791661274605434, 0.879142973793104);
    const mat3 outMat = mat3(1.19687900512017, -0.0528968517574562, -0.0529716355144438,
                             -0.0980208811401368, 1.15190312990417, -0.0980434501171241,
                             -0.0990297440797205, -0.0989611768448433, 1.15107367264116);
    const float minEv = -12.47393, maxEv = 4.026069;
    c = inMat * max(c, vec3(1e-10));
    c = clamp(log2(max(c, vec3(1e-10))), minEv, maxEv);
    c = (c - minEv) / (maxEv - minEv);
    c = agxContrast(c);
    c = outMat * c;
    return pow(clamp(c, 0.0, 1.0), vec3(2.2));
}

vec3 tonemap(vec3 c)
{
    int mode = int(gTone.y + 0.5);
    if (mode == 1) return tonemapAgX(c);
    if (mode == 2) return clamp(c, 0.0, 1.0);
    return tonemapACES(c);
}

vec3 backgroundColor(vec2 uv)
{
    int mode = int(gBackground.x + 0.5);
    if (mode == 0)
    {
        vec4 wp = gInvViewProj * vec4(uv * 2.0 - 1.0, 1.0, 1.0);
        vec3 dir = gEye.w > 0.5 ? gForward.xyz : normalize(wp.xyz / wp.w - gEye.xyz);
        vec3 d = envSpace(dir);
        vec3 c = gBackground.y > 0.0
            ? textureLod(tSpec, d, gBackground.y * gEnv.w).rgb
            : textureLod(tEquirect, equirectUV(d), 0.0).rgb;
        return tonemap(adjustSaturation(c, gEnv.z) * gEnv.x * gTone.x);
    }
    float top = 1.0 - uv.y;  // 0 at the top of the frame
    if (mode == 1) return gBgColor.rgb;
    if (mode == 2) return mix(gBgColor.rgb, gBgColor2.rgb, top);
    if (mode == 3)
    {
        float aspect = gScreen.x * gScreen.w;
        vec2 p = (uv - 0.5) * vec2(aspect, 1.0);
        float r = length(p) / (0.5 * sqrt(aspect * aspect + 1.0));
        return mix(gBgColor.rgb, gBgColor2.rgb, smoothstep(0.0, 1.0, r));
    }
    return vec3(0.0);
}

// Tonemaps one sample over the background and folds it into the running
// average: accum = mix(previous, sample, 1 / (n + 1)) (gTone.z holds the weight).
void main()
{
    ivec2 px = ivec2(gl_FragCoord.xy);
    vec4 hdr = texelFetch(tSrc, px, 0);
    float a = clamp(hdr.a, 0.0, 1.0);
    vec3 scene = a > 1e-5 ? tonemap(hdr.rgb / a * gTone.x) * a : vec3(0.0);
    vec3 rgb = scene + backgroundColor(vUV) * (1.0 - a);
    float alpha = gBackground.z > 0.5 ? a : 1.0;
    vec4 sampleColor = vec4(rgb, alpha);
    vec4 previous = texelFetch(tAccum, px, 0);
    fragColor = mix(previous, sampleColor, gTone.z);
}
)GLSL";

const char* kFsResolve = R"GLSL(
out vec4 fragColor;
void main()
{
    vec4 acc = texelFetch(tAccum, ivec2(gl_FragCoord.xy), 0);
    float a = clamp(acc.a, 0.0, 1.0);
    vec3 rgb = acc.rgb;
    if (gTone.w < 0.5) rgb = a > 1e-4 ? rgb / a : vec3(0.0);
    else a = 1.0;
    vec3 s = linearToSrgb(rgb);
    float n = interleavedGradientNoise(gl_FragCoord.xy) + interleavedGradientNoise(gl_FragCoord.xy + 37.0) - 1.0;
    s += n / 255.0;
    fragColor = vec4(clamp(s, 0.0, 1.0), a);
}
)GLSL";

const char* kFsBlit = R"GLSL(
in vec2 vUV;
out vec4 fragColor;
void main()
{
    vec4 c = textureLod(tSrc, vUV, 0.0);
    if (gPass.y > 0.5)
    {
        vec2 cell = floor(gl_FragCoord.xy / 10.0);
        float ck = mod(cell.x + cell.y, 2.0) < 1.0 ? 0.80 : 0.62;
        c.rgb = mix(vec3(ck), c.rgb, c.a);
    }
    fragColor = vec4(c.rgb, 1.0);
}
)GLSL";

const char* kFsCubeCommon = R"GLSL(
vec3 cubeDirection(int face, vec2 uv)
{
    float u = uv.x * 2.0 - 1.0;
    float v = uv.y * 2.0 - 1.0;
    vec3 d;
    if (face == 0) d = vec3(1.0, -v, -u);
    else if (face == 1) d = vec3(-1.0, -v, u);
    else if (face == 2) d = vec3(u, 1.0, v);
    else if (face == 3) d = vec3(u, -1.0, -v);
    else if (face == 4) d = vec3(u, -v, 1.0);
    else d = vec3(-u, -v, -1.0);
    return normalize(d);
}

float radicalInverse(uint bits)
{
    bits = (bits << 16u) | (bits >> 16u);
    bits = ((bits & 0x55555555u) << 1u) | ((bits & 0xAAAAAAAAu) >> 1u);
    bits = ((bits & 0x33333333u) << 2u) | ((bits & 0xCCCCCCCCu) >> 2u);
    bits = ((bits & 0x0F0F0F0Fu) << 4u) | ((bits & 0xF0F0F0F0u) >> 4u);
    bits = ((bits & 0x00FF00FFu) << 8u) | ((bits & 0xFF00FF00u) >> 8u);
    return float(bits) * 2.3283064365386963e-10;
}

vec2 hammersley(uint i, uint n) { return vec2((float(i) + 0.5) / float(n), radicalInverse(i)); }

vec3 importanceSampleGGX(vec2 xi, float a, vec3 N, vec3 T, vec3 B)
{
    float phi = 2.0 * PI * xi.x;
    float cosT = sqrt((1.0 - xi.y) / (1.0 + (a * a - 1.0) * xi.y));
    float sinT = sqrt(max(0.0, 1.0 - cosT * cosT));
    return normalize(T * (sinT * cos(phi)) + B * (sinT * sin(phi)) + N * cosT);
}
)GLSL";

// Cube faces are rendered with uv from gl_FragCoord: texel row 0 is t = 0 in
// both APIs, so the face formulas are the same as in the HLSL.
const char* kFsEquirectToCube = R"GLSL(
out vec4 fragColor;
void main()
{
    vec2 uv = gl_FragCoord.xy * gScreen.zw;
    vec3 d = cubeDirection(int(gFace.x + 0.5), uv);
    fragColor = vec4(textureLod(tEquirect, equirectUV(d), gFace.z).rgb, 1.0);
}
)GLSL";

const char* kFsPrefilter = R"GLSL(
out vec4 fragColor;
void main()
{
    vec2 uv = gl_FragCoord.xy * gScreen.zw;
    vec3 N = cubeDirection(int(gFace.x + 0.5), uv);
    float roughness = gFace.y;
    if (roughness <= 0.0) { fragColor = vec4(textureLod(tEnv, N, 0.0).rgb, 1.0); return; }
    float a = roughness * roughness;
    vec3 helper = abs(N.z) < 0.999 ? vec3(0, 0, 1) : vec3(1, 0, 0);
    vec3 T = normalize(cross(helper, N));
    vec3 B = cross(N, T);
    const uint COUNT = 64u;
    float srcRes = gFace.z;
    float saTexel = 4.0 * PI / (6.0 * srcRes * srcRes);
    vec3 sum = vec3(0.0);
    float wsum = 0.0;
    for (uint k = 0u; k < COUNT; ++k)
    {
        vec3 H = importanceSampleGGX(hammersley(k, COUNT), a, N, T, B);
        float NoH = clamp(dot(N, H), 0.0, 1.0);
        vec3 L = 2.0 * NoH * H - N;
        float NoL = dot(N, L);
        if (NoL > 0.0)
        {
            float pdf = D_GGX(NoH, a) * 0.25 + 1e-4;
            float saSample = 1.0 / (float(COUNT) * pdf);
            float lod = max(0.5 * log2(saSample / saTexel) + 1.0, 0.0);
            sum += textureLod(tEnv, L, lod).rgb * NoL;
            wsum += NoL;
        }
    }
    fragColor = vec4(sum / max(wsum, 1e-4), 1.0);
}
)GLSL";

const char* kFsBrdfLut = R"GLSL(
out vec4 fragColor;
void main()
{
    vec2 uv = gl_FragCoord.xy * gScreen.zw;
    float NoV = max(uv.x, 1e-3);
    float roughness = max(uv.y, 0.02);
    float a = roughness * roughness;
    vec3 V = vec3(sqrt(1.0 - NoV * NoV), 0.0, NoV);
    vec3 N = vec3(0, 0, 1), T = vec3(1, 0, 0), B = vec3(0, 1, 0);
    float A = 0.0, Bs = 0.0;
    const uint COUNT = 256u;
    for (uint k = 0u; k < COUNT; ++k)
    {
        vec3 H = importanceSampleGGX(hammersley(k, COUNT), a, N, T, B);
        vec3 L = 2.0 * dot(V, H) * H - V;
        float NoL = clamp(L.z, 0.0, 1.0);
        float NoH = clamp(H.z, 0.0, 1.0);
        float VoH = clamp(dot(V, H), 0.0, 1.0);
        if (NoL > 0.0)
        {
            float vis = V_SmithGGXCorrelated(NoV, NoL, a);
            float gv = 4.0 * vis * NoL * VoH / max(NoH, 1e-4);
            float fc = pow(1.0 - VoH, 5.0);
            A += (1.0 - fc) * gv;
            Bs += fc * gv;
        }
    }
    fragColor = vec4(A / float(COUNT), Bs / float(COUNT), 0.0, 1.0);
}
)GLSL";

}  // namespace

std::string glslVertex(GlVertexShader v) {
    std::string s = std::string(kVersion) + kBlocks;
    switch (v) {
        case GlVertexShader::Mesh: return s + kPartsBlock + kVsMesh;
        case GlVertexShader::Shadow: return s + kPartsBlock + kVsShadow;
        case GlVertexShader::Ground: return s + kVsGround;
        case GlVertexShader::Fullscreen: return s + kVsFullscreen;
    }
    return s;
}

std::string glslFragment(GlFragmentShader f) {
    std::string s = std::string(kVersion) + kBlocks + kFsCommon;
    switch (f) {
        case GlFragmentShader::Empty: return std::string(kVersion) + "precision mediump float;\n" + kFsEmpty;
        case GlFragmentShader::Model: return s + kFsModel;
        case GlFragmentShader::Ground: return s + kFsGround;
        case GlFragmentShader::Ssao: return s + kFsSsao;
        case GlFragmentShader::AoBlur: return s + kFsAoBlur;
        case GlFragmentShader::TonemapAccumulate: return s + kFsTonemap;
        case GlFragmentShader::Resolve: return s + kFsResolve;
        case GlFragmentShader::Blit: return s + kFsBlit;
        case GlFragmentShader::EquirectToCube: return s + kFsCubeCommon + kFsEquirectToCube;
        case GlFragmentShader::Prefilter: return s + kFsCubeCommon + kFsPrefilter;
        case GlFragmentShader::BrdfLut: return s + kFsCubeCommon + kFsBrdfLut;
    }
    return s;
}

}  // namespace spindle
