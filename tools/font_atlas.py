"""Build HD font atlases for vkproxy from the game's own bitmap fonts.

The game packs each VariableSans .ffn font into an atlas texture at load time.
vkproxy replaces a sampled texture of w x h with <w>x<h>@<scale>.bgra, so for
each font this tool computes the game's atlas layout (same size, same glyph
cells) and redraws every glyph `scale` times larger from a vector font
(Comic Sans MS, which VariableSans imitates) with Windows GDI:

- one GDI size per font: the largest size, at or below the one whose 'H' is as
  tall as the scaled 'H' cell, at which 85% of glyphs fit their cells on one
  shared baseline;
- glyphs drawn at 4x that size and area-averaged down (smooth, even strokes;
  GDI's own anti-aliasing grid-fits outlines and has only 4x4 subsamples);
- glyphs wider than their cell are condensed (GDI lfWidth), taller ones shrunk,
  and any that still miss the baseline are nudged into the cell.

Pixels are written as premultiplied white (b = g = r = a = coverage).

Only the plain fonts are built; the shadow variants (_s, _bs) and size 48 are
skipped, as are the per-language folders (Fonts/Polish, ...). Rendering needs
Windows (gdi32) and the Comic Sans MS font.

Usage:
    python tools/font_atlas.py <game_dir> <out_dir> [--scale N] [--font variablesans_12 ...]
    python tools/font_atlas.py <game_dir> --sizes        (print each font's atlas size)
Then copy <out_dir>/*.bgra to <game_dir>/vkproxy/replace/.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import far
import ffn

ATLAS_W = 1024
FACE = "Comic Sans MS"
SUPERSAMPLE = 4
FIT_SHARE = 0.85
FONT_MEMBER = re.compile(r"^Fonts[\\/](variablesans_\d+)\.ffn$", re.IGNORECASE)


@dataclass
class Ink:
    """A rendered glyph: coverage canvas plus the font's ascent (baseline row)."""
    w: int
    h: int
    cov: bytes
    ascent: int

    def bbox(self) -> Optional[tuple[int, int, int, int]]:
        xs = [i % self.w for i, v in enumerate(self.cov) if v]
        if not xs:
            return None
        ys = [i // self.w for i, v in enumerate(self.cov) if v]
        return min(xs), min(ys), max(xs) + 1, max(ys) + 1


# render(text, size, width) -> Ink: draw one character at GDI height `size`
# (lfHeight = -size) and average width `width` (0 = natural), origin x = size // 2.
Renderer = Callable[[str, int, int], Ink]


def layout(glyphs: dict[int, ffn.Glyph]) -> tuple[dict[int, tuple[int, int]], int, int]:
    """The game's atlas packing: glyph cells by code, atlas width and height.

    Glyphs go left to right in code order, skipping empty ones; a new row starts
    when x + w > 1024. The row step is the tallest glyph seen so far (never
    reset, and including the glyph that wraps); the height is the lowest bottom.
    Matches the game's atlases pixel for pixel.
    """
    x = y = step = width = height = 0
    cells = {}
    for code in sorted(glyphs):
        g = glyphs[code]
        if g.w == 0 or g.h == 0:
            continue
        step = max(step, g.h)
        if x + g.w > ATLAS_W:
            y += step
            x = 0
        cells[code] = (x, y)
        x += g.w
        width = max(width, x)
        height = max(height, y + g.h)
    return cells, width, height


def char_for(code: int) -> str:
    try:
        return bytes([code]).decode("cp1252")
    except (UnicodeDecodeError, ValueError):
        return chr(code)


@dataclass
class AtlasStats:
    size: int = 0
    baseline: int = 0
    fit: int = 0
    drawn: int = 0
    condensed: int = 0
    shrunk: int = 0
    nudged: int = 0


def build_atlas(font: ffn.Font, scale: int, render: Renderer) -> tuple[int, int, bytearray, AtlasStats]:
    """Coverage (one byte per pixel) of the scaled atlas, plus its 1x size."""
    gl = font.glyphs
    cells, aw, ah = layout(gl)
    W, H = aw * scale, ah * scale
    out = bytearray(W * H)
    st = AtlasStats()

    # Start at the size whose 'H' is as tall as the scaled 'H' cell.
    target = gl[ord("H")].h * scale
    size = 8
    for s in range(8, 400):
        bb = render("H", s, 0).bbox()
        if bb and bb[3] - bb[1] >= target:
            size = s
            break

    def evaluate(sz):
        # Baseline votes: each glyph that fits its cell votes for every baseline
        # (in scaled line coordinates) that keeps it inside the cell.
        inks, votes = {}, {}
        for code in cells:
            ch = char_for(code)
            if ch.isspace():
                continue
            ink = render(ch, sz, 0)
            inks[code] = ink
            g, bb = gl[code], ink.bbox()
            if not bb or bb[2] - bb[0] > g.w * scale:
                continue
            lo = scale * g.top + ink.ascent - bb[1]
            hi = scale * (g.top + g.h) + ink.ascent - bb[3]
            for b in range(lo, hi + 1):
                votes[b] = votes.get(b, 0) + 1
        best = max(votes, key=votes.get) if votes else 0
        return best, votes, inks

    for sz in range(size, max(int(size * 0.7), 1), -1):
        baseline, votes, inks = evaluate(sz)
        if votes.get(baseline, 0) >= FIT_SHARE * len(inks):
            break
    st.size, st.baseline, st.fit, st.drawn = sz, baseline, votes.get(baseline, 0), len(inks)

    for code, (cx, cy) in cells.items():
        if code not in inks:
            continue
        g = gl[code]
        cw, chh = g.w * scale, g.h * scale
        cell_top = scale * g.top
        ch = char_for(code)
        s, width = sz, 0
        ink = inks[code]
        bb = ink.bbox()
        for _ in range(60):
            if not bb:
                break
            fits_w = bb[2] - bb[0] <= cw
            if fits_w and bb[3] - bb[1] <= chh:
                break
            if not fits_w:  # too wide: condense via lfWidth (average char width)
                width = max(1, int(s * 0.45 * cw / (bb[2] - bb[0]))) if width == 0 else max(1, width - 1)
                st.condensed += 1
            else:  # too tall: shrink this glyph a little
                width = max(1, round(width * (s - 1) / s)) if width else 0
                s -= 1
                st.shrunk += 1
            ink = render(ch, s, width)
            bb = ink.bbox()
        if not bb:
            continue
        x0, y0, x1, y1 = bb
        # Sit on the shared baseline; nudge vertically into the cell if needed.
        top = baseline - ink.ascent + y0
        clamped = min(max(top, cell_top), cell_top + chh - (y1 - y0))
        st.nudged += clamped != top
        oy = cy * scale + (clamped - cell_top) - y0
        ox = cx * scale + (cw - (x1 - x0)) // 2 - x0
        for yy in range(y0, y1):
            for xx in range(x0, x1):
                v = ink.cov[yy * ink.w + xx]
                px, py = ox + xx, oy + yy
                if v and cx * scale <= px < cx * scale + cw and cy * scale <= py < cy * scale + chh:
                    out[py * W + px] = max(out[py * W + px], v)
    return aw, ah, out, st


def to_bgra(cov: bytes) -> bytes:
    return bytes(b for v in cov for b in (v, v, v, v))


def gdi_renderer(face: str = FACE, supersample: int = SUPERSAMPLE) -> Renderer:
    """Renderer drawing with Windows GDI at `supersample` x the size, shrunk with HALFTONE."""
    import ctypes as C
    import ctypes.wintypes as W

    gdi = C.WinDLL("gdi32")
    gdi.CreateFontW.restype = W.HANDLE
    gdi.CreateFontW.argtypes = [C.c_int] * 5 + [W.DWORD] * 8 + [W.LPCWSTR]
    gdi.CreateCompatibleDC.restype = W.HDC
    gdi.CreateCompatibleDC.argtypes = [W.HDC]
    gdi.CreateDIBSection.restype = W.HANDLE
    gdi.CreateDIBSection.argtypes = [W.HDC, C.c_void_p, W.UINT, C.POINTER(C.c_void_p), W.HANDLE, W.DWORD]
    gdi.SelectObject.restype = W.HANDLE
    gdi.SelectObject.argtypes = [W.HDC, W.HANDLE]
    gdi.DeleteObject.argtypes = [W.HANDLE]
    gdi.DeleteDC.argtypes = [W.HDC]
    gdi.SetTextColor.argtypes = [W.HDC, W.DWORD]
    gdi.SetBkMode.argtypes = [W.HDC, C.c_int]
    gdi.GetTextMetricsW.argtypes = [W.HDC, C.c_void_p]
    gdi.TextOutW.argtypes = [W.HDC, C.c_int, C.c_int, W.LPCWSTR, C.c_int]
    gdi.StretchBlt.argtypes = [W.HDC] + [C.c_int] * 4 + [W.HDC] + [C.c_int] * 4 + [W.DWORD]
    gdi.SetStretchBltMode.argtypes = [W.HDC, C.c_int]
    gdi.SetBrushOrgEx.argtypes = [W.HDC, C.c_int, C.c_int, C.c_void_p]
    gdi.GdiFlush.argtypes = []

    class BITMAPINFOHEADER(C.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                    ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                    ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                    ("biClrImportant", W.DWORD)]

    class TEXTMETRICW(C.Structure):
        _fields_ = [(n, W.LONG) for n in ("tmHeight", "tmAscent", "tmDescent", "tmInternalLeading",
                                         "tmExternalLeading", "tmAveCharWidth", "tmMaxCharWidth", "tmWeight",
                                         "tmOverhang", "tmDigitizedAspectX", "tmDigitizedAspectY")] + \
                   [(n, W.WCHAR) for n in ("tmFirstChar", "tmLastChar", "tmDefaultChar", "tmBreakChar")] + \
                   [(n, C.c_byte) for n in ("tmItalic", "tmUnderlined", "tmStruckOut", "tmPitchAndFamily",
                                           "tmCharSet")]

    def dib(w, h):
        dc = gdi.CreateCompatibleDC(None)
        bmi = BITMAPINFOHEADER(C.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = C.c_void_p()
        bmp = gdi.CreateDIBSection(dc, C.byref(bmi), 0, C.byref(bits), None, 0)
        gdi.SelectObject(dc, bmp)
        return dc, bmp, bits

    def render(ch: str, size: int, width: int) -> Ink:
        ss = supersample
        w, h = size * 3 + 8, size * 2 + 8
        big_dc, big_bmp, _ = dib(w * ss, h * ss)
        font = gdi.CreateFontW(-size * ss, width * ss, 0, 0, 400, 0, 0, 0, 0, 0, 0, 4, 0, face)  # 4: antialiased
        gdi.SelectObject(big_dc, font)
        gdi.SetTextColor(big_dc, 0xFFFFFF)
        gdi.SetBkMode(big_dc, 1)  # transparent
        tm = TEXTMETRICW()
        gdi.GetTextMetricsW(big_dc, C.byref(tm))
        gdi.TextOutW(big_dc, (size // 2) * ss, 0, ch, 1)
        small_dc, small_bmp, bits = dib(w, h)
        gdi.SetStretchBltMode(small_dc, 4)  # HALFTONE: area average
        gdi.SetBrushOrgEx(small_dc, 0, 0, None)
        gdi.StretchBlt(small_dc, 0, 0, w, h, big_dc, 0, 0, w * ss, h * ss, 0x00CC0020)  # SRCCOPY
        gdi.GdiFlush()
        raw = C.string_at(bits, w * h * 4)
        gdi.DeleteObject(font)
        for dc, bmp in ((big_dc, big_bmp), (small_dc, small_bmp)):
            gdi.DeleteDC(dc)
            gdi.DeleteObject(bmp)
        return Ink(w, h, raw[1::4], round(tm.tmAscent / ss))  # green channel = coverage

    return render


def game_fonts(game_dir: Path) -> dict[str, ffn.Font]:
    """Plain VariableSans fonts from UIGraphics.far, by name (e.g. variablesans_12)."""
    data = (game_dir / "UIGraphics" / "UIGraphics.far").read_bytes()
    fonts = {}
    for e in far.read_far(data):
        m = FONT_MEMBER.match(e.name)
        if m and not m.group(1).lower().endswith("_48"):
            fonts[m.group(1).lower()] = ffn.read_ffn(data[e.offset:e.offset + e.size])
    return fonts


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Build HD font atlases for vkproxy.")
    ap.add_argument("game_dir", type=Path)
    ap.add_argument("out_dir", nargs="?", type=Path)
    ap.add_argument("--scale", type=int, default=3, help="UI scale of the atlases (default 3, for 2160p)")
    ap.add_argument("--font", action="append", help="build only this font (repeatable)")
    ap.add_argument("--sizes", action="store_true", help="only print each font's atlas size")
    args = ap.parse_args(argv)
    fonts = game_fonts(args.game_dir)
    if args.sizes:
        for name, font in sorted(fonts.items()):
            _, w, h = layout(font.glyphs)
            print(f"{name:20} {w}x{h}")
        return 0
    if not args.out_dir:
        ap.error("out_dir is required unless --sizes")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    render = gdi_renderer()
    for name, font in sorted(fonts.items()):
        if args.font and name not in args.font:
            continue
        aw, ah, cov, st = build_atlas(font, args.scale, render)
        path = args.out_dir / f"{aw}x{ah}@{args.scale}.bgra"
        path.write_bytes(to_bgra(cov))
        print(f"{name}: {path.name}, GDI size {st.size}, {st.fit}/{st.drawn} glyphs fit on baseline "
              f"{st.baseline}, condensed {st.condensed}, shrunk {st.shrunk}, nudged {st.nudged}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
