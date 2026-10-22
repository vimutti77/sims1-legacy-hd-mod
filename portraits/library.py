"""Portraits for Sims made later: every head and skin texture drawn ahead, for vkproxy to match.

Legacy draws the portraits of a Sim made in Create-a-Sim, and of townies and NPCs when they
are made (their templates carry none), with one camera per picture (POSES, measured on the
fitted pictures of such Sims; the speech balloons' framed to the head, FRAMED) on fixed
backgrounds (BACKGROUNDS). So every head the game
has (a head suit, xskin-<suit>-HEAD-HEAD, with its glasses or hat, and each head texture of
its code: c802fa_cornelia with c802fa{lgt,med,drk}_*.bmp) is drawn here into the six
pictures twice: at 1x as the game would (what vkproxy compares a picture without an HD file
with, in a player's game) and at 3x with the HD head texture (what it then shows). The
players get these files; nothing is drawn on their machines.

Files in --work (default work/portraits/library):
  <w>x<h>_<id>@3.bgra   the 3x pictures; id: the head and texture's key hashed like a content name
  portraits.lib         the 1x pictures: b"PLIB", u32 version 2, u32 kinds, then per kind
                        u32 w, h, count, f32 max error, f32 margin (ACCEPT) and count x (u64 id,
                        w*h*4 bytes B8G8R8A8 truncated to RGB565 as the game uploads them)
--install copies both into <game>/vkproxy/replace (the pictures packed).

Usage:
    py portraits/library.py --game <dir> [--install]
Needs numpy.
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):  # one BLAS thread per worker
    os.environ.setdefault(_v, "1")

import argparse  # noqa: E402
import re  # noqa: E402
import struct  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hd_pack  # noqa: E402
import hd_ui  # noqa: E402
import redraw  # noqa: E402
import spr2  # noqa: E402
from head import Head, compose, project, render  # noqa: E402

FILE = "portraits.lib"
TONES = ("lgt", "med", "drk")
# per picture: the background of each head region (RGB, as in the stored BMP)
BACKGROUNDS = {
    2002: [(0, 0, 82)] * 3,
    2003: [(214, 0, 0), (140, 45, 41), (66, 93, 90), (33, 146, 74), (0, 202, 58)],
    2004: [(0, 0, 0)],
    2005: [(248, 252, 248)],
    2006: [(248, 252, 248)],
    2007: [(200, 196, 192)],
}
FRAME_2004 = (0, 0, 248)  # the one-pixel frame around 2004's head
# per picture: the pose of each head region (yaw, pitch, roll, scale, cx, cy) the game draws
# Create-a-Sim's (and townies') pictures with: the medians of pose.fit_view's fits on the 111 of
# the 167 human Sims of a neighbourhood whose control panel faces share one camera (the rest are
# Maxis' ready-made Sims, drawn otherwise, whose pictures redraw.py matches exactly instead).
# Spread around them: under 0.05 rad, 3% scale, 0.7 px.
POSES: dict[int, list[tuple]] = {
    2002: [(0.001, -0.169, 0.000, 51.836, 18.062, 29.206),
           (0.132, -0.329, 0.040, 51.649, 18.041, 29.133),
           (-0.121, -0.011, 0.001, 51.973, 18.074, 29.237)],
    2003: [(-0.316, -0.083, -0.022, 50.500, 20.606, 28.441)] * 5,
    2004: [(0.004, -0.161, 0.002, 29.419, 11.822, 16.500)],
    2007: [(0.003, -0.213, 0.003, 43.504, 19.011, 27.615)],  # the pitch varies (-0.28..-0.10): no rule found
}
# The speech balloons' pictures are framed to the head instead: turned so, the head's outline
# as drawn fills the picture's height, its top at the top, centred across (scale off by 0.8%
# at the median, 2.5% at the 90th percentile).
FRAMED = {2005: (-0.337, -0.093, -0.022), 2006: (-0.322, -0.075, -0.022)}
# What vkproxy accepts: the closest head's mean difference per channel (of 255) at most the first
# value, and the next closest's at least the second value more (small pictures of different
# heads look alike). Emulated on the 246 human Sims of two neighbourhoods, ready-made ones
# included (those must not match): no picture was given another head's at slightly looser
# limits (2002 19.5/1, 2003 12/0, 2004 11.5/1, 2005 18/1, 2006 17/3, 2007 9.5/1).
ACCEPT = {2002: (14.0, 2.0), 2003: (11.0, 1.0), 2004: (11.0, 1.5), 2005: (16.0, 2.0), 2006: (15.0, 3.0),
          2007: (9.0, 1.5)}


def poses_for(head: Head, cid: int) -> list[tuple]:
    if cid not in FRAMED:
        return POSES[cid]
    w, h = redraw.SIZES[cid]
    turn = FRAMED[cid]
    sx, sy, _ = project(head, (*turn, 1.0, 0.0, 0.0))
    scale = h / (sy.max() - sy.min())
    return [(*turn, scale, w / 2 - (sx.max() + sx.min()) / 2 * scale, -sy.min() * scale)]


def key_id(key: str) -> str:
    """The 16 hex digits a library picture is named by (as content names are hashed)."""
    return spr2.content_name(0, 0, key.encode("utf-8")).split("_", 1)[1]


def candidates(files: dict) -> list[tuple[str, str]]:
    """(suit, texture) of every head the game has: each head suit with each head texture of its
    code (c802fa_cornelia: c802falgt_cornelia, c802fadrk_cornelia3, ...)."""
    suits = sorted({n[len("xskin-"):n.rindex("-head-head.")] for n in files
                    if n.startswith("xskin-") and "-head-head." in n})
    textures = sorted(n[:-4] for n in files if n.endswith(".bmp"))
    out = []
    for suit in suits:
        m = re.match(r"([a-z]\d{3}[a-z]{2})_", suit)
        if not m:
            continue
        code = m.group(1)
        out += [(suit, t) for t in textures if t.startswith(code) and t[len(code):len(code) + 3] in TONES]
    return out


def picture(head: Head, textures, cid: int, scale: int) -> np.ndarray:
    """The picture cid of this head (RGB floats) at 1x or 3x, as Create-a-Sim draws it."""
    w, h = redraw.SIZES[cid]
    out = np.zeros((h * scale, w * scale, 3))
    if cid == 2004:
        out[:] = FRAME_2004
    for (x, y, rw, rh), bg, p in zip(redraw.PICTURES[cid][0], BACKGROUNDS[cid], poses_for(head, cid)):
        drawn = (*p[:3], p[3] * scale, p[4] * scale, p[5] * scale)
        colour, cov = render(head, textures, drawn, (rw * scale, rh * scale), redraw.SAMPLES)
        out[y * scale:(y + rh) * scale, x * scale:(x + rw) * scale] = compose(colour, cov, bg)
    return np.clip(np.round(out), 0, 255).astype(np.uint8)


def build_head(job):
    """(id, {cid: 1x bytes as uploaded}) of one head and texture, its 3x pictures written; or
    (key, reason) when it cannot be drawn."""
    suit, texture, out_dir = job
    found = redraw.head_of(f"{suit},HEAD-HEAD={texture}")
    key = f"{suit}={texture}"
    if isinstance(found, str):
        return key, found
    skins, tex1, tex3 = found
    head = Head(skins)
    hid = key_id(key)
    small = {}
    for cid in redraw.PICTURES:
        w, h = redraw.SIZES[cid]
        one = picture(head, tex1, cid, 1)
        rgba = np.concatenate([one, np.full((h, w, 1), 255, np.uint8)], 2).tobytes()
        small[cid] = hd_ui.upload(rgba)  # B8G8R8A8, RGB565-truncated
        big = picture(head, tex3, cid, redraw.SCALE)
        bgra = np.concatenate([big[..., ::-1], np.full(big.shape[:2] + (1,), 255, np.uint8)], 2)
        (Path(out_dir) / f"{w}x{h}_{hid}@{redraw.SCALE}.bgra").write_bytes(bgra.tobytes())
    return hid, small


def write_library(path: Path, heads: list[tuple[str, dict]]) -> None:
    with open(path, "wb") as f:
        f.write(b"PLIB" + struct.pack("<II", 2, len(redraw.PICTURES)))
        for cid in redraw.PICTURES:
            w, h = redraw.SIZES[cid]
            f.write(struct.pack("<IIIff", w, h, len(heads), *ACCEPT[cid]))
            for hid, small in heads:
                f.write(struct.pack("<Q", int(hid, 16)) + small[cid])


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Draw every head's portraits ahead, for vkproxy to match.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=REPO / "work" / "portraits" / "library")
    ap.add_argument("--install", action="store_true", help="copy them into <game>/vkproxy/replace")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    args = ap.parse_args(argv)
    t0 = time.time()
    args.work.mkdir(parents=True, exist_ok=True)
    files = redraw.game_files(args.game)
    replace = args.game / "vkproxy" / "replace"
    heads = candidates(files)
    print(f"{len(heads)} heads with their textures ({time.time() - t0:.0f} s)", flush=True)
    done, skipped = [], []
    with ProcessPoolExecutor(max_workers=max(1, args.workers), initializer=redraw._init,
                             initargs=(files, str(replace))) as pool:
        for i, (a, b) in enumerate(pool.map(build_head, [(s, t, str(args.work)) for s, t in heads])):
            (done if isinstance(b, dict) else skipped).append((a, b))
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(heads)} ({time.time() - t0:.0f} s)", flush=True)
    for key, why in skipped:
        print(f"  not drawn: {key}: {why}")
    write_library(args.work / FILE, done)
    print(f"{len(done)} heads drawn, {len(skipped)} not ({time.time() - t0:.0f} s)")
    if args.install:
        dest = hd_pack.Installed(replace)
        for f in sorted(args.work.glob(f"*@{redraw.SCALE}.bgra")):
            if dest.mtime(f.name) is None or dest.mtime(f.name) < f.stat().st_mtime:
                dest.copy(f, f.name)
        hd_pack.finish_install(dest)
        (replace / FILE).write_bytes((args.work / FILE).read_bytes())
        print(f"installed into {replace}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
