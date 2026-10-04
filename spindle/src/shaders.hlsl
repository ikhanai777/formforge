// Spindle renderer shaders (HLSL, shader model 5.0 / D3D11 feature level 11_0).
// Compiled at runtime by gpu.cpp; entry points are selected by name.
//
// Conventions
//   * World space is Z-up, millimetres. Matrices are row_major and transform
//     column vectors: mul(M, v).
//   * The HDR scene buffer holds PREMULTIPLIED colour; alpha is coverage.
//   * Accumulation averages tonemapped, display-linear colour; the resolve
//     pass converts to sRGB once at the end.

static const float PI = 3.14159265359;

cbuffer FrameCB : register(b0)
{
    row_major float4x4 gViewProj;
    row_major float4x4 gView;
    row_major float4x4 gProj;
    row_major float4x4 gProjInv;
    row_major float4x4 gInvViewProj;
    row_major float4x4 gLightViewProj;
    row_major float4x4 gWorld;
    row_major float4x4 gMirror;   // identity, or the z = 0 mirror for the reflection pass
    float4 gEye;          // xyz eye position, w = 1 for orthographic
    float4 gForward;      // xyz camera forward
    float4 gScreen;       // width, height, 1/width, 1/height
    float4 gKeyDir;       // xyz towards the key light, w = direct key intensity
    float4 gKeyColor;     // rgb key colour, w = how strongly shadow darkens environment light
    float4 gFillDir;      // xyz, w intensity
    float4 gFillColor;
    float4 gRimDir;       // xyz, w intensity
    float4 gRimColor;
    float4 gEnv;          // x intensity, y rotation (radians), z saturation, w specular mip count - 1
    float4 gSH[9];        // irradiance SH (already divided by pi)
    float4 gBackground;   // x mode, y blur 0..1, z transparent, w environment cube mip count - 1
    float4 gBgColor;      // display-linear
    float4 gBgColor2;
    float4 gGround;       // x mode, y floor roughness, z reflection strength, w shadow strength
    float4 gFloorColor;   // rgb linear albedo, w fade radius
    float4 gAO;           // x strength, y radius (world), z enabled, w noise offset
    float4 gShadow;       // x texel size (uv), y normal offset (world), z PCF radius (texels), w enabled
    float4 gTone;         // x exposure multiplier, y tonemap mode, z 1/sample count, w resolve mode
    float4 gModel;        // xyz bounding sphere centre, w radius
    float4 gPass;         // x reflection pass, y checkerboard in blit, z noise frame, w unused
};

cbuffer MaterialCB : register(b1)
{
    float4 gBase;          // rgb linear albedo, w roughness
    float4 gPbr;           // x metalness, y clearcoat, z clearcoat roughness, w sheen
    float4 gTrans;         // x transmission, y ior, z unused, w unused
    float4 gTransTint;
    float4 gLayer;         // x enabled, y layer height, z depth
    float4 gNoise;         // x strength, y scale, z stretch along x
    float4 gPattern;       // x type, y scale, z strength, w triplanar sharpness
    float4 gPatternColor;  // rgb linear
    float4 gGradient;      // rgb linear, w enabled
    float4 gGradientRange; // x min z, y max z
    float4 gWire;          // rgb, w width in pixels
};

cbuffer BakeCB : register(b2)
{
    float4 gFace;          // x cube face, y roughness, z source lod / resolution, w sample count
};

Texture2D<float> tShadow   : register(t0);
TextureCube      tSpec     : register(t1);
Texture2D<float2> tBrdf    : register(t2);
Texture2D<float> tAO       : register(t3);
Texture2D        tRefl     : register(t4);
Texture2D        tUser     : register(t5);
Texture2D<float> tDepth    : register(t6);
Texture2D        tSrc      : register(t7);   // HDR scene / blit source
Texture2D        tAccum    : register(t8);
Texture2D        tEquirect : register(t9);
TextureCube      tEnv      : register(t10);
Texture2D<float> tAORaw    : register(t11);

SamplerComparisonState sShadow : register(s0);
SamplerState sLinearClamp : register(s1);
SamplerState sLinearWrap  : register(s2);
SamplerState sPointClamp  : register(s3);
SamplerState sEquirect    : register(s4);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

float3 rotZ(float3 d, float a)
{
    float c = cos(a), s = sin(a);
    return float3(c * d.x - s * d.y, s * d.x + c * d.y, d.z);
}

float3 envSpace(float3 d) { return rotZ(d, -gEnv.y); }

float luminance(float3 c) { return dot(c, float3(0.2126, 0.7152, 0.0722)); }

float3 adjustSaturation(float3 c, float s) { return max(0, lerp(luminance(c).xxx, c, s)); }

float2 equirectUV(float3 d)
{
    float u = 0.5 - atan2(d.y, d.x) / (2.0 * PI);
    float v = acos(clamp(d.z, -1.0, 1.0)) / PI;
    return float2(u, v);
}

