# vkproxy

Drop-in `vulkan-1.dll` (32-bit) for The Sims Legacy Collection. Draws UI windows at
screen resolution instead of upscaling them from 1x, swaps the game's bitmap font atlases
for HD ones, and smooths the remaining magnified bitmaps (icons) with xBR instead of
nearest-neighbour.

The game loads `vulkan-1.dll` by name, so this DLL placed next to `Sims.exe` is used
instead of the system loader. It forwards every call to the real loader
(`%SystemRoot%\SysWOW64\vulkan-1.dll`), and:

- creates every render target smaller than the screen (UI windows) UI-scale times larger
  and scales all drawing into it, so the window is composited 1:1; when the game reads
  such a target back (lot thumbnails on save) it gets a 1x copy;
- replaces a sampled texture of `w`×`h` with `vkproxy\replace\<w>x<h>@<scale>.bgra`
  next to the DLL when that file exists (HD font atlases, generated from the user's own
  game files; never shipped);
- for each pipeline that uses the game's textured-quad fragment shader, creates a twin
  pipeline with `src/xbr.frag` and uses it for magnified draws;
- content replacement: when the pixels uploaded into a sampled texture hash to a file
  `vkproxy\replace\<w>x<h>_<hash>@<scale>.bgra` (made by `tools/hd_art.py` from the
  user's own game files: HD object sprites), keeps an HD twin image and binds it for the
  draws that sample that texture. The game's own image is untouched; textures without a
  file keep xBR. Twins are kept by content after the game's image goes, so turning the
  camera back to a view reuses them (idle twins: up to a quarter of the GPU memory, at
  most 2 GB, longest idle dropped first).

The log has a `perf:` line for each frame that uploads textures or takes over 40 ms:
time spent hashing uploads, reading replacement files and allocating, and how many twins
are shown, kept and idle.

## Build

Needs the Vulkan SDK (headers + `glslc`) and llvm-mingw (`i686-w64-mingw32-clang++`).

```powershell
powershell -File vkproxy\build.ps1   # -> vkproxy\build\vulkan-1.dll
```

## Use

1. In game: Options → Graphics → Resolution = your monitor's native resolution
   (e.g. `3840x2160`; without `-dpi` the UI scale is height / 600, i.e. 3 on 2160p).
2. Copy `vkproxy\build\vulkan-1.dll` next to `Sims.exe`, and the HD font atlases (if
   generated) to `vkproxy\replace\` in the same folder. Remove both to uninstall.

Keys (only while the game window has focus): **F9** toggles the HD (content-replaced)
textures, **F10** toggles xBR, **F11** logs every render pass and draw of the next frame.
Creating an empty file `hd.toggle`, `xbr.toggle` or `trace.now` in the output folder does
the same without touching the keyboard. Log and dumped shaders go to
`%VKPROXY_DIR%` (default `%TEMP%\vkproxy`).

Environment variables, for testing (set before starting `Sims.exe` directly; needs
`steam_appid.txt`):

| Variable | Effect |
|---|---|
| `VKPROXY_XBR=0` | start with xBR off |
| `VKPROXY_XBR_ALL=0` | xBR only on UI window composites (the old behaviour), not on every quad |
| `VKPROXY_LAYER_SCALE=N` | render UI windows at N× instead of the game's UI scale; `1` = off |
| `VKPROXY_REPLACE=0` | don't replace textures (HD font atlases) |
| `VKPROXY_DIR=<folder>` | log, shader dumps and `replace\` go here |
| `VKPROXY_DUMP=1` | save every texture upload to `dump\` |
| `VKPROXY_WORLDLOG=1` | name every texture upload by content hash; then **F8** (or a `world.now` file) writes the next frame's draws with their textures, push constants and vertices to `world.log` (how the catalogue builder's assembly rules were found) |

## Limits

- In the lot, only textures with a content-replacement file change (object sprites);
  floors, walls and Sims are drawn as the game made them.
- xBR smooths edges but adds no detail (see FINDINGS).
- Readbacks of enlarged targets are handled for `vkCmdCopyImageToBuffer` (lot thumbnails);
  `vkCmdCopyImage`/`vkCmdBlitImage` from them are only logged (not seen so far).
- The textured-quad shader is matched by SPIR-V hash (`kQuadFragShader`); a game update
  that changes that shader disables the effect (the game still runs).
