"""Tests using small synthetic files (no game data needed).

Run:  python -m unittest discover -s tests
"""
import math
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import exe_dump  # noqa: E402
import exe_strings  # noqa: E402
import far  # noqa: E402
import ffn  # noqa: E402
import font_atlas  # noqa: E402
import iff  # noqa: E402
import hd_art  # noqa: E402
import hd_composite  # noqa: E402
import hd_floors  # noqa: E402
import hd_translucent  # noqa: E402
import hd_pack  # noqa: E402
import hd_shapes  # noqa: E402
import hd_skins  # noqa: E402
import hd_ui  # noqa: E402
import stairs  # noqa: E402
import hd_walls  # noqa: E402
import inventory  # noqa: E402
import spr1  # noqa: E402
import spr2  # noqa: E402


def make_far(files: dict[str, bytes], name_len_fmt: str = "<I") -> bytes:
    body = b""
    offsets = []
    for data in files.values():
        offsets.append(16 + len(body))
        body += data
    manifest = struct.pack("<I", len(files))
    for (name, data), off in zip(files.items(), offsets):
        n = name.encode("latin-1")
        manifest += struct.pack("<III", len(data), len(data), off) + struct.pack(name_len_fmt, len(n)) + n
    header = far.MAGIC + struct.pack("<II", 1, 16 + len(body))
    return header + body + manifest


def make_iff(chunks: list[tuple[str, int, str, bytes]], version: bytes = b"2.5") -> bytes:
    sig = b"IFF FILE " + version + b":TYPE FOLLOWED BY SIZE\0 JAMIE DOORNBOS & MAXIS 1"
    sig = sig.ljust(60, b"\0")
    out = sig + struct.pack(">I", 0)
    for ctype, cid, label, data in chunks:
        out += ctype.encode() + struct.pack(">IHH", 76 + len(data), cid, 0x10)
        out += label.encode().ljust(64, b"\0") + data
    return out


def make_ffn(glyphs: dict[int, tuple[int, int, int, int, int]], sheet_rows: int = 16) -> bytes:
    """FFN with glyphs {code: (w, h, x, y, top)}; every glyph pixel has value 15."""
    records = b"".join(ffn.RECORD.pack(code, w, h, x, y, 1, 1, top) for code, (w, h, x, y, top) in glyphs.items())
    data_off = ffn.RECORDS + len(records)
    header = bytearray(ffn.RECORDS)
    header[:4] = ffn.MAGIC
    struct.pack_into("<H", header, 0x0A, len(glyphs))
    struct.pack_into("<I", header, 0x1C, data_off)
    sheet = bytearray(ffn.SHEET_W * sheet_rows)
    for w, h, x, y, _ in glyphs.values():
        for yy in range(h):
            for xx in range(w):
                if (y + yy) < sheet_rows:  # a glyph may deliberately overhang the sheet
                    sheet[(y + yy) * ffn.SHEET_W + x + xx] = 15
    packed = bytes((sheet[i] << 4) | sheet[i + 1] for i in range(0, len(sheet), 2))
    palette = bytes(17 * i for i in range(16))
    return bytes(header) + records + palette + packed


def box_renderer(ch: str, size: int, width: int) -> font_atlas.Ink:
    """Fake renderer: every character is a solid box size tall and width (or size // 2) wide,
    with its top 2 px below the canvas top and the baseline at its bottom."""
    w, h = size * 3 + 8, size * 2 + 8
    bw = width or size // 2
    x0 = size // 2
    cov = bytearray(w * h)
    for y in range(2, 2 + size):
        cov[y * w + x0:y * w + x0 + bw] = b"\xff" * bw
    return font_atlas.Ink(w, h, bytes(cov), size + 2)


class FfnTests(unittest.TestCase):
    def test_reads_glyphs_and_pixels(self):
        font = ffn.read_ffn(make_ffn({ord("A"): (3, 4, 5, 2, 1), ord("B"): (2, 2, 0, 8, 0)}))
        self.assertEqual(sorted(font.glyphs), [ord("A"), ord("B")])
        a = font.glyphs[ord("A")]
        self.assertEqual((a.w, a.h, a.x, a.y, a.left, a.right, a.top), (3, 4, 5, 2, 1, 1, 1))
        self.assertEqual(font.pixels(ord("A")), [[15] * 3] * 4)
        self.assertEqual(font.sheet[2 * ffn.SHEET_W + 4], 0)  # just left of A
        self.assertEqual(font.palette[15], 255)

    def test_rejects_bad_files(self):
        good = make_ffn({ord("A"): (3, 4, 0, 0, 0)}, sheet_rows=4)
        with self.assertRaises(ffn.FfnError):
            ffn.read_ffn(b"XXXX" + good[4:])
        with self.assertRaises(ffn.FfnError):
            ffn.read_ffn(good[:40])  # bitmap offset past the end
        with self.assertRaises(ffn.FfnError):  # glyph below the sheet
            ffn.read_ffn(make_ffn({ord("A"): (3, 4, 0, 2, 0)}, sheet_rows=4))


