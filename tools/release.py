"""Lay out the mod's release files from this machine's install: one folder (and archive) per part.

Players get the finished files (the user's decision, 2026-10-22), shipped apart from the repo
(Nexus Mods). Each part is a folder laid out like the game folder, to be extracted into it; the
mod replaces no file of the game's, so removing vulkan-1.dll and the vkproxy folder uninstalls
it. The parts, apart from core, can be had or left out each on its own:

  core       vulkan-1.dll (the release build), the font atlases (every scale built), the tile
             cursor and the control panel's gradients, README.txt
  objects    object sprites (every zoom), catalogue pictures, depth twins, translucent.names
  walls      wallpapers, wall tops, floors, the wall masks (*.wallmasks)
  sims       the Sims' textures, mesh.names
  portraits  the ready-made Sims' portraits (from the game folder's own families) and the
             portrait library (portraits.lib); pictures of this player's own Sims are left out

The HD files of a part go into its own packs, vkproxy/replace/hd_<part digit><NNN>.pack, so parts
never overwrite each other. Which part a file belongs to is worked out from the game's files
(about a minute on all cores); files that belong to none are listed and left out.

Usage:
    py tools/release.py --game <dir> --version 0.1                    every part, into
                                         %LOCALAPPDATA%/sims1-legacy-hd-mod/release/0.1
    py tools/release.py --game <dir> --version 0.1 --archive          ... and a .7z of each (zip without 7z)
    py tools/release.py --game <dir> --version 0.1 --parts core walls
Build the release DLL first: powershell -File vkproxy/build.ps1 -Release
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import bmp
import hd_art
import hd_pack
import hd_shapes
import hd_ui
import hd_walls
import iff
import spr2

REPO = Path(__file__).resolve().parents[1]
PARTS = {"core": 0, "objects": 1, "walls": 2, "sims": 3, "portraits": 4}  # name -> pack number digit
PACK_BYTES = 1 << 30
PORTRAIT_CHUNKS = range(2002, 2008)  # BMP_ chunks of a character file: its portraits (portraits/redraw.py)
LISTS = {"translucent.names": "objects", "mesh.names": "sims", "portraits.lib": "portraits"}
README = REPO / "docs" / "README_players.txt"


def _object_names(job) -> set[str]:
    """Content names of one IFF's frames and of their depth textures."""
    data, floors = job
    names = set()
    for name, _tex, f in hd_art.decoded_frames(data, iff.read_iff(data)):
        names.add(name)
        if not floors:
            depth = hd_art.depth_texture(f)
            if any(depth):
                names.add(spr2.content_name(f.w, f.h, depth) + "_f9")
    return names


def game_names(game: Path, log=print) -> dict[str, set[str]]:
    """Part -> content names (no @scale) that belong to it, from the game's files."""
    t0 = time.time()
    jobs = [(data, False) for _n, data, _c, _cat in hd_art.object_files(game, "all")]
    floor_jobs = [(data, True) for _n, data, _c, _cat in hd_art.floor_files(game, "all")]
    objects, floors = set(), set()
    with ProcessPoolExecutor() as ex:
        for names in ex.map(_object_names, jobs, chunksize=4):
            objects |= names
        for names in ex.map(_object_names, floor_jobs, chunksize=4):
            floors |= names
    log(f"  objects and floors: {len(objects)} and {len(floors)} names ({time.time() - t0:.0f} s)")
    tops = {spr2.content_name(w, h, tex) for w, h, tex, _m in hd_walls.top_frames(game)}
    core = {name for name, *_ in hd_shapes.shape_frames(game)} | {name for name, *_ in hd_ui.gradients(game)}
    portraits = set()
    for d in sorted(game.glob("*")):  # the families the game comes with, not this player's saves
        if d.is_dir() and d.name.lower().startswith(("userdata", "template")):
            for path in sorted((d / "Characters").glob("*.iff")):
                data = path.read_bytes()
                try:
                    chunks = iff.read_iff(data)
                except iff.IffError:
                    continue
                for c in chunks:
                    if c.type == "BMP_" and c.id in PORTRAIT_CHUNKS:
                        try:
                            w, h, rgba = bmp.decode_bmp(data[c.offset:c.offset + c.size])
                        except bmp.FormatError:
                            continue
                        portraits.add(spr2.content_name(w, h, hd_ui.upload(rgba)))
    return {"core": core, "objects": objects, "walls": tops | floors, "portraits": portraits}


def library_names(path: Path) -> set[str]:
    """The portrait library's pictures (portraits/library.py write_library), as content names."""
    data = path.read_bytes()
    if data[:4] != b"PLIB":
        raise ValueError(f"{path}: not a portrait library")
    _version, kinds = struct.unpack_from("<II", data, 4)
    p, names = 12, set()
    for _ in range(kinds):
        w, h, count, _e, _m = struct.unpack_from("<IIIff", data, p)
        p += 20
        for _ in range(count):
            (hid,) = struct.unpack_from("<Q", data, p)
            names.add(f"{w}x{h}_{hid:016x}")
            p += 8 + w * h * 4
    return names


