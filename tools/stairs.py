"""Stair-stepped 1x outlines redrawn as straight, anti-aliased edges at 3x.

The lot is isometric: the edges of walls, wall tops and floor tiles are lines of slope +-1/2,
or vertical or horizontal, which a 1x sprite draws as stairs, so at 3x they show as 3-pixel
steps. For each 1x pixel on an outline, the line through its neighbourhood is fitted: for each
slope and each side the inside may be on, the pixel centres bound the line's offset to an
interval (covered ones on its inside, the others outside); the feasible candidate with the
widest interval wins and its middle is taken. The neighbourhood is 3 columns and 2 rows each
way, smaller near corners where no one line fits it (WINDOWS). Each 3x pixel of that 1x pixel is
then covered by the share of its SAMPLES x SAMPLES points inside the line. Where no line fits
even the smallest neighbourhood (a pixel-wide spike), the 1x pixel is kept as it is.

The same stairs give the same line whichever side is covered, so two sprites meeting along an
edge at 1x (a wall top on a wall face) still meet at 3x. vkproxy does the same for wall coverage
masks (stair_coverage in vkproxy.cpp): keep the two in step.

Usage:
    py tools/stairs.py <w> <h> <mask.raw> <out.raw>   (one byte per pixel, nonzero = covered)
"""
from __future__ import annotations

import sys
from pathlib import Path

SCALE = 3
SAMPLES = 4
WINDOWS = ((3, 2), (2, 1), (1, 1))  # columns, rows each way: the first that one line fits is used
SLOPES = (0.5, -0.5, 0.0)  # lines y = c + s * x; vertical lines x = c are tried too


def fit(mask: bytes, w: int, h: int, x: int, y: int):
    """The line through the outline at 1x pixel (x, y): (slope or None for vertical, offset, inside
    is below / right of it), or None. Near a corner no line fits the whole window, so smaller
    ones are tried (WINDOWS)."""
    for wx, wy in WINDOWS:
        line = fit_window(mask, w, h, x, y, wx, wy)
        if line is not None:
            return line
    return None


def fit_window(mask: bytes, w: int, h: int, x: int, y: int, wx: int, wy: int):
    pts = [(px + 0.5, py + 0.5, bool(mask[py * w + px]))
           for py in range(max(0, y - wy), min(h, y + wy + 1))
           for px in range(max(0, x - wx), min(w, x + wx + 1))]
    best = None
    for s in SLOPES + (None,):
        # inside below (y >= line) / right (x >= line), then above / left
        for below in (True, False):
            lo, hi = float("-inf"), float("inf")
            for cx, cy, covered in pts:
                v = cx if s is None else cy - s * cx  # the offset that puts the line through this centre
                if covered == below:  # this centre must be at or past the line on the 'below' side
                    hi = min(hi, v)
                else:
                    lo = max(lo, v)
            if lo < hi and lo > float("-inf") and hi < float("inf"):
                if best is None or hi - lo > best[0]:
                    best = (hi - lo, s, (lo + hi) / 2, below)
    return None if best is None else best[1:]


def inside(line, x: float, y: float) -> bool:
    s, c, below = line
    v = x if s is None else y - s * x
    return v >= c if below else v <= c


def mark(coverage: int, original: bool) -> int:
    """A coverage (0-255) stored with whether the 1x pixel it lies in was covered: 128-255 for
    those, 0-127 for pixels the straight edges add. The shaders push added pixels a little back
    (vkproxy world_wall.frag): their depth is the wall's plane carried past where the wall was,
    and against an object standing at the wall they would win, as a dashed line over it."""
    return (128 if original else 0) + (coverage * 127 + 127) // 255


def upscale(mask: bytes, w: int, h: int, grow_top: float = 0.0, marked: bool = False) -> bytes:
    """Coverage (0-255) at SCALE x the size, edges straightened and anti-aliased. grow_top (1x
    pixels) moves the sloped edges whose inside is below them that far up: a wall face's top
    edge, which a wall top covers; were both edges soft there, what is behind the wall would
    show through between them (up to a quarter where each covers half). marked: values as
    mark() stores them."""
    W = w * SCALE
    out = bytearray(W * h * SCALE)
    n = SAMPLES * SAMPLES
    for y in range(h):
        for x in range(w):
            v = 255 if mask[y * w + x] else 0
            edge = any(bool(mask[yy * w + xx]) != bool(v)
                       for yy in range(max(0, y - 1), min(h, y + 2)) for xx in range(max(0, x - 1), min(w, x + 2)))
            line = fit(mask, w, h, x, y) if edge else None
            if line is not None and grow_top and line[0] in (0.5, -0.5) and line[2]:
                line = (line[0], line[1] - grow_top, line[2])
            for j in range(SCALE):
                for i in range(SCALE):
                    X, Y = x * SCALE + i, y * SCALE + j
                    if line is None:
                        c = v
                    else:
                        hits = sum(inside(line, (X + (a + 0.5) / SAMPLES) / SCALE, (Y + (b + 0.5) / SAMPLES) / SCALE)
                                   for b in range(SAMPLES) for a in range(SAMPLES))
                        c = (hits * 255 + n // 2) // n
                    out[Y * W + X] = mark(c, bool(v)) if marked else c
    return bytes(out)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    w, h = int(argv[0]), int(argv[1])
    mask = Path(argv[2]).read_bytes()
    if len(mask) != w * h:
        print(f"{argv[2]}: {len(mask)} bytes, not {w}x{h}")
        return 1
    Path(argv[3]).write_bytes(upscale(mask, w, h))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
