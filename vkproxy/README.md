# vkproxy

Drop-in `vulkan-1.dll` (32-bit) for The Sims Legacy Collection. Proof of concept for
option ข in `docs/FINDINGS.md`: upscale UI windows with xBR instead of nearest-neighbour,
and (experimental) draw UI windows at screen resolution with HD font atlases.

The game loads `vulkan-1.dll` by name, so this DLL placed next to `Sims.exe` is used
instead of the system loader. It forwards every call to the real loader
(`%SystemRoot%\SysWOW64\vulkan-1.dll`), and:

- for each pipeline that uses the game's textured-quad fragment shader, creates a twin
  pipeline with `src/xbr.frag`;
- when a draw samples a render target smaller than the pass it draws into (a UI window
  being composited onto the frame), binds the twin for that draw only.

## Build

Needs the Vulkan SDK (headers + `glslc`) and llvm-mingw (`i686-w64-mingw32-clang++`).

```powershell
powershell -File vkproxy\build.ps1   # -> vkproxy\build\vulkan-1.dll
```

## Use

1. In game: Options → Graphics → Resolution = your monitor's native resolution
   (e.g. `3840x2160`; without `-dpi` the UI scale is height / 600, i.e. 3 on 2160p).
2. Copy `vkproxy\build\vulkan-1.dll` next to `Sims.exe`. Remove it to uninstall.

Keys (only while the game window has focus): **F10** toggles xBR on/off, **F11** logs every
render pass and draw of the next frame. Creating an empty file `xbr.toggle` or `trace.now` in
the output folder does the same without touching the keyboard. Log and dumped shaders go to
`%VKPROXY_DIR%` (default `%TEMP%\vkproxy`).

Environment variables (set before starting `Sims.exe` directly; needs `steam_appid.txt`):

| Variable | Effect |
|---|---|
| `VKPROXY_XBR=0` | start with xBR off |
| `VKPROXY_LAYER_SCALE=3` | experimental: render UI layers at 3x (screen resolution) instead of 1x |
| `VKPROXY_REPLACE=0` | with layer scale: don't replace textures from `replace\<w>x<h>@3.bgra` (HD font atlases) |
| `VKPROXY_XBR_ALL=1` | experimental: xBR on every textured quad (icons inside layers), not only layer composites |
| `VKPROXY_DUMP=1` | save every texture upload to `dump\` |

## Limits

- Only UI is affected; the in-lot world is untouched.
- xBR smooths edges but adds no detail (see FINDINGS).
- Layer scale: a CPU readback of an enlarged layer (e.g. a thumbnail written to a save) is not
  handled yet, so don't save the game with `VKPROXY_LAYER_SCALE` set.
- The textured-quad shader is matched by SPIR-V hash (`kQuadFragShader`); a game update
  that changes that shader disables the effect (the game still runs).
