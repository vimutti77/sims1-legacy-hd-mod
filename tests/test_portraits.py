"""Tests for portraits/ (drawing a head, finding a picture's pose) with a synthetic head.

Needs numpy; skipped without it.
"""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "portraits"))

import skn  # noqa: E402

try:
    import numpy as np

    import library
    import pose
    import redraw
    from head import Head, compose, render
except ImportError:  # numpy missing
    np = None


def box_head():
    """A box of 0.6 (up) x 0.5 (sideways) x 0.4 (deep) around the head bone's origin, its faces
    coloured from the quarters of a 4x4 texture (the front, +z, the lightest), so turns show."""
    xs, ys, zs = (-0.3, 0.3), (-0.25, 0.25), (-0.2, 0.2)
    verts, faces, uvs = [], [], []

    def quad(corners, uv):
        base = len(verts)
        for c in corners:
            verts.append((*c, 0.0, 0.0, 1.0))
            uvs.append(uv)
        faces.extend([(base, base + 1, base + 2), (base, base + 2, base + 3)])

    x0, x1 = xs
    y0, y1 = ys
    z0, z1 = zs
    quad([(x0, y0, z1), (x0, y1, z1), (x1, y1, z1), (x1, y0, z1)], (0.125, 0.125))  # front
    quad([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)], (0.875, 0.875))  # back
    quad([(x1, y0, z0), (x1, y0, z1), (x1, y1, z1), (x1, y1, z0)], (0.875, 0.125))  # top
    quad([(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)], (0.875, 0.125))  # bottom
    quad([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], (0.125, 0.875))  # right
    quad([(x0, y0, z0), (x0, y0, z1), (x1, y0, z1), (x1, y0, z0)], (0.125, 0.875))  # left
    skin = skn.Skin("xskin-box-HEAD-HEAD", "box", ["HEAD"], faces, [(0, 0, len(verts), -1, 0)], uvs, [], verts)
    tex = np.zeros((4, 4, 3))
    tex[:2, :2] = (240, 200, 160)   # front
    tex[:2, 2:] = (90, 60, 30)      # top / bottom
    tex[2:, :2] = (200, 120, 80)    # sides
    tex[2:, 2:] = (40, 40, 40)      # back
    return Head([skin]), [tex]


