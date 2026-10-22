"""HD portraits of the Sims (control panel, relationships, speech balloons), drawn again at 3x.

The game does not draw a Sim's portraits while playing: they are pictures stored in the
character file (UserData*\\Characters\\User*.iff, BMP_ chunks), drawn when the Sim was made:
    2002 'faces'        105x41   three heads of 35x41 (control panel buttons, lists)
    2003 'rel. images'  200x40   one head on five backgrounds (relationship levels)
    2004 'newimage'      25x25   a head inside a one-pixel frame
    2005 'speech_med'    16x16   speech balloons
    2006 'speech_large'  32x32
    2007 'web_image'     45x45   (not in every file)
Legacy uploads them truncated to RGB565, like the UI bitmaps, so vkproxy finds each by name.
For every human Sim (skeleton adult or child; a pet's head is part of its body mesh, which
needs the skeleton: not done) this reads the head skins and texture from STR# 200 (e.g.
"c802fa_cornelia,HEAD-HEAD=c802falgt_cornelia": xskin-c802fa_cornelia-HEAD-HEAD.bmf/.skn and
c802falgt_cornelia.bmp), finds the pose each picture was drawn with (pose.py), and draws it
again three times larger with the HD head texture (tools/hd_skins.py, read from the installed
packs; the game's texture where there is none). Frames and backgrounds come from the
picture; a head suit's other skins on the head bone (glasses, a hat) are drawn with it, in
the textures their meshes name. Pictures whose fit stays worse than their limit (PICTURES)
are left as they are.

Files in --work (default work/portraits):
  hd/<picture>@3.bgra   the HD pictures, named as vkproxy matches the game's picture
  results.tsv           one row per picture: character file, Sim, chunk id, name, error, what was done
  failed.txt            pictures that could not be done (a pet, a fit too poor): skipped unless --force

Usage:
    py portraits/redraw.py --game <dir> [--install]          every character in the saves
    py portraits/redraw.py --game <dir> --files User00027 ... [--install]
New Sims bring new pictures. --install also writes <game>/vkproxy/replace/portraits.auto with
this command: vkproxy runs it (--install --loose: unpacked, while the game is open) when it
sees control panel faces without an HD picture, and picks up the new pictures the next time
they are shown (the next lot). Needs numpy.
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):  # one BLAS thread per worker
    os.environ.setdefault(_v, "1")

import argparse  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bmp  # noqa: E402
import far  # noqa: E402
import pose  # noqa: E402
import hd_pack  # noqa: E402
import hd_ui  # noqa: E402
import iff  # noqa: E402
import skn  # noqa: E402
import spr2  # noqa: E402
from head import Head, compose, render  # noqa: E402

SCALE = 3
SAMPLES = 4       # per pixel and axis when drawing the HD picture
SKIP_ARCHIVES = ("uigraphics.far", "sound.far", "sounddata.far")
HUMANS = ("adult", "child")
# chunk id -> (regions (x, y, w, h) holding a head, whether they all show the same pose, the
# worst fit error kept: small pictures differ more per pixel when the fit is right, checked
# by eye on the pictures just over it)
PICTURES = {
    2002: ([(35 * k, 0, 35, 41) for k in range(3)], False, 12.0),
    2003: ([(40 * k, 0, 40, 40) for k in range(5)], True, 12.0),  # each on its own background
    2004: ([(1, 1, 23, 23)], False, 12.0),
    2005: ([(0, 0, 16, 16)], False, 16.0),
    2006: ([(0, 0, 32, 32)], False, 14.0),
    2007: ([(0, 0, 45, 45)], False, 16.0),
}


SIZES = {2002: (105, 41), 2003: (200, 40), 2004: (25, 25), 2005: (16, 16), 2006: (32, 32), 2007: (45, 45)}


def background(part: np.ndarray) -> np.ndarray:
    """The colour most of a region's edge pixels have: its background (a corner can be hair)."""
    edge = np.concatenate([part[0], part[-1], part[1:-1, 0], part[1:-1, -1]])
    colours, counts = np.unique(edge.astype(int), axis=0, return_counts=True)
    return colours[np.argmax(counts)].astype(float)


