"""Tests for catalog/ (object files, assembly, fit, the build's choices) with synthetic files.

Needs numpy; skipped without it.
"""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "catalog"))
sys.path.insert(0, str(ROOT / "tests"))

import objects  # noqa: E402
import spr2  # noqa: E402
from test_tools import make_iff  # noqa: E402

try:
    import numpy as np

    import assemble
    import build
    import fit
except ImportError:  # numpy missing
    np = None


def bmp24(w, h, rgb_rows, top_down=False):
    """24-bit BMP; rgb_rows[y][x] = (r, g, b), top row first."""
    stride = (w * 3 + 3) & ~3
    rows = rgb_rows if top_down else rgb_rows[::-1]
    pixels = b"".join(bytes(c for r, g, b in row for c in (b, g, r)).ljust(stride, b"\0") for row in rows)
    return (b"BM" + struct.pack("<IHHI", 54 + len(pixels), 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, w, -h if top_down else h, 1, 24, 0, len(pixels), 0, 0, 0, 0) + pixels)


def bmp8(w, h, index_rows, palette, rle=False):
    """8-bit BMP (plain or RLE8); index_rows top row first; palette [(r, g, b)]."""
    pal = b"".join(bytes((b, g, r, 0)) for r, g, b in palette)
    if rle:
        body = b""
        for row in index_rows[::-1]:
            # first pixel as a run of one, the rest as one literal run (when 3 or more)
            body += bytes((1, row[0]))
            rest = bytes(row[1:])
            if len(rest) >= 3:
                body += bytes((0, len(rest))) + rest + (b"\0" if len(rest) % 2 else b"")
            else:
                body += b"".join(bytes((1, v)) for v in rest)
            body += b"\0\0"
        body += b"\0\1"
    else:
        stride = (w + 3) & ~3
        body = b"".join(bytes(row).ljust(stride, b"\0") for row in index_rows[::-1])
    off = 54 + len(pal)
    return (b"BM" + struct.pack("<IHHI", off + len(body), 0, 0, off)
            + struct.pack("<IiiHHIIiiII", 40, w, h, 1, 8, 1 if rle else 0, len(body), 0, 0, len(palette), 0)
            + pal + body)


def objd(base, ngraphics=1, master=0, sub=0, catpic=0, guid=0x12345678, level=0, dyn=(0, 0)):
    v = [0] * 70
    v[1], v[2], v[8], v[9], v[12], v[13] = base, ngraphics, master, sub & 0xFFFF, guid & 0xFFFF, guid >> 16
    v[39], v[54], v[62], v[63] = catpic, level, dyn[0], dyn[1]
    return struct.pack("<I", 138) + struct.pack("<70H", *v)


def dgrp(images, version=20004):
    """images: [(direction, zoom, [(spr, frame, ox, oy, flags)])]."""
    out = struct.pack("<HI", version, len(images))
    for direction, zoom, sprites in images:
        out += struct.pack("<III", direction, zoom, len(sprites))
        for sid, frame, ox, oy, flags in sprites:
            out += struct.pack("<IIiifI", sid, frame, ox, oy, 0.0, flags) + struct.pack("<ff", 0.0, 0.0)
    return out


def frame(w, h, x, y, pixels):
    """SPR2 frame; pixels[y][x] = (z, colour index) or None."""
    def run(cmd, n, payload=b""):
        return struct.pack("<H", cmd << 13 | n) + payload

    body = b""
    for row in pixels:
        runs, xx = b"", 0
        while xx < w:
            n = 0
            if row[xx] is None:
                while xx + n < w and row[xx + n] is None:
                    n += 1
                runs += run(3, n)
            else:
                while xx + n < w and row[xx + n] is not None:
                    n += 1
                runs += run(1, n, b"".join(bytes(p) for p in row[xx:xx + n]))
            xx += n
        body += run(0, 2 + len(runs), runs)
    return struct.pack("<HHIHHHH", w, h, 3, 1, 0, y, x) + body + run(5, 0)


