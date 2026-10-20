#version 450
// The game's object sprite fragment shader (SPIR-V hash b2e55b6e3b04a39c, decompiled),
// sampling a tenth of a pixel right of and below the pixel centre (see world_wall.frag).

layout(set = 0, binding = 0) uniform sampler2D tex;
layout(set = 1, binding = 0) uniform sampler2D zbuf;  // the sprite's depth, 1 = no pixel

layout(location = 0) in vec4 tint;
layout(location = 1) in vec2 uv;
layout(location = 2) in vec2 depth;  // nearest, farthest
layout(location = 3) flat in float rank;  // world_object.vert
layout(location = 0) out vec4 colour;

const float kShift = 0.1;
// Which pixels this pipeline draws (vkproxy.cpp, g_world_partial): 0 all; 1 the opaque ones,
// with the game's depth writes; 2 the partly covered ones, after every opaque one of the pass
// (back to front, writing depth too). Drawn in the game's order instead, an edge pixel of an object in
// front blended with the floor and its depth then kept out the object behind, drawn later: a
// line of light dots along the edge, there for as long as the game kept that order.
layout(constant_id = 0) const int kPass = 0;
const float kOpaque = 0.999;
// Ties at the very same depth, between pieces of an object, go to the one the game draws later,
// also when partly covered pixels are drawn again at the end of the pass (world_object.vert).
const float kRankStep = 0.00000024;  // 4 ulp at depths 0.5-1; the 122 ranks stay under a third of a z step

void main() {
    vec2 at = uv + (dFdx(uv) + dFdy(uv)) * kShift;
    vec4 c = (tint * texture(tex, at)) * vec4(1.031372547149658203125, 1.0156862735748291015625, 1.031372547149658203125, 1.0);
    if (c.w < 0.25) discard;
    if (kPass == 1 && c.w < kOpaque) discard;
    if (kPass == 2 && c.w >= kOpaque) discard;
    float z = texture(zbuf, at).x;
    if (z >= 1.0) discard;
    gl_FragDepth = mix(depth.x, depth.y, z * 1.0039370059967041015625) - rank * kRankStep;
    colour = c;
}
