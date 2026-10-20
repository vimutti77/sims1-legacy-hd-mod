#version 450
// The game's wall fragment shader (SPIR-V hash dfa0d8fccf7ee369, decompiled), sampling a
// tenth of a pixel right of and below the pixel centre.
//
// At the middle zoom a texel is 1.5 screen pixels, so every other texel edge falls exactly on
// a pixel centre and which texel a pixel gets depends on rounding, differently for each
// sprite: the stair-stepped edges of a wall and of what meets it (a wall top, the wall of
// the next floor) then miss each other by a pixel here and there. Sampling every world
// sprite at the same offset point decides those ties the same way for all of them.

layout(push_constant, std430) uniform Push {
    mat4 transform;
    vec4 light;
} pc;

layout(set = 1, binding = 0) uniform sampler2D mask;  // the wall's coverage
layout(set = 0, binding = 0) uniform sampler2D wall;

layout(location = 0) in vec3 world_pos;
layout(location = 1) in vec4 ambient;
layout(location = 2) in vec2 uv;
layout(location = 3) in vec2 depth;
layout(location = 4) in vec4 light0;
layout(location = 5) in vec4 light1;
layout(location = 0) out vec4 colour;

const float kShift = 0.1;
// Depth added to the pixels the proxy's smooth-edged masks add outside the game's own coverage:
// their depth is the wall's plane carried past the wall, and against an object standing at
// the wall they won, a dashed line over the object. A little behind, they lose such near ties
// and still draw over what is really behind the wall.
const float kAddedBehind = 0.0005;

void main() {
    vec2 at = uv + (dFdx(uv) + dFdy(uv)) * kShift;
    // the proxy's masks store coverage with a flag (tools/stairs.py mark): 128-255 inside the
    // game's coverage, 0-127 added; the game's own 0/255 masks read the same way as 0 and 1
    float r = floor(texture(mask, at).x * 255.0 + 0.5);
    bool added = r < 128.0;
    float m = (added ? r : r - 128.0) / 127.0;
    if (m <= 0.0500000007450580596923828125) discard;
    vec2 at_wall = at;
    if (added) {
        // a pixel the straight edges add outside the game's coverage: coloured from the nearest
        // covered one, not from what the wall's texture holds there (a fence's wallpaper has its
        // background there: light teeth along the top of a white railing)
        vec2 px = 1.0 / vec2(textureSize(mask, 0));
        float nearest = 1e9;
        for (int dy = -3; dy <= 3; ++dy)
            for (int dx = -3; dx <= 3; ++dx) {
                vec2 q = at + vec2(dx, dy) * px;
                float d = float(dx * dx + dy * dy);
                if (d < nearest && texture(mask, q).x * 255.0 >= 127.5) {
                    nearest = d;
                    at_wall = q;
                }
            }
    }
    vec4 t = texture(wall, at_wall * vec2(textureSize(mask, 0)) / vec2(textureSize(wall, 0)));
    if (t.w <= 0.0) discard;
    vec3 d0 = world_pos - light0.xyz;
    float range = pc.light.y - pc.light.x;
    float f0 = 1.0 - pow(clamp((dot(d0, d0) - pc.light.x) / range, 0.0, 1.0), pc.light.z);
    vec3 d1 = world_pos - light1.xyz;
    float f1 = 1.0 - pow(clamp((dot(d1, d1) - pc.light.x) / range, 0.0, 1.0), pc.light.z);
    colour = (min(ambient + vec4(fma(f0 * f0, light0.w, (f1 * f1) * light1.w)), vec4(1.0)) * t) *
             vec4(1.031372547149658203125, 1.0156862735748291015625, 1.031372547149658203125, 1.0);
    colour *= m;
    gl_FragDepth = depth.x + (added ? kAddedBehind : 0.0);
}
