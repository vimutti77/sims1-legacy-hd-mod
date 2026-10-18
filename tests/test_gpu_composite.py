"""ai_upscale/gpu_composite.py against the reference tools/hd_composite.py, on synthetic images.

Needs torch, so it is skipped outside the ai_upscale venv:
    work\\ext\\torch\\Scripts\\python.exe -m unittest tests.test_gpu_composite
"""
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "ai_upscale"))

import hd_composite  # noqa: E402

try:
    import torch
    import gpu_composite
except ImportError:  # outside the venv
    torch = None


def synthetic(w0: int, h0: int, s: int, seed: int):
    """A premultiplied BGRA original with empty, soft and solid texels, and an RGB 'upscale'."""
    rnd = random.Random(seed)
    orig = bytearray()
    for _ in range(w0 * h0):
        a = rnd.choice((0, 0, 255, 255, 255, rnd.randrange(1, 255)))
        orig += bytes(rnd.randrange(a + 1) for _ in range(3)) + bytes((a,))
    ai = bytes(rnd.randrange(256) for _ in range(w0 * s * h0 * s * 3))
    return bytes(orig), ai


@unittest.skipIf(torch is None, "needs torch (ai_upscale venv)")
class GpuCompositeTests(unittest.TestCase):
    def check(self, w0: int, h0: int, s: int, seed: int, strength: float) -> None:
        orig, ai = synthetic(w0, h0, s, seed)
        W, H = w0 * s, h0 * s
        rgba = bytearray()
        for i in range(W * H):
            rgba += ai[i * 3:i * 3 + 3] + b"\xff"
        ai3 = rgba if s == 3 else hd_composite.shrink(W, H, rgba, w0 * 3, h0 * 3)
        want = hd_composite.composite(w0, h0, orig, ai3, backproject_strength=strength)

        t_orig = torch.frombuffer(bytearray(orig), dtype=torch.uint8).view(h0, w0, 4)
        t_ai = torch.frombuffer(bytearray(ai), dtype=torch.uint8).view(H, W, 3).permute(2, 0, 1)
        got = bytes(gpu_composite.composite(t_orig, t_ai, backproject_strength=strength).flatten().tolist())
        self.assertEqual(len(got), len(want))
        self.assertEqual(got[3::4], bytes(want[3::4]))  # coverage is never different
        diffs = [abs(a - b) for a, b in zip(got, want) if a != b]
        if s == 3 and not strength:
            self.assertEqual(diffs, [])
        else:  # float32 vs double, and exact .5 ties, may round the other way
            self.assertLessEqual(max(diffs, default=0), 1)
            self.assertLessEqual(len(diffs), max(2, len(got) // 20))

    def test_same_scale_is_exact(self):
        self.check(7, 5, 3, 1, 0.0)

    def test_x4_model(self):
        self.check(9, 6, 4, 2, 0.0)
        self.check(1, 1, 4, 3, 0.0)

    def test_backprojection_matches_the_reference(self):
        self.check(7, 5, 3, 4, gpu_composite.BACKPROJECT)
        self.check(9, 6, 4, 5, gpu_composite.BACKPROJECT)
        self.assertEqual(gpu_composite.BACKPROJECT, hd_composite.BACKPROJECT)


if __name__ == "__main__":
    unittest.main()
