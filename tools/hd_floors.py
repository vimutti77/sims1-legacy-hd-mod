"""Smooth the outline of the installed HD floor tiles (vkproxy draws them in two parts).

A floor tile is a diamond w = 2h - 1 wide that the game lays edge to edge, the next tile
((w + 1) / 2, h / 2) away. Its stair-stepped outline interlocks with the next tile's: unseen
between tiles of one floor, a sawtooth between two floors or along the grass. This adds,
outside the tile's own coverage, the pixels its straightened outline (tools/stairs.py) covers
in part, coloured from the tile itself one tile further on (the floor continues there), with
the part covered as alpha (1-254; the tile's own pixels stay 255, as the game's are). vkproxy
draws a tile's own pixels as the game does and, once all floor tiles are drawn and before the
walls, these partly covered ones without depth writes, over the teeth of the tile next to it.

Usage:
    py tools/hd_floors.py --game <dir>             count the tiles that would change
    py tools/hd_floors.py --game <dir> --install   rewrite them in <game>/vkproxy/replace
Every HD floor build (tools/hd_art.py --category floors --install) applies it as well.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import hd_pack
import stairs

S = stairs.SCALE


def is_tile(w: int, h: int, tex: bytes) -> bool:
    """A floor diamond whose alpha is all 0 or 255 (the game's floors are)."""
    return w + 1 == 2 * h and not (set(tex[3::4]) - {0, 255})


def edges(w: int, h: int, tex: bytes, hd: bytes) -> bytes:
    """The HD tile `hd` (premultiplied B8G8R8A8, S x the 1x tile `tex`) with its outline's
    partly covered pixels added outside the 1x coverage (any added before are replaced)."""
    mask = bytes(1 if a else 0 for a in tex[3::4])
    cov = stairs.upscale(mask, w, h)
    W, H = w * S, h * S
    a, b = (w + 1) // 2 * S, h // 2 * S
    shifts = ((a, b), (-a, b), (a, -b), (-a, -b), (2 * a, 0), (-2 * a, 0), (0, 2 * b), (0, -2 * b))
    out = bytearray(hd)

    def own(x: int, y: int) -> bool:
        return 0 <= x < W and 0 <= y < H and mask[(y // S) * w + x // S] and hd[(y * W + x) * 4 + 3] == 255

    for Y in range(H):
        for X in range(W):
            if mask[(Y // S) * w + X // S]:
                continue
            i = Y * W + X
            c = cov[i]
            if not c:
                out[i * 4:i * 4 + 4] = b"\0\0\0\0"
                continue
            src = next(((X - dx, Y - dy) for dx, dy in shifts if own(X - dx, Y - dy)), None)
            if src is None:  # by the diamond's corners: the nearest of the tile's own pixels
                near = [(abs(dx) + abs(dy), X + dx, Y + dy) for dy in range(-2 * S, 2 * S + 1)
                        for dx in range(-2 * S, 2 * S + 1) if own(X + dx, Y + dy)]
                if not near:
                    out[i * 4:i * 4 + 4] = b"\0\0\0\0"
                    continue
                src = min(near)[1:]
            j = (src[1] * W + src[0]) * 4
            c = min(max(c, 1), 254)
            out[i * 4:i * 4 + 3] = bytes((hd[j + k] * c + 127) // 255 for k in range(3))
            out[i * 4 + 3] = c
    return bytes(out)


def _one(args: tuple) -> tuple[str, bytes | None]:
    name, w, h, tex, hd = args
    new = edges(w, h, tex, hd)
    return name, (new if new != hd else None)


def floor_tiles(game: Path):
    """(name, w, h, 1x texture) of every floor tile of the game and its packs."""
    import hd_art
    seen = set()
    for _fname, data, chunks, _cat in hd_art.floor_files(game, "all"):
        for name, tex in hd_art.frames(data, chunks):
            w, h = map(int, name.split("_", 1)[0].split("x"))
            if name not in seen and is_tile(w, h, tex):
                seen.add(name)
                yield name, w, h, tex


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Smooth the outline of the installed HD floor tiles.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--install", action="store_true", help="rewrite them in <game>/vkproxy/replace")
    args = ap.parse_args(argv)
    dest = hd_pack.Installed(args.game / "vkproxy" / "replace")
    jobs = []
    for name, w, h, tex in floor_tiles(args.game):
        hd = dest.read(f"{name}@3.bgra")
        if hd is not None and len(hd) == w * h * S * S * 4:
            jobs.append((name, w, h, tex, hd))
    changed = 0
    with ProcessPoolExecutor() as pool:
        for name, new in pool.map(_one, jobs, chunksize=8):
            if new is None:
                continue
            changed += 1
            if args.install:
                dest.write(f"{name}@3.bgra", new)
    if args.install and changed:
        hd_pack.finish_install(dest)
    print(f"{len(jobs)} HD floor tiles, {changed} {'rewritten' if args.install else 'to rewrite (--install)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
