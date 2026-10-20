"""HD twins of the middle- and far-zoom frames of object sprites, from the near frame's HD twin.

Legacy draws the lot at every zoom from the near-zoom sprites (scaled), but some screens use
the smaller frames themselves, e.g. the object picture in the panel shown while an object is
picked up in Buy mode (the middle-zoom frames, drawn 1:1). Their HD twins were upscaled by the
AI from those small frames, which hold little detail, and look blurred; the near frame's HD
twin shrunk to their size is sharp.

The objects' draw groups (DGRP) give each small frame's near frame: for a graphic and camera
direction, the n-th sprite at the middle (far) zoom is the n-th sprite at the near zoom, drawn
2 (4) times smaller (pairs). The near frame's installed HD twin (3x near) is placed by the
frame header offsets (searched a few pixels around for the best overlap with the small frame's
coverage), area-averaged down to 3x the small frame, and cut to the small frame's coverage:
never more than the original's (see tools/hd_composite.py); where the shrunk picture leaves a
covered pixel empty, the small frame's own AI twin fills it. When a frame has several near
frames, the one whose colours match best is taken; it is used when its shape fits (match,
allowing a pixel's shift) at least MIN_MATCH and its colours differ by at most MAX_ERROR.

Needs numpy (the ai_upscale venv). Usage:
    python ai_upscale/zoom_twins.py --game <dir> [--work <dir>] [--names <name>...] [--install]
<work> is tools/hd_art.py's (sprite_index.tsv, orig/); results go to <work>/zooms/ (or --out).
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import hd_pack  # noqa: E402

S = 3          # HD scale
SEARCH = 2     # near-HD pixels searched around the offsets the headers give
MIN_MATCH = 0.5  # shape fit (match): with DGRP pairs 99% fit above 0.89; below 0.5 are near-empty sprites
MAX_ERROR = 40.0  # mean colour difference allowed (0-255): above it, samples showed another part or
                  # variant (half the frames are under 6.5; 25-40 are noisy textures drawn apart)
DRAWN_ALPHA = 32  # a shrunk pixel at least this opaque counts as drawn (glass is ~100)
GAP_ALPHA = 64  # a shrunk pixel this transparent inside the coverage is a gap: filled from the small twin


NEAR, ZOOM_K = 3, {2: 2, 1: 4}  # DGRP zoom levels: near, and middle/far with their size divisor


def file_pairs(job) -> list:
    """(small frame, near frame, k) of one object file, by its draw groups (see pairs)."""
    name, data = job
    sys.path.insert(0, str(REPO / "catalog"))
    import objects  # noqa: E402
    import spr2  # noqa: E402
    try:
        obj = objects.ObjectFile(data, name)
    except Exception:  # noqa: BLE001 - a file that does not parse is skipped, as hd_art does
        return []
    info = {}

    def frame(sid, index):
        if (sid, index) not in info:
            got = obj.frame(sid, index)
            if got is None:
                info[sid, index] = None
            else:
                f, pal, hx, hy = got
                tex = spr2.game_texture(f, pal)
                info[sid, index] = dict(name=spr2.content_name(f.w, f.h, tex), w=f.w, h=f.h, hx=hx, hy=hy)
        return info[sid, index]

    out = []
    for images in obj.dgrp.values():
        by = {(im["direction"], im["zoom"]): im["sprites"] for im in images}
        for (direction, zoom), sprites in by.items():
            near = by.get((direction, NEAR))
            if zoom not in ZOOM_K or not near or len(near) != len(sprites):
                continue
            for s, n in zip(sprites, near):
                if (s["flags"] ^ n["flags"]) & 1:  # one mirrored, the other not
                    continue
                small, big = frame(s["spr"], s["frame"]), frame(n["spr"], n["frame"])
                if small and big and small["name"] != big["name"]:
                    out.append((small, big, ZOOM_K[zoom]))
    return out


def pairs(game: Path, names=None):
    """(small frame, [(near frame, k)]) from the objects' draw groups (DGRP): for each graphic
    and camera direction, the n-th sprite of the middle (far) zoom image is the n-th sprite of
    the near one, drawn 2 (4) times smaller. Frames are dicts: name, w, h, hx, hy. (Matching by
    size within a SPR2 chunk found another view for some objects: the same view's near frame
    can be in another chunk.)"""
    import hd_art  # noqa: E402
    files = [(name, data) for name, data, _chunks, _cat in hd_art.object_files(game, "all")]
    out = {}
    with ProcessPoolExecutor() as ex:
        for found in ex.map(file_pairs, files, chunksize=8):
            for small, big, k in found:
                if names and small["name"] not in names:
                    continue
                entry = out.setdefault(small["name"], (small, []))
                if all(c["name"] != big["name"] for c, _k in entry[1]):
                    entry[1].append((big, k))
    return list(out.values())


def load(data: bytes, w: int, h: int) -> np.ndarray:
    return np.frombuffer(data, np.uint8).reshape(h, w, 4).astype(np.float32)


def colour_error(shrunk: np.ndarray, o: np.ndarray) -> float:
    """Mean difference (0-255, premultiplied RGBA) between the shrunk picture taken down to 1x
    and the original, over the original's pixels (translucent ones too: glass panes have no
    opaque pixel at all)."""
    h, w = o.shape[:2]
    one = shrunk.reshape(h, S, w, S, 4).mean((1, 3))
    drawn = o[..., 3] > 0
    if not drawn.any():
        return 0.0
    return float(np.abs(one - o)[drawn].mean())


def grow(m: np.ndarray, r: int) -> np.ndarray:
    """m with every set pixel spread r pixels each way (a square)."""
    out = m.copy()
    p = np.pad(m, r)
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            out |= p[dy:dy + m.shape[0], dx:dx + m.shape[1]]
    return out


def match(mine: np.ndarray, cover: np.ndarray) -> float:
    """How well the shrunk picture's shape (mine) fits the original's coverage (at 3x), allowing
    one original pixel of difference: the smaller of the share of the coverage near the picture
    and the share of the picture near the coverage. Plain overlap punished thin and scattered
    sprites (door frames, flowers, strings of lights) for a pixel's shift."""
    if not cover.any() or not mine.any():
        return 0.0
    recall = (cover & grow(mine, S)).sum() / cover.sum()
    precision = (mine & grow(cover, S)).sum() / mine.sum()
    return float(min(recall, precision))


