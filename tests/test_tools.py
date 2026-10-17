"""Tests using small synthetic files (no game data needed).

Run:  python -m unittest discover -s tests
"""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import exe_strings  # noqa: E402
import far  # noqa: E402
import ffn  # noqa: E402
import font_atlas  # noqa: E402
import iff  # noqa: E402
import inventory  # noqa: E402


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
