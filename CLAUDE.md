# CLAUDE.md

Handoff context for **sims1-legacy-hd-mod**. Read `docs/PLAN.md` for the full plan.

## Goal

Make **The Sims™ Legacy Collection** (The Sims 1, 2025 re-release, Steam) look sharp on high-res displays (1440p, 4K) without lowering the desktop resolution. Order of work: **UI and text first** (phases 0–3), in-lot graphics later (phase 4).

## Current state

- **Phase 0 done (2026-10-17). Results: `docs/FINDINGS.md`.** H2, H3, H4 true; H1 probably false (no layout data found).
- **Phase 1 works and is in daily use (2026-10-17)**: `vkproxy/` draws UI windows at screen resolution, swaps in HD font atlases and xBRs the remaining bitmaps; plain Steam launch, saving works. Details and the bugs found on the way: FINDINGS "Phase 1".
- **HD object sprites installed for the whole game (2026-10-17)**: base, ep1–ep7, deluxe, shared = 85,753 files, 11.5 GB in `<game>\vkproxy\replace`, made by `tools/hd_art.py` with UltraSharp V2 (~2 h on the RTX 5090; the pipeline has since been made 2.4× faster, FINDINGS "ความเร็ว pipeline"). Base/seating reviewed in game; the rest awaits the user's review (F9).
- `tools/`: `far.py`, `iff.py`, `inventory.py`, `exe_strings.py`, `ffn.py` (font reader), `font_atlas.py` (builds the HD atlases from the user's install), `spr2.py`, `hd_composite.py`, `hd_art.py` (HD sprites); AI step and GPU composite in `ai_upscale/`. 51 tests pass (`py -m unittest discover -s tests`; on this machine `python` is the Store alias, use `py`; the 3 GPU-composite tests run only in the venv; `tests/test_catalog.py` needs numpy).
- **HD catalogue pictures (2026-10-18)**: `catalog/` (numpy) redraws the 74x37 Buy/Build pictures from the installed HD sprites, using assembly rules measured in the running game (vkproxy `VKPROXY_WORLDLOG=1` + F8; FINDINGS "ภาพในรายการ"). 1,398 of 1,406 installed (6 of them painted icons upscaled by the AI: `catalog/ai_pictures.py`, list in `work/catalog/ai.txt`) (2026-10-18, after the user's review of the rest). Hand-made lists (`overrides.tsv`, `approved.txt`, `rejected.txt`) and `installed.txt` are in `work/catalog/`; `catalog/review.py --pending` is the page for the ones not installed (ticks + notes, copied back to the chat).
- `launcher/` (display-mode switcher) is superseded by in-game Resolution + vkproxy; never tested on Windows.

## Key facts gathered so far

- Current build (Steam buildid 17905164) **has** in-game display options: Options → Graphics → Monitor / Resolution / Windowed. Resolution list on a 4K monitor: 800x600 … 3840x2160, in 4:3 and 16:9 pairs. Saved as strings in `Options.iff`; applied on restart.
- **Sims 1 equivalent of `uiScaleFactor` = launch option `-dpi:N`**: integer scale `s = clamp(N, 1, H/600)`; logical resolution = Resolution / s. Default `s = H/600` (3 on 2160p → oversized UI). `-dpi:1` = UI drawn 1:1 (H4 solved, no exe patch), but the in-lot world is 1:1 too.
- Command line is `-name:value` (or `-name_value`) and only whitelisted names are read: `dpi`, `monitor`, `maxaspect`, `w`, `skip_intro`, `crashtest`, language codes, `skipLocaleValidation`. That is why `-r…` is ignored.
- `Sims.exe` is 32-bit, wrapped in **SteamStub** (`.bind` section): `.text` is encrypted on disk, so analyse a memory dump of the running process (ASLR on). Renderer is Vulkan.
- Real user data (saves, `Options.iff`) is in `%USERPROFILE%\Saved Games\Electronic Arts\The Sims 25\UserData\`, not the install folder; the game rewrites `Options.iff` and character files on exit.
- UI bitmaps: `UIGraphics\UIGraphics.far` (8-bit RLE8 BMPs). **Loose files at `UIGraphics\<dir>\<name>` override the FAR** (H3). `Res_*.RT` only map resource IDs to bitmap paths; no coordinates, so layout is likely hard-coded in the exe.
- Rendering: UI is drawn at logical resolution and nearest-upscaled; the in-lot world is drawn at native resolution (sprites magnified with nearest).
- FaithBeam's Sims 1 Widescreen Patcher does **not** accept the Legacy exe.
- sims2-4k-ui-patch (lah7) is the reference project for Sims 2; Sims 2 Legacy exposes `uiScaleFactor` (cheat `uintProp uiScaleFactor 1|2|3`, and `Graphics Rules.sgr`).
- Sims themselves are real-time 3D meshes with bitmap textures; objects/walls/floors are pre-rendered SPR2 sprites (color + alpha + z-buffer, 3 zoom levels). (From original-game knowledge; not yet checked.)

## How Phase 1 works

The game draws each UI window (and each neighbourhood lot image, tooltip, …) into its own 1× render target, then composites it onto the frame scaled ×s (UI scale) with a nearest sampler. Text is glyph quads from font atlases the game packs from `.ffn` files at load time; icons are 4-state sprite sheets.

`vkproxy/` is a drop-in 32-bit `vulkan-1.dll` (next to `Sims.exe`) that forwards to the real loader and, by default:
- creates every render target smaller than the swapchain ×s (s = swapchain height / 600, lowered by `-dpi:N`) and scales all drawing into it; `vkCmdCopyImageToBuffer` from such a target (lot thumbnails on save) reads a nearest-shrunk 1× copy;
- replaces a sampled texture of w×h with `<game>\vkproxy\replace\<w>x<h>@<s>.bgra` (HD font atlases from `tools/font_atlas.py`; atlas sizes are unique per font);
- draws every UI textured quad with an xBR twin pipeline (the shader passes through at ~1:1).

User's setup: Resolution `3840x2160 (16:9)`, no `-dpi` (s = 3), launched from Steam. Env vars (`VKPROXY_*`, see `vkproxy/README.md`) are for testing only.

Testing notes:
- `steam_appid.txt` (3314060) next to `Sims.exe` lets the game be started directly (for env vars, RenderDoc); remove it at the end of the project.
- Don't send keystrokes or clicks while the user's editor has focus; use the proxy's trigger files (`xbr.toggle`, `trace.now` in the log folder) or ask the user to press F10/F11.
- Check whether the user is playing before killing `Sims.exe`. Before asking the user to save during a test, back up `UserData`; restore with `robocopy /MIR` and verify.
- 32-bit Vulkan validation layer: Vulkan SDK 1.3.296 `Bin32` (1.4 has none). It wraps handles, so it hides handle-reuse bugs (see FINDINGS).
- Proxy state keyed by Vulkan handles must be dropped on destroy: drivers reuse handle values.

## Next steps

1. HD object sprites, part by part: `py tools\hd_art.py --game <dir> --pack <pack> --category <cat> --install`, then the user reviews in game (F9 toggles HD). All parts built and installed; reviewed: base/seating. G: has only ~6 GB free: `work\hd_art\hd` duplicates the installed files and can go if space is needed. Rules learnt (FINDINGS "sprite ของ object แบบ HD"): coverage must never exceed the original (1x z-buffer textures → leaks through walls), so the AI's alpha is unused and outlines stay stepped; smooth outlines need HD z-buffer twins too.
2. Floors (`floors.iff`): upscale with neighbouring tiles as context so tiles stay seamless. (The white dashes at the cuts between an object's sprites were the composite fading soft edges into empty texels, fixed 2026-10-18: FINDINGS "รอยต่อระหว่างชิ้น".) Base-game fences are in `GameData\walls.iff` (old SPR# sprites, not handled yet) with the walls.
3. Catalogue pictures: 8 stay as the game's: system objects that cannot be built; the Lot 99 bean pile, the Pegasus feather (blurry) and the beer (bottle colour unknown) are rejected. Spell effects (`Magic - Spell - …`, Wand Sparkle) are installed as assembled at the user's request although their colours differ. No PALT of the file fixes the colours: Maxis drew those pictures differently. They are probably never shown in game anyway (no Buy category, no inventory). Effects that the colour fit hides are placed by `box` with every dynamic sprite on (overrides). Pictures the user confirmed are kept in `work\catalog\reviewed\` (`review.py --confirm <page> --picked <copied lines>`) and left off later pages. The user may still review the ~40% of installed ones not yet looked at (`py catalog\review.py --game <dir>`); their picks go into `work\catalog\*.txt|tsv`, then `py catalog\build.py --game <dir> --install`. Open question: the eventual patcher needs those lists in the repo (they hold object labels and picture hashes, not images).
4. HD icons: dump uploads (`VKPROXY_DUMP=1`), upscale, replace by content hash (same mechanism as sprites).
5. Untested: Steam Launch Options, `-maxaspect`, `-monitor`, `-skip_intro`, `-w`; other resolutions (1440p → s = 2, needs `--scale 2` atlases); Polish/Russian fonts (`Fonts\<lang>\`, not built yet).
6. If the memory-dump / xref analysis is needed again (e.g. after an EA update), move those scripts into `tools/` with tests; method is in FINDINGS "วิธีทำซ้ำ". The game has not been updated since 2025-04-04.

Install folder: `C:\Program Files (x86)\Steam\steamapps\common\The Sims Legacy Collection` (app 3314060). Backups of it and of Saved Games are in `backup\`.

## Conventions

- Python 3.9+, **standard library only** for `tools/`. Each tool is a CLI with a module docstring as usage.
- Every new parser gets a unittest using a synthetic file built in the test (no game data in tests).
- **Never commit game files or anything extracted from them.** `work/`, `extracted/`, `backup/` and game file extensions are git-ignored. The eventual product is a patcher run against the user's own install; it never redistributes EA assets.
- Treat extracted game data as untrusted input: `far.py` refuses path traversal in member names; keep that property in any new extractor.
- `docs/` (PLAN.md, FINDINGS.md) is written in Thai; code, comments and README in English.
- Dev machine: Windows, PowerShell, repo at `G:\work\sims1-legacy-hd-mod`.
