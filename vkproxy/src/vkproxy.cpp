// vkproxy: drop-in vulkan-1.dll for The Sims Legacy (32-bit).
//
// Sims.exe loads Vulkan with LoadLibraryA("vulkan-1.dll") and resolves everything
// through vkGetInstanceProcAddr, so a DLL with that name next to the exe is loaded
// instead of the system loader. This DLL forwards every call to the real loader
// and observes rendering: it logs resource creation, dumps SPIR-V shaders and,
// when F11 is pressed, logs every render pass and draw of the next frame.
//
// The game draws each UI window into its own 1x render target and composites
// it onto the frame, scaled by its UI scale, with its textured-quad pipeline
// and a nearest sampler. By default this DLL:
// - creates those render targets UI-scale times larger and scales every pass
//   into them, so UI is drawn at screen resolution (step 1b; readbacks such as
//   lot thumbnails are shrunk back to 1x). VKPROXY_LAYER_SCALE=N forces N, 1 = off.
// - replaces a sampled texture of w x h with <vkproxy\replace>\<w>x<h>@<scale>.bgra
//   when that file exists (the HD font atlases); VKPROXY_REPLACE=0 disables this.
// - draws every textured quad that is magnified (icons, the remaining 1x
//   bitmaps) with xbr.frag instead of nearest: for each pipeline using the
//   game's quad fragment shader we create a twin using xbr.frag. F10 toggles
//   this at runtime; VKPROXY_XBR=0 starts with it off, VKPROXY_XBR_ALL=0 limits
//   it to layer composites (option ข).
// VKPROXY_DUMP=1 saves every texture upload to <VKPROXY_DIR>\dump.
//
// Output goes to %VKPROXY_DIR% (default %TEMP%\vkproxy). Replacement files are
// read from %VKPROXY_DIR%\replace if VKPROXY_DIR is set, else from
// <folder of this DLL>\vkproxy\replace.
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>
#include <windows.h>

#include <cpuid.h>
#include <immintrin.h>

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

const uint32_t kXbrFrag[] =
#include "xbr_frag.inc"
    ;
// The game's textured-quad fragment shader: out = vertex colour * texture(binding 0).
const char* const kQuadFragShader = "shader_466f913aec289dea.spv";

// The in-lot sprite shaders, replaced by copies that sample on the exact texel lattice with
// one tie-break for all sprites (src/world_*.frag): at the middle zoom the game's own leave
// light dots along the line between floors and along wall tops.
const uint32_t kWorldFloorFrag[] =
#include "world_floor_frag.inc"
    ;
const uint32_t kWorldWallFrag[] =
#include "world_wall_frag.inc"
    ;
const uint32_t kWorldObjectFrag[] =
#include "world_object_frag.inc"
    ;
const uint32_t kWorldObjectYiqFrag[] =
#include "world_object_yiq_frag.inc"
    ;
const uint32_t kWorldWallTopFrag[] =
#include "world_wall_top_frag.inc"
    ;
struct WorldShader {
    const char* game;  // the game's fragment shader it replaces
    const uint32_t* code;
    size_t size;
    bool split;  // drawn in two parts: opaque pixels, then the partly covered ones (g_world_partial)
};
const int kWorldFloor = 0;  // kWorldShaders index of the floor tiles' shader
const WorldShader kWorldShaders[] = {
    {"shader_63b9db14d8318909.spv", kWorldFloorFrag, sizeof kWorldFloorFrag, true},          // floors, wall tops
    {"shader_dfa0d8fccf7ee369.spv", kWorldWallFrag, sizeof kWorldWallFrag, false},           // walls
    {"shader_b2e55b6e3b04a39c.spv", kWorldObjectFrag, sizeof kWorldObjectFrag, true},        // objects
    {"shader_c616498e50bad3d1.spv", kWorldObjectYiqFrag, sizeof kWorldObjectYiqFrag, true},  // objects, brightened
    {"shader_ccd02c43ced1b15e.spv", kWorldWallTopFrag, sizeof kWorldWallTopFrag, false},     // wall tops, walls down
};

std::recursive_mutex g_mu;
FILE* g_log = nullptr;
std::string g_dir;
HMODULE g_real = nullptr;
PFN_vkGetInstanceProcAddr real_GIPA = nullptr;
PFN_vkGetDeviceProcAddr real_GDPA = nullptr;
PFN_vkCreateInstance real_CreateInstance = nullptr;
PFN_vkCreateDevice real_CreateDevice = nullptr;
unsigned g_frame = 0;
bool g_trace = false;  // log draws until the next present
unsigned g_trace_left = 0;  // frames still to trace (F11: kTraceFrames)
const unsigned kTraceFrames = 90;  // ~1.5 s: time to press F11 and then do what is to be traced
bool g_xbr_on = true;
bool g_hd_on = true;  // F9 / hd.toggle: draw with HD twins (content replacement)
bool g_world_on = true;  // F6 / world.toggle, VKPROXY_WORLDFIX=0: the world shader twins (kWorldShaders)
unsigned g_xbr_draws = 0;  // substituted draws in the current frame
// Layer enlargement (step 1b): render targets smaller than the swapchain are
// created g_layer_scale times larger, and passes into them are scaled to match,
// so UI windows are rendered at screen resolution instead of 1x. By default the
// scale is the game's own UI scale (see auto_layer_scale), so a layer ends up
// composited 1:1; VKPROXY_LAYER_SCALE=N forces N (1 = off).
int g_layer_scale = 1;
bool g_layer_scale_auto = true;
int g_dpi_arg = 0;  // -dpi:N (or -dpi_N) on the game's command line, 0 if absent
uint32_t g_swap_w = 0, g_swap_h = 0;
bool g_dump = false;  // VKPROXY_DUMP=1: save texture uploads (see dump_upload)
bool g_replace_on = true;  // VKPROXY_REPLACE=0 disables texture replacement
bool g_xbr_all = true;     // VKPROXY_XBR_ALL=0: xBR only on layer composites (see xbr_twin_for)
// World draw log (VKPROXY_WORLDLOG=1): every texture upload is named by its content
// hash, and F8 / world.now writes the next kWorldFrames frames' draws with their textures, push
// constants and vertices to <VKPROXY_DIR>\world.log (how the game assembles sprites).
bool g_names_on = false;
bool g_world_now = false;
unsigned g_world_left = 0;          // frames still to log
const unsigned kWorldFrames = 60;   // F8 logs this many frames (~1 s): time to make a passing effect happen
// Texture replacement (step 2): a sampled B8G8R8A8 image of w x h is created
// g_layer_scale times larger when <g_replace_dir>\<w>x<h>@<scale>.bgra exists,
// and every upload into it is replaced by that file's pixels.
std::string g_replace_dir;  // %VKPROXY_DIR%\replace if set, else <dll folder>\vkproxy\replace
VkInstance g_instance = VK_NULL_HANDLE;
VkPhysicalDevice g_phys = VK_NULL_HANDLE;
VkDevice g_device = VK_NULL_HANDLE;
struct Replacement {
    std::string path;
    uint32_t w, h;      // scaled size
    uint32_t bpp = 4;   // bytes per texel
};
// Host-visible copy of a replacement file, shared by every image it replaces
// (the game recreates its font atlases often).
struct Staging {
    VkBuffer buffer = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;  // none for a range of the ring (twin_staging)
    VkDeviceSize offset = 0;
};
VkShaderModule g_xbr_module = VK_NULL_HANDLE;
// Time the proxy spends on texture uploads, per frame: logged ("perf:") for every frame
// that uploads textures or takes longer than kSlowFrameMs, to find what makes turning
// and zooming the camera slow.
struct Perf {
    double copy = 0;    // all of vkCmdCopyBufferToImage, the parts below included
    double hash = 0;    // content names of uploads
    double alloc = 0;   // staging buffers: create, allocate, map
    double file = 0;    // reading replacement files
    double image = 0;   // HD twin images: create, allocate, view
    double submit = 0;  // vkQueueSubmit
    unsigned uploads = 0, hashed = 0, loaded = 0, reused = 0;
    double mb = 0;      // replacement bytes read
};
Perf g_perf;
double g_ms_per_tick = 0;
double g_last_present_ms = 0;
unsigned g_perf_lines = 0;
const double kSlowFrameMs = 40;
double now_ms() {
    LARGE_INTEGER t;
    QueryPerformanceCounter(&t);
    return static_cast<double>(t.QuadPart) * g_ms_per_tick;
}
// Benchmark (F7 or a bench.now file): the next kBenchFrames frames' times and the proxy's
// share of them, summarised in the log and appended to <VKPROXY_DIR>\bench.log, to compare
// proxy versions and settings (F9 HD, F10 xBR) on the same scene.
const unsigned kBenchFrames = 600;
bool g_bench = false;
struct Bench {
    std::vector<double> frames;  // ms between presents
    double draws_ms = 0, uploads_ms = 0, submit_ms = 0;
    unsigned draws = 0, hd_draws = 0, xbr_draws = 0;
};
Bench g_bench_acc;

void logf(const char* fmt, ...) {
    if (!g_log) return;
    SYSTEMTIME t;
    GetLocalTime(&t);
    fprintf(g_log, "%02d:%02d:%02d.%03d ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
    fflush(g_log);
}

// N from "-dpi:N" or "-dpi_N" in the command line (the game's own syntax), else 0.
int dpi_argument(const char* cmd) {
    for (const char* p = cmd; (p = strchr(p, '-')) != nullptr; ++p)
        if (!_strnicmp(p + 1, "dpi", 3) && (p[4] == ':' || p[4] == '_') && p[5] >= '0' && p[5] <= '9')
            return atoi(p + 5);
    return 0;
}

// Folder this DLL was loaded from (the game folder), without a trailing slash.
std::string dll_folder() {
    HMODULE self = nullptr;
    char path[MAX_PATH];
    if (!GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            reinterpret_cast<LPCSTR>(&dll_folder), &self) ||
        !GetModuleFileNameA(self, path, MAX_PATH))
        return ".";
    std::string s = path;
    return s.substr(0, s.find_last_of("\\/"));
}

void init() {
    static std::once_flag once;
    std::call_once(once, [] {
        char buf[MAX_PATH];
        DWORD n = GetEnvironmentVariableA("VKPROXY_DIR", buf, MAX_PATH);
        bool dir_set = n > 0 && n < MAX_PATH;
        if (!dir_set) {
            GetTempPathA(MAX_PATH, buf);
            strcat(buf, "vkproxy");
        }
        g_dir = buf;
        g_replace_dir = dir_set ? g_dir + "\\replace" : dll_folder() + "\\vkproxy\\replace";
        LARGE_INTEGER freq;
        QueryPerformanceFrequency(&freq);
        g_ms_per_tick = 1000.0 / static_cast<double>(freq.QuadPart);
        CreateDirectoryA(g_dir.c_str(), nullptr);
        g_log = fopen((g_dir + "\\vkproxy.log").c_str(), "w");
        if (GetEnvironmentVariableA("VKPROXY_XBR", buf, MAX_PATH) && buf[0] == '0') g_xbr_on = false;
        if (GetEnvironmentVariableA("VKPROXY_WORLDFIX", buf, MAX_PATH) && buf[0] == '0') g_world_on = false;
        if (GetEnvironmentVariableA("VKPROXY_LAYER_SCALE", buf, MAX_PATH)) {
            g_layer_scale = std::min(std::max(atoi(buf), 1), 4);
            g_layer_scale_auto = false;
        }
        g_dpi_arg = dpi_argument(GetCommandLineA());
        if (GetEnvironmentVariableA("VKPROXY_DUMP", buf, MAX_PATH) && buf[0] == '1') g_dump = true;
        if (GetEnvironmentVariableA("VKPROXY_REPLACE", buf, MAX_PATH) && buf[0] == '0') g_replace_on = false;
        if (GetEnvironmentVariableA("VKPROXY_XBR_ALL", buf, MAX_PATH) && buf[0] == '0') g_xbr_all = false;
        if (GetEnvironmentVariableA("VKPROXY_WORLDLOG", buf, MAX_PATH) && buf[0] == '1') g_names_on = true;
        logf("settings: xbr=%d xbr_all=%d layer_scale=%s replace=%d (%s) dump=%d worldlog=%d -dpi=%d", g_xbr_on,
             g_xbr_all, g_layer_scale_auto ? "auto" : std::to_string(g_layer_scale).c_str(), g_replace_on,
             g_replace_dir.c_str(), g_dump, g_names_on, g_dpi_arg);
        GetSystemDirectoryA(buf, MAX_PATH);  // SysWOW64 for a 32-bit process
        strcat(buf, "\\vulkan-1.dll");
        g_real = LoadLibraryA(buf);
        if (g_real)
            real_GIPA = reinterpret_cast<PFN_vkGetInstanceProcAddr>(
                reinterpret_cast<void*>(GetProcAddress(g_real, "vkGetInstanceProcAddr")));
        logf("vkproxy loaded, real loader %s -> %p", buf, static_cast<void*>(g_real));
    });
}

// ---- tracked state ---------------------------------------------------------

struct ImageInfo {
    unsigned id;
    uint32_t w, h;  // as the game asked for
    VkFormat format;
    VkImageUsageFlags usage;
    int scale = 1;  // actual extent is w*scale x h*scale
};
struct FbInfo {
    uint32_t w, h;
    std::vector<uint64_t> views;
    int scale = 1;
};
struct Binding {
    uint32_t binding;
    uint64_t view;
    uint64_t sampler;
};
// One vkCmdBindDescriptorSets / vkCmdPushConstants as the game issued it (to issue again).
struct SetCall {
    uint64_t layout = 0;
    uint32_t first = 0;
    std::vector<uint64_t> sets;
    std::vector<uint32_t> dyn;
    unsigned seq = 0;
};
struct PushRec {
    uint64_t layout = 0;
    VkShaderStageFlags stages = 0;
    uint32_t offset = 0, size = 0;
    unsigned char bytes[128] = {};
    unsigned seq = 0;
};
struct DrawArgs {
    bool indexed = false;
    uint32_t count = 0, instances = 0, first = 0;
    int32_t vertex_offset = 0;
    uint32_t first_instance = 0;
};
// An object draw's partly covered pixels, drawn again at the end of the pass (see g_world_partial).
struct Deferred {
    uint64_t game_pipeline = 0;              // the game's (for its vertex layout)
    float key = 0;                           // objects: the farthest depth of its vertices
    VkPipeline pipeline = VK_NULL_HANDLE;
    VkPipelineLayout layout = VK_NULL_HANDLE;  // the pipeline's: sets and push constants go with it
    uint32_t sets = 0;                         // how many of set[] its shader uses
    VkDescriptorSet set[2] = {};
    std::vector<PushRec> pushes;
    std::vector<std::pair<uint64_t, VkDeviceSize>> vbs;
    uint64_t ib = 0;
    VkDeviceSize ib_off = 0;
    VkIndexType ib_type = VK_INDEX_TYPE_UINT16;
    DrawArgs args;
    std::vector<VkViewport> viewports;
    std::vector<VkRect2D> scissors;
};
struct CmdState {
    uint64_t pipeline = 0;
    uint64_t bound = 0;  // what is really bound: the game's pipeline or a twin of it
    std::vector<uint64_t> sets;
    uint32_t rp_w = 0, rp_h = 0;
    std::vector<uint64_t> targets;  // views being rendered to
    int scale = 1;                  // >1 while inside a pass into enlarged targets
    VkRect2D area{};                // render area of the current pass (as issued)
    std::vector<VkViewport> viewports;  // as last set by the game (unscaled)
    std::vector<VkRect2D> scissors;
    std::vector<uint64_t> set_layouts;  // pipeline layout the game last bound each set with
    // for the world draw log
    std::vector<std::pair<uint64_t, VkDeviceSize>> vbs;  // vertex buffer per binding
    uint64_t ib = 0;
    VkDeviceSize ib_off = 0;
    VkIndexType ib_type = VK_INDEX_TYPE_UINT16;
    unsigned char pc[128] = {};  // push constant bytes as last pushed
    uint32_t pc_size = 0;
    // to draw again at the end of a render pass and then restore the game's state
    bool in_pass = false;  // inside vkCmdBeginRenderPass .. vkCmdEndRenderPass
    unsigned seq = 0;
    std::vector<std::shared_ptr<SetCall>> set_calls;  // per set index: the call that last bound it
    std::vector<PushRec> pushes;                      // the last push for each set of stages
    std::vector<Deferred> deferred;        // objects': drawn at the end of the pass
    std::vector<Deferred> deferred_floor;  // floor tiles': before the next wall or object draw
    std::vector<Deferred> sorted;          // the objects' as sorted, to check their keys at submit
};

