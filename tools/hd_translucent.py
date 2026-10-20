"""List the object sprites that are mostly see-through, for vkproxy's depth ties.

Pieces on one plane (a window and the curtain hung over it, a sash and its handle) are drawn at
the very same depth, and the game's order decides which shows; the game changes that order when
an object is put down or the cursor passes over it, and on loading. vkproxy keeps the order
steady, and lets a sprite most of whose pixels are partly covered in the game's art (a sheer
curtain, a handle, a small detail) win such ties outright: it is meant to lie on top. Those
sprites are named, one content name a line (<w>x<h>_<hash>, as vkproxy names uploads), in
<game>/vkproxy/replace/translucent.names.

Usage:
    py tools/hd_translucent.py --game <dir>             count them
    py tools/hd_translucent.py --game <dir> --install   write the list
tools/hd_art.py --install writes it too when it is missing.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

SHARE = 0.5  # partly covered pixels, as a share of the drawn ones, from which a sprite counts
FILE = "translucent.names"


def is_translucent(tex: bytes) -> bool:
    """A sprite (B8G8R8A8) at least SHARE of whose drawn pixels are partly covered."""
    alpha = tex[3::4]
    drawn = len(alpha) - alpha.count(0)
    partial = drawn - alpha.count(255)
    return drawn > 0 and partial >= SHARE * drawn


def _file_names(job: tuple) -> list[str]:
    import hd_art
    data, chunks = job
    return [name for name, tex in hd_art.frames(data, chunks) if is_translucent(tex)]


def translucent_names(game: Path) -> list[str]:
    import hd_art
    jobs = ((data, chunks) for _name, data, chunks, _cat in hd_art.object_files(game, "all"))
    names: set[str] = set()
    with ProcessPoolExecutor() as pool:
        for found in pool.map(_file_names, jobs, chunksize=4):
            names.update(found)
    return sorted(names)


def install(game: Path, names: list[str] | None = None) -> int:
    names = translucent_names(game) if names is None else names
    folder = game / "vkproxy" / "replace"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / FILE).write_bytes(("\n".join(names) + "\n").encode("ascii"))
    return len(names)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="List the object sprites that are mostly see-through.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--install", action="store_true", help=f"write <game>/vkproxy/replace/{FILE}")
    args = ap.parse_args(argv)
    names = translucent_names(args.game)
    if args.install:
        install(args.game, names)
    print(f"{len(names)} mostly see-through sprites{' listed' if args.install else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
