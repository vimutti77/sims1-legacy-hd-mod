#version 450
// The game's other object sprite fragment shader (SPIR-V hash c616498e50bad3d1, decompiled:
// as world_object.frag, then brightened in YIQ: the object under the cursor), sampling a tenth
// of a pixel right of and below the pixel centre (see world_wall.frag).

layout(set = 0, binding = 0) uniform sampler2D tex;
layout(set = 1, binding = 0) uniform sampler2D zbuf;

layout(location = 0) in vec4 tint;
layout(location = 1) in vec2 uv;
layout(location = 2) in vec2 depth;
layout(location = 3) flat in float rank;  // world_object.vert
layout(location = 0) out vec4 colour;

const float kShift = 0.1;
layout(constant_id = 0) const int kPass = 0;  // see world_object.frag
const float kOpaque = 0.999;
const float kRankStep = 0.00000024;  // see world_object.frag

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
    float y = dot(colour.xyz, vec3(0.2989999949932098388671875, 0.58700001239776611328125, 0.114000000059604644775390625));
    vec3 yiq = vec3(y, dot(colour.xyz, vec3(-0.1459999978542327880859375, -0.28799998760223388671875, 0.4339999854564666748046875)),
                    dot(colour.xyz, vec3(0.61699998378753662109375, -0.517000019550323486328125, -0.100000001490116119384765625)));
    // The game adds the brightening whatever the pixel's coverage; its colours are premultiplied,
    // so a half-transparent edge pixel got a full lift and showed as a light dot: along the
    // stair-stepped edges of HD sprites, a dashed line on the object under the cursor. Scaled
    // by coverage here (the game's greyed-out shader, f0dfef753f754849, does scale its tint).
    yiq.x = y + 0.0900000035762786865234375 * colour.w;
    colour.x = dot(yiq, vec3(1.0, -0.001000000047497451305389404296875, 1.13600003719329833984375));
    colour.y = dot(yiq, vec3(1.0, -0.3959999978542327880859375, -0.578000009059906005859375));
    colour.z = dot(yiq, vec3(1.0, 2.040999889373779296875, -0.00200000009499490261077880859375));
}