def default_saves() -> Path:
    return Path(os.environ.get("USERPROFILE", "~")).expanduser() / "Saved Games" / "Electronic Arts" / "The Sims 25"


def game_files(game: Path) -> dict[str, tuple]:
    """File name (lower case) -> where it is, for the skins and textures: loose files in the
    Skins folders, then members of the archives (the first one found wins)."""
    found: dict[str, tuple] = {}
    paths = []
    for root, dirs, files in os.walk(game):
        dirs[:] = [d for d in dirs if d.lower() != "vkproxy"]
        paths += [Path(root) / f for f in files]
    for p in sorted(paths):
        if p.suffix.lower() in (".skn", ".bmf", ".bmp") and any(
                part.lower().startswith("skins") for part in p.relative_to(game).parts[:-1]):
            found.setdefault(p.name.lower(), (str(p),))
    for p in sorted(paths):
        if p.suffix.lower() == ".far" and p.name.lower() not in SKIP_ARCHIVES:
            try:
                entries = far.read_far(p.read_bytes())
            except far.FarError:
                continue
            for e in entries:
                n = e.name.replace("/", "\\").rsplit("\\", 1)[-1].lower()
                if n.endswith((".skn", ".bmf", ".bmp")):
                    found.setdefault(n, (str(p), e.offset, e.size))
    return found


def read_file(files: dict, name: str) -> bytes | None:
    where = files.get(name.lower())
    if where is None:
        return None
    if len(where) == 1:
        return Path(where[0]).read_bytes()
    with open(where[0], "rb") as f:
        f.seek(where[1])
        return f.read(where[2])


def character_files(saves: Path, game: Path) -> list[Path]:
    out = []
    for base in (saves, game):
        for d in sorted(base.glob("*")):
            if d.is_dir() and (d.name.lower().startswith("userdata") or d.name.lower().startswith("template")):
                out += sorted((d / "Characters").glob("*.iff"))
    return out


def rgb(rgba: bytes, w: int, h: int) -> np.ndarray:
    return np.frombuffer(rgba, np.uint8).reshape(h, w, 4)[..., :3].astype(float)


# ---- per character (in a worker) ------------------------------------------------------

_files: dict = {}
_installed: hd_pack.Installed | None = None


def _init(files: dict, replace: str) -> None:
    global _files, _installed
    _files = files
    _installed = hd_pack.Installed(Path(replace))


def head_of(entry: str) -> tuple[list[skn.Skin], list[np.ndarray], list[np.ndarray]] | str:
    """The skins of a head entry of STR# 200 with their textures at 1x and 3x, or why not."""
    parts = [p.strip() for p in entry.split(",") if p.strip()]
    if len(parts) < 2 or parts[0].upper() == "NONE":
        return f"no head ({entry!r})"
    suit, skins, tex1, tex3 = parts[0], [], [], []
    named = dict(p.partition("=")[::2] for p in parts[1:])  # skin -> texture (the Sim's skin tone)
    # the suit's other skins on the head (glasses, a hat) keep the texture their mesh names
    prefix = f"xskin-{suit}-".lower()
    others = sorted({n[len(prefix):-4] for n in _files if n.startswith(prefix) and n.endswith((".bmf", ".skn"))}
                    - {s.lower() for s in named})
    for skin_name in list(named) + others:
        base = f"xskin-{suit}-{skin_name}"
        data = read_file(_files, base + ".bmf")
        kind = ".bmf"
        if data is None:
            data, kind = read_file(_files, base + ".skn"), ".skn"
        if data is None:
            return f"missing {base}"
        try:
            skin = skn.read_skin(kind, data)
        except skn.SkinError as e:
            return f"{base}: {e}"
        if skin_name not in named and [b.upper() for b in skin.bones] != ["HEAD"]:
            continue
        tex_name = named.get(skin_name, skin.texture)
        tex = read_file(_files, tex_name + ".bmp")
        if tex is None:
            if skin_name in named:
                return f"missing {tex_name}.bmp"
            continue
        try:
            w, h, rgba = bmp.decode_bmp(tex)
        except bmp.FormatError as e:
            return f"{tex_name}.bmp: {e}"
        skins.append(skin)
        tex1.append(rgb(rgba, w, h))
        hd = _installed.read(f"{spr2.content_name(w, h, hd_ui.upload(rgba))}@{SCALE}.bgra")
        tex3.append(np.frombuffer(hd, np.uint8).reshape(h * SCALE, w * SCALE, 4)[..., 2::-1].astype(float)
                    if hd and len(hd) == w * h * SCALE * SCALE * 4 else tex1[-1])
    return skins, tex1, tex3


