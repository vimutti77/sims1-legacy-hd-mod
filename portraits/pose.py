"""Find the pose a stored portrait was drawn with: the head drawn at 1x matched to the picture.

fit_view: the head's outline in the picture gives a first scale and place for a grid of
turns (yaw, pitch, roll); the best of those is refined by Nelder-Mead on the mean colour
difference (2 x 2 samples per pixel), and the error is measured again with 4 x 4.
Measured on the game's pictures: 4.7-7.1 / 255 per channel for the control panel faces of
expansion-pack and base-game Sims alike (the base game's were drawn larger and turned
differently, so every picture is fitted on its own).
"""
from __future__ import annotations

import numpy as np

from head import Head, compose, project, render

YAWS = (-0.4, -0.2, 0.0, 0.2, 0.4)
PITCHES = (-0.4, -0.2, 0.0, 0.2)
ROLLS = (-0.15, 0.0, 0.15)
WIDTHS = (0.85, 1.0, 1.2)  # the head's outline against the picture's: hair can be cut off at the sides
REFINED = 3                # best starts refined
LIMITS = np.array([0.9, 0.9, 0.5])  # yaw, pitch, roll beyond which a fit is wrong
STEP = np.array([0.08, 0.08, 0.08, 2.0, 1.0, 1.0])  # first simplex: radians, px per unit, px
OUTLINE = 24  # summed RGB difference from the background from which a pixel is the head's


def nelder_mead(f, x0, step, iters: int = 500, tol: float = 1e-3):
    """Minimise f from x0 (Nelder-Mead, standard coefficients); (x, f(x))."""
    n = len(x0)
    pts = [np.asarray(x0, float)] + [np.asarray(x0, float) + np.eye(n)[i] * step[i] for i in range(n)]
    vals = [f(p) for p in pts]
    for _ in range(iters):
        order = np.argsort(vals)
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        if vals[-1] - vals[0] < tol and max(np.abs(p - pts[0]).max() for p in pts[1:]) < tol:
            break
        centre = np.mean(pts[:-1], axis=0)
        r = centre + (centre - pts[-1])
        fr = f(r)
        if fr < vals[0]:
            e = centre + 2 * (centre - pts[-1])
            fe = f(e)
            pts[-1], vals[-1] = (e, fe) if fe < fr else (r, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = r, fr
        else:
            c = centre + 0.5 * (pts[-1] - centre) if fr >= vals[-1] else centre + 0.5 * (r - centre)
            fc = f(c)
            if fc < min(fr, vals[-1]):
                pts[-1], vals[-1] = c, fc
            else:  # shrink towards the best
                pts = [pts[0]] + [pts[0] + 0.5 * (p - pts[0]) for p in pts[1:]]
                vals = [vals[0]] + [f(p) for p in pts[1:]]
    i = int(np.argmin(vals))
    return pts[i], vals[i]


def outline_box(picture: np.ndarray, background) -> tuple[float, float, float, float] | None:
    """(left, top, right, bottom) of the pixels that differ from the background, or None."""
    m = np.abs(picture - np.asarray(background, float)).sum(-1) > OUTLINE
    if not m.any():
        return None
    ys, xs = np.nonzero(m)
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def first_guesses(head: Head, turn, box, size) -> list[np.ndarray]:
    """Poses with this turn whose outline is as wide as the picture's (or a little narrower or
    wider), its top at the picture's top, or its centre at the picture's centre when the
    hair is cut off at the top (a beehive)."""
    sx, sy, _ = project(head, (*turn, 1.0, 0.0, 0.0))
    left, top, right, _ = box
    out = []
    for k in WIDTHS:
        scale = k * (right - left) / max(1e-6, sx.max() - sx.min())
        cx = (left + right) / 2 - (sx.max() + sx.min()) / 2 * scale
        for cy in (top - sy.min() * scale, size[1] / 2 - (sy.max() + sy.min()) / 2 * scale):
            out.append(np.array([*turn, scale, cx, cy]))
    return out


def fit_view(head: Head, textures, picture: np.ndarray, background) -> tuple[np.ndarray, float] | None:
    """(pose, mean error per channel) of a head drawn into this picture (RGB floats), or None
    when the picture shows no head. A pose turned further than LIMITS (upside down: a tall
    hat filling the picture) counts as no fit."""
    h, w = picture.shape[:2]
    box = outline_box(picture, background)
    if box is None:
        return None

    def err(pose, ss=2):
        if np.any(np.abs(pose[:3]) > LIMITS) or pose[3] <= 0:
            return 1e9
        colour, cov = render(head, textures, pose, (w, h), ss)
        return float(np.abs(compose(colour, cov, background) - picture).mean())

    starts = [s for y in YAWS for p in PITCHES for r in ROLLS for s in first_guesses(head, (y, p, r), box, (w, h))]
    ranked = sorted(starts, key=lambda s: err(s, 1))
    pose, e = min((nelder_mead(err, s, STEP) for s in ranked[:REFINED]), key=lambda t: t[1])
    if e >= 1e9:
        return None
    return pose, err(pose, 4)