def spr2_chunk(frames, palette_id=1):
    offsets, body = [], b""
    for f in frames:
        offsets.append(12 + 4 * len(frames) + len(body))
        body += f
    return struct.pack("<III", 1000, len(frames), palette_id) + struct.pack(f"<{len(frames)}I", *offsets) + body


def palt(colours):
    return struct.pack("<II", 1, len(colours)) + b"\0" * 8 + bytes(c for rgb in colours for c in rgb)


PALETTE = [(0, 0, 0), (248, 0, 0), (0, 0, 248), (0, 252, 0), (248, 252, 0)]


def two_colour(w, h, left, right, z=0):
    return [[(z, left if x < w // 2 else right) for x in range(w)] for _ in range(h)]


class ObjectFileTests(unittest.TestCase):
    def test_bmp_kinds_decode_alike(self):
        palette = [(10, 20, 30), (200, 100, 50), (0, 255, 0)]
        idx = [[0, 1, 2, 1, 0], [2, 2, 1, 0, 0]]
        rgb = [[palette[i] for i in row] for row in idx]
        want = (5, 2, bytes(c for row in rgb for r, g, b in row for c in (r, g, b, 255)))
        self.assertEqual(objects.decode_bmp(bmp24(5, 2, rgb)), want)
        self.assertEqual(objects.decode_bmp(bmp24(5, 2, rgb, top_down=True)), want)
        self.assertEqual(objects.decode_bmp(bmp8(5, 2, idx, palette)), want)
        self.assertEqual(objects.decode_bmp(bmp8(5, 2, idx, palette, rle=True)), want)
        with self.assertRaises(objects.FormatError):
            objects.decode_bmp(bmp24(5, 2, rgb)[:60])
        with self.assertRaises(objects.FormatError):
            objects.decode_bmp(b"XX" + bmp24(5, 2, rgb)[2:])

    def test_dgrp_versions(self):
        version, images = objects.read_dgrp(dgrp([(1, 3, [(7, 2, -5, 9, 1)]), (4, 3, [])]))
        self.assertEqual(version, 20004)
        self.assertEqual([(im["direction"], im["zoom"], len(im["sprites"])) for im in images], [(1, 3, 1), (4, 3, 0)])
        s = images[0]["sprites"][0]
        self.assertEqual((s["spr"], s["frame"], s["ox"], s["oy"], s["flags"]), (7, 2, -5, 9, 1))
        old = struct.pack("<HH", 20000, 1) + struct.pack("<HBB", 1, 16, 2) + struct.pack("<HHHHhh", 0, 7, 1, 0, 3, -4)
        _, images = objects.read_dgrp(old)
        self.assertEqual((images[0]["direction"], images[0]["zoom"], images[0]["sprites"][0]["oy"]), (16, 2, -4))
        with self.assertRaises(objects.FormatError):
            objects.read_dgrp(dgrp([(1, 3, [(7, 2, -5, 9, 1)])])[:-10])
        with self.assertRaises(objects.FormatError):
            objects.read_dgrp(struct.pack("<HI", 1234, 0))

    def test_catalogue_objects_group_tiles_under_the_lead(self):
        pic = bmp24(74, 37, [[(1, 2, 3)] * 74] * 37)
        data = make_iff([
            ("OBJD", 1, "Sofa", objd(100, master=5, sub=-1, catpic=2000)),
            ("OBJD", 2, "Sofa tile 0", objd(100, master=5, sub=0, catpic=2000)),
            ("OBJD", 3, "Sofa tile 1", objd(101, master=5, sub=1, catpic=2000)),
            ("OBJD", 4, "Lamp", objd(200, catpic=2001)),
            ("OBJD", 5, "No picture", objd(300)),
            ("BMP_", 2000, "pic", pic), ("BMP_", 2001, "pic", pic),
        ])
        obj = objects.ObjectFile(data, "test.iff")
        got = [(o["label"], [t["label"] for t in tiles]) for o, tiles in objects.catalogue_objects(obj)]
        self.assertEqual(got, [("Sofa", ["Sofa tile 0", "Sofa tile 1"]), ("Lamp", ["Lamp"])])
        o = obj.objds[0]
        self.assertEqual((o["sub"], o["guid"], o["catpic"]), (-1, 0x12345678, 2000))
        w, h, rgba = obj.picture(o)
        self.assertEqual(objects.picture_name(w, h, rgba), spr2.content_name(74, 37, bytes((3, 2, 1, 255)) * 74 * 37))

    def test_frame_z(self):
        f = spr2.decode_frame(frame(2, 1, 0, 0, [[(7, 1), None]]), 0)
        self.assertEqual((f.pixels, f.z), ([(1, None), None], [7, 255]))


def sprite_file(pictures=None, extra_frames=()):
    """An object with two states (DGRP 100: red | blue, DGRP 101: green | yellow), one view."""
    frames = [frame(8, 6, 30, 40, two_colour(8, 6, 1, 2)), frame(8, 6, 30, 40, two_colour(8, 6, 3, 4))]
    frames += list(extra_frames)
    chunks = [("OBJD", 1, "Thing", objd(100, ngraphics=2, catpic=2000)), ("PALT", 1, "pal", palt(PALETTE)),
              ("SPR2", 10, "spr", spr2_chunk(frames)),
              ("DGRP", 100, "g0", dgrp([(1, 3, [(10, 0, -68, 0, 0)])])),
              ("DGRP", 101, "g1", dgrp([(1, 3, [(10, 1, -68, 0, 0)])]))]
    if pictures is not None:
        chunks.append(("BMP_", 2000, "pic", pictures))
    return make_iff(chunks)


@unittest.skipIf(np is None, "needs numpy")
class AssembleTests(unittest.TestCase):
    def test_nearer_z_wins_whatever_the_order(self):
        far_ = frame(4, 1, 0, 0, [[(200, 1)] * 4])
        near = frame(4, 1, 0, 0, [[(10, 2)] * 2 + [(255, 2), (250, 2)]])
        data = make_iff([("PALT", 1, "pal", palt(PALETTE)), ("SPR2", 10, "spr", spr2_chunk([far_, near])),
                         ("DGRP", 100, "a", dgrp([(1, 3, [(10, 1, -68, 0, 0), (10, 0, -68, 0, 0)])]))])
        obj = objects.ObjectFile(data)
        tile = dict(base=100, sub=0, ngraphics=1, level=0, dyn_base=0, dyn_n=0)
        c = assemble.assemble(assemble.Sprites(obj), [tile], 0, 0)
        red, blue = np.array([248, 0, 0]) / 255, np.array([0, 0, 248]) / 255
        # near (blue) drawn first stays in front; its z 255 pixel is not drawn; z 250 is behind 200
        for x, want in enumerate((blue, blue, red, red)):
            np.testing.assert_allclose(c[0, x, :3], want, atol=1e-6)

    def test_a_tile_lower_on_screen_is_nearer(self):
        # view 0: tile (1, 0) is 64 px left of and 32 px lower than tile (0, 0); the sprites are
        # placed so that both cover the same pixel. One tile step nearer is worth GAME_K (~48) z units.
        for z_lower, wins in ((140, True), (160, False)):
            data = make_iff([("PALT", 1, "pal", palt(PALETTE)),
                             ("SPR2", 10, "spr", spr2_chunk([frame(1, 1, 0, 32, [[(100, 1)]]),
                                                             frame(1, 1, 0, 0, [[(z_lower, 2)]])])),
                             ("DGRP", 100, "a", dgrp([(1, 3, [(10, 0, -68, 0, 0)])])),
                             ("DGRP", 101, "b", dgrp([(1, 3, [(10, 1, -4, 0, 0)])]))])
            obj = objects.ObjectFile(data)
            tiles = [dict(base=100, sub=0, ngraphics=1, level=0, dyn_n=0),
                     dict(base=101, sub=1, ngraphics=1, level=0, dyn_n=0)]
            c = assemble.assemble(assemble.Sprites(obj), tiles, 0, 0)
            self.assertEqual(c.shape[:2], (1, 1))
            np.testing.assert_allclose(c[0, 0, :3], np.array([0, 0, 248] if wins else [248, 0, 0]) / 255, atol=1e-6)
    def test_dynamic_sprites_are_off_unless_asked(self):
        data = make_iff([("PALT", 1, "pal", palt(PALETTE)),
                         ("SPR2", 10, "spr", spr2_chunk([frame(1, 1, 0, 0, [[(0, 1)]])])),
                         ("SPR2", 11, "dyn", spr2_chunk([frame(1, 1, 1, 0, [[(0, 2)]])])),
                         ("DGRP", 100, "a", dgrp([(1, 3, [(10, 0, -68, 0, 0), (11, 0, -67, 0, 0)])]))])
        obj = objects.ObjectFile(data)
        tile = dict(base=100, sub=0, ngraphics=1, level=0, dyn_base=11, dyn_n=1)
        sprites = assemble.Sprites(obj)
        self.assertEqual(assemble.assemble(sprites, [tile], 0, 0).shape[:2], (1, 1))
        self.assertEqual(assemble.assemble(sprites, [tile], 0, 0, dynamic={11}).shape[:2], (1, 2))
        self.assertEqual(assemble.dynamic_ids(obj, [tile], 0, 0), {11})

    def test_a_tile_with_fewer_states_stays_in_its_last(self):
        # state 1 of the object: tile A has it (DGRP 101), tile B has one state only (DGRP 200)
        tiles = [dict(base=100, ngraphics=2), dict(base=200, ngraphics=1), dict(base=300, ngraphics=0)]
        self.assertEqual([assemble.tile_dgrp(t, 1) for t in tiles], [101, 200, 300])


@unittest.skipIf(np is None, "needs numpy")
class FitTests(unittest.TestCase):
    def test_recovers_scale_and_offset(self):
        canvas = np.zeros((40, 60, 4), np.float32)
        canvas[5:35, 10:50] = (0.8, 0.1, 0.1, 1)
        canvas[20:35, 30:50] = (0.1, 0.1, 0.7, 1)
        bg = np.array([104, 164, 184]) / 255
        target = fit.render(canvas, 0.55, 3.3, 4.6, 37, bg)
        mse, s, x, y = fit.fit(canvas, target, bg)
        self.assertLess(mse, 1e-4)
        self.assertAlmostEqual(s, 0.55, delta=0.01)
        self.assertAlmostEqual(x, 3.3, delta=0.3)
        self.assertAlmostEqual(y, 4.6, delta=0.3)
        x, y = fit.centred(canvas, 0.5, 37)
        self.assertAlmostEqual((x + 0.5 * 10) - (37 - (x + 0.5 * 50)), 0)  # equal margins left and right


def picture_of(canvas, s, x, y):
    """A 74x37 catalogue picture of canvas as the game makes it (normal | selected), as a BMP."""
    normal = fit.render(canvas, s, x, y, 37, np.array([104, 164, 184]) / 255)
    selected = fit.render(canvas, s, x, y, 37, np.array([152, 196, 208]) / 255, add=build.SELECTED_ADD)
    rgb = (np.concatenate([normal, selected], axis=1) * 255 + 0.5).astype(np.uint8)
    return bmp24(74, 37, [[tuple(int(c) for c in px) for px in row] for row in rgb])


@unittest.skipIf(np is None, "needs numpy")
class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hd_dir = Path(self.tmp.name)
        # the picture shows state 1 (green | yellow)
        obj = objects.ObjectFile(sprite_file())
        tile = obj.objds[0]
        c = assemble.assemble(assemble.Sprites(obj), [tile], 1, 0)
        self.data = sprite_file(picture_of(c, 2.0, 10.4, 12.2))
        self.obj = objects.ObjectFile(self.data, "thing.iff")
        for sid, frames in ((10, 2),):  # HD twins: nearest 3x
            for i in range(frames):
                f, pal, _, _ = self.obj.frame(sid, i)
                tex = np.frombuffer(spr2.game_texture(f, pal), np.uint8).reshape(f.h, f.w, 4)
                big = np.repeat(np.repeat(tex, 3, 0), 3, 1)
                (self.hd_dir / f"{spr2.content_name(f.w, f.h, tex.tobytes())}@3.bgra").write_bytes(big.tobytes())

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, override):
        sprites = assemble.Sprites(self.obj, self.hd_dir)
        (o, tiles), = objects.catalogue_objects(self.obj)
        return build.build_object(self.obj, sprites, o, tiles, override)

    def test_finds_the_state_and_draws_from_hd(self):
        row, px = self.build({})
        self.assertEqual((row["graphic"], row["view"], row["mirror"], row["source"]), (1, 0, False, "hd"))
        self.assertLess(max(row["rmse_normal"], row["rmse_selected"]), 3)
        self.assertGreater(row["iou"], 0.95)
        self.assertAlmostEqual(row["scale"], 2.0, delta=0.05)
        self.assertEqual(len(px), 222 * 111 * 4)
        hd = np.frombuffer(px, np.uint8).reshape(111, 222, 4)
        self.assertTrue((hd[..., 3] == 255).all())
        self.assertEqual(tuple(hd[0, 0, :3]), (184, 164, 104))  # BGR of the normal background

    def test_overrides(self):
        row, px = self.build({"none": True})
        self.assertEqual((row["source"], px), ("none (by hand)", None))
        row, _ = self.build({"graphic": 0, "mirror": True})  # mirrored only when asked
        self.assertEqual((row["graphic"], row["mirror"]), (0, True))
        row, _ = self.build({"center": True})
        canvas_w = 8 * row["scale"]  # the one sprite, 8 px wide
        self.assertAlmostEqual(row["x"] + canvas_w / 2, 37 / 2, delta=0.01)

    def test_load_overrides(self):
        p = self.hd_dir / "overrides.tsv"
        p.write_text("# comment\nA\t21\nB\t0\toff\t2\ttrue\tcenter\nC\tnone\nD\t\ton:\nE\t1\ton:200,202\n", encoding="utf-8")
        ov = build.load_overrides(p)
        self.assertEqual(ov["A"], dict(graphic=21, dynamic=None, view=None, mirror=None, center=False))
        self.assertEqual(ov["B"], dict(graphic=0, dynamic=False, view=2, mirror=True, center=True))
        self.assertEqual(ov["C"], {"none": True})
        self.assertEqual((ov["D"]["graphic"], ov["D"]["dynamic"]), (None, frozenset()))
        self.assertEqual(ov["E"]["dynamic"], frozenset({200, 202}))

    def test_choose(self):
        def row(label, pic, err, iou):
            return dict(label=label, picture=pic, rmse_normal=err, rmse_selected=err, iou=iou)

        rows = [row("good", "p1", 5, 0.9), row("bad colour", "p2", 20, 0.9), row("bad shape", "p3", 5, 0.5),
                row("approved", "p4", 30, 0.2), row("rejected", "p5", 1, 1.0), row("by hand", "p6", 40, 0.1),
                row("none by hand", "p7", 40, 0.1), row("unbuilt", "p8", -1, 0),
                row("shares p2, fits", "p2", 4, 0.95)]
        chosen = build.choose(rows, approved={"p4"}, rejected={"p5"},
                              overrides={"by hand": {"graphic": 1}, "none by hand": {"none": True}})
        self.assertEqual(chosen, {"p1", "p2", "p4", "p6"})


if __name__ == "__main__":
    unittest.main()