class FontAtlasTests(unittest.TestCase):
    def test_layout_matches_game_packer(self):
        g = lambda w, h: ffn.Glyph(w, h, 0, 0, 1, 1, 0)  # noqa: E731
        glyphs = {1: g(600, 10), 2: g(300, 20), 3: g(200, 5), 4: g(0, 9), 5: g(900, 5)}
        cells, w, h = font_atlas.layout(glyphs)
        # 3 wraps (900 + 200 > 1024) by the tallest glyph so far (20); 5 wraps again
        # by 20, not by the 5 of its own row: the step is never reset. 4 is empty.
        self.assertEqual(cells, {1: (0, 0), 2: (600, 0), 3: (0, 20), 5: (0, 40)})
        self.assertEqual((w, h), (900, 45))

    def test_build_atlas_draws_inside_cells(self):
        # Seven 4x6 glyphs and one narrow 2x6 'm': 7/8 fit at full size (>= 85%),
        # so the size stays at the 'H' match and 'm' alone is condensed.
        glyphs = {ord(c): (4, 6, 4 * i, 0, 0) for i, c in enumerate("Habcdef")}
        glyphs[ord("m")] = (2, 6, 28, 0, 0)
        font = ffn.read_ffn(make_ffn(glyphs))
        aw, ah, cov, st = font_atlas.build_atlas(font, 3, box_renderer)
        self.assertEqual((aw, ah), (30, 6))
        W = aw * 3
        self.assertEqual(len(cov), W * 18)
        self.assertEqual((st.size, st.baseline, st.fit, st.drawn), (18, 18, 7, 8))
        cells, _, _ = font_atlas.layout(font.glyphs)

        def ink_in(code):
            (cx, cy), g = cells[code], font.glyphs[code]
            return [(x, y) for y in range(cy * 3, (cy + g.h) * 3) for x in range(cx * 3, (cx + g.w) * 3)
                    if cov[y * W + x]]

        h = ink_in(ord("H"))
        self.assertEqual(len(h), 9 * 18)  # 9-wide box, centred in the 12-wide cell, full height
        self.assertEqual(min(x for x, _ in h), 1)
        m = ink_in(ord("m"))  # 9 wide at this size, 6-wide cell: condensed to fit
        self.assertTrue(m and max(x for x, _ in m) - min(x for x, _ in m) + 1 <= 6)
        self.assertEqual(st.condensed, 1)
        self.assertEqual(st.nudged, 0)
        self.assertEqual(sum(1 for v in cov if v), sum(len(ink_in(ord(c))) for c in "Habcdefm"))

    def test_to_bgra_is_premultiplied_white(self):
        self.assertEqual(font_atlas.to_bgra(bytes([0, 128])), bytes([0, 0, 0, 0, 128, 128, 128, 128]))

    @unittest.skipUnless(sys.platform == "win32", "GDI rendering needs Windows")
    def test_gdi_renderer_supersamples(self):
        ink = font_atlas.gdi_renderer()("H", 24, 0)
        self.assertIsNotNone(ink.bbox())
        self.assertGreater(ink.ascent, 0)
        self.assertGreater(len(set(ink.cov)), 17)  # more grey levels than GDI's 4x4 anti-aliasing


def spr2_frame_body() -> bytes:
    """A 3x3 frame: row 0 = colour 1, colour 2, gap; row 1 skipped;
    row 2 = colour 3 at alpha 10, colour 1 with z, gap."""
    def run(cmd, n, payload=b""):
        return struct.pack("<H", cmd << 13 | n) + payload

    row0 = run(6, 2, bytes([1, 2])) + run(3, 1)
    row2 = run(2, 1, bytes([0, 3, 10, 0])) + run(1, 1, bytes([0, 1]))  # (z, colour, alpha) padded
    rows = run(0, 2 + len(row0), row0) + run(4, 1) + run(0, 2 + len(row2), row2) + run(5, 0)
    return struct.pack("<HHIHHHH", 3, 3, 7, 1, 0, 0, 0) + rows


def make_spr2(version: int) -> bytes:
    body = spr2_frame_body()
    if version == 1000:
        return struct.pack("<IIII", 1000, 1, 1, 16) + body
    return struct.pack("<IIIII", 1001, 1, 1, 1001, len(body)) + body


PALETTE = [(0, 0, 0), (255, 255, 255), (100, 150, 200), (84, 10, 10)]


class Spr2Tests(unittest.TestCase):
    EXPECTED = bytes([248, 252, 248, 255, 200, 148, 96, 255, 0, 0, 0, 0] + [0] * 12
                     # colour 3 (84, 10, 10) at alpha 80: 565, premultiplied, 565 again
                     + [0, 0, 24, 80, 248, 252, 248, 255, 0, 0, 0, 0])

    def test_both_versions_decode_to_the_game_texture(self):
        for version in (1000, 1001):
            data = make_spr2(version)
            (off, pal), = spr2.frame_offsets(data)
            frame = spr2.decode_frame(data, off)
            self.assertEqual((frame.w, frame.h, pal), (3, 3, 1))
            self.assertEqual(spr2.game_texture(frame, PALETTE), self.EXPECTED)

    def test_content_name(self):
        self.assertEqual(spr2.content_name(1, 1, b""), "1x1_14650fb0739d0383")  # vkproxy's offset basis
        self.assertRegex(spr2.content_name(3, 3, self.EXPECTED), r"^3x3_[0-9a-f]{16}$")

    def test_game_textures_reads_an_iff(self):
        palt = struct.pack("<II", 1, len(PALETTE)) + b"\0" * 8 + bytes(c for rgb in PALETTE for c in rgb)
        data = make_iff([("PALT", 1, "pal", palt), ("SPR2", 100, "sprite", make_spr2(1000))])
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "obj.iff")
            p.write_bytes(data)
            (name, w, h, tex, where), = spr2.game_textures(p)
        self.assertEqual((w, h, tex), (3, 3, self.EXPECTED))
        self.assertEqual(name, spr2.content_name(3, 3, self.EXPECTED))
        self.assertIn("SPR2#100", where)

    def test_spr2_palette_wins_over_the_frame_palette(self):
        def palt(colours):
            return struct.pack("<II", 1, len(colours)) + b"\0" * 8 + bytes(c for rgb in colours for c in rgb)

        data = bytearray(make_spr2(1000))
        struct.pack_into("<H", data, 16 + 8, 2)  # the frame header names palette 2
        other = [(0, 0, 0), (0, 255, 0), (0, 255, 0), (0, 255, 0)]
        iff_data = make_iff([("PALT", 1, "spr2's", palt(PALETTE)), ("PALT", 2, "frame's", palt(other)),
                             ("SPR2", 100, "sprite", bytes(data))])
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "obj.iff")
            p.write_bytes(iff_data)
            (_name, _w, _h, tex, _where), = spr2.game_textures(p)
        self.assertEqual(tex, self.EXPECTED)  # drawn with PALT 1, the SPR2 chunk's id
        frame = spr2.decode_frame(bytes(data), 16)
        self.assertEqual(spr2.frame_palette({2: other}, frame, 1), other)  # SPR2 id missing: frame id

    def test_rejects_broken_data(self):
        with self.assertRaises(spr2.Spr2Error):
            spr2.frame_offsets(b"\x00\x01")
        with self.assertRaises(spr2.Spr2Error):
            spr2.frame_offsets(struct.pack("<III", 999, 0, 0))
        broken = bytearray(make_spr2(1000))
        struct.pack_into("<H", broken, 16 + 16 + 2, 6 << 13 | 200)  # a 200-pixel run in a short row
        with self.assertRaises(spr2.Spr2Error):
            spr2.decode_frame(bytes(broken), 16)


