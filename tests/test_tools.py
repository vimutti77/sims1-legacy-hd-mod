"""Tests using small synthetic files (no game data needed).

Run:  python -m unittest discover -s tests
"""
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import exe_strings  # noqa: E402
import far  # noqa: E402
import ffn  # noqa: E402
import font_atlas  # noqa: E402
import iff  # noqa: E402
import hd_composite  # noqa: E402
import inventory  # noqa: E402
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
