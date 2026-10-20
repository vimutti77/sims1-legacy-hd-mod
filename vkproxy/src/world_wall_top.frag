#version 450
// The game's fragment shader for wall tops with the walls cut away or down (SPIR-V hash
// ccd02c43ced1b15e, decompiled). A second texture (set 1, R8_UINT, the size of the game's
// texture) gives each texel one of eight depths passed in from the vertices; 0 where the
// wall top has no pixel, which the game never reads (it discards empty pixels first).
//
// The pixels that the HD wall tops' straight edges add outside the game's coverage
// (tools/hd_walls.py: an even alpha below 255) read that 0 and got the depth of level 7,
// in front of an object standing at the wall: dotted lines along both edges of the wall top,
// over a clock by the wall. Here they take the farthest level of the covered texels around
// them, a little behind (as world_floor.frag pushes the walls-up wall tops' added pixels).

layout(set = 0, binding = 0) uniform sampler2D tex;
layout(set = 1, binding = 0) uniform usampler2D level_map;

layout(location = 0) in vec4 tint;
layout(location = 1) in vec2 uv;
layout(location = 2) in vec4 depth_a;  // levels 0-3
layout(location = 3) in vec4 depth_b;  // levels 4-7
layout(location = 0) out vec4 colour;

const float kAddedBehind = 0.0005;

// the game's 256-entry table: which of the eight depths a texel's value picks, 255 = none
uint level_of(uint v) {
    v %= 256u;
    if (v == 255u) return 255u;
    if (v < 63u) return 7u;
    if (v < 89u) return 6u;
    if (v < 114u) return 5u;
    if (v < 140u) return 4u;
    if (v < 165u) return 3u;
    if (v < 191u) return 2u;
    if (v < 216u) return 1u;
    return 0u;
}

float depth_at(uint level) {
    float d[8] = float[](depth_a.x, depth_a.y, depth_a.z, depth_a.w, depth_b.x, depth_b.y, depth_b.z, depth_b.w);
    return d[level];
}

bool added_alpha(float w) {
    float a = floor(w * 255.0 + 0.5);
    return a < 255.0 && mod(a, 2.0) == 0.0;
}

void main() {
    vec4 t = texture(tex, uv);
    colour = (tint * t) * vec4(1.031372547149658203125, 1.0156862735748291015625, 1.031372547149658203125, 1.0);
    if (colour.w <= 0.0) discard;
    if (!added_alpha(t.w)) {  // the game's own pixels, as the game draws them
        uint level = level_of(texture(level_map, uv).x);
        if (level >= 255u) discard;
        gl_FragDepth = depth_at(level);
        return;
    }
    ivec2 size = textureSize(level_map, 0);
    ivec2 at = clamp(ivec2(floor(uv * vec2(size))), ivec2(0), size - 1);
    float farthest = -1.0;
    for (int dy = -1; dy <= 1; ++dy)
        for (int dx = -1; dx <= 1; ++dx) {
            ivec2 q = at + ivec2(dx, dy);
            if (any(lessThan(q, ivec2(0))) || any(greaterThanEqual(q, size))) continue;
            float w = texture(tex, (vec2(q) + 0.5) / vec2(size)).w;  // the HD texel at its centre
            if (w <= 0.0 || added_alpha(w)) continue;  // not one of the game's pixels
            uint level = level_of(texelFetch(level_map, q, 0).x);
            if (level < 255u) farthest = max(farthest, depth_at(level));
        }
    if (farthest < 0.0) discard;
    gl_FragDepth = farthest + kAddedBehind;
}
