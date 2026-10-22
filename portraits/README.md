# portraits

HD portraits of the Sims: the heads on the control panel buttons, in the relationship panel,
in speech balloons and in the family lists, drawn again three times larger.

The game does not draw these while playing. Each character file
(`UserData*\Characters\User*.iff` in the saves) stores them as `BMP_` chunks 2002–2007,
drawn when the Sim was made, and Legacy uploads them truncated to RGB565 like any UI
bitmap, so vkproxy can swap each for an HD picture by the hash of its pixels.

`redraw.py` reads the Sim's head (STR# 200 names the head skins and texture), finds the pose
each stored picture was drawn with (`pose.py`: head drawn at 1x and matched to the picture),
and draws it again at 3x with the HD head texture made by `tools/hd_skins.py`
(`head.py`). Frames and backgrounds are taken from the picture.

```powershell
py portraits\redraw.py --game "<install dir>" --install
```

Sims made later (in Create-a-Sim, townies and NPCs as the game makes them) get pictures
the game draws with one camera per picture. `library.py` draws every head the game has, with
each of its skin textures, into the six pictures ahead (690 heads, about a minute on 24
cores, 434 MB): vkproxy matches a new picture with the closest at 1x and shows its 3x one.

```powershell
py portraits\library.py --game "<install dir>" --install
```

Players get the files these make (`redraw.py`'s for the ready-made Sims, `library.py`'s for
the rest); they run neither. Pets are done too: a cat's or dog's head is the skins on the
head bone in its body suit, drawn nearer (`PET_POSES`). Needs numpy. Findings:
`docs/FINDINGS.md`, "ภาพหัวในแถบควบคุม".
