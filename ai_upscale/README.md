# ai_upscale

The AI step of the HD-art pipeline (`tools/hd_art.py` runs it for you): upscale a folder
of PNGs with a super-resolution model. It is kept out of `tools/` because it needs
PyTorch; everything before and after it (reading game files, preparing images,
compositing the result into vkproxy textures) is standard-library Python in `tools/`.

## Setup (once)

```powershell
py -m venv work\ext\torch
work\ext\torch\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cu128
work\ext\torch\Scripts\python -m pip install -r ai_upscale\requirements.txt
```

Models are downloaded on first use into `ai_upscale/models/` (git-ignored) and checked
against the sha256 in `models.json`.

## Choosing a model

```powershell
work\ext\torch\Scripts\python ai_upscale\upscale.py --list
work\ext\torch\Scripts\python ai_upscale\upscale.py --model pbrify-v4 <in_dir> <out_dir>
work\ext\torch\Scripts\python ai_upscale\upscale.py --model D:\models\something.safetensors <in_dir> <out_dir>
```

Any model [spandrel](https://github.com/chaiNNer-org/spandrel) can load works. To make
one selectable by name, add it to `models.json` (file, url, sha256, licence). The
models were compared on Sims 1 objects in `docs/FINDINGS.md`; `ultrasharp-v2` is the
default. Mind each model's licence before sharing its output.

## Notes

- Real-ESRGAN's ncnn-vulkan build (2022) caused GPU resets (TDR) on an RTX 5090; the
  PyTorch/CUDA path here has none. Don't run upscaling while the game is running.
- Output is written at the model's native scale (4x for all listed models);
  `tools/hd_composite.py` area-averages it to the game's 3x.
- Runs are resumable: existing outputs are skipped.
