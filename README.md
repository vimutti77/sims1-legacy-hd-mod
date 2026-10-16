# sims1-legacy-hd-mod

Goal: a crisp, native-resolution UI for **The Sims™ Legacy Collection** (The Sims 1, 2025 re-release) on high-resolution displays (1440p, 4K).

**Status: Phase 0 — feasibility spike.** Nothing here modifies the game yet.

## Why this is hard

The Legacy build auto-scales its 800×600-era UI to the desktop resolution. A 4K UI mod (upscaled assets + doubled layout coordinates) only works if the game can be forced to draw the UI at 1:1. The same blocker stopped [sims2-4k-ui-patch](https://github.com/lah7/sims2-4k-ui-patch) from supporting the Sims 2 Legacy release. Phase 0 exists to find out whether that switch exists for Sims 1 before investing more.

Order of work: **UI and text first** (phases 0–3), then in-lot graphics (phase 4). Sims themselves are real-time 3D with bitmap textures; objects, walls and floors are pre-rendered SPR2 sprites with color, alpha and z-buffer channels, which makes them the harder part.

## Layout

```
docs/PLAN.md          phases, go/no-go criteria, spike checklist
tools/inventory.py    walk the game folder, identify formats by magic bytes
tools/exe_strings.py  find scaling/resolution strings in Sims.exe
tools/far.py          list/extract FAR archives (1a/1b)
tools/iff.py          list/dump IFF 2.5 chunks (BMP_, SPR#, STR#, ...)
launcher/             interim fix: run the game at 1080p, restore 4K on exit
tests/                unit tests on synthetic files (no game data)
```

Python 3.9+ standard library only.

## Phase 0 quick start (Windows)

```powershell
$game = "C:\Program Files (x86)\Steam\steamapps\common\The Sims Legacy Collection"
python tools\inventory.py   $game          -o work\inventory.md
python tools\exe_strings.py "$game\Sims.exe" -o work\strings.md
```

Then follow the checklist in [docs/PLAN.md](docs/PLAN.md).

## Ground rules

- **Never commit game files.** Extracted assets, `.far`, `.iff`, `.exe` and anything under `work/` are git-ignored.
- The eventual tool patches the user's own install, like sims2-4k-ui-patch; it does not redistribute EA assets.
- Always back up before patching. Steam "Verify integrity of game files" restores originals.

## Tests

```
python -m unittest discover -s tests
```
