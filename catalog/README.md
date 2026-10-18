# catalog/: HD catalogue pictures

The Buy and Build mode lists show each object as a 74×37 picture (37×37 normal, then
37×37 "selected", brightened). Upscaling those small pictures directly only gives a
soft guess. These tools draw each picture again from the object's own sprites, the
installed HD ones, so the 222×111 result is as sharp as the object in the lot.

Needs numpy (the ai_upscale venv has it, and so may your system Python). Build the HD
object sprites first (`tools/hd_art.py ... --install`): the pictures are drawn from the
files in `<game>\vkproxy\replace`.

```powershell
$game = "C:\Program Files (x86)\Steam\steamapps\common\The Sims Legacy Collection"
py catalog\build.py --game $game --install            # about 1.5 minutes on 24 threads
py catalog\review.py --game $game                      # local page: game's picture vs HD
py catalog\review.py --game $game --pending            # only what is not installed
py catalog\build.py --game $game --labels "Child's Stepstool" --install   # one object again
```

## How a picture is made

1. **Assemble** (`assemble.py`): the object's sprites, placed and depth-tested the way the
   game draws them in a lot. The rules were taken from the running game's draw calls
   (vkproxy's world log); they are listed in the module docstring.
2. **Fit** (`fit.py`): every state (up to 32), view, mirror and dynamic-sprite setting is
   scaled and placed onto the game's picture; the best match by colour error wins.
   The game's pictures were made by Maxis from renders, not by the game, so a few are
   not reachable from the sprites at all (spell icons, magazines; slot contents such as
   food on a display).
3. **Draw** (`build.py`): the winning assembly again from the HD sprites, 3× larger, on
   both backgrounds.

A picture is installed when the 1× assembly matches the game's picture (colour error
≤ 12 of 255 on both halves, shapes overlap ≥ 85%), or when it was approved by eye.

## Hand-made lists (in `work/catalog/`)

| File | |
|---|---|
| `overrides.tsv` | per object label: state, dynamic sprites on/off, view, mirror, `center`; or `none` |
| `approved.txt` | pictures accepted by eye although they fail the checks |
| `rejected.txt` | pictures found wrong by eye: never installed |
| `installed.txt` | written by `--install`, so a later install can take pictures out again |

`review.py` lets you tick wrong pictures and add a note to each; the copy button puts the
ticked and noted ones on the clipboard. `states.py --pictures <file>` shows every view and
state of chosen objects to pick from; its copy button gives `overrides.tsv` lines.
The pages hold images made from your game files, so they stay in `work/`.
