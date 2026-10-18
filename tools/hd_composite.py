"""Turn the game's textures plus an upscaler's output into vkproxy HD textures.

Two steps around the AI upscaler (ai_upscale/):

  prep       <orig_dir> <png_dir>
             For each <name>.bgra (a game texture as vkproxy sees it: premultiplied
             B8G8R8A8; name = <w>x<h>_<hash>), write <png_dir>/<name>.png with straight
             alpha, and every not-solid pixel filled with the picture mirrored across
             the nearest solid edge. The upscaler then sees surfaces continue past
             cuts and outlines instead of a hard edge, and draws no halo there.

  composite  <orig_dir> <ai_dir> <out_dir>
             For each <ai_dir>/<name>.png (the upscaler's output, any integer scale
             >= 3), write <out_dir>/<name>@3.bgra, a 3x premultiplied B8G8R8A8 texture:
             - coverage is exactly the original's (nearest alpha), never more: the
               world shaders read each pixel's depth from a separate 1x z-buffer
               texture that is empty (= frontmost) outside the sprite, so a leaked
               pixel would draw through walls; tiles and object pieces still meet;
             - colour inside fully opaque areas comes from the upscaler (area-averaged
               to 3x), pulled halfway back towards the original (back-projection:
               the difference between the original and the 3x colour shrunk back to
               1x is spread back bilinearly; the upscaler sometimes repaints small,
               noisy sprites, e.g. a newspaper's print became folds);
             - where the original has partial alpha nearby (soft shadows, anti-aliased
               edges) the original is sampled bilinearly instead, from its non-empty
               texels only: an empty texel in the mix faded the edge, and where two
               pieces of an object meet, the piece below showed through as a line.

Both steps skip outputs that already exist and run on all CPU cores.

Usage:
    python tools/hd_composite.py prep      <orig_dir> <png_dir>
    python tools/hd_composite.py composite <orig_dir> <ai_dir> <out_dir>
"""
from __future__ import annotations

import os
import struct
import sys
import zlib
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

SCALE = 3
SOLID = 128  # alpha at or above which a pixel counts as solid for mirroring
BACKPROJECT = 0.5  # how far the upscaler's colour is pulled back to the original (0 = not, 1 = fully)
BACKPROJECT_ITERS = 3


def size_of(name: str) -> tuple[int, int]:
    w, h = name.split("_", 1)[0].split("x")
    return int(w), int(h)


# ---- PNG (8-bit RGB/RGBA, no interlace) ----------------------------------------

def write_png(path: Path, w: int, h: int, rgba: bytes) -> None:
    raw = b"".join(b"\0" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def read_png(path: Path) -> tuple[int, int, bytearray]:
    d = path.read_bytes()
    if d[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path.name}: not a PNG")
    pos, idat, w = 8, b"", 0
    channels = 4
    while pos + 8 <= len(d):
        n, t = struct.unpack(">I4s", d[pos:pos + 8])
        body = d[pos + 8:pos + 8 + n]
        if t == b"IHDR":
            w, h, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body[:13])
            if depth != 8 or ctype not in (2, 6) or interlace:
                raise ValueError(f"{path.name}: only 8-bit RGB/RGBA, non-interlaced")
            channels = 4 if ctype == 6 else 3
        elif t == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * channels
    out = bytearray(w * h * 4)
    prev = bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        for x in range(w):
            o = (y * w + x) * 4
            out[o:o + 3] = line[x * channels:x * channels + 3]
            out[o + 3] = line[x * channels + 3] if channels == 4 else 255
    return w, h, out


# ---- prep ------------------------------------------------------------------------

