# sims1-legacy-hd-mod

Sharp graphics for **The Sims™ Legacy Collection** (The Sims 1, 2025 re-release, Steam)
on 1440p and 4K displays, without lowering the desktop resolution.

Out of the box the game draws its 800×600-era UI at low resolution and enlarges it
(3× on a 2160p screen), and every object in a lot is a small bitmap enlarged the same way.
This mod:

- **draws the UI at screen resolution**: every UI window is rendered at the display's
  real pixel count instead of being enlarged afterwards, so layout and clicks stay exactly
  as the game made them;
- **replaces the bitmap fonts** with HD ones redrawn from a vector font;
- **smooths the remaining bitmaps** (icons, buttons) with xBR instead of blocky
  nearest-neighbour enlargement;
- **replaces object sprites** (furniture, plants, doors, …) with versions upscaled by an
  AI model, made from your own game files.

It works by a drop-in `vulkan-1.dll` placed next to `Sims.exe` (`vkproxy/`). No game file
is modified, and the mod never ships EA assets: fonts and sprites are generated on your
machine from your own install.

**Status:** in development; used daily on one 4K setup (RTX 5090). Floors, walls, icons
and the Sims themselves are not HD yet. See [docs/FINDINGS.md](docs/FINDINGS.md) (Thai) for
everything learnt so far.

## Requirements

- The Sims Legacy Collection (Steam). In game: Options → Graphics → Resolution = your
  monitor's native resolution (e.g. `3840x2160 (16:9)`).
- Windows, Python 3.9+ (the `py` launcher).
- To build the DLL: [Vulkan SDK](https://vulkan.lunarg.com/) headers and llvm-mingw
  (`i686-w64-mingw32-clang++`).
- For HD sprites: an NVIDIA GPU with CUDA and a PyTorch venv (see
  [ai_upscale/README.md](ai_upscale/README.md)).

## Install

Back up the game folder first, or use Steam's "Verify integrity of game files" to undo.

```powershell
$game = "C:\Program Files (x86)\Steam\steamapps\common\The Sims Legacy Collection"

# 1. the DLL
powershell -File vkproxy\build.ps1
copy vkproxy\build\vulkan-1.dll $game\

# 2. HD fonts (about a minute)
py tools\font_atlas.py $game "$game\vkproxy\replace"

# 3. HD object sprites, one part at a time (optional; needs the ai_upscale venv)
py tools\hd_art.py --game $game --list                       # parts and their size
py tools\hd_art.py --game $game --category seating --install --clean
py tools\hd_art.py --game $game --pack ep1 --category all --install --clean
```

Start the game from Steam as usual. To uninstall, delete `vulkan-1.dll` and the
`vkproxy` folder from the game folder.

`hd_art.py` resumes where it stopped and skips parts already done. The whole game
(base, ep1–ep7, deluxe, shared) is about 86,000 sprites: roughly 2 hours on an RTX 5090
and 11.5 GB in the game folder. The work folder (`work\hd_art`) holds another copy of the
HD files plus the originals (about 13 GB); `--clean` deletes the AI outputs, the largest
intermediates, as each part finishes. Choose another model with `--model`
(`py ai_upscale\upscale.py --list`).

The HD files are not distributed: they are made from EA's art (and the fonts from
Comic Sans MS), each AI model has its own licence, and they are named after the exact
pixels of your install, so building them on your machine is the only way that is both
allowed and guaranteed to match.

## Keys in game

| Key | |
|---|---|
| F9 | HD sprites on/off (compare with the original) |
| F10 | xBR on/off |
| F11 | log the next frame's draw calls (`%TEMP%\vkproxy\vkproxy.log`) |

## Known limits

- Object outlines keep the original stair-step edge: the game reads each pixel's depth
  from a low-resolution z-buffer, so a sprite must not grow past its original shape.
- Floors, walls, wallpapers, UI icons and Sims (3D models) are not HD yet.
- Catalogue thumbnails are small (37×37) and stay soft.
- Some AI models add detail of their own (e.g. stained glass); the model can be changed.

## Repository layout

```
vkproxy/          the drop-in vulkan-1.dll (C++): UI at screen resolution, xBR,
                  texture replacement by size (fonts) and by content hash (sprites)
tools/            standard-library Python:
  font_atlas.py     HD font atlases from the game's .ffn fonts
  spr2.py           decode SPR2 sprites exactly as the game uploads them
  hd_composite.py   prepare images for the upscaler, composite its output back
  hd_art.py         the whole HD-sprite pipeline for one part, plus install
  far.py, iff.py, ffn.py, inventory.py, exe_strings.py   file-format readers
ai_upscale/       the AI step (PyTorch + spandrel), model list with licences
launcher/         old interim fix (switch desktop to 1080p); superseded
docs/             PLAN.md and FINDINGS.md (Thai)
tests/            unit tests on synthetic files (no game data)
```

## Ground rules

- **Never commit game files** or anything made from them (fonts, sprites, dumps):
  `work/` and game file extensions are git-ignored.
- Tools treat game files as untrusted input (bounds-checked parsers, no path traversal).
- Mind each AI model's licence before sharing its output (`ai_upscale/models.json`).

## Tests

```
py -m unittest discover -s tests
```
