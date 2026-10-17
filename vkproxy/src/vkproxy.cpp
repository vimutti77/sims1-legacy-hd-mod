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

#include <algorithm>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <tuple>
#include <unordered_map>
#include <vector>

namespace {

const uint32_t kXbrFrag[] =
#include "xbr_frag.inc"
    ;
// The game's textured-quad fragment shader: out = vertex colour * texture(binding 0).
const char* const kQuadFragShader = "shader_466f913aec289dea.spv";

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
bool g_xbr_on = true;
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
// Texture replacement (step 2): a sampled B8G8R8A8 image of w x h is created
// g_layer_scale times larger when <g_replace_dir>\<w>x<h>@<scale>.bgra exists,
// and every upload into it is replaced by that file's pixels.
std::string g_replace_dir;  // %VKPROXY_DIR%\replace if set, else <dll folder>\vkproxy\replace
VkInstance g_instance = VK_NULL_HANDLE;
VkPhysicalDevice g_phys = VK_NULL_HANDLE;
VkDevice g_device = VK_NULL_HANDLE;
struct Replacement {
    std::string path;
    uint32_t w, h;  // scaled size
};
// Host-visible copy of a replacement file, shared by every image it replaces
// (the game recreates its font atlases often).
struct Staging {
    VkBuffer buffer = VK_NULL_HANDLE;
    VkDeviceMemory memory = VK_NULL_HANDLE;
};
VkShaderModule g_xbr_module = VK_NULL_HANDLE;

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
        CreateDirectoryA(g_dir.c_str(), nullptr);
        g_log = fopen((g_dir + "\\vkproxy.log").c_str(), "w");
        if (GetEnvironmentVariableA("VKPROXY_XBR", buf, MAX_PATH) && buf[0] == '0') g_xbr_on = false;
        if (GetEnvironmentVariableA("VKPROXY_LAYER_SCALE", buf, MAX_PATH)) {
            g_layer_scale = std::min(std::max(atoi(buf), 1), 4);
            g_layer_scale_auto = false;
        }
        g_dpi_arg = dpi_argument(GetCommandLineA());
        if (GetEnvironmentVariableA("VKPROXY_DUMP", buf, MAX_PATH) && buf[0] == '1') g_dump = true;
        if (GetEnvironmentVariableA("VKPROXY_REPLACE", buf, MAX_PATH) && buf[0] == '0') g_replace_on = false;
        if (GetEnvironmentVariableA("VKPROXY_XBR_ALL", buf, MAX_PATH) && buf[0] == '0') g_xbr_all = false;
        logf("settings: xbr=%d xbr_all=%d layer_scale=%s replace=%d (%s) dump=%d -dpi=%d", g_xbr_on, g_xbr_all,
             g_layer_scale_auto ? "auto" : std::to_string(g_layer_scale).c_str(), g_replace_on,
             g_replace_dir.c_str(), g_dump, g_dpi_arg);
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
struct CmdState {
    uint64_t pipeline = 0;
    std::vector<uint64_t> sets;
    uint32_t rp_w = 0, rp_h = 0;
    std::vector<uint64_t> targets;  // views being rendered to
    int scale = 1;                  // >1 while inside a pass into enlarged targets
    VkRect2D area{};                // render area of the current pass (as issued)
    std::vector<VkViewport> viewports;  // as last set by the game (unscaled)
    std::vector<VkRect2D> scissors;
};

std::unordered_map<uint64_t, ImageInfo> g_images;           // VkImage
std::unordered_map<uint64_t, uint64_t> g_view_image;        // VkImageView -> VkImage
std::unordered_map<uint64_t, FbInfo> g_fbs;                 // VkFramebuffer
std::unordered_map<uint64_t, std::string> g_samplers;       // VkSampler -> "mag/min"
std::unordered_map<uint64_t, std::vector<Binding>> g_sets;  // VkDescriptorSet
std::unordered_map<uint64_t, std::string> g_modules;        // VkShaderModule -> spv file
std::unordered_map<uint64_t, unsigned> g_pipelines;         // VkPipeline -> id
std::unordered_map<uint64_t, VkPipeline> g_xbr_twin;        // game pipeline -> xBR twin
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
    X(vkCmdBeginRenderPass)                                                                    \
    X(vkCmdBeginRendering)                                                                     \
    X(vkCmdBeginRenderingKHR)                                                                  \
    X(vkCmdBindPipeline)                                                                       \
    X(vkCmdBindDescriptorSets)                                                                 \
    X(vkCmdDraw)                                                                               \
    X(vkCmdDrawIndexed)                                                                        \
    X(vkCmdSetViewport)                                                                        \
    X(vkCmdSetScissor)                                                                         \
    X(vkCmdPushConstants)                                                                      \
    X(vkCmdClearAttachments)                                                                   \
    X(vkCmdCopyImage)                                                                          \
    X(vkCmdBlitImage)                                                                          \
    X(vkCmdCopyImageToBuffer)                                                                  \
    X(vkCmdCopyBufferToImage)                                                                  \
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

// Drivers reuse handle values once an object is destroyed (the validation layer
// hides this by wrapping handles). A stale replacement entry made the next image
// with the same handle receive a full-size atlas copy past its end: GPU fault.
VKAPI_ATTR void VKAPI_CALL hk_vkDestroyImage(VkDevice d, VkImage image, const VkAllocationCallbacks* a) {
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        g_images.erase((uint64_t)image);
        if (g_replace.erase((uint64_t)image)) g_freed_replaced.insert((uint64_t)image);
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
        uint64_t h = 1469598103934665603ull;  // FNV-1a
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

// Create a copy of a game pipeline whose fragment stage is xbr.frag.
VkPipeline make_xbr_twin(VkDevice d, VkPipelineCache c, const VkGraphicsPipelineCreateInfo& src) {
    if (!g_xbr_module) {
        VkShaderModuleCreateInfo mi{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
        mi.codeSize = sizeof kXbrFrag;
        mi.pCode = kXbrFrag;
        if (real_vkCreateShaderModule(d, &mi, nullptr, &g_xbr_module) != VK_SUCCESS) {
            logf("xbr: shader module creation failed");
            return VK_NULL_HANDLE;
        }
    }
    std::vector<VkPipelineShaderStageCreateInfo> stages(src.pStages, src.pStages + src.stageCount);
    for (auto& s : stages)
        if (s.stage == VK_SHADER_STAGE_FRAGMENT_BIT) {
            s.module = g_xbr_module;
            s.pName = "main";
            s.pSpecializationInfo = nullptr;
        }
    VkGraphicsPipelineCreateInfo ci = src;
    ci.pStages = stages.data();
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
            for (uint32_t s = 0; s < ci[i].stageCount; ++s) {
                auto m = g_modules.find((uint64_t)ci[i].pStages[s].module);
                bool fs = ci[i].pStages[s].stage == VK_SHADER_STAGE_FRAGMENT_BIT;
                stages += fs ? " fs=" : " vs=";
                stages += m != g_modules.end() ? m->second : "?";
                if (fs && m != g_modules.end() && m->second == kQuadFragShader) quad_fs = true;
            }
            const char* blend = "?";
            if (ci[i].pColorBlendState && ci[i].pColorBlendState->attachmentCount)
                blend = ci[i].pColorBlendState->pAttachments[0].blendEnable ? "blend" : "opaque";
            logf("createGraphicsPipeline pipe#%u%s layout=%llx renderPass=%llx %s", id, stages.c_str(),
                 (unsigned long long)ci[i].layout, (unsigned long long)ci[i].renderPass, blend);
            if (quad_fs) {
                VkPipeline twin = make_xbr_twin(d, c, ci[i]);
                if (twin) g_xbr_twin[(uint64_t)out[i]] = twin;
                logf("  xbr twin for pipe#%u: %s", id, twin ? "ok" : "FAILED");
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
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        if (bp == VK_PIPELINE_BIND_POINT_GRAPHICS) g_cmd[cb].pipeline = (uint64_t)p;
    }
    real_vkCmdBindPipeline(cb, bp, p);
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
    }
    real_vkCmdBindDescriptorSets(cb, bp, layout, first, n, sets, ndyn, dyn);
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

VKAPI_ATTR void VKAPI_CALL hk_vkCmdDraw(VkCommandBuffer cb, uint32_t vc, uint32_t ic, uint32_t fv, uint32_t fi) {
    VkPipeline original = VK_NULL_HANDLE;
    VkPipeline twin = xbr_twin_for(cb, &original);
    trace_draw(cb, twin ? "draw[xbr]" : "draw", vc);
    if (twin) real_vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, twin);
    real_vkCmdDraw(cb, vc, ic, fv, fi);
    if (twin) real_vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, original);
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdDrawIndexed(VkCommandBuffer cb, uint32_t n, uint32_t ic, uint32_t fi,
                                                int32_t vo, uint32_t fin) {
    VkPipeline original = VK_NULL_HANDLE;
    VkPipeline twin = xbr_twin_for(cb, &original);
    trace_draw(cb, twin ? "drawIndexed[xbr]" : "drawIndexed", n);
    if (twin) real_vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, twin);
    real_vkCmdDrawIndexed(cb, n, ic, fi, vo, fin);
    if (twin) real_vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_GRAPHICS, original);
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
    if (r == VK_SUCCESS && g_dump) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        g_buf_mem[(uint64_t)b] = BufMem{(uint64_t)m, off};
    }
    return r;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkMapMemory(VkDevice d, VkDeviceMemory m, VkDeviceSize off, VkDeviceSize size,
                                              VkMemoryMapFlags fl, void** pp) {
    VkResult r = real_vkMapMemory(d, m, off, size, fl, pp);
    if (r == VK_SUCCESS && g_dump) {
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

// Write the pixels of a whole-image upload into a sampled texture to
// <VKPROXY_DIR>\dump\<w>x<h>_<hash>.bgra (once per distinct content).
void dump_upload(VkBuffer src, VkImage dst, uint32_t n, const VkBufferImageCopy* r) {
    auto im = g_images.find((uint64_t)dst);
    if (im == g_images.end() || n != 1 || im->second.w < 256 || im->second.format != VK_FORMAT_B8G8R8A8_UNORM ||
        (im->second.usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT))
        return;
    const ImageInfo& ii = im->second;
    if (r[0].imageExtent.width != ii.w || r[0].imageExtent.height != ii.h ||
        (r[0].bufferRowLength && r[0].bufferRowLength != ii.w))
        return;
    auto bm = g_buf_mem.find((uint64_t)src);
    if (bm == g_buf_mem.end()) return;
    auto mm = g_mem_map.find(bm->second.memory);
    if (mm == g_mem_map.end()) return;
    VkDeviceSize start = bm->second.offset + r[0].bufferOffset;
    VkDeviceSize size = 4ull * ii.w * ii.h;
    if (start < mm->second.offset || (mm->second.size != VK_WHOLE_SIZE && start + size > mm->second.offset + mm->second.size))
        return;
    const unsigned char* p = reinterpret_cast<unsigned char*>(mm->second.ptr + (start - mm->second.offset));
    uint64_t h = 1469598103934665603ull;
    for (VkDeviceSize i = 0; i < size; ++i) h = (h ^ p[i]) * 1099511628211ull;
    char name[96];
    snprintf(name, sizeof name, "%ux%u_%016llx.bgra", ii.w, ii.h, static_cast<unsigned long long>(h));
    if (!g_dumped.insert(name).second) return;
    std::string dir = g_dir + "\\dump";
    CreateDirectoryA(dir.c_str(), nullptr);
    if (FILE* f = fopen((dir + "\\" + name).c_str(), "wb")) {
        fwrite(p, 1, static_cast<size_t>(size), f);
        fclose(f);
        logf("dump: %s", name);
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

// Host-visible buffer holding a replacement file, for uploads into its images.
// Made once per file; a failed attempt is remembered as an empty Staging.
VkBuffer staging_for(const Replacement& rp) {
    auto cached = g_staging.find(rp.path);
    if (cached != g_staging.end()) return cached->second.buffer;
    Staging& st = g_staging[rp.path];
    auto create_buffer = reinterpret_cast<PFN_vkCreateBuffer>(device_fn("vkCreateBuffer"));
    auto requirements =
        reinterpret_cast<PFN_vkGetBufferMemoryRequirements>(device_fn("vkGetBufferMemoryRequirements"));
    auto allocate = reinterpret_cast<PFN_vkAllocateMemory>(device_fn("vkAllocateMemory"));
    auto bind = reinterpret_cast<PFN_vkBindBufferMemory>(device_fn("vkBindBufferMemory"));
    auto map = reinterpret_cast<PFN_vkMapMemory>(device_fn("vkMapMemory"));
    if (!create_buffer || !requirements || !allocate || !bind || !map) return VK_NULL_HANDLE;

    VkDeviceSize size = 4ull * rp.w * rp.h;
    VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bi.size = size;
    bi.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    if (create_buffer(g_device, &bi, nullptr, &st.buffer) != VK_SUCCESS) {
        st.buffer = VK_NULL_HANDLE;
        return VK_NULL_HANDLE;
    }
    VkMemoryRequirements req;
    requirements(g_device, st.buffer, &req);
    uint32_t type = memory_type(req.memoryTypeBits,
                                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = req.size;
    ai.memoryTypeIndex = type;
    void* ptr = nullptr;
    if (type == UINT32_MAX || allocate(g_device, &ai, nullptr, &st.memory) != VK_SUCCESS) st.memory = VK_NULL_HANDLE;
    size_t got = 0;
    if (st.memory && bind(g_device, st.buffer, st.memory, 0) == VK_SUCCESS &&
        map(g_device, st.memory, 0, size, 0, &ptr) == VK_SUCCESS) {
        if (FILE* f = fopen(rp.path.c_str(), "rb")) {
            got = fread(ptr, 1, static_cast<size_t>(size), f);
            fclose(f);
        }
    }
    logf("replace: staged %s (%u bytes read)", rp.path.c_str(), static_cast<unsigned>(got));
    if (got != size) free_staging(st);
    return st.buffer;
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdCopyBufferToImage(VkCommandBuffer cb, VkBuffer src, VkImage dst,
                                                     VkImageLayout dl, uint32_t n, const VkBufferImageCopy* r) {
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
        for (auto& rb : g_readback) free_readback(rb.second);
        g_readback.clear();
        auto destroy_pipeline = reinterpret_cast<PFN_vkDestroyPipeline>(device_fn("vkDestroyPipeline"));
        auto destroy_module = reinterpret_cast<PFN_vkDestroyShaderModule>(device_fn("vkDestroyShaderModule"));
        for (auto& t : g_xbr_twin)
            if (destroy_pipeline) destroy_pipeline(d, t.second, nullptr);
        g_xbr_twin.clear();
        if (g_xbr_module && destroy_module) destroy_module(d, g_xbr_module, nullptr);
        g_xbr_module = VK_NULL_HANDLE;
        g_device = VK_NULL_HANDLE;
    }
    real_vkDestroyDevice(d, a);
}

VKAPI_ATTR void VKAPI_CALL hk_vkCmdEndRenderPass(VkCommandBuffer cb) {
    real_vkCmdEndRenderPass(cb);
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (g_trace) logf("  [end]");
    CmdState& st = g_cmd[cb];
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

VKAPI_ATTR VkResult VKAPI_CALL hk_vkQueueSubmit(VkQueue q, uint32_t n, const VkSubmitInfo* s, VkFence f) {
    VkResult r = real_vkQueueSubmit(q, n, s, f);
    if (r != VK_SUCCESS) {
        static unsigned logged = 0;
        if (logged++ < 20) logf("ERROR vkQueueSubmit -> %d at frame %u", static_cast<int>(r), g_frame);
    }
    return r;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkQueuePresentKHR(VkQueue q, const VkPresentInfoKHR* pi) {
    VkResult r = real_vkQueuePresentKHR(q, pi);
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (r != VK_SUCCESS) {
        static unsigned logged = 0;
        if (logged++ < 20) logf("ERROR vkQueuePresentKHR -> %d at frame %u", static_cast<int>(r), g_frame);
    }
    ++g_frame;
    if (g_trace) {
        logf("== end of traced frame %u (%u xbr draws)", g_frame, g_xbr_draws);
        g_trace = false;
    }
    g_xbr_draws = 0;
    // Hotkeys only count while the game window has focus; automation uses the
    // trigger files instead (checked twice a second at 60 fps).
    DWORD pid = 0;
    GetWindowThreadProcessId(GetForegroundWindow(), &pid);
    bool focused = pid == GetCurrentProcessId();
    bool toggle = (GetAsyncKeyState(VK_F10) & 1) && focused;
    bool trace = (GetAsyncKeyState(VK_F11) & 1) && focused;
    if (g_frame % 30 == 0) {
        toggle = consume_trigger("xbr.toggle") || toggle;
        trace = consume_trigger("trace.now") || trace;
    }
    if (toggle) {
        g_xbr_on = !g_xbr_on;
        logf("== xbr %s at frame %u", g_xbr_on ? "on" : "off", g_frame);
    }
    if (trace) {
        g_trace = true;
        logf("== tracing frame %u", g_frame + 1);
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
