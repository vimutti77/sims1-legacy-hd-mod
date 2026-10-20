#version 450
// The game's floor / wall-top fragment shader (SPIR-V hash 63b9db14d8318909, decompiled),
// sampling its texture on the exact texel lattice.
//
// At the middle zoom the game draws the near-zoom sprites at half size, but floor tiles and
// wall tops (this pipeline) a little larger than half (a 127x64 tile on 64 x 32.25 pixels,
// not 63.5 x 32): their edges drift by up to a pixel from the walls' and stick out over the
// wall tops (light dots along the line between floors) or leave gaps under the wall tops.
// Here the texel is counted from the quad's uv = 0 corner (its position, which the game
// halves exactly) at the exact size: 2^n or 3 * 2^n screen pixels per texel, the nearest
// to the drawn size. Every world shader samples a tenth of a pixel right of and below the
// pixel centre, so sprites that meet at the near zoom meet here too (see world_wall.frag).

layout(push_constant, std430) uniform Push {
    mat4 transform;
    vec4 light;  // x: near, y: far (squared distances), z: falloff power
} pc;

layout(set = 0, binding = 0) uniform sampler2D tex;

layout(location = 0) in vec3 world_pos;
layout(location = 1) in vec4 ambient;
layout(location = 2) in vec2 uv;
layout(location = 3) in vec2 depth;
layout(location = 4) in vec4 light0;  // xyz position, w strength
layout(location = 5) in vec4 light1;
layout(location = 0) out vec4 colour;

const float kShift = 0.1;
const float kAddedBehind = 0.0005;  // see world_wall.frag
// Floor tiles (odd widths; wall tops are even) from tools/hd_floors.py have their own pixels
// opaque and, outside them, the partly covered pixels of their straightened outline. Which this
// pipeline draws (vkproxy.cpp, g_world_partial): 0 all; 1 the opaque ones, as the game draws
// a tile; 2 the partly covered ones, once every floor tile is drawn (before the walls), without
// depth writes and a hair in front, so that they win the tie with the tile next to them, on
// the same plane, and blend over its stair-stepped teeth.
layout(constant_id = 0) const int kPass = 0;
const float kFringeAhead = 0.00001;

float snap(float r) {
    float a = exp2(round(log2(r)));
    float b = 3.0 * exp2(round(log2(r / 3.0)));
    return abs(log2(a / r)) < abs(log2(b / r)) ? a : b;
}

void main() {
    vec2 dx = dFdx(uv), dy = dFdy(uv);
    vec2 size = vec2(textureSize(tex, 0));
    vec2 per = vec2(abs(dx.x), abs(dy.y));  // uv per screen pixel (the quads are axis aligned)
    vec2 at = uv + (dx + dy) * kShift;
    vec2 drawn = 1.0 / (per * size);  // screen pixels per texel, as drawn
    vec2 exact = vec2(snap(drawn.x), snap(drawn.y));
    vec2 texel = floor(at / per / exact);
    if (any(lessThan(texel, vec2(0.0))) || any(greaterThanEqual(texel, size))) discard;
    vec4 t = texelFetch(tex, ivec2(texel), 0);
    if (t.w <= 0.0) discard;
    float a = floor(t.w * 255.0 + 0.5);
    bool tile = (int(size.x) & 1) == 1;
    bool fringe = tile && a < 255.0;
    if (kPass == 1 && fringe) discard;
    if (kPass == 2 && !fringe) discard;
    vec3 d0 = world_pos - light0.xyz;
    float range = pc.light.y - pc.light.x;
    float f0 = 1.0 - pow(clamp((dot(d0, d0) - pc.light.x) / range, 0.0, 1.0), pc.light.z);
    vec3 d1 = world_pos - light1.xyz;
    float f1 = 1.0 - pow(clamp((dot(d1, d1) - pc.light.x) / range, 0.0, 1.0), pc.light.z);
    colour = (min(ambient + vec4(fma(f0 * f0, light0.w, (f1 * f1) * light1.w)), vec4(1.0)) * t) *
             vec4(1.031372547149658203125, 1.0156862735748291015625, 1.031372547149658203125, 1.0);
    // HD wall tops (tools/hd_walls.py) mark the pixels their straight edges add outside the game's
    // coverage with an even alpha (the game's own are 0 or 255): pushed a little back, as in
    // world_wall.frag, so they lose near ties against objects at the wall
    if (fringe)
        gl_FragDepth = depth.x - kFringeAhead;
    else
        gl_FragDepth = depth.x + (!tile && a < 255.0 && mod(a, 2.0) == 0.0 ? kAddedBehind : 0.0);
}
