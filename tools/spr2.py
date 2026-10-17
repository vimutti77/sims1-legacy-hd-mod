"""Reader for The Sims 1 SPR2 sprites and Legacy's texture conversion.

SPR2 chunk (little-endian, after FreeSO's documentation):
    u32 version
      1000: u32 frame count, u32 palette id, u32 offset of each frame
      1001: u32 palette id, u32 frame count, then the frames back to back,
            each prefixed by u32 version and u32 size
    frame: u16 width, u16 height, u32 flags (1 colour, 2 z-buffer, 4 alpha),
           u16 palette id, u16 transparent index, u16 y offset, u16 x offset,
           then rows; each u16 marker is command << 13 | count:
        0  a row: count bytes including the marker, holding pixel runs
             1  count pixels of (z, colour index)
             2  count pixels of (z, colour index, alpha 0-31), padded to even
             3  count transparent pixels
             6  count pixels of colour index, padded to even
        4  skip count rows
        5  end of the sprite
PALT chunk (palette): u32 version, u32 entry count, 8 reserved bytes, R G B entries.

Legacy uploads each frame as a premultiplied B8G8R8A8 texture (checked byte for
byte against 1,245 uploads of the running game): colour -> RGB565, times alpha
with (c * a + 128) // 255, -> RGB565 again; alpha = 5-bit alpha * 8, 255 for
pixels without alpha, 0 where nothing is drawn. vkproxy recognises a texture
by "<w>x<h>_<FNV-1a 64 of those bytes>" (content_name).

Usage:
    python tools/spr2.py <file.iff|archive.far>                     list sprites
    python tools/spr2.py <file.iff|archive.far>... --export <dir>   write every frame as
                                                                     <dir>/<content name>.bgra
"""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import far
import iff


class Spr2Error(ValueError):
    pass


@dataclass
class Frame:
    w: int
    h: int
    flags: int
    palette_id: int
    transparent: int
    # per pixel, row by row: None (nothing drawn) or (colour index, alpha 0-31 or None)
    pixels: list


