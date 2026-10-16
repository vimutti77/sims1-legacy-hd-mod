# CLAUDE.md

Handoff context for **sims1-legacy-hd-mod**. Read `docs/PLAN.md` for the full plan.

## Goal

Make **The Sims™ Legacy Collection** (The Sims 1, 2025 re-release, Steam) look sharp on high-res displays (1440p, 4K) without lowering the desktop resolution. Order of work: **UI and text first** (phases 0–3), in-lot graphics later (phase 4).

## Current state

- **Phase 0 (feasibility spike) has not started.** No game files have been inspected yet.
- `tools/` are written and pass `python -m unittest discover -s tests` (8 tests, synthetic files only). **They have never been run against real game data.** Expect format surprises; FAR/IFF parsers follow community docs (FreeSO), not verified on Legacy files.
- `launcher/` (SimsLauncher.ps1 + Sims4K.bat) switches the display to 1920×1080 via Win32 `ChangeDisplaySettings` (CDS_FULLSCREEN, temporary), launches Sims.exe, waits for exit, restores. Written on Linux, **not yet tested on Windows**. This is the fallback if the mod is a no-go.

## Key facts gathered so far

- Legacy has no in-game resolution option for Sims 1; it renders at desktop resolution, borderless. Steam launch options `-w -r…` are ignored by Sims 1 Legacy (they work for Sims 2 Legacy).
- On 4K desktops the Legacy UI looks oversized and pixelated; it looks best at a 1920×1080 desktop. Community workaround = switch desktop resolution before launch (QRes/NirCmd scripts, "TSLC Fixed UI Launcher" by Anarkkie).
- Neighborhood map screen is pillarboxed 4:3 with a blue frame by design; lots render widescreen.
- FaithBeam's Sims 1 Widescreen Patcher does **not** accept the Legacy exe.
- sims2-4k-ui-patch (lah7) is the reference project; it does not support Sims 2 Legacy because Legacy auto-scales UI and the mod needs UI drawn at 1:1. Sims 2 Legacy exposes `uiScaleFactor` (cheat `uintProp uiScaleFactor 1|2|3`, and `Graphics Rules.sgr`). **Unknown whether Sims 1 Legacy has an equivalent — this is the first thing to look for in `strings.md`.**
- Sims themselves are real-time 3D meshes with bitmap textures; objects/walls/floors are pre-rendered SPR2 sprites (color + alpha + z-buffer, 3 zoom levels). (From original-game knowledge; confirm in phase 0.)

## The main blocker (H4)

Can the game be forced to draw the UI at 1:1 on a 4K desktop? If not (config, registry, command line, or a single findable exe patch), stop and keep the launcher. See hypotheses H1–H4 and go/no-go in `docs/PLAN.md`.

## Next steps

1. Back up the game folder.
2. Run, with `$game` = the Steam install folder (likely `C:\Program Files (x86)\Steam\steamapps\common\The Sims Legacy Collection`, unconfirmed):
   ```powershell
   python tools\inventory.py   $game            -o work\inventory.md
   python tools\exe_strings.py "$game\Sims.exe" -o work\strings.md
   ```
3. In `strings.md` look for scale/resolution config keys, cheats, switches. Also run `exe_strings.py` on any new DLLs that `inventory.md` lists.
4. Locate UI layout + bitmaps; try the decisive H3 test (move one button / recolor one bitmap).
5. Record results in `docs/FINDINGS.md` (create it).

## Conventions

- Python 3.9+, **standard library only** for `tools/`. Each tool is a CLI with a module docstring as usage.
- Every new parser gets a unittest using a synthetic file built in the test (no game data in tests).
- **Never commit game files or anything extracted from them.** `work/`, `extracted/`, `backup/` and game file extensions are git-ignored. The eventual product is a patcher run against the user's own install; it never redistributes EA assets.
- Treat extracted game data as untrusted input: `far.py` refuses path traversal in member names; keep that property in any new extractor.
- `docs/PLAN.md` is written in Thai; code, comments and README in English.
- Dev machine: Windows, PowerShell, repo at `G:\work\sims1-legacy-hd-mod`.