def draw_picture(head: Head, tex1, tex3, picture: np.ndarray, regions, shared: bool):
    """The HD picture (RGB uint8, 3x) and the worst fit error of its regions, or (None, error)."""
    out = picture.repeat(SCALE, 0).repeat(SCALE, 1)  # frames as they are
    worst, found = 0.0, None
    for x, y, w, h in regions:
        part = picture[y:y + h, x:x + w]
        bg = background(part)
        if found is None or not shared:
            fitted = pose.fit_view(head, tex1, part, bg)
            if fitted is None:
                return None, float("inf")
            found, err = fitted
            worst = max(worst, err)
        hd_pose = np.array([*found[:3], found[3] * SCALE, found[4] * SCALE, found[5] * SCALE])
        colour, cov = render(head, tex3, hd_pose, (w * SCALE, h * SCALE), SAMPLES)
        out[y * SCALE:(y + h) * SCALE, x * SCALE:(x + w) * SCALE] = compose(colour, cov, bg)
    return np.clip(np.round(out), 0, 255).astype(np.uint8), worst


def build_character(job) -> list[tuple]:
    path, hd_dir, force, failed = job
    path, hd_dir = Path(path), Path(hd_dir)
    rows = []
    try:
        data = path.read_bytes()
        chunks = iff.read_iff(data)
    except (OSError, iff.IffError) as e:
        return [(path.name, "", "", "", "", f"unreadable: {e}")]
    sim = next((c.label for c in chunks if c.type == "OBJD"), "")
    body = []
    for c in chunks:
        if c.type == "STR#" and c.id == 200:
            try:
                body = iff.str_set(data[c.offset:c.offset + c.size])
            except iff.IffError:
                pass
    pictures = []
    for c in chunks:
        if c.type == "BMP_" and c.id in PICTURES:
            try:
                w, h, rgba = bmp.decode_bmp(data[c.offset:c.offset + c.size])
            except bmp.FormatError:
                continue
            pictures.append((c.id, w, h, rgba, spr2.content_name(w, h, hd_ui.upload(rgba))))
    todo = [p for p in pictures if force or not ((hd_dir / f"{p[4]}@{SCALE}.bgra").exists() or p[4] in failed)]
    if not todo:
        return [(path.name, sim, cid, name, "", "built before") for cid, _w, _h, _rgba, name in pictures]
    if len(body) < 3 or body[0].lower() not in HUMANS:
        why = f"skeleton {body[0]!r}: not done" if body else "no STR# 200"
        return [(path.name, sim, cid, name, "", why) for cid, _w, _h, _rgba, name in todo]
    found = head_of(body[2])
    if isinstance(found, str):
        return [(path.name, sim, cid, name, "", found) for cid, _w, _h, _rgba, name in todo]
    skins, tex1, tex3 = found
    head = Head(skins)
    for cid, w, h, rgba, name in todo:
        regions, shared, max_error = PICTURES[cid]
        if (w, h) != SIZES[cid]:
            rows.append((path.name, sim, cid, name, "", f"unexpected size {w}x{h}"))
            continue
        hd, err = draw_picture(head, tex1, tex3, rgb(rgba, w, h), regions, shared)
        if hd is None or err > max_error:  # mean difference per channel (of 255)
            rows.append((path.name, sim, cid, name, f"{err:.2f}", "fit too poor: kept the game's"))
            continue
        bgra = np.concatenate([hd[..., ::-1], np.full(hd.shape[:2] + (1,), 255, np.uint8)], 2)
        (hd_dir / f"{name}@{SCALE}.bgra").write_bytes(bgra.tobytes())
        rows.append((path.name, sim, cid, name, f"{err:.2f}", "drawn"))
    return rows


