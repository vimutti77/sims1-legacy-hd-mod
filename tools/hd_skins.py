"""Build HD textures for the Sims' 3D meshes (skins) for vkproxy from the user's own game files.

Sims, pets and the things they hold are 3D meshes with 8-bit BMP textures: bodies 256x256,
heads 128x128, hands 64x64, accessories and effects mostly smaller. They are loose in the
Skins folders (GameData/Skins, ExpansionShared/SkinsBuy, ExpansionPack2/Skins) and inside the
archives (GameData/Textures/Textures.far, the packs' Skins/, Accessories/ and
ExpansionPackAssets/, Downloads/). Legacy uploads each one opaque as B8G8R8A8 truncated to
RGB565, like the UI bitmaps (hd_ui.upload; checked byte for byte against the uploads of 16
Sims' textures), so the name vkproxy matches an upload by is known from the file.

Steps, each skipping work already done (so interrupted runs resume):
  extract  every texture of at least MIN_SIDE x MIN_SIDE, at the file's full colour (not
           truncated: the AI and the back-projection work from the art itself), into
           <work>/orig/<name>.bgra
  prep     -> <work>/in/<name>.png (hd_composite.prep_one; opaque, so nothing to fill)
  upscale  ai_upscale/upscale.py --model M --composite -> <work>/hd/<M>/<name>@3.bgra (the venv)
  install  copy them into <game>/vkproxy/replace/ (packed) and list them in mesh.names
           there: vkproxy gives these twins mipmaps and samples them trilinearly, since a
           mesh is drawn at any size (three zoom levels), not at the size it was made for
           like a sprite.

Usage:
    py tools/hd_skins.py --game <dir> --list                       textures per size
    py tools/hd_skins.py --game <dir> [--files Cornelia ...] [--work <dir>] [--install]
The HD textures take ~7 GB for the whole game (and as much again in --work until it is
deleted). Close the game before running: the upscaler uses the GPU.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import bmp
import far
import hd_art
import hd_composite
import hd_pack
import hd_ui
import spr2

REPO = Path(__file__).resolve().parents[1]
MIN_SIDE = 32  # smaller textures (sparkles, 8x8 and 16x16 bits) are left as they are
FILE = "mesh.names"
SKIP_ARCHIVES = ("uigraphics.far", "sound.far", "sounddata.far")


def texture_files(game: Path):
    """(where, BMP bytes) of every mesh texture in the install: BMPs in the Skins folders and
    BMP members of the archives (not the UI's or the sounds')."""
    paths = []
    for root, dirs, files in os.walk(game):
        dirs[:] = [d for d in dirs if d.lower() != "vkproxy"]  # our own files (HD packs, backups)
        paths += [Path(root) / f for f in files]
    for path in sorted(paths):
        suffix = path.suffix.lower()
        if suffix == ".bmp" and any(p.lower().startswith("skins") for p in path.relative_to(game).parts[:-1]):
            yield str(path.relative_to(game)), path.read_bytes()
        elif suffix == ".far" and path.name.lower() not in SKIP_ARCHIVES:
            data = path.read_bytes()
            try:
                entries = far.read_far(data)
            except far.FarError:
                continue
            for e in entries:
                if e.name.lower().endswith(".bmp"):
                    yield f"{path.relative_to(game)}/{e.name}", data[e.offset:e.offset + e.size]


def textures(game: Path, min_side: int = MIN_SIDE):
    """(name vkproxy matches, w, h, full-colour B8G8R8A8, where) of every distinct mesh
    texture of at least min_side x min_side."""
    seen = set()
    for where, data in texture_files(game):
        try:
            w, h, rgba = bmp.decode_bmp(data)
        except bmp.FormatError:
            continue
        if w < min_side or h < min_side:
            continue
        name = spr2.content_name(w, h, hd_ui.upload(rgba))
        if name in seen:
            continue
        seen.add(name)
        bgra = bytearray(rgba)
        bgra[0::4], bgra[2::4] = rgba[2::4], rgba[0::4]
        yield name, w, h, bytes(bgra), where


def install_names(folder: Path, names) -> int:
    """Add names to <folder>/mesh.names (kept: textures installed by an earlier run)."""
    path = folder / FILE
    have = set(path.read_text(encoding="ascii").split()) if path.exists() else set()
    have |= set(names)
    folder.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(sorted(have)) + "\n").encode("ascii"))
    return len(have)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Build HD textures for the Sims' meshes for vkproxy.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=REPO / "work" / "hd_skins")
    ap.add_argument("--files", nargs="*", default=[], help="only textures whose path contains one of these strings")
    ap.add_argument("--model", default="ultrasharp-v2", help="name in ai_upscale/models.json or a model file")
    ap.add_argument("--python", type=Path, default=REPO / "work" / "ext" / "torch" / "Scripts" / "python.exe",
                    help="python of the ai_upscale venv")
    ap.add_argument("--workers", type=int, default=2, help="upscaler processes sharing the GPU")
    ap.add_argument("--install", action="store_true", help="copy the HD textures into <game>/vkproxy/replace")
    ap.add_argument("--list", action="store_true", help="textures per size, then exit")
    args = ap.parse_args(argv)

    if args.list:
        sizes = Counter((w, h) for _name, w, h, _tex, _where in textures(args.game))
        for (w, h), n in sizes.most_common():
            print(f"{w}x{h}: {n}")
        print(f"{sum(sizes.values())} textures")
        return 0

    t0 = [time.time()]

    def lap() -> str:
        t, t0[0] = t0[0], time.time()
        return f"{t0[0] - t:.0f} s"

    hd = args.work / "hd" / Path(args.model).stem
    orig, pngs = args.work / "orig", args.work / "in"
    for d in (orig, pngs, hd):
        d.mkdir(parents=True, exist_ok=True)

    # 1. extract
    names = []
    for name, _w, _h, tex, where in textures(args.game):
        if args.files and not any(k.lower() in where.lower() for k in args.files):
            continue
        names.append(name)
        if not (orig / f"{name}.bgra").exists():
            (orig / f"{name}.bgra").write_bytes(tex)
    print(f"{len(names)} distinct textures ({lap()})")

    # 2. prep
    new = [n for n in names if not (pngs / f"{n}.png").exists()]
    n_prep = hd_composite.run_all([(orig / f"{n}.bgra", pngs / f"{n}.png") for n in new], hd_composite.prep_one)
    print(f"prep: {n_prep} new ({lap()})")

    # 3. upscale + composite
    todo = [n for n in names if not (hd / f"{n}@3.bgra").exists()]
    if todo:
        if hd_art.game_running():
            print("The game is running: close it before upscaling (GPU).")
            return 1
        todo_file = args.work / "todo.txt"
        todo_file.write_text("\n".join(todo) + "\n", encoding="utf-8")
        cmd = [str(args.python), "-W", "ignore", str(REPO / "ai_upscale" / "upscale.py"), "--model", args.model,
               "--composite", str(orig), "--names", str(todo_file), "--workers", str(args.workers),
               str(pngs), str(hd)]
        print("upscale:", " ".join(cmd), flush=True)
        if subprocess.run(cmd).returncode != 0:
            return 1
        todo_file.unlink()
        print(f"upscale: {lap()}")

    # 4. install (files that are new or rebuilt since the last install)
    if args.install:
        dest = hd_pack.Installed(args.game / "vkproxy" / "replace")
        n, done = 0, []
        for name in names:
            src = hd / f"{name}@3.bgra"
            if not src.exists():
                continue
            done.append(name)
            installed = dest.mtime(f"{name}@3.bgra")
            if installed is not None and installed >= src.stat().st_mtime:
                continue
            dest.copy(src, f"{name}@3.bgra")
            n += 1
        listed = install_names(dest.folder, done)
        hd_pack.finish_install(dest)
        print(f"installed {n} HD textures into {dest.folder}, {listed} listed in {FILE} "
              f"({lap()}; restart the game to load them)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
