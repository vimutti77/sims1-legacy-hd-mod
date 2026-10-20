"""Build HD object sprites for vkproxy from the user's own game files, one part at a time.

Steps, each skipping work already done (so parts and interrupted runs resume):
  extract    decode the chosen objects' SPR2 frames exactly as Legacy uploads them
             into <work>/orig/<name>.bgra (name = what vkproxy matches)
  prep       straight alpha + mirrored fill -> <work>/in/<name>.png   (hd_composite)
  upscale    ai_upscale/upscale.py --model M --composite: AI, then the composite
             step on the GPU -> <work>/hd/<M>/<name>@3.bgra            (needs the venv)
  install    copy the part's new files into <game>/vkproxy/replace/  (--install)
To try other composite rules on the AI output itself, run ai_upscale/upscale.py
without --composite and tools/hd_composite.py composite by hand.

A part is a category of objects within a pack:
  --pack      base | ep1 ... ep7 | deluxe | shared | downloads (Downloads/: Maxis' free
              objects and custom content; also loose GameData/Objects/*.iff) |
              global (GameData/Global, Sprites.iff) | all   (default base)
  --category  seating surfaces appliances electronics plumbing decorative lighting misc
              (buy-mode category from the object's OBJD) | build | outdoor | other
              (objects without a buy-mode category, split by file name) | floors | all
  --files     only object files whose name contains one of these strings
Floors (GameData/floors.iff, GameData/Floors/*.flr and the .flr files in the packs'
archives) are SPR2 diamonds the game lays edge to edge: their input is filled with the
neighbouring tiles instead of a mirror image (hd_composite.tile_fill), so no seam shows
between tiles.

Touched up by hand: <work>/edits/<name>.png (the HD texture as straight RGBA, 3x the
sprite or any whole multiple of that) is installed instead of the built texture: its
colour, and its alpha (stair-stepped outlines rounded off by erasing or by painting).
Where it paints past the original's coverage, a 3x depth texture is installed with it
(<w>x<h>_<hash>_f9@3.raw, depth_twin): the game's 1x depth texture has no depth there.
--export-edit <name>... writes the built texture there to start from; --install-edits
installs them all.

Usage:
    py tools/hd_art.py --game <dir> --list                    frames per category
    py tools/hd_art.py --game <dir> --category seating [--model ultrasharp-v2] [--install]
    py tools/hd_art.py --game <dir> --pack all --category all --install   everything
Close the game before running: the upscaler uses the GPU.
"""
from __future__ import annotations

import argparse
import math
import struct
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from itertools import chain
from pathlib import Path

import far
import hd_composite
import hd_pack
import iff
import spr2

REPO = Path(__file__).resolve().parents[1]
FUNCTION_FLAGS_FIELD = 38  # OBJD: u32 version, then u16 fields (FreeSO field list)
BUY_CATEGORIES = {1: "seating", 2: "surfaces", 4: "appliances", 8: "electronics", 16: "plumbing",
                  32: "decorative", 64: "misc", 128: "lighting"}
BUILD_NAMES = ("door", "window", "stair", "fireplace", "column", "shutter", "pooldiv", "poolladder", "fence",
               "arch", "railing")
OUTDOOR_NAMES = ("tree", "shrub", "flower", "mailbox", "trashoutside", "urn", "hedge", "bush")
PACKS = {  # glob patterns under the install folder
    "base": ["GameData/Objects/Objects.far"],
    "deluxe": ["Deluxe/Deluxe.far"],
    "shared": ["ExpansionShared/ExpansionShared.far"],
    **{f"ep{n}": [f"ExpansionPack{n if n > 1 else ''}/ExpansionPack{n if n > 1 else ''}.far"] for n in range(1, 8)},
    # Maxis' free download objects shipped with Legacy, and the player's own custom content
    "downloads": ["Downloads/**/*.far", "Downloads/**/*.iff", "GameData/Objects/*.iff"],
    "global": ["GameData/Global/Global.far", "GameData/Sprites.iff"],
}
CATEGORIES = sorted(set(BUY_CATEGORIES.values())) + ["build", "outdoor", "other", "floors"]


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
    """(file name, bytes, chunks, category) of every object IFF in the pack ("all": every pack)."""
    patterns = [p for k in (PACKS if pack == "all" else [pack]) for p in PACKS[k]]
    for path in (path for pattern in patterns for path in sorted(game.glob(pattern))):
        for name, data in spr2.iter_iffs(path):
            if Path(name).name.lower() == "floors.iff":
                continue
            try:
                chunks = iff.read_iff(data)
            except iff.IffError:
                continue
            if any(c.type == "SPR2" for c in chunks):
                yield name, data, chunks, category_of(name, data, chunks)