float3 shIrradiance(float3 n)
{
    float3 r = gSH[0].rgb * 0.282095
             + gSH[1].rgb * (0.488603 * n.y)
             + gSH[2].rgb * (0.488603 * n.z)
             + gSH[3].rgb * (0.488603 * n.x)
             + gSH[4].rgb * (1.092548 * n.x * n.y)
             + gSH[5].rgb * (1.092548 * n.y * n.z)
             + gSH[6].rgb * (0.315392 * (3.0 * n.z * n.z - 1.0))
             + gSH[7].rgb * (1.092548 * n.x * n.z)
             + gSH[8].rgb * (0.546274 * (n.x * n.x - n.y * n.y));
    return max(r, 0);
}

float3 envDiffuse(float3 n) { return adjustSaturation(shIrradiance(envSpace(n)), gEnv.z) * gEnv.x; }

float3 envSpecular(float3 r, float roughness)
{
    float3 c = tSpec.SampleLevel(sLinearClamp, envSpace(r), saturate(roughness) * gEnv.w).rgb;
    return adjustSaturation(c, gEnv.z) * gEnv.x;
}

float interleavedGradientNoise(float2 p)
{
    return frac(52.9829189 * frac(dot(p, float2(0.06711056, 0.00583715))));
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

float3 F_Schlick(float3 f0, float VoH) { return f0 + (1.0 - f0) * pow(1.0 - VoH, 5.0); }

float3 directLight(float3 L, float3 radiance, float3 N, float3 V, float3 diffColor, float3 f0, float roughness)
{
    float NoL = saturate(dot(N, L));
    if (NoL <= 0.0) return 0;
    float a = max(roughness * roughness, 0.01);
    float3 H = normalize(L + V);
    float NoH = saturate(dot(N, H));
    float VoH = saturate(dot(V, H));
    float NoV = max(dot(N, V), 1e-4);
    float3 F = F_Schlick(f0, VoH);
    float3 spec = D_GGX(NoH, a) * V_SmithGGXCorrelated(NoV, NoL, a) * F;
    float3 diff = diffColor / PI * (1.0 - F);
    return (diff + spec) * radiance * NoL;
}

static const float2 kPoisson[12] = {
    float2(-0.326, -0.406), float2(-0.840, -0.074), float2(-0.696, 0.457), float2(-0.203, 0.621),
    float2(0.962, -0.195), float2(0.473, -0.480), float2(0.519, 0.767), float2(0.185, -0.893),
    float2(0.507, 0.064), float2(0.896, 0.412), float2(-0.322, -0.933), float2(-0.792, -0.598)
};

float shadowFactor(float3 P, float3 Ng, float2 pixel)
{
    if (gShadow.w < 0.5) return 1.0;
    float3 Po = P + Ng * gShadow.y;
    float4 lp = mul(gLightViewProj, float4(Po, 1.0));
    float3 ndc = lp.xyz / lp.w;
    float2 uv = float2(ndc.x * 0.5 + 0.5, -ndc.y * 0.5 + 0.5);
    if (any(uv < 0.0) || any(uv > 1.0) || ndc.z > 1.0) return 1.0;
    float z = ndc.z - 0.0004;
    float angle = interleavedGradientNoise(pixel + gPass.z * 7.0) * 2.0 * PI;
    float2 cs = float2(cos(angle), sin(angle));
    float r = gShadow.z * gShadow.x;
    float sum = 0.0;
    [unroll] for (int k = 0; k < 12; ++k)
    {
        float2 o = float2(kPoisson[k].x * cs.x - kPoisson[k].y * cs.y, kPoisson[k].x * cs.y + kPoisson[k].y * cs.x);
        sum += tShadow.SampleCmpLevelZero(sShadow, uv + o * r, z);
    }
    return sum / 12.0;
}

float aoAt(float2 pixel)
{
    if (gAO.z < 0.5) return 1.0;
    return lerp(1.0, tAO.Load(int3(pixel, 0)), gAO.x);
}

// ---- procedural noise -------------------------------------------------------

float hash13(float3 p)
{
    p = frac(p * 0.1031);
    p += dot(p, p.zyx + 31.32);
    return frac((p.x + p.y) * p.z);
}

float vnoise(float3 p)
{
    float3 i = floor(p);
    float3 f = frac(p);
    float3 u = f * f * (3.0 - 2.0 * f);
    float n000 = hash13(i);
    float n100 = hash13(i + float3(1, 0, 0));
    float n010 = hash13(i + float3(0, 1, 0));
    float n110 = hash13(i + float3(1, 1, 0));
    float n001 = hash13(i + float3(0, 0, 1));
    float n101 = hash13(i + float3(1, 0, 1));
    float n011 = hash13(i + float3(0, 1, 1));
    float n111 = hash13(i + float3(1, 1, 1));
    return lerp(lerp(lerp(n000, n100, u.x), lerp(n010, n110, u.x), u.y),
                lerp(lerp(n001, n101, u.x), lerp(n011, n111, u.x), u.y), u.z);
}

float fbm(float3 p)
{
    float s = 0.0, a = 0.5;
    [unroll] for (int k = 0; k < 4; ++k)
    {
        s += a * vnoise(p);
        p = p * 2.03 + float3(17.1, 9.7, 3.3);
        a *= 0.5;
    }
    return s / 0.9375;
}

float3 triplanarWeights(float3 n, float sharpness)
{
    float3 w = pow(abs(n) + 1e-4, sharpness);
    return w / (w.x + w.y + w.z);
}

float3 triplanarTexture(float3 p, float3 dpx, float3 dpy, float3 w)
{
    float3 x = tUser.SampleGrad(sLinearWrap, p.yz, dpx.yz, dpy.yz).rgb;
    float3 y = tUser.SampleGrad(sLinearWrap, p.xz, dpx.xz, dpy.xz).rgb;
    float3 z = tUser.SampleGrad(sLinearWrap, p.xy, dpx.xy, dpy.xy).rgb;
    return x * w.x + y * w.y + z * w.z;
}

// Twill-woven carbon fibre tone (0 = dark gap, 1 = lit tow) in tow units.
float carbon2(float2 uv)
{
    float2 c = floor(uv);
    float2 f = frac(uv);
    bool horizontal = fmod(c.x + c.y + 1024.0, 4.0) < 2.0;
    float across = horizontal ? f.y : f.x;
    float along = horizontal ? f.x : f.y;
    float bundle = sin(across * PI);
    float fibres = 0.5 + 0.5 * sin(across * PI * 22.0);
    float shade = horizontal ? 1.0 : 0.65;
    return saturate(bundle * lerp(0.8, 1.0, fibres) * shade * (0.85 + 0.15 * sin(along * PI)));
}

// ---------------------------------------------------------------------------
// Mesh passes
// ---------------------------------------------------------------------------

struct MeshIn
{
    float3 pos : POSITION;
    float3 nrm : NORMAL;
};

struct MeshV
{
    float4 pos  : SV_Position;
    float3 wpos : WPOS;
    float3 wnrm : WNRM;
    float3 opos : OPOS;
    float3 onrm : ONRM;
};

struct MeshVW
{
    float4 pos  : SV_Position;
    float3 wpos : WPOS;
    float3 wnrm : WNRM;
    float3 opos : OPOS;
    float3 onrm : ONRM;
    float3 bary : BARY;
};

MeshV VS_Mesh(MeshIn i)
{
    MeshV o;
    float4 w = mul(gWorld, float4(i.pos, 1.0));
    o.pos = mul(gViewProj, mul(gMirror, w));
    o.wpos = w.xyz;
    o.wnrm = mul((float3x3)gWorld, i.nrm);
    o.opos = i.pos;
    o.onrm = i.nrm;
    return o;
}

float4 VS_Shadow(MeshIn i) : SV_Position
{
    return mul(gLightViewProj, mul(gWorld, float4(i.pos, 1.0)));
}

[maxvertexcount(3)]
void GS_Wire(triangle MeshV v[3], inout TriangleStream<MeshVW> stream)
{
    [unroll] for (int k = 0; k < 3; ++k)
    {
        MeshVW o;
        o.pos = v[k].pos;
        o.wpos = v[k].wpos;
        o.wnrm = v[k].wnrm;
        o.opos = v[k].opos;
        o.onrm = v[k].onrm;
        o.bary = float3(k == 0, k == 1, k == 2);
        stream.Append(o);
    }
}

#ifdef WIREFRAME
#define MODEL_IN MeshVW
#else
#define MODEL_IN MeshV
#endif

float4 PS_Model(MODEL_IN i) : SV_Target
{
    float3 P = i.wpos;
    float3 V = gEye.w > 0.5 ? -gForward.xyz : normalize(gEye.xyz - P);

    // Orient everything to the visible side using the geometric normal, so
    // inverted or inconsistent winding never renders black.
    float3 Ng = normalize(cross(ddx(P), ddy(P)));
    if (dot(Ng, V) < 0.0) Ng = -Ng;
    float3 No = normalize(i.onrm);
    float3 Nw = normalize(i.wnrm);
    if (dot(Nw, Ng) < 0.0) { Nw = -Nw; No = -No; }

    float3 op = i.opos;
    float3 dpx = ddx(op), dpy = ddy(op);
    float footprint = max(length(dpx), length(dpy));
    float fwz = abs(dpx.z) + abs(dpy.z);

    // ---- albedo and roughness ----
    float3 albedo = gBase.rgb;
    float roughness = gBase.w;
    float metal = gPbr.x;

    if (gGradient.w > 0.5)
    {
        float t = saturate((op.z - gGradientRange.x) / max(1e-4, gGradientRange.y - gGradientRange.x));
        albedo = lerp(albedo, gGradient.rgb, t);
    }

    int pattern = (int)gPattern.x;
    float scale = max(gPattern.y, 1e-3);
    float strength = gPattern.z;
    float3 q = op / scale;
    float3 tw = triplanarWeights(No, gPattern.w);
    if (pattern == 1)  // image, triplanar
    {
        float3 t = triplanarTexture(q, dpx / scale, dpy / scale, tw);
        albedo = lerp(albedo, t, strength);
    }
    else if (pattern == 2)  // wood: rings around the Z axis
    {
        float3 warp = float3(fbm(q * 0.7), fbm(q * 0.7 + 5.2), 0) - 0.5;
        float r = length(q.xy + warp.xy * 1.2);
        float ring = frac(r + fbm(q * float3(3, 3, 0.3)) * 0.25);
        float late = smoothstep(0.55, 0.8, ring) * (1.0 - smoothstep(0.9, 1.0, ring));
        float grain = vnoise(q * float3(40, 40, 1.5));
        float t = saturate(late * 0.85 + grain * 0.2);
        albedo = lerp(albedo, gPatternColor.rgb, t * strength);
    }
    else if (pattern == 3)  // marble
    {
        float v = sin((q.x + q.y * 0.6 + q.z * 0.3) * 2.0 * PI * 1.5 + fbm(q * 3.0) * 7.0);
        float vein = pow(1.0 - abs(v), 8.0);
        float cloud = fbm(q * 9.0);
        float t = saturate(vein * 0.9 + (cloud - 0.5) * 0.15);
        albedo = lerp(albedo, gPatternColor.rgb, t * strength);
    }
    else if (pattern == 4)  // carbon fibre, triplanar
    {
        float t = carbon2(q.yz) * tw.x + carbon2(q.xz) * tw.y + carbon2(q.xy) * tw.z;
        albedo = lerp(gPatternColor.rgb, albedo, lerp(1.0, t, strength));
        roughness = lerp(roughness, roughness * (1.3 - 0.6 * t), strength);
    }
    else if (pattern == 5)  // 3D checker
    {
        float3 c = floor(q + 1e-3);
        float parity = fmod(abs(c.x + c.y + c.z), 2.0);
        albedo = lerp(albedo, gPatternColor.rgb, parity * strength);
    }
    else if (pattern == 6)  // granite
    {
        float n1 = vnoise(q * 4.0), n2 = vnoise(q * 9.0 + 5.0), n3 = vnoise(q * 17.0 + 11.0);
        float dark = smoothstep(0.55, 0.62, n1 * 0.6 + n2 * 0.4);
        float speck = smoothstep(0.80, 0.84, n3);
        albedo = lerp(albedo, gPatternColor.rgb, dark * strength);
        albedo = lerp(albedo, float3(0.85, 0.85, 0.85), speck * 0.6 * strength);
    }

    // ---- surface detail as an object-space height gradient ----
    float3 g = 0;
    if (gLayer.x > 0.5)
    {
        // FDM layers: rounded beads stacked along Z, faded out once a layer is
        // smaller than a pixel (accumulation averages the rest).
        float H = max(gLayer.y, 1e-3);
        float t = frac(op.z / H);
        float fade = saturate(2.0 - 2.0 * fwz / H);
        g.z += gLayer.z * 1.5 * cos(PI * t) * fade;
    }
    if (gNoise.x > 0.0)
    {
        float s = max(gNoise.y, 1e-3);
        float3 stretch = float3(max(gNoise.z, 1e-3), 1, 1);
        float3 nq = op / (s * stretch);
        const float e = 0.05;
        float n0 = fbm(nq);
        float3 d = float3(fbm(nq + float3(e, 0, 0)), fbm(nq + float3(0, e, 0)), fbm(nq + float3(0, 0, e))) - n0;
        float3 grad = d / e / (s * stretch);
        float fade = saturate(2.0 - 2.0 * footprint / s);
        g += grad * gNoise.x * s * 0.6 * fade;
    }
    float3 Nb = normalize(No - (g - No * dot(No, g)));
    float3 N = normalize(mul((float3x3)gWorld, Nb));
    if (dot(N, V) < 0.0) N = normalize(N - V * (dot(N, V) - 1e-3));  // keep bumped normals visible

    // ---- lighting ----
    roughness = clamp(roughness, 0.02, 1.0);
    float NoV = max(dot(N, V), 1e-4);
    float3 R = reflect(-V, N);
    float3 f0 = lerp(float3(0.04, 0.04, 0.04), albedo, metal);
    float3 diffColor = albedo * (1.0 - metal);
    float2 ab = tBrdf.SampleLevel(sLinearClamp, float2(NoV, roughness), 0);
    float3 specWeight = f0 * ab.x + ab.y;

    float ao = aoAt(i.pos.xy);
    float specAO = saturate(pow(NoV + ao, exp2(-16.0 * roughness - 1.0)) - 1.0 + ao);
    float sh = shadowFactor(P, Ng, i.pos.xy);
    // A cast shadow only removes environment light on surfaces that face the
    // key; surfaces turned away are already dim through the SH irradiance.
    float facing = saturate(dot(Ng, gKeyDir.xyz) * 2.0);
    float envVis = 1.0 - gKeyColor.w * (1.0 - sh) * facing;

    float3 diffuse = envDiffuse(N) * diffColor * ao;
    float3 specular = envSpecular(R, roughness) * specWeight * specAO;

    if (gTrans.x > 0.0)
    {
        // Approximate transmission: refract the environment, tint, no inter-reflection.
        float3 T = refract(-V, N, 1.0 / max(gTrans.y, 1.0));
        if (dot(T, T) < 1e-4) T = R;
        float3 through = envSpecular(T, saturate(roughness + 0.15)) * gTransTint.rgb;
        diffuse = lerp(diffuse, through * (1.0 - specWeight), gTrans.x);
    }

    float3 color = (diffuse + specular) * envVis;
    color += directLight(gKeyDir.xyz, gKeyColor.rgb * gKeyDir.w, N, V, diffColor, f0, roughness) * sh;
    color += directLight(gFillDir.xyz, gFillColor.rgb * gFillDir.w, N, V, diffColor, f0, roughness) * ao;
    color += directLight(gRimDir.xyz, gRimColor.rgb * gRimDir.w, N, V, diffColor, f0, roughness);

    if (gPbr.w > 0.0)  // sheen: a soft grazing-angle glow
        color += gPbr.w * pow(1.0 - NoV, 3.0) * envDiffuse(N) * lerp(albedo, 1.0, 0.4) * ao;

    if (gPbr.y > 0.0)  // clearcoat over the un-bumped surface
    {
        float ccr = clamp(gPbr.z, 0.02, 1.0);
        float NoVc = max(dot(Nw, V), 1e-4);
        float2 abc = tBrdf.SampleLevel(sLinearClamp, float2(NoVc, ccr), 0);
        float Fc = (0.04 + 0.96 * pow(1.0 - NoVc, 5.0)) * gPbr.y;
        float3 Rc = reflect(-V, Nw);
        float3 cc = envSpecular(Rc, ccr) * (0.04 * abc.x + abc.y) * gPbr.y * specAO * envVis;
        cc += directLight(gKeyDir.xyz, gKeyColor.rgb * gKeyDir.w, Nw, V, 0, 0.04, ccr) * gPbr.y * sh;
        color = color * (1.0 - Fc) + cc;
    }

#ifdef WIREFRAME
    float3 d3 = fwidth(i.bary);
    float3 edge3 = smoothstep(0.0, d3 * max(gWire.w, 0.5), i.bary);
    float edge = 1.0 - min(edge3.x, min(edge3.y, edge3.z));
    color = lerp(color, gWire.rgb, edge);
#endif
    return float4(max(color, 0), 1.0);
}

// ---------------------------------------------------------------------------
// Ground
// ---------------------------------------------------------------------------

struct GroundV
{
    float4 pos  : SV_Position;
    float3 wpos : WPOS;
};

GroundV VS_Ground(uint id : SV_VertexID)
{
    const float2 corners[6] = { float2(-1, -1), float2(1, -1), float2(1, 1), float2(-1, -1), float2(1, 1), float2(-1, 1) };
    GroundV o;
    float3 p = float3(corners[id] * gFloorColor.w * 1.02, 0.0);
    o.pos = mul(gViewProj, float4(p, 1.0));
    o.wpos = p;
    return o;
}

float4 PS_Ground(GroundV i) : SV_Target
{
    float3 P = i.wpos;
    float3 N = float3(0, 0, 1);
    float fadeR = gFloorColor.w;
    float fade = 1.0 - smoothstep(fadeR * 0.25, fadeR, length(P.xy));
    float sh = shadowFactor(P, N, i.pos.xy);
    float ao = aoAt(i.pos.xy);
    int mode = (int)gGround.x;

    if (mode == 1)  // shadow catcher: only darkens what is behind it
    {
        float lit = (1.0 - (1.0 - sh) * gGround.w) * ao;
        return float4(0, 0, 0, saturate(1.0 - lit) * fade);
    }

    float3 V = gEye.w > 0.5 ? -gForward.xyz : normalize(gEye.xyz - P);
    float NoV = max(V.z, 1e-4);
    float rough = clamp(gGround.y, 0.02, 1.0);
    float envVis = 1.0 - gGround.w * (1.0 - sh);
    float3 albedo = gFloorColor.rgb;
    float2 ab = tBrdf.SampleLevel(sLinearClamp, float2(NoV, rough), 0);
    float3 specW = 0.04 * ab.x + ab.y;
    float3 col = envDiffuse(N) * albedo * ao * envVis + envSpecular(reflect(-V, N), rough) * specW * ao * envVis;
    col += directLight(gKeyDir.xyz, gKeyColor.rgb * gKeyDir.w, N, V, albedo, 0.04, rough) * sh;
    col += directLight(gFillDir.xyz, gFillColor.rgb * gFillDir.w, N, V, albedo, 0.04, rough) * ao;

    if (mode == 3)  // reflective floor: blurred planar reflection, stronger at grazing angles
    {
        float2 uv = i.pos.xy * gScreen.zw;
        float4 r = tRefl.SampleLevel(sLinearClamp, uv, rough * 5.0);
        float k = gGround.z * lerp(0.35, 1.0, pow(1.0 - NoV, 2.0));
        col = col * (1.0 - k * r.a) + r.rgb * k;
    }
    return float4(col * fade, fade);
}

// ---------------------------------------------------------------------------
// Fullscreen passes
// ---------------------------------------------------------------------------

struct FSV
{
    float4 pos : SV_Position;
    float2 uv  : TEXCOORD0;
};

FSV VS_Fullscreen(uint id : SV_VertexID)
{
    FSV o;
    float2 uv = float2((id << 1) & 2, id & 2);
    o.pos = float4(uv * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
    o.uv = uv;
    return o;
}

float3 viewPosition(float2 uv, float depth)
{
    float4 ndc = float4(uv.x * 2.0 - 1.0, 1.0 - uv.y * 2.0, depth, 1.0);
    float4 v = mul(gProjInv, ndc);
    return v.xyz / v.w;
}

float PS_SSAO(FSV i) : SV_Target
{
    int2 px = int2(i.pos.xy);
    float d = tDepth.Load(int3(px, 0));
    if (d >= 1.0) return 1.0;
    float2 uv = (float2(px) + 0.5) * gScreen.zw;
    float3 P = viewPosition(uv, d);

    // Reconstruct the normal from the neighbour with the smaller depth step.
    float3 Pr = viewPosition(uv + float2(gScreen.z, 0), tDepth.Load(int3(px + int2(1, 0), 0)));
    float3 Pl = viewPosition(uv - float2(gScreen.z, 0), tDepth.Load(int3(px - int2(1, 0), 0)));
    float3 Pd = viewPosition(uv + float2(0, gScreen.w), tDepth.Load(int3(px + int2(0, 1), 0)));
    float3 Pu = viewPosition(uv - float2(0, gScreen.w), tDepth.Load(int3(px - int2(0, 1), 0)));
    float3 dx = abs(Pr.z - P.z) < abs(P.z - Pl.z) ? Pr - P : P - Pl;
    float3 dy = abs(Pd.z - P.z) < abs(P.z - Pu.z) ? Pd - P : P - Pu;
    float3 N = normalize(cross(dx, dy));
    float3 toEye = gEye.w > 0.5 ? float3(0, 0, 1) : normalize(-P);
    if (dot(N, toEye) < 0.0) N = -N;

    float angle = interleavedGradientNoise(float2(px) + gAO.w * 5.588238) * 2.0 * PI;
    float3 helper = abs(N.z) < 0.9 ? float3(0, 0, 1) : float3(1, 0, 0);
    float3 T = normalize(cross(helper, N));
    float3 B = cross(N, T);
    float ca = cos(angle), sa = sin(angle);
    float3 T2 = T * ca + B * sa;
    float3 B2 = B * ca - T * sa;

    float R = gAO.y;
    float occlusion = 0.0;
    const int COUNT = 12;
    [unroll] for (int k = 0; k < COUNT; ++k)
    {
        // Cosine-weighted hemisphere directions, denser near the centre.
        float u = (k + 0.5) / COUNT;
        float phi = k * 2.39996323;
        float r = sqrt(u);
        float3 dir = float3(r * cos(phi), r * sin(phi), sqrt(1.0 - u));
        float len = lerp(0.15, 1.0, (k * k) / float(COUNT * COUNT));
        float3 S = P + (T2 * dir.x + B2 * dir.y + N * dir.z) * (R * len);
        float4 c = mul(gProj, float4(S, 1.0));
        float2 suv = float2(c.x / c.w * 0.5 + 0.5, -c.y / c.w * 0.5 + 0.5);
        if (any(suv < 0.0) || any(suv > 1.0)) continue;
        float sd = tDepth.SampleLevel(sPointClamp, suv, 0);
        float3 Sp = viewPosition(suv, sd);
        float range = smoothstep(0.0, 1.0, R / max(abs(P.z - Sp.z), 1e-5));
        occlusion += (Sp.z >= S.z + 0.02 * R ? 1.0 : 0.0) * range;
    }
    return saturate(1.0 - occlusion / COUNT);
}

float PS_AOBlur(FSV i) : SV_Target
{
    int2 px = int2(i.pos.xy);
    float d0 = tDepth.Load(int3(px, 0));
    if (d0 >= 1.0) return 1.0;
    float z0 = viewPosition((float2(px) + 0.5) * gScreen.zw, d0).z;
    float sum = 0.0, wsum = 0.0;
    [unroll] for (int y = -2; y <= 1; ++y)
    [unroll] for (int x = -2; x <= 1; ++x)
    {
        int2 p = px + int2(x, y);
        float d = tDepth.Load(int3(p, 0));
        float z = viewPosition((float2(p) + 0.5) * gScreen.zw, d).z;
        float w = abs(z - z0) < gAO.y * 0.25 ? 1.0 : 0.0;
        sum += tAORaw.Load(int3(p, 0)) * w;
        wsum += w;
    }
    return wsum > 0.0 ? sum / wsum : 1.0;
}

// ---- tonemapping ----------------------------------------------------------

float3 tonemapACES(float3 x)
{
    // Krzysztof Narkowicz's fit of the ACES RRT+ODT.
    x *= 0.6;
    return saturate((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14));
}

float3 agxContrast(float3 x)
{
    float3 x2 = x * x;
    float3 x4 = x2 * x2;
    return 15.5 * x4 * x2 - 40.14 * x4 * x + 31.96 * x4 - 6.868 * x2 * x + 0.4298 * x2 + 0.1191 * x - 0.00232;
}

float3 tonemapAgX(float3 c)
{
    // Minimal AgX (Benjamin Wrensch's fit of Troy Sobotka's AgX).
    const float3x3 inMat = float3x3(0.842479062253094, 0.0423282422610123, 0.0423756549057051,
                                    0.0784335999999992, 0.878468636469772, 0.0784336,
                                    0.0792237451477643, 0.0791661274605434, 0.879142973793104);
    const float3x3 outMat = float3x3(1.19687900512017, -0.0528968517574562, -0.0529716355144438,
                                     -0.0980208811401368, 1.15190312990417, -0.0980434501171241,
                                     -0.0990297440797205, -0.0989611768448433, 1.15107367264116);
    const float minEv = -12.47393, maxEv = 4.026069;
    c = mul(max(c, 1e-10), inMat);
    c = clamp(log2(max(c, 1e-10)), minEv, maxEv);
    c = (c - minEv) / (maxEv - minEv);
    c = agxContrast(c);
    c = mul(c, outMat);
    return pow(saturate(c), 2.2);
}

float3 tonemap(float3 c)
{
    int mode = (int)gTone.y;
    if (mode == 1) return tonemapAgX(c);
    if (mode == 2) return saturate(c);
    return tonemapACES(c);
}

float3 backgroundColor(float2 uv)
{
    int mode = (int)gBackground.x;
    if (mode == 0)
    {
        float4 wp = mul(gInvViewProj, float4(uv.x * 2.0 - 1.0, 1.0 - uv.y * 2.0, 1.0, 1.0));
        float3 dir = gEye.w > 0.5 ? gForward.xyz : normalize(wp.xyz / wp.w - gEye.xyz);
        float3 d = envSpace(dir);
        float3 c = gBackground.y > 0.0
            ? tSpec.SampleLevel(sLinearClamp, d, gBackground.y * gEnv.w).rgb
            : tEquirect.SampleLevel(sEquirect, equirectUV(d), 0).rgb;
        return tonemap(adjustSaturation(c, gEnv.z) * gEnv.x * gTone.x);
    }
    if (mode == 1) return gBgColor.rgb;
    if (mode == 2) return lerp(gBgColor.rgb, gBgColor2.rgb, uv.y);
    if (mode == 3)
    {
        float aspect = gScreen.x * gScreen.w;
        float2 p = (uv - 0.5) * float2(aspect, 1.0);
        float r = length(p) / (0.5 * sqrt(aspect * aspect + 1.0));
        return lerp(gBgColor.rgb, gBgColor2.rgb, smoothstep(0.0, 1.0, r));
    }
    return 0;
}

// Tonemaps one sample and composites it over the display-referred background.
// Output is added into the accumulation buffer.
float4 PS_TonemapAccum(FSV i) : SV_Target
{
    float4 hdr = tSrc.Load(int3(i.pos.xy, 0));
    float a = saturate(hdr.a);
    float3 scene = a > 1e-5 ? tonemap(hdr.rgb / a * gTone.x) * a : 0;
    float3 rgb = scene + backgroundColor(i.uv) * (1.0 - a);
    float alpha = gBackground.z > 0.5 ? a : 1.0;
    return float4(rgb, alpha);
}

float3 linearToSrgb(float3 c)
{
    c = saturate(c);
    return c <= 0.0031308 ? c * 12.92 : 1.055 * pow(c, 1.0 / 2.4) - 0.055;
}

// Average of the accumulated samples -> sRGB 8-bit, straight alpha (mode 0)
// or composited over black (mode 1, for formats without alpha).
float4 PS_Resolve(FSV i) : SV_Target
{
    float4 acc = tAccum.Load(int3(i.pos.xy, 0)) * gTone.z;
    float a = saturate(acc.a);
    float3 rgb = acc.rgb;
    if (gTone.w < 0.5) rgb = a > 1e-4 ? rgb / a : 0;
    else a = 1.0;
    float3 s = linearToSrgb(rgb);
    // Triangular dither of +-1 LSB hides banding in gradients and soft shadows.
    float n = interleavedGradientNoise(i.pos.xy) + interleavedGradientNoise(i.pos.xy + 37.0) - 1.0;
    s += n / 255.0;
    return float4(saturate(s), a);
}

// Viewport display of the resolved image; transparency shows as a checkerboard.
float4 PS_Blit(FSV i) : SV_Target
{
    float4 c = tSrc.SampleLevel(sPointClamp, i.uv, 0);
    if (gPass.y > 0.5)
    {
        float2 cell = floor(i.pos.xy / 10.0);
        float ck = fmod(cell.x + cell.y, 2.0) < 1.0 ? 0.80 : 0.62;
        c.rgb = lerp(ck.xxx, c.rgb, c.a);
    }
    return float4(c.rgb, 1.0);
}

// ---------------------------------------------------------------------------
// Image-based lighting precomputation
// ---------------------------------------------------------------------------

float3 cubeDirection(int face, float2 uv)
{
    float u = uv.x * 2.0 - 1.0;
    float v = uv.y * 2.0 - 1.0;
    float3 d;
    if (face == 0) d = float3(1.0, -v, -u);
    else if (face == 1) d = float3(-1.0, -v, u);
    else if (face == 2) d = float3(u, 1.0, v);
    else if (face == 3) d = float3(u, -1.0, -v);
    else if (face == 4) d = float3(u, -v, 1.0);
    else d = float3(-u, -v, -1.0);
    return normalize(d);
}

float4 PS_EquirectToCube(FSV i) : SV_Target
{
    float3 d = cubeDirection((int)gFace.x, i.uv);
    return float4(tEquirect.SampleLevel(sEquirect, equirectUV(d), gFace.z).rgb, 1.0);
}

float2 hammersley(uint i, uint n)
{
    return float2((i + 0.5) / n, reversebits(i) * 2.3283064365386963e-10);
}

float3 importanceSampleGGX(float2 xi, float a, float3 N, float3 T, float3 B)
{
    float phi = 2.0 * PI * xi.x;
    float cosT = sqrt((1.0 - xi.y) / (1.0 + (a * a - 1.0) * xi.y));
    float sinT = sqrt(1.0 - cosT * cosT);
    return normalize(T * (sinT * cos(phi)) + B * (sinT * sin(phi)) + N * cosT);
}

float4 PS_Prefilter(FSV i) : SV_Target
{
    float3 N = cubeDirection((int)gFace.x, i.uv);
    float roughness = gFace.y;
    if (roughness <= 0.0) return float4(tEnv.SampleLevel(sLinearClamp, N, 0).rgb, 1.0);
    float a = roughness * roughness;
    float3 helper = abs(N.z) < 0.999 ? float3(0, 0, 1) : float3(1, 0, 0);
    float3 T = normalize(cross(helper, N));
    float3 B = cross(N, T);
    const uint COUNT = 128;
    float srcRes = gFace.z;
    float saTexel = 4.0 * PI / (6.0 * srcRes * srcRes);
    float3 sum = 0;
    float wsum = 0;
    for (uint k = 0; k < COUNT; ++k)
    {
        float3 H = importanceSampleGGX(hammersley(k, COUNT), a, N, T, B);
        float NoH = saturate(dot(N, H));
        float3 L = 2.0 * NoH * H - N;
        float NoL = dot(N, L);
        if (NoL > 0.0)
        {
            // Filtered importance sampling: read a blurrier mip where samples are sparse.
            float pdf = D_GGX(NoH, a) * 0.25 + 1e-4;
            float saSample = 1.0 / (COUNT * pdf);
            float lod = max(0.5 * log2(saSample / saTexel) + 1.0, 0.0);
            sum += tEnv.SampleLevel(sLinearClamp, L, lod).rgb * NoL;
            wsum += NoL;
        }
    }
    return float4(sum / max(wsum, 1e-4), 1.0);
}

float2 PS_BrdfLut(FSV i) : SV_Target
{
    float NoV = max(i.uv.x, 1e-3);
    float roughness = max(i.uv.y, 0.02);
    float a = roughness * roughness;
    float3 V = float3(sqrt(1.0 - NoV * NoV), 0.0, NoV);
    float3 N = float3(0, 0, 1), T = float3(1, 0, 0), B = float3(0, 1, 0);
    float A = 0.0, Bs = 0.0;
    const uint COUNT = 256;
    for (uint k = 0; k < COUNT; ++k)
    {
        float3 H = importanceSampleGGX(hammersley(k, COUNT), a, N, T, B);
        float3 L = 2.0 * dot(V, H) * H - V;
        float NoL = saturate(L.z);
        float NoH = saturate(H.z);
        float VoH = saturate(dot(V, H));
        if (NoL > 0.0)
        {
            float vis = V_SmithGGXCorrelated(NoV, NoL, a);
            float gv = 4.0 * vis * NoL * VoH / max(NoH, 1e-4);
            float fc = pow(1.0 - VoH, 5.0);
            A += (1.0 - fc) * gv;
            Bs += fc * gv;
        }
    }
    return float2(A, Bs) / COUNT;
}
