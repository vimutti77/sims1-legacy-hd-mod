"""Reader for The Sims 1 bitmap fonts (.ffn, e.g. UIGraphics.far Fonts/variablesans_12.ffn).

Layout (little-endian, inferred from the Legacy files):
    0x00  b"FNTF"
    0x0A  u16 glyph count
    0x1C  u32 offset of the bitmap data
    0x20  glyph records, 11 bytes each:
              u16 char (cp1252) | u8 w | u8 h | u16 x | u16 y | i8 left | i8 right | i8 top
          (x, y) is the glyph's place in the sheet; top is its offset below the line top.
    data  16-byte grey palette, then a 4-bit sheet 256 pixels wide, high nibble first.

The game packs these glyphs into its own atlas texture at load time (see
font_atlas.layout).

Usage:
    python tools/ffn.py <file.ffn> [sheet.pgm]
"""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"FNTF"
SHEET_W = 256
RECORDS = 0x20
RECORD = struct.Struct("<HBBHHbbb")


class FfnError(ValueError):
    pass


@dataclass
class Glyph:
    w: int
    h: int
    x: int
    y: int
    left: int
    right: int
    top: int


@dataclass
class Font:
    glyphs: dict[int, Glyph]  # by character code
    palette: bytes            # 16 grey levels
    sheet: bytearray          # one 4-bit value per pixel, SHEET_W wide
    rows: int

    def pixels(self, code: int) -> list[list[int]]:
        """The glyph's 4-bit values, row by row."""
        g = self.glyphs[code]
        return [[self.sheet[(g.y + yy) * SHEET_W + g.x + xx] for xx in range(g.w)] for yy in range(g.h)]


def read_ffn(data: bytes) -> Font:
    if data[:4] != MAGIC:
        raise FfnError("not an FFN font")
    if len(data) < RECORDS:
        raise FfnError("header truncated")
    (count,) = struct.unpack_from("<H", data, 0x0A)
    (data_off,) = struct.unpack_from("<I", data, 0x1C)
    if RECORDS + count * RECORD.size > data_off or data_off + 16 > len(data):
        raise FfnError("glyph table or bitmap offset out of range")
    palette = data[data_off:data_off + 16]
    packed = data[data_off + 16:]
    rows = len(packed) // (SHEET_W // 2)
    sheet = bytearray(SHEET_W * rows)
    for i, b in enumerate(packed[:rows * SHEET_W // 2]):
        sheet[2 * i] = b >> 4
        sheet[2 * i + 1] = b & 15
    glyphs = {}
    for i in range(count):
        code, w, h, x, y, left, right, top = RECORD.unpack_from(data, RECORDS + i * RECORD.size)
        if x + w > SHEET_W or y + h > rows:
            raise FfnError(f"glyph {code} lies outside the sheet")
        glyphs[code] = Glyph(w, h, x, y, left, right, top)
    return Font(glyphs, palette, sheet, rows)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    font = read_ffn(Path(argv[0]).read_bytes())
    print(f"{len(font.glyphs)} glyphs, sheet {SHEET_W}x{font.rows}, palette {list(font.palette)}")
    if len(argv) > 1:
        grey = bytes(font.palette[v] for v in font.sheet)
        Path(argv[1]).write_bytes(b"P5 %d %d 255\n" % (SHEET_W, font.rows) + grey)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
