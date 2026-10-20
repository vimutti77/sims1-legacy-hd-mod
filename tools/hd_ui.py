"""HD UI bitmaps redrawn from the user's UIGraphics.far: dithered gradients made smooth.

Some UI backgrounds are 8-bit bitmaps whose colour gradients are dithered (the control
panel's cpanel\\Backgrounds\\PanelBack.bmp has 85 colours), and Legacy uploads UI bitmaps
truncated to RGB565 (upload(); checked byte for byte), so at 3x the dither shows as grain.
For a gradient that runs along x:
  - each row is averaged along x (Gaussian, sigma 4), never across a step of EDGE or more
    between neighbours (the panel's ends);
  - rows that then differ by less than ROW_STEP (what is left of the dither) are averaged
    along y (sigma 2), never across a bigger step (the bevel lines along the top and bottom);
  - drawn at 3x bilinear along x, nearest along y (lines stay crisp), nearest at steps.

Usage:
    py tools/hd_ui.py --game <dir> [--work <dir>] [--install]
writes <work>/<name>@3.bgra (name as vkproxy matches the upload) and with --install puts them
into <game>/vkproxy/replace (packed: tools/hd_pack.py).
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import bmp
import far
import hd_pack
import spr2

SCALE = 3
EDGE = 24       # a step this big between neighbours along x is an edge, not dither
ROW_STEP = 4    # rows (after smoothing along x) this close are averaged along y
SIGMA_X, SIGMA_Y = 4.0, 2.0
GRADIENTS = [r"cpanel\Backgrounds\PanelBack.bmp"]  # in UIGraphics.far: gradients along x


def upload(rgba: bytes) -> bytes:
    """The B8G8R8A8 texture Legacy uploads for a decoded UI bitmap (opaque): RGB565, truncated."""
    out = bytearray(len(rgba))
    out[0::4] = bytes(b & 0xF8 for b in rgba[2::4])
    out[1::4] = bytes(g & 0xFC for g in rgba[1::4])
    out[2::4] = bytes(r & 0xF8 for r in rgba[0::4])
    out[3::4] = rgba[3::4]
    return bytes(out)


def kernel(sigma: float) -> list[float]:
    r = int(3 * sigma)
    return [math.exp(-0.5 * (i / sigma) ** 2) for i in range(-r, r + 1)]


def masked_blur(rows: list[list[float]], keep: list[list[bool]], k: list[float]) -> list[list[float]]:
    """Each value averaged along its row with weights k over the values not in keep (which
    stay as they are and do not contribute)."""
    r = len(k) // 2
    out = []
    for vals, kept in zip(rows, keep):
        n = len(vals)
        row = []
        for x in range(n):
            if kept[x]:
                row.append(vals[x])
                continue
            num = den = 0.0
            for i in range(max(0, x - r), min(n, x + r + 1)):
                if not kept[i]:
                    f = k[i - x + r]
                    num += f * vals[i]
                    den += f
            row.append(num / den)
        out.append(row)
    return out


def transpose(m):
    return [list(c) for c in zip(*m)]


def smooth_gradient(w: int, h: int, tex: bytes) -> bytes:
    """The 3x B8G8R8A8 texture of a dithered gradient along x (opaque). See the module docstring."""
    chans = [[[float(tex[(y * w + x) * 4 + c]) for x in range(w)] for y in range(h)] for c in range(3)]
    step = [[max(abs(ch[y][x + 1] - ch[y][x]) for ch in chans) for x in range(w - 1)] for y in range(h)]
    edge = [[(x > 0 and step[y][x - 1] >= EDGE) or (x < w - 1 and step[y][x] >= EDGE) for x in range(w)]
            for y in range(h)]
    kx, ky = kernel(SIGMA_X), kernel(SIGMA_Y)
    chans = [masked_blur(ch, edge, kx) for ch in chans]
    jump = [[max(abs(ch[y + 1][x] - ch[y][x]) for ch in chans) for x in range(w)] for y in range(h - 1)]
    vedge = [[edge[y][x] or (y > 0 and jump[y - 1][x] >= ROW_STEP) or (y < h - 1 and jump[y][x] >= ROW_STEP)
              for x in range(w)] for y in range(h)]
    vkeep = transpose(vedge)
    chans = [transpose(masked_blur(transpose(ch), vkeep, ky)) for ch in chans]
    W = w * SCALE
    taps = []
    for X in range(W):
        sx = (X + 0.5) / SCALE - 0.5
        x0 = min(max(math.floor(sx), 0), w - 1)
        x1 = min(x0 + 1, w - 1)
        taps.append((x0, x1, min(max(sx - x0, 0.0), 1.0), min(X // SCALE, w - 1)))
    out = bytearray(W * h * SCALE * 4)
    for y in range(h):
        line = bytearray(W * 4)
        for X, (x0, x1, f, xn) in enumerate(taps):
            blend = not (edge[y][x0] or edge[y][x1])
            for c in range(3):
                v = chans[c][y][x0] * (1 - f) + chans[c][y][x1] * f if blend else chans[c][y][xn]
                line[X * 4 + c] = min(255, max(0, int(v + 0.5)))
            line[X * 4 + 3] = 255
        for k in range(SCALE):
            o = (y * SCALE + k) * W * 4
            out[o:o + W * 4] = line
    return bytes(out)


def gradients(game: Path):
    """(content name, w, h, upload) of the GRADIENTS in the user's UIGraphics.far."""
    path = game / "UIGraphics" / "UIGraphics.far"
    data = path.read_bytes()
    entries = {e.name.lower(): e for e in far.read_far(data)}
    for name in GRADIENTS:
        e = entries.get(name.lower())
        if e is None:
            print(f"{name}: not in {path}")
            continue
        w, h, rgba = bmp.decode_bmp(data[e.offset:e.offset + e.size])
        tex = upload(rgba)
        yield spr2.content_name(w, h, tex), w, h, tex


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Smooth HD versions of dithered UI gradients.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=Path(__file__).resolve().parents[1] / "work" / "hd_ui")
    ap.add_argument("--install", action="store_true", help="put them into <game>/vkproxy/replace")
    args = ap.parse_args(argv)
    args.work.mkdir(parents=True, exist_ok=True)
    made = []
    for name, w, h, tex in gradients(args.game):
        (args.work / f"{name}@{SCALE}.bgra").write_bytes(smooth_gradient(w, h, tex))
        made.append(name)
        print(f"{name}: smoothed")
    if args.install and made:
        dest = hd_pack.Installed(args.game / "vkproxy" / "replace")
        for name in made:
            dest.copy(args.work / f"{name}@{SCALE}.bgra", f"{name}@{SCALE}.bgra")
        hd_pack.finish_install(dest)
        print(f"installed {len(made)} into {dest.folder}; restart the game to see them")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
