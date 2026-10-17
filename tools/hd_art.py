"""Build HD object sprites for vkproxy from the user's own game files, one part at a time.

Steps, each skipping work already done (so parts and interrupted runs resume):
  extract    decode the chosen objects' SPR2 frames exactly as Legacy uploads them
             into <work>/orig/<name>.bgra (name = what vkproxy matches)
  prep       straight alpha + mirrored fill -> <work>/in/<name>.png   (hd_composite)
  upscale    ai_upscale/upscale.py --model M -> <work>/ai/<M>/        (needs the venv)
  composite  -> <work>/hd/<M>/<name>@3.bgra                            (hd_composite)
  install    copy the part's files into <game>/vkproxy/replace/  (--install)
With --clean the part's AI outputs are deleted once composited (they are the
largest intermediates; keep them only to re-run the composite step).

A part is a category of objects within a pack:
  --pack      base | ep1 ... ep7 | deluxe | shared   (default base)
  --category  seating surfaces appliances electronics plumbing decorative lighting misc
              (buy-mode category from the object's OBJD) | build | outdoor | other
              (objects without a buy-mode category, split by file name) | all
  --files     only object files whose name contains one of these strings
Floors (floors.iff) are not objects and are never included: they tile and need
their own treatment.

Usage:
    py tools/hd_art.py --game <dir> --list                    frames per category
    py tools/hd_art.py --game <dir> --category seating [--model ultrasharp-v2] [--install] [--clean]
Close the game before running: the upscaler uses the GPU.
"""
from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import hd_composite
import iff
import spr2

REPO = Path(__file__).resolve().parents[1]
FUNCTION_FLAGS_FIELD = 38  # OBJD: u32 version, then u16 fields (FreeSO field list)
BUY_CATEGORIES = {1: "seating", 2: "surfaces", 4: "appliances", 8: "electronics", 16: "plumbing",
                  32: "decorative", 64: "misc", 128: "lighting"}
BUILD_NAMES = ("door", "window", "stair", "fireplace", "column", "shutter", "pooldiv", "poolladder", "fence",
               "arch", "railing")
OUTDOOR_NAMES = ("tree", "shrub", "flower", "mailbox", "trashoutside", "urn", "hedge", "bush")
PACKS = {
    "base": ["GameData/Objects/Objects.far"],
    "deluxe": ["Deluxe/Deluxe.far"],
    "shared": ["ExpansionShared/ExpansionShared.far"],
    **{f"ep{n}": [f"ExpansionPack{n if n > 1 else ''}/ExpansionPack{n if n > 1 else ''}.far"] for n in range(1, 8)},
}
CATEGORIES = sorted(set(BUY_CATEGORIES.values())) + ["build", "outdoor", "other"]


def category_of(name: str, data: bytes, chunks: list) -> str:
    votes = Counter()
    for c in chunks:
        if c.type == "OBJD" and c.size >= 4 + 2 * (FUNCTION_FLAGS_FIELD + 1):
            (flags,) = struct.unpack_from("<H", data, c.offset + 4 + 2 * FUNCTION_FLAGS_FIELD)
            for bit, cat in BUY_CATEGORIES.items():
                if flags & bit:
                    votes[cat] += 1
    if votes:
        return votes.most_common(1)[0][0]
    stem = Path(name).stem.lower()
    if any(k in stem for k in OUTDOOR_NAMES):  # first: "FlowersOutdoor" contains "door"
        return "outdoor"
    if any(k in stem for k in BUILD_NAMES):
        return "build"
    return "other"


def object_files(game: Path, pack: str):
    """(file name, bytes, chunks, category) of every object IFF in the pack."""
    for rel in PACKS[pack]:
        path = game / rel
        if not path.exists():
            continue
        for name, data in spr2.iter_iffs(path):
            if Path(name).name.lower() == "floors.iff":
                continue
            try:
                chunks = iff.read_iff(data)
            except iff.IffError:
                continue
            if any(c.type == "SPR2" for c in chunks):
                yield name, data, chunks, category_of(name, data, chunks)


def frames(data: bytes, chunks: list):
    """(content name, texture) of every decodable frame in one IFF."""
    pals = spr2.read_palettes(data, chunks)
    for c in chunks:
        if c.type != "SPR2":
            continue
        s = data[c.offset:c.offset + c.size]
        try:
            offsets = spr2.frame_offsets(s)
        except spr2.Spr2Error:
            continue
        for off, default_pal in offsets:
            try:
                f = spr2.decode_frame(s, off)
            except (spr2.Spr2Error, struct.error):
                continue
            pal = pals.get(f.palette_id) or pals.get(default_pal)
            if pal and f.w and f.h:
                tex = spr2.game_texture(f, pal)
                yield spr2.content_name(f.w, f.h, tex), tex