def read_palettes(data: bytes, chunks: list[iff.IffChunk]) -> dict[int, list[tuple[int, int, int]]]:
    pals = {}
    for c in chunks:
        if c.type == "PALT" and c.size >= 16:
            (n,) = struct.unpack_from("<I", data, c.offset + 4)
            n = min(n, 256, (c.size - 16) // 3)
            base = c.offset + 16
            pals[c.id] = [tuple(data[base + i * 3:base + i * 3 + 3]) for i in range(n)]
    return pals


def frame_offsets(spr2: bytes) -> list[tuple[int, int]]:
    """(offset of each frame header, default palette id)."""
    if len(spr2) < 12:
        raise Spr2Error("SPR2 header truncated")
    (version,) = struct.unpack_from("<I", spr2, 0)
    if version == 1000:
        count, pal = struct.unpack_from("<II", spr2, 4)
        if 12 + 4 * count > len(spr2):
            raise Spr2Error("offset table truncated")
        return [(o, pal) for o in struct.unpack_from(f"<{count}I", spr2, 12)]
    if version == 1001:
        pal, count = struct.unpack_from("<II", spr2, 4)
        out, pos = [], 12
        for _ in range(count):
            if pos + 8 > len(spr2):
                raise Spr2Error("frame table truncated")
            (size,) = struct.unpack_from("<I", spr2, pos + 4)
            out.append((pos + 8, pal))
            pos += 8 + size
        return out
    raise Spr2Error(f"unknown SPR2 version {version}")


def decode_frame(spr2: bytes, offset: int) -> Frame:
    if offset + 16 > len(spr2):
        raise Spr2Error("frame header out of range")
    w, h, flags, pal, transparent, _y, _x = struct.unpack_from("<HHIHHHH", spr2, offset)
    px: list = [None] * (w * h)
    pos, y, end_of_data = offset + 16, 0, len(spr2)
    while pos + 2 <= end_of_data:
        (marker,) = struct.unpack_from("<H", spr2, pos)
        cmd, count = marker >> 13, marker & 0x1FFF
        pos += 2
        if cmd == 0:
            row_end, x = min(pos - 2 + count, end_of_data), 0
            while pos + 2 <= row_end:
                (run,) = struct.unpack_from("<H", spr2, pos)
                rcmd, n = run >> 13, run & 0x1FFF
                pos += 2
                if rcmd in (1, 2, 6):
                    size = {1: 2, 2: 3, 6: 1}[rcmd]
                    if pos + n * size > row_end:
                        raise Spr2Error("pixel run past the end of its row")
                    for _ in range(n):
                        if x < w and y < h:
                            px[y * w + x] = (spr2[pos + 1], spr2[pos + 2]) if rcmd == 2 else \
                                            (spr2[pos + 1], None) if rcmd == 1 else (spr2[pos], None)
                        pos += size
                        x += 1
                    pos += (n * size) % 2  # runs are padded to an even length
                elif rcmd == 3:
                    x += n
                else:
                    break
            pos = max(pos, row_end)
            y += 1
        elif cmd == 4:
            y += count
        elif cmd == 5:
            break
        else:
            raise Spr2Error(f"unknown row command {cmd}")
    return Frame(w, h, flags, pal, transparent, px)


def game_texture(frame: Frame, palette: list[tuple[int, int, int]]) -> bytes:
    """The frame as Legacy uploads it: premultiplied B8G8R8A8 through RGB565."""
    out = bytearray(frame.w * frame.h * 4)
    for i, p in enumerate(frame.pixels):
        if p is None:
            continue
        idx, a5 = p
        r, g, b = palette[idx] if idx < len(palette) else (0, 0, 0)
        r, g, b = r & 0xF8, g & 0xFC, b & 0xF8
        a = 255 if a5 is None else min(255, a5 * 8)
        if a != 255:
            r, g, b = (r * a + 128) // 255, (g * a + 128) // 255, (b * a + 128) // 255
        out[i * 4:i * 4 + 4] = bytes((b & 0xF8, g & 0xFC, r & 0xF8, a))
    return bytes(out)


CONTENT_HASH_BASIS = 1469598103934665603  # vkproxy's value; standard FNV-1a 64 uses 14695981039346656037


def content_name(w: int, h: int, data: bytes) -> str:
    """vkproxy's name for a texture's pixels: <w>x<h>_<hash, 16 hex digits>.

    The hash is FNV-1a 64 but with vkproxy's offset basis (the standard one
    without its last digit). Every dump and replacement file is named with it,
    so it must stay as it is.
    """
    v = CONTENT_HASH_BASIS
    for byte in data:
        v = ((v ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{w}x{h}_{v:016x}"


def iter_iffs(path: Path) -> Iterator[tuple[str, bytes]]:
    """(name, bytes) of an .iff file or of every .iff inside a FAR archive."""
    data = path.read_bytes()
    if data[:8] == far.MAGIC:
        for e in far.read_far(data):
            if e.name.lower().endswith(".iff"):
                yield f"{path.name}/{e.name}", data[e.offset:e.offset + e.size]
    else:
        yield path.name, data


def game_textures(path: Path) -> Iterator[tuple[str, int, int, bytes, str]]:
    """(content name, w, h, texture, where) for every decodable frame in path."""
    for name, data in iter_iffs(path):
        try:
            chunks = iff.read_iff(data)
        except iff.IffError:
            continue
        pals = read_palettes(data, chunks)
        for c in chunks:
            if c.type != "SPR2":
                continue
            spr2 = data[c.offset:c.offset + c.size]
            try:
                offsets = frame_offsets(spr2)
            except Spr2Error:
                continue
            for i, (off, default_pal) in enumerate(offsets):
                try:
                    f = decode_frame(spr2, off)
                except (Spr2Error, struct.error):
                    continue
                pal: Optional[list] = pals.get(f.palette_id) or pals.get(default_pal)
                if pal is None or f.w == 0 or f.h == 0:
                    continue
                tex = game_texture(f, pal)
                yield content_name(f.w, f.h, tex), f.w, f.h, tex, f"{name} SPR2#{c.id} frame {i}"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    out = None
    if "--export" in argv:
        i = argv.index("--export")
        out = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
        out.mkdir(parents=True, exist_ok=True)
    total = written = 0
    for src in argv:
        for name, w, h, tex, where in game_textures(Path(src)):
            total += 1
            if out is None:
                print(f"{name}  {where}")
            elif not (out / f"{name}.bgra").exists():
                (out / f"{name}.bgra").write_bytes(tex)
                written += 1
    print(f"{total} frames" + (f", {written} new files in {out}" if out else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
