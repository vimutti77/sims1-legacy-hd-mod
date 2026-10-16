# vkproxy

Drop-in `vulkan-1.dll` (32-bit) for The Sims Legacy Collection. Proof of concept for
option ข in `docs/FINDINGS.md`: upscale UI windows with xBR instead of nearest-neighbour.

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

1. In game: Options → Graphics → Resolution = your monitor's native resolution.
2. Launch option `-dpi:2` (UI scale 2).
3. Copy `vkproxy\build\vulkan-1.dll` next to `Sims.exe`. Remove it to uninstall.

Keys (only while the game window has focus): **F10** toggles xBR on/off, **F11** logs every
render pass and draw of the next frame. Creating an empty file `xbr.toggle` or `trace.now` in
the output folder does the same without touching the keyboard. Log and dumped shaders go to
`%VKPROXY_DIR%` (default `%TEMP%\vkproxy`). `VKPROXY_XBR=0` starts with xBR off.

## Limits

- Only UI drawn into a separate window layer is affected; the in-lot world is untouched.
- xBR smooths edges but adds no detail (see FINDINGS).
- The textured-quad shader is matched by SPIR-V hash (`kQuadFragShader`); a game update
  that changes that shader disables the effect (the game still runs).