std::unordered_map<uint64_t, ImageInfo> g_images;           // VkImage
std::unordered_map<uint64_t, uint64_t> g_view_image;        // VkImageView -> VkImage
std::unordered_map<uint64_t, FbInfo> g_fbs;                 // VkFramebuffer
std::unordered_map<uint64_t, std::string> g_samplers;       // VkSampler -> "mag/min"
std::unordered_map<uint64_t, std::vector<Binding>> g_sets;  // VkDescriptorSet
std::unordered_map<uint64_t, std::string> g_modules;        // VkShaderModule -> spv file
std::unordered_map<uint64_t, unsigned> g_pipelines;         // VkPipeline -> id
std::unordered_map<uint64_t, VkPipeline> g_xbr_twin;        // game pipeline -> xBR twin
std::unordered_map<uint64_t, VkPipeline> g_world_twin;      // game pipeline -> twin with a world shader
// Object sprites have partly covered pixels along their edges (more in HD than in the game's
// own art), blended over what is already drawn, and they write depth. The game draws objects
// in an order that can change from frame to frame and sometimes draws a piece in front before
// the one behind it: the front piece's edge then blends with the floor, and its depth keeps the
// piece behind from filling in (light dots along the edge, for as long as that order lasts).
// So a split object pipeline's twin draws only the opaque pixels, and every such draw is
// repeated at the end of its render pass with this pipeline for the partly covered ones (back
// to front, see flush_partial), which then blend over everything opaque behind them, whatever
// the game's order. They still write depth:
// without it a chair behind a table, its legs' edges repeated after the table's, showed through
// the table's edge and the flowers on it.
std::unordered_map<uint64_t, VkPipeline> g_world_partial;  // game pipeline -> partly covered pixels' twin
// Floor tiles likewise (tools/hd_floors.py: the partly covered pixels of their straightened
// outline lie outside their own), but repeated once the floor tiles are drawn, before the
// first wall or object: drawn later, they would win ties with the bottom rows of walls.
std::unordered_map<uint64_t, int> g_world_kind;  // game pipeline -> its kWorldShaders index
std::unordered_map<uint64_t, uint64_t> g_world_layout;  // game pipeline (with a world twin) -> its layout
VkShaderModule g_world_modules[std::size(kWorldShaders)] = {};
std::unordered_map<VkCommandBuffer, CmdState> g_cmd;
std::unordered_map<uint64_t, Replacement> g_replace;        // VkImage -> replacement pixels
std::unordered_map<std::string, Staging> g_staging;         // replacement file -> staging buffer
std::set<uint64_t> g_freed_replaced;                         // destroyed replaced images (to log reuse)
// 1x images that readbacks of enlarged images are shrunk into, one per size and
// format (readbacks are rare: lot thumbnails when saving).
struct Readback {
    VkImage image = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;
};
std::map<std::tuple<uint32_t, uint32_t, VkFormat>, Readback> g_readback;
// Content replacement (HD art for icons and object pictures): when the pixels
// uploaded into a sampled w x h texture hash to H and <replace>\<w>x<h>_<H>@<s>.bgra
// exists, the proxy keeps an s-times larger twin image with those pixels, and
// draws that sample the texture bind a descriptor set pointing at the twin.
// The game's own image is left alone; textures without a file keep xBR.
struct HdImage {
    // kWall: an HD wallpaper over the game's upload (see g_wall_masks); kWallMask: a wall's
    // coverage mask (R8, set 1 of the wall pipeline) magnified, which a wall twin needs.
    enum Kind : unsigned char { kFile, kWall, kWallMask };
    VkImage image = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;  // its own; none when in a block (see g_blocks)
    VkImageView view = VK_NULL_HANDLE;
    std::string name;
    VkDeviceSize bytes = 0;
    Kind kind = kFile;
    int block = -1;           // the block holding it, or -1
    VkDeviceSize offset = 0;  // its range there
};
std::unordered_map<uint64_t, HdImage> g_hd;               // game VkImage -> the HD twin it shows
// HD twins by content name. The game destroys a camera view's sprites when the camera
// turns or zooms and uploads them again when it comes back, so a twin no image shows any
// more is kept for reuse (no file read, no allocation), until the idle ones go over
// budget: then the longest idle go first.
struct Twin {
    HdImage hd;
    unsigned users = 0;  // game images showing it
    unsigned seq = 0;    // bumped on every acquire and release: tells stale queue entries
};
std::unordered_map<std::string, Twin> g_twins;
std::deque<std::tuple<std::string, unsigned, unsigned>> g_released;  // (name, seq, frame) of recent releases
std::deque<std::pair<std::string, unsigned>> g_idle;                 // (name, seq): idle, out of flight, oldest first
VkDeviceSize g_idle_bytes = 0;
VkDeviceSize g_twin_budget = 512ull << 20;  // set from the GPU's memory in hk_vkCreateDevice
unsigned g_idle_count = 0, g_idle_max = 2048;
std::set<std::string> g_hd_names;                          // "<w>x<h>_<hash>" with a file
// Packs (tools/hd_pack.py): the content replacements in a few big files, opened while the
// game loads, so a file is one read at an offset instead of an open (~5 ms the first time:
// the antivirus scans it). Loose files win over packed ones.
struct Packed {
    HANDLE file;
    uint64_t offset;
    uint32_t size;
};
std::unordered_map<std::string, Packed> g_packed;  // "<name>@<scale>.<ext>" -> where
std::vector<HANDLE> g_pack_files;
std::set<std::pair<uint32_t, uint32_t>> g_hd_sizes;        // sizes worth hashing
std::set<std::pair<uint32_t, uint32_t>> g_z_sizes;         // the same for depth textures (R8)
// Walls: the game uploads an in-lot wall as its wallpaper frame with the empty corners filled
// (the wall top in one colour, the bottom row repeated down), so only the pixels the frame
// covers identify the wallpaper. <replace>\<w>x<h>.wallmasks holds the coverage masks of the
// wallpaper frames of that size (tools/hd_walls.py), most used first; the HD wallpaper is
// <w>x<h>_<hash of the covered pixels>_w<mask>@<scale>.bgra, drawn over the upload's corners.
// A mask is kept as its runs of covered pixels (first pixel, count), in row order.
using WallMask = std::vector<std::pair<uint32_t, uint32_t>>;
std::map<std::pair<uint32_t, uint32_t>, std::vector<WallMask>> g_wall_masks;
std::unordered_set<std::string> g_no_wallpaper;  // wall uploads no mask finds a file for (fences, ...)
bool g_hd_scanned = false;
std::unordered_map<uint64_t, bool> g_dsl_single;           // VkDescriptorSetLayout -> only binding 0, one sampler
std::unordered_map<uint64_t, uint64_t> g_set_layout;       // VkDescriptorSet -> its layout
std::map<std::tuple<uint64_t, uint64_t, uint64_t>, VkDescriptorSet> g_hd_sets;  // (view, sampler, layout) -> set
VkDescriptorPool g_hd_pool = VK_NULL_HANDLE;
// Upload dumping (VKPROXY_DUMP=1): where each buffer's bytes are mapped, so the
// pixels of a texture upload can be read when the copy is recorded.
struct BufMem {
    uint64_t memory;
    VkDeviceSize offset;
};
struct MemMap {
    char* ptr;
    VkDeviceSize offset, size;
};
std::unordered_map<uint64_t, BufMem> g_buf_mem;  // VkBuffer -> memory binding
std::unordered_map<uint64_t, MemMap> g_mem_map;  // VkDeviceMemory -> host mapping
std::set<std::string> g_dumped;
std::set<std::string> g_fetched;
// World draw log state (see g_names_on).
std::unordered_map<uint64_t, std::string> g_image_name;  // VkImage -> content name of its last upload
struct PipeInfo {
    std::string vs, fs;
    std::vector<VkVertexInputBindingDescription> bindings;
    std::vector<VkVertexInputAttributeDescription> attrs;
};
std::unordered_map<uint64_t, PipeInfo> g_pipe_info;  // VkPipeline
struct DrawRec {
    uint64_t pipeline;
    std::vector<std::tuple<size_t, uint32_t, uint64_t>> tex;  // (set, binding, VkImage)
    std::vector<std::pair<uint64_t, VkDeviceSize>> vbs;
    bool indexed;
    uint64_t ib;
    VkDeviceSize ib_off;
    VkIndexType ib_type;
    uint32_t first, count;
    int32_t vertex_offset;
    std::vector<unsigned char> pc;
    uint32_t rp_w, rp_h;
};
std::unordered_map<VkCommandBuffer, std::vector<DrawRec>> g_draw_recs;  // recorded this frame, written at submit
unsigned g_world_seq = 0;
unsigned g_next_image = 1, g_next_pipeline = 1;

std::string image_desc(uint64_t view) {
    auto v = g_view_image.find(view);
    if (v == g_view_image.end()) return "view?";
    auto i = g_images.find(v->second);
    if (i == g_images.end()) return "img?";
    char b[96];
    snprintf(b, sizeof b, "img#%u(%ux%u fmt%d use%x)", i->second.id, i->second.w, i->second.h,
             static_cast<int>(i->second.format), static_cast<unsigned>(i->second.usage));
    return b;
}

std::string img_desc(uint64_t image) {
    auto i = g_images.find(image);
    if (i == g_images.end()) return "img?";
    char b[64];
    snprintf(b, sizeof b, "img#%u(%ux%u)", i->second.id, i->second.w, i->second.h);
    return b;
}

const char* filter_name(VkFilter f) { return f == VK_FILTER_NEAREST ? "nearest" : "linear"; }

// ---- device-level hooks ----------------------------------------------------

#define DEVICE_HOOKS(X)                                                                        \
    X(vkCreateImage)                                                                           \
    X(vkDestroyImage)                                                                          \
    X(vkDestroyDevice)                                                                         \
    X(vkCreateImageView)                                                                       \
    X(vkCreateFramebuffer)                                                                     \
    X(vkCreateSampler)                                                                         \
    X(vkCreateShaderModule)                                                                    \
    X(vkCreateGraphicsPipelines)                                                               \
    X(vkUpdateDescriptorSets)                                                                  \
    X(vkCreateDescriptorSetLayout)                                                             \
    X(vkAllocateDescriptorSets)                                                                \
    X(vkCmdBeginRenderPass)                                                                    \
    X(vkCmdBeginRendering)                                                                     \
    X(vkCmdBeginRenderingKHR)                                                                  \
    X(vkCmdBindPipeline)                                                                       \
    X(vkCmdBindDescriptorSets)                                                                 \
    X(vkCmdDraw)                                                                               \
    X(vkCmdDrawIndexed)                                                                        \
    X(vkCmdBindVertexBuffers)                                                                  \
    X(vkCmdBindIndexBuffer)                                                                    \
    X(vkCmdSetViewport)                                                                        \
    X(vkCmdSetScissor)                                                                         \
    X(vkCmdPushConstants)                                                                      \
    X(vkCmdClearAttachments)                                                                   \
    X(vkCmdCopyImage)                                                                          \
    X(vkCmdBlitImage)                                                                          \
    X(vkCmdCopyImageToBuffer)                                                                  \
    X(vkCmdCopyBufferToImage)                                                                  \
    X(vkBeginCommandBuffer)                                                                    \
    X(vkCmdNextSubpass)                                                                        \
    X(vkCmdEndRenderPass)                                                                      \
    X(vkQueueSubmit)                                                                           \
    X(vkBindBufferMemory)                                                                      \
    X(vkMapMemory)                                                                             \
    X(vkUnmapMemory)                                                                           \
    X(vkCreateSwapchainKHR)                                                                    \
    X(vkQueuePresentKHR)

#define DECL_REAL(fn) PFN_##fn real_##fn = nullptr;
DEVICE_HOOKS(DECL_REAL)
#undef DECL_REAL

bool should_enlarge(const VkImageCreateInfo* ci) {
    const VkImageUsageFlags attach = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
    return g_layer_scale > 1 && g_swap_w && (ci->usage & attach) && ci->imageType == VK_IMAGE_TYPE_2D &&
           ci->extent.width < g_swap_w && ci->extent.height < g_swap_h;
}

// Path of a replacement for a sampled texture of this size, or "" if none.
std::string replacement_for(const VkImageCreateInfo* ci) {
    const VkImageUsageFlags attach = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
    if (!g_replace_on || g_layer_scale < 2 || (ci->usage & attach) || !(ci->usage & VK_IMAGE_USAGE_SAMPLED_BIT) ||
        !(ci->usage & VK_IMAGE_USAGE_TRANSFER_DST_BIT) || ci->format != VK_FORMAT_B8G8R8A8_UNORM ||
        ci->mipLevels != 1 || ci->arrayLayers != 1)
        return "";
    char name[64];
    snprintf(name, sizeof name, "\\%ux%u@%d.bgra", ci->extent.width, ci->extent.height, g_layer_scale);
    std::string path = g_replace_dir + name;
    WIN32_FILE_ATTRIBUTE_DATA fa;
    if (!GetFileAttributesExA(path.c_str(), GetFileExInfoStandard, &fa)) return "";
    uint64_t want = 4ull * ci->extent.width * ci->extent.height * g_layer_scale * g_layer_scale;
    if (fa.nFileSizeLow != want || fa.nFileSizeHigh) {
        logf("replace: %s has wrong size", path.c_str());
        return "";
    }
    return path;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateImage(VkDevice d, const VkImageCreateInfo* ci,
                                                 const VkAllocationCallbacks* a, VkImage* out) {
    int scale = 1;
    VkImageCreateInfo big;
    std::string repl;
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        if (should_enlarge(ci)) scale = g_layer_scale;
        repl = replacement_for(ci);
        if (!repl.empty()) scale = g_layer_scale;
    }
    if (scale > 1) {
        big = *ci;
        big.extent.width *= scale;
        big.extent.height *= scale;
    }
    VkResult r = real_vkCreateImage(d, scale > 1 ? &big : ci, a, out);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        ImageInfo info{g_next_image++, ci->extent.width, ci->extent.height, ci->format, ci->usage, scale};
        g_images[(uint64_t)*out] = info;
        if (!repl.empty()) {
            g_replace[(uint64_t)*out] = Replacement{repl, ci->extent.width * scale, ci->extent.height * scale};
            logf("createImage img#%u %ux%u sampled -> replaced by %s", info.id, info.w, info.h, repl.c_str());
        } else {
            g_replace.erase((uint64_t)*out);  // a reused handle must not inherit a replacement
        }
        if (g_freed_replaced.erase((uint64_t)*out)) {
            static unsigned logged = 0;
            if (logged++ < 20)
                logf("note: img#%u %ux%u usage=%x reuses the handle of a destroyed replaced image", info.id, info.w,
                     info.h, static_cast<unsigned>(info.usage));
        }
        if (ci->usage & (VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT))
            logf("createImage img#%u %ux%u fmt%d usage=%x (%s attachment)%s", info.id, info.w, info.h,
                 static_cast<int>(info.format), static_cast<unsigned>(info.usage),
                 (ci->usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT) ? "color" : "depth",
                 scale > 1 ? " ENLARGED" : "");
    }
    return r;
}

void free_hd(HdImage& hd);
void release_twin(const std::string& name);

// Drivers reuse handle values once an object is destroyed (the validation layer
// hides this by wrapping handles). A stale replacement entry made the next image
// with the same handle receive a full-size atlas copy past its end: GPU fault.
VKAPI_ATTR void VKAPI_CALL hk_vkDestroyImage(VkDevice d, VkImage image, const VkAllocationCallbacks* a) {
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        g_images.erase((uint64_t)image);
        g_image_name.erase((uint64_t)image);
        if (g_replace.erase((uint64_t)image)) g_freed_replaced.insert((uint64_t)image);
        auto hd = g_hd.find((uint64_t)image);
        if (hd != g_hd.end()) {  // kept for reuse (see Twin)
            release_twin(hd->second.name);
            g_hd.erase(hd);
        }
    }
    real_vkDestroyImage(d, image, a);
}

int image_scale(uint64_t image) {
    auto i = g_images.find(image);
    return i == g_images.end() ? 1 : i->second.scale;
}

int view_scale(uint64_t view) {
    auto v = g_view_image.find(view);
    return v == g_view_image.end() ? 1 : image_scale(v->second);
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateImageView(VkDevice d, const VkImageViewCreateInfo* ci,
                                                     const VkAllocationCallbacks* a, VkImageView* out) {
    VkResult r = real_vkCreateImageView(d, ci, a, out);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        g_view_image[(uint64_t)*out] = (uint64_t)ci->image;
    }
    return r;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateFramebuffer(VkDevice d, const VkFramebufferCreateInfo* ci,
                                                       const VkAllocationCallbacks* a, VkFramebuffer* out) {
    int scale = 1;
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        for (uint32_t i = 0; i < ci->attachmentCount; ++i)
            if (view_scale((uint64_t)ci->pAttachments[i]) > 1) scale = view_scale((uint64_t)ci->pAttachments[i]);
        // A scaled framebuffer must still fit inside every attachment that was not
        // enlarged (e.g. a shared full-screen depth buffer is fine); otherwise it is
        // invalid and could fault the GPU.
        bool fits = true;
        for (uint32_t i = 0; scale > 1 && i < ci->attachmentCount; ++i) {
            auto v = g_view_image.find((uint64_t)ci->pAttachments[i]);
            auto im = v == g_view_image.end() ? g_images.end() : g_images.find(v->second);
            if (im == g_images.end()) {
                fits = false;
                continue;
            }
            uint32_t aw = im->second.w * im->second.scale, ah = im->second.h * im->second.scale;
            if (aw < ci->width * scale || ah < ci->height * scale) fits = false;
        }
        if (scale > 1 && !fits) {
            std::string atts;
            for (uint32_t i = 0; i < ci->attachmentCount; ++i)
                atts += " " + image_desc((uint64_t)ci->pAttachments[i]) + "x" +
                        std::to_string(view_scale((uint64_t)ci->pAttachments[i]));
            logf("WARNING framebuffer %ux%u: scaled size would exceed an attachment:%s (not scaled)", ci->width,
                 ci->height, atts.c_str());
            scale = 1;
        }
    }
    VkFramebufferCreateInfo big = *ci;
    big.width *= scale;
    big.height *= scale;
    VkResult r = real_vkCreateFramebuffer(d, &big, a, out);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        FbInfo fb{ci->width, ci->height, {}, scale};
        for (uint32_t i = 0; i < ci->attachmentCount; ++i) fb.views.push_back((uint64_t)ci->pAttachments[i]);
        g_fbs[(uint64_t)*out] = fb;
    }
    return r;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateSampler(VkDevice d, const VkSamplerCreateInfo* ci,
                                                   const VkAllocationCallbacks* a, VkSampler* out) {
    VkResult r = real_vkCreateSampler(d, ci, a, out);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        std::string s = std::string(filter_name(ci->magFilter)) + "/" + filter_name(ci->minFilter);
        g_samplers[(uint64_t)*out] = s;
        logf("createSampler %s address=%d/%d unnormalized=%u", s.c_str(), static_cast<int>(ci->addressModeU),
             static_cast<int>(ci->addressModeV), ci->unnormalizedCoordinates);
    }
    return r;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateShaderModule(VkDevice d, const VkShaderModuleCreateInfo* ci,
                                                        const VkAllocationCallbacks* a, VkShaderModule* out) {
    VkResult r = real_vkCreateShaderModule(d, ci, a, out);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        uint64_t h = 1469598103934665603ull;  // FNV-1a with a non-standard basis: see content_name
        auto p = reinterpret_cast<const unsigned char*>(ci->pCode);
        for (size_t i = 0; i < ci->codeSize; ++i) h = (h ^ p[i]) * 1099511628211ull;
        char name[64];
        snprintf(name, sizeof name, "shader_%016llx.spv", static_cast<unsigned long long>(h));
        std::string path = g_dir + "\\" + name;
        if (FILE* f = fopen(path.c_str(), "wb")) {
            fwrite(ci->pCode, 1, ci->codeSize, f);
            fclose(f);
        }
        g_modules[(uint64_t)*out] = name;
        logf("createShaderModule %s (%u bytes)", name, static_cast<unsigned>(ci->codeSize));
    }
    return r;
}

