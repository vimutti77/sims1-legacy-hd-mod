"""Build HD wallpapers for vkproxy from the user's own game files.

An in-lot wall is drawn from a wallpaper frame (SPR#, tools/spr1.py): the left or right face of
one wall segment, at each zoom. Legacy uploads it with the frame's empty corners filled (the
wall top in one colour, the bottom row repeated down), so vkproxy recognises a wall by the
pixels the frame covers alone: <w>x<h>.wallmasks lists the coverage masks of each frame size
(most used first) and <w>x<h>_<hash of the covered pixels>_w<mask>@3.bgra is the HD wallpaper,
drawn over the game's own corners. See vkproxy/README.md.

Wall segments meet edge to edge along the wall, the next one (w, -w/2) or (w, w/2) away, so
the upscaler gets each frame between copies of itself at those places and the middle is cut
out of its result: the pattern runs on across the seams instead of meeting a mirror image.

Wall tops (the strip drawn along the top of a wall, 46 shapes per zoom in walls.iff) are
uploaded as they are and named like sprites; their HD twins get the upscaler's colour and an
outline straightened and anti-aliased by tools/stairs.py, as vkproxy does for the wall faces'
coverage masks, so the two still meet.

Steps (each skips work already done):
  extract    wallpaper frames 0 and 1 of walls.iff (palette 1536+N) and the .wll files
             -> <work>/orig/<name>.bgra, the masks -> <work>/<w>x<h>.wallmasks; the wall tops
  prep       the frame between its neighbours along the wall -> <work>/in/<name>.png
  upscale    ai_upscale/upscale.py (needs the venv and the GPU: close the game)
  composite  the middle of the result, coverage from the frame -> <work>/hd/<name>@3.bgra
  install    hd/ and the masks into <game>/vkproxy/replace (--install)

Usage:
    py tools/hd_walls.py --game <dir> [--work work/hd_walls] [--model ultrasharp-v2] [--install]
"""
from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import far
import hd_composite
import hd_pack
import iff
import spr1
import spr2
import stairs

REPO = Path(__file__).resolve().parents[1]
FACES = (0, 1)  # frames 0 and 1 of a wallpaper chunk: the two wall faces
FNV_BASIS = 1469598103934665603  # vkproxy's, as spr2.content_name


def wall_files(game: Path):
    """(file name, bytes) of the base game's walls.iff and every .wll file."""
    yield "walls.iff", (game / "GameData" / "walls.iff").read_bytes()
    for p in sorted((game / "GameData" / "Walls").glob("*.wll")):
        yield p.name, p.read_bytes()
    for p in sorted(game.glob("*/*.far")):
        data = p.read_bytes()
        if data[:8] != far.MAGIC:
            continue
        for e in far.read_far(data):
            if e.name.lower().endswith(".wll"):
                yield f"{p.name}/{e.name}", data[e.offset:e.offset + e.size]


