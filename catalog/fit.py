"""Place an assembled object onto a catalogue picture: scale and offset (numpy).

A picture pixel is the area average of the assembled image scaled by s and moved by
(x, y): picture = s * assembled + (x, y). The fit starts from the scale and offset that
map the object's bounding box onto the picture's, then walks scale and offset downhill
(mean squared colour error), halving the steps until they are tiny. The assembled image
is first shrunk to about 3x the picture, which keeps each step cheap.
"""
from __future__ import annotations

import numpy as np

OBJECT_DIFF = 6 / 255  # a picture pixel this far from the background belongs to the object


def area_matrix(n_src: int, n_out: int, scale: float, shift: float) -> np.ndarray:
    """(n_out, n_src) weights: output pixel j covers source [(j - shift) / scale, (j + 1 - shift) / scale)."""
    lo = (np.arange(n_out)[:, None] - shift) / scale
    hi = lo + 1 / scale
    j = np.arange(n_src)[None, :]
    overlap = np.clip(np.minimum(hi, j + 1) - np.maximum(lo, j), 0, None)
    return (overlap * scale).astype(np.float32)


def render(canvas, scale: float, x: float, y: float, height: int, bg, add: float = 0.0, width: int | None = None):
    """Straight RGB (height, width, 3): canvas scaled and moved over background colour bg (0..1).
    add brightens the object by that much, weighted by its coverage (the selected picture)."""
    width = width or height
    h, w = canvas.shape[:2]
    my, mx = area_matrix(h, height, scale, y), area_matrix(w, width, scale, x)
    rows = my @ canvas.reshape(h, w * 4)
    img = (rows.reshape(height, w, 4).transpose(0, 2, 1) @ mx.T).transpose(0, 2, 1)
    return np.clip(img[..., :3] + add * img[..., 3:4] + np.asarray(bg)[None, None, :] * (1 - img[..., 3:4]), 0, 1)


def shrink(canvas, p: float):
    """canvas area-resampled by p (<= 1)."""
    if p >= 1:
        return canvas
    h, w = canvas.shape[:2]
    oh, ow = int(np.ceil(h * p)), int(np.ceil(w * p))
    rows = area_matrix(h, oh, p, 0) @ canvas.reshape(h, w * 4)
    return (rows.reshape(oh, w, 4).transpose(0, 2, 1) @ area_matrix(w, ow, p, 0).T).transpose(0, 2, 1)


def background(picture):
    """The most common colour on the picture's border."""
    border = np.concatenate([picture[0], picture[-1], picture[:, 0], picture[:, -1]])
    vals, counts = np.unique((border * 255).round().astype(int), axis=0, return_counts=True)
    return vals[counts.argmax()] / 255


def bbox(mask):
    ys, xs = np.nonzero(mask)
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def first_guess(canvas, picture, bg):
    """(scale, x, y) mapping the canvas's bounding box onto the picture object's, or None."""
    obj_mask = np.abs(picture - bg).max(axis=2) > OBJECT_DIFF
    if not obj_mask.any() or canvas[..., 3].max() <= 0.5:
        return None
    tb, cb = bbox(obj_mask), bbox(canvas[..., 3] > 0.5)
    s = ((tb[2] - tb[0]) / (cb[2] - cb[0]) + (tb[3] - tb[1]) / (cb[3] - cb[1])) / 2
    return s, (tb[0] + tb[2]) / 2 - s * (cb[0] + cb[2]) / 2, (tb[1] + tb[3]) / 2 - s * (cb[1] + cb[3]) / 2


def _mse(small, p, s, x, y, picture, bg) -> float:
    return float(((render(small, s / p, x, y, picture.shape[0], bg, width=picture.shape[1]) - picture) ** 2).mean())


def quick_error(canvas, picture, bg):
    """Mean squared error at the first guess (to rank candidates cheaply), or None."""
    g = first_guess(canvas, picture, bg)
    if g is None:
        return None
    p = min(1.0, 3 * g[0])
    return _mse(shrink(canvas, p), p, *g, picture, bg)


def fit(canvas, picture, bg):
    """(mse, scale, x, y) of the best placement found, or None."""
    g = first_guess(canvas, picture, bg)
    if g is None:
        return None
    s, x, y = g
    p = min(1.0, 3 * s)
    small = shrink(canvas, p)
    best = _mse(small, p, s, x, y, picture, bg)
    ds, dxy = 0.04 * s, 1.0
    while dxy > 0.06:
        moved = False
        for cand in ((s + ds, x, y), (s - ds, x, y), (s, x + dxy, y), (s, x - dxy, y), (s, x, y + dxy), (s, x, y - dxy)):
            e = _mse(small, p, *cand, picture, bg)
            if e < best:
                best, (s, x, y), moved = e, cand, True
        if not moved:
            ds, dxy = ds / 2, dxy / 2
    return best, s, x, y


def centred(canvas, scale: float, size: int):
    """(x, y) that put the canvas's bounding box in the middle of a size x size picture."""
    x0, y0, x1, y1 = bbox(canvas[..., 3] > 0.5)
    return size / 2 - scale * (x0 + x1) / 2, size / 2 - scale * (y0 + y1) / 2