// A shader module of ours, made once (*module); false if it cannot be.
bool own_module(VkDevice d, const uint32_t* code, size_t size, VkShaderModule* module) {
    if (*module) return true;
    VkShaderModuleCreateInfo mi{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
    mi.codeSize = size;
    mi.pCode = code;
    if (real_vkCreateShaderModule(d, &mi, nullptr, module) == VK_SUCCESS) return true;
    *module = VK_NULL_HANDLE;
    return false;
}

// Create a copy of a game pipeline whose fragment stage is `fs` instead (specialised with
// `spec`), optionally without depth writes.
VkPipeline make_twin(VkDevice d, VkPipelineCache c, const VkGraphicsPipelineCreateInfo& src, VkShaderModule fs,
                     const VkSpecializationInfo* spec = nullptr, bool no_depth_write = false) {
    std::vector<VkPipelineShaderStageCreateInfo> stages(src.pStages, src.pStages + src.stageCount);
    for (auto& s : stages)
        if (s.stage == VK_SHADER_STAGE_FRAGMENT_BIT) {
            s.module = fs;
            s.pName = "main";
            s.pSpecializationInfo = spec;
        }
    VkGraphicsPipelineCreateInfo ci = src;
    ci.pStages = stages.data();
    VkPipelineDepthStencilStateCreateInfo ds{};
    if (no_depth_write) {
        if (!src.pDepthStencilState) return VK_NULL_HANDLE;
        ds = *src.pDepthStencilState;
        ds.depthWriteEnable = VK_FALSE;
        ci.pDepthStencilState = &ds;
    }
    ci.flags &= ~VK_PIPELINE_CREATE_DERIVATIVE_BIT;
    ci.basePipelineHandle = VK_NULL_HANDLE;
    ci.basePipelineIndex = -1;
    VkPipeline twin = VK_NULL_HANDLE;
    VkResult r = real_vkCreateGraphicsPipelines(d, c, 1, &ci, nullptr, &twin);
    return r == VK_SUCCESS ? twin : VK_NULL_HANDLE;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateGraphicsPipelines(VkDevice d, VkPipelineCache c, uint32_t n,
                                                             const VkGraphicsPipelineCreateInfo* ci,
                                                             const VkAllocationCallbacks* a, VkPipeline* out) {
    VkResult r = real_vkCreateGraphicsPipelines(d, c, n, ci, a, out);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        for (uint32_t i = 0; i < n; ++i) {
            unsigned id = g_next_pipeline++;
            g_pipelines[(uint64_t)out[i]] = id;
            std::string stages;
            bool quad_fs = false;
            int world = -1;  // index into kWorldShaders
            for (uint32_t s = 0; s < ci[i].stageCount; ++s) {
                auto m = g_modules.find((uint64_t)ci[i].pStages[s].module);
                bool fs = ci[i].pStages[s].stage == VK_SHADER_STAGE_FRAGMENT_BIT;
                stages += fs ? " fs=" : " vs=";
                stages += m != g_modules.end() ? m->second : "?";
                if (fs && m != g_modules.end() && m->second == kQuadFragShader) quad_fs = true;
                for (int w = 0; fs && m != g_modules.end() && w < static_cast<int>(std::size(kWorldShaders)); ++w)
                    if (m->second == kWorldShaders[w].game) world = w;
            }
            const char* blend = "?";
            if (ci[i].pColorBlendState && ci[i].pColorBlendState->attachmentCount)
                blend = ci[i].pColorBlendState->pAttachments[0].blendEnable ? "blend" : "opaque";
            logf("createGraphicsPipeline pipe#%u%s layout=%llx renderPass=%llx %s", id, stages.c_str(),
                 (unsigned long long)ci[i].layout, (unsigned long long)ci[i].renderPass, blend);
            PipeInfo pi;
            for (uint32_t s = 0; s < ci[i].stageCount; ++s) {
                auto m = g_modules.find((uint64_t)ci[i].pStages[s].module);
                (ci[i].pStages[s].stage == VK_SHADER_STAGE_FRAGMENT_BIT ? pi.fs : pi.vs) =
                    m != g_modules.end() ? m->second : "?";
            }
            if (const VkPipelineVertexInputStateCreateInfo* vi = ci[i].pVertexInputState) {
                pi.bindings.assign(vi->pVertexBindingDescriptions,
                                   vi->pVertexBindingDescriptions + vi->vertexBindingDescriptionCount);
                pi.attrs.assign(vi->pVertexAttributeDescriptions,
                                vi->pVertexAttributeDescriptions + vi->vertexAttributeDescriptionCount);
            }
            g_pipe_info[(uint64_t)out[i]] = std::move(pi);
            if (quad_fs) {
                VkPipeline twin = own_module(d, kXbrFrag, sizeof kXbrFrag, &g_xbr_module)
                                      ? make_twin(d, c, ci[i], g_xbr_module)
                                      : VK_NULL_HANDLE;
                if (twin) g_xbr_twin[(uint64_t)out[i]] = twin;
                logf("  xbr twin for pipe#%u: %s", id, twin ? "ok" : "FAILED");
            }
            if (world >= 0) {
                const WorldShader& ws = kWorldShaders[world];
                // kPass (constant_id 0 in world_object.frag): 0 all pixels, 1 opaque, 2 the rest
                static const int32_t kPasses[3] = {0, 1, 2};
                VkSpecializationMapEntry entry{0, 0, sizeof(int32_t)};
                VkSpecializationInfo spec[3];
                for (int k = 0; k < 3; ++k) spec[k] = {1, &entry, sizeof(int32_t), &kPasses[k]};
                VkPipeline twin = VK_NULL_HANDLE, partial = VK_NULL_HANDLE;
                if (own_module(d, ws.code, ws.size, &g_world_modules[world])) {
                    // floor tiles' partly covered pixels lie on the next tile (same depth): no depth writes,
                    // or the walls drawn next would lose ties along their bottom row; objects' do write,
                    // so that among the repeated draws the pieces in front still hide those behind
                    if (ws.split)
                        partial = make_twin(d, c, ci[i], g_world_modules[world], &spec[2], world == kWorldFloor);
                    twin = make_twin(d, c, ci[i], g_world_modules[world], ws.split ? &spec[partial ? 1 : 0] : nullptr);
                    if (partial && !twin) {
                        auto destroy = reinterpret_cast<PFN_vkDestroyPipeline>(real_GDPA(d, "vkDestroyPipeline"));
                        if (destroy) destroy(d, partial, nullptr);
                        partial = VK_NULL_HANDLE;
                    }
                }
                if (twin) g_world_twin[(uint64_t)out[i]] = twin;
                if (partial) g_world_partial[(uint64_t)out[i]] = partial;
                g_world_kind[(uint64_t)out[i]] = world;
                g_world_layout[(uint64_t)out[i]] = (uint64_t)ci[i].layout;
                logf("  world twin for pipe#%u (%s): %s%s", id, ws.game, twin ? "ok" : "FAILED",
                     ws.split ? (partial ? ", split" : ", NOT split") : "");
            }
        }
    }
    return r;
}

VKAPI_ATTR void VKAPI_CALL hk_vkUpdateDescriptorSets(VkDevice d, uint32_t nw, const VkWriteDescriptorSet* w,
                                                      uint32_t nc, const VkCopyDescriptorSet* c) {
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        for (uint32_t i = 0; i < nw; ++i) {
            if (w[i].descriptorType != VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER &&
                w[i].descriptorType != VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE)
                continue;
            auto& v = g_sets[(uint64_t)w[i].dstSet];
            for (uint32_t k = 0; k < w[i].descriptorCount; ++k) {
                Binding b{w[i].dstBinding + k, (uint64_t)w[i].pImageInfo[k].imageView,
                          (uint64_t)w[i].pImageInfo[k].sampler};
                bool replaced = false;
                for (auto& e : v)
                    if (e.binding == b.binding) e = b, replaced = true;
                if (!replaced) v.push_back(b);
            }
        }
        if (nc) logf("updateDescriptorSets: %u copies (not tracked)", nc);
    }
    real_vkUpdateDescriptorSets(d, nw, w, nc, c);
}

void begin_pass(VkCommandBuffer cb, uint32_t w, uint32_t h, std::vector<uint64_t> targets, const char* how) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    CmdState& st = g_cmd[cb];
    st.rp_w = w;
    st.rp_h = h;
    st.targets = std::move(targets);
    if (g_trace) {
        std::string t;
        for (auto v : st.targets) t += " " + image_desc(v);
        logf("  [%s] %ux%u ->%s", how, w, h, t.c_str());
    }
}

VkViewport scaled(VkViewport v, int s) {
    v.x *= s;
    v.y *= s;
    v.width *= s;
    v.height *= s;
    return v;
}

VkRect2D scaled(VkRect2D r, int s) {
    r.offset.x *= s;
    r.offset.y *= s;
    r.extent.width *= s;
    r.extent.height *= s;
    return r;
}

// Re-issue the game's last viewports/scissors at scale s (1 restores them).
void apply_dynamic_state(VkCommandBuffer cb, const CmdState& st, int s) {
    if (!st.viewports.empty()) {
        std::vector<VkViewport> v;
        for (auto& x : st.viewports) v.push_back(scaled(x, s));
        real_vkCmdSetViewport(cb, 0, static_cast<uint32_t>(v.size()), v.data());
    }
    if (!st.scissors.empty()) {
        std::vector<VkRect2D> r;
        for (auto& x : st.scissors) r.push_back(scaled(x, s));
        real_vkCmdSetScissor(cb, 0, static_cast<uint32_t>(r.size()), r.data());
    }
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdBeginRenderPass(VkCommandBuffer cb, const VkRenderPassBeginInfo* bi,
                                                    VkSubpassContents sc) {
    std::vector<uint64_t> targets;
    int scale = 1;
    uint32_t fb_w = 0, fb_h = 0;
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        auto fb = g_fbs.find((uint64_t)bi->framebuffer);
        if (fb != g_fbs.end()) {
            targets = fb->second.views;
            scale = fb->second.scale;
            fb_w = fb->second.w * scale;
            fb_h = fb->second.h * scale;
        }
    }
    begin_pass(cb, bi->renderArea.extent.width, bi->renderArea.extent.height, targets,
               scale > 1 ? "renderPass ENLARGED" : "renderPass");
    VkRenderPassBeginInfo big = *bi;
    big.renderArea = scaled(bi->renderArea, scale);
    if (scale > 1) {  // keep the scaled render area inside the scaled framebuffer
        VkRect2D& r = big.renderArea;
        uint32_t ox = static_cast<uint32_t>(std::max(r.offset.x, 0)), oy = static_cast<uint32_t>(std::max(r.offset.y, 0));
        if (ox + r.extent.width > fb_w || oy + r.extent.height > fb_h) {
            static unsigned warned = 0;
            if (warned++ < 20)
                logf("WARNING renderArea (%d,%d %ux%u) exceeds enlarged framebuffer %ux%u; clamped", r.offset.x,
                     r.offset.y, r.extent.width, r.extent.height, fb_w, fb_h);
            r.extent.width = ox < fb_w ? fb_w - ox : 0;
            r.extent.height = oy < fb_h ? fb_h - oy : 0;
        }
    }
    real_vkCmdBeginRenderPass(cb, &big, sc);
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    CmdState& st = g_cmd[cb];
    st.scale = scale;
    st.area = big.renderArea;
    st.in_pass = true;
    st.deferred.clear();
    st.deferred_floor.clear();
    if (scale > 1) apply_dynamic_state(cb, st, scale);
}

void begin_rendering(VkCommandBuffer cb, const VkRenderingInfo* ri) {
    std::vector<uint64_t> targets;
    for (uint32_t i = 0; i < ri->colorAttachmentCount; ++i)
        targets.push_back((uint64_t)ri->pColorAttachments[i].imageView);
    begin_pass(cb, ri->renderArea.extent.width, ri->renderArea.extent.height, targets, "rendering");
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdBeginRendering(VkCommandBuffer cb, const VkRenderingInfo* ri) {
    begin_rendering(cb, ri);
    real_vkCmdBeginRendering(cb, ri);
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdBeginRenderingKHR(VkCommandBuffer cb, const VkRenderingInfo* ri) {
    begin_rendering(cb, ri);
    real_vkCmdBeginRenderingKHR(cb, ri);
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdBindPipeline(VkCommandBuffer cb, VkPipelineBindPoint bp, VkPipeline p) {
    VkPipeline bound = p;
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        if (bp == VK_PIPELINE_BIND_POINT_GRAPHICS) {
            g_cmd[cb].pipeline = (uint64_t)p;  // the game's: what the proxy looks things up by
            auto twin = g_world_on ? g_world_twin.find((uint64_t)p) : g_world_twin.end();
            if (twin != g_world_twin.end()) bound = twin->second;
            g_cmd[cb].bound = (uint64_t)bound;
        }
    }
    real_vkCmdBindPipeline(cb, bp, bound);
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdBindDescriptorSets(VkCommandBuffer cb, VkPipelineBindPoint bp,
                                                       VkPipelineLayout layout, uint32_t first, uint32_t n,
                                                       const VkDescriptorSet* sets, uint32_t ndyn,
                                                       const uint32_t* dyn) {
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        auto& v = g_cmd[cb].sets;
        if (v.size() < first + n) v.resize(first + n);
        for (uint32_t i = 0; i < n; ++i) v[first + i] = (uint64_t)sets[i];
        if (bp == VK_PIPELINE_BIND_POINT_GRAPHICS) {
            auto& l = g_cmd[cb].set_layouts;
            if (l.size() < first + n) l.resize(first + n);
            for (uint32_t i = 0; i < n; ++i) l[first + i] = (uint64_t)layout;
            CmdState& st = g_cmd[cb];
            auto call = std::make_shared<SetCall>();
            call->layout = (uint64_t)layout;
            call->first = first;
            call->sets.assign(sets, sets + n);
            call->dyn.assign(dyn, dyn + ndyn);
            call->seq = ++st.seq;
            if (st.set_calls.size() < first + n) st.set_calls.resize(first + n);
            for (uint32_t i = 0; i < n; ++i) st.set_calls[first + i] = call;
        }
    }
    real_vkCmdBindDescriptorSets(cb, bp, layout, first, n, sets, ndyn, dyn);
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateDescriptorSetLayout(VkDevice d, const VkDescriptorSetLayoutCreateInfo* ci,
                                                              const VkAllocationCallbacks* a,
                                                              VkDescriptorSetLayout* out) {
    VkResult r = real_vkCreateDescriptorSetLayout(d, ci, a, out);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        const VkDescriptorSetLayoutBinding* b = ci->pBindings;
        g_dsl_single[(uint64_t)*out] = ci->bindingCount == 1 && b[0].binding == 0 && b[0].descriptorCount == 1 &&
                                       b[0].descriptorType == VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    }
    return r;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkAllocateDescriptorSets(VkDevice d, const VkDescriptorSetAllocateInfo* ai,
                                                           VkDescriptorSet* out) {
    VkResult r = real_vkAllocateDescriptorSets(d, ai, out);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        for (uint32_t i = 0; i < ai->descriptorSetCount; ++i)
            g_set_layout[(uint64_t)out[i]] = (uint64_t)ai->pSetLayouts[i];
    }
    return r;
}

PFN_vkVoidFunction device_fn(const char* name);

// For a draw that samples textures with HD twins: descriptor sets like the bound ones
// but pointing at the twins, plus what to rebind afterwards. Set 0 holds the texture
// (a sprite, an icon); in the world's object pipeline set 1 holds the sprite's depth
// texture, which has a twin when the sprite's HD coverage grows past the original's.
struct HdBind {
    static const size_t kSets = 2;
    VkDescriptorSet ours[kSets] = {}, original[kSets] = {};
    VkPipelineLayout layout[kSets] = {};
    bool any() const { return ours[0] || ours[1]; }
};

// Our copy of the draw's set s when that set holds only one texture and it has a twin
// (whose kind goes to *kind).
VkDescriptorSet hd_set_for(const CmdState& st, size_t s, HdImage::Kind* kind) {
    if (s >= st.sets.size() || s >= st.set_layouts.size() || !st.set_layouts[s]) return VK_NULL_HANDLE;
    auto set = g_sets.find(st.sets[s]);
    auto dsl = g_set_layout.find(st.sets[s]);
    if (set == g_sets.end() || dsl == g_set_layout.end() || !g_dsl_single[dsl->second]) return VK_NULL_HANDLE;
    const Binding* b0 = nullptr;
    for (auto& b : set->second)
        if (b.binding == 0) b0 = &b;
    if (!b0) return VK_NULL_HANDLE;
    auto v = g_view_image.find(b0->view);
    auto hd = v == g_view_image.end() ? g_hd.end() : g_hd.find(v->second);
    if (hd == g_hd.end()) return VK_NULL_HANDLE;
    *kind = hd->second.kind;
    auto key = std::make_tuple((uint64_t)hd->second.view, b0->sampler, dsl->second);
    auto cached = g_hd_sets.find(key);
    if (cached != g_hd_sets.end()) return cached->second;
    if (!g_hd_pool) {
        auto create_pool = reinterpret_cast<PFN_vkCreateDescriptorPool>(device_fn("vkCreateDescriptorPool"));
        VkDescriptorPoolSize ps{VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 4096};
        VkDescriptorPoolCreateInfo pi{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
        pi.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
        pi.maxSets = 4096;
        pi.poolSizeCount = 1;
        pi.pPoolSizes = &ps;
        if (!create_pool || create_pool(g_device, &pi, nullptr, &g_hd_pool) != VK_SUCCESS) {
            g_hd_pool = VK_NULL_HANDLE;
            return VK_NULL_HANDLE;
        }
    }
    VkDescriptorSet ours = VK_NULL_HANDLE;
    VkDescriptorSetLayout layout = (VkDescriptorSetLayout)dsl->second;
    VkDescriptorSetAllocateInfo ai{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    ai.descriptorPool = g_hd_pool;
    ai.descriptorSetCount = 1;
    ai.pSetLayouts = &layout;
    if (real_vkAllocateDescriptorSets(g_device, &ai, &ours) != VK_SUCCESS) return VK_NULL_HANDLE;
    VkDescriptorImageInfo ii{(VkSampler)b0->sampler, hd->second.view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
    VkWriteDescriptorSet w{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
    w.dstSet = ours;
    w.descriptorCount = 1;
    w.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    w.pImageInfo = &ii;
    real_vkUpdateDescriptorSets(g_device, 1, &w, 0, nullptr);  // the real one: not tracked as a game set
    g_hd_sets[key] = ours;
    return ours;
}

HdBind hd_bind_for(VkCommandBuffer cb) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    HdBind hb;
    if (g_hd.empty() || !g_hd_on) return hb;
    CmdState& st = g_cmd[cb];
    HdImage::Kind kind[HdBind::kSets] = {};
    for (size_t s = 0; s < HdBind::kSets; ++s) hb.ours[s] = hd_set_for(st, s, &kind[s]);
    // The wall shader samples the wall (set 0) at uv * mask size / wall size (the mask in
    // set 1, sampled at uv), so a wall twin goes only with its mask's twin and vice versa.
    bool wall = hb.ours[0] && kind[0] == HdImage::kWall, mask = hb.ours[1] && kind[1] == HdImage::kWallMask;
    if (wall != mask) hb.ours[wall ? 0 : 1] = VK_NULL_HANDLE;
    for (size_t s = 0; s < HdBind::kSets; ++s)
        if (hb.ours[s]) {
            hb.original[s] = (VkDescriptorSet)st.sets[s];
            hb.layout[s] = (VkPipelineLayout)st.set_layouts[s];
        }
    return hb;
}

void trace_draw(VkCommandBuffer cb, const char* kind, uint32_t count) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (!g_trace) return;
    CmdState& st = g_cmd[cb];
    auto p = g_pipelines.find(st.pipeline);
    std::string tex;
    for (size_t s = 0; s < st.sets.size(); ++s) {
        auto it = g_sets.find(st.sets[s]);
        if (it == g_sets.end()) continue;
        for (auto& b : it->second) {
            auto sm = g_samplers.find(b.sampler);
            tex += " s" + std::to_string(s) + "b" + std::to_string(b.binding) + "=" + image_desc(b.view) + ":" +
                   (sm != g_samplers.end() ? sm->second : std::string("?"));
        }
    }
    logf("    %s %u pipe#%u%s", kind, count, p != g_pipelines.end() ? p->second : 0, tex.c_str());
}

// The xBR twin to use for the next draw on cb, or VK_NULL_HANDLE.
// Default: every draw through a UI quad pipeline; the shader passes texels
// through when the quad is drawn at about 1:1 (HD atlases, enlarged layers).
// VKPROXY_XBR_ALL=0: only draws that composite a UI layer, i.e. sample a render
// target smaller than the pass being drawn into.
VkPipeline xbr_twin_for(VkCommandBuffer cb, VkPipeline* original) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (!g_xbr_on) return VK_NULL_HANDLE;
    CmdState& st = g_cmd[cb];
    auto twin = g_xbr_twin.find(st.pipeline);
    if (twin == g_xbr_twin.end() || st.sets.empty()) return VK_NULL_HANDLE;
    if (!g_xbr_all) {
        auto set = g_sets.find(st.sets[0]);
        if (set == g_sets.end()) return VK_NULL_HANDLE;
        bool layer = false;
        for (auto& b : set->second) {
            if (b.binding != 0) continue;
            auto v = g_view_image.find(b.view);
            auto im = v == g_view_image.end() ? g_images.end() : g_images.find(v->second);
            if (im != g_images.end() && (im->second.usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT) &&
                (im->second.w < st.rp_w || im->second.h < st.rp_h))
                layer = true;
        }
        if (!layer) return VK_NULL_HANDLE;
    }
    *original = (VkPipeline)st.pipeline;
    ++g_xbr_draws;
    return twin->second;
}

// After an object draw through a split twin (g_world_partial): remember it, with the textures
// it drew with, to draw its partly covered pixels at the end of the render pass.
void defer_partial(VkCommandBuffer cb, const HdBind& hb, const DrawArgs& args) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    CmdState& st = g_cmd[cb];
    if (!st.in_pass || !g_world_on) return;
    auto partial = g_world_partial.find(st.pipeline);
    auto twin = g_world_twin.find(st.pipeline);
    if (partial == g_world_partial.end() || twin == g_world_twin.end() || st.bound != (uint64_t)twin->second) return;
    auto kind = g_world_kind.find(st.pipeline);
    auto layout = g_world_layout.find(st.pipeline);
    if (kind == g_world_kind.end() || layout == g_world_layout.end()) return;
    static unsigned warned = 0;
    Deferred d;
    d.game_pipeline = st.pipeline;
    d.pipeline = partial->second;
    d.layout = (VkPipelineLayout)layout->second;
    d.sets = kind->second == kWorldFloor ? 1 : 2;  // floors: the texture; objects: texture and depth
    for (size_t s = 0; s < d.sets; ++s) {
        d.set[s] = hb.ours[s] ? hb.ours[s] : s < st.sets.size() ? (VkDescriptorSet)st.sets[s] : VK_NULL_HANDLE;
        // these sets hold one texture each; one with dynamic offsets is not ours to repeat
        bool dyn = s < st.set_calls.size() && st.set_calls[s] && !st.set_calls[s]->dyn.empty();
        if (!d.set[s] || dyn) {
            if (warned++ < 20) logf("WARNING partly covered pixels of a draw not repeated (set %zu)", s);
            return;
        }
    }
    // the push constants as pushed with this layout (others belong to other pipelines)
    for (auto& p : st.pushes)
        if (p.layout == layout->second) d.pushes.push_back(p);
    if (d.pushes.empty() && warned++ < 20) logf("WARNING a repeated draw has no push constants of its layout");
    d.vbs = st.vbs;
    d.ib = st.ib;
    d.ib_off = st.ib_off;
    d.ib_type = st.ib_type;
    d.args = args;
    d.viewports = st.viewports;
    d.scissors = st.scissors;
    (kind->second == kWorldFloor ? st.deferred_floor : st.deferred).push_back(std::move(d));
}

void issue_pushes(VkCommandBuffer cb, std::vector<PushRec> pushes) {
    std::sort(pushes.begin(), pushes.end(), [](const PushRec& a, const PushRec& b) { return a.seq < b.seq; });
    for (auto& p : pushes)
        real_vkCmdPushConstants(cb, (VkPipelineLayout)p.layout, p.stages, p.offset, p.size, p.bytes);
}

void issue_buffers(VkCommandBuffer cb, const std::vector<std::pair<uint64_t, VkDeviceSize>>& vbs, uint64_t ib,
                   VkDeviceSize ib_off, VkIndexType ib_type) {
    for (uint32_t i = 0; i < vbs.size(); ++i)
        if (vbs[i].first) {
            VkBuffer b = (VkBuffer)vbs[i].first;
            real_vkCmdBindVertexBuffers(cb, i, 1, &b, &vbs[i].second);
        }
    if (ib) real_vkCmdBindIndexBuffer(cb, (VkBuffer)ib, ib_off, ib_type);
}

void issue_dynamic_state(VkCommandBuffer cb, const std::vector<VkViewport>& vp, const std::vector<VkRect2D>& sc,
                         int s) {
    std::vector<VkViewport> v;
    for (auto& x : vp) v.push_back(scaled(x, s));
    if (!v.empty()) real_vkCmdSetViewport(cb, 0, static_cast<uint32_t>(v.size()), v.data());
    std::vector<VkRect2D> r;
    for (auto& x : sc) r.push_back(scaled(x, s));
    if (!r.empty()) real_vkCmdSetScissor(cb, 0, static_cast<uint32_t>(r.size()), r.data());
}

const char* mapped_at(uint64_t buffer, VkDeviceSize off, VkDeviceSize len);

// The farthest depth among a deferred object draw's vertices (vertex attribute 3: nearest and
// farthest depth), from the game's mapped vertex buffer; false if it cannot be read.
bool far_depth(const Deferred& d, float* out) {
    auto pi = g_pipe_info.find(d.game_pipeline);
    if (pi == g_pipe_info.end()) return false;
    const VkVertexInputAttributeDescription* attr = nullptr;
    for (auto& a : pi->second.attrs)
        if (a.location == 3 && a.format == VK_FORMAT_R32G32_SFLOAT) attr = &a;
    if (!attr || attr->binding >= d.vbs.size()) return false;
    uint32_t stride = 0;
    for (auto& b : pi->second.bindings)
        if (b.binding == attr->binding) stride = b.stride;
    uint32_t n = std::min(d.args.count, 64u);
    float farthest = -1;
    for (uint32_t k = 0; k < n; ++k) {
        int64_t vertex = d.args.first + k;
        if (d.args.indexed) {
            size_t isz = d.ib_type == VK_INDEX_TYPE_UINT32 ? 4 : 2;
            const char* ip = mapped_at(d.ib, d.ib_off + (d.args.first + k) * isz, isz);
            if (!ip) return false;
            vertex = (isz == 4 ? *reinterpret_cast<const uint32_t*>(ip) : *reinterpret_cast<const uint16_t*>(ip)) +
                     static_cast<int64_t>(d.args.vertex_offset);
        }
        const char* p = mapped_at(d.vbs[attr->binding].first, d.vbs[attr->binding].second + vertex * stride + attr->offset, 8);
        if (!p) return false;
        float z[2];
        memcpy(z, p, sizeof z);
        farthest = std::max(farthest, z[1]);
    }
    *out = farthest;
    return n > 0;
}

// The deferred draws' partly covered pixels (st.deferred or st.deferred_floor), then the game's
// state as it was. The objects' go back to front (by their farthest depth), not in the game's
// order: two partly covered pixels over each other, of a leaf in front of an iron lattice, were
// drawn front first now and then, the leaf's then blending with the floor (light leaf edges).
void flush_partial(VkCommandBuffer cb, bool floors) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    CmdState& st = g_cmd[cb];
    std::vector<Deferred>& list = floors ? st.deferred_floor : st.deferred;
    if (list.empty()) return;
    if (!floors) {
        bool keyed = true;
        for (auto& d : list) keyed = keyed && far_depth(d, &d.key);
        if (keyed) {
            std::stable_sort(list.begin(), list.end(), [](const Deferred& a, const Deferred& b) { return a.key > b.key; });
            st.sorted.insert(st.sorted.end(), list.begin(), list.end());
        } else {
            static unsigned warned = 0;
            if (warned++ < 20) logf("WARNING repeated object draws left in the game's order (vertices not readable)");
        }
    }
    if (g_trace) logf("    [partly covered pixels of %zu %s draws]", list.size(), floors ? "floor" : "object");
    VkPipeline pipeline = VK_NULL_HANDLE;
    const std::vector<VkViewport>* vp = &st.viewports;
    const std::vector<VkRect2D>* sc = &st.scissors;
    for (auto& d : list) {
        if (d.pipeline != pipeline) real_vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, pipeline = d.pipeline);
        real_vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, d.layout, 0, d.sets, d.set, 0, nullptr);
        issue_pushes(cb, d.pushes);
        issue_buffers(cb, d.vbs, d.args.indexed ? d.ib : 0, d.ib_off, d.ib_type);
        if (d.viewports.size() != vp->size() || d.scissors.size() != sc->size() ||
            !std::equal(d.viewports.begin(), d.viewports.end(), vp->begin(),
                        [](const VkViewport& a, const VkViewport& b) { return !memcmp(&a, &b, sizeof a); }) ||
            !std::equal(d.scissors.begin(), d.scissors.end(), sc->begin(),
                        [](const VkRect2D& a, const VkRect2D& b) { return !memcmp(&a, &b, sizeof a); })) {
            issue_dynamic_state(cb, d.viewports, d.scissors, st.scale);
            vp = &d.viewports;
            sc = &d.scissors;
        }
        const DrawArgs& a = d.args;
        if (a.indexed)
            real_vkCmdDrawIndexed(cb, a.count, a.instances, a.first, a.vertex_offset, a.first_instance);
        else
            real_vkCmdDraw(cb, a.count, a.instances, a.first, a.first_instance);
    }
    list.clear();
    // the game's state again: pipeline, sets (their binding calls in the game's order), push
    // constants, buffers, viewports and scissors
    if (st.bound) real_vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, (VkPipeline)st.bound);
    std::vector<std::shared_ptr<SetCall>> calls;
    for (auto& c : st.set_calls)
        if (c && std::find(calls.begin(), calls.end(), c) == calls.end()) calls.push_back(c);
    std::sort(calls.begin(), calls.end(), [](const auto& a, const auto& b) { return a->seq < b->seq; });
    for (auto& c : calls)
        real_vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, (VkPipelineLayout)c->layout, c->first,
                                     static_cast<uint32_t>(c->sets.size()),
                                     reinterpret_cast<const VkDescriptorSet*>(c->sets.data()),
                                     static_cast<uint32_t>(c->dyn.size()), c->dyn.data());
    issue_pushes(cb, st.pushes);
    issue_buffers(cb, st.vbs, st.ib, st.ib_off, st.ib_type);
    issue_dynamic_state(cb, st.viewports, st.scissors, st.scale);
}

