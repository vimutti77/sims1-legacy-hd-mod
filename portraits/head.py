"""Draw a Sim's head (its head skins, tools/skn.py) the way the game drew its portraits.

The portraits stored in a character file were drawn with the head texture alone (no
lighting: a fit of ambient and diffuse light to them gave ambient 1.0, diffuse 0.0), from a
camera DIST head units away (a fitted value; nearly orthographic). A pose is (yaw, pitch,
roll, scale, cx, cy): the head turned, then drawn `scale` pixels per unit with the head
bone's origin at (cx, cy) in the picture. Drawn with `ss` x `ss` samples per pixel, textures
sampled bilinearly; returns the colour where the head is and its coverage per pixel.
"""
from __future__ import annotations

import numpy as np

DIST = 10.0
# head bone axes on the picture: x (up) -> up, y -> right, z (the face) -> towards the viewer
HEAD_AXES = np.array([[0, 1, 0], [-1, 0, 0], [0, 0, 1]], float)


class Head:
    """The triangles of a head's skins, as arrays."""

    def __init__(self, skins):
        pos, uv, faces = [], [], []
        base = 0
        for s in skins:
            v = np.asarray(s.vertices, float).reshape(-1, 6)
            pos.append(v[:, :3])
            t = np.zeros((len(v), 2))
            t[:len(s.uvs)] = np.asarray(s.uvs, float).reshape(-1, 2)
            uv.append(t)
            faces.append(np.asarray(s.faces, int).reshape(-1, 3) + base)
            base += len(v)
        self.pos = np.concatenate(pos)
        self.uv = np.concatenate(uv)
        self.faces = np.concatenate(faces)
        self.skin_of = np.concatenate([np.full(len(f), i) for i, f in enumerate(faces)])  # which texture


def rotation(yaw: float, pitch: float, roll: float) -> np.ndarray:
    cy, sy, cp, sp, cr, sr = np.cos(yaw), np.sin(yaw), np.cos(pitch), np.sin(pitch), np.cos(roll), np.sin(roll)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
    return rz @ rx @ ry @ HEAD_AXES


def project(head: Head, pose) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Picture x, y and depth (larger: nearer) of every vertex."""
    yaw, pitch, roll, scale, cx, cy = pose
    p = head.pos @ rotation(yaw, pitch, roll).T
    f = DIST / (DIST - p[:, 2])
    return p[:, 0] * f * scale + cx, p[:, 1] * f * scale + cy, p[:, 2]


def render(head: Head, textures: list[np.ndarray], pose, size: tuple[int, int], ss: int = 4):
    """(colour, coverage) of the head drawn into a w x h picture; textures: RGB float arrays,
    one per skin."""
    w, h = size
    W, H = w * ss, h * ss
    sx, sy, z = project(head, pose)
    sx, sy = sx * ss, sy * ss
    img = np.zeros((H, W, 3))
    cov = np.zeros((H, W))
    zbuf = np.full((H, W), -np.inf)
    for (a, b, c), skin in zip(head.faces, head.skin_of):
        xs, ys = np.array([sx[a], sx[b], sx[c]]), np.array([sy[a], sy[b], sy[c]])
        x0, x1 = max(0, int(np.floor(xs.min()))), min(W - 1, int(np.ceil(xs.max())))
        y0, y1 = max(0, int(np.floor(ys.min()))), min(H - 1, int(np.ceil(ys.max())))
        if x0 > x1 or y0 > y1:
            continue
        d = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
        if abs(d) < 1e-12:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        w0 = ((ys[1] - ys[2]) * (gx - xs[2]) + (xs[2] - xs[1]) * (gy - ys[2])) / d
        w1 = ((ys[2] - ys[0]) * (gx - xs[2]) + (xs[0] - xs[2]) * (gy - ys[2])) / d
        w2 = 1 - w0 - w1
        zz = w0 * z[a] + w1 * z[b] + w2 * z[c]
        zb = zbuf[y0:y1 + 1, x0:x1 + 1]
        m = (w0 >= 0) & (w1 >= 0) & (w2 >= 0) & (zz > zb)
        if not m.any():
            continue
        tex = textures[skin]
        th, tw = tex.shape[:2]
        u = (w0 * head.uv[a, 0] + w1 * head.uv[b, 0] + w2 * head.uv[c, 0])[m] * tw - 0.5
        v = (w0 * head.uv[a, 1] + w1 * head.uv[b, 1] + w2 * head.uv[c, 1])[m] * th - 0.5
        u0, v0 = np.floor(u).astype(int), np.floor(v).astype(int)
        fu, fv = (u - u0)[:, None], (v - v0)[:, None]
        ua, ub = np.clip(u0, 0, tw - 1), np.clip(u0 + 1, 0, tw - 1)
        va, vb = np.clip(v0, 0, th - 1), np.clip(v0 + 1, 0, th - 1)
        col = ((tex[va, ua] * (1 - fu) + tex[va, ub] * fu) * (1 - fv) + (tex[vb, ua] * (1 - fu) + tex[vb, ub] * fu) * fv)
        zb[m] = zz[m]
        img[y0:y1 + 1, x0:x1 + 1][m] = col
        cov[y0:y1 + 1, x0:x1 + 1][m] = 1
    return img.reshape(h, ss, w, ss, 3).mean(axis=(1, 3)), cov.reshape(h, ss, w, ss).mean(axis=(1, 3))


def compose(colour: np.ndarray, coverage: np.ndarray, background) -> np.ndarray:
    """The head over a plain background (colour is already weighted by coverage)."""
    return colour + (1 - coverage[..., None]) * np.asarray(background, float)