def derive(small: dict, near: dict, k: int, orig: bytes, small_hd: bytes, near_hd: bytes):
    """The small frame's new HD twin from one near frame: (bytes or None, fit, colour error)."""
    w, h = small["w"], small["h"]
    o = load(orig, w, h)
    cover = np.repeat(np.repeat(o[..., 3] > 0, S, 0), S, 1)
    n = load(near_hd, near["w"] * S, near["h"] * S)
    W, H = w * S * k, h * S * k  # the small frame's HD at near-HD pixels
    ex, ey = S * (near["hx"] - k * small["hx"]), S * (near["hy"] - k * small["hy"])
    best = None
    for dy in range(-SEARCH, SEARCH + 1):
        for dx in range(-SEARCH, SEARCH + 1):
            canvas = np.zeros((H, W, 4), np.float32)
            x0, y0 = ex + dx, ey + dy
            sx0, sy0 = max(0, -x0), max(0, -y0)
            tx0, ty0 = max(0, x0), max(0, y0)
            cw = min(n.shape[1] - sx0, W - tx0)
            ch = min(n.shape[0] - sy0, H - ty0)
            if cw <= 0 or ch <= 0:
                continue
            canvas[ty0:ty0 + ch, tx0:tx0 + cw] = n[sy0:sy0 + ch, sx0:sx0 + cw]
            shrunk = canvas.reshape(h * S, k, w * S, k, 4).mean((1, 3))  # premultiplied: a plain mean
            mine = shrunk[..., 3] >= DRAWN_ALPHA
            iou = (mine & cover).sum() / max(1, (mine | cover).sum())
            if best is None or iou > best[0]:
                best = (iou, shrunk, dx, dy)
    if best is None:
        return None, 0.0, 255.0
    _iou, shrunk, dx, dy = best  # the offset with the best plain overlap
    fit = match(shrunk[..., 3] >= DRAWN_ALPHA, cover)
    err = colour_error(shrunk, o)
    if fit < MIN_MATCH or err > MAX_ERROR:
        return None, fit, err
    fill = load(small_hd, w * S, h * S)
    gap = cover & (shrunk[..., 3] < GAP_ALPHA)
    out = np.where(gap[..., None], fill, shrunk)
    out[~cover] = 0
    return np.clip(out + 0.5, 0, 255).astype(np.uint8).tobytes(), fit, err


