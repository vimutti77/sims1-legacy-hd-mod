"""Redraw the tile outline cursor at 3x with smooth edges.

The yellow outline drawn on the floor under an object being placed is Objects.far/Cursors.iff
SPR2 300 (one tile, a frame per zoom): a diamond band in one colour whose 1x edges are stairs,
which the AI copy keeps (it never covers more than the original). Its eight edges are straight
lines of slope +-1/2, so they are fitted instead: along each edge the stairs bound the line's
offset to an interval (a pixel is covered when its centre is inside), the middle of all the
intervals is taken, and the lines must rasterise back to the frame exactly. The band is then
drawn at 3x with 4x4 samples per pixel (anti-aliased edges; the outline is drawn by a sprite
shader without a depth texture, so its coverage may differ from the original's).

Usage:
    py tools/hd_shapes.py --game <dir> [--work <dir>] [--install]
writes <work>/<name>@3.bgra for each frame (name as vkproxy matches the game's upload) and with
--install puts them into <game>/vkproxy/replace (packed: tools/hd_pack.py).
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

import far
import hd_pack
import iff
import spr2

SCALE = 3
SAMPLES = 4  # per axis, per 3x pixel
SHAPES = [("Cursors.iff", 300)]  # (file in Objects.far, SPR2 id): the tile outline


class ShapeError(ValueError):
    pass


def mask_of(tex: bytes, w: int, h: int) -> list[list[bool]]:
    return [[tex[(y * w + x) * 4 + 3] > 0 for x in range(w)] for y in range(h)]


def runs(col: list[bool]) -> list[tuple[int, int]]:
    """Covered runs (first, last) of a column, top to bottom."""
    out, start = [], None
    for y, c in enumerate(col + [False]):
        if c and start is None:
            start = y
        elif not c and start is not None:
            out.append((start, y - 1))
            start = None
    return out


def fit(points, slope: float, h: int) -> float:
    """Offset c of the line y = c + slope * x through the stairs of one edge. points: (x, r):
    in column x the edge passes between rows r and r + 1 (one on each side), so between their
    centres; row -1 or h is past the texture's border and bounds nothing."""
    lo, hi = float("-inf"), float("inf")
    for x, r in points:
        cx = x + 0.5
        if r >= 0:
            lo = max(lo, r + 0.5 - slope * cx)
        if r + 1 < h:
            hi = min(hi, r + 1.5 - slope * cx)
    if lo >= hi:
        raise ShapeError("the edge is not a straight line of slope %s" % slope)
    if lo == float("-inf") or hi == float("inf"):
        return hi - 0.5 if lo == float("-inf") else lo + 0.5
    return (lo + hi) / 2


def split(xs: list[int], rows: dict, extreme) -> tuple[list[int], list[int]]:
    """The columns of the left and the right edge meeting at a tip: the tip is the run of
    columns at the extreme row, its first half going left and its second half right (the
    middle column of an odd run is on both lines)."""
    best = extreme(rows[x] for x in xs)
    at = [x for x in xs if rows[x] == best]
    first, last = at[0], at[-1]
    mid = (first + last) / 2
    return [x for x in xs if x < mid], [x for x in xs if x > mid]


def fit_band(mask: list[list[bool]]):
    """The eight edge lines (slope, offset) of a diamond band: outer top-left, top-right,
    bottom-left, bottom-right, then the hole's in the same order."""
    h, w = len(mask), len(mask[0])
    cols = [runs([mask[y][x] for y in range(h)]) for x in range(w)]
    if any(not c for c in cols) or any(len(c) > 2 for c in cols):
        raise ShapeError("not a diamond band")
    xs = list(range(w))
    top = {x: cols[x][0][0] for x in xs}
    bottom = {x: cols[x][-1][1] for x in xs}
    # in column x each edge passes between rows r and r + 1: (x, r) as fit takes it
    left, right = split(xs, top, min)
    tl = fit([(x, top[x] - 1) for x in left], -0.5, h)
    tr = fit([(x, top[x] - 1) for x in right], 0.5, h)
    left, right = split(xs, bottom, max)
    bl = fit([(x, bottom[x]) for x in left], 0.5, h)
    br = fit([(x, bottom[x]) for x in right], -0.5, h)
    holed = [x for x in xs if len(cols[x]) == 2]
    if not holed:
        raise ShapeError("no hole")
    upper = {x: cols[x][0][1] for x in holed}  # last row of the band above the hole
    lower = {x: cols[x][1][0] for x in holed}  # first row of the band below it
    left, right = split(holed, upper, min)
    itl = fit([(x, upper[x]) for x in left], -0.5, h)
    itr = fit([(x, upper[x]) for x in right], 0.5, h)
    left, right = split(holed, lower, max)
    ibl = fit([(x, lower[x] - 1) for x in left], 0.5, h)
    ibr = fit([(x, lower[x] - 1) for x in right], -0.5, h)
    return [(-0.5, tl), (0.5, tr), (0.5, bl), (-0.5, br), (-0.5, itl), (0.5, itr), (0.5, ibl), (-0.5, ibr)]