// Swap in the xBR twin pipeline and/or an HD texture set around one draw.
template <typename F>
void substituted_draw(VkCommandBuffer cb, const char* kind, const DrawArgs& args, F draw) {
    const uint32_t count = args.count;
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        CmdState& st = g_cmd[cb];
        auto world = g_world_kind.find(st.pipeline);  // a wall or object: the floor tiles are done
        if (!st.deferred_floor.empty() && world != g_world_kind.end() && world->second != kWorldFloor)
            flush_partial(cb, true);
    }
    struct Timed {  // the proxy's time around each draw, while benchmarking
        double t0 = g_bench ? now_ms() : 0;
        bool hd = false, xbr = false;
        ~Timed() {
            if (!t0) return;
            std::lock_guard<std::recursive_mutex> lk(g_mu);
            g_bench_acc.draws_ms += now_ms() - t0;
            ++g_bench_acc.draws;
            g_bench_acc.hd_draws += hd;
            g_bench_acc.xbr_draws += xbr;
        }
    } timed;
    VkPipeline original = VK_NULL_HANDLE;
    VkPipeline twin = xbr_twin_for(cb, &original);
    HdBind hb = hd_bind_for(cb);
    timed.hd = hb.any();
    timed.xbr = twin != VK_NULL_HANDLE;
    trace_draw(cb, hb.any() ? (twin ? "draw[xbr,hd]" : "draw[hd]") : (twin ? "draw[xbr]" : kind), count);
    if (twin) real_vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, twin);
    // The swapped sets hold only their texture (checked), so no dynamic offsets to repeat.
    for (uint32_t s = 0; s < HdBind::kSets; ++s)
        if (hb.ours[s])
            real_vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, hb.layout[s], s, 1, &hb.ours[s], 0, nullptr);
    draw();
    defer_partial(cb, hb, args);
    for (uint32_t s = 0; s < HdBind::kSets; ++s)
        if (hb.ours[s])
            real_vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, hb.layout[s], s, 1, &hb.original[s], 0,
                                         nullptr);
    if (twin) real_vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, original);
}

