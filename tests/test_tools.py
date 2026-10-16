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