class HdCompositeTests(unittest.TestCase):
    def test_png_roundtrip_and_filtered_rows(self):
        rgba = bytes(range(2 * 2 * 4))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "a.png")
            hd_composite.write_png(p, 2, 2, rgba)
            self.assertEqual(hd_composite.read_png(p), (2, 2, bytearray(rgba)))
            # same pixels, rows stored with the Sub (1) and Up (2) filters
            r0, r1 = rgba[:8], rgba[8:]
            sub0 = r0[:4] + bytes((r0[4 + i] - r0[i]) & 255 for i in range(4))
            up1 = bytes((r1[i] - r0[i]) & 255 for i in range(8))
            raw = b"\x01" + sub0 + b"\x02" + up1
            ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)

            def chunk(t, d):
                return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

            p.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
            self.assertEqual(hd_composite.read_png(p)[2], bytearray(rgba))

    def test_to_straight(self):
        self.assertEqual(hd_composite.to_straight(bytes([10, 20, 30, 128, 1, 2, 3, 255, 9, 9, 9, 0])),
                         bytearray([60, 40, 20, 128, 3, 2, 1, 255, 9, 9, 9, 0]))

    def test_mirror_fill_reflects_across_the_edge(self):
        # one row: two solid pixels (red, green) then three transparent ones
        row = [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 0, 0), (0, 0, 0, 0), (0, 0, 0, 0)]
        out = hd_composite.mirror_fill(5, 1, bytearray(b for px in row for b in px))
        colours = [tuple(out[i * 4:i * 4 + 3]) for i in range(5)]
        # x=2 mirrors x=0 across x=1; beyond the picture the nearest solid colour is used
        self.assertEqual(colours, [(255, 0, 0), (0, 255, 0), (255, 0, 0), (0, 255, 0), (0, 255, 0)])
        self.assertEqual([out[i * 4 + 3] for i in range(5)], [255, 255, 0, 0, 0])  # alpha untouched

    def test_tile_fill_puts_the_neighbouring_tiles_around_a_floor_diamond(self):
        # a 15x8 floor diamond (w = 2h - 1), every pixel its own colour; the game lays the
        # next tiles (8, 4) and (-8, 4) away, so the corners are those tiles' pixels
        w, h = 15, 8

        def inside(x, y):
            return abs(x - 7) <= 2 * y if y < 4 else 2 * (y - 4) <= x <= 14 - 2 * (y - 4)

        rgba = bytearray(w * h * 4)
        for y in range(h):
            for x in range(w):
                if inside(x, y):
                    rgba[(y * w + x) * 4:(y * w + x + 1) * 4] = bytes((x * 16, y * 30, 7, 255))
        out = hd_composite.tile_fill(w, h, rgba)

        def colour(buf, x, y):
            return tuple(buf[(y * w + x) * 4:(y * w + x) * 4 + 3])

        self.assertEqual(colour(out, 14, 7), colour(rgba, 6, 3))  # lower right: the tile (8, 4) away
        self.assertEqual(colour(out, 0, 0), colour(rgba, 8, 4))   # upper left: the tile (-8, -4) away
        self.assertEqual(colour(out, 7, 5), colour(rgba, 7, 5))   # inside: untouched
        self.assertEqual(out[3::4], rgba[3::4])                   # alpha untouched
        # every corner pixel got a neighbour's colour, none a mirror image
        for y in range(h):
            for x in range(w):
                if not inside(x, y):
                    self.assertTrue(any(inside(x - dx, y - dy) and colour(out, x, y) == colour(rgba, x - dx, y - dy)
                                        for dx, dy in ((8, 4), (-8, 4), (8, -4), (-8, -4))), (x, y))
        # not a floor diamond: mirror fill, as for objects
        row = bytearray(b for px in [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 0, 0)] for b in px)
        self.assertEqual(hd_composite.tile_fill(3, 1, row), hd_composite.mirror_fill(3, 1, row))

    def test_composite_keeps_coverage_and_takes_colour_inside(self):
        # 3x1 original (premultiplied BGRA): opaque, opaque, transparent
        orig = bytes([10, 20, 30, 255, 10, 20, 30, 255, 0, 0, 0, 0])
        ai3 = bytes([200, 100, 50, 255] * 9 * 3)  # upscaler says: a different colour, opaque everywhere
        out = hd_composite.composite(3, 1, orig, ai3, backproject_strength=0)
        px = [tuple(out[i * 4:i * 4 + 4]) for i in range(9 * 3)]
        for y in range(3):
            row = px[y * 9:(y + 1) * 9]
            self.assertEqual(row[:6], [(50, 100, 200, 255)] * 6)  # AI colour, as BGRA, inside
            self.assertEqual(row[6:], [(0, 0, 0, 0)] * 3)  # never more coverage than the original

    def test_backprojection_pulls_the_colour_back_to_the_original(self):
        # 2x1 opaque original, red then blue (premultiplied BGRA); the upscaler made both grey
        orig = bytes([0, 0, 200, 255, 200, 0, 0, 255])
        ai3 = bytes([100, 100, 100, 255] * 6 * 3)

        def block_means(out):  # mean RGB of each texel's 3x3 block
            return [[sum(out[((y * 6) + tx * 3 + x) * 4 + c] for y in range(3) for x in range(3)) / 9
                     for c in range(3)] for tx in range(2)]

        full = block_means(hd_composite.backproject(2, 1, orig, ai3, 1.0, iters=20))
        half = block_means(hd_composite.backproject(2, 1, orig, ai3, 0.5, iters=20))
        for got, want in zip(full, ([200, 0, 0], [0, 0, 200])):
            self.assertTrue(all(abs(g - w) <= 2 for g, w in zip(got, want)), full)  # shrinks back to the original
        for got, want in zip(half, ([150, 50, 50], [50, 50, 150])):
            self.assertTrue(all(abs(g - w) <= 2 for g, w in zip(got, want)), half)  # halfway there
        self.assertEqual(hd_composite.backproject(2, 1, orig, ai3, 0.0), bytearray(ai3))

    def test_composite_samples_soft_areas_from_the_original(self):
        orig = bytes([0, 0, 0, 128, 0, 0, 0, 128])  # a 2x1 half-transparent shadow
        out = hd_composite.composite(2, 1, orig, bytes([255] * 6 * 3 * 4))
        self.assertTrue(all(out[i * 4:i * 4 + 4] == bytes([0, 0, 0, 128]) for i in range(18)))

    def test_soft_edges_do_not_fade_into_empty_texels(self):
        # 3x1: opaque grey, half-transparent grey, empty. The half-transparent texel keeps its
        # alpha up to its edge; an empty neighbour would have faded it (the piece below an
        # object's cut then showed through as a line).
        orig = bytes([100, 100, 100, 255, 50, 50, 50, 128, 0, 0, 0, 0])
        out = hd_composite.composite(3, 1, orig, bytes([100] * 9 * 3 * 4), backproject_strength=0)
        alpha = [out[i * 4 + 3] for i in range(9)]  # first row of the 9x3 result
        self.assertEqual(alpha[3:6], [170, 128, 128])  # blends towards the opaque side only
        self.assertEqual(alpha[6:], [0, 0, 0])

    def test_shrink_is_an_area_average(self):
        # 4x1 -> 3x1: half red, half blue
        src = bytes([255, 0, 0, 255] * 2 + [0, 0, 255, 255] * 2)
        out = hd_composite.shrink(4, 1, src, 3, 1)
        self.assertEqual(out[0:4], bytearray([255, 0, 0, 255]))
        self.assertTrue(all(abs(a - b) <= 1 for a, b in zip(out[4:8], [128, 0, 128, 255])), out[4:8])  # 50/50
        self.assertEqual(out[8:12], bytearray([0, 0, 255, 255]))


