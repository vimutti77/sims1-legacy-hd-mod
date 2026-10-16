// vkproxy: drop-in vulkan-1.dll for The Sims Legacy (32-bit).
//
// Sims.exe loads Vulkan with LoadLibraryA("vulkan-1.dll") and resolves everything
// through vkGetInstanceProcAddr, so a DLL with that name next to the exe is loaded
// instead of the system loader. This DLL forwards every call to the real loader
// and observes rendering: it logs resource creation, dumps SPIR-V shaders and,
// when F11 is pressed, logs every render pass and draw of the next frame.
//
// UI upscaling (option ข): the game draws each UI window into its own 1x
// render target and composites it onto the frame with its textured-quad
// pipeline and a nearest sampler. For every pipeline using that fragment
// shader we create a twin that uses xbr.frag instead, and swap to the twin
// for draws that sample a render target smaller than the current pass.
// F10 toggles this at runtime; VKPROXY_XBR=0 starts with it off.
//
// Output goes to %VKPROXY_DIR% (default %TEMP%\vkproxy).
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan.h>
#include <windows.h>

#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <set>
#include <string>
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
VkShaderModule g_xbr_module = VK_NULL_HANDLE;

void logf(const char* fmt, ...) {
    if (!g_log) return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
    fflush(g_log);
}

void init() {
    static std::once_flag once;
    std::call_once(once, [] {
        char buf[MAX_PATH];
        DWORD n = GetEnvironmentVariableA("VKPROXY_DIR", buf, MAX_PATH);
        if (n == 0 || n >= MAX_PATH) {
            GetTempPathA(MAX_PATH, buf);
            strcat(buf, "vkproxy");
        }
        g_dir = buf;
        CreateDirectoryA(g_dir.c_str(), nullptr);
        g_log = fopen((g_dir + "\\vkproxy.log").c_str(), "w");
        if (GetEnvironmentVariableA("VKPROXY_XBR", buf, MAX_PATH) && buf[0] == '0') g_xbr_on = false;
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
    uint32_t w, h;
    VkFormat format;
    VkImageUsageFlags usage;
};
struct FbInfo {
    uint32_t w, h;
    std::vector<uint64_t> views;
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

const char* filter_name(VkFilter f) { return f == VK_FILTER_NEAREST ? "nearest" : "linear"; }

// ---- device-level hooks ----------------------------------------------------

#define DEVICE_HOOKS(X)                                                                        \
    X(vkCreateImage)                                                                           \
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
    X(vkCreateSwapchainKHR)                                                                    \
    X(vkQueuePresentKHR)

#define DECL_REAL(fn) PFN_##fn real_##fn = nullptr;
DEVICE_HOOKS(DECL_REAL)
#undef DECL_REAL

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateImage(VkDevice d, const VkImageCreateInfo* ci,
                                                 const VkAllocationCallbacks* a, VkImage* out) {
    VkResult r = real_vkCreateImage(d, ci, a, out);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        ImageInfo info{g_next_image++, ci->extent.width, ci->extent.height, ci->format, ci->usage};
        g_images[(uint64_t)*out] = info;
        if (ci->usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT)
            logf("createImage img#%u %ux%u fmt%d usage=%x (color attachment)", info.id, info.w, info.h,
                 static_cast<int>(info.format), static_cast<unsigned>(info.usage));
    }
    return r;
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
    VkResult r = real_vkCreateFramebuffer(d, ci, a, out);
    if (r == VK_SUCCESS) {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        FbInfo fb{ci->width, ci->height, {}};
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
VkPipeline make_xbr_twin(VkDevice d, VkPipelineCache c, const VkGraphicsPipelineCreateInfo& src,
                         const VkAllocationCallbacks* a) {
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
    VkResult r = real_vkCreateGraphicsPipelines(d, c, 1, &ci, a, &twin);
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
                VkPipeline twin = make_xbr_twin(d, c, ci[i], a);
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

VKAPI_ATTR void VKAPI_CALL hk_vkCmdBeginRenderPass(VkCommandBuffer cb, const VkRenderPassBeginInfo* bi,
                                                    VkSubpassContents sc) {
    std::vector<uint64_t> targets;
    {
        std::lock_guard<std::recursive_mutex> lk(g_mu);
        auto fb = g_fbs.find((uint64_t)bi->framebuffer);
        if (fb != g_fbs.end()) targets = fb->second.views;
    }
    begin_pass(cb, bi->renderArea.extent.width, bi->renderArea.extent.height, targets, "renderPass");
    real_vkCmdBeginRenderPass(cb, bi, sc);
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

// The xBR twin to use for the next draw on cb, or VK_NULL_HANDLE: the bound
// pipeline has a twin and set 0 binding 0 is a render target smaller than the
// pass being drawn into (i.e. a UI layer being composited).
VkPipeline xbr_twin_for(VkCommandBuffer cb, VkPipeline* original) {
    std::lock_guard<std::recursive_mutex> lk(g_mu);
    if (!g_xbr_on) return VK_NULL_HANDLE;
    CmdState& st = g_cmd[cb];
    auto twin = g_xbr_twin.find(st.pipeline);
    if (twin == g_xbr_twin.end() || st.sets.empty()) return VK_NULL_HANDLE;
    auto set = g_sets.find(st.sets[0]);
    if (set == g_sets.end()) return VK_NULL_HANDLE;
    for (auto& b : set->second) {
        if (b.binding != 0) continue;
        auto v = g_view_image.find(b.view);
        if (v == g_view_image.end()) return VK_NULL_HANDLE;
        auto im = g_images.find(v->second);
        if (im == g_images.end()) return VK_NULL_HANDLE;
        const ImageInfo& ii = im->second;
        if ((ii.usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT) && (ii.w < st.rp_w || ii.h < st.rp_h)) {
            *original = (VkPipeline)st.pipeline;
            ++g_xbr_draws;
            return twin->second;
        }
    }
    return VK_NULL_HANDLE;
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

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateSwapchainKHR(VkDevice d, const VkSwapchainCreateInfoKHR* ci,
                                                        const VkAllocationCallbacks* a, VkSwapchainKHR* out) {
    logf("createSwapchain %ux%u fmt%d images>=%u", ci->imageExtent.width, ci->imageExtent.height,
         static_cast<int>(ci->imageFormat), ci->minImageCount);
    return real_vkCreateSwapchainKHR(d, ci, a, out);
}

// True (and deletes the file) if <VKPROXY_DIR>\<name> exists.
bool consume_trigger(const char* name) {
    std::string path = g_dir + "\\" + name;
    if (GetFileAttributesA(path.c_str()) == INVALID_FILE_ATTRIBUTES) return false;
    DeleteFileA(path.c_str());
    return true;
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkQueuePresentKHR(VkQueue q, const VkPresentInfoKHR* pi) {
    VkResult r = real_vkQueuePresentKHR(q, pi);
    std::lock_guard<std::recursive_mutex> lk(g_mu);
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
    return real_CreateInstance(ci, a, out);
}

VKAPI_ATTR VkResult VKAPI_CALL hk_vkCreateDevice(VkPhysicalDevice pd, const VkDeviceCreateInfo* ci,
                                                  const VkAllocationCallbacks* a, VkDevice* out) {
    for (uint32_t i = 0; i < ci->enabledExtensionCount; ++i) logf("device ext %s", ci->ppEnabledExtensionNames[i]);
    return real_CreateDevice(pd, ci, a, out);
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