@unittest.skipIf(np is None, "needs numpy")
class RenderTests(unittest.TestCase):
    def test_front_view(self):
        head, tex = box_head()
        colour, cov = render(head, tex, (0, 0, 0, 20, 10, 10), (20, 20), ss=2)
        pic = compose(colour, cov, (0, 0, 128))
        # 0.5 wide x 0.6 high at 20 px a unit (a little more: the front is nearer the camera)
        self.assertTrue(np.allclose(pic[10, 10], (240, 200, 160)))
        self.assertTrue(np.allclose(pic[1, 1], (0, 0, 128)))
        self.assertEqual(cov[10].sum() // 1, 10)
        self.assertEqual(cov[:, 10].sum() // 1, 12)

    def test_turned_shows_a_side(self):
        head, tex = box_head()
        colour, cov = render(head, tex, (0.6, 0, 0, 20, 10, 10), (20, 20), ss=2)
        seen = {tuple(np.round(c)) for c in colour[cov == 1]}
        self.assertIn((200.0, 120.0, 80.0), seen)  # a side
        self.assertIn((240.0, 200.0, 160.0), seen)  # still the front


@unittest.skipIf(np is None, "needs numpy")
class FitTests(unittest.TestCase):
    def test_nelder_mead(self):
        x, v = pose.nelder_mead(lambda p: float(((p - np.array([1.0, -2.0])) ** 2).sum()), np.zeros(2), np.ones(2))
        self.assertLess(np.abs(x - np.array([1.0, -2.0])).max(), 0.01)
        self.assertLess(v, 1e-4)

    def test_finds_the_pose_a_picture_was_drawn_with(self):
        head, tex = box_head()
        truth = np.array([0.25, -0.1, 0.05, 40, 17, 18])
        colour, cov = render(head, tex, truth, (35, 41), ss=4)
        picture = np.round(compose(colour, cov, (0, 0, 82)))
        found, err = pose.fit_view(head, tex, picture, (0, 0, 82))
        self.assertLess(err, 3)
        self.assertLess(np.abs(found[3:] - truth[3:]).max(), 1.5)
        self.assertLess(np.abs(found[:3] - truth[:3]).max(), 0.12)

    def test_nothing_to_fit(self):
        head, tex = box_head()
        self.assertIsNone(pose.fit_view(head, tex, np.zeros((10, 10, 3)), (0, 0, 0)))


@unittest.skipIf(np is None, "needs numpy")
class BuildTests(unittest.TestCase):
    def test_background_is_the_commonest_edge_colour(self):
        part = np.zeros((6, 6, 3))
        part[:] = (0, 0, 82)
        part[:3, :] = (120, 80, 40)  # hair across the top half, corner included
        part[0, 0] = (120, 80, 40)
        self.assertEqual(tuple(redraw.background(part)), (0, 0, 82))

    def test_hd_picture_keeps_the_frame(self):
        head, tex = box_head()
        picture = np.zeros((25, 25, 3))
        picture[:] = (0, 0, 248)  # the frame
        colour, cov = render(head, tex, (0, 0, 0, 25, 11.5, 11.5), (23, 23), ss=4)
        picture[1:24, 1:24] = np.round(compose(colour, cov, (0, 0, 0)))
        hd, err = redraw.draw_picture(head, tex, tex, picture, [(1, 1, 23, 23)], False)
        self.assertLess(err, 3)
        self.assertEqual(hd.shape, (75, 75, 3))
        self.assertTrue((hd[:3] == (0, 0, 248)).all() and (hd[:, :3] == (0, 0, 248)).all())  # 3 px of frame
        self.assertTrue(np.allclose(hd[37, 37], (240, 200, 160), atol=2))  # the front, middle
        self.assertTrue(np.allclose(hd[5, 5], (0, 0, 0), atol=2))  # background inside the frame


@unittest.skipIf(np is None, "needs numpy")
class LibraryTests(unittest.TestCase):
    def test_heads_pair_with_the_textures_of_their_code(self):
        files = {n: () for n in ("xskin-c802fa_cornelia-head-head.bmf", "xskin-c802fa_cornelia-head-hat.bmf",
                                 "c802falgt_cornelia.bmp", "c802fadrk_other.bmp", "c802fa_cornelia.bmp",
                                 "c803malgt_gunter.bmp", "xskin-b602faskn_x-pelvis-body.bmf")}
        self.assertEqual(library.candidates(files), [("c802fa_cornelia", "c802fadrk_other"),
                                                     ("c802fa_cornelia", "c802falgt_cornelia")])
        self.assertRegex(library.key_id("c802fa_cornelia=c802falgt_cornelia"), "^[0-9a-f]{16}$")

    def test_speech_pictures_are_framed_to_the_head(self):
        head, tex = box_head()
        pose_2006 = library.poses_for(head, 2006)[0]
        colour, cov = render(head, tex, pose_2006, (32, 32), ss=4)
        rows = np.nonzero(cov.max(1) > 0.5)[0]
        cols = np.nonzero(cov.max(0) > 0.5)[0]
        self.assertEqual((rows.min(), rows.max()), (0, 31))  # fills the height, top at the top
        self.assertLessEqual(abs((cols.min() + cols.max() + 1) / 2 - 16), 1)  # centred across

    def test_library_file(self):
        heads = [("00000000000000aa", {cid: bytes([i]) * (w * h * 4) for cid, (w, h) in redraw.SIZES.items()})
                 for i in (1, 2)]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "portraits.lib"
            library.write_library(path, heads)
            data = path.read_bytes()
        self.assertEqual(data[:12], b"PLIB" + struct.pack("<II", 2, len(redraw.PICTURES)))
        w, h, n, err, margin = struct.unpack_from("<IIIff", data, 12)
        self.assertEqual((w, h, n), (105, 41, 2))
        self.assertEqual((err, margin), library.ACCEPT[2002])
        self.assertEqual(struct.unpack_from("<Q", data, 32)[0], 0xaa)
        self.assertEqual(data[40:44], bytes([1]) * 4)
        size = 12 + sum(20 + 2 * (8 + w * h * 4) for w, h in redraw.SIZES.values())
        self.assertEqual(len(data), size)


if __name__ == "__main__":
    unittest.main()