def covered_hash(w: int, h: int, bgra: bytes, mask: bytes) -> str:
    """16 hex digits of FNV-1a 64 (vkproxy's basis) over the BGRA of the pixels in mask."""
    v = FNV_BASIS
    for i in range(w * h):
        if mask[i]:
            for byte in bgra[i * 4:i * 4 + 4]:
                v = ((v ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{v:016x}"


def wallpaper_frames(game: Path):
    """(w, h, texture, mask) of every wallpaper face frame in the game."""
    for fname, data in wall_files(game):
        try:
            for _c, i, f, pal in spr1.frames(fname, data):
                if pal is not None and i in FACES and f.w > 1 and f.h > 1:
                    yield f.w, f.h, spr1.game_texture(f, pal), bytes(p is not None for p in f.pixels)
        except iff.IffError:
            continue


def top_frames(game: Path):
    """(w, h, texture, mask) of every wall-top frame: the unlabelled SPR# chunks of walls.iff
    that name a palette (46 shapes per zoom: straight pieces, corners, junctions). Uploaded as
    they are, so they are named by their whole texture like sprites."""
    data = (game / "GameData" / "walls.iff").read_bytes()
    pals = spr2.read_palettes(data, iff.read_iff(data))
    for c, _i, f, _pal in spr1.frames("walls.iff", data):
        if c.label:
            continue
        palette_id, _offsets = spr1.frame_offsets(data[c.offset:c.offset + c.size])
        pal = pals.get(palette_id)
        if pal is not None and f.w > 1 and f.h > 1:
            yield f.w, f.h, spr1.game_texture(f, pal), bytes(p is not None for p in f.pixels)


def composite_top_one(job) -> None:
    """A wall top: the upscaler's colour as for sprites, its outline straightened and anti-aliased
    (tools/stairs.py) instead of the 1x coverage: wall tops are drawn by the floor shader, which
    has no 1x depth texture to keep to, and the wall faces they sit on get the same edges."""
    orig, ai_png, out = job
    w, h = hd_composite.size_of(orig.stem)
    W, H, rgba = hd_composite.read_png(ai_png)
    ai3 = rgba if W == w * 3 else hd_composite.shrink(W, H, rgba, w * 3, h * 3)
    tex = orig.read_bytes()
    base = hd_composite.composite(w, h, tex, ai3)  # premultiplied, the 1x coverage
    cov = stairs.upscale(bytes(1 if tex[i * 4 + 3] else 0 for i in range(w * h)), w, h)
    W, H = w * 3, h * 3
    px = bytearray(len(base))
    for p, a in enumerate(cov):
        if not a:
            continue
        o = p * 4
        if base[o + 3]:  # inside the 1x coverage: the composite's colour (opaque there)
            c = base[o:o + 3]
        else:  # an edge pixel the straight line adds: the colour of the nearest one inside (the
            # upscaler's own colour differs a little, and alternating with the composite's along
            # the stairs it showed as a dashed line)
            c = nearest_colour(base, W, H, p % W, p // W)
        # inside the game's coverage odd, added even: vkproxy's floor shader pushes the added ones
        # a little back (world_floor.frag), or they would win over objects standing at the wall
        a = a | 1 if tex[((p // W // 3) * w + p % W // 3) * 4 + 3] else max(2, a & ~1)
        px[o:o + 4] = bytes(((c[0] * a + 127) // 255, (c[1] * a + 127) // 255, (c[2] * a + 127) // 255, a))
    out.write_bytes(bytes(px))


def nearest_colour(px: bytes, W: int, H: int, x: int, y: int, reach: int = 4) -> bytes:
    """The BGR of the nearest pixel of px (premultiplied BGRA, opaque where drawn) that is drawn,
    by rings around (x, y); black when none is within reach."""
    for r in range(1, reach + 1):
        best = None
        for yy in range(max(0, y - r), min(H, y + r + 1)):
            for xx in range(max(0, x - r), min(W, x + r + 1)):
                if max(abs(xx - x), abs(yy - y)) != r or not px[(yy * W + xx) * 4 + 3]:
                    continue
                d = (xx - x) ** 2 + (yy - y) ** 2
                if best is None or d < best[0]:
                    best = (d, (yy * W + xx) * 4)
        if best:
            return bytes(px[best[1]:best[1] + 3])
    return b"\0\0\0"


def write_masks(path: Path, w: int, h: int, masks: list[bytes]) -> None:
    path.write_bytes(b"WMSK" + struct.pack("<III", w, h, len(masks)) + b"".join(masks))


def slope(w: int, h: int, mask: bytes) -> int:
    """Rows the wall rises from its left to its right column (its top edge), or 0."""
    def first(x):
        return next((y for y in range(h) if mask[y * w + x]), None)
    a, b = first(0), first(w - 1)
    return 0 if a is None or b is None else a - b


def wall_input(w: int, h: int, bgra: bytes, mask: bytes) -> tuple[int, int, bytearray]:
    """(cw, ch, straight RGBA): the frame with its neighbours along the wall left and right of
    it, (w, -rise) and (-w, rise) away (rise: w/2 up for a face rising to the right), the rest
    mirror-filled. The frame sits at (w, w // 2)."""
    rise = slope(w, h, mask)
    rise = w // 2 if rise > 0 else -(w // 2) if rise < 0 else 0
    cw, ch = 3 * w, h + w
    out = bytearray(cw * ch * 4)
    rgba = hd_composite.to_straight(bgra)
    for dx, dy in ((0, 0), (w, -rise), (-w, rise)):
        ox, oy = w + dx, w // 2 + dy
        for y in range(h):
            for x in range(w):
                if mask[y * w + x]:
                    o = ((oy + y) * cw + ox + x) * 4
                    if out[o + 3] == 0:
                        out[o:o + 4] = rgba[(y * w + x) * 4:(y * w + x) * 4 + 4]
    return cw, ch, hd_composite.mirror_fill(cw, ch, out)


def prep_one(job) -> None:
    orig, png = job
    w, h = hd_composite.size_of(orig.stem)
    bgra = orig.read_bytes()
    mask = bytes(1 if bgra[i * 4 + 3] else 0 for i in range(w * h))
    cw, ch, rgba = wall_input(w, h, bgra, mask)
    hd_composite.write_png(png, cw, ch, bytes(rgba))


def composite_one(job) -> None:
    """The middle of the upscaler's result (where the frame sits) composited onto the frame."""
    orig, ai_png, out = job
    w, h = hd_composite.size_of(orig.stem)
    W, H, rgba = hd_composite.read_png(ai_png)
    k = W // (3 * w)
    x0, y0 = w * k, (w // 2) * k
    crop = b"".join(bytes(rgba[((y0 + y) * W + x0) * 4:((y0 + y) * W + x0 + w * k) * 4]) for y in range(h * k))
    ai3 = crop if k == hd_composite.SCALE else hd_composite.shrink(w * k, h * k, crop, w * 3, h * 3)
    out.write_bytes(bytes(hd_composite.composite(w, h, orig.read_bytes(), ai3)))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Build HD wallpapers for vkproxy.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=REPO / "work" / "hd_walls")
    ap.add_argument("--model", default="ultrasharp-v2", help="name in ai_upscale/models.json or a model file")
    ap.add_argument("--python", type=Path, default=REPO / "work" / "ext" / "torch" / "Scripts" / "python.exe",
                    help="python of the ai_upscale venv")
    ap.add_argument("--workers", type=int, default=4, help="upscaler processes sharing the GPU")
    ap.add_argument("--install", action="store_true", help="copy the HD wallpapers into <game>/vkproxy/replace")
    args = ap.parse_args(argv)
    t0 = [time.time()]

    def lap() -> str:
        t, t0[0] = t0[0], time.time()
        return f"{t0[0] - t:.0f} s"

    orig, pngs, ai, hd = (args.work / d for d in ("orig", "in", "ai", "hd"))
    for d in (orig, pngs, ai, hd):
        d.mkdir(parents=True, exist_ok=True)

    # 1. extract: masks per size, most used first, then each frame named by its covered pixels
    frames = list(wallpaper_frames(args.game))
    by_size: dict[tuple, Counter] = {}
    for w, h, _tex, mask in frames:
        by_size.setdefault((w, h), Counter())[mask] += 1
    mask_ids = {}
    for (w, h), counts in by_size.items():
        ordered = [m for m, _n in counts.most_common()]
        mask_ids.update({((w, h), m): i for i, m in enumerate(ordered)})
        write_masks(args.work / f"{w}x{h}.wallmasks", w, h, ordered)
    names = []
    for w, h, tex, mask in frames:
        name = f"{w}x{h}_{covered_hash(w, h, tex, mask)}_w{mask_ids[((w, h), mask)]}"
        if name in names:
            continue
        names.append(name)
        if not (orig / f"{name}.bgra").exists():
            (orig / f"{name}.bgra").write_bytes(tex)
    tops = []
    for w, h, tex, _mask in top_frames(args.game):
        name = spr2.content_name(w, h, tex)
        if name not in tops:
            tops.append(name)
            if not (orig / f"{name}.bgra").exists():
                (orig / f"{name}.bgra").write_bytes(tex)
    print(f"{len(frames)} wall frames, {len(names)} distinct, {len(by_size)} sizes; {len(tops)} wall tops ({lap()})")

    # 2. prep
    jobs = [(orig / f"{n}.bgra", pngs / f"{n}.png") for n in names if not (pngs / f"{n}.png").exists()]
    print(f"prep: {hd_composite.run_all([(j,) for j in jobs], prep_one)} new ({lap()})")
    jobs = [(orig / f"{n}.bgra", pngs / f"{n}.png") for n in tops if not (pngs / f"{n}.png").exists()]
    print(f"prep wall tops: {hd_composite.run_all(jobs, hd_composite.prep_one)} new ({lap()})")  # mirror filled

    # 3. upscale (colour only; no --composite: the result is cut down first)
    todo = [n for n in names + tops if not (hd / f"{n}@3.bgra").exists() and not (ai / f"{n}.png").exists()]
    if todo:
        import hd_art  # only for game_running
        if hd_art.game_running():
            print("The game is running: close it before upscaling (GPU).")
            return 1
        todo_file = args.work / "todo.txt"
        todo_file.write_text("\n".join(todo) + "\n", encoding="utf-8")
        cmd = [str(args.python), "-W", "ignore", str(REPO / "ai_upscale" / "upscale.py"), "--model", args.model,
               "--names", str(todo_file), "--workers", str(args.workers), str(pngs), str(ai)]
        print("upscale:", " ".join(cmd), flush=True)
        if subprocess.run(cmd).returncode != 0:
            return 1
        todo_file.unlink()
        print(f"upscale: {lap()}")

    # 4. composite
    jobs = [(orig / f"{n}.bgra", ai / f"{n}.png", hd / f"{n}@3.bgra") for n in names
            if (ai / f"{n}.png").exists() and not (hd / f"{n}@3.bgra").exists()]
    print(f"composite: {hd_composite.run_all([(j,) for j in jobs], composite_one)} new ({lap()})")
    jobs = [(orig / f"{n}.bgra", ai / f"{n}.png", hd / f"{n}@3.bgra") for n in tops
            if (ai / f"{n}.png").exists() and not (hd / f"{n}@3.bgra").exists()]
    print(f"composite wall tops: {hd_composite.run_all([(j,) for j in jobs], composite_top_one)} new ({lap()})")

    # 5. install
    if args.install:
        dest = hd_pack.Installed(args.game / "vkproxy" / "replace")
        n = 0
        for name in names + tops:
            src, installed = hd / f"{name}@3.bgra", dest.mtime(f"{name}@3.bgra")
            if src.exists() and (installed is None or installed < src.stat().st_mtime):
                dest.copy(src, f"{name}@3.bgra")
                n += 1
        for m in args.work.glob("*.wallmasks"):  # read when the game loads: stay loose
            shutil.copyfile(m, dest.folder / m.name)
        hd_pack.finish_install(dest)
        print(f"installed {n} HD wallpapers and wall tops and {len(by_size)} mask files into {dest.folder} ({lap()}; restart the game)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
