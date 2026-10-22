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

Run it again after making or changing Sims: their pictures are new. Pets (skeletons `kat`,
`dog`) are not done yet: their head is part of the body mesh, which needs the skeleton.
Needs numpy. Findings: `docs/FINDINGS.md`, "ภาพหัวในแถบควบคุม".
