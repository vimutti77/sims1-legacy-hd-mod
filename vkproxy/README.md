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
- walls: an in-lot wall is uploaded as its wallpaper frame with the empty corners filled
  (the wall top in one colour, the bottom row repeated), so it is recognised by the pixels
  the frame covers alone: `vkproxy\replace\<w>x<h>.wallmasks` lists the frames' coverage
  masks per size and `<w>x<h>_<hash of the covered pixels>_w<mask>@<scale>.bgra` is the HD
  wallpaper (`tools/hd_walls.py`). The twin is that file drawn over the upload magnified, so
  the wall top and corners stay the game's own. The wall shader reads the wall at
  `uv * size of its coverage mask (set 1) / size of the wall`, so each wall's mask is
  magnified 3x too and bound with it (only both or neither). The magnified mask has its
  stair-stepped edges redrawn as straight, anti-aliased lines (`stair_coverage`, as
  `tools/stairs.py` does for the HD wall tops that sit on them); the shader multiplies the
  wall's colour by the mask, so the edges come out soft. A wall-sized upload with no HD
  wallpaper (a fence, custom content) gets the upload magnified so its mask can go with it.
- replacement files are read from packs (`tools/hd_pack.py`: a few 1 GB files with an
  index) opened while the game loads; a loose file in `replace\` wins over a packed one.
  Reads run on 16 threads and are waited for before the next `vkQueueSubmit`. Staging (a
  256 MB ring) and the twins' GPU memory (128 MB blocks) are also set up while the game
  loads, and the loose files are opened once in the background (the antivirus scans a
  file the first time it is opened, ~5 ms; `VKPROXY_WARMUP=0` turns that off).
- in-lot sprite shaders: the floor/wall-top, wall and object pipelines get twins with
  `src/world_*.frag` (the game's shaders as decompiled, sampling differently): at the
  middle zoom the game draws floors and wall tops slightly larger than half size and
  every other texel edge falls on a pixel centre, which leaves light dots along the line
  between floors and along wall tops. Ours sample floors on the exact texel lattice and
  every sprite a tenth of a pixel right of and below the pixel centre. The object twins
  draw only opaque pixels; each such draw is repeated just before `vkCmdEndRenderPass`
  without depth writes for the partly covered edge pixels, then the game's state is
  restored. The game's object order changes from frame to frame and sometimes puts a
  piece in front before the one behind it, and an edge pixel drawn then blended with
  the floor and kept the piece behind out (light dots along the edge). **F6** (or
  `world.toggle`) switches back to the game's shaders.
- depth twins: each object sprite comes with a 1x depth texture (R8: the sprite's z, 0
  where it has no pixel, i.e. nearest). For a sprite whose HD coverage grows past the
  original (an outline rounded off by hand, `hd_art.py --install-edits`), a 3x depth
  texture `<w>x<h>_<hash>_f9@<scale>.raw` is bound in its place (set 1 of the world's
  object pipeline), so the new edge pixels have a depth instead of drawing through walls.

The log has a `perf:` line for each frame that uploads textures or takes over 40 ms:
time spent hashing uploads, waiting for replacement files to be read and allocating, and
how many twins are shown, kept and idle.

**F7** (or a `bench.now` file) benchmarks the next 600 frames (about 10 s): frame time
average, p50/p95/p99 and worst, and the proxy's share per frame (draw hooks, uploads,
submit), with the HD/xBR settings. The summary goes to the log and is appended to
`bench.log` in the output folder, so versions and settings can be compared on the same
scene (e.g. once with F9 HD on, once off).

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

Keys (only while the game window has focus): **F6** switches the in-lot sprite shaders
(ours / the game's), **F7** benchmarks (below), **F9** toggles the HD (content-replaced)
textures, **F10** toggles xBR, **F11** logs every render pass and draw of the next 90
frames (time to press it and then do what is to be traced). Creating an empty file
`world.toggle`, `bench.now`, `hd.toggle`, `xbr.toggle` or `trace.now` in the output folder
does the same without touching the keyboard. Log and dumped shaders go to
`%VKPROXY_DIR%` (default `%TEMP%\vkproxy`).

Environment variables, for testing (set before starting `Sims.exe` directly; needs
`steam_appid.txt`):

| Variable | Effect |
|---|---|
| `VKPROXY_XBR=0` | start with xBR off |
| `VKPROXY_XBR_ALL=0` | xBR only on UI window composites (the old behaviour), not on every quad |
| `VKPROXY_LAYER_SCALE=N` | render UI windows at N× instead of the game's UI scale; `1` = off |
| `VKPROXY_REPLACE=0` | don't replace textures (HD font atlases) |
| `VKPROXY_WORLDFIX=0` | start with the game's own in-lot sprite shaders (F6 switches) |
| `VKPROXY_WARMUP=0` | don't open the loose replacement files in the background at start |
| `VKPROXY_DIR=<folder>` | log, shader dumps and `replace\` go here |
| `VKPROXY_DUMP=1` | save every texture upload to `dump\` |
| `VKPROXY_WORLDLOG=1` | name every texture upload by content hash; then **F8** (or a `world.now` file) writes the next frame's draws with their textures, push constants and vertices to `world.log` (how the catalogue builder's assembly rules were found) |

## Limits

- In the lot, only textures with a content-replacement file change (object sprites,
  floors, wallpapers, the tile outline cursor); fences, other wall styles and Sims are
  drawn as the game made them.
- xBR smooths edges but adds no detail (see FINDINGS).
- Readbacks of enlarged targets are handled for `vkCmdCopyImageToBuffer` (lot thumbnails);
  `vkCmdCopyImage`/`vkCmdBlitImage` from them are only logged (not seen so far).
- The textured-quad shader is matched by SPIR-V hash (`kQuadFragShader`); a game update
  that changes that shader disables the effect (the game still runs).