def game_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/fi", "imagename eq Sims.exe"], capture_output=True, text=True).stdout
    except OSError:
        return False
    return "Sims.exe" in out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Build HD object sprites for vkproxy, one part at a time.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=REPO / "work" / "hd_art")
    ap.add_argument("--pack", choices=sorted(PACKS), default="base")
    ap.add_argument("--category", choices=CATEGORIES + ["all"])
    ap.add_argument("--files", nargs="*", default=[], help="only object files containing one of these strings")
    ap.add_argument("--model", default="ultrasharp-v2", help="name in ai_upscale/models.json or a model file")
    ap.add_argument("--python", type=Path, default=REPO / "work" / "ext" / "torch" / "Scripts" / "python.exe",
                    help="python of the ai_upscale venv")
    ap.add_argument("--install", action="store_true", help="copy the part's HD files into <game>/vkproxy/replace")
    ap.add_argument("--clean", action="store_true",
                    help="delete the part's AI outputs once composited (large; only needed to re-composite)")
    ap.add_argument("--list", action="store_true", help="frames per category in the pack, then exit")
    args = ap.parse_args(argv)

    if args.list:
        per_cat, files_per_cat = Counter(), defaultdict(list)
        for name, data, chunks, cat in object_files(args.game, args.pack):
            n = sum(1 for _ in frames(data, chunks))
            per_cat[cat] += n
            files_per_cat[cat].append(Path(name).stem)
        for cat, n in per_cat.most_common():
            print(f"{cat:12} {n:6} frames  {', '.join(sorted(files_per_cat[cat]))}")
        return 0
    if not args.category:
        ap.error("--category is required (or --list)")

    orig = args.work / "orig"
    pngs = args.work / "in"
    ai = args.work / "ai" / Path(args.model).stem
    hd = args.work / "hd" / Path(args.model).stem
    for d in (orig, pngs, ai, hd):
        d.mkdir(parents=True, exist_ok=True)

    # 1. extract
    names: list[str] = []
    seen = set()
    for name, data, chunks, cat in object_files(args.game, args.pack):
        if args.category != "all" and cat != args.category:
            continue
        if args.files and not any(k.lower() in name.lower() for k in args.files):
            continue
        for cname, tex in frames(data, chunks):
            if cname in seen:
                continue
            seen.add(cname)
            names.append(cname)
            if not (orig / f"{cname}.bgra").exists():
                (orig / f"{cname}.bgra").write_bytes(tex)
    print(f"part {args.pack}/{args.category}{' ' + ','.join(args.files) if args.files else ''}: "
          f"{len(names)} distinct textures")
    (args.work / f"part_{args.pack}_{args.category}.txt").write_text("\n".join(names) + "\n")

    # 2. prep
    jobs = [(orig / f"{n}.bgra", pngs / f"{n}.png") for n in names if not (pngs / f"{n}.png").exists()]
    print(f"prep: {hd_composite.run_all(jobs, hd_composite.prep_one)} new")

    # 3. upscale (only this part's images)
    todo = [n for n in names if not (ai / f"{n}.png").exists() and not (hd / f"{n}@3.bgra").exists()]
    if todo:
        if game_running():
            print("The game is running: close it before upscaling (GPU).")
            return 1
        part_in = args.work / "in_part"
        if part_in.exists():
            shutil.rmtree(part_in)
        part_in.mkdir()
        for n in todo:
            shutil.copyfile(pngs / f"{n}.png", part_in / f"{n}.png")
        cmd = [str(args.python), "-W", "ignore", str(REPO / "ai_upscale" / "upscale.py"), "--model", args.model,
               str(part_in), str(ai)]
        print("upscale:", " ".join(cmd), flush=True)
        if subprocess.run(cmd).returncode != 0:
            return 1
        shutil.rmtree(part_in)

    # 4. composite
    jobs = [(orig / f"{n}.bgra", ai / f"{n}.png", hd / f"{n}@3.bgra") for n in names
            if (ai / f"{n}.png").exists() and not (hd / f"{n}@3.bgra").exists()]
    print(f"composite: {hd_composite.run_all(jobs, hd_composite.composite_one)} new")
    if args.clean:
        n = 0
        for name in names:
            if (hd / f"{name}@3.bgra").exists() and (ai / f"{name}.png").exists():
                (ai / f"{name}.png").unlink()
                n += 1
        print(f"clean: removed {n} AI outputs")

    # 5. install
    if args.install:
        dest = args.game / "vkproxy" / "replace"
        dest.mkdir(parents=True, exist_ok=True)
        n = 0
        for name in names:
            src = hd / f"{name}@3.bgra"
            if src.exists():
                shutil.copyfile(src, dest / src.name)
                n += 1
        print(f"installed {n} HD textures into {dest} (restart the game to load them)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