def job(args):
    small, cands, work, out, replace = args
    inst = _installed(replace)
    name = small["name"]
    orig = work / "orig" / f"{name}.bgra"
    # the fill: the AI's own twin of this frame as hd_art.py built it (the installed one may be
    # an earlier run of this script); the installed one when the build is gone
    built = sorted((work / "hd").glob(f"*/{name}@{S}.bgra"))
    small_hd = built[0].read_bytes() if built else inst.read(f"{name}@{S}.bgra")
    if not orig.exists() or small_hd is None:
        return name, "", None, 0.0, 0.0
    best = None  # the candidate whose colours match best
    for near, k in cands:
        near_hd = inst.read(f"{near['name']}@{S}.bgra")
        if near_hd is None:
            continue
        data, iou, err = derive(small, near, k, orig.read_bytes(), small_hd, near_hd)
        if best is None or (data is not None, -err) > (best[1] is not None, -best[3]):
            best = (near["name"], data, iou, err)
    if best is None:
        return name, "", None, 0.0, 0.0
    near_name, data, iou, err = best
    if data is None:
        return name, near_name, "poor fit", iou, err
    (out / f"{name}@{S}.bgra").write_bytes(data)
    return name, near_name, "ok", iou, err


_inst = {}


def _installed(folder):
    if folder not in _inst:
        _inst[folder] = hd_pack.Installed(folder)
    return _inst[folder]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Middle/far-zoom HD twins from the near frames' HD twins.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=REPO / "work" / "hd_art")
    ap.add_argument("--names", nargs="*", help="only these frames (content names)")
    ap.add_argument("--out", type=Path, help="where the results go (default <work>/zooms)")
    ap.add_argument("--install", action="store_true", help="put the results into <game>/vkproxy/replace")
    args = ap.parse_args(argv)
    replace = args.game / "vkproxy" / "replace"
    out = args.out or args.work / "zooms"
    out.mkdir(parents=True, exist_ok=True)
    todo = [(s, c, args.work, out, replace) for s, c in pairs(args.game, set(args.names or []))]
    todo = [t for t in todo if not (out / f"{t[0]['name']}@{S}.bgra").exists()]
    print(f"{len(todo)} small frames with a near frame to build from")
    made = skipped = 0
    with ProcessPoolExecutor() as ex, open(out / "fit.tsv", "a", encoding="utf-8") as log:
        for i, (name, near_name, res, iou, err) in enumerate(ex.map(job, todo, chunksize=32)):
            if res:
                log.write(f"{name}\t{near_name}\t{res}\t{iou:.3f}\t{err:.1f}\n")
            if res == "ok":
                made += 1
            elif res:
                skipped += 1
            if args.names and res:
                print(f"{name}: {res} from {near_name}, fit {iou:.2f}, colour error {err:.1f}")
            if (i + 1) % 5000 == 0:
                print(f"  {i + 1}/{len(todo)}", flush=True)
    print(f"made {made}, left alone {skipped} (poor fit; fits in {out / 'fit.tsv'})")
    if args.install:
        dest = hd_pack.Installed(replace)
        built = sorted(out.glob(f"*@{S}.bgra"))
        if args.names:
            built = [p for p in built if p.name[:-len(f"@{S}.bgra")] in args.names]
        n = 0
        for p in built:
            if dest.read(p.name) != p.read_bytes():
                dest.copy(p, p.name)
                n += 1
        hd_pack.finish_install(dest)
        print(f"installed {n} into {dest.folder}; restart the game to see them")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