// World draw log: remember this draw's state; its vertices are read at submit.
void record_draw(VkCommandBuffer cb, bool indexed, uint32_t first, uint32_t count, int32_t vertex_offset) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (!g_world_now) return;
    CmdState& st = g_cmd[cb];
    DrawRec r{};
    r.pipeline = st.pipeline;
    for (size_t s = 0; s < st.sets.size(); ++s) {
        auto it = g_sets.find(st.sets[s]);
        if (it == g_sets.end()) continue;
        for (auto& b : it->second) {
            auto v = g_view_image.find(b.view);
            r.tex.emplace_back(s, b.binding, v != g_view_image.end() ? v->second : 0);
        }
    }
    r.vbs = st.vbs;
    r.indexed = indexed;
    r.ib = st.ib;
    r.ib_off = st.ib_off;
    r.ib_type = st.ib_type;
    r.first = first;
    r.count = count;
    r.vertex_offset = vertex_offset;
    r.pc.assign(st.pc, st.pc + st.pc_size);
    r.rp_w = st.rp_w;
    r.rp_h = st.rp_h;
    g_draw_recs[cb].push_back(std::move(r));
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdDraw(VkCommandBuffer cb, uint32_t vc, uint32_t ic, uint32_t fv, uint32_t fi) {
    record_draw(cb, false, fv, vc, 0);
    substituted_draw(cb, "draw", DrawArgs{false, vc, ic, fv, 0, fi}, [&] { real_vkCmdDraw(cb, vc, ic, fv, fi); });
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdDrawIndexed(VkCommandBuffer cb, uint32_t n, uint32_t ic, uint32_t fi,
                                                int32_t vo, uint32_t fin) {
    record_draw(cb, true, fi, n, vo);
    substituted_draw(cb, "drawIndexed", DrawArgs{true, n, ic, fi, vo, fin},
                     [&] { real_vkCmdDrawIndexed(cb, n, ic, fi, vo, fin); });
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdBindVertexBuffers(VkCommandBuffer cb, uint32_t first, uint32_t n,
                                                     const VkBuffer* bufs, const VkDeviceSize* offs) {
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        auto& v = g_cmd[cb].vbs;
        if (v.size() < first + n) v.resize(first + n);
        for (uint32_t i = 0; i < n; ++i) v[first + i] = {(uint64_t)bufs[i], offs[i]};
    }
    real_vkCmdBindVertexBuffers(cb, first, n, bufs, offs);
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdBindIndexBuffer(VkCommandBuffer cb, VkBuffer b, VkDeviceSize off, VkIndexType t) {
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        CmdState& st = g_cmd[cb];
        st.ib = (uint64_t)b;
        st.ib_off = off;
        st.ib_type = t;
    }
    real_vkCmdBindIndexBuffer(cb, b, off, t);
}

// Transfers touching an enlarged image are not rescaled yet; make them visible.
void warn_if_enlarged(const char* op, uint64_t image) {
    static unsigned warned = 0;
    if (image_scale(image) > 1 && warned < 50) {
        ++warned;
        logf("WARNING %s on enlarged %s (not rescaled) frame %u", op, img_desc(image).c_str(), g_frame);
    }
}

// ---- trace-only command hooks (log while tracing, otherwise pass through) ----

bool tracing() {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    return g_trace;
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdSetViewport(VkCommandBuffer cb, uint32_t first, uint32_t n,
                                               const VkViewport* v) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (g_trace && n) logf("    setViewport (%.1f,%.1f %.1fx%.1f)", v[0].x, v[0].y, v[0].width, v[0].height);
    CmdState& st = g_cmd[cb];
    if (st.viewports.size() < first + n) st.viewports.resize(first + n);
    for (uint32_t i = 0; i < n; ++i) st.viewports[first + i] = v[i];
    if (st.scale > 1) {
        std::vector<VkViewport> s;
        for (uint32_t i = 0; i < n; ++i) s.push_back(scaled(v[i], st.scale));
        real_vkCmdSetViewport(cb, first, n, s.data());
    } else {
        real_vkCmdSetViewport(cb, first, n, v);
    }
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdSetScissor(VkCommandBuffer cb, uint32_t first, uint32_t n, const VkRect2D* r) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (g_trace && n)
        logf("    setScissor (%d,%d %ux%u)", r[0].offset.x, r[0].offset.y, r[0].extent.width, r[0].extent.height);
    CmdState& st = g_cmd[cb];
    if (st.scissors.size() < first + n) st.scissors.resize(first + n);
    for (uint32_t i = 0; i < n; ++i) st.scissors[first + i] = r[i];
    if (st.scale > 1) {
        std::vector<VkRect2D> s;
        for (uint32_t i = 0; i < n; ++i) s.push_back(scaled(r[i], st.scale));
        real_vkCmdSetScissor(cb, first, n, s.data());
    } else {
        real_vkCmdSetScissor(cb, first, n, r);
    }
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdPushConstants(VkCommandBuffer cb, VkPipelineLayout layout,
                                                 VkShaderStageFlags stages, uint32_t offset, uint32_t size,
                                                 const void* values) {
    if (tracing()) {
        std::string s;
        auto f = static_cast<const float*>(values);
        for (uint32_t i = 0; i < size / 4 && i < 16; ++i) {
            char b[24];
            snprintf(b, sizeof b, " %.5g", f[i]);
            s += b;
        }
        logf("    pushConstants stages=%x off=%u size=%u:%s", static_cast<unsigned>(stages), offset, size, s.c_str());
    }
    if (offset + size <= sizeof(CmdState::pc)) {  // kept for the world draw log and for draws issued again
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        CmdState& st = g_cmd[cb];
        memcpy(st.pc + offset, values, size);
        st.pc_size = std::max(st.pc_size, offset + size);
        PushRec* rec = nullptr;
        for (auto& p : st.pushes)
            if (p.stages == stages && p.offset == offset && p.size == size) rec = &p;
        if (!rec) rec = &st.pushes.emplace_back();
        rec->layout = (uint64_t)layout;
        rec->stages = stages;
        rec->offset = offset;
        rec->size = size;
        memcpy(rec->bytes, values, size);
        rec->seq = ++st.seq;
    }
    real_vkCmdPushConstants(cb, layout, stages, offset, size, values);
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdClearAttachments(VkCommandBuffer cb, uint32_t na, const VkClearAttachment* a,
                                                    uint32_t nr, const VkClearRect* r) {
    int scale;
    VkRect2D area;
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        if (g_trace && nr)
            logf("    clearAttachments %u rects, first (%d,%d %ux%u)", nr, r[0].rect.offset.x, r[0].rect.offset.y,
                 r[0].rect.extent.width, r[0].rect.extent.height);
        scale = g_cmd[cb].scale;
        area = g_cmd[cb].area;
    }
    if (scale > 1) {
        std::vector<VkClearRect> s(r, r + nr);
        for (auto& x : s) {  // scale, then keep inside the render area (required by the spec)
            x.rect = scaled(x.rect, scale);
            int32_t x0 = std::max(x.rect.offset.x, area.offset.x), y0 = std::max(x.rect.offset.y, area.offset.y);
            int64_t x1 = std::min<int64_t>(int64_t(x.rect.offset.x) + x.rect.extent.width,
                                           int64_t(area.offset.x) + area.extent.width);
            int64_t y1 = std::min<int64_t>(int64_t(x.rect.offset.y) + x.rect.extent.height,
                                           int64_t(area.offset.y) + area.extent.height);
            x.rect.offset = {x0, y0};
            x.rect.extent = {static_cast<uint32_t>(std::max<int64_t>(0, x1 - x0)),
                             static_cast<uint32_t>(std::max<int64_t>(0, y1 - y0))};
        }
        real_vkCmdClearAttachments(cb, na, a, nr, s.data());
    } else {
        real_vkCmdClearAttachments(cb, na, a, nr, r);
    }
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdCopyImage(VkCommandBuffer cb, VkImage src, VkImageLayout sl, VkImage dst,
                                             VkImageLayout dl, uint32_t n, const VkImageCopy* r) {
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        if (g_trace && n)
            logf("    copyImage %s -> %s %u regions, first %ux%u", img_desc((uint64_t)src).c_str(),
                 img_desc((uint64_t)dst).c_str(), n, r[0].extent.width, r[0].extent.height);
        warn_if_enlarged("copyImage src", (uint64_t)src);
        warn_if_enlarged("copyImage dst", (uint64_t)dst);
        static unsigned logged = 0;
        if (g_hd.count((uint64_t)dst) && logged++ < 50)  // its HD twin would now be stale
            logf("copyImage into %s, which has an HD twin, frame %u", img_desc((uint64_t)dst).c_str(), g_frame);
    }
    real_vkCmdCopyImage(cb, src, sl, dst, dl, n, r);
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdBlitImage(VkCommandBuffer cb, VkImage src, VkImageLayout sl, VkImage dst,
                                             VkImageLayout dl, uint32_t n, const VkImageBlit* r, VkFilter f) {
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        if (g_trace && n)
            logf("    blitImage %s -> %s %u regions", img_desc((uint64_t)src).c_str(), img_desc((uint64_t)dst).c_str(),
                 n);
        warn_if_enlarged("blitImage src", (uint64_t)src);
        warn_if_enlarged("blitImage dst", (uint64_t)dst);
    }
    real_vkCmdBlitImage(cb, src, sl, dst, dl, n, r, f);
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkBindBufferMemory(VkDevice d, VkBuffer b, VkDeviceMemory m, VkDeviceSize off) {
    VkResult r = real_vkBindBufferMemory(d, b, m, off);
    if (r == VK_SUCCESS) {  // needed to read upload pixels (content replacement, dumps)
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        g_buf_mem[(uint64_t)b] = BufMem{(uint64_t)m, off};
    }
    return r;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkMapMemory(VkDevice d, VkDeviceMemory m, VkDeviceSize off, VkDeviceSize size,
                                              VkMemoryMapFlags fl, void** pp) {
    VkResult r = real_vkMapMemory(d, m, off, size, fl, pp);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        g_mem_map[(uint64_t)m] = MemMap{static_cast<char*>(*pp), off, size};
    }
    return r;
}

VKAPI_ATTR void VKAPI_CALL hk_vkUnmapMemory(VkDevice d, VkDeviceMemory m) {
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        g_mem_map.erase((uint64_t)m);
    }
    real_vkUnmapMemory(d, m);
}

// Bytes per texel of the formats read from uploads (0: not handled).
uint32_t texel_size(VkFormat f) {
    switch (f) {
    case VK_FORMAT_R8_UNORM:
    case VK_FORMAT_R8_UINT:
        return 1;
    case VK_FORMAT_R8G8_UNORM:
    case VK_FORMAT_R16_UNORM:
    case VK_FORMAT_R5G6B5_UNORM_PACK16:
    case VK_FORMAT_B5G6R5_UNORM_PACK16:
        return 2;
    case VK_FORMAT_B8G8R8A8_UNORM:
    case VK_FORMAT_R8G8B8A8_UNORM:
    case VK_FORMAT_R32_SFLOAT:
        return 4;
    default:
        return 0;
    }
}

// The pixels of a whole-image upload into a sampled texture (sprites are B8G8R8A8; their
// depth textures another format), read from the game's mapped staging memory, or null
// (partial upload, unmapped source, a format texel_size does not know...).
const unsigned char* upload_pixels(VkBuffer src, VkImage dst, uint32_t n, const VkBufferImageCopy* r,
                                   const ImageInfo** info) {
    auto im = g_images.find((uint64_t)dst);
    if (im == g_images.end() || n != 1 || im->second.w * im->second.h < 64 ||
        (im->second.usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT))
        return nullptr;
    if (!texel_size(im->second.format)) {
        static std::set<int> logged;
        if (logged.insert(im->second.format).second)
            logf("upload: format %d (%ux%u) not read", static_cast<int>(im->second.format), im->second.w, im->second.h);
        return nullptr;
    }
    const ImageInfo& ii = im->second;
    if (r[0].imageExtent.width != ii.w || r[0].imageExtent.height != ii.h || r[0].imageOffset.x ||
        r[0].imageOffset.y || (r[0].bufferRowLength && r[0].bufferRowLength != ii.w))
        return nullptr;
    auto bm = g_buf_mem.find((uint64_t)src);
    if (bm == g_buf_mem.end()) return nullptr;
    auto mm = g_mem_map.find(bm->second.memory);
    if (mm == g_mem_map.end()) return nullptr;
    VkDeviceSize start = bm->second.offset + r[0].bufferOffset;
    VkDeviceSize size = 1ull * texel_size(ii.format) * ii.w * ii.h;
    if (start < mm->second.offset || (mm->second.size != VK_WHOLE_SIZE && start + size > mm->second.offset + mm->second.size))
        return nullptr;
    *info = &ii;
    return reinterpret_cast<unsigned char*>(mm->second.ptr + (start - mm->second.offset));
}

// Copy out of mapped memory with streaming 16-byte loads. The game's staging memory
// is not host-cached (write-combined): read a byte at a time it gave ~11 MB/s, 1.85 ms
// per sprite and 1.8 s of every camera turn.
__attribute__((target("sse4.1"))) void copy_streaming(unsigned char* dst, const unsigned char* src, size_t n) {
    size_t i = 0;
    for (; i < n && (reinterpret_cast<uintptr_t>(src + i) & 15); ++i) dst[i] = src[i];
    for (; i + 64 <= n; i += 64) {
        __m128i* s = reinterpret_cast<__m128i*>(const_cast<unsigned char*>(src + i));
        __m128i a = _mm_stream_load_si128(s), b = _mm_stream_load_si128(s + 1);
        __m128i c = _mm_stream_load_si128(s + 2), d = _mm_stream_load_si128(s + 3);
        _mm_storeu_si128(reinterpret_cast<__m128i*>(dst + i), a);
        _mm_storeu_si128(reinterpret_cast<__m128i*>(dst + i + 16), b);
        _mm_storeu_si128(reinterpret_cast<__m128i*>(dst + i + 32), c);
        _mm_storeu_si128(reinterpret_cast<__m128i*>(dst + i + 48), d);
    }
    for (; i + 16 <= n; i += 16)
        _mm_storeu_si128(reinterpret_cast<__m128i*>(dst + i),
                         _mm_stream_load_si128(reinterpret_cast<__m128i*>(const_cast<unsigned char*>(src + i))));
    for (; i < n; ++i) dst[i] = src[i];
}

void copy_from_mapped(unsigned char* dst, const unsigned char* src, size_t n) {
    static const bool sse41 = [] {
        unsigned a, b, c, d;
        return __get_cpuid(1, &a, &b, &c, &d) && (c & bit_SSE4_1);
    }();
    if (sse41)
        copy_streaming(dst, src, n);
    else
        memcpy(dst, src, n);
}

std::vector<unsigned char> g_name_buf;  // cached copy of the pixels being named

// "<w>x<h>_<hash of the pixels>": names dumps and content replacements. The
// hash is FNV-1a 64 with 1469598103934665603 as offset basis (the standard
// basis lost its last digit); every dump, replacement file and the shader name
// kQuadFragShader depend on it, so it stays (tools/spr2.py matches it). Textures
// of other formats than B8G8R8A8 get "_f<VkFormat>" after it.
std::string content_name(const ImageInfo& ii, const unsigned char* mapped) {
    size_t size = 1ull * texel_size(ii.format) * ii.w * ii.h;
    g_name_buf.resize(size);
    copy_from_mapped(g_name_buf.data(), mapped, size);
    const unsigned char* p = g_name_buf.data();
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i < size; ++i) h = (h ^ p[i]) * 1099511628211ull;
    char name[64];
    snprintf(name, sizeof name, "%ux%u_%016llx", ii.w, ii.h, static_cast<unsigned long long>(h));
    if (ii.format != VK_FORMAT_B8G8R8A8_UNORM) return name + ("_f" + std::to_string(ii.format));
    return name;
}

// Write the pixels of a whole-image upload into a sampled texture to
// <VKPROXY_DIR>\dump\<content name>.bgra, or .raw for other formats (once per content).
void dump_upload(VkBuffer src, VkImage dst, uint32_t n, const VkBufferImageCopy* r) {
    const ImageInfo* info = nullptr;
    const unsigned char* p = upload_pixels(src, dst, n, r, &info);
    if (!p) return;
    const ImageInfo& ii = *info;
    VkDeviceSize size = 1ull * texel_size(ii.format) * ii.w * ii.h;
    std::string name = content_name(ii, p) + (ii.format == VK_FORMAT_B8G8R8A8_UNORM ? ".bgra" : ".raw");
    if (!g_dumped.insert(name).second) return;
    std::string dir = g_dir + "\\dump";
    CreateDirectoryA(dir.c_str(), nullptr);
    if (FILE* f = fopen((dir + "\\" + name).c_str(), "wb")) {
        fwrite(p, 1, static_cast<size_t>(size), f);
        fclose(f);
        logf("dump: %s", name.c_str());
    }
}

PFN_vkVoidFunction device_fn(const char* name) { return real_GDPA(g_device, name); }

// First memory type allowed by `bits` that has all of `want`, or UINT32_MAX.
uint32_t memory_type(uint32_t bits, VkMemoryPropertyFlags want) {
    auto mem_props = reinterpret_cast<PFN_vkGetPhysicalDeviceMemoryProperties>(
        real_GIPA(g_instance, "vkGetPhysicalDeviceMemoryProperties"));
    if (!mem_props) return UINT32_MAX;
    VkPhysicalDeviceMemoryProperties props;
    mem_props(g_phys, &props);
    for (uint32_t i = 0; i < props.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (props.memoryTypes[i].propertyFlags & want) == want) return i;
    return UINT32_MAX;
}

void free_staging(Staging& s) {
    auto destroy_buffer = reinterpret_cast<PFN_vkDestroyBuffer>(device_fn("vkDestroyBuffer"));
    auto free_memory = reinterpret_cast<PFN_vkFreeMemory>(device_fn("vkFreeMemory"));
    if (s.buffer && destroy_buffer) destroy_buffer(g_device, s.buffer, nullptr);
    if (s.memory && free_memory) free_memory(g_device, s.memory, nullptr);  // also unmaps
    s = Staging{};
}

// A host-visible buffer of `size` bytes, mapped at *ptr, or an empty Staging.
Staging new_staging(VkDeviceSize size, void** ptr) {
    Staging st;
    auto create_buffer = reinterpret_cast<PFN_vkCreateBuffer>(device_fn("vkCreateBuffer"));
    auto requirements =
        reinterpret_cast<PFN_vkGetBufferMemoryRequirements>(device_fn("vkGetBufferMemoryRequirements"));
    auto allocate = reinterpret_cast<PFN_vkAllocateMemory>(device_fn("vkAllocateMemory"));
    auto bind = reinterpret_cast<PFN_vkBindBufferMemory>(device_fn("vkBindBufferMemory"));
    auto map = reinterpret_cast<PFN_vkMapMemory>(device_fn("vkMapMemory"));
    if (!create_buffer || !requirements || !allocate || !bind || !map) return st;

    double t0 = now_ms();
    VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bi.size = size;
    bi.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    if (create_buffer(g_device, &bi, nullptr, &st.buffer) != VK_SUCCESS) return Staging{};
    VkMemoryRequirements req;
    requirements(g_device, st.buffer, &req);
    uint32_t type = memory_type(req.memoryTypeBits,
                                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = req.size;
    ai.memoryTypeIndex = type;
    if (type == UINT32_MAX || allocate(g_device, &ai, nullptr, &st.memory) != VK_SUCCESS) st.memory = VK_NULL_HANDLE;
    if (!st.memory || bind(g_device, st.buffer, st.memory, 0) != VK_SUCCESS ||
        map(g_device, st.memory, 0, size, 0, ptr) != VK_SUCCESS)
        free_staging(st);
    g_perf.alloc += now_ms() - t0;
    return st;
}

// Host-visible buffer holding a replacement file (rp.w x rp.h, rp.bpp bytes per texel), or
// empty. Read here and now: font atlases (HD twins go through queue_file).
Staging load_staging(const Replacement& rp) {
    VkDeviceSize size = 1ull * rp.bpp * rp.w * rp.h;
    void* ptr = nullptr;
    Staging st = new_staging(size, &ptr);
    if (!st.buffer) return st;
    size_t got = 0;
    if (FILE* f = fopen(rp.path.c_str(), "rb")) {
        got = fread(ptr, 1, static_cast<size_t>(size), f);
        fclose(f);
    }
    if (got != size) {
        logf("replace: could not read %s (%u bytes)", rp.path.c_str(), static_cast<unsigned>(got));
        free_staging(st);
    }
    return st;
}

// src (w/s x h/s texels of bpp bytes) magnified s times, nearest, into dst (w x h): each row
// magnified once, then copied s times (dst may be a write-combined mapping).
void magnify(unsigned char* dst, const unsigned char* src, uint32_t w, uint32_t h, uint32_t bpp, uint32_t s) {
    uint32_t w1 = w / s, row = w * bpp;
    std::vector<unsigned char> line(row);
    for (uint32_t y1 = 0; y1 < h / s; ++y1) {
        for (uint32_t x = 0; x < w; ++x) memcpy(&line[1ull * x * bpp], src + (1ull * y1 * w1 + x / s) * bpp, bpp);
        for (uint32_t k = 0; k < s; ++k) memcpy(dst + (1ull * y1 * s + k) * row, line.data(), row);
    }
}

// A wall's coverage mask (1x, 0/255) at s times the size with its stair-stepped edges redrawn
// as straight, anti-aliased lines, the way tools/stairs.py redraws the wall tops (keep the two
// in step: the wall tops sit on these edges). The wall shader multiplies the wall's colour by
// the mask and blends, so the partial values give soft edges. For each 1x pixel on an outline
// the line through its neighbourhood is fitted: for each slope (+1/2, -1/2, 0, vertical) and
// side, the pixel centres bound its offset to an interval; the widest feasible one wins, its
// middle is taken; smaller neighbourhoods are tried near corners; with no line, nearest.
struct StairLine {
    int slope;  // index into kStairSlopes, or 3: vertical
    double c;
    bool below;  // inside is below (right of) the line
};
const double kStairSlopes[3] = {0.5, -0.5, 0.0};
const int kStairWindows[3][2] = {{3, 2}, {2, 1}, {1, 1}};
const int kStairSamples = 4;
// A wall face's sloped top edge is moved this far up (1x pixels): the wall top drawn on it has
// a soft edge too, and two soft edges over each other let what is behind the wall show through
// (up to a quarter where each covers half). Moved, the face is solid under the wall top's edge.
// tools/stairs.py upscale(grow_top=) does the same.
const double kWallTopGrow = 0.4;

bool stair_fit(const unsigned char* m, int w, int h, int x, int y, StairLine* line) {
    const double inf = 1e300;
    for (auto& win : kStairWindows) {
        double best = -1;
        for (int si = 0; si < 4; ++si)
            for (int below = 1; below >= 0; --below) {
                double lo = -inf, hi = inf;
                for (int py = std::max(0, y - win[1]); py <= std::min(h - 1, y + win[1]); ++py)
                    for (int px = std::max(0, x - win[0]); px <= std::min(w - 1, x + win[0]); ++px) {
                        double cx = px + 0.5, cy = py + 0.5;
                        double v = si == 3 ? cx : cy - kStairSlopes[si] * cx;
                        if ((m[py * w + px] != 0) == (below != 0))
                            hi = std::min(hi, v);
                        else
                            lo = std::max(lo, v);
                    }
                if (lo < hi && lo > -inf && hi < inf && hi - lo > best) {
                    best = hi - lo;
                    *line = StairLine{si, (lo + hi) / 2, below != 0};
                }
            }
        if (best >= 0) return true;
    }
    return false;
}

void stair_coverage(unsigned char* dst, const unsigned char* m, int w, int h, int s) {
    int W = w * s;
    std::vector<unsigned char> out(static_cast<size_t>(W) * h * s);
    const int n = kStairSamples * kStairSamples;
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x) {
            bool on = m[y * w + x] != 0;
            bool edge = false;
            for (int yy = std::max(0, y - 1); yy <= std::min(h - 1, y + 1) && !edge; ++yy)
                for (int xx = std::max(0, x - 1); xx <= std::min(w - 1, x + 1); ++xx)
                    if ((m[yy * w + xx] != 0) != on) edge = true;
            StairLine line{};
            bool fitted = edge && stair_fit(m, w, h, x, y, &line);
            if (fitted && line.slope < 2 && line.below) line.c -= kWallTopGrow;  // a face's top edge: under the wall top
            for (int j = 0; j < s; ++j)
                for (int i = 0; i < s; ++i) {
                    int X = x * s + i, Y = y * s + j;
                    unsigned char v = on ? 255 : 0;
                    if (fitted) {
                        int hits = 0;
                        for (int b = 0; b < kStairSamples; ++b)
                            for (int a = 0; a < kStairSamples; ++a) {
                                double sx = (X + (a + 0.5) / kStairSamples) / s, sy = (Y + (b + 0.5) / kStairSamples) / s;
                                double t = line.slope == 3 ? sx : sy - kStairSlopes[line.slope] * sx;
                                hits += line.below ? t >= line.c : t <= line.c;
                            }
                        v = static_cast<unsigned char>((hits * 255 + n / 2) / n);
                    }
                    // stored with whether the 1x pixel was covered (tools/stairs.py mark): 128-255,
                    // or 0-127 for a pixel the edges add, which world_wall.frag pushes back a little
                    out[static_cast<size_t>(Y) * W + X] = static_cast<unsigned char>((on ? 128 : 0) + (v * 127 + 127) / 255);
                }
        }
    memcpy(dst, out.data(), out.size());  // dst may be a write-combined mapping
}

// Replacement files for HD twins are read by worker threads. The first open of a file costs
// ~5 ms (the antivirus scans it), reading it ~0.1 ms, and opens overlap (16 at once: ~0.5 ms
// a file), so a lot, floor or camera view that brings hundreds of new files no longer waits
// for them one by one. A read fills its staging buffer's mapping; vkQueueSubmit waits for
// every read queued before it (the copies from those buffers are in what it submits).
struct FileJob {
    std::string path;
    unsigned char* dst = nullptr;      // the staging buffer's mapping
    std::vector<unsigned char> under;  // the game's 1x upload
    uint32_t w = 0, h = 0, bpp = 4;    // the file's (scaled) size
    bool wall = false;
    const Packed* packed = nullptr;    // in a pack: read from there, not from path
};
std::mutex g_jobs_mu;
std::condition_variable g_jobs_cv, g_jobs_done;
std::deque<FileJob> g_jobs;
unsigned g_jobs_pending = 0;  // queued or being read
unsigned g_jobs_loaded = 0;   // since the last wait_files
double g_jobs_mb = 0;
std::vector<std::string> g_jobs_failed;
const unsigned kFileWorkers = 16;

// One job: the file into its staging buffer. For a wall (see g_wall_masks, B8G8R8A8) the
// file's premultiplied pixels are drawn over the upload, magnified, so its corners (wall top,
// repeated bottom row) come from the game and the result is opaque like the game's. A file
// that cannot be read leaves the game's own pixels, magnified.
bool read_job(FileJob& j) {
    std::vector<unsigned char> px(1ull * j.w * j.h * j.bpp);
    size_t got = 0;
    if (j.packed) {  // positional reads on a shared handle: safe from several threads
        OVERLAPPED ov{};
        ov.Offset = static_cast<DWORD>(j.packed->offset);
        ov.OffsetHigh = static_cast<DWORD>(j.packed->offset >> 32);
        DWORD n = 0;
        if (j.packed->size == px.size() && ReadFile(j.packed->file, px.data(), j.packed->size, &n, &ov)) got = n;
    } else if (FILE* f = fopen(j.path.c_str(), "rb")) {
        got = fread(px.data(), 1, px.size(), f);
        fclose(f);
    }
    uint32_t s = static_cast<uint32_t>(g_layer_scale);
    if (got != px.size()) {
        magnify(j.dst, j.under.data(), j.w, j.h, j.bpp, s);
        return false;
    }
    if (j.wall)  // composed in cached memory, then copied: the mapping is write-combined
        for (uint32_t y = 0; y < j.h; ++y)
            for (uint32_t x = 0; x < j.w; ++x) {
                unsigned char* o = &px[(1ull * y * j.w + x) * 4];
                unsigned a = o[3];
                if (a == 255) continue;  // most of a wall
                const unsigned char* u = &j.under[(1ull * (y / s) * (j.w / s) + x / s) * 4];
                for (int c = 0; c < 3; ++c) o[c] = static_cast<unsigned char>(o[c] + (u[c] * (255 - a) + 127) / 255);
                o[3] = 255;
            }
    memcpy(j.dst, px.data(), px.size());
    return true;
}

DWORD WINAPI file_worker(void*) {
    for (;;) {
        FileJob j;
        {
            std::unique_lock<std::mutex> lk(g_jobs_mu);
            g_jobs_cv.wait(lk, [] { return !g_jobs.empty(); });
            j = std::move(g_jobs.front());
            g_jobs.pop_front();
        }
        bool ok = read_job(j);
        _mm_sfence();  // the write-combined stores reach memory before the submit that waits for this
        std::lock_guard<std::mutex> lk(g_jobs_mu);
        if (ok) {
            ++g_jobs_loaded;
            g_jobs_mb += 1.0 * j.w * j.h * j.bpp / 1048576.0;
        } else {
            g_jobs_failed.push_back(j.path);
        }
        if (--g_jobs_pending == 0) g_jobs_done.notify_all();
    }
}

void queue_file(FileJob&& j) {
    static unsigned workers = [] {
        unsigned n = 0;
        for (unsigned i = 0; i < kFileWorkers; ++i)
            if (HANDLE t = CreateThread(nullptr, 0, file_worker, nullptr, 0, nullptr)) {
                CloseHandle(t);
                ++n;
            }
        logf("replace: %u file reader threads", n);
        return n;
    }();
    std::lock_guard<std::mutex> lk(g_jobs_mu);
    if (!workers) {  // no threads: read here
        bool ok = read_job(j);
        ok ? (void)++g_jobs_loaded : g_jobs_failed.push_back(j.path);
        return;
    }
    ++g_jobs_pending;
    g_jobs.push_back(std::move(j));
    g_jobs_cv.notify_one();
}

// Wait for every queued read (before a submit, or before freeing staging buffers); the wait
// and what was read go to g_perf ("files").
void wait_files() {
    double t0 = now_ms();
    unsigned loaded;
    double mb;
    std::vector<std::string> failed;
    {
        std::unique_lock<std::mutex> lk(g_jobs_mu);
        if (!g_jobs_pending && !g_jobs_loaded && g_jobs_failed.empty()) return;
        g_jobs_done.wait(lk, [] { return g_jobs_pending == 0; });
        loaded = g_jobs_loaded;
        mb = g_jobs_mb;
        failed.swap(g_jobs_failed);
        g_jobs_loaded = 0;
        g_jobs_mb = 0;
    }
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    g_perf.file += now_ms() - t0;
    g_perf.loaded += loaded;
    g_perf.mb += mb;
    for (auto& p : failed) logf("replace: could not read %s (showing the game's pixels)", p.c_str());
}

// The antivirus scans a file the first time it is opened (~5 ms) and remembers it: later
// opens take ~0.03 ms, from any process. When the device is created (the game's loading
// screen) background threads open every replacement file once, so the first lot, floor or
// camera view that needs them does not wait for the scans. VKPROXY_WARMUP=0: off.
std::vector<std::string> g_warm_files;
std::atomic<size_t> g_warm_next{0};
std::atomic<unsigned> g_warm_left{0};
double g_warm_t0 = 0;
const unsigned kWarmThreads = 8;

DWORD WINAPI warm_worker(void*) {
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL);
    for (size_t i; (i = g_warm_next++) < g_warm_files.size();) {
        HANDLE f = CreateFileA(g_warm_files[i].c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
        if (f != INVALID_HANDLE_VALUE) CloseHandle(f);
    }
    if (--g_warm_left == 0) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        logf("warm-up: opened %u replacement files in %.1f s", static_cast<unsigned>(g_warm_files.size()),
             (now_ms() - g_warm_t0) / 1000);
    }
    return 0;
}