def floor_files(game: Path, pack: str):
    """(file name, bytes, chunks, "floors") of every floor file in the pack ("all": every pack):
    the base game's floors.iff and GameData/Floors/*.flr, and the .flr files in archives."""
    packs = list(PACKS) if pack == "all" else [pack]
    paths = [path for k in packs for pattern in PACKS[k] for path in sorted(game.glob(pattern))]
    if "base" in packs:
        paths += [game / "GameData" / "floors.iff"] + sorted((game / "GameData" / "Floors").glob("*.flr"))
    for path in paths:
        if not path.is_file():
            continue
        data = path.read_bytes()
        if data[:8] == far.MAGIC:
            members = [(f"{path.name}/{e.name}", data[e.offset:e.offset + e.size]) for e in far.read_far(data)
                       if e.name.lower().endswith(".flr")]
        elif path.suffix.lower() == ".flr" or path.name.lower() == "floors.iff":
            members = [(path.name, data)]
        else:
            continue
        for name, body in members:
            try:
                chunks = iff.read_iff(body)
            except iff.IffError:
                continue
            if any(c.type == "SPR2" for c in chunks):
                yield name, body, chunks, "floors"


def frames(data: bytes, chunks: list):
    """(content name, texture) of every decodable frame in one IFF."""
    for name, tex, _f in decoded_frames(data, chunks):
        yield name, tex


def decoded_frames(data: bytes, chunks: list):
    """(content name, texture, spr2.Frame) of every decodable frame in one IFF."""
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
            pal = spr2.frame_palette(pals, f, default_pal)
            if pal and f.w and f.h:
                tex = spr2.game_texture(f, pal)
                yield spr2.content_name(f.w, f.h, tex), tex, f


def depth_texture(f: spr2.Frame) -> bytes:
    """A frame's depth texture as Legacy uploads it (R8; checked against 82 of 82 uploads of
    the running game): the frame's z where it has a pixel, 0 where it has none. 0 is the
    nearest depth, so an HD pixel drawn outside the original sprite would show through
    walls and objects in front: HD coverage can only grow with a depth twin (depth_twin)."""
    return bytes(0 if p is None else z for p, z in zip(f.pixels, f.z))


GROUND_Z_PER_PX = 0.5  # z units nearer per HD pixel lower on screen: 48.2 per tile step of 32 px, at 3x
TWIN_NEARER = 1  # z units the whole twin is drawn nearer (see depth_twin)


