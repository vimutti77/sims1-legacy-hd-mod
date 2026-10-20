#version 450
// The game's object sprite vertex shader (SPIR-V hash f21725c4dec90cbe, decompiled), passing on
// the draw's rank as well: the proxy draws an object sprite with firstInstance = its place
// among the sprites of the same depth band (vertex depths), in the game's order but kept from
// frame to frame (vkproxy.cpp, object_rank), and world_object*.frag bring later ones a hair
// forward. Two pieces of an
// object at the very same depth are then decided as the game's order decides them, also for
// the partly covered pixels drawn again at the end of the pass: a window's jamb stays under
// its sash panel, drawn after it, and the handle drawn after the sash stays on it.

layout(push_constant, std430) uniform Push {
    mat4 transform;
} pc;

layout(location = 0) in vec3 position;
layout(location = 1) in vec4 tint_in;
layout(location = 2) in vec2 uv_in;
layout(location = 3) in vec2 depth_in;  // nearest, farthest

layout(location = 0) out vec4 tint;
layout(location = 1) out vec2 uv;
layout(location = 2) out vec2 depth;
layout(location = 3) flat out float rank;

void main() {
    gl_Position = pc.transform * vec4(position, 1.0);
    tint = tint_in;
    uv = uv_in;
    depth = depth_in;
    rank = float(gl_InstanceIndex);
}