DWORD WINAPI warm_start(void*) {
    g_warm_t0 = now_ms();
    WIN32_FIND_DATAA fd;
    HANDLE find = FindFirstFileA((g_replace_dir + "\\*").c_str(), &fd);
    if (find == INVALID_HANDLE_VALUE) return 0;
    do
        if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) g_warm_files.push_back(g_replace_dir + "\\" + fd.cFileName);
    while (FindNextFileA(find, &fd));
    FindClose(find);
    g_warm_left = kWarmThreads;
    for (unsigned i = 0; i < kWarmThreads; ++i)
        if (HANDLE t = CreateThread(nullptr, 0, warm_worker, nullptr, 0, nullptr))
            CloseHandle(t);
        else
            --g_warm_left;
    return 0;
}

void start_warmup() {
    static bool started = false;
    char buf[8];
    if (started || !g_replace_on ||
        (GetEnvironmentVariableA("VKPROXY_WARMUP", buf, sizeof buf) && buf[0] == '0'))
        return;
    started = true;
    if (HANDLE t = CreateThread(nullptr, 0, warm_start, nullptr, 0, nullptr)) CloseHandle(t);
}

// Staging for a font atlas, kept for the session: the game recreates its atlases
// often. A failed attempt is remembered as an empty Staging.
VkBuffer staging_for(const Replacement& rp) {
    auto cached = g_staging.find(rp.path);
    if (cached != g_staging.end()) return cached->second.buffer;
    Staging st = load_staging(rp);
    if (st.buffer) logf("replace: staged %s", rp.path.c_str());
    g_staging[rp.path] = st;
    return st.buffer;
}

// One-off staging (HD twins: thousands of files) is freed this many presents
// after the copy was recorded, by when the GPU has long finished it.
const unsigned kStagingFrames = 16;
std::vector<std::pair<Staging, unsigned>> g_staging_retire;  // staging, frame recorded

// Staging for HD twins: one host-visible buffer made while the game loads and mapped for the
// session, used as a ring: each upload takes the next bytes and gives them back kStagingFrames
// presents later. A buffer per upload cost ~0.08 ms (create, allocate, map), hundreds of them
// when a floor or camera view comes in. What does not fit gets a buffer of its own.
const VkDeviceSize kRingBytes = 256ull << 20;  // a lot's first view: ~180 MB
struct Ring {
    Staging st;
    unsigned char* ptr = nullptr;
    VkDeviceSize size = 0, head = 0, used = 0;
    std::deque<std::pair<VkDeviceSize, unsigned>> live;  // (bytes taken, frame), oldest first
};
Ring g_ring;

void free_retired_staging(bool all) {
    wait_files();  // nothing in flight writes into them (a recorded upload never submitted)
    size_t keep = 0;
    for (auto& s : g_staging_retire) {
        if (all || g_frame - s.second > kStagingFrames)
            free_staging(s.first);
        else
            g_staging_retire[keep++] = s;
    }
    g_staging_retire.resize(keep);
    while (!g_ring.live.empty() && (all || g_frame - g_ring.live.front().second > kStagingFrames)) {
        g_ring.used -= g_ring.live.front().first;
        g_ring.live.pop_front();
    }
}

void make_ring() {
    void* ptr = nullptr;
    g_ring.st = new_staging(kRingBytes, &ptr);
    g_ring.ptr = static_cast<unsigned char*>(ptr);
    g_ring.size = g_ring.st.buffer ? kRingBytes : 0;
    logf("hd: staging ring of %u MB%s", static_cast<unsigned>(kRingBytes >> 20), g_ring.size ? "" : " NOT made");
}

// `size` bytes of staging for an HD twin, mapped at *ptr: a range of the ring, else a buffer
// of its own (freed by free_retired_staging).
Staging twin_staging(VkDeviceSize size, void** ptr) {
    VkDeviceSize n = (size + 255) & ~VkDeviceSize{255}, start = g_ring.head, waste = 0;
    if (start + n > g_ring.size) {  // wraps: the bytes left at the end go with it
        waste = g_ring.size - start;
        start = 0;
    }
    if (g_ring.size && g_ring.used + waste + n <= g_ring.size) {
        g_ring.head = start + n;
        g_ring.used += waste + n;
        g_ring.live.emplace_back(waste + n, g_frame);
        *ptr = g_ring.ptr + start;
        Staging st;
        st.buffer = g_ring.st.buffer;
        st.offset = start;
        return st;
    }
    Staging st = new_staging(size, ptr);
    if (st.buffer) g_staging_retire.push_back({st, g_frame});
    return st;
}

// A content replacement file name for this scale, <w>x<h>_<hash>@<scale>.bgra (sprites),
// <w>x<h>_<hash>_f9@<scale>.raw (sprite depth textures, R8) or <w>x<h>_<hash>_w<mask>@<scale>.bgra
// (walls), goes into g_hd_names; true if it is one.
bool index_file(const std::string& n) {
    std::string bgra = "@" + std::to_string(g_layer_scale) + ".bgra", raw = "@" + std::to_string(g_layer_scale) + ".raw";
    auto ends = [&](const std::string& e) { return n.size() > e.size() && !n.compare(n.size() - e.size(), e.size(), e); };
    bool depth = ends(raw);
    if (!depth && !ends(bgra)) return false;
    size_t at = n.rfind('@');
    unsigned w = 0, h = 0;
    char hash[17] = {};
    if (sscanf(n.c_str(), "%ux%u_%16[0-9a-f]", &w, &h, hash) != 3 || strlen(hash) != 16) return false;
    std::string base = std::to_string(w) + "x" + std::to_string(h) + "_" + hash;
    std::string rest = n.substr(0, at).substr(std::min(base.size(), at));
    bool wall = !depth && rest.size() > 2 && rest.compare(0, 2, "_w") == 0 &&
                rest.find_first_not_of("0123456789", 2) == std::string::npos;
    if (n.compare(0, base.size(), base) != 0 || !(rest == (depth ? "_f9" : "") || wall)) return false;
    g_hd_names.insert(n.substr(0, at));
    if (!wall) (depth ? g_z_sizes : g_hd_sizes).insert({w, h});
    return true;
}

// One pack's index (format: tools/hd_pack.py) into g_packed; the pack stays open.
void index_pack(const std::string& path) {
    HANDLE f = CreateFileA(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    if (f == INVALID_HANDLE_VALUE) return;
    LARGE_INTEGER size;
    struct {
        uint32_t count;
        uint64_t index_at;
        char magic[4];
        uint32_t version;
    } __attribute__((packed)) foot;
    auto read_at = [&](uint64_t at, void* dst, uint32_t n) {
        OVERLAPPED ov{};
        ov.Offset = static_cast<DWORD>(at);
        ov.OffsetHigh = static_cast<DWORD>(at >> 32);
        DWORD got = 0;
        return ReadFile(f, dst, n, &got, &ov) && got == n;
    };
    std::vector<char> index;
    bool ok = GetFileSizeEx(f, &size) && size.QuadPart >= static_cast<LONGLONG>(sizeof foot) &&
              read_at(size.QuadPart - sizeof foot, &foot, sizeof foot) && !memcmp(foot.magic, "HDPK", 4) &&
              foot.version == 1 && foot.index_at <= size.QuadPart - sizeof foot &&
              size.QuadPart - sizeof foot - foot.index_at < (64u << 20);
    if (ok) {
        index.resize(static_cast<size_t>(size.QuadPart - sizeof foot - foot.index_at));
        ok = read_at(foot.index_at, index.data(), static_cast<uint32_t>(index.size()));
    }
    unsigned n = 0;
    for (size_t p = 0; ok && n < foot.count; ++n) {
        uint16_t len;
        uint64_t off;
        uint32_t bytes;
        if (p + 2 > index.size() || (memcpy(&len, &index[p], 2), len > 200) || p + 2 + len + 20 > index.size()) {
            ok = false;
            break;
        }
        std::string name(&index[p + 2], len);
        memcpy(&off, &index[p + 2 + len], 8);
        memcpy(&bytes, &index[p + 2 + len + 8], 4);
        p += 2 + len + 20;
        if (off + bytes > foot.index_at || name.find_first_not_of("0123456789abcdefwx_@.rgw") != std::string::npos) {
            ok = false;
            break;
        }
        if (index_file(name)) g_packed[name] = Packed{f, off, bytes};
    }
    if (!ok) logf("hd: %s is not a pack (or is damaged): files from it may be missing", path.c_str());
    g_pack_files.push_back(f);
}

// Index the content replacements once, while the game loads (swapchain creation): the packs
// (tools/hd_pack.py) in name order, a later pack's copy winning, then the loose files, which
// win over the packs.
void scan_hd_files() {
    g_hd_scanned = true;
    double t0 = now_ms();
    WIN32_FIND_DATAA fd;
    std::vector<std::string> packs, loose;
    HANDLE find = FindFirstFileA((g_replace_dir + "\\*").c_str(), &fd);
    if (find != INVALID_HANDLE_VALUE) {
        do {
            std::string n = fd.cFileName;
            if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) continue;
            if (n.size() == 12 && !n.compare(0, 3, "hd_") && !n.compare(7, 5, ".pack"))
                packs.push_back(n);
            else
                loose.push_back(n);
        } while (FindNextFileA(find, &fd));
        FindClose(find);
    }
    std::sort(packs.begin(), packs.end());
    for (auto& p : packs) index_pack(g_replace_dir + "\\" + p);
    for (auto& n : loose)
        if (index_file(n)) g_packed.erase(n);
    logf("hd: %u packs (%u files in them), %u loose files, indexed in %.0f ms", static_cast<unsigned>(packs.size()),
         static_cast<unsigned>(g_packed.size()), static_cast<unsigned>(loose.size()), now_ms() - t0);
    // wall coverage masks: "WMSK", u32 w, h, count, then count x w*h bytes
    find = FindFirstFileA((g_replace_dir + "\\*.wallmasks").c_str(), &fd);
    if (find != INVALID_HANDLE_VALUE) {
        do {
            FILE* f = fopen((g_replace_dir + "\\" + fd.cFileName).c_str(), "rb");
            if (!f) continue;
            char magic[4];
            uint32_t head[3];
            if (fread(magic, 1, 4, f) == 4 && !memcmp(magic, "WMSK", 4) && fread(head, 4, 3, f) == 3 &&
                head[0] && head[1] && head[0] * head[1] <= (1u << 22) && head[2] <= 1024) {
                auto& masks = g_wall_masks[{head[0], head[1]}];
                std::vector<unsigned char> m(1ull * head[0] * head[1]);
                for (uint32_t i = 0; i < head[2] && fread(m.data(), 1, m.size(), f) == m.size(); ++i) {
                    WallMask runs;
                    for (uint32_t p = 0; p < m.size(); ++p)
                        if (m[p] && (!p || !m[p - 1]))
                            runs.emplace_back(p, 1);
                        else if (m[p])
                            ++runs.back().second;
                    masks.push_back(std::move(runs));
                }
            }
            fclose(f);
        } while (FindNextFileA(find, &fd));
        FindClose(find);
    }
    logf("hd: %u content replacements (%u depth sizes, %u wall sizes) in %s",
         static_cast<unsigned>(g_hd_names.size()), static_cast<unsigned>(g_z_sizes.size()),
         static_cast<unsigned>(g_wall_masks.size()), g_replace_dir.c_str());
}

// The descriptor sets made for draws with a twin (hd_bind_for); made again when needed.
void free_hd_sets(VkImageView view) {
    auto free_sets = reinterpret_cast<PFN_vkFreeDescriptorSets>(device_fn("vkFreeDescriptorSets"));
    auto it = g_hd_sets.lower_bound(std::make_tuple((uint64_t)view, uint64_t{0}, uint64_t{0}));
    while (it != g_hd_sets.end() && std::get<0>(it->first) == (uint64_t)view) {
        if (free_sets && g_hd_pool) free_sets(g_device, g_hd_pool, 1, &it->second);
        it = g_hd_sets.erase(it);
    }
}

// HD twin images are bound to ranges of big device-local blocks, made while the game loads
// (more when needed): an allocation per twin cost ~0.1 ms, hundreds of them when a floor or
// camera view comes in. Large twins get memory of their own.
const VkDeviceSize kBlockBytes = 128ull << 20;
struct Block {
    VkDeviceMemory memory = VK_NULL_HANDLE;
    uint32_t type = 0;
    std::map<VkDeviceSize, VkDeviceSize> free;  // offset -> size, adjacent ranges merged
};
std::vector<Block> g_blocks;

bool new_block(uint32_t type) {
    auto allocate = reinterpret_cast<PFN_vkAllocateMemory>(device_fn("vkAllocateMemory"));
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = kBlockBytes;
    ai.memoryTypeIndex = type;
    Block b;
    b.type = type;
    if (!allocate || allocate(g_device, &ai, nullptr, &b.memory) != VK_SUCCESS) return false;
    b.free[0] = kBlockBytes;
    g_blocks.push_back(std::move(b));
    return true;
}

// A range for req in a block of memory type `type` (first fit; a new block when none has
// room): (block, offset), or block -1.
std::pair<int, VkDeviceSize> block_alloc(const VkMemoryRequirements& req, uint32_t type) {
    if (req.size > kBlockBytes / 8 || !req.alignment || (req.alignment & (req.alignment - 1))) return {-1, 0};
    for (int pass = 0; pass < 2; ++pass) {
        for (size_t i = 0; i < g_blocks.size(); ++i) {
            Block& b = g_blocks[i];
            if (b.type != type) continue;
            for (auto it = b.free.begin(); it != b.free.end(); ++it) {
                VkDeviceSize start = it->first, end = it->first + it->second;
                VkDeviceSize off = (start + req.alignment - 1) & ~(req.alignment - 1);
                if (off + req.size > end) continue;
                b.free.erase(it);
                if (off > start) b.free[start] = off - start;
                if (end > off + req.size) b.free[off + req.size] = end - (off + req.size);
                return {static_cast<int>(i), off};
            }
        }
        if (pass == 0 && !new_block(type)) break;
    }
    return {-1, 0};
}

void block_free(int block, VkDeviceSize off, VkDeviceSize size) {
    auto& f = g_blocks[block].free;
    auto next = f.lower_bound(off);
    if (next != f.end() && off + size == next->first) {
        size += next->second;
        next = f.erase(next);
    }
    if (next != f.begin()) {
        auto prev = std::prev(next);
        if (prev->first + prev->second == off) {
            prev->second += size;
            return;
        }
    }
    f[off] = size;
}

