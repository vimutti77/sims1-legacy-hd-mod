# ai_upscale

The AI step of the HD-art pipeline (`tools/hd_art.py` runs it for you): upscale a folder
of PNGs with a super-resolution model and, with `--composite`, turn the result into
vkproxy textures on the GPU (`gpu_composite.py`, the same rules as
`tools/hd_composite.py`, which stays the tested reference). It is kept out of `tools/`
because it needs PyTorch; everything before it (reading game files, preparing images)
is standard-library Python in `tools/`.

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
- Without `--composite`, output is written at the model's native scale (4x for all
  listed models) as PNG; `tools/hd_composite.py composite` area-averages it to the
  game's 3x. Use this to try other composite rules without re-running the model.
- Runs are resumable: existing outputs are skipped.
- Speed: sprites are small, and DAT issues ~3,300 GPU kernels per batch, so on a fast
  GPU one process is limited by the CPU launching them (~70 ms vs ~20 ms of GPU work).
  `--workers N` (default 4) runs N processes on one GPU; each reserves 5–8 GB of
  Windows commit charge, so fewer run when memory is short.
- `keep_masks_on_gpu` works around window-attention models (DAT, SwinIR, ...) that
  rebuild their attention masks on the CPU every batch and wait for the GPU to copy them.
- Inputs go to the model as contiguous NCHW: a channels-last view runs slower and
  changes the output.
- Test the GPU composite against the reference (needs the venv):
  `work\ext\torch\Scripts\python -m unittest tests.test_gpu_composite`
