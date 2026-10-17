"""Upscale a folder of RGBA PNGs with an AI super-resolution model (PyTorch + spandrel).

This is the only step of the HD-art pipeline that needs third-party packages;
everything else is in tools/ (standard library). See README.md for setup.

Usage:
    <venv python> ai_upscale/upscale.py --model ultrasharp-v2 <in_dir> <out_dir>
    <venv python> ai_upscale/upscale.py --model path/to/any_model.safetensors <in_dir> <out_dir>
    <venv python> ai_upscale/upscale.py --list

--model takes a name from models.json (downloaded to --models-dir on first use
and checked against its sha256) or a path to any model file spandrel can load.
Outputs are written at the model's native scale; files that already exist in
out_dir are skipped, so an interrupted run can be resumed.

Only colour goes through the model (outputs are RGB): tools/hd_composite.py takes
the coverage from the original texture. Each image is mirrored by PAD pixels on
every side, so the model never sees a hard image edge, and to the model's size
requirements; images that end up the same size run as one batch. The padding
is cropped off.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAD = 8
BATCH_PIXELS = 512 * 512  # input pixels per GPU batch
CHUNK = 1024              # images loaded at a time


def load_registry() -> dict:
    return {k: v for k, v in json.loads((HERE / "models.json").read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def resolve_model(name: str, models_dir: Path) -> Path:
    p = Path(name)
    if p.suffix in (".pth", ".safetensors", ".pt", ".ckpt") and p.exists():
        return p
    reg = load_registry()
    if name not in reg:
        sys.exit(f"unknown model {name!r}; known: {', '.join(reg)} (or pass a model file path)")
    entry = reg[name]
    path = models_dir / entry["file"]
    if not path.exists():
        models_dir.mkdir(parents=True, exist_ok=True)
        print(f"downloading {entry['url']}")
        tmp = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(entry["url"], tmp)
        tmp.replace(path)
    if entry.get("sha256") and sha256(path) != entry["sha256"]:
        sys.exit(f"{path} does not match the sha256 in models.json; delete it and retry")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("in_dir", nargs="?", type=Path)
    ap.add_argument("out_dir", nargs="?", type=Path)
    ap.add_argument("--model", default="ultrasharp-v2")
    ap.add_argument("--models-dir", type=Path, default=HERE / "models")
    ap.add_argument("--list", action="store_true", help="list the models in models.json")
    args = ap.parse_args()
    if args.list:
        for name, e in load_registry().items():
            print(f"{name:15} {e['arch']:8} x{e['scale']}  {e['license']:20} {e['note']}")
        return 0
    if not args.in_dir or not args.out_dir:
        ap.error("in_dir and out_dir are required")

    import numpy as np
    import torch
    import torch.nn.functional as F
    from PIL import Image
    from spandrel import ModelLoader

    model_path = resolve_model(args.model, args.models_dir)
    desc = ModelLoader().load_from_file(str(model_path))
    dtype = torch.float16 if desc.supports_half else torch.bfloat16 if desc.supports_bfloat16 else torch.float32
    model = desc.model.cuda().eval().to(dtype)
    req, scale = desc.size_requirements, desc.scale
    print(f"{model_path.name}: {desc.architecture.name} x{scale} {dtype}")

    # Images are small (sprites are ~10-300 px), so one at a time leaves the GPU
    # idle: images whose padded size is the same (animation frames, rotations)
    # run as one batch, with file reading/writing on other threads. Sizes are
    # not rounded up to shared buckets: models such as DAT attend over the whole
    # image, so extra padding would change every output pixel.
    step = req.multiple_of or 1

    def bucket(h: int, w: int) -> tuple[int, int]:
        th = -(-max(h + 2 * PAD, req.minimum, 16) // step) * step
        tw = -(-max(w + 2 * PAD, req.minimum, 16) // step) * step
        return (max(th, tw),) * 2 if req.square else (th, tw)

    def load(p: Path):
        return np.asarray(Image.open(p).convert("RGB"))

    def save(p: Path, rgb) -> None:
        tmp = args.out_dir / (p.stem + ".part.png")
        Image.fromarray(rgb, "RGB").save(tmp, compress_level=1)
        tmp.replace(args.out_dir / p.name)  # whole files only, so a resumed run never sees half an image

    @torch.inference_mode()
    def infer(x):
        nonlocal model, dtype
        try:
            return model(x.to(dtype))
        except RuntimeError:  # some architectures have ops without bf16 kernels
            model, dtype = model.float(), torch.float32
            print("  falling back to float32")
            return model(x.float())

    @torch.inference_mode()
    def run_batch(items, th: int, tw: int, io, pending) -> None:
        xs = []
        for _, im in items:
            h, w = im.shape[:2]
            t = torch.from_numpy(im).cuda().permute(2, 0, 1)[None].float() / 255
            t = F.pad(t, (PAD,) * 4, mode="reflect" if min(h, w) > PAD else "replicate")  # reflect needs PAD < size
            xs.append(F.pad(t, (0, tw - t.shape[3], 0, th - t.shape[2]), mode="replicate"))
        y = (infer(torch.cat(xs)).float().clamp(0, 1) * 255 + 0.5).to(torch.uint8)
        for (p, im), out in zip(items, y):
            h, w = im.shape[:2]
            crop = out[:, PAD * scale:(PAD + h) * scale, PAD * scale:(PAD + w) * scale].permute(1, 2, 0)
            pending.append(io.submit(save, p, crop.contiguous().cpu().numpy()))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    todo = [p for p in sorted(args.in_dir.glob("*.png")) if not (args.out_dir / p.name).exists()]
    print(f"{len(todo)} images to upscale (colour only: the composite step keeps the original alpha)")
    t0, done = time.time(), 0
    with ThreadPoolExecutor(max_workers=8) as io:
        for start in range(0, len(todo), CHUNK):
            chunk = todo[start:start + CHUNK]
            groups = defaultdict(list)
            for p, im in zip(chunk, io.map(load, chunk)):
                groups[bucket(*im.shape[:2])].append((p, im))
            pending: list = []
            for (th, tw), items in sorted(groups.items()):
                per_batch = max(1, BATCH_PIXELS // (th * tw))
                for k in range(0, len(items), per_batch):
                    run_batch(items[k:k + per_batch], th, tw, io, pending)
            for f in pending:
                f.result()
            done += len(chunk)
            el = time.time() - t0
            print(f"  {done}/{len(todo)}  {el:.0f} s, ~{el / done * (len(todo) - done) / 60:.0f} min left", flush=True)
    print(f"done {len(todo)} in {time.time() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
