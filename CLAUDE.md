# CLAUDE.md

Handoff context for **sims1-legacy-hd-mod**. Read `docs/PLAN.md` for the full plan.

## Goal

Make **The Sims™ Legacy Collection** (The Sims 1, 2025 re-release, Steam) look sharp on high-res displays (1440p, 4K) without lowering the desktop resolution. Order of work: **UI and text first** (phases 0–3), in-lot graphics later (phase 4).

## Current state

- **Phase 0 done (2026-10-17). Results: `docs/FINDINGS.md`.** H2, H3, H4 true; H1 probably false (no layout data found).
- `tools/` have been run against the real install. `far.py` lists `UIGraphics.far` fine. `iff.py` had the wrong header signature; fixed (all 1,578 install `.iff` files parse). 9 tests pass (`py -m unittest discover -s tests`; on this machine `python` is the Store alias, use `py`).
- `launcher/` (SimsLauncher.ps1 + Sims4K.bat) switches the display to 1920×1080 via Win32 `ChangeDisplaySettings`, launches Sims.exe, restores. Still **not tested on Windows**, and probably superseded: in-game Resolution `3840x2160 (16:9)` + launch option `-dpi:2` gives crisp 2× nearest-neighbor scaling without touching the desktop.

## Key facts gathered so far

- Current build (Steam buildid 17905164) **has** in-game display options: Options → Graphics → Monitor / Resolution / Windowed. Resolution list on a 4K monitor: 800x600 … 3840x2160, in 4:3 and 16:9 pairs. Saved as strings in `Options.iff`; applied on restart.
- **Sims 1 equivalent of `uiScaleFactor` = launch option `-dpi:N`**: integer scale `s = clamp(N, 1, H/600)`; logical resolution = Resolution / s. Default `s = H/600` (3 on 2160p → oversized UI). `-dpi:1` = UI drawn 1:1 (H4 solved, no exe patch), but the in-lot world is 1:1 too.
- Command line is `-name:value` (or `-name_value`) and only whitelisted names are read: `dpi`, `monitor`, `maxaspect`, `w`, `skip_intro`, `crashtest`, language codes, `skipLocaleValidation`. That is why `-r…` is ignored.
- `Sims.exe` is 32-bit, wrapped in **SteamStub** (`.bind` section): `.text` is encrypted on disk, so analyse a memory dump of the running process (ASLR on). Renderer is Vulkan.
- Real user data (saves, `Options.iff`) is in `%USERPROFILE%\Saved Games\Electronic Arts\The Sims 25\UserData\`, not the install folder; the game rewrites `Options.iff` and character files on exit.
- UI bitmaps: `UIGraphics\UIGraphics.far` (8-bit RLE8 BMPs). **Loose files at `UIGraphics\<dir>\<name>` override the FAR** (H3). `Res_*.RT` only map resource IDs to bitmap paths; no coordinates, so layout is likely hard-coded in the exe.
- Rendering at `-dpi:2`: UI is drawn at logical resolution and nearest-upscaled; the in-lot world is drawn at native resolution with smooth filtering.
- FaithBeam's Sims 1 Widescreen Patcher does **not** accept the Legacy exe.
- sims2-4k-ui-patch (lah7) is the reference project for Sims 2; Sims 2 Legacy exposes `uiScaleFactor` (cheat `uintProp uiScaleFactor 1|2|3`, and `Graphics Rules.sgr`).
- Sims themselves are real-time 3D meshes with bitmap textures; objects/walls/floors are pre-rendered SPR2 sprites (color + alpha + z-buffer, 3 zoom levels). (From original-game knowledge; not yet checked.)

## Current approach (Phase 1)

The game draws each UI window (and each neighbourhood lot image) into its own 1× render target, then composites it onto the frame scaled ×s with a nearest sampler. Text comes from font atlases (1020×64, 1018×52, 1018×43) drawn as glyph quads; icons are 4-state sprite sheets.

- `vkproxy/` is a drop-in 32-bit `vulkan-1.dll` (next to `Sims.exe`) that forwards to the real loader. Done: xBR on the UI-layer composite draws (works, user keeps it). User's chosen UI scale: 3× (Resolution `3840x2160`, no `-dpi`).
- Next: (1) enlarge UI-layer render targets 3× inside the proxy (images, framebuffers, render area, viewport/scissor, clears, copies) so layout/input stay the same; (2) re-render the font atlases at 3× from a vector font; (3) per-texture xBR (later AI/hand-made) for icons.
- Testing setup: `steam_appid.txt` (3314060) next to `Sims.exe` stops Steam relaunching the game, so it can be started directly (needed for RenderDoc and env vars). Don't send keystrokes while the user's editor has focus; use the proxy's trigger files (`xbr.toggle`, `trace.now` in `%VKPROXY_DIR%`).

## Next steps

1. UI-layer enlargement in `vkproxy` (see above).
2. Untested: setting launch options in Steam Launch Options (only tested by running `Sims.exe` directly), `-maxaspect`, `-monitor`, `-skip_intro`, `-w`.
3. If the memory-dump / xref analysis is needed again (e.g. after an EA update), move those scripts into `tools/` with tests; method is in FINDINGS "วิธีทำซ้ำ". The game has not been updated since 2025-04-04.

Install folder: `C:\Program Files (x86)\Steam\steamapps\common\The Sims Legacy Collection` (app 3314060). Backups of it and of Saved Games are in `backup\`.

## Conventions

- Python 3.9+, **standard library only** for `tools/`. Each tool is a CLI with a module docstring as usage.
- Every new parser gets a unittest using a synthetic file built in the test (no game data in tests).
- **Never commit game files or anything extracted from them.** `work/`, `extracted/`, `backup/` and game file extensions are git-ignored. The eventual product is a patcher run against the user's own install; it never redistributes EA assets.
- Treat extracted game data as untrusted input: `far.py` refuses path traversal in member names; keep that property in any new extractor.
- `docs/` (PLAN.md, FINDINGS.md) is written in Thai; code, comments and README in English.
- Dev machine: Windows, PowerShell, repo at `G:\work\sims1-legacy-hd-mod`.