def classify(game: Path, replace: Path, inst: hd_pack.Installed, log=print):
    """Part -> installed content files (name@scale.ext), and the files of no part."""
    known = game_names(game, log)
    mesh = set((replace / "mesh.names").read_text().split()) if (replace / "mesh.names").exists() else set()
    library = library_names(replace / "portraits.lib") if (replace / "portraits.lib").exists() else set()
    parts = {p: [] for p in PARTS}
    left = []
    for name in sorted(inst.names()):
        base = name.split("@")[0]
        if base in known["core"]:
            parts["core"].append(name)
        elif base in mesh:
            parts["sims"].append(name)
        elif re.search(r"_w\d+$", base) or base in known["walls"]:
            parts["walls"].append(name)
        elif base in known["objects"] or base.startswith("74x37_"):  # catalogue pictures: 74x37
            parts["objects"].append(name)
        elif base in known["portraits"] or base in library:
            parts["portraits"].append(name)
        else:
            left.append(name)
    return parts, left


def newer_than_game(game: Path) -> list[Path]:
    """Object files newer than the game: custom content, not to ship."""
    stamp = (game / "Sims.exe").stat().st_mtime + 24 * 3600
    found = []
    for pattern in ("Downloads/**/*.far", "Downloads/**/*.iff", "GameData/Objects/*.iff"):
        found += [p for p in game.glob(pattern) if p.stat().st_mtime > stamp]
    return found


def write_part(part: str, names: list[str], inst: hd_pack.Installed, replace: Path, game: Path, out: Path,
               dll: Path, log=print) -> None:
    root = out / part
    if root.exists():
        shutil.rmtree(root)
    dest = root / "vkproxy" / "replace"
    dest.mkdir(parents=True)
    digit, number, i = PARTS[part], 0, 0
    while i < len(names):
        pack = dest / f"hd_{digit}{number:03d}.pack"
        size = 0

        def files():
            nonlocal i, size
            while i < len(names) and size < PACK_BYTES:
                data = inst.read(names[i])
                size += len(data)
                yield names[i], data, 0.0
                i += 1

        index = hd_pack.write_pack(pack, files())
        log(f"  {part}: {pack.name}, {len(index)} files, {size / 2**20:.0f} MB")
        number += 1
    for f, owner in LISTS.items():
        if owner == part and (replace / f).exists():
            shutil.copy2(replace / f, dest / f)
    if part == "walls":
        for f in replace.glob("*.wallmasks"):
            shutil.copy2(f, dest / f.name)
    if part == "core":
        for f in replace.iterdir():  # font atlases: <w>x<h>@<scale>.bgra, loose
            if re.fullmatch(r"\d+x\d+@\d+\.bgra", f.name):
                shutil.copy2(f, dest / f.name)
        shutil.copy2(dll, root / "vulkan-1.dll")
        if README.exists():
            shutil.copy2(README, root / "vkproxy" / "README.txt")
    with open(root / "vkproxy" / f"{part}.manifest", "w", encoding="ascii", newline="\n") as m:
        for f in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix != ".manifest"):
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            m.write(f"{digest} {f.stat().st_size} {f.relative_to(root).as_posix()}\n")


def archive(part_dir: Path, path: Path, log=print) -> None:
    seven = shutil.which("7z")
    if path.exists():
        path.unlink()
    if seven:
        path = path.with_suffix(".7z")
        subprocess.run([seven, "a", "-t7z", "-mx=7", "-mmt=on", str(path), "."], cwd=part_dir, check=True,
                       stdout=subprocess.DEVNULL)
    else:
        path = path.with_suffix(".zip")
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in sorted(p for p in part_dir.rglob("*") if p.is_file()):
                z.write(f, f.relative_to(part_dir).as_posix())
    log(f"  {path.name}: {path.stat().st_size / 2**20:.0f} MB")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", type=Path,
                    help="default: %%LOCALAPPDATA%%/sims1-legacy-hd-mod/release/<version> (about 24 GB with the archives)")
    ap.add_argument("--dll", type=Path, default=REPO / "vkproxy" / "build-release" / "vulkan-1.dll")
    ap.add_argument("--parts", nargs="*", default=list(PARTS), choices=list(PARTS))
    ap.add_argument("--archive", action="store_true", help="a .7z of each part (zip when 7z is not found)")
    args = ap.parse_args(argv)
    if hd_art.game_running():
        print("Close the game first: it keeps the packs open.")
        return 1
    if not args.dll.exists():
        print(f"{args.dll}: build it first (powershell -File vkproxy/build.ps1 -Release)")
        return 1
    custom = newer_than_game(args.game)
    if custom:
        print("Custom content (newer than the game): its HD files would ship. Remove it first:")
        for p in custom[:20]:
            print(f"  {p}")
        return 1
    out = args.out or Path(os.environ.get("LOCALAPPDATA", REPO / "work")) / "sims1-legacy-hd-mod" / "release" / args.version
    replace = args.game / "vkproxy" / "replace"
    inst = hd_pack.Installed(replace)
    t0 = time.time()
    print(f"sorting {len(inst.names())} HD files into parts...")
    parts, left = classify(args.game, replace, inst)
    for p in PARTS:
        print(f"  {p}: {len(parts[p])} files")
    if left:
        sizes = {}
        for n in left:
            k = n.split("_")[0]
            sizes[k] = sizes.get(k, 0) + 1
        print(f"  left out (no part: this player's own portraits, or something unknown): {len(left)}, by size: "
              + ", ".join(f"{k} {v}" for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])[:12]))
    for p in args.parts:
        write_part(p, parts[p], inst, replace, args.game, out, args.dll)
        if args.archive:
            archive(out / p, out / f"Sims1LegacyHD-{args.version}-{p}.7z")
    print(f"done in {time.time() - t0:.0f} s: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