def to_straight(bgra: bytes) -> bytearray:
    """Premultiplied BGRA -> straight RGBA."""
    out = bytearray(len(bgra))
    for i in range(0, len(bgra), 4):
        b, g, r, a = bgra[i:i + 4]
        if 0 < a < 255:
            r, g, b = (min(255, (c * 255 + a // 2) // a) for c in (r, g, b))
        out[i:i + 4] = bytes((r, g, b, a))
    return out


def mirror_fill(w: int, h: int, rgba: bytearray) -> bytearray:
    """Colour every not-solid pixel p with the solid pixel mirrored across the
    nearest solid pixel q (2q - p), or with q itself when that is not solid."""
    solid = [rgba[i * 4 + 3] >= SOLID for i in range(w * h)]
    near = [-1] * (w * h)
    queue = deque(i for i in range(w * h) if solid[i])
    for i in queue:
        near[i] = i
    while queue:
        i = queue.popleft()
        x, y = i % w, i // w
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and near[ny * w + nx] < 0:
                near[ny * w + nx] = near[i]
                queue.append(ny * w + nx)
    out = bytearray(rgba)
    for i in range(w * h):
        if solid[i] or near[i] < 0:
            continue
        x, y, q = i % w, i // w, near[i]
        rx, ry = 2 * (q % w) - x, 2 * (q // w) - y
        src = ry * w + rx if 0 <= rx < w and 0 <= ry < h and solid[ry * w + rx] else q
        out[i * 4:i * 4 + 3] = rgba[src * 4:src * 4 + 3]
    return out


def prep_one(orig: Path, png: Path) -> None:
    w, h = size_of(orig.stem)
    write_png(png, w, h, mirror_fill(w, h, to_straight(orig.read_bytes())))


# ---- composite ---------------------------------------------------------------------

def area_weights(n_in: int, n_out: int) -> list[list[tuple[int, float]]]:
    r = n_in / n_out
    out = []
    for i in range(n_out):
        lo, hi = i * r, (i + 1) * r
        out.append([(j, (min(hi, j + 1) - max(lo, j)) / r) for j in range(int(lo), min(n_in, int(hi) + 1))
                    if min(hi, j + 1) - max(lo, j) > 1e-9])
    return out


def shrink(w: int, h: int, rgba: bytes, ow: int, oh: int) -> bytearray:
    """Area-average resize of straight RGBA (colour weighted by alpha)."""
    wx, wy = area_weights(w, ow), area_weights(h, oh)
    out = bytearray(ow * oh * 4)
    for oy in range(oh):
        for ox in range(ow):
            acc = [0.0, 0.0, 0.0]
            a_sum = cov = 0.0
            for sy, fy in wy[oy]:
                for sx, fx in wx[ox]:
                    f, i = fx * fy, (sy * w + sx) * 4
                    a = rgba[i + 3] / 255
                    acc[0] += rgba[i] * a * f
                    acc[1] += rgba[i + 1] * a * f
                    acc[2] += rgba[i + 2] * a * f
                    a_sum += a * f
                    cov += f
            o = (oy * ow + ox) * 4
            if a_sum > 0:
                out[o:o + 3] = bytes(min(255, int(c / a_sum + 0.5)) for c in acc)
            out[o + 3] = min(255, int(255 * a_sum / cov + 0.5))
    return out


def up_weights(n: int) -> list[list[tuple[int, float]]]:
    """Bilinear weights of each of SCALE*n outputs over n inputs (pixel centres, edges clamped)."""
    out = []
    for i in range(n * SCALE):
        src = (i + 0.5) / SCALE - 0.5
        j0 = int(src // 1)
        t = src - j0
        out.append([(min(max(j0, 0), n - 1), 1 - t), (min(max(j0 + 1, 0), n - 1), t)])
    return out


def backproject(w0: int, h0: int, orig: bytes, ai3: bytes, strength: float,
                iters: int = BACKPROJECT_ITERS) -> bytearray:
    """ai3 (straight RGBA at 3x) with its opaque texels' colour pulled towards the original:
    iters times, the difference between each opaque original texel and the mean of its 3x3
    block is spread back bilinearly; the result is strength of the way there."""
    W, H = w0 * SCALE, h0 * SCALE
    opaque = [orig[i * 4 + 3] == 255 for i in range(w0 * h0)]
    cur = [float(ai3[(p // 3) * 4 + p % 3]) for p in range(W * H * 3)]  # RGB per 3x pixel
    wx, wy = up_weights(w0), up_weights(h0)
    for _ in range(iters):
        res = [0.0] * (w0 * h0 * 3)
        for ty in range(h0):
            for tx in range(w0):
                t = ty * w0 + tx
                if not opaque[t]:
                    continue
                for c in range(3):
                    s = sum(cur[((ty * SCALE + yy) * W + tx * SCALE + xx) * 3 + c]
                            for yy in range(SCALE) for xx in range(SCALE))
                    res[t * 3 + c] = orig[t * 4 + 2 - c] - s / (SCALE * SCALE)  # orig is BGRA
        for y in range(H):
            for x in range(W):
                if not opaque[(y // SCALE) * w0 + x // SCALE]:
                    continue
                for c in range(3):
                    v = sum(fy * fx * res[(jy * w0 + jx) * 3 + c] for jy, fy in wy[y] for jx, fx in wx[x])
                    i = (y * W + x) * 3 + c
                    cur[i] = min(255.0, max(0.0, cur[i] + v))
    out = bytearray(ai3)
    for p in range(W * H):
        for c in range(3):
            a = ai3[p * 4 + c]
            out[p * 4 + c] = int(a + strength * (cur[p * 3 + c] - a) + 0.5)
    return out


def composite(w0: int, h0: int, orig: bytes, ai3: bytes, backproject_strength: float = BACKPROJECT) -> bytearray:
    """3x premultiplied BGRA from the original (premultiplied BGRA, w0 x h0) and the
    upscaler's result at 3x (straight RGBA). See the module docstring."""
    if backproject_strength:
        ai3 = backproject(w0, h0, orig, ai3, backproject_strength)
    W = w0 * SCALE
    soft = bytearray(w0 * h0)  # texel with a partially transparent pixel in its 3x3 neighbourhood
    for y in range(h0):
        for x in range(w0):
            if any(0 < orig[(yy * w0 + xx) * 4 + 3] < 255
                   for yy in range(max(0, y - 1), min(h0, y + 2)) for xx in range(max(0, x - 1), min(w0, x + 2))):
                soft[y * w0 + x] = 1
    out = bytearray(W * h0 * SCALE * 4)
    for y in range(h0 * SCALE):
        for x in range(W):
            sx, sy = x // SCALE, y // SCALE
            if orig[(sy * w0 + sx) * 4 + 3] == 0:
                continue
            o = (y * W + x) * 4
            if soft[sy * w0 + sx]:
                # bilinear over the texels that are not empty: an empty one would fade the
                # edge, and where two pieces of an object meet the piece below shows through
                fx, fy = (x + 0.5) / SCALE - 0.5, (y + 0.5) / SCALE - 0.5
                x0, y0 = min(max(int(fx // 1), 0), w0 - 1), min(max(int(fy // 1), 0), h0 - 1)
                x1, y1 = min(w0 - 1, x0 + 1), min(h0 - 1, y0 + 1)
                tx, ty = min(max(fx - x0, 0.0), 1.0), min(max(fy - y0, 0.0), 1.0)
                taps = [(t, f) for t, f in (((y0 * w0 + x0) * 4, (1 - tx) * (1 - ty)), ((y0 * w0 + x1) * 4, tx * (1 - ty)),
                                            ((y1 * w0 + x0) * 4, (1 - tx) * ty), ((y1 * w0 + x1) * 4, tx * ty))
                        if orig[t + 3]]
                total = 0.0  # added one by one: sum() of floats rounds differently from Python 3.12
                for _t, f in taps:
                    total += f
                for c in range(4):
                    v = 0.0
                    for t, f in taps:
                        v += orig[t + c] * f
                    out[o + c] = int(v / total + 0.5)
            else:  # fully opaque area: upscaler colour
                i = (y * W + x) * 4
                out[o:o + 4] = bytes((ai3[i + 2], ai3[i + 1], ai3[i], 255))
    return out


def composite_one(orig: Path, ai_png: Path, out: Path) -> None:
    w0, h0 = size_of(orig.stem)
    w, h, rgba = read_png(ai_png)
    s = w // w0
    if (w, h) != (w0 * s, h0 * s) or s < SCALE:
        raise ValueError(f"{ai_png.name}: {w}x{h} is not an integer upscale >= {SCALE} of {w0}x{h0}")
    ai3 = rgba if s == SCALE else shrink(w, h, rgba, w0 * SCALE, h0 * SCALE)
    out.write_bytes(bytes(composite(w0, h0, orig.read_bytes(), ai3)))


# ---- CLI -----------------------------------------------------------------------------

def run_all(jobs: list[tuple], fn) -> int:
    done = 0
    with ProcessPoolExecutor(max_workers=os.cpu_count()) as pool:
        for i, _ in enumerate(pool.map(fn, *zip(*jobs), chunksize=16) if jobs else []):
            done = i + 1
            if done % 2000 == 0:
                print(f"  {done}/{len(jobs)}", flush=True)
    return done


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[0] not in ("prep", "composite") or (argv[0] == "composite" and len(argv) < 4):
        print(__doc__)
        return 2
    orig_dir = Path(argv[1])
    if argv[0] == "prep":
        out = Path(argv[2])
        out.mkdir(parents=True, exist_ok=True)
        jobs = [(p, out / f"{p.stem}.png") for p in sorted(orig_dir.glob("*.bgra"))
                if not (out / f"{p.stem}.png").exists()]
        print(f"prep: {run_all(jobs, prep_one)} new PNGs in {out}")
    else:
        ai_dir, out = Path(argv[2]), Path(argv[3])
        out.mkdir(parents=True, exist_ok=True)
        jobs = [(orig_dir / f"{p.stem}.bgra", p, out / f"{p.stem}@{SCALE}.bgra") for p in sorted(ai_dir.glob("*.png"))
                if (orig_dir / f"{p.stem}.bgra").exists() and not (out / f"{p.stem}@{SCALE}.bgra").exists()]
        print(f"composite: {run_all(jobs, composite_one)} new HD textures in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