class HdArtTests(unittest.TestCase):
    def test_a_touched_up_texture(self):
        # built 4x1 texture: opaque grey, half-covered grey (a shadow), opaque grey, empty;
        # the edit paints all red and opaque, except that it erases the third pixel
        built = bytes([100, 100, 100, 255, 50, 50, 50, 128, 100, 100, 100, 255, 0, 0, 0, 0])
        with tempfile.TemporaryDirectory() as d:
            png = Path(d) / "e.png"
            hd_composite.write_png(png, 4, 1, bytes([200, 0, 0, 255, 200, 0, 0, 255, 200, 0, 0, 0, 200, 0, 0, 255]))
            # the shadow keeps its alpha, the erased pixel goes, the empty one is painted: grown
            self.assertEqual(hd_art.apply_edit(built, 4, 1, png),
                             (bytes([0, 0, 200, 255, 0, 0, 100, 128, 0, 0, 0, 0, 0, 0, 200, 255]), True))
            built = built[:8]
            # drawn twice as large: averaged down; nothing painted outside: not grown
            hd_composite.write_png(png, 4, 2, bytes([200, 0, 0, 255, 0, 0, 0, 255] * 4))
            self.assertEqual(hd_art.apply_edit(built, 2, 1, png), (bytes([0, 0, 100, 255, 0, 0, 50, 128]), False))
            hd_composite.write_png(png, 3, 1, bytes(12))
            with self.assertRaises(ValueError):
                hd_art.apply_edit(built, 2, 1, png)
            # exported for editing: straight colour, opaque where anything is drawn
            hd_art.export_edit(bytes([10, 20, 30, 255, 0, 0, 0, 0]), 2, 1, png)
            self.assertEqual(hd_composite.read_png(png)[2], bytearray([30, 20, 10, 255, 0, 0, 0, 0]))

    def test_depth_texture_and_its_twin(self):
        # 3x1 frame: a pixel at depth 10, one with no depth (255: the game discards it), none
        f = spr2.Frame(3, 1, 7, 0, 0, [(1, None), (1, None), None], [10, 255, 255])
        self.assertEqual(hd_art.depth_texture(f), bytes([10, 255, 0]))  # 0 where no pixel: nearest
        twin = hd_art.depth_twin(3, 1, bytes([1, 1, 0]), hd_art.depth_texture(f))
        self.assertEqual(len(twin), 9 * 3)
        for y in range(3):
            # 3x the original where it covers, one unit nearer (255 stays 255); past it, the
            # nearest depth (the depth-10 pixel: the 255 one has none to give), as near
            self.assertEqual(twin[y * 9:(y + 1) * 9], bytes([9] * 3 + [255] * 3 + [9] * 3))
        # grown below the original: nearer the lower it is, as the ground it stands on
        twin = hd_art.depth_twin(1, 2, bytes([1, 0]), bytes([20, 0]))
        self.assertEqual([twin[y * 3] for y in range(6)], [19, 19, 19, 18, 18, 17])


def make_spr1(frames: list[bytes], palette: int = spr1.NO_PALETTE) -> bytes:
    """SPR# chunk body: version 504, the frames' offsets, the frames."""
    head = struct.pack("<III", 504, len(frames), palette)
    pos = len(head) + 4 * len(frames)
    offsets = []
    for f in frames:
        offsets.append(pos)
        pos += len(f)
    return head + struct.pack(f"<{len(frames)}I", *offsets) + b"".join(frames)


class Spr1Tests(unittest.TestCase):
    # a 5x3 frame: row 0 skips 1 then fills 2 with index 7; row 1 is skipped; row 2 has
    # 3 literal indexes (padded to even), then ends
    FRAME = (struct.pack("<IHH", 0, 3, 5)
             + bytes([0x04, 2 + 2 + 2 + 2 + 2, 0x01, 1, 0x02, 2, 7, 0, 0x05, 0])
             + bytes([0x09, 1])
             + bytes([0x04, 2 + 2 + 4 + 2, 0x03, 3, 1, 2, 3, 0, 0x05, 0])
             + bytes([0x05, 0]))

    def test_decodes_rows_skips_and_runs(self):
        palette, offsets = spr1.frame_offsets(make_spr1([self.FRAME]))
        self.assertEqual(palette, spr1.NO_PALETTE)
        f = spr1.decode_frame(make_spr1([self.FRAME]), offsets[0])
        self.assertEqual((f.w, f.h), (5, 3))
        self.assertEqual(f.pixels, [None, 7, 7, None, None] + [None] * 5 + [1, 2, 3, None, None])

    def test_game_texture_is_rgb565_and_opaque(self):
        f = spr1.Frame(2, 1, [3, None])
        pal = [(0, 0, 0)] * 3 + [(0xFF, 0x87, 0x13)]
        self.assertEqual(spr1.game_texture(f, pal), bytes([0x10, 0x84, 0xF8, 255, 0, 0, 0, 0]))

    def test_wallpaper_palettes(self):
        pals = {1536 + 21: ["wallpaper 21"], 9: ["own"]}
        paper = iff.IffChunk("SPR#", 1066, 0, "21:3(2069)", 0, 0)
        style = iff.IffChunk("SPR#", 1066, 0, "21:3:0(1066)", 0, 0)
        self.assertEqual(spr1.chunk_palette("walls.iff", paper, spr1.NO_PALETTE, pals), ["wallpaper 21"])
        self.assertIsNone(spr1.chunk_palette("walls.iff", style, spr1.NO_PALETTE, pals))  # wall styles: none
        self.assertEqual(spr1.chunk_palette("Brick.wll", paper, 9, pals), ["own"])

    def test_frames_of_a_file(self):
        data = make_iff([("PALT", 1557, "", struct.pack("<IIII", 0, 8, 0, 0) + bytes(range(24))),
                         ("SPR#", 1066, "21:3(2069)", make_spr1([self.FRAME]))])
        [(c, i, f, pal)] = list(spr1.frames("walls.iff", data))
        self.assertEqual((c.id, i, f.w, f.h, len(pal)), (1066, 0, 5, 3, 8))

    def test_rejects_broken_data(self):
        with self.assertRaises(spr1.Spr1Error):
            spr1.frame_offsets(make_spr1([self.FRAME])[:14])
        with self.assertRaises(spr1.Spr1Error):  # a run longer than its row
            spr1.decode_frame(struct.pack("<IHH", 0, 1, 4) + bytes([0x04, 6, 0x03, 9, 1, 2]), 0)
        with self.assertRaises(spr1.Spr1Error):
            spr1.decode_frame(struct.pack("<IHH", 0, 1, 4) + bytes([0x07, 0]), 0)