def inside(lines, x: float, y: float) -> bool:
    """Whether (x, y) (1x pixel units) is in the band."""
    def diamond(ls):
        (s0, c0), (s1, c1), (s2, c2), (s3, c3) = ls
        return y >= c0 + s0 * x and y >= c1 + s1 * x and y <= c2 + s2 * x and y <= c3 + s3 * x
    return diamond(lines[:4]) and not diamond(lines[4:])


def redraw(tex: bytes, w: int, h: int) -> bytes:
    """The 3x premultiplied B8G8R8A8 texture of a band frame; ShapeError if it is not one."""
    mask = mask_of(tex, w, h)
    lines = fit_band(mask)
    wrong = sum(inside(lines, x + 0.5, y + 0.5) != mask[y][x] for y in range(h) for x in range(w))
    if wrong:
        raise ShapeError(f"the fitted lines miss {wrong} pixels")
    colours = {tex[i:i + 4] for i in range(0, len(tex), 4) if tex[i + 3] == 255}
    if len(colours) != 1:
        raise ShapeError("not one colour")
    b, g, r, _ = next(iter(colours))
    out = bytearray(w * SCALE * h * SCALE * 4)
    n = SAMPLES * SAMPLES
    for Y in range(h * SCALE):
        for X in range(w * SCALE):
            hits = sum(inside(lines, (X + (i + 0.5) / SAMPLES) / SCALE, (Y + (j + 0.5) / SAMPLES) / SCALE)
                       for j in range(SAMPLES) for i in range(SAMPLES))
            if hits:
                a = hits * 255 // n if hits < n else 255
                o = (Y * w * SCALE + X) * 4
                out[o:o + 4] = bytes(((b * a + 127) // 255, (g * a + 127) // 255, (r * a + 127) // 255, a))
    return bytes(out)


def shape_frames(game: Path):
    """(content name, w, h, texture) of the frames of SHAPES in the user's install."""
    objects = game / "GameData" / "Objects" / "Objects.far"
    entries = {e.name.lower(): e for e in far.read_far(objects.read_bytes())} if objects.exists() else {}
    data_all = objects.read_bytes() if objects.exists() else b""
    for file_name, sprite in SHAPES:
        e = entries.get(file_name.lower())
        if e is None:
            print(f"{file_name}: not in {objects}")
            continue
        data = data_all[e.offset:e.offset + e.size]
        chunks = iff.read_iff(data)
        pals = spr2.read_palettes(data, chunks)
        for c in chunks:
            if c.type != "SPR2" or c.id != sprite:
                continue
            s = data[c.offset:c.offset + c.size]
            for off, default_pal in spr2.frame_offsets(s):
                try:
                    f = spr2.decode_frame(s, off)
                except (spr2.Spr2Error, struct.error):
                    continue
                pal = spr2.frame_palette(pals, f, default_pal)
                if pal and f.w and f.h:
                    tex = spr2.game_texture(f, pal)
                    yield spr2.content_name(f.w, f.h, tex), f.w, f.h, tex


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Redraw the tile outline cursor at 3x with smooth edges.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=Path(__file__).resolve().parents[1] / "work" / "hd_shapes")
    ap.add_argument("--install", action="store_true", help="put them into <game>/vkproxy/replace")
    args = ap.parse_args(argv)
    args.work.mkdir(parents=True, exist_ok=True)
    made = []
    for name, w, h, tex in shape_frames(args.game):
        try:
            hd = redraw(tex, w, h)
        except ShapeError as e:
            print(f"{name}: {e}")
            continue
        (args.work / f"{name}@{SCALE}.bgra").write_bytes(hd)
        made.append(name)
        print(f"{name}: redrawn")
    if args.install and made:
        dest = hd_pack.Installed(args.game / "vkproxy" / "replace")
        for name in made:
            dest.copy(args.work / f"{name}@{SCALE}.bgra", f"{name}@{SCALE}.bgra")
        hd_pack.finish_install(dest)
        print(f"installed {len(made)} into {dest.folder}; restart the game to see them")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
