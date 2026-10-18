"""Upscale a folder of RGBA PNGs with an AI super-resolution model (PyTorch + spandrel).

This is the only step of the HD-art pipeline that needs third-party packages;
everything else is in tools/ (standard library). See README.md for setup.

Usage:
    <venv python> ai_upscale/upscale.py --model ultrasharp-v2 <in_dir> <out_dir>
    <venv python> ai_upscale/upscale.py --model path/to/any_model.safetensors <in_dir> <out_dir>
    <venv python> ai_upscale/upscale.py --model ultrasharp-v2 --composite <orig_dir> <in_dir> <out_dir>
    <venv python> ai_upscale/upscale.py --list

--model takes a name from models.json (downloaded to --models-dir on first use
and checked against its sha256) or a path to any model file spandrel can load.
Outputs are <out_dir>/<name>.png at the model's native scale, or with
--composite <orig_dir> the finished vkproxy texture <out_dir>/<name>@3.bgra,
composited on the GPU with the original <orig_dir>/<name>.bgra (gpu_composite.py).
--names <file> limits the run to the names listed in the file. Outputs that
already exist are skipped, so an interrupted run can be resumed.

Only colour goes through the model (outputs are RGB): the composite step takes
the coverage from the original texture. Each image is mirrored by PAD pixels on
every side, so the model never sees a hard image edge, and to the model's size
requirements; images that end up the same size run as one batch. The padding
is cropped off. --workers processes share the GPU: sprites are small, so one
process spends most of its time launching kernels rather than computing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAD = 8
BATCH_PIXELS = 512 * 512  # input pixels per GPU batch
BATCH_COST = 20_000       # per-batch overhead in input pixels, for balancing workers
LOOKAHEAD = 8             # batches loaded ahead of the GPU
REPORT = 2000             # images between progress lines
WORKER_COMMIT = 8 << 30   # memory Windows commits per worker process (measured: 5-8 GB)
RESERVE_COMMIT = 8 << 30  # commit left for the rest of the system


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


def png_size(p: Path) -> tuple[int, int]:
    """(height, width) from the PNG header."""
    with p.open("rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise ValueError(f"{p.name}: not a PNG")
    w, h = struct.unpack(">II", head[16:24])
    return h, w


def keep_masks_on_gpu(model) -> None:
    """Window-attention models (DAT, SwinIR, HAT, DRCT, ...) rebuild their shifted-window
    masks on the CPU in every forward for any size but their training size, and copy
    them to the GPU, which makes the CPU wait for the GPU each time. Build each mask
    once per size and upload it without waiting instead."""
    for m in model.modules():
        build = getattr(m, "calculate_mask", None)
        if not callable(build):
            continue
        last: dict = {}  # one size at a time: batches come sorted by size

        def cached(*a, build=build, last=last, **kw):
            key = repr((a, kw))
            if key not in last:
                masks = build(*a, **kw)
                last.clear()
                last[key] = (tuple(t.pin_memory().cuda(non_blocking=True) for t in masks)
                             if isinstance(masks, tuple) else masks.pin_memory().cuda(non_blocking=True))
            return last[key]

        m.calculate_mask = cached


def affordable_workers(wanted: int) -> int:
    """As many workers as the free commit charge (RAM + page file) can hold: on Windows
    each CUDA process commits 5-8 GB (CUDA libraries and kernels, mostly never touched),
    and when the commit runs out every worker dies."""
    if sys.platform != "win32" or wanted <= 1:
        return wanted
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
            (n, ctypes.c_uint64) for n in ("total_phys", "avail_phys", "total_commit", "avail_commit",
                                           "total_virtual", "avail_virtual", "avail_extended")]

    s = MemoryStatus()
    s.length = ctypes.sizeof(s)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
        return wanted
    return max(1, min(wanted, (s.avail_commit - RESERVE_COMMIT) // WORKER_COMMIT))


def run_workers(args, model_path: Path, groups: dict) -> int:
    """Split whole size groups over --workers child processes, balanced by cost."""
    n = min(args.workers, len(groups))
    shards: list[list[Path]] = [[] for _ in range(n)]
    load = [0] * n
    for (th, tw), files in sorted(groups.items(), key=lambda kv: -kv[0][0] * kv[0][1] * len(kv[1])):
        i = load.index(min(load))
        per_batch = max(1, BATCH_PIXELS // (th * tw))
        load[i] += th * tw * len(files) + BATCH_COST * -(-len(files) // per_batch)
        shards[i] += files
    tmp = Path(tempfile.mkdtemp(prefix="upscale_"))
    try:
        procs = []
        for i, files in enumerate(shards):
            names = tmp / f"w{i}.txt"
            names.write_text("\n".join(p.stem for p in files) + "\n", encoding="utf-8")
            cmd = [sys.executable, "-W", "ignore", __file__, str(args.in_dir), str(args.out_dir),
                   "--model", str(model_path), "--names", str(names), "--workers", "1", "--tag", f"w{i}"]
            if args.composite:
                cmd += ["--composite", str(args.composite)]
            procs.append(subprocess.Popen(cmd))
        return max(p.wait() for p in procs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("in_dir", nargs="?", type=Path)
    ap.add_argument("out_dir", nargs="?", type=Path)
    ap.add_argument("--model", default="ultrasharp-v2")
    ap.add_argument("--models-dir", type=Path, default=HERE / "models")
    ap.add_argument("--composite", type=Path, metavar="ORIG_DIR",
                    help="write finished <name>@3.bgra textures, composited with ORIG_DIR/<name>.bgra")
    ap.add_argument("--names", type=Path, help="file listing the names (without .png) to process")
    ap.add_argument("--workers", type=int, default=4,
                    help="processes sharing the GPU (fewer if memory is short)")
    ap.add_argument("--tag", default="", help=argparse.SUPPRESS)
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
    from PIL import Image
    from spandrel import ModelLoader

    import gpu_composite

    model_path = resolve_model(args.model, args.models_dir)
    desc = ModelLoader().load_from_file(str(model_path))
    req, scale = desc.size_requirements, desc.scale
    if args.composite and scale < gpu_composite.SCALE:
        sys.exit(f"{model_path.name} is x{scale}; --composite needs x{gpu_composite.SCALE} or more")
    tag = f"{args.tag}: " if args.tag else ""

    # Images are small (sprites are ~10-300 px), so one at a time leaves the GPU
    # idle: images whose padded size is the same (animation frames, rotations)
    # run as one batch. Sizes are not rounded up to shared buckets: models such
    # as DAT attend over the whole image, so extra padding would change every
    # output pixel.
    step = req.multiple_of or 1

    def bucket(h: int, w: int) -> tuple[int, int]:
        th = -(-max(h + 2 * PAD, req.minimum, 16) // step) * step
        tw = -(-max(w + 2 * PAD, req.minimum, 16) // step) * step
        return (max(th, tw),) * 2 if req.square else (th, tw)

    def target(p: Path) -> Path:
        return args.out_dir / (f"{p.stem}@{gpu_composite.SCALE}.bgra" if args.composite else p.name)

    wanted = set(args.names.read_text(encoding="utf-8").split()) if args.names else None
    todo = [p for p in sorted(args.in_dir.glob("*.png"))
            if (wanted is None or p.stem in wanted) and not target(p).exists()]
    groups: dict[tuple[int, int], list[Path]] = defaultdict(list)
    for p in todo:
        groups[bucket(*png_size(p))].append(p)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if not args.tag:
        print(f"{len(todo)} images to upscale (colour only: the composite step keeps the original alpha)")
    workers = min(affordable_workers(args.workers), len(groups))
    if workers < min(args.workers, len(groups)):
        print(f"free memory (commit charge) allows {workers} of {args.workers} workers")
    if workers > 1:
        args.workers = workers
        t0 = time.time()
        code = run_workers(args, model_path, groups)
        print(f"done {len(todo)} in {time.time() - t0:.0f} s ({workers} workers)")
        return code

    dtype = torch.float16 if desc.supports_half else torch.bfloat16 if desc.supports_bfloat16 else torch.float32
    model = desc.model.cuda().eval().to(dtype)
    keep_masks_on_gpu(model)
    if not args.tag:
        print(f"{model_path.name}: {desc.architecture.name} x{scale} {dtype}")

    # The GPU must never wait for the CPU: images are read and padded on I/O
    # threads into pinned memory and uploaded without blocking; results are
    # copied back (which waits for the GPU) on the I/O threads too.
    def load_batch(files: list[Path], th: int, tw: int):
        sizes, ims, origs = [], [], []
        for p in files:
            im = np.asarray(Image.open(p).convert("RGB"))
            h, w = im.shape[:2]
            sizes.append((p, h, w))
            im = np.pad(im, ((PAD, PAD), (PAD, PAD), (0, 0)), mode="reflect" if min(h, w) > PAD else "edge")
            ims.append(np.pad(im, ((0, th - im.shape[0]), (0, tw - im.shape[1]), (0, 0)), mode="edge"))
            if args.composite:
                data = (args.composite / f"{p.stem}.bgra").read_bytes()
                if len(data) != w * h * 4:
                    raise ValueError(f"{p.stem}: the original is not {w}x{h} BGRA")
                origs.append(torch.frombuffer(bytearray(data), dtype=torch.uint8).view(h, w, 4).pin_memory())
            else:
                origs.append(None)
        return sizes, torch.from_numpy(np.stack(ims)).pin_memory(), origs

    def save(path: Path, t) -> None:
        a = t.cpu().numpy()
        tmp = path.with_name(path.name + ".part")
        if args.composite:
            tmp.write_bytes(a.tobytes())
        else:
            Image.fromarray(a, "RGB").save(tmp, format="PNG", compress_level=1)
        tmp.replace(path)  # whole files only, so a resumed run never sees half an image

    @torch.inference_mode()
    def infer(x):
        nonlocal model, dtype
        try:
            return model(x.to(dtype))
        except RuntimeError:  # some architectures have ops without bf16 kernels
            model, dtype = model.float(), torch.float32
            print(f"{tag}falling back to float32")
            return model(x.float())

    @torch.inference_mode()
    def run_batch(loaded, io, pending: list) -> None:
        sizes, batch, origs = loaded
        x = batch.cuda(non_blocking=True).permute(0, 3, 1, 2).contiguous().float() / 255  # NCHW like the model
        y = (infer(x).float().clamp(0, 1) * 255 + 0.5).floor()
        for (p, h, w), orig, out in zip(sizes, origs, y):
            crop = out[:, PAD * scale:(PAD + h) * scale, PAD * scale:(PAD + w) * scale]
            if args.composite:
                result = gpu_composite.composite(orig.cuda(non_blocking=True), crop)
            else:
                result = crop.permute(1, 2, 0).to(torch.uint8).contiguous()
            pending.append(io.submit(save, target(p), result))

    batches = []
    for (th, tw), files in sorted(groups.items()):
        per_batch = max(1, BATCH_PIXELS // (th * tw))
        batches += [(files[k:k + per_batch], th, tw) for k in range(0, len(files), per_batch)]
    t0, done, reported = time.time(), 0, 0
    with ThreadPoolExecutor(max_workers=8) as io:
        ahead: deque = deque()
        queue = iter(batches)

        def fetch() -> None:
            b = next(queue, None)
            if b:
                ahead.append((len(b[0]), io.submit(load_batch, *b)))

        for _ in range(LOOKAHEAD):
            fetch()
        pending: list = []
        while ahead:
            n, loaded = ahead.popleft()
            fetch()
            run_batch(loaded.result(), io, pending)
            done += n
            if done - reported >= REPORT or not ahead:
                reported = done
                still = []
                for f in pending:
                    if f.done():
                        f.result()  # raise any write error
                    else:
                        still.append(f)
                pending[:] = still
                el = time.time() - t0
                print(f"  {tag}{done}/{len(todo)}  {el:.0f} s, ~{el / done * (len(todo) - done) / 60:.0f} min left",
                      flush=True)
        for f in pending:
            f.result()
    if not args.tag:
        print(f"done {len(todo)} in {time.time() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