class HdWallsTests(unittest.TestCase):
    def test_covered_hash_is_the_content_hash_of_the_covered_pixels(self):
        bgra = bytes(range(4 * 6))
        self.assertEqual("3x2_" + hd_walls.covered_hash(3, 2, bgra, bytes([1] * 6)),
                         spr2.content_name(3, 2, bgra))
        # pixels outside the mask (the corners the game fills) do not count
        other = bytearray(bgra)
        other[0:4] = b"\xff\xff\xff\xff"
        mask = bytes([0, 1, 1, 1, 1, 1])
        self.assertEqual(hd_walls.covered_hash(3, 2, bgra, mask), hd_walls.covered_hash(3, 2, bytes(other), mask))

    def test_a_wall_face_between_its_neighbours(self):
        # 4x4 face rising to the right: column x covers rows 2 - x // 2 .. 3 - x // 2 (+ 1)
        w = h = 4
        mask = bytearray(w * h)
        for x in range(w):
            for y in range(2 - x // 2, 4 - x // 2):
                mask[y * w + x] = 1
        self.assertGreater(hd_walls.slope(w, h, bytes(mask)), 0)
        bgra = bytearray(w * h * 4)
        for i in range(w * h):
            if mask[i]:
                bgra[i * 4:i * 4 + 4] = bytes((0, 0, i, 255))  # B, G, R, A
        cw, ch, rgba = hd_walls.wall_input(w, h, bytes(bgra), bytes(mask))
        self.assertEqual((cw, ch), (12, 8))

        def red(x, y):
            return rgba[(y * cw + x) * 4]

        # the frame sits at (w, w/2); its right neighbour w to the right and w/2 higher
        self.assertEqual(red(4 + 0, 2 + 2), 2 * w + 0)          # frame pixel (0, 2)
        self.assertEqual(red(8 + 0, 2 + 2 - 2), 2 * w + 0)      # the same pixel of the right neighbour
        self.assertEqual(red(0 + 3, 2 + 1 + 2), 1 * w + 3)      # pixel (3, 1) of the left neighbour

    def test_masks_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "2x1.wallmasks"
            hd_walls.write_masks(p, 2, 1, [b"\x01\x00", b"\x01\x01"])
            self.assertEqual(p.read_bytes(), b"WMSK" + struct.pack("<III", 2, 1, 2) + b"\x01\x00\x01\x01")


class HdShapesTests(unittest.TestCase):
    LINES = [(-0.5, 8.0), (0.5, -9.0), (0.5, 9.5), (-0.5, 26.0), (-0.5, 10.0), (0.5, -7.0), (0.5, 7.0), (-0.5, 24.0)]

    def band(self, w=34, h=18):
        tex = bytearray(w * h * 4)
        for y in range(h):
            for x in range(w):
                if hd_shapes.inside(self.LINES, x + 0.5, y + 0.5):
                    tex[(y * w + x) * 4:(y * w + x) * 4 + 4] = bytes((0, 252, 248, 255))
        return bytes(tex)

    def test_fitted_lines_give_the_sprite_back(self):
        tex = self.band()
        mask = hd_shapes.mask_of(tex, 34, 18)
        lines = hd_shapes.fit_band(mask)
        self.assertTrue(all(hd_shapes.inside(lines, x + 0.5, y + 0.5) == mask[y][x]
                            for y in range(18) for x in range(34)))

    def test_redraw_is_smooth_and_premultiplied(self):
        hd = hd_shapes.redraw(self.band(), 34, 18)
        self.assertEqual(len(hd), 34 * 3 * 18 * 3 * 4)
        alphas = set(hd[3::4])
        self.assertIn(255, alphas)
        self.assertTrue(any(0 < a < 255 for a in alphas))  # anti-aliased edges
        for i in range(0, len(hd), 4):
            self.assertLessEqual(hd[i + 1], hd[i + 3])  # colour never above alpha

    def test_rejects_other_shapes(self):
        tex = bytearray(self.band())
        tex[(9 * 34 + 17) * 4 + 3] = 255  # a pixel in the hole
        with self.assertRaises(hd_shapes.ShapeError):
            hd_shapes.redraw(bytes(tex), 34, 18)


class StairsTests(unittest.TestCase):
    W, H = 40, 24

    def strip(self):
        """A strip between two lines of slope 1/2, ended by a vertical cut: as a 1x sprite."""
        return bytes(1 if x + 0.5 < 30 and 0.5 * (x + 0.5) <= y + 0.5 <= 0.5 * (x + 0.5) + 6 else 0
                     for y in range(self.H) for x in range(self.W))

    def test_edges_become_straight_and_soft(self):
        m = self.strip()
        cov = stairs.upscale(m, self.W, self.H)
        W3 = self.W * 3
        self.assertEqual(len(cov), W3 * self.H * 3)
        self.assertTrue(any(0 < v < 255 for v in cov))  # anti-aliased edges
        # along the lower edge (inside above it) the coverage of each column falls once, smoothly
        x = 45  # a 3x column well inside the strip's length
        col = [cov[y * W3 + x] for y in range(self.H * 3)]
        rises = sum(1 for a, b in zip(col, col[1:]) if b > a)
        falls = sum(1 for a, b in zip(col, col[1:]) if b < a)
        self.assertLessEqual(rises, 2)
        self.assertLessEqual(falls, 2)
        # a straight edge: where coverage crosses half, one row per two 3x columns (slope 1/2)
        def half(xx):
            c = [cov[y * W3 + xx] for y in range(self.H * 3)]
            return max(y for y in range(self.H * 3) if c[y] >= 128)
        steps = [half(xx + 2) - half(xx) for xx in range(30, 60, 2)]
        self.assertTrue(all(s == 1 for s in steps), steps)

    def test_a_grown_wall_face_is_solid_under_the_wall_tops_soft_edge(self):
        face = bytes(1 if y + 0.5 >= 4 + 0.5 * (x + 0.5) else 0 for y in range(self.H) for x in range(self.W))
        top = bytes(1 - v for v in face)
        f, t = stairs.upscale(face, self.W, self.H, grow_top=0.4), stairs.upscale(top, self.W, self.H)
        soft = [i for i, v in enumerate(t) if 0 < v < 255]
        self.assertTrue(soft)
        self.assertTrue(all(f[i] == 255 for i in soft))
        self.assertTrue(all(f[i] > 0 or t[i] == 255 for i in range(len(f))))  # nothing open between them

    def test_marked_coverage_tells_added_pixels_apart(self):
        m = self.strip()
        plain, marked = stairs.upscale(m, self.W, self.H), stairs.upscale(m, self.W, self.H, marked=True)
        W3 = self.W * 3
        for i, (c, v) in enumerate(zip(plain, marked)):
            original = m[(i // W3 // 3) * self.W + (i % W3) // 3]
            self.assertEqual(v >= 128, bool(original))
            self.assertLessEqual(abs((v - 128 if original else v) * 255 / 127 - c), 2)
        self.assertEqual(stairs.mark(255, True), 255)
        self.assertEqual(stairs.mark(0, False), 0)

    def test_both_sides_of_an_edge_still_meet(self):
        m = self.strip()
        inv = bytes(1 - v for v in m)
        a, b = stairs.upscale(m, self.W, self.H), stairs.upscale(inv, self.W, self.H)
        self.assertTrue(all(abs(x + y - 255) <= 16 for x, y in zip(a, b)))


class HdTranslucentTests(unittest.TestCase):
    @staticmethod
    def sprite(alphas: list[int]) -> bytes:
        return b"".join(bytes((10, 20, 30, a)) for a in alphas)

    def test_mostly_partly_covered_sprites_count(self):
        self.assertTrue(hd_translucent.is_translucent(self.sprite([0, 0, 128, 128, 255])))  # 2 of 3 drawn
        self.assertTrue(hd_translucent.is_translucent(self.sprite([0, 100, 255])))  # half
        self.assertFalse(hd_translucent.is_translucent(self.sprite([0, 128, 255, 255])))  # 1 of 3
        self.assertFalse(hd_translucent.is_translucent(self.sprite([0, 0, 0])))  # nothing drawn

    def test_install_writes_the_list(self):
        with tempfile.TemporaryDirectory() as d:
            n = hd_translucent.install(Path(d), ["8x8_00aa", "4x24_7a35"])
            self.assertEqual(n, 2)
            text = (Path(d) / "vkproxy" / "replace" / hd_translucent.FILE).read_text(encoding="ascii")
            self.assertEqual(text.split(), ["8x8_00aa", "4x24_7a35"])


class HdFloorsTests(unittest.TestCase):
    W, H = 31, 16  # a floor diamond: w = 2h - 1, the next tile ((w + 1) / 2, h / 2) away
    A, B = 16, 8

    def owner(self, x: int, y: int) -> tuple[int, int]:
        """Which tile of the floor (lattice (A, B), (A, -B)) a 1x pixel belongs to."""
        dx, dy = (x + 0.5 - self.W / 2) / self.A, (y + 0.5 - self.H / 2) / self.B
        return math.floor((dx + dy) / 2 + 0.5), math.floor((dx - dy) / 2 + 0.5)

    def tile(self, bgr: tuple[int, int, int]) -> tuple[bytes, bytes]:
        """A one-colour tile, 1x and nearest 3x (as an HD tile's own pixels are)."""
        tex = b"".join(bytes(bgr) + b"\xff" if self.owner(x, y) == (0, 0) else b"\0\0\0\0"
                       for y in range(self.H) for x in range(self.W))
        W3 = self.W * 3
        hd = b"".join(tex[((Y // 3) * self.W + X // 3) * 4:][:4] for Y in range(self.H * 3) for X in range(W3))
        return tex, hd

    def test_own_pixels_kept_and_outline_pixels_added_outside(self):
        tex, hd = self.tile((0, 0, 200))
        self.assertTrue(hd_floors.is_tile(self.W, self.H, tex))
        out = hd_floors.edges(self.W, self.H, tex, hd)
        W3 = self.W * 3
        added = 0
        for Y in range(self.H * 3):
            for X in range(W3):
                i = (Y * W3 + X) * 4
                if tex[((Y // 3) * self.W + X // 3) * 4 + 3]:
                    self.assertEqual(out[i:i + 4], hd[i:i + 4])
                elif out[i + 3]:
                    added += 1
                    self.assertTrue(1 <= out[i + 3] <= 254)
                    self.assertLessEqual(abs(out[i + 2] - 200 * out[i + 3] / 255), 1)  # premultiplied red
                    self.assertEqual(out[i:i + 2], b"\0\0")
        self.assertGreater(added, 0)
        self.assertEqual(hd_floors.edges(self.W, self.H, tex, out), out)  # again: the same

    def test_two_floors_meet_on_a_straight_line(self):
        """Tile 1 (red) and tile 2 (blue) one tile apart along (A, B): each tile's own pixels, then
        their added pixels blended over, as vkproxy draws them; the edge between them is straight."""
        (t1, h1), (t2, h2) = self.tile((0, 0, 255)), self.tile((255, 0, 0))
        e1, e2 = hd_floors.edges(self.W, self.H, t1, h1), hd_floors.edges(self.W, self.H, t2, h2)
        W3, H3 = self.W * 3, self.H * 3
        cw, ch = W3 + self.A * 3, H3 + self.B * 3
        canvas = [[None] * cw for _ in range(ch)]
        placed = ((e1, 0, 0), (e2, self.A * 3, self.B * 3))
        for fringe in (False, True):
            for img, ox, oy in placed:
                for Y in range(H3):
                    for X in range(W3):
                        p = img[(Y * W3 + X) * 4:][:4]
                        if (p[3] == 255) == fringe or not p[3]:
                            continue
                        under = canvas[Y + oy][X + ox]
                        if fringe and under is None:
                            continue
                        a = p[3] / 255
                        canvas[Y + oy][X + ox] = [p[k] + (under[k] * (1 - a) if fringe else 0) for k in range(3)]

        def crossing(x: int) -> int:  # the last row down the column that is mostly red
            return max(y for y in range(ch) if canvas[y][x] is not None and canvas[y][x][2] > canvas[y][x][0])

        # the shared edge runs from tile 1's right corner down-left to its bottom corner
        xs = range(W3 // 2 + 9, W3 - 9, 2)
        steps = [crossing(x + 2) - crossing(x) for x in xs]
        self.assertTrue(all(s == -1 for s in steps), steps)


class HdUiTests(unittest.TestCase):
    def test_upload_is_rgb565_truncated(self):
        self.assertEqual(hd_ui.upload(bytes((115, 49, 255, 255))), bytes((0xF8, 0x30, 0x70, 255)))

    def test_dithered_gradient_becomes_smooth_and_keeps_its_line(self):
        w, h = 60, 8
        tex = bytearray()
        for y in range(h):
            for x in range(w):
                if y == 1:  # a bevel line, 16 brighter
                    v = 160
                else:  # a gradient 144 -> 96 along x, dithered in steps of 8
                    v = 144 - 48 * x // w
                    v = v // 8 * 8 + (8 if (x * 7 + y * 3) % 5 < (v % 8) * 5 // 8 else 0)
                tex += bytes((v, v, v, 255))
        hd = hd_ui.smooth_gradient(w, h, bytes(tex))
        W = w * 3
        self.assertEqual(len(hd), W * h * 3 * 4)
        row = [hd[(4 * 3 * W + X) * 4] for X in range(W)]  # a row in the middle
        self.assertLessEqual(max(abs(a - b) for a, b in zip(row, row[1:])), 2)  # no dither steps left
        self.assertGreater(row[0], row[-1])
        line = [hd[(1 * 3 * W + X) * 4] for X in range(W)]
        self.assertTrue(all(v == 160 for v in line))  # the line is not blurred into its neighbours


def make_bmp24(w: int, h: int, rgb: list[tuple[int, int, int]]) -> bytes:
    """24-bit bottom-up BMP; rgb[y * w + x] = (r, g, b), top row first."""
    stride = (w * 3 + 3) & ~3
    rows = [bytes(c for r, g, b in rgb[y * w:(y + 1) * w] for c in (b, g, r)).ljust(stride, b"\0")
            for y in reversed(range(h))]
    pixels = b"".join(rows)
    return (b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(pixels), 0, 0, 0, 0) + pixels)


class HdSkinsTests(unittest.TestCase):
    def test_finds_mesh_textures_and_names_them_as_uploaded(self):
        def tex(w, h, seed):
            return [((x * 7 + seed) % 256, (y * 13 + seed) % 256, (x + y + seed) % 256) for y in range(h) for x in range(w)]

        body, head, tiny = tex(32, 32, 1), tex(32, 64, 2), tex(16, 16, 3)
        with tempfile.TemporaryDirectory() as d:
            game = Path(d)
            (game / "GameData" / "Skins").mkdir(parents=True)
            (game / "GameData" / "Skins" / "B001body.bmp").write_bytes(make_bmp24(32, 32, body))
            (game / "GameData" / "Skins" / "spark.bmp").write_bytes(make_bmp24(16, 16, tiny))  # too small
            (game / "GameData" / "Textures").mkdir()
            (game / "GameData" / "Textures" / "Textures.far").write_bytes(
                make_far({"C001head.bmp": make_bmp24(32, 64, head), "B001copy.bmp": make_bmp24(32, 32, body)}))
            (game / "UIGraphics").mkdir()  # UI bitmaps are not mesh textures
            (game / "UIGraphics" / "UIGraphics.far").write_bytes(make_far({"panel.bmp": make_bmp24(32, 32, head[:1024])}))
            (game / "GameData" / "Walls").mkdir()  # nor are BMPs outside the Skins folders
            (game / "GameData" / "Walls" / "x.bmp").write_bytes(make_bmp24(32, 32, head[:1024]))
            (game / "vkproxy" / "Skins").mkdir(parents=True)  # nor our own files
            (game / "vkproxy" / "Skins" / "y.bmp").write_bytes(make_bmp24(32, 32, head[1024:]))
            found = list(hd_skins.textures(game))
        self.assertEqual([(w, h, where.replace("\\", "/")) for _n, w, h, _t, where in found],
                         [(32, 32, "GameData/Skins/B001body.bmp"), (32, 64, "GameData/Textures/Textures.far/C001head.bmp")])
        for (name, w, h, t, _where), rgb in zip(found, (body, head)):
            self.assertEqual(t, bytes(c for r, g, b in rgb for c in (b, g, r, 255)))  # full colour
            uploaded = bytes(c for r, g, b in rgb for c in (b & 0xF8, g & 0xFC, r & 0xF8, 255))
            self.assertEqual(name, spr2.content_name(w, h, uploaded))  # named as Legacy uploads it

    def test_names_list_grows(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(hd_skins.install_names(Path(d), ["8x8_00bb", "8x8_00aa"]), 2)
            self.assertEqual(hd_skins.install_names(Path(d), ["8x8_00cc", "8x8_00aa"]), 3)
            self.assertEqual((Path(d) / hd_skins.FILE).read_text(encoding="ascii"), "8x8_00aa\n8x8_00bb\n8x8_00cc\n")


class HdPackTests(unittest.TestCase):
    A, B, C = "2x2_00000000000000aa@3.bgra", "2x2_00000000000000bb_f9@3.raw", "2x2_00000000000000cc_w3@3.bgra"

    def test_pack_read_and_override(self):
        with tempfile.TemporaryDirectory() as d:
            inst = hd_pack.Installed(Path(d))
            inst.write(self.A, b"a" * 36)
            inst.write(self.B, b"b" * 9)
            (Path(d) / "8x8@3.bgra").write_bytes(b"font")  # not content: stays loose
            self.assertEqual(inst.pack(log=lambda s: None), 2)
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["8x8@3.bgra", "hd_0000.pack"])
            again = hd_pack.Installed(Path(d))
            self.assertEqual(again.read(self.A), b"a" * 36)
            self.assertEqual(again.read(self.B), b"b" * 9)
            self.assertEqual(again.size(self.B), 9)
            again.write(self.A, b"A" * 36)  # loose wins over the pack
            self.assertEqual(again.read(self.A), b"A" * 36)
            again.write(self.C, b"c" * 36)
            again.pack(log=lambda s: None)  # a later pack wins over an earlier one
            last = hd_pack.Installed(Path(d))
            self.assertEqual([p.name for p in last.packs()], ["hd_0000.pack", "hd_0001.pack"])
            self.assertEqual(last.read(self.A), b"A" * 36)
            self.assertEqual(last.compact(log=lambda s: None), 1)
            self.assertEqual(hd_pack.Installed(Path(d)).read(self.A), b"A" * 36)

    def test_remove_and_backup(self):
        with tempfile.TemporaryDirectory() as d:
            inst = hd_pack.Installed(Path(d) / "replace")
            inst.write(self.A, b"a" * 36)
            inst.write(self.C, b"c" * 36)
            inst.pack(backup=Path(d) / "loose", log=lambda s: None)
            self.assertEqual((Path(d) / "loose" / self.A).read_bytes(), b"a" * 36)
            inst.write(self.A, b"A" * 36)
            inst.pack(log=lambda s: None)  # now in both packs
            self.assertEqual(inst.remove([self.A]), 1)
            self.assertNotIn(self.A, hd_pack.Installed(Path(d) / "replace"))
            self.assertEqual(hd_pack.Installed(Path(d) / "replace").read(self.C), b"c" * 36)
            self.assertEqual(inst.remove([self.C]), 1)
            self.assertEqual(list((Path(d) / "replace").iterdir()), [])  # empty packs go

    def test_rejects_bad_packs(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hd_0000.pack"
            p.write_bytes(b"x" * 40)
            with self.assertRaises(ValueError):
                hd_pack.read_index(p)
            raw = b"..\\evil@3.bgra"
            p.write_bytes(b"data" + struct.pack("<H", len(raw)) + raw + struct.pack("<QId", 0, 4, 0)
                          + hd_pack.FOOTER.pack(1, 4, hd_pack.MAGIC, hd_pack.VERSION))
            with self.assertRaises(ValueError):
                hd_pack.read_index(p)


class FarTests(unittest.TestCase):
    def test_roundtrip_1a_and_1b(self):
        files = {"UIGraphics/panel.bmp": b"BM" + b"x" * 30, "readme.txt": b"hello"}
        for fmt in ("<I", "<H"):
            entries = far.read_far(make_far(files, fmt))
            self.assertEqual([e.name for e in entries], list(files))
            self.assertEqual([e.size for e in entries], [32, 5])

    def test_extract(self):
        files = {"a/b.bmp": b"BM1234", "c.txt": b"hi"}
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "t.far")
            src.write_bytes(make_far(files))
            self.assertEqual(far.extract(src, Path(tmp, "out")), 2)
            self.assertEqual(Path(tmp, "out", "a", "b.bmp").read_bytes(), b"BM1234")

    def test_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "evil.far")
            src.write_bytes(make_far({"../../evil.txt": b"x"}))
            with self.assertRaises(far.FarError):
                far.extract(src, Path(tmp, "out"))

    def test_rejects_non_far(self):
        with self.assertRaises(far.FarError):
            far.read_far(b"not a far file at all")


class IffTests(unittest.TestCase):
    def test_lists_chunks(self):
        data = make_iff([("BMP_", 128, "panel", b"BM" + b"\0" * 10), ("STR#", 300, "labels", b"\0" * 4)])
        chunks = iff.read_iff(data)
        self.assertEqual([(c.type, c.id, c.label, c.size) for c in chunks],
                         [("BMP_", 128, "panel", 12), ("STR#", 300, "labels", 4)])
        self.assertEqual(data[chunks[0].offset:chunks[0].offset + 2], b"BM")

    def test_accepts_2_0_and_rejects_other_signatures(self):
        chunk = [("STR#", 1, "x", b"\0" * 4)]
        self.assertEqual(len(iff.read_iff(make_iff(chunk, b"2.0"))), 1)
        bogus = b"IFF FILE 2.5:TYPE FOLLOWING+SIZE".ljust(64, b"\0")
        with self.assertRaises(iff.IffError):
            iff.read_iff(bogus)

    def test_bad_size(self):
        data = bytearray(make_iff([("BMP_", 1, "x", b"abc")]))
        struct.pack_into(">I", data, 64 + 4, 9999)
        with self.assertRaises(iff.IffError):
            iff.read_iff(bytes(data))


class ExeDumpTests(unittest.TestCase):
    def test_sections_of_a_mapped_image(self):
        pe = 0x80
        image = bytearray(0x400)
        image[:2] = b"MZ"
        struct.pack_into("<I", image, 0x3C, pe)
        image[pe:pe + 4] = b"PE\0\0"
        struct.pack_into("<HH", image, pe + 4, 0x14C, 2)  # i386, 2 sections
        struct.pack_into("<H", image, pe + 20, 0xE0)      # optional header size
        table = pe + 24 + 0xE0
        for i, (name, rva, size, flags) in enumerate(((b".text", 0x1000, 0x2345, 0x60000020),
                                                      (b".data", 0x4000, 0x100, 0xC0000040))):
            o = table + 40 * i
            image[o:o + 8] = name.ljust(8, b"\0")
            struct.pack_into("<II", image, o + 8, size, rva)
            struct.pack_into("<I", image, o + 36, flags)
        self.assertEqual(exe_dump.sections(bytes(image)),
                         [(".text", 0x1000, 0x2345, 0x60000020), (".data", 0x4000, 0x100, 0xC0000040)])
        with self.assertRaises(ValueError):
            exe_dump.sections(b"ZM" + bytes(image[2:]))


class StringsTests(unittest.TestCase):
    def test_ascii_and_utf16(self):
        blob = b"\0\0UIScaleFactor\0\0" + "Resolution".encode("utf-16le") + b"\0\0ab\0"
        found = {(enc, s) for _, enc, s in exe_strings.extract_strings(blob)}
        self.assertIn(("ascii", "UIScaleFactor"), found)
        self.assertIn(("utf16", "Resolution"), found)
        self.assertTrue(exe_strings.matches("UIScaleFactor", exe_strings.DEFAULT_KEYWORDS))


class InventoryTests(unittest.TestCase):
    def test_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "game")
            (root / "UIGraphics").mkdir(parents=True)
            (root / "UIGraphics" / "x.far").write_bytes(make_far({"a.bmp": b"BM"}))
            (root / "Sims.exe").write_bytes(b"MZ" + b"\0" * 100)
            report = inventory.build_report(root)
            self.assertIn("FAR archive", report)
            self.assertIn("Sims.exe", report)
            self.assertIn("UIGraphics", report)


if __name__ == "__main__":
    unittest.main()