# ---- CLI --------------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Draw the Sims' portraits again at 3x for vkproxy.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--saves", type=Path, default=default_saves(), help="the folder holding UserData, UserData2, ...")
    ap.add_argument("--work", type=Path, default=REPO / "work" / "portraits")
    ap.add_argument("--files", nargs="*", default=[], help="only character files whose name contains one of these")
    ap.add_argument("--force", action="store_true", help="draw pictures again that were drawn before")
    ap.add_argument("--install", action="store_true", help="copy the HD pictures into <game>/vkproxy/replace")
    ap.add_argument("--loose", action="store_true",
                    help="with --install: leave them loose, not packed (vkproxy runs it so while the game is open)")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    args = ap.parse_args(argv)

    t0 = time.time()
    hd_dir = args.work / "hd"
    hd_dir.mkdir(parents=True, exist_ok=True)
    chars = [p for p in character_files(args.saves, args.game)
             if not args.files or any(k.lower() in p.name.lower() for k in args.files)]
    files = game_files(args.game)
    replace = args.game / "vkproxy" / "replace"
    print(f"{len(chars)} character files, {len(files)} skin files and textures ({time.time() - t0:.0f} s)", flush=True)
    failed_file = args.work / "failed.txt"  # pictures that cannot be done: not tried again
    failed = set(failed_file.read_text(encoding="ascii").split()) if failed_file.exists() else set()
    rows = []
    with ProcessPoolExecutor(max_workers=max(1, args.workers), initializer=_init,
                             initargs=(files, str(replace))) as pool:
        jobs = [(str(p), str(hd_dir), args.force, failed) for p in chars]
        for i, r in enumerate(pool.map(build_character, jobs)):
            rows += r
            if (i + 1) % 20 == 0:
                print(f"  {i + 1}/{len(chars)} ({time.time() - t0:.0f} s)", flush=True)
    seen = {}
    for row in rows:  # a picture shared by copies of a character: the first row that drew it
        seen.setdefault(row[3], row)
    done = {r[3] for r in rows if r[5] in ("drawn", "built before")}
    failed |= {r[3] for r in rows if r[3]} - done
    failed_file.write_text("".join(n + "\n" for n in sorted(failed)), encoding="ascii")
    (args.work / "results.tsv").write_text(
        "file\tsim\tid\tpicture\terror\twhat\n" + "".join("\t".join(map(str, r)) + "\n" for r in rows), encoding="utf-8")
    counts: dict[str, int] = {}
    for r in seen.values():
        what = r[5] if not r[5].startswith(("missing", "no head", "skeleton")) else r[5].split(" ")[0]
        counts[what] = counts.get(what, 0) + 1
    print(", ".join(f"{n} {w}" for w, n in sorted(counts.items(), key=lambda t: -t[1])) + f" ({time.time() - t0:.0f} s)")
    if args.install:
        dest = hd_pack.Installed(replace)
        n = 0
        for f in sorted(hd_dir.glob(f"*@{SCALE}.bgra")):
            installed = dest.mtime(f.name)
            if installed is None or installed < f.stat().st_mtime:
                dest.copy(f, f.name)
                n += 1
        if not args.loose:
            hd_pack.finish_install(dest)
        write_auto(replace, args)
        print(f"installed {n} HD portraits into {replace} (restart the game to see them)")
    return 0


def write_auto(replace: Path, args) -> None:
    """<replace>/portraits.auto, for vkproxy: the picture sizes it looks for new HD files of, and
    the command it starts (in the background, at low priority, unpacked) when it sees a Sim's
    control panel faces that have none: a Sim made or changed since the last run."""
    workers = max(2, (os.cpu_count() or 4) // 4)
    cmd = (f'"{sys.executable}" "{Path(__file__).resolve()}" --game "{args.game.resolve()}" '
           f'--saves "{args.saves.resolve()}" --work "{args.work.resolve()}" --install --loose --workers {workers}')
    sizes = " ".join(f"{w}x{h}" for w, h in SIZES.values())
    (replace / "portraits.auto").write_text(f"sizes {sizes}\ntrigger {SIZES[2002][0]}x{SIZES[2002][1]}\nrun {cmd}\n",
                                            encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
