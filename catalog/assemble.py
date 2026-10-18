"""Assemble an object's sprites the way the game draws them, as float images (numpy).

The rules come from recording the running game's draw calls (vkproxy's world log,
docs/FINDINGS.md):
  - every tile of the object draws the sprites of its DGRP image for the camera direction
    and zoom; tile (x, y) sits at x * ax + y * ay on screen (VIEW_AXES, near zoom; half of
    that at the middle zoom, a quarter at the far one);
  - a sprite's x is the DGRP x + 68 (the frame header x, mirrored for mirrored sprites;
    34 and 17 at the other zooms); its y is the SPR2 frame header y (= DGRP y + 348 - the
    frame's height: the DGRP y is the sprite's bottom, not its top);
  - each frame uses the SPR2 chunk's palette (spr2.frame_palette);
  - depth = z + GAME_K * (tile steps away from the camera) - DGRP_Z_K * (the sprite's DGRP z,
    its height, which also raises the sprite LIFT_PX per unit); a pixel is drawn when its alpha
    is at least 0.25, its z byte below 255 and its depth not behind what is there (ties go
    to the later draw). Sprites without a z-buffer (shadows) are drawn in order;
  - dynamic sprites (OBJD fields 62/63) are off unless the object turns them on;
  - tiles on an upper floor (OBJD level offset) are left out, as the catalogue pictures do.

Images are premultiplied RGBA float32 in 0..1, (height, width, 4).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

import objects
import spr2

# screen vectors (near zoom) of one step along tile x and tile y, per DGRP view
VIEW_AXES = {0: ((-64, 32), (64, 32)), 1: ((-64, -32), (-64, 32)),
             2: ((64, -32), (-64, -32)), 3: ((64, 32), (64, -32))}
NEAR_ZOOM = 3
ZOOM_SIZE = {3: 1.0, 2: 0.5, 1: 0.25}  # screen size of a DGRP zoom level relative to the near one
HEADER_X = 68  # DGRP x + 68 = frame header x
GAME_K = 0.00529 / 0.0279 * 254  # z units per tile step: the game's depth offset per tile / z range
# z units per unit of a sprite's DGRP z (its height above the tile: candy floss in a cart's
# bowl is 1.306, so the game draws it 0.001152 nearer than the cart, 1/6 of a tile step)
DGRP_Z_K = 0.001152 / 1.306 / 0.0279 * 254
LIFT_PX = 16  # and that many screen pixels (near zoom) higher per unit of DGRP z (21 for the floss)
TILE_STEP_Y = 32  # screen y (near zoom) of one tile step
MIN_ALPHA = 0.25
SCALE = 3  # HD sprites are 3x


class Sprites:
    """Decoded frames of one object, cached: the original (RGBA + z) and the HD twin."""

    def __init__(self, obj: objects.ObjectFile, hd_dir: Path | None = None):
        self.obj, self.hd_dir = obj, hd_dir
        self._orig: dict = {}
        self._hd: dict = {}

    def original(self, sid: int, frame: int):
        """(rgba, z uint8, header x, header y) of a frame, or None."""
        key = (sid, frame)
        if key not in self._orig:
            r = self.obj.frame(sid, frame)
            if r is None:
                self._orig[key] = None
            else:
                f, pal, hx, hy = r
                tex = spr2.game_texture(f, pal)
                rgba = np.frombuffer(tex, np.uint8).reshape(f.h, f.w, 4)[..., [2, 1, 0, 3]].astype(np.float32) / 255
                z = np.array(f.z, np.uint8).reshape(f.h, f.w)
                self._orig[key] = (rgba, z, hx, hy, spr2.content_name(f.w, f.h, tex))
        r = self._orig[key]
        return None if r is None else r[:4]


    def hd(self, sid: int, frame: int):
        """The HD twin (3x premultiplied RGBA) of a frame, or None when it is not built."""
        key = (sid, frame)
        if key not in self._hd:
            self._hd[key] = None
            if self.original(sid, frame) is not None and self.hd_dir is not None:
                rgba, _z, _hx, _hy, name = self._orig[key]
                p = self.hd_dir / f"{name}@{SCALE}.bgra"
                h, w = rgba.shape[0] * SCALE, rgba.shape[1] * SCALE
                if p.exists() and p.stat().st_size == w * h * 4:
                    tex = np.frombuffer(p.read_bytes(), np.uint8).reshape(h, w, 4)
                    self._hd[key] = tex[..., [2, 1, 0, 3]].astype(np.float32) / 255
        return self._hd[key]


def view_images(obj: objects.ObjectFile, dgrp_id: int, zoom: int = NEAR_ZOOM) -> list[dict]:
    """The images of a DGRP at one zoom, one per view."""
    return [im for im in obj.dgrp.get(dgrp_id, []) if im["zoom"] == zoom]


def is_dynamic(tile: dict, sid: int) -> bool:
    return bool(tile.get("dyn_n")) and tile["dyn_base"] <= sid < tile["dyn_base"] + tile["dyn_n"]


def tile_dgrp(tile: dict, graphic: int) -> int:
    """DGRP id of a tile in a state. Tiles can have fewer states than the object (a smoothie
    counter's jar has six, its other half one): those stay in their last one."""
    return tile["base"] + min(graphic, max(1, tile["ngraphics"] or 1) - 1)


def dynamic_ids(obj: objects.ObjectFile, tiles: list[dict], graphic: int, view: int) -> set[int]:
    """SPR2 ids of the dynamic sprites this state and view would draw."""
    ids = set()
    for t in tiles:
        views = view_images(obj, tile_dgrp(t, graphic))
        if view < len(views):
            ids |= {s["spr"] for s in views[view]["sprites"] if is_dynamic(t, s["spr"])}
    return ids


@dataclass
class Piece:
    """One sprite as drawn: its top-left on screen (x, y), tile steps away from the camera
    (depth order), image and z-buffer as drawn (mirrored when the DGRP says so)."""
    steps: float
    lift: float  # the DGRP z: drawn this much nearer
    x: int
    y: int
    rgba: np.ndarray
    z: np.ndarray
    sid: int
    frame: int
    flip: bool
    dynamic: bool


def place(sprites: Sprites, tiles: list[dict], graphic: int, view: int, zoom: int = NEAR_ZOOM, hd: bool = False,
          dynamic=False, all_dynamic: bool = False):
    """The pieces of the object in one state, view and zoom, or None when the view is missing
    (or, with hd, a sprite has no HD twin). dynamic: True / False for all dynamic sprites, or
    the set of SPR2 ids that are on; all_dynamic: keep every dynamic sprite, marked."""
    obj = sprites.obj
    zf = ZOOM_SIZE[zoom]
    ax, ay = ((round(v[0] * zf), round(v[1] * zf)) for v in VIEW_AXES[view])
    m = SCALE if hd else 1
    pieces = []
    for t in tiles:
        if tile_dgrp(t, graphic) not in obj.dgrp:
            continue
        views = view_images(obj, tile_dgrp(t, graphic), zoom)
        if view >= len(views):
            return None
        if t.get("level"):
            continue
        tx, ty = (t["sub"] & 0xFF, t["sub"] >> 8) if t["sub"] >= 0 else (0, 0)
        bx, by = tx * ax[0] + ty * ay[0], tx * ax[1] + ty * ay[1]
        for s in views[view]["sprites"]:
            dyn = is_dynamic(t, s["spr"])
            if dyn and not all_dynamic:
                on = s["spr"] in dynamic if isinstance(dynamic, (set, frozenset)) else bool(dynamic)
                if not on:
                    continue
            r = sprites.original(s["spr"], s["frame"])
            if r is None:
                continue
            rgba, z, _hx, hy = r
            if hd:
                rgba = sprites.hd(s["spr"], s["frame"])
                if rgba is None:
                    return None
                z = np.repeat(np.repeat(z, SCALE, 0), SCALE, 1)
            flip = bool(s["flags"] & 1)
            if flip:
                rgba, z = rgba[:, ::-1], z[:, ::-1]
            lift_px = round(LIFT_PX * zf * s["z"])
            pieces.append(Piece(by / (TILE_STEP_Y * zf), s["z"], m * (bx + s["ox"] + round(HEADER_X * zf)),
                                m * (by + hy - lift_px), rgba, z, s["spr"], s["frame"], flip, dyn))
    return pieces


def draw(pieces: list[Piece], bounds=None):
    """(canvas, x0, y0): the pieces drawn as the game does, on a canvas whose top-left is the
    screen point (x0, y0); bounds = (x0, y0, w, h), or the pieces' extent."""
    if bounds is None:
        x0, y0 = min(p.x for p in pieces), min(p.y for p in pieces)
        w = max(p.x + p.rgba.shape[1] for p in pieces) - x0
        h = max(p.y + p.rgba.shape[0] for p in pieces) - y0
    else:
        x0, y0, w, h = bounds
    canvas = np.zeros((h, w, 4), np.float32)
    zbuf = np.full((h, w), np.inf, np.float32)
    for p in sorted(pieces, key=lambda p: p.steps):  # tiles further back first
        ph, pw = p.z.shape
        cy0, cx0 = max(p.y - y0, 0), max(p.x - x0, 0)
        cy1, cx1 = min(p.y - y0 + ph, h), min(p.x - x0 + pw, w)
        if cy0 >= cy1 or cx0 >= cx1:
            continue
        sl = (slice(cy0, cy1), slice(cx0, cx1))
        src = (slice(cy0 - (p.y - y0), cy1 - (p.y - y0)), slice(cx0 - (p.x - x0), cx1 - (p.x - x0)))
        rgba, z = p.rgba[src], p.z[src]
        a = rgba[..., 3]
        if not (p.z[p.rgba[..., 3] > 0] < 255).any():  # no z-buffer: drawn in order
            mask = a > 0
        else:
            d = z.astype(np.float32) - GAME_K * p.steps - DGRP_Z_K * p.lift  # lower on screen = nearer
            mask = (a >= MIN_ALPHA) & (z < 255) & (d <= zbuf[sl])
            zbuf[sl] = np.where(mask, d, zbuf[sl])
        c = canvas[sl]
        canvas[sl] = np.where(mask[..., None], rgba + c * (1 - a[..., None]), c)
    return canvas, x0, y0


def assemble(sprites: Sprites, tiles: list[dict], graphic: int, view: int, hd: bool = False,
             dynamic=False, mirror: bool = False):
    """The object in one state (graphic) and view, near zoom, or None when nothing can be
    drawn (or, with hd, a sprite has no HD twin). dynamic: True / False for all dynamic
    sprites, or the set of SPR2 ids that are on. hd: from the HD twins, three times larger,
    same layout."""
    pieces = place(sprites, tiles, graphic, view, hd=hd, dynamic=dynamic)
    if not pieces:
        return None
    canvas = draw(pieces)[0]
    return canvas[:, ::-1] if mirror else canvas