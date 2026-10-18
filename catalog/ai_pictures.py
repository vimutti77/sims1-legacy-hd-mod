"""Catalogue pictures that cannot be drawn from sprites, upscaled by the AI instead.

Some pictures are icons painted on their own (magazines, a gift box of preserves): the
object has no sprite that looks like them. The pictures named in <work>/ai.txt (one
picture name per line, '#' starts a comment) are taken from the game, each 37x37 half
(normal, selected) upscaled on its own by ai_upscale/upscale.py --composite, which keeps
the colours close to the original (back-projection), and joined into
<work>/ai/<picture>@3.bgra. build.py --install installs them with the others; a picture
that can be drawn from sprites is drawn instead.

A picture can be cleaned up by hand before the upscaler sees it (stray pixels on an
outline grow into a ragged rim): --export writes the game's picture to
<work>/ai_input/<picture>.png (74x37; 37x37 will do when both halves are the same); edit it,
save it as an 8-bit PNG, and run again:
an input newer than its result is upscaled again, and the result then comes from it.

A picture redrawn by hand at any size (<work>/art/<picture>.png, the object on a plain
background) is used instead of the upscaler: it is scaled and placed onto the game's
picture as build.py places sprites (fit.py), over the picture's own background.

Usage:
    py catalog/ai_pictures.py --game <dir> [--work work/catalog] [--model ultrasharp-v2]
    py catalog/ai_pictures.py --game <dir> --export <picture> ...
Needs the ai_upscale venv (--python, default work/ext/torch) and the GPU: close the game.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import fit  # noqa: E402
import hd_art  # noqa: E402
import hd_composite  # noqa: E402
import iff  # noqa: E402
import objects  # noqa: E402
import spr2  # noqa: E402

HALF = objects.PICTURE_SIZE[1]
SCALE = 3


def game_pictures(game: Path, wanted: set[str]) -> dict[str, bytes]:
    """picture name -> RGBA of the wanted catalogue pictures."""
    out = {}
    for pattern in (p for k in sorted(hd_art.PACKS) for p in hd_art.PACKS[k]):
        for path in sorted(game.glob(pattern)):
            for _name, data in spr2.iter_iffs(path):
                if b"BMP_" not in data:
                    continue
                try:
                    chunks = iff.read_iff(data)
                except iff.IffError:
                    continue
                for c in chunks:
                    if c.type != "BMP_":
                        continue
                    try:
                        w, h, px = objects.decode_bmp(data[c.offset:c.offset + c.size])
                    except objects.FormatError:
                        continue
                    if (w, h) == objects.PICTURE_SIZE and objects.picture_name(w, h, px) in wanted:
                        out[objects.picture_name(w, h, px)] = px
    return out


def half(rgba: bytes, k: int) -> bytes:
    """RGBA of the k-th 37x37 half of a 74x37 picture."""
    w = objects.PICTURE_SIZE[0]
    return b"".join(rgba[(y * w + k * HALF) * 4:(y * w + (k + 1) * HALF) * 4] for y in range(HALF))


def same_halves(rgba: bytes, tolerance: int = 16) -> bool:
    """Whether the normal and selected halves are the same picture (inventory icons; a few
    pixels can differ slightly)."""
    return max(abs(a - b) for a, b in zip(half(rgba, 0), half(rgba, 1))) <= tolerance


def fit_art(art: bytes, aw: int, ah: int, picture: bytes):
    """(BGRA 222x111, rms colour error 0-255) of a drawing placed onto a game picture."""
    import numpy as np  # only this needs numpy
    img = np.frombuffer(art, np.uint8).reshape(ah, aw, 4)[..., :3].astype(np.float32) / 255
    art_bg = fit.background(img)
    cover = np.clip((np.abs(img - art_bg).max(axis=2) - 10 / 255) / (40 / 255), 0, 1)[..., None]
    canvas = np.concatenate([img * cover, cover], axis=2)  # premultiplied, as fit.py takes
    pic = np.frombuffer(picture, np.uint8).reshape(HALF, 2 * HALF, 4)[..., :3].astype(np.float32) / 255
    target, bg = pic[:, :HALF], fit.background(pic[:, :HALF])
    mse, s, x, y = fit.fit(canvas, target, bg)
    halves = []
    for k in (0, 1):
        t = pic[:, k * HALF:(k + 1) * HALF]
        add = 0.0 if same_halves(picture) else build.SELECTED_ADD * k
        out = fit.render(canvas, s * SCALE, x * SCALE, y * SCALE, HALF * SCALE, fit.background(t), add=add)
        bgra = np.concatenate([out[..., ::-1], np.ones(out.shape[:2] + (1,), np.float32)], axis=2)
        halves.append((bgra * 255 + 0.5).astype(np.uint8).tobytes())
    return join(halves), float(np.sqrt(mse)) * 255


def join(halves: list[bytes]) -> bytes:
    """Two (3*37)^2 BGRA halves side by side."""
    n = HALF * SCALE
    return b"".join(h[y * n * 4:(y + 1) * n * 4] for y in range(n) for h in halves)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Upscale catalogue pictures that cannot be drawn from sprites.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=REPO / "work" / "catalog")
    ap.add_argument("--model", default="ultrasharp-v2")
    ap.add_argument("--python", type=Path, default=REPO / "work" / "ext" / "torch" / "Scripts" / "python.exe")
    ap.add_argument("--export", nargs="+", metavar="PICTURE", help="write these pictures to edit before upscaling")
    args = ap.parse_args(argv)
    out_dir, in_dir = args.work / "ai", args.work / "ai_input"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.export:
        in_dir.mkdir(parents=True, exist_ok=True)
        for pic, rgba in game_pictures(args.game, set(args.export)).items():
            hd_composite.write_png(in_dir / f"{pic}.png", *objects.PICTURE_SIZE, rgba)
            print("wrote", in_dir / f"{pic}.png")
        return 0

    art_dir = args.work / "art"

    def stale(pic):
        out = out_dir / f"{pic}@3.bgra"
        return not out.exists() or any(p.exists() and p.stat().st_mtime > out.stat().st_mtime
                                       for p in (in_dir / f"{pic}.png", art_dir / f"{pic}.png"))

    todo = {p for p in build.read_list(args.work / "ai.txt") if stale(p)}
    drawn = {p for p in todo if (art_dir / f"{p}.png").exists()}  # redrawn by hand: no upscaler
    if drawn:
        for pic, rgba in game_pictures(args.game, drawn).items():
            aw, ah, art = hd_composite.read_png(art_dir / f"{pic}.png")
            out, err = fit_art(bytes(art), aw, ah, rgba)
            (out_dir / f"{pic}@3.bgra").write_bytes(out)
            print(f"{pic}: drawing placed onto the picture, colour error {err:.1f}")
        todo -= drawn
    if not todo:
        print("nothing to upscale")
        return 0
    if hd_art.game_running():
        print("The game is running: close it before upscaling (GPU).")
        return 1
    pictures = game_pictures(args.game, todo)
    for pic in todo:  # pictures cleaned up by hand
        edited = in_dir / f"{pic}.png"
        if edited.exists():
            w, h, px = hd_composite.read_png(edited)
            if (w, h) == (HALF, HALF) and pic in pictures and same_halves(pictures[pic]):
                # one half edited, of a picture whose halves are the same (inventory icons)
                px = bytearray(b"".join(px[y * HALF * 4:(y + 1) * HALF * 4] * 2 for y in range(HALF)))
                w = 2 * HALF
            if (w, h) != objects.PICTURE_SIZE:
                print(f"{edited.name}: {w}x{h}, not {objects.PICTURE_SIZE[0]}x{objects.PICTURE_SIZE[1]}; skipped")
                pictures.pop(pic, None)
                continue
            px[3::4] = b"\xff" * (w * h)  # the picture is opaque
            pictures[pic] = bytes(px)
    missing = todo - set(pictures)
    if missing:
        print("not in the game:", ", ".join(sorted(missing)))
    tmp = args.work / "ai_tmp"
    for d in ("orig", "in", "out"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    names = []
    for pic, rgba in pictures.items():
        for k in (0, 1):
            name = f"{HALF}x{HALF}_{pic.split('_', 1)[1]}{'ab'[k]}"  # size first: upscale.py reads it
            px = half(rgba, k)
            (tmp / "out" / f"{name}@3.bgra").unlink(missing_ok=True)  # upscale.py skips what exists
            (tmp / "orig" / f"{name}.bgra").write_bytes(objects.to_bgra(px))
            hd_composite.write_png(tmp / "in" / f"{name}.png", HALF, HALF, px)
            names.append(name)
    (tmp / "names.txt").write_text("\n".join(names) + "\n")
    cmd = [str(args.python), "-W", "ignore", str(REPO / "ai_upscale" / "upscale.py"), "--model", args.model,
           "--composite", str(tmp / "orig"), "--names", str(tmp / "names.txt"), "--workers", "1",
           str(tmp / "in"), str(tmp / "out")]
    if subprocess.run(cmd).returncode != 0:
        return 1
    for pic in pictures:
        halves = [(tmp / "out" / f"{HALF}x{HALF}_{pic.split('_', 1)[1]}{c}@3.bgra").read_bytes() for c in "ab"]
        (out_dir / f"{pic}@3.bgra").write_bytes(join(halves))
    print(f"{len(pictures)} pictures upscaled into {out_dir}; install with build.py --install-only")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