// Blocks for the twins, made while the game loads: as many as a sixteenth of the GPU's
// memory holds, at most 4, of the type a twin image needs.
void make_blocks(VkDeviceSize heap) {
    auto requirements = reinterpret_cast<PFN_vkGetImageMemoryRequirements>(device_fn("vkGetImageMemoryRequirements"));
    auto destroy_image = reinterpret_cast<PFN_vkDestroyImage>(device_fn("vkDestroyImage"));
    // not real_vkCreateImage: set only once the game fetches vkCreateImage, after this
    auto create_image = reinterpret_cast<PFN_vkCreateImage>(device_fn("vkCreateImage"));
    if (!requirements || !destroy_image || !create_image) return;
    VkImageCreateInfo ci{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = VK_FORMAT_B8G8R8A8_UNORM;
    ci.extent = {192, 192, 1};
    ci.mipLevels = 1;
    ci.arrayLayers = 1;
    ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.tiling = VK_IMAGE_TILING_OPTIMAL;
    ci.usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    VkImage probe = VK_NULL_HANDLE;
    if (create_image(g_device, &ci, nullptr, &probe) != VK_SUCCESS) return;
    VkMemoryRequirements req;
    requirements(g_device, probe, &req);
    destroy_image(g_device, probe, nullptr);
    uint32_t type = memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    unsigned want = static_cast<unsigned>(std::min<VkDeviceSize>(4, heap / 16 / kBlockBytes)), made = 0;
    while (type != UINT32_MAX && made < want && new_block(type)) ++made;
    logf("hd: %u blocks of %u MB for twin images", made, static_cast<unsigned>(kBlockBytes >> 20));
}

void free_hd(HdImage& hd) {
    auto destroy_view = reinterpret_cast<PFN_vkDestroyImageView>(device_fn("vkDestroyImageView"));
    auto destroy_image = reinterpret_cast<PFN_vkDestroyImage>(device_fn("vkDestroyImage"));
    auto free_memory = reinterpret_cast<PFN_vkFreeMemory>(device_fn("vkFreeMemory"));
    free_hd_sets(hd.view);
    if (hd.view && destroy_view) destroy_view(g_device, hd.view, nullptr);
    if (hd.image && destroy_image) destroy_image(g_device, hd.image, nullptr);
    if (hd.memory && free_memory) free_memory(g_device, hd.memory, nullptr);
    if (hd.block >= 0) block_free(hd.block, hd.offset, hd.bytes);
    hd = HdImage{};
}

// A sampled image `scale` times the size of ii, with a view, or an empty HdImage.
HdImage make_hd(const ImageInfo& ii, int scale, const std::string& name) {
    HdImage hd;
    hd.name = name;
    auto requirements = reinterpret_cast<PFN_vkGetImageMemoryRequirements>(device_fn("vkGetImageMemoryRequirements"));
    auto allocate = reinterpret_cast<PFN_vkAllocateMemory>(device_fn("vkAllocateMemory"));
    auto bind = reinterpret_cast<PFN_vkBindImageMemory>(device_fn("vkBindImageMemory"));
    if (!requirements || !allocate || !bind) return hd;
    VkImageCreateInfo ci{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = ii.format;
    ci.extent = {ii.w * scale, ii.h * scale, 1};
    ci.mipLevels = 1;
    ci.arrayLayers = 1;
    ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.tiling = VK_IMAGE_TILING_OPTIMAL;
    ci.usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    ci.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    if (real_vkCreateImage(g_device, &ci, nullptr, &hd.image) != VK_SUCCESS) {
        hd.image = VK_NULL_HANDLE;
        return hd;
    }
    VkMemoryRequirements req;
    requirements(g_device, hd.image, &req);
    hd.bytes = req.size;
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = req.size;
    ai.memoryTypeIndex = memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    if (ai.memoryTypeIndex == UINT32_MAX) ai.memoryTypeIndex = memory_type(req.memoryTypeBits, 0);
    VkImageViewCreateInfo vi{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    vi.image = hd.image;
    vi.viewType = VK_IMAGE_VIEW_TYPE_2D;
    vi.format = ii.format;
    vi.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    auto [block, offset] = ai.memoryTypeIndex == UINT32_MAX ? std::make_pair(-1, VkDeviceSize{0})
                                                             : block_alloc(req, ai.memoryTypeIndex);
    bool bound;
    if (block >= 0) {
        hd.block = block;
        hd.offset = offset;
        bound = bind(g_device, hd.image, g_blocks[block].memory, offset) == VK_SUCCESS;
    } else {
        bound = ai.memoryTypeIndex != UINT32_MAX && allocate(g_device, &ai, nullptr, &hd.memory) == VK_SUCCESS &&
                bind(g_device, hd.image, hd.memory, 0) == VK_SUCCESS;
    }
    if (!bound || real_vkCreateImageView(g_device, &vi, nullptr, &hd.view) != VK_SUCCESS) free_hd(hd);
    return hd;
}

void acquire_twin(Twin& t) {
    if (t.users++ == 0) {
        g_idle_bytes -= t.hd.bytes;
        --g_idle_count;
        ++t.seq;
    }
}

// A game image no longer shows the twin: when nobody does, it goes idle (kept for reuse).
void release_twin(const std::string& name) {
    auto t = g_twins.find(name);
    if (t == g_twins.end() || t->second.users == 0 || --t->second.users) return;
    ++t->second.seq;
    g_idle_bytes += t->second.hd.bytes;
    ++g_idle_count;
    g_released.emplace_back(name, t->second.seq, g_frame);
}

// Once per present. A released twin may still be read by commands in flight, so only
// kStagingFrames later are its descriptor sets freed (bounding the pool) and may it be
// dropped; idle twins over budget are dropped longest idle first.
void maintain_twins() {
    while (!g_released.empty() && g_frame - std::get<2>(g_released.front()) > kStagingFrames) {
        auto [name, seq, frame] = g_released.front();
        g_released.pop_front();
        auto t = g_twins.find(name);
        if (t == g_twins.end() || t->second.users || t->second.seq != seq) continue;  // shown again since
        free_hd_sets(t->second.hd.view);
        g_idle.emplace_back(name, seq);
    }
    while ((g_idle_bytes > g_twin_budget || g_idle_count > g_idle_max) && !g_idle.empty()) {
        auto [name, seq] = g_idle.front();
        g_idle.pop_front();
        auto t = g_twins.find(name);
        if (t == g_twins.end() || t->second.users || t->second.seq != seq) continue;
        g_idle_bytes -= t->second.hd.bytes;
        --g_idle_count;
        free_hd(t->second.hd);
        g_twins.erase(t);
    }
}

// The HD wallpaper file for a wall upload (its pixels in g_name_buf), or "": the upload's
// pixels under each known coverage mask of its size, hashed like content_name, tried in turn.
std::string wall_file_name(const ImageInfo& ii, const std::vector<WallMask>& masks) {
    const unsigned char* p = g_name_buf.data();
    for (size_t m = 0; m < masks.size(); ++m) {
        uint64_t h = 1469598103934665603ull;
        for (auto [first, count] : masks[m])
            for (const unsigned char *b = p + 4ull * first, *e = b + 4ull * count; b < e; ++b)
                h = (h ^ *b) * 1099511628211ull;
        char name[80];
        snprintf(name, sizeof name, "%ux%u_%016llx_w%u", ii.w, ii.h, static_cast<unsigned long long>(h),
                 static_cast<unsigned>(m));
        if (g_hd_names.count(name)) return name;
    }
    return "";
}

// For each upload into a sampled texture: when its pixels have a replacement
// file, record (into cb, after the game's own copy) the fill of an HD twin.
void hd_upload(VkCommandBuffer cb, VkBuffer src, VkImage dst, uint32_t n, const VkBufferImageCopy* r) {
    if (g_layer_scale < 2 || !g_replace_on || !g_swap_w) return;
    if (!g_hd_scanned) scan_hd_files();
    auto existing = g_hd.find((uint64_t)dst);
    if (g_hd_names.empty() && existing == g_hd.end()) return;
    auto im = g_images.find((uint64_t)dst);
    if (im == g_images.end()) return;
    std::string name;
    const ImageInfo* info = nullptr;
    bool depth = im->second.format == VK_FORMAT_R8_UNORM;  // a sprite's depth texture
    bool colour = im->second.format == VK_FORMAT_B8G8R8A8_UNORM;
    auto wall = colour ? g_wall_masks.find({im->second.w, im->second.h}) : g_wall_masks.end();
    bool wall_mask = depth && g_wall_masks.count({im->second.w, im->second.h});  // same size as its wall
    if (wall != g_wall_masks.end() && n) {  // every upload into a wall-sized image, whole or not
        static unsigned logged = 0;
        if (logged++ < 300)
            logf("wall upload: img#%u %ux%u, %u regions, first at (%d,%d) %ux%u row length %u, frame %u",
                 im->second.id, im->second.w, im->second.h, n, r[0].imageOffset.x, r[0].imageOffset.y,
                 r[0].imageExtent.width, r[0].imageExtent.height, r[0].bufferRowLength, g_frame);
    }
    if ((colour && (g_hd_sizes.count({im->second.w, im->second.h}) || wall != g_wall_masks.end())) ||
        (depth && (g_z_sizes.count({im->second.w, im->second.h}) || wall_mask)))
        if (const unsigned char* p = upload_pixels(src, dst, n, r, &info)) {
            double t0 = now_ms();
            name = content_name(*info, p);
            g_perf.hash += now_ms() - t0;
            ++g_perf.hashed;
        }
    if (existing != g_hd.end()) {
        if (existing->second.name == name) return;  // same pixels again: the twin is still right
        release_twin(existing->second.name);        // new pixels; the old twin may still be in flight
        g_hd.erase(existing);
    }
    if (name.empty()) return;
    auto cached = g_twins.find(name);
    if (cached != g_twins.end()) {  // shown before (or by another image now): filled already
        acquire_twin(cached->second);
        g_hd[(uint64_t)dst] = cached->second.hd;
        ++g_perf.reused;
        return;
    }
    std::string file = name;  // the replacement file, without @scale
    HdImage::Kind kind = HdImage::kFile;
    if (!g_hd_names.count(name)) {
        if (wall_mask) {  // no file: the upload magnified
            file.clear();
            kind = HdImage::kWallMask;
        } else {
            if (wall == g_wall_masks.end()) return;
            // g_name_buf holds the upload (content_name). A wall with no HD wallpaper (a fence,
            // custom content) gets the upload magnified, so its mask's twin, smooth-edged, can go
            // with it (the two are only bound together)
            file = g_no_wallpaper.count(name) ? std::string() : wall_file_name(*info, wall->second);
            static unsigned logged = 0;
            if (logged++ < 300)
                logf("wall: img#%u %s -> %s", info->id, name.c_str(), file.empty() ? "no HD wallpaper: magnified" : file.c_str());
            if (file.empty()) g_no_wallpaper.insert(name);
            kind = HdImage::kWall;
        }
    }
    const ImageInfo& ii = *info;
    Replacement rp{file.empty() ? std::string()
                                : g_replace_dir + "\\" + file + "@" + std::to_string(g_layer_scale) +
                                      (depth ? ".raw" : ".bgra"),
                   ii.w * g_layer_scale, ii.h * g_layer_scale, texel_size(ii.format)};
    auto barrier = reinterpret_cast<PFN_vkCmdPipelineBarrier>(device_fn("vkCmdPipelineBarrier"));
    if (!barrier) return;
    void* mapped = nullptr;
    Staging st = twin_staging(1ull * rp.bpp * rp.w * rp.h, &mapped);
    if (!st.buffer) return;
    if (kind == HdImage::kWallMask)
        stair_coverage(static_cast<unsigned char*>(mapped), g_name_buf.data(), static_cast<int>(ii.w),
                       static_cast<int>(ii.h), g_layer_scale);
    else if (rp.path.empty())  // a wall without an HD wallpaper
        magnify(static_cast<unsigned char*>(mapped), g_name_buf.data(), rp.w, rp.h, rp.bpp, g_layer_scale);
    else {  // filled before the next submit (wait_files)
        FileJob job{rp.path, static_cast<unsigned char*>(mapped), g_name_buf, rp.w, rp.h, rp.bpp, kind == HdImage::kWall};
        auto packed = g_packed.find(rp.path.substr(g_replace_dir.size() + 1));
        if (packed != g_packed.end()) job.packed = &packed->second;
        queue_file(std::move(job));
    }
    VkBuffer staged = st.buffer;
    double t0 = now_ms();
    HdImage hd = make_hd(ii, g_layer_scale, name);
    g_perf.image += now_ms() - t0;
    if (!hd.image) return;
    hd.kind = kind;
    VkImageMemoryBarrier b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.srcAccessMask = 0;
    b.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    b.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    b.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = hd.image;
    b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    barrier(cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &b);
    VkBufferImageCopy reg{};
    reg.bufferOffset = st.offset;
    reg.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    reg.imageExtent = {rp.w, rp.h, 1};
    real_vkCmdCopyBufferToImage(cb, staged, hd.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &reg);
    b.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    b.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
    b.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    b.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    barrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0, nullptr, 0, nullptr, 1, &b);
    g_hd[(uint64_t)dst] = hd;
    Twin& t = g_twins[name];
    t.hd = hd;
    t.users = 1;
    static unsigned logged = 0;
    if (logged++ < 100 || (depth && kind == HdImage::kFile))
        logf("hd: img#%u %s -> %ux%u twin%s", ii.id, name.c_str(), rp.w, rp.h, kind == HdImage::kWallMask ? " (magnified)" : "");
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdCopyBufferToImage(VkCommandBuffer cb, VkBuffer src, VkImage dst,
                                                     VkImageLayout dl, uint32_t n, const VkBufferImageCopy* r) {
    double t0 = now_ms();
    struct Timed {
        double t0;
        ~Timed() {
            std::lock_guard<std::recursive_mutex> lk(g_mu);
            g_perf.copy += now_ms() - t0;
            ++g_perf.uploads;
        }
    } timed{t0};
    VkBuffer staged = VK_NULL_HANDLE;
    uint32_t w = 0, h = 0;
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        if (g_trace && n)
            logf("    copyBufferToImage -> %s %u regions, first %ux%u", img_desc((uint64_t)dst).c_str(), n,
                 r[0].imageExtent.width, r[0].imageExtent.height);
        if (g_dump) dump_upload(src, dst, n, r);
        auto rp = g_replace.find((uint64_t)dst);
        if (rp != g_replace.end()) {
            staged = staging_for(rp->second);
            w = rp->second.w;
            h = rp->second.h;
        } else {
            warn_if_enlarged("copyBufferToImage dst", (uint64_t)dst);
        }
    }
    if (staged) {  // upload the whole replacement instead of the game's 1x pixels
        VkBufferImageCopy reg{};
        reg.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
        reg.imageExtent = {w, h, 1};
        real_vkCmdCopyBufferToImage(cb, staged, dst, dl, 1, &reg);
        return;
    }
    real_vkCmdCopyBufferToImage(cb, src, dst, dl, n, r);
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (g_names_on) {
        const ImageInfo* info = nullptr;
        if (const unsigned char* p = upload_pixels(src, dst, n, r, &info))
            g_image_name[(uint64_t)dst] = content_name(*info, p);
        else
            g_image_name.erase((uint64_t)dst);
    }
    hd_upload(cb, src, dst, n, r);
}

void free_readback(Readback& rb) {
    auto destroy_image = reinterpret_cast<PFN_vkDestroyImage>(device_fn("vkDestroyImage"));
    auto free_memory = reinterpret_cast<PFN_vkFreeMemory>(device_fn("vkFreeMemory"));
    if (rb.image && destroy_image) destroy_image(g_device, rb.image, nullptr);
    if (rb.memory && free_memory) free_memory(g_device, rb.memory, nullptr);
    rb = Readback{};
}

// A 1x image of the game's size for a readback of an enlarged image, or null.
VkImage readback_image(const ImageInfo& info) {
    auto key = std::make_tuple(info.w, info.h, info.format);
    auto cached = g_readback.find(key);
    if (cached != g_readback.end()) return cached->second.image;
    Readback& rb = g_readback[key];
    auto requirements = reinterpret_cast<PFN_vkGetImageMemoryRequirements>(device_fn("vkGetImageMemoryRequirements"));
    auto allocate = reinterpret_cast<PFN_vkAllocateMemory>(device_fn("vkAllocateMemory"));
    auto bind = reinterpret_cast<PFN_vkBindImageMemory>(device_fn("vkBindImageMemory"));
    auto format_props = reinterpret_cast<PFN_vkGetPhysicalDeviceFormatProperties>(
        real_GIPA(g_instance, "vkGetPhysicalDeviceFormatProperties"));
    if (!requirements || !allocate || !bind || !format_props || !real_vkCmdBlitImage ||
        !device_fn("vkCmdPipelineBarrier"))
        return VK_NULL_HANDLE;
    VkFormatProperties fp;
    format_props(g_phys, info.format, &fp);
    const VkFormatFeatureFlags blit = VK_FORMAT_FEATURE_BLIT_SRC_BIT | VK_FORMAT_FEATURE_BLIT_DST_BIT;
    if ((fp.optimalTilingFeatures & blit) != blit) return VK_NULL_HANDLE;
    VkImageCreateInfo ci{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = info.format;
    ci.extent = {info.w, info.h, 1};
    ci.mipLevels = 1;
    ci.arrayLayers = 1;
    ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.tiling = VK_IMAGE_TILING_OPTIMAL;
    ci.usage = VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    ci.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    if (real_vkCreateImage(g_device, &ci, nullptr, &rb.image) != VK_SUCCESS) {  // the real one: never enlarged
        rb.image = VK_NULL_HANDLE;
        return VK_NULL_HANDLE;
    }
    VkMemoryRequirements req;
    requirements(g_device, rb.image, &req);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = req.size;
    ai.memoryTypeIndex = memory_type(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    if (ai.memoryTypeIndex == UINT32_MAX) ai.memoryTypeIndex = memory_type(req.memoryTypeBits, 0);
    if (ai.memoryTypeIndex == UINT32_MAX || allocate(g_device, &ai, nullptr, &rb.memory) != VK_SUCCESS ||
        bind(g_device, rb.image, rb.memory, 0) != VK_SUCCESS) {
        free_readback(rb);
        return VK_NULL_HANDLE;
    }
    logf("readback: made %ux%u fmt%d image for shrinking enlarged readbacks", info.w, info.h,
         static_cast<int>(info.format));
    return rb.image;
}

// Shrink enlarged `src` (scale s) into the 1x image `small` and leave it in
// TRANSFER_SRC_OPTIMAL. Nearest: each 1x pixel takes the centre of its s x s
// block, which is what the game would have drawn at 1x.
void shrink_into(VkCommandBuffer cb, VkImage src, VkImageLayout sl, VkImage small, const ImageInfo& info) {
    auto barrier = reinterpret_cast<PFN_vkCmdPipelineBarrier>(device_fn("vkCmdPipelineBarrier"));
    VkImageMemoryBarrier b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;  // the previous readback through this image
    b.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    b.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    b.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = small;
    b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    barrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &b);
    VkImageBlit blit{};
    blit.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    blit.srcOffsets[1] = {static_cast<int32_t>(info.w * info.scale), static_cast<int32_t>(info.h * info.scale), 1};
    blit.dstSubresource = blit.srcSubresource;
    blit.dstOffsets[1] = {static_cast<int32_t>(info.w), static_cast<int32_t>(info.h), 1};
    real_vkCmdBlitImage(cb, src, sl, small, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &blit, VK_FILTER_NEAREST);
    b.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    b.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    b.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    b.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    barrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &b);
}

// The game reads some render targets back to the CPU (lot thumbnails when
// saving). Those targets are enlarged like UI layers, so read from a 1x copy.
VKAPI_ATTR void VKAPI_CALL hk_vkCmdCopyImageToBuffer(VkCommandBuffer cb, VkImage src, VkImageLayout sl,
                                                     VkBuffer dst, uint32_t n, const VkBufferImageCopy* r) {
    VkImage small = VK_NULL_HANDLE;
    ImageInfo info{};
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        static unsigned count = 0;
        if (g_trace || count < 20) {
            ++count;
            logf("    copyImageToBuffer %s %u regions, first %ux%u (frame %u)", img_desc((uint64_t)src).c_str(), n,
                 n ? r[0].imageExtent.width : 0, n ? r[0].imageExtent.height : 0, g_frame);
        }
        auto im = g_images.find((uint64_t)src);
        if (im != g_images.end() && im->second.scale > 1) {
            info = im->second;
            bool plain = true;  // color, mip 0, layer 0 only
            for (uint32_t i = 0; i < n; ++i) {
                const VkImageSubresourceLayers& s = r[i].imageSubresource;
                if (s.aspectMask != VK_IMAGE_ASPECT_COLOR_BIT || s.mipLevel || s.baseArrayLayer || s.layerCount != 1)
                    plain = false;
            }
            if (plain) small = readback_image(info);
            if (!small) warn_if_enlarged("copyImageToBuffer src", (uint64_t)src);
        }
    }
    if (small) {
        shrink_into(cb, src, sl, small, info);
        real_vkCmdCopyImageToBuffer(cb, small, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, dst, n, r);
        return;
    }
    real_vkCmdCopyImageToBuffer(cb, src, sl, dst, n, r);
}

// Free what the proxy created on this device before the game destroys it.
VKAPI_ATTR void VKAPI_CALL hk_vkDestroyDevice(VkDevice d, const VkAllocationCallbacks* a) {
    if (d && d == g_device) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        for (auto& s : g_staging) free_staging(s.second);
        g_staging.clear();
        free_retired_staging(true);
        for (auto& rb : g_readback) free_readback(rb.second);
        g_readback.clear();
        for (auto& t : g_twins) free_hd(t.second.hd);  // g_hd only points at these
        g_twins.clear();
        free_staging(g_ring.st);
        g_ring = Ring{};
        auto free_memory = reinterpret_cast<PFN_vkFreeMemory>(device_fn("vkFreeMemory"));
        for (auto& b : g_blocks)
            if (free_memory) free_memory(d, b.memory, nullptr);
        g_blocks.clear();
        g_hd.clear();
        g_released.clear();
        g_idle.clear();
        g_idle_bytes = 0;
        g_idle_count = 0;
        auto destroy_pool = reinterpret_cast<PFN_vkDestroyDescriptorPool>(device_fn("vkDestroyDescriptorPool"));
        if (g_hd_pool && destroy_pool) destroy_pool(d, g_hd_pool, nullptr);
        g_hd_pool = VK_NULL_HANDLE;
        g_hd_sets.clear();
        auto destroy_pipeline = reinterpret_cast<PFN_vkDestroyPipeline>(device_fn("vkDestroyPipeline"));
        auto destroy_module = reinterpret_cast<PFN_vkDestroyShaderModule>(device_fn("vkDestroyShaderModule"));
        for (auto& t : g_xbr_twin)
            if (destroy_pipeline) destroy_pipeline(d, t.second, nullptr);
        g_xbr_twin.clear();
        if (g_xbr_module && destroy_module) destroy_module(d, g_xbr_module, nullptr);
        g_xbr_module = VK_NULL_HANDLE;
        for (auto& t : g_world_twin)
            if (destroy_pipeline) destroy_pipeline(d, t.second, nullptr);
        g_world_twin.clear();
        for (auto& t : g_world_partial)
            if (destroy_pipeline) destroy_pipeline(d, t.second, nullptr);
        g_world_partial.clear();
        g_world_kind.clear();
        g_world_layout.clear();
        for (auto& m : g_world_modules) {
            if (m && destroy_module) destroy_module(d, m, nullptr);
            m = VK_NULL_HANDLE;
        }
        g_device = VK_NULL_HANDLE;
    }
    real_vkDestroyDevice(d, a);
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkBeginCommandBuffer(VkCommandBuffer cb, const VkCommandBufferBeginInfo* bi) {
    {
        // a new recording starts with nothing bound: forget what the last one bound, so that
        // flush_partial never binds a set or buffer that may be gone by now
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        CmdState& st = g_cmd[cb];
        st.bound = 0;
        st.in_pass = false;
        st.set_calls.clear();
        st.pushes.clear();
        st.deferred.clear();
        st.deferred_floor.clear();
        st.sorted.clear();
        st.vbs.clear();
        st.ib = 0;
    }
    return real_vkBeginCommandBuffer(cb, bi);
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdNextSubpass(VkCommandBuffer cb, VkSubpassContents sc) {
    flush_partial(cb, true);  // their pipelines belong to this subpass
    flush_partial(cb, false);
    real_vkCmdNextSubpass(cb, sc);
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdEndRenderPass(VkCommandBuffer cb) {
    flush_partial(cb, true);
    flush_partial(cb, false);
    real_vkCmdEndRenderPass(cb);
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (g_trace) logf("  [end]");
    CmdState& st = g_cmd[cb];
    st.in_pass = false;
    if (st.scale > 1) {
        st.scale = 1;
        apply_dynamic_state(cb, st, 1);  // later passes expect the game's values
    }
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateSwapchainKHR(VkDevice d, const VkSwapchainCreateInfoKHR* ci,
                                                        const VkAllocationCallbacks* a, VkSwapchainKHR* out) {
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        g_swap_w = ci->imageExtent.width;
        g_swap_h = ci->imageExtent.height;
        // The game's UI scale: height / 600, lowered by -dpi:N (FINDINGS, H4).
        if (g_layer_scale_auto) {
            int s = std::max(1, static_cast<int>(g_swap_h / 600));
            if (g_dpi_arg > 0) s = std::min(s, g_dpi_arg);
            g_layer_scale = std::min(s, 4);
        }
        // the HD files' index, now that the scale is known (still on the loading screen)
        if (!g_hd_scanned && g_replace_on && g_layer_scale >= 2) scan_hd_files();
    }
    logf("createSwapchain %ux%u fmt%d images>=%u (layer scale %d)", ci->imageExtent.width, ci->imageExtent.height,
         static_cast<int>(ci->imageFormat), ci->minImageCount, g_layer_scale);
    return real_vkCreateSwapchainKHR(d, ci, a, out);
}

// True (and deletes the file) if <VKPROXY_DIR>\<name> exists.
bool consume_trigger(const char* name) {
    std::string path = g_dir + "\\" + name;
    if (GetFileAttributesA(path.c_str()) == INVALID_FILE_ATTRIBUTES) return false;
    DeleteFileA(path.c_str());
    return true;
}

// Host pointer to len bytes at off within buffer, or null when that memory is not mapped.
const char* mapped_at(uint64_t buffer, VkDeviceSize off, VkDeviceSize len) {
    auto bm = g_buf_mem.find(buffer);
    if (bm == g_buf_mem.end()) return nullptr;
    auto mm = g_mem_map.find(bm->second.memory);
    if (mm == g_mem_map.end()) return nullptr;
    VkDeviceSize start = bm->second.offset + off;
    if (start < mm->second.offset || (mm->second.size != VK_WHOLE_SIZE && start + len > mm->second.offset + mm->second.size))
        return nullptr;
    return mm->second.ptr + (start - mm->second.offset);
}

std::string attr_text(const char* p, VkFormat fmt) {
    char b[160];
    auto f = reinterpret_cast<const float*>(p);
    auto u = reinterpret_cast<const unsigned char*>(p);
    switch (fmt) {
    case VK_FORMAT_R32_SFLOAT: snprintf(b, sizeof b, "%g", f[0]); break;
    case VK_FORMAT_R32G32_SFLOAT: snprintf(b, sizeof b, "%g,%g", f[0], f[1]); break;
    case VK_FORMAT_R32G32B32_SFLOAT: snprintf(b, sizeof b, "%g,%g,%g", f[0], f[1], f[2]); break;
    case VK_FORMAT_R32G32B32A32_SFLOAT: snprintf(b, sizeof b, "%g,%g,%g,%g", f[0], f[1], f[2], f[3]); break;
    case VK_FORMAT_R8G8B8A8_UNORM:
    case VK_FORMAT_B8G8R8A8_UNORM: snprintf(b, sizeof b, "#%02x%02x%02x%02x", u[0], u[1], u[2], u[3]); break;
    default: snprintf(b, sizeof b, "fmt%d", static_cast<int>(fmt));
    }
    return b;
}

// Write the world-log draws recorded into cb (called just before it is submitted,
// when the game has finished writing its vertex data).
void write_world_draws(VkCommandBuffer cb) {
    auto it = g_draw_recs.find(cb);
    if (it == g_draw_recs.end()) return;
    static FILE* out = nullptr;
    if (!out) out = fopen((g_dir + "\\world.log").c_str(), "w");
    if (!out) return;
    for (auto& r : it->second) {
        auto pid = g_pipelines.find(r.pipeline);
        auto pi = g_pipe_info.find(r.pipeline);
        fprintf(out, "D %u frame %u pipe#%u vs=%s fs=%s %s first=%u count=%u voff=%d pass=%ux%u\n", g_world_seq++,
                g_frame, pid != g_pipelines.end() ? pid->second : 0, pi != g_pipe_info.end() ? pi->second.vs.c_str() : "?",
                pi != g_pipe_info.end() ? pi->second.fs.c_str() : "?", r.indexed ? "indexed" : "plain", r.first, r.count,
                r.vertex_offset, r.rp_w, r.rp_h);
        for (auto& [set, binding, image] : r.tex) {
            auto n = g_image_name.find(image);
            auto im = g_images.find(image);
            fprintf(out, "  T s%u b%u %s %ux%u\n", static_cast<unsigned>(set), binding,
                    n != g_image_name.end() ? n->second.c_str() : img_desc(image).c_str(),
                    im != g_images.end() ? im->second.w : 0, im != g_images.end() ? im->second.h : 0);
        }
        if (!r.pc.empty()) {
            fprintf(out, "  P");
            for (size_t i = 0; i + 4 <= r.pc.size(); i += 4) fprintf(out, " %g", *reinterpret_cast<const float*>(&r.pc[i]));
            fprintf(out, "\n");
        }
        if (pi == g_pipe_info.end()) continue;
        uint32_t n = std::min(r.count, 64u);
        for (uint32_t k = 0; k < n; ++k) {
            int64_t vertex = r.first + k;
            if (r.indexed) {
                size_t isz = r.ib_type == VK_INDEX_TYPE_UINT32 ? 4 : 2;
                const char* ip = mapped_at(r.ib, r.ib_off + (r.first + k) * isz, isz);
                if (!ip) break;
                vertex = (isz == 4 ? *reinterpret_cast<const uint32_t*>(ip) : *reinterpret_cast<const uint16_t*>(ip)) +
                         static_cast<int64_t>(r.vertex_offset);
            }
            std::string line;
            for (auto& a : pi->second.attrs) {
                uint32_t stride = 0;
                for (auto& b : pi->second.bindings)
                    if (b.binding == a.binding) stride = b.stride;
                if (a.binding >= r.vbs.size()) continue;
                const char* p = mapped_at(r.vbs[a.binding].first, r.vbs[a.binding].second + vertex * stride + a.offset, 16);
                line += " " + std::to_string(a.location) + "=" + (p ? attr_text(p, a.format) : std::string("?"));
            }
            fprintf(out, "  V %lld%s\n", static_cast<long long>(vertex), line.c_str());
        }
    }
    fflush(out);
    g_draw_recs.erase(it);
}

// The repeated object draws were sorted by vertex depths read while recording; the game might
// write its vertices later. Read them again now, when they are final, and log any change.
void check_sorted(VkCommandBuffer cb) {
    auto it = g_cmd.find(cb);
    if (it == g_cmd.end() || it->second.sorted.empty()) return;
    static unsigned checked = 0, changed = 0, logged = 0;
    for (auto& d : it->second.sorted) {
        float k;
        ++checked;
        if (!far_depth(d, &k) || k != d.key) ++changed;
    }
    it->second.sorted.clear();
    if (changed && logged < 20) {
        ++logged;
        logf("WARNING %u of %u repeated object draws had other vertex depths at submit", changed, checked);
        changed = 0;
    }
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkQueueSubmit(VkQueue q, uint32_t n, const VkSubmitInfo* s, VkFence f) {
    wait_files();  // the staging buffers the submitted uploads copy from are filled
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        for (uint32_t i = 0; i < n; ++i)
            for (uint32_t k = 0; k < s[i].commandBufferCount; ++k) check_sorted(s[i].pCommandBuffers[k]);
        if (!g_draw_recs.empty())
            for (uint32_t i = 0; i < n; ++i)
                for (uint32_t k = 0; k < s[i].commandBufferCount; ++k) write_world_draws(s[i].pCommandBuffers[k]);
    }
    double t0 = now_ms();
    VkResult r = real_vkQueueSubmit(q, n, s, f);
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        g_perf.submit += now_ms() - t0;
    }
    if (r != VK_SUCCESS) {
        static unsigned logged = 0;
        if (logged++ < 20) logf("ERROR vkQueueSubmit -> %d at frame %u", static_cast<int>(r), g_frame);
    }
    return r;
}

// Summary of a finished benchmark: to the log and appended to bench.log.
void write_bench() {
    Bench& b = g_bench_acc;
    std::vector<double> f = b.frames;
    if (f.size() > 1) f.erase(f.begin());  // the first interval started before the benchmark
    std::sort(f.begin(), f.end());
    double total = 0;
    for (double x : f) total += x;
    size_t n = f.size();
    auto pct = [&](double p) { return n ? f[std::min(n - 1, static_cast<size_t>(p * n))] : 0.0; };
    char line[512];
    snprintf(line, sizeof line,
             "%u frames, avg %.2f ms (%.1f fps), p50 %.2f, p95 %.2f, p99 %.2f, max %.1f; proxy per frame: "
             "draws %.3f ms (%.0f draws, %.0f HD, %.0f xBR), uploads %.3f ms, submit %.3f ms; hd=%d xbr=%d scale=%d "
             "%ux%u, %u HD twins shown",
             static_cast<unsigned>(n), n ? total / n : 0, n ? 1000.0 * n / total : 0, pct(0.5), pct(0.95), pct(0.99),
             n ? f.back() : 0, b.draws_ms / b.frames.size(), 1.0 * b.draws / b.frames.size(),
             1.0 * b.hd_draws / b.frames.size(), 1.0 * b.xbr_draws / b.frames.size(), b.uploads_ms / b.frames.size(),
             b.submit_ms / b.frames.size(), g_hd_on, g_xbr_on, g_layer_scale, g_swap_w, g_swap_h,
             static_cast<unsigned>(g_hd.size()));
    logf("== benchmark: %s", line);
    if (FILE* out = fopen((g_dir + "\\bench.log").c_str(), "a")) {
        SYSTEMTIME t;
        GetLocalTime(&t);
        fprintf(out, "%04d-%02d-%02d %02d:%02d:%02d %s\n", t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond,
                line);
        fclose(out);
    }
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkQueuePresentKHR(VkQueue q, const VkPresentInfoKHR* pi) {
    VkResult r = real_vkQueuePresentKHR(q, pi);
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (r != VK_SUCCESS) {
        static unsigned logged = 0;
        if (logged++ < 20) logf("ERROR vkQueuePresentKHR -> %d at frame %u", static_cast<int>(r), g_frame);
    }
    ++g_frame;
    double now = now_ms(), frame_ms = g_last_present_ms ? now - g_last_present_ms : 0;
    g_last_present_ms = now;
    if ((g_perf.uploads || frame_ms > kSlowFrameMs) && g_perf_lines < 20000) {
        ++g_perf_lines;
        logf("perf: frame %u %.1f ms; %u uploads %.1f ms: hash %.1f (%u), reused %u, files %.1f (%u, %.1f MB), "
             "staging %.1f, twins %.1f; submit %.1f; shown %u, twins %u (idle %u, %.0f MB), staging %u",
             g_frame, frame_ms, g_perf.uploads, g_perf.copy, g_perf.hash, g_perf.hashed, g_perf.reused, g_perf.file,
             g_perf.loaded, g_perf.mb, g_perf.alloc, g_perf.image, g_perf.submit, static_cast<unsigned>(g_hd.size()),
             static_cast<unsigned>(g_twins.size()), g_idle_count, g_idle_bytes / 1048576.0,
             static_cast<unsigned>(g_staging_retire.size()));
    }
    if (g_bench) {
        g_bench_acc.frames.push_back(frame_ms);
        g_bench_acc.uploads_ms += g_perf.copy;
        g_bench_acc.submit_ms += g_perf.submit;
        if (g_bench_acc.frames.size() >= kBenchFrames) {
            g_bench = false;
            write_bench();
        }
    }
    g_perf = Perf{};
    if (!g_staging_retire.empty() || !g_ring.live.empty()) free_retired_staging(false);
    maintain_twins();
    if (g_trace) {
        logf("== end of traced frame %u (%u xbr draws)", g_frame, g_xbr_draws);
        g_trace = --g_trace_left > 0;
    }
    g_xbr_draws = 0;
    // Hotkeys only count while the game window has focus; automation uses the
    // trigger files instead (checked twice a second at 60 fps).
    DWORD pid = 0;
    GetWindowThreadProcessId(GetForegroundWindow(), &pid);
    bool focused = pid == GetCurrentProcessId();
    bool toggle = (GetAsyncKeyState(VK_F10) & 1) && focused;
    bool trace = (GetAsyncKeyState(VK_F11) & 1) && focused;
    bool hd_toggle = (GetAsyncKeyState(VK_F9) & 1) && focused;
    bool world = (GetAsyncKeyState(VK_F8) & 1) && focused;
    bool bench = (GetAsyncKeyState(VK_F7) & 1) && focused;
    bool world_toggle = (GetAsyncKeyState(VK_F6) & 1) && focused;
    if (g_frame % 30 == 0) {
        world_toggle = consume_trigger("world.toggle") || world_toggle;
        toggle = consume_trigger("xbr.toggle") || toggle;
        trace = consume_trigger("trace.now") || trace;
        hd_toggle = consume_trigger("hd.toggle") || hd_toggle;
        world = consume_trigger("world.now") || world;
        bench = consume_trigger("bench.now") || bench;
    }
    if (bench && !g_bench) {
        g_bench = true;
        g_bench_acc = Bench{};
        logf("== benchmark: %u frames from frame %u", kBenchFrames, g_frame + 1);
    }
    if (g_world_now) {
        g_world_now = --g_world_left > 0;
        g_draw_recs.clear();  // anything recorded but never submitted
        logf("== world log of frame %u written (%u draws so far)", g_frame, g_world_seq);
    }
    if (world) {
        if (g_names_on) {
            g_world_now = true;
            g_world_left = kWorldFrames;
            logf("== world log: recording frames %u to %u", g_frame + 1, g_frame + kWorldFrames);
        } else {
            logf("== world log needs VKPROXY_WORLDLOG=1 from game start (texture names)");
        }
    }
    if (toggle) {
        g_xbr_on = !g_xbr_on;
        logf("== xbr %s at frame %u", g_xbr_on ? "on" : "off", g_frame);
    }
    if (hd_toggle) {
        g_hd_on = !g_hd_on;
        logf("== hd %s at frame %u", g_hd_on ? "on" : "off", g_frame);
    }
    if (world_toggle) {
        g_world_on = !g_world_on;
        logf("== world shaders %s at frame %u", g_world_on ? "ours" : "the game's", g_frame);
    }
    if (trace) {
        g_trace = true;
        g_trace_left = kTraceFrames;
        logf("== tracing frames %u to %u", g_frame + 1, g_frame + kTraceFrames);
    }
    return r;
}

PFN_vkVoidFunction hook_for(const char* name, PFN_vkVoidFunction real) {
#define MATCH(fn)                                                         \
    if (!strcmp(name, #fn)) {                                             \
        real_##fn = reinterpret_cast<PFN_##fn>(real);                     \
        return reinterpret_cast<PFN_vkVoidFunction>(hk_##fn);             \
    }
    DEVICE_HOOKS(MATCH)
#undef MATCH
    return nullptr;
}

void note_fetch(const char* how, const char* name) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (g_fetched.insert(name).second) logf("fetch %s %s", how, name);
}

// ---- instance-level hooks --------------------------------------------------

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateInstance(const VkInstanceCreateInfo* ci, const VkAllocationCallbacks* a,
                                                    VkInstance* out) {
    for (uint32_t i = 0; i < ci->enabledExtensionCount; ++i) logf("instance ext %s", ci->ppEnabledExtensionNames[i]);
    VkResult r = real_CreateInstance(ci, a, out);
    if (r == VK_SUCCESS) g_instance = *out;
    return r;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateDevice(VkPhysicalDevice pd, const VkDeviceCreateInfo* ci,
                                                  const VkAllocationCallbacks* a, VkDevice* out) {
    for (uint32_t i = 0; i < ci->enabledExtensionCount; ++i) logf("device ext %s", ci->ppEnabledExtensionNames[i]);
    VkResult r = real_CreateDevice(pd, ci, a, out);
    if (r == VK_SUCCESS) {
        g_phys = pd;
        g_device = *out;
        // idle HD twins kept for reuse: a quarter of the largest GPU-local heap, at most
        // 2 GB, and half the driver's allocation count (each twin is one allocation)
        auto mem_props = reinterpret_cast<PFN_vkGetPhysicalDeviceMemoryProperties>(
            real_GIPA(g_instance, "vkGetPhysicalDeviceMemoryProperties"));
        auto props = reinterpret_cast<PFN_vkGetPhysicalDeviceProperties>(
            real_GIPA(g_instance, "vkGetPhysicalDeviceProperties"));
        VkDeviceSize heap = 0;
        if (mem_props) {
            VkPhysicalDeviceMemoryProperties mp;
            mem_props(pd, &mp);
            for (uint32_t i = 0; i < mp.memoryHeapCount; ++i)
                if (mp.memoryHeaps[i].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) heap = std::max(heap, mp.memoryHeaps[i].size);
        }
        if (heap) g_twin_budget = std::min<VkDeviceSize>(heap / 4, 2048ull << 20);
        uint32_t max_allocs = 4096;
        if (props) {
            VkPhysicalDeviceProperties p;
            props(pd, &p);
            max_allocs = p.limits.maxMemoryAllocationCount;
        }
        g_idle_max = std::min<uint32_t>(max_allocs / 2, 8192);
        logf("hd twins: idle budget %u MB, %u twins (GPU heap %u MB, at most %u allocations)",
             static_cast<unsigned>(g_twin_budget >> 20), g_idle_max, static_cast<unsigned>(heap >> 20), max_allocs);
        if (g_replace_on) {  // while the game loads rather than when a lot needs them
            std::lock_guard<std::recursive_mutex> lk(g_mu);
            make_ring();
            make_blocks(heap);
        }
        start_warmup();
    }
    return r;
}

}  // namespace

extern "C" {

__declspec(dllexport) VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice d, const char* name) {
    init();
    if (!real_GDPA) return nullptr;
    PFN_vkVoidFunction real = real_GDPA(d, name);
    if (!real) return nullptr;
    note_fetch("device", name);
    if (!strcmp(name, "vkGetDeviceProcAddr")) return reinterpret_cast<PFN_vkVoidFunction>(vkGetDeviceProcAddr);
    if (PFN_vkVoidFunction h = hook_for(name, real)) return h;
    return real;
}

__declspec(dllexport) VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance inst,
                                                                                   const char* name) {
    init();
    if (!real_GIPA) return nullptr;
    PFN_vkVoidFunction real = real_GIPA(inst, name);
    if (!real) return nullptr;
    note_fetch("instance", name);
    if (!strcmp(name, "vkGetInstanceProcAddr")) return reinterpret_cast<PFN_vkVoidFunction>(vkGetInstanceProcAddr);
    if (!strcmp(name, "vkGetDeviceProcAddr")) {
        real_GDPA = reinterpret_cast<PFN_vkGetDeviceProcAddr>(real);
        return reinterpret_cast<PFN_vkVoidFunction>(vkGetDeviceProcAddr);
    }
    if (!strcmp(name, "vkCreateInstance")) {
        real_CreateInstance = reinterpret_cast<PFN_vkCreateInstance>(real);
        return reinterpret_cast<PFN_vkVoidFunction>(hk_vkCreateInstance);
    }
    if (!strcmp(name, "vkCreateDevice")) {
        real_CreateDevice = reinterpret_cast<PFN_vkCreateDevice>(real);
        if (!real_GDPA && inst)
            real_GDPA = reinterpret_cast<PFN_vkGetDeviceProcAddr>(real_GIPA(inst, "vkGetDeviceProcAddr"));
        return reinterpret_cast<PFN_vkVoidFunction>(hk_vkCreateDevice);
    }
    if (PFN_vkVoidFunction h = hook_for(name, real)) return h;
    return real;
}

}  // extern "C"
