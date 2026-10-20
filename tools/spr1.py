"""Reader for The Sims 1 SPR# sprites (the older format: wallpapers, wall styles and fences in
GameData/walls.iff and the .wll files) and Legacy's texture conversion of them.

SPR# chunk (little-endian):
    u32 version (504), u32 frame count, u32 palette id (0xFFFFFFFF: none given),
    u32 offset of each frame
    frame: u32 reserved, u16 height, u16 width, then commands of a byte and a count byte:
        0x04 n   a row of n bytes (this command included), holding pixel commands:
                   0x01 n  skip n pixels
                   0x02 n  n pixels of one colour index (the index, then a pad byte)
                   0x03 n  n pixels of colour indexes, padded to an even length
                   0x05    end of the row
        0x09 n   skip n rows
        0x05     end of the sprite
        0x00, 0x08  nothing
Palette: a wallpaper chunk of walls.iff labelled "N:Z(id)" uses PALT 1536+N (its own palette
id is 0xFFFFFFFF); a .wll file uses its palette id, or its only PALT.

Legacy uploads a frame as B8G8R8A8: colour -> RGB565, alpha 255 where the frame has a pixel
(checked against the textures of the running game). In-lot walls are uploaded with their empty
corners filled (the wall top above, the bottom row repeated below), so only the pixels the frame
covers are the frame's.

Usage:
    py tools/spr1.py <file.iff|file.wll>     list the SPR# chunks and their frames
"""
from __future__ import annotations

import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import iff
import spr2

NO_PALETTE = 0xFFFFFFFF
WALLPAPER_PALETTE_BASE = 1536


class Spr1Error(ValueError):
    pass


@dataclass
class Frame:
    w: int
    h: int
    pixels: list  # per pixel, row by row: colour index, or None where nothing is drawn


def frame_offsets(chunk: bytes) -> tuple[int, list[int]]:
    """(palette id, offset of each frame)."""
    if len(chunk) < 12:
        raise Spr1Error("SPR# header truncated")
    _version, count, palette = struct.unpack_from("<III", chunk, 0)
    if 12 + 4 * count > len(chunk):
        raise Spr1Error("SPR# offset table truncated")
    offsets = list(struct.unpack_from(f"<{count}I", chunk, 12))
    if any(o + 8 > len(chunk) for o in offsets):
        raise Spr1Error("SPR# frame offset out of range")
    return palette, offsets


def decode_frame(chunk: bytes, offset: int) -> Frame:
    if offset + 8 > len(chunk):
        raise Spr1Error("frame header out of range")
    _reserved, h, w = struct.unpack_from("<IHH", chunk, offset)
    px: list = [None] * (w * h)
    pos, y, end = offset + 8, 0, len(chunk)
    while pos + 2 <= end:
        cmd, n = chunk[pos], chunk[pos + 1]
        pos += 2
        if cmd in (0x00, 0x08):
            continue
        if cmd == 0x05:
            break
        if cmd == 0x09:
            y += n
            continue
        if cmd != 0x04:
            raise Spr1Error(f"unknown command {cmd:#x}")
        row_end, x = min(pos - 2 + n, end), 0
        while pos + 2 <= row_end:
            rcmd, rn = chunk[pos], chunk[pos + 1]
            pos += 2
            if rcmd == 0x01:
                x += rn
            elif rcmd == 0x02:
                if pos + 2 > row_end:
                    raise Spr1Error("fill past the end of its row")
                v = chunk[pos]
                pos += 2
                for _ in range(rn):
                    if x < w and y < h:
                        px[y * w + x] = v
                    x += 1
            elif rcmd == 0x03:
                if pos + rn > row_end:
                    raise Spr1Error("pixel run past the end of its row")
                for i in range(rn):
                    if x < w and y < h:
                        px[y * w + x] = chunk[pos + i]
                    x += 1
                pos += rn + (rn & 1)
            elif rcmd == 0x05:
                break
            else:
                raise Spr1Error(f"unknown row command {rcmd:#x}")
        pos = max(pos, row_end)
        y += 1
    return Frame(w, h, px)


def game_texture(frame: Frame, palette: list) -> bytes:
    """The frame as Legacy uploads it: B8G8R8A8 through RGB565, alpha 255 where it has a pixel."""
    out = bytearray(frame.w * frame.h * 4)
    for i, v in enumerate(frame.pixels):
        if v is None:
            continue
        r, g, b = palette[v] if v < len(palette) else (0, 0, 0)
        out[i * 4:i * 4 + 4] = bytes((b & 0xF8, g & 0xFC, r & 0xF8, 255))
    return bytes(out)


def chunk_palette(file_name: str, chunk: iff.IffChunk, palette_id: int, pals: dict) -> Optional[list]:
    """The palette a wall file's SPR# chunk is drawn with, or None (wall styles have none)."""
    if Path(file_name).name.lower() == "walls.iff":
        m = re.match(r"(\d+):\d+\(", chunk.label)  # wallpapers only, not "N:Z:K" wall styles
        return pals.get(WALLPAPER_PALETTE_BASE + int(m.group(1))) if m else None
    if palette_id != NO_PALETTE and palette_id in pals:
        return pals[palette_id]
    return next(iter(pals.values())) if len(pals) == 1 else None


def frames(file_name: str, data: bytes):
    """(chunk, frame index, Frame, palette or None) of every decodable SPR# frame of one file."""
    chunks = iff.read_iff(data)
    pals = spr2.read_palettes(data, chunks)
    for c in chunks:
        if c.type != "SPR#":
            continue
        body = data[c.offset:c.offset + c.size]
        try:
            palette_id, offsets = frame_offsets(body)
        except Spr1Error:
            continue
        pal = chunk_palette(file_name, c, palette_id, pals)
        for i, off in enumerate(offsets):
            try:
                yield c, i, decode_frame(body, off), pal
            except Spr1Error:
                continue


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for name in argv:
        path = Path(name)
        n = 0
        for c, i, f, pal in frames(path.name, path.read_bytes()):
            if i == 0:
                print(f"SPR# {c.id} {c.label!r}: palette {'yes' if pal else 'none'}")
            print(f"  frame {i}: {f.w}x{f.h}")
            n += 1
        print(f"{n} frames")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
