#version 450
// Drop-in replacement for the game's textured-quad fragment shader
// (out = vertex colour * texture) that upscales the sampled UI layer with
// xBR level 2. Algorithm after Hyllian's xBR-lv2 shader (MIT licence);
// works at any scale. Texels are mixed as stored (no premultiply), like the
// game's own shader: some layers (neighbourhood lot images) keep colour in
// alpha-0 texels and rely on the game's blend mode.
layout(set = 0, binding = 0) uniform sampler2D tex;
layout(location = 0) in vec4 vColor;
layout(location = 1) in vec2 vUV;
layout(location = 0) out vec4 outColor;

const float XBR_Y_WEIGHT = 48.0;
const float XBR_EQ_THRESHOLD = 15.0;
const float XBR_LV2_COEFFICIENT = 2.0;
const vec3 Y = vec3(0.2126, 0.7152, 0.0722);

const vec4 Ao = vec4(1.0, -1.0, -1.0, 1.0);
const vec4 Bo = vec4(1.0, 1.0, -1.0, -1.0);
const vec4 Co = vec4(1.5, 0.5, -0.5, 0.5);
const vec4 Ax = vec4(1.0, -1.0, -1.0, 1.0);
const vec4 Bx = vec4(0.5, 2.0, -0.5, -2.0);
const vec4 Cx = vec4(1.0, 1.0, -0.5, 0.0);
const vec4 Ay = vec4(1.0, -1.0, -1.0, 1.0);
const vec4 By = vec4(2.0, 0.5, -2.0, -0.5);
const vec4 Cy = vec4(2.0, 0.0, -1.0, 0.5);
const vec4 Ci = vec4(0.25);

vec4 df(vec4 a, vec4 b) { return abs(a - b); }
vec4 diff(vec4 a, vec4 b) { return vec4(notEqual(a, b)); }
vec4 eq(vec4 a, vec4 b) { return step(df(a, b), vec4(XBR_EQ_THRESHOLD)); }
vec4 neq(vec4 a, vec4 b) { return vec4(1.0) - eq(a, b); }
vec4 wd(vec4 a, vec4 b, vec4 c, vec4 d, vec4 e, vec4 f, vec4 g, vec4 h) {
    return df(a, b) + df(a, c) + df(d, e) + df(d, f) + 4.0 * df(g, h);
}

ivec2 g_base;
ivec2 g_max;
// Neighbour texel, clamped to the layer so edges never wrap to the other side.
vec4 px(int x, int y) { return texelFetch(tex, clamp(g_base + ivec2(x, y), ivec2(0), g_max), 0); }
float lum(vec4 p) { return XBR_Y_WEIGHT * (dot(p.rgb, Y) + p.a); }
vec4 lum4(vec4 a, vec4 b, vec4 c, vec4 d) { return vec4(lum(a), lum(b), lum(c), lum(d)); }
float cdf(vec4 a, vec4 b) { return dot(abs(a - b), vec4(1.0)); }

void main() {
    vec2 size = vec2(textureSize(tex, 0));
    vec2 pos = vUV * size;
    // Only upscale; a layer drawn 1:1 (e.g. at -dpi:1) passes through unchanged.
    float scale = max(1.0, 1.0 / max(fwidth(pos.x), 1e-4));
    if (scale < 1.5) {
        outColor = vColor * texture(tex, vUV);
        return;
    }
    vec2 fp = fract(pos);
    g_max = textureSize(tex, 0) - ivec2(1);
    g_base = clamp(ivec2(floor(pos)), ivec2(0), g_max);

    //    A1 B1 C1
    // A0  A  B  C C4
    // D0  D  E  F F4
    // G0  G  H  I I4
    //    G5 H5 I5
    vec4 A1 = px(-1, -2), B1 = px(0, -2), C1 = px(1, -2);
    vec4 A = px(-1, -1), B = px(0, -1), C = px(1, -1);
    vec4 D = px(-1, 0), E = px(0, 0), F = px(1, 0);
    vec4 G = px(-1, 1), H = px(0, 1), I = px(1, 1);
    vec4 G5 = px(-1, 2), H5 = px(0, 2), I5 = px(1, 2);
    vec4 A0 = px(-2, -1), D0 = px(-2, 0), G0 = px(-2, 1);
    vec4 C4 = px(2, -1), F4 = px(2, 0), I4 = px(2, 1);

    vec4 b = lum4(B, D, H, F);
    vec4 c = lum4(C, A, G, I);
    vec4 e = vec4(lum(E));
    vec4 d = b.yzwx;
    vec4 f = b.wxyz;
    vec4 g = c.zwxy;
    vec4 h = b.zwxy;
    vec4 i = c.wxyz;
    vec4 i4 = lum4(I4, C1, A0, G5);
    vec4 i5 = lum4(I5, C4, A1, G0);
    vec4 h5 = lum4(H5, F4, B1, D0);
    vec4 f4 = h5.yzwx;

    // Anti-aliased edge width follows the on-screen scale (2 at -dpi:2).
    vec4 delta = vec4(1.0 / scale);
    vec4 deltaL = vec4(0.5, 1.0, 0.5, 1.0) / scale;
    vec4 deltaU = deltaL.yxwz;

    vec4 fx = Ao * fp.y + Bo * fp.x;
    vec4 fx_l = Ax * fp.y + Bx * fp.x;
    vec4 fx_u = Ay * fp.y + By * fp.x;

    vec4 irlv0 = diff(e, f) * diff(e, h);
    vec4 irlv1 = irlv0 * (neq(f, b) * neq(h, d) + eq(e, i) * neq(f, i4) * neq(h, i5) + eq(e, g) + eq(e, c));
    vec4 irlv2l = diff(e, g) * diff(d, g);
    vec4 irlv2u = diff(e, c) * diff(b, c);

    vec4 fx45i = clamp((fx + delta - Co - Ci) / (2.0 * delta), 0.0, 1.0);
    vec4 fx45 = clamp((fx + delta - Co) / (2.0 * delta), 0.0, 1.0);
    vec4 fx30 = clamp((fx_l + deltaL - Cx) / (2.0 * deltaL), 0.0, 1.0);
    vec4 fx60 = clamp((fx_u + deltaU - Cy) / (2.0 * deltaU), 0.0, 1.0);

    vec4 wd1 = wd(e, c, g, i, h5, f4, h, f);
    vec4 wd2 = wd(h, d, i5, f, i4, b, e, i);

    vec4 edri = step(wd1, wd2) * irlv0;
    vec4 edr = step(wd1 + vec4(0.1), wd2) * step(vec4(0.5), irlv1);
    vec4 edr_l = step(XBR_LV2_COEFFICIENT * df(f, g), df(h, c)) * irlv2l * edr;
    vec4 edr_u = step(XBR_LV2_COEFFICIENT * df(h, c), df(f, g)) * irlv2u * edr;

    fx45 = edr * fx45;
    fx30 = edr_l * fx30;
    fx60 = edr_u * fx60;
    fx45i = edri * fx45i;

    vec4 pxsel = step(df(e, f), df(e, h));
    vec4 maximos = max(max(fx30, fx60), max(fx45, fx45i));

    vec4 res1 = E;
    res1 = mix(res1, mix(H, F, pxsel.x), maximos.x);
    res1 = mix(res1, mix(B, D, pxsel.z), maximos.z);
    vec4 res2 = E;
    res2 = mix(res2, mix(F, B, pxsel.y), maximos.y);
    res2 = mix(res2, mix(D, H, pxsel.w), maximos.w);
    vec4 res = mix(res1, res2, step(cdf(E, res1), cdf(E, res2)));

    outColor = vColor * res;
}
