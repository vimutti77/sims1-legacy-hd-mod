"""The composite step of tools/hd_composite.py on the GPU, for upscale.py --composite.

Same rules as hd_composite.composite (whose docstring says why), applied to the
upscaler's output while it is still on the GPU, so no 4x intermediate is written:
- coverage is exactly the original's nearest alpha, never more;
- fully opaque areas take the upscaler's colour, area-averaged to 3x and pulled
  halfway back towards the original (back-projection, hd_composite.BACKPROJECT);
- texels with partial alpha within one texel take the original sampled bilinearly
  from its non-empty texels.
The result equals hd_composite.composite up to rounding (+-1).
"""
from __future__ import annotations

import torch
import torch.nn.functional as F

SCALE = 3
BACKPROJECT = 0.5  # keep in step with tools/hd_composite.py
BACKPROJECT_ITERS = 3


def up_matrix(n: int, device) -> torch.Tensor:
    """(SCALE*n, n) bilinear weights (pixel centres, edges clamped), as hd_composite.up_weights."""
    src = (torch.arange(n * SCALE, device=device, dtype=torch.float64) + 0.5) / SCALE - 0.5
    j0 = torch.floor(src)
    t = src - j0
    m = torch.zeros(n * SCALE, n, device=device, dtype=torch.float64)
    rows = torch.arange(n * SCALE, device=device)
    m.index_put_((rows, j0.clamp(0, n - 1).long()), 1 - t, accumulate=True)
    m.index_put_((rows, (j0 + 1).clamp(0, n - 1).long()), t, accumulate=True)
    return m.float()


def backproject(orig: torch.Tensor, ai: torch.Tensor, strength: float = BACKPROJECT,
                iters: int = BACKPROJECT_ITERS) -> torch.Tensor:
    """ai (3, 3*h0, 3*w0) RGB 0..255 with its opaque texels pulled towards orig (premultiplied
    BGRA): the difference between each opaque texel and its 3x3 block mean is spread back
    bilinearly, iters times; the result is strength of the way there."""
    h0, w0 = orig.shape[:2]
    opaque = orig[..., 3] == 255
    target = orig[..., [2, 1, 0]].permute(2, 0, 1).float()
    uy, ux = up_matrix(h0, ai.device), up_matrix(w0, ai.device)
    mask3 = opaque.repeat_interleave(SCALE, 0).repeat_interleave(SCALE, 1)
    cur = ai.clone()
    for _ in range(iters):
        small = cur.reshape(3, h0, SCALE, w0, SCALE).mean(dim=(2, 4))
        res = torch.where(opaque, target - small, torch.zeros((), device=ai.device))
        cur = torch.where(mask3, (cur + uy @ res @ ux.T).clamp(0, 255), cur)
    return (ai + strength * (cur - ai) + 0.5).floor()


def area_matrix(n_in: int, n_out: int, device) -> torch.Tensor:
    """(n_out, n_in) weights of an area-average resize (hd_composite.area_weights)."""
    r = n_in / n_out
    lo = torch.arange(n_out, device=device, dtype=torch.float64)[:, None] * r
    j = torch.arange(n_in, device=device, dtype=torch.float64)[None, :]
    overlap = (torch.minimum(lo + r, j + 1) - torch.maximum(lo, j)).clamp(min=0)
    return (overlap / r).float()


def soft_sample(orig: torch.Tensor) -> torch.Tensor:
    """(3*h0, 3*w0, 4) float: orig sampled bilinearly (pixel centres, edges clamped) from its
    non-empty texels only, the weights renormalised; computed in double precision and in the
    reference's order, so it rounds exactly as hd_composite.composite does."""
    h0, w0 = orig.shape[:2]
    dev = orig.device

    def axis(n):
        f = (torch.arange(n * SCALE, device=dev, dtype=torch.float64) + 0.5) / SCALE - 0.5
        i0 = torch.floor(f).clamp(0, n - 1)
        t = (f - i0).clamp(0, 1)
        return i0.long(), (i0 + 1).clamp(max=n - 1).long(), t

    y0, y1, ty = axis(h0)
    x0, x1, tx = axis(w0)
    o = orig.double()
    num = torch.zeros(h0 * SCALE, w0 * SCALE, 4, device=dev, dtype=torch.float64)
    den = torch.zeros(h0 * SCALE, w0 * SCALE, device=dev, dtype=torch.float64)
    for yy, wy in ((y0, 1 - ty), (y1, ty)):
        for xx, wx in ((x0, 1 - tx), (x1, tx)):
            tap = o[yy][:, xx]
            f = (wx[None, :] * wy[:, None]) * (tap[..., 3] > 0)
            num = num + tap * f[..., None]
            den = den + f
    return (num / den.clamp(min=1e-12)[..., None] + 0.5).floor().clamp(0, 255).float()


def composite(orig: torch.Tensor, ai: torch.Tensor, backproject_strength: float = BACKPROJECT) -> torch.Tensor:
    """orig: (h0, w0, 4) uint8 premultiplied BGRA; ai: (3, s*h0, s*w0) RGB, values 0..255
    (s >= 3). Returns the (3*h0, 3*w0, 4) uint8 premultiplied BGRA HD texture."""
    h0, w0 = orig.shape[:2]
    H, W = h0 * SCALE, w0 * SCALE
    ai = ai.float()
    if ai.shape[1:] != (H, W):
        ai = area_matrix(ai.shape[1], H, ai.device) @ ai @ area_matrix(ai.shape[2], W, ai.device).T
        ai = (ai + 0.5).floor().clamp(0, 255)
    if backproject_strength:
        ai = backproject(orig, ai, backproject_strength)
    a = orig[..., 3]
    partial = ((a > 0) & (a < 255)).float()[None, None]
    soft = F.max_pool2d(partial, 3, stride=1, padding=1)[0, 0] > 0

    def up(m: torch.Tensor) -> torch.Tensor:
        return m.repeat_interleave(SCALE, 0).repeat_interleave(SCALE, 1)

    bilinear = soft_sample(orig)
    opaque = torch.cat([ai.flip(0).permute(1, 2, 0), torch.full((H, W, 1), 255.0, device=ai.device)], dim=2)
    out = torch.where(up(soft)[..., None], bilinear, opaque)
    out = torch.where(up(a > 0)[..., None], out, torch.zeros((), device=ai.device))
    return out.to(torch.uint8)
