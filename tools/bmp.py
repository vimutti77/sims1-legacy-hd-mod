"""Reader for Windows BMP files as The Sims uses them: 24-bit, and 8-bit plain or RLE8
(catalogue pictures in object files, UI bitmaps in UIGraphics.far).

Usage:
    python tools/bmp.py <file.bmp>     size and format
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path


class FormatError(ValueError):
    pass


def decode_bmp(b: bytes) -> tuple[int, int, bytes]:
    """(w, h, RGBA bytes, top row first) of a 24-bit or 8-bit (plain or RLE8) BMP."""
    try:
        if b[:2] != b"BM":
            raise FormatError("not a BMP")
        (off,) = struct.unpack_from("<I", b, 10)
        hsize, w, h, _planes, bpp, comp = struct.unpack_from("<IiiHHI", b, 14)
        if not (bpp == 24 and comp == 0 or bpp == 8 and comp in (0, 1)) or not (0 < w <= 4096 and 0 < abs(h) <= 4096):
            raise FormatError(f"unsupported BMP: {w}x{h}, {bpp} bpp, compression {comp}")
        top_down, h = h < 0, abs(h)
        pal = []
        if bpp == 8:
            (ncol,) = struct.unpack_from("<I", b, 46)
            p0 = 14 + hsize
            pal = [(b[p0 + i * 4 + 2], b[p0 + i * 4 + 1], b[p0 + i * 4]) for i in range(min(ncol or 256, 256))]
        if comp == 1:
            rows = _rle8_rows(b, off, w, h)
        else:
            stride = ((w * bpp + 31) // 32) * 4
            if off + stride * h > len(b):
                raise FormatError("BMP pixels truncated")
            rows = [b[off + y * stride:off + y * stride + w * bpp // 8] for y in range(h)]
    except (struct.error, IndexError) as e:
        raise FormatError(f"BMP truncated: {e}") from None
    px = bytearray(w * h * 4)
    for y in range(h):
        row = rows[y if top_down else h - 1 - y]
        o = y * w * 4
        for x in range(w):
            if bpp == 24:
                bl, g, r = row[x * 3:x * 3 + 3]
            else:
                r, g, bl = pal[row[x]] if row[x] < len(pal) else (0, 0, 0)
            px[o + x * 4:o + x * 4 + 4] = bytes((r, g, bl, 255))
    return w, h, bytes(px)


def _rle8_rows(b: bytes, pos: int, w: int, h: int) -> list[bytearray]:
    """Rows (in file order, bottom-up) of an RLE8 bitmap."""
    rows = [bytearray(w) for _ in range(h)]
    x = y = 0
    while pos + 1 < len(b) and y < h:
        n, v = b[pos], b[pos + 1]
        pos += 2
        if n:  # n pixels of colour v
            for _ in range(n):
                if x < w:
                    rows[y][x] = v
                x += 1
        elif v == 0:  # end of row
            x, y = 0, y + 1
        elif v == 1:  # end of bitmap
            break
        elif v == 2:  # move
            x, y = x + b[pos], y + b[pos + 1]
            pos += 2
        else:  # v literal pixels, padded to even
            for i in range(v):
                if x < w:
                    rows[y][x] = b[pos + i]
                x += 1
            pos += v + (v & 1)
    return rows


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    try:
        w, h, _ = decode_bmp(Path(argv[0]).read_bytes())
    except FormatError as e:
        print(e)
        return 1
    print(f"{w}x{h}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