def depth_twin(w: int, h: int, covered: bytes, depth: bytes) -> bytes:
    """The 3x depth texture to go with an HD texture that covers more than the original:
    the original's depth (3x nearest) where it covers; elsewhere the depth of the nearest
    pixel it covers that has one (z < 255), made nearer the lower the new pixel is (as the
    ground gets nearer down the screen). Without that, an edge grown below a pot's foot
    sat behind the table top it stands on, which hid it. All of it is then TWIN_NEARER
    nearer: the pot's own corner pixel sat 0.8 units behind that table top (hidden in the
    original game too, unnoticed at 1x; measured from the world log). Pixels the HD texture
    leaves empty are discarded by their alpha; 255 (no depth) stays 255."""
    s = hd_composite.SCALE
    W, H = w * s, h * s
    near = bytearray([255]) * (W * H)  # nearest depth; 255 (discarded) where none reaches
    from_y = [0] * (W * H)             # screen row of the pixel that depth came from
    done = bytearray(W * H)
    queue = deque()
    for y in range(H):
        for x in range(W):
            i = (y // s) * w + x // s
            if covered[i] and depth[i] < 255:
                near[y * W + x] = depth[i]
                from_y[y * W + x] = y
                done[y * W + x] = 1
                queue.append(y * W + x)
    # through every pixel, past the covered ones without depth too; four neighbours, so a
    # pixel beside the sprite takes its depth from the same row and one below it from above
    while queue:
        j = queue.popleft()
        x, y = j % W, j // W
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < W and 0 <= ny < H and not done[ny * W + nx]:
                done[ny * W + nx] = 1
                near[ny * W + nx] = near[j]
                from_y[ny * W + nx] = from_y[j]
                queue.append(ny * W + nx)
    out = bytearray(near)
    for j in range(W * H):
        if near[j] < 255:
            lower = max(0, j // W - from_y[j])
            out[j] = max(0, near[j] - math.ceil(GROUND_Z_PER_PX * lower) - TWIN_NEARER)
    for y in range(H):  # where the original covers, its own depth
        for x in range(W):
            i = (y // s) * w + x // s
            if covered[i]:
                out[y * W + x] = depth[i] if depth[i] == 255 else max(0, depth[i] - TWIN_NEARER)
    return bytes(out)


def apply_edit(built: bytes, w: int, h: int, edit: Path) -> tuple[bytes, bool]:
    """(texture, grown): the built HD texture (premultiplied BGRA, w x h) with the colour of a
    hand-edited PNG (straight RGBA, w x h or a whole multiple, area-averaged down). Where
    the built texture covers anything, the edit's alpha counts only where it is lower (an
    outline rounded off by erasing; soft shadows keep theirs); where it covers nothing, the
    edit's alpha is taken as drawn (an outline rounded off by painting), and grown is True:
    such a texture needs a depth twin (depth_twin), or those pixels draw through walls."""
    ew, eh, rgba = hd_composite.read_png(edit)
    k = ew // w
    if k < 1 or (ew, eh) != (w * k, h * k):
        raise ValueError(f"{edit.name}: {ew}x{eh} is not {w}x{h} or a whole multiple of it")
    if k > 1:
        rgba = hd_composite.shrink(ew, eh, rgba, w, h)
    out = bytearray(built)
    grown = False
    for i in range(0, len(out), 4):
        a = min(built[i + 3], rgba[i + 3]) if built[i + 3] else rgba[i + 3]
        grown = grown or (a > 0 and not built[i + 3])
        r, g, b = rgba[i:i + 3]
        out[i:i + 4] = bytes(((b * a + 127) // 255, (g * a + 127) // 255, (r * a + 127) // 255, a))
    return bytes(out), grown


def find_depths(game: Path, names: set, cache: Path) -> dict:
    """name -> (w, h, coverage, depth texture) of the frames with these texture names, from the
    game's files (all packs: a slow scan), kept in <cache>/<name>.depth for the next time."""
    out, left = {}, set()
    for n in names:
        p = cache / f"{n}.depth"
        w, h = hd_composite.size_of(n)
        if p.exists() and p.stat().st_size == 2 * w * h:
            d = p.read_bytes()
            out[n] = (w, h, d[:w * h], d[w * h:])
        else:
            left.add(n)
    if left:
        cache.mkdir(parents=True, exist_ok=True)
        for _name, data, chunks, _cat in chain(object_files(game, "all"), floor_files(game, "all")):
            for cname, _tex, f in decoded_frames(data, chunks):
                if cname in left:
                    covered = bytes(p is not None for p in f.pixels)
                    out[cname] = (f.w, f.h, covered, depth_texture(f))
                    (cache / f"{cname}.depth").write_bytes(covered + depth_texture(f))
                    left.discard(cname)
            if not left:
                break
    return out


def install_edit(name: str, built: Path, edit: Path, dest: hd_pack.Installed, depths) -> str:
    """Install one touched-up texture and, when its coverage grew, its depth twin; returns
    what was done. depths(name) gives find_depths' entry for the name."""
    w, h = hd_composite.size_of(name)
    s = hd_composite.SCALE
    tex, grown = apply_edit(built.read_bytes(), w * s, h * s, edit)
    if grown:
        fw, fh, covered, depth = depths(name)
        dname = spr2.content_name(fw, fh, depth) + "_f9"
        dest.write(f"{dname}@{s}.raw", depth_twin(fw, fh, covered, depth))
    dest.write(f"{name}@{s}.bgra", tex)
    return "grown, with a depth twin" if grown else "colour"


def export_edit(built: bytes, w: int, h: int, png: Path) -> None:
    """The built texture as a PNG to touch up: straight colour, opaque wherever the sprite
    covers anything (the alpha in the PNG is not used)."""
    rgba = hd_composite.to_straight(built)
    for i in range(3, len(rgba), 4):
        rgba[i] = 255 if rgba[i] else 0
    hd_composite.write_png(png, w, h, bytes(rgba))


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
    ap.add_argument("--pack", choices=sorted(PACKS) + ["all"], default="base")
    ap.add_argument("--category", choices=CATEGORIES + ["all"])
    ap.add_argument("--files", nargs="*", default=[], help="only object files containing one of these strings")
    ap.add_argument("--model", default="ultrasharp-v2", help="name in ai_upscale/models.json or a model file")
    ap.add_argument("--python", type=Path, default=REPO / "work" / "ext" / "torch" / "Scripts" / "python.exe",
                    help="python of the ai_upscale venv")
    ap.add_argument("--install", action="store_true", help="copy the part's HD files into <game>/vkproxy/replace")
    ap.add_argument("--workers", type=int, default=4,
                    help="upscaler processes sharing the GPU (fewer if memory is short)")
    ap.add_argument("--list", action="store_true", help="frames per category in the pack, then exit")
    ap.add_argument("--export-edit", nargs="+", metavar="NAME",
                    help="write these built HD textures to <work>/edits/ to touch up, then exit")
    ap.add_argument("--install-edits", action="store_true",
                    help="install every texture in <work>/edits/ (whatever its part), then exit")
    args = ap.parse_args(argv)

    hd = args.work / "hd" / Path(args.model).stem
    edits = args.work / "edits"

    def depths(name: str):
        return find_depths(args.game, {name}, args.work / "depth")[name]

    if args.install_edits:
        dest = hd_pack.Installed(args.game / "vkproxy" / "replace")
        n = 0
        for edit in sorted(edits.glob("*.png")):
            src = hd / f"{edit.stem}@3.bgra"
            if not src.exists():
                print(f"{edit.name}: no built texture {src.name} to take the coverage from")
                continue
            try:
                done = install_edit(edit.stem, src, edit, dest, depths)
            except ValueError as e:
                print(e)
                continue
            print(f"{edit.stem}: {done}")
            n += 1
        hd_pack.finish_install(dest)
        print(f"installed {n} touched-up textures into {dest.folder} (restart the game to load them)")
        return 0
    if args.export_edit:
        edits.mkdir(parents=True, exist_ok=True)
        for name in args.export_edit:
            src = hd / f"{name}@3.bgra"
            if not src.exists():
                print(f"{name}: not built ({src})")
                continue
            w, h = hd_composite.size_of(name)
            export_edit(src.read_bytes(), w * hd_composite.SCALE, h * hd_composite.SCALE, edits / f"{name}.png")
            print("wrote", edits / f"{name}.png")
        return 0

    def files():
        yield from object_files(args.game, args.pack)
        yield from floor_files(args.game, args.pack)

    if args.list:
        per_cat, files_per_cat = Counter(), defaultdict(list)
        for name, data, chunks, cat in files():
            n = sum(1 for _ in frames(data, chunks))
            per_cat[cat] += n
            files_per_cat[cat].append(Path(name).stem)
        for cat, n in per_cat.most_common():
            print(f"{cat:12} {n:6} frames  {', '.join(sorted(files_per_cat[cat]))}")
        return 0
    if not args.category:
        ap.error("--category is required (or --list)")

    t0 = [time.time()]

    def lap() -> str:
        t, t0[0] = t0[0], time.time()
        return f"{t0[0] - t:.0f} s"

    orig = args.work / "orig"
    pngs = args.work / "in"
    for d in (orig, pngs, hd):
        d.mkdir(parents=True, exist_ok=True)

    # 1. extract
    names: list[str] = []
    seen, floors = set(), set()
    for name, data, chunks, cat in files():
        if args.category != "all" and cat != args.category:
            continue
        if args.files and not any(k.lower() in name.lower() for k in args.files):
            continue
        for cname, tex in frames(data, chunks):
            if cname in seen:
                continue
            seen.add(cname)
            names.append(cname)
            if cat == "floors":
                floors.add(cname)
            if not (orig / f"{cname}.bgra").exists():
                (orig / f"{cname}.bgra").write_bytes(tex)
    print(f"part {args.pack}/{args.category}{' ' + ','.join(args.files) if args.files else ''}: "
          f"{len(names)} distinct textures ({lap()})")
    (args.work / f"part_{args.pack}_{args.category}.txt").write_text("\n".join(names) + "\n")

    # 2. prep
    new = [n for n in names if not (pngs / f"{n}.png").exists()]
    n_prep = 0
    for fn, part in ((hd_composite.prep_one, [n for n in new if n not in floors]),
                     (hd_composite.prep_floor_one, [n for n in new if n in floors])):
        n_prep += hd_composite.run_all([(orig / f"{n}.bgra", pngs / f"{n}.png") for n in part], fn)
    print(f"prep: {n_prep} new ({lap()})")

    # 3. upscale + composite (only this part's images)
    todo = [n for n in names if not (hd / f"{n}@3.bgra").exists()]
    if todo:
        if game_running():
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
        n = 0
        for name in names:
            src, edit = hd / f"{name}@3.bgra", edits / f"{name}.png"
            newest = max((p.stat().st_mtime for p in (src, edit) if p.exists()), default=None)
            installed = dest.mtime(f"{name}@3.bgra")
            if newest is None or (installed is not None and installed >= newest):
                continue
            if edit.exists() and src.exists():  # touched up by hand
                install_edit(name, src, edit, dest, depths)
            elif src.exists():
                dest.copy(src, f"{name}@3.bgra")
            else:
                continue
            n += 1
        hd_pack.finish_install(dest)  # loose files into a pack: first opens are slow in game
        print(f"installed {n} HD textures into {dest.folder} ({lap()}; restart the game to load them)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
