"""HD catalogue pictures (Buy/Build mode list, 74x37 -> 222x111) from the objects' own sprites.

A catalogue picture is the object drawn small: its sprites (one state, one view) on a
coloured background. For every catalogue object this
  1. assembles its sprites as the game draws them (assemble.py) in every state and view
     (and dynamic sprites off / on), and keeps the one that best matches the game's
     picture once placed on it (fit.py);
  2. draws that assembly again from the HD sprites, three times larger, on both halves of
     the picture (the right one brightened, as the game's "selected" picture is).
The HD sprites are the installed ones (tools/hd_art.py), so build those first.

Files in --work (default work/catalog):
  hd/<picture>@3.bgra   the HD pictures, named as vkproxy matches the game's picture
  results.tsv           one row per catalogue object: the state, view, fit and errors
  overrides.tsv         choices made by hand, per object label (tab separated):
                        label, state, dynamic, view, mirror (true/false),
                        "center" (put the object in the middle, e.g. where the game's
                        picture left room for a shadow); an empty column keeps the
                        search's choice; "label <tab> none" = no state matches (keep the
                        game's picture). dynamic: on / off = all of the object's dynamic
                        sprites, then each toggled while it helps; on:200,203 = exactly
                        those SPR2 ids, on: = exactly none. '#' starts a comment.
  approved.txt          pictures accepted by eye although they fail the checks
  rejected.txt          pictures found wrong by eye: never installed
  installed.txt         what --install put into the game (so it can take it out again)
--install copies a picture when it passes the checks (colour error of the 1x assembly
<= MAX_ERROR on both halves and overlap of the shapes >= MIN_OVERLAP), was approved, or
had its state chosen by hand; never when rejected. Pictures installed before that no
longer qualify are removed from the game.

Usage:
    py catalog/build.py --game <dir> [--install]               every catalogue object
    py catalog/build.py --game <dir> --labels "Child's Stepstool" ... [--install]
    py catalog/build.py --game <dir> --install-only             apply the lists again
Needs numpy (the ai_upscale venv has it).
"""
from __future__ import annotations

import os

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):  # one BLAS thread per worker
    os.environ.setdefault(_v, "1")

import argparse  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import assemble  # noqa: E402
import fit  # noqa: E402
import hd_art  # noqa: E402
import iff  # noqa: E402
import objects  # noqa: E402
import spr2  # noqa: E402

SELECTED_ADD = 20 / 255  # the selected (right) picture shows the object brightened by about this
MAX_GRAPHICS = 64  # states tried per object (the mailbox's raised flag is #4, a TV's picture #21, a full pizza #34)
CANDIDATES = 5  # best-ranked (state, view, mirror) candidates that get the full fit
MAX_ERROR = 12.0  # install: RMS colour error (0-255) of the 1x assembly, worse half
MIN_OVERLAP = 0.85  # install: intersection over union of the object's pixels, normal half
SHAPE_DIFF = 12 / 255
HEADER = ("iff", "label", "picture", "tiles", "graphic", "view", "rmse_normal", "rmse_selected", "scale",
          "source", "x", "y", "guid", "objd_id", "bmp_id", "iou", "dynamic", "mirror")


def load_overrides(path: Path) -> dict[str, dict]:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t") + [""] * 6
        if f[1].strip() == "none":
            out[f[0]] = {"none": True}
            continue
        flag = {"true": True, "false": False}
        dyn = f[2].strip()
        out[f[0]] = {"graphic": int(f[1]) if f[1].strip() else None,
                     "dynamic": frozenset(int(x) for x in dyn[3:].split(",") if x.strip())
                     if dyn.startswith("on:") else {"on": True, "off": False}.get(dyn),
                     "view": int(f[3]) if f[3].strip() else None,
                     "mirror": flag.get(f[4].strip().lower()),
                     "center": f[5].strip() == "center"}
    return out


def read_list(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {w for line in path.read_text(encoding="utf-8-sig").splitlines() if not line.startswith("#")
            for w in line.split()}


def build_object(obj: objects.ObjectFile, sprites: assemble.Sprites, o: dict, tiles: list[dict], ov: dict):
    """(results row, HD picture as BGRA bytes or None) for one catalogue object."""
    w, h, rgba = obj.picture(o)
    row = dict(iff=obj.name, label=o["label"], picture=objects.picture_name(w, h, rgba), tiles=len(tiles),
               graphic=-1, view=-1, rmse_normal=-1.0, rmse_selected=-1.0, scale=0.0, source="", x=0.0, y=0.0,
               guid=f"{o['guid']:08x}", objd_id=o["id"], bmp_id=o["catpic"], iou=0.0, dynamic=False, mirror=False)
    if ov.get("none"):
        row["source"] = "none (by hand)"
        return row, None
    pic = np.frombuffer(rgba, np.uint8).reshape(h, w, 4)[..., :3].astype(np.float32) / 255
    halves = [pic[:, :h], pic[:, h:2 * h]]
    bgs = [fit.background(t) for t in halves]

    def choices(key, default):
        return [ov[key]] if ov.get(key) is not None else default

    drawn = [t for t in tiles if t["base"] in obj.dgrp] or tiles
    graphics = max(1, min(max(t["ngraphics"] or 1 for t in drawn), MAX_GRAPHICS))
    # dynamic sprites are off unless the object's own code turns them on (fence posts, cake
    # candles, TV picture, food in the fridge...): try all off, all on, then each one toggled
    has_dynamic = any(t.get("dyn_n") for t in tiles)
    scored = []
    for dyn in choices("dynamic", [False, True] if has_dynamic else [False]):
        for g in choices("graphic", range(graphics)):
            for view in choices("view", range(4)):
                c = assemble.assemble(sprites, tiles, g, view, dynamic=dyn)
                if c is None:
                    continue
                # rotations only: the game draws two of the four views by mirroring sprites, so a
                # mirrored view is either one of the rotations or a picture the game never shows
                for mirror in choices("mirror", [False]):
                    cm = c[:, ::-1] if mirror else c
                    q = fit.quick_error(cm, halves[0], bgs[0])
                    if q is not None:
                        scored.append((q, g, view, dyn, mirror, cm))
    if not scored:
        return row, None
    scored.sort(key=lambda r: r[0])
    best = None
    for _q, g, view, dyn, mirror, c in scored[:CANDIDATES]:
        f = fit.fit(c, halves[0], bgs[0])
        if f and (best is None or f[0] < best[0][0]):
            best = (f, g, view, dyn, mirror, c)
    if best is None:
        return row, None
    (mse, s, x, y), g, view, dyn, mirror, c = best
    ids = assemble.dynamic_ids(obj, tiles, g, view)
    if ids and not isinstance(ov.get("dynamic"), frozenset):  # a set given by hand is used as it is
        on = set(ids) if dyn else set()
        improved = True
        while improved:
            improved = False
            for i in sorted(ids):
                trial = on ^ {i}
                ct = assemble.assemble(sprites, tiles, g, view, dynamic=frozenset(trial), mirror=mirror)
                if ct is None or ct[..., 3].max() <= 0.5:
                    continue
                f = fit.fit(ct, halves[0], bgs[0])
                if f and f[0] < mse - 1e-9:
                    (mse, s, x, y), on, c, improved = f, trial, ct, True
        dyn = frozenset(on)
    if ov.get("center"):
        x, y = fit.centred(c, s, h)
    # the HD sprites when the picture is drawn larger than the 1x sprites (3s > 1), else the 1x ones
    use_hd = 3 * s > 1
    src = assemble.assemble(sprites, tiles, g, view, hd=True, dynamic=dyn, mirror=mirror) if use_hd else c
    if src is None:
        src, use_hd = c, False
    k = 1 if use_hd else 3
    outs, errs = [], []
    for t, bg, add in zip(halves, bgs, (0.0, SELECTED_ADD)):
        outs.append(fit.render(src, s * k, 3 * x, 3 * y, 3 * h, bg, add=add))
        errs.append(float(np.sqrt(((fit.render(c, s, x, y, h, bg, add=add) - t) ** 2).mean()) * 255))
    # shape check: a near-empty assembly can still have a small colour error on a picture
    # that is mostly background
    lo = fit.render(c, s, x, y, h, bgs[0])
    mask_t = np.abs(halves[0] - bgs[0]).max(axis=2) > SHAPE_DIFF
    mask_r = np.abs(lo - bgs[0]).max(axis=2) > SHAPE_DIFF
    iou = float((mask_t & mask_r).sum() / max(1, (mask_t | mask_r).sum()))
    row.update(graphic=g, view=view, rmse_normal=errs[0], rmse_selected=errs[1], scale=s,
               source="hd" if use_hd else "orig", x=x, y=y, iou=iou, mirror=mirror,
               dynamic=("on:" + ",".join(map(str, sorted(dyn)))) if isinstance(dyn, frozenset) else dyn)
    out = np.concatenate(outs, axis=1)[..., ::-1]
    out = np.concatenate([out, np.ones(out.shape[:2] + (1,), np.float32)], axis=2)
    return row, (out.clip(0, 1) * 255 + 0.5).astype(np.uint8).tobytes()


def build_file(job):
    """Rows and pictures of every catalogue object in one IFF."""
    name, data, sprites_dir, overrides, labels = job
    try:
        obj = objects.ObjectFile(data, name)
    except iff.IffError:
        return []
    sprites = assemble.Sprites(obj, sprites_dir)
    out = []
    for o, tiles in objects.catalogue_objects(obj):
        if labels and o["label"] not in labels:
            continue
        pic = obj.picture(o)
        if pic is None or pic[:2] != objects.PICTURE_SIZE:
            continue
        try:
            out.append(build_object(obj, sprites, o, tiles, overrides.get(o["label"], {})))
        except Exception as e:  # report it, don't lose the whole run
            row = dict(zip(HEADER, (name, o["label"], objects.picture_name(*pic), len(tiles), -1, -1, -1.0, -1.0,
                                    0.0, f"error {e!r}", 0.0, 0.0, f"{o['guid']:08x}", o["id"], o["catpic"], 0.0,
                                    False, False)))
            out.append((row, None))
    return out


def error_of(row) -> float:
    return max(float(row["rmse_normal"]), float(row["rmse_selected"])) if float(row["rmse_normal"]) >= 0 else -1.0


def best_rows(rows) -> dict[str, dict]:
    """picture -> the best-fitting built row (objects can share a picture: recolours,
    inventory tokens of world objects)."""
    best = {}
    for r in rows:
        e = error_of(r)
        if e >= 0 and (r["picture"] not in best or e < error_of(best[r["picture"]])):
            best[r["picture"]] = r
    return best


def choose(rows, approved: set, rejected: set, overrides: dict) -> set[str]:
    """The pictures to install."""
    chosen = set()
    for pic, r in best_rows(rows).items():
        passes = error_of(r) <= MAX_ERROR and float(r["iou"]) >= MIN_OVERLAP
        by_hand = r["label"] in overrides and not overrides[r["label"]].get("none")
        if (passes or pic in approved or by_hand) and pic not in rejected:
            chosen.add(pic)
    return chosen


def read_results(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    header = lines[0].split("\t")
    return [dict(zip(header, l.split("\t"))) for l in lines[1:]]


def write_results(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write("# picture pixel = scale * (1x assembled pixel) + (x, y); the HD picture is all x3\n")
        fh.write("\t".join(HEADER) + "\n")
        for r in rows:
            fh.write("\t".join(f"{r[k]:.4f}" if isinstance(r[k], float) else str(r[k]) for k in HEADER) + "\n")


def install(work: Path, game: Path, rows) -> None:
    chosen = choose(rows, read_list(work / "approved.txt"), read_list(work / "rejected.txt"),
                    load_overrides(work / "overrides.tsv"))
    dest = game / "vkproxy" / "replace"
    dest.mkdir(parents=True, exist_ok=True)
    copied = removed = 0
    for pic in sorted(chosen):
        src, dst = work / "hd" / f"{pic}@3.bgra", dest / f"{pic}@3.bgra"
        if not src.exists():
            continue
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            shutil.copyfile(src, dst)
            copied += 1
    for pic in read_list(work / "installed.txt") - chosen:
        if (dest / f"{pic}@3.bgra").exists():
            (dest / f"{pic}@3.bgra").unlink()
            removed += 1
    installed = sorted(p for p in chosen if (dest / f"{p}@3.bgra").exists())
    (work / "installed.txt").write_text("\n".join(installed) + "\n")
    print(f"installed {len(installed)} catalogue pictures ({copied} new or changed, {removed} taken out) "
          f"into {dest}; restart the game to see them")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="HD catalogue pictures from the objects' own sprites.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--work", type=Path, default=REPO / "work" / "catalog")
    ap.add_argument("--sprites", type=Path, help="HD sprites (default: <game>/vkproxy/replace)")
    ap.add_argument("--labels", nargs="*", default=[], help="only the objects with these labels")
    ap.add_argument("--install", action="store_true", help="then install the chosen pictures")
    ap.add_argument("--install-only", action="store_true", help="install from the last build, don't build")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args(argv)
    sprites_dir = args.sprites or args.game / "vkproxy" / "replace"
    (args.work / "hd").mkdir(parents=True, exist_ok=True)
    results = args.work / "results.tsv"
    if args.install_only:
        install(args.work, args.game, read_results(results))
        return 0

    t0 = time.time()
    labels = set(args.labels)
    overrides = load_overrides(args.work / "overrides.tsv")
    jobs = []
    for pattern in (p for k in sorted(hd_art.PACKS) for p in hd_art.PACKS[k]):
        for path in sorted(args.game.glob(pattern)):
            for name, data in spr2.iter_iffs(path):
                if b"BMP_" in data and b"OBJD" in data and (
                        not labels or any(l.encode("latin-1", "ignore") in data for l in labels)):
                    jobs.append((name, data, sprites_dir, overrides, labels))
    print(f"{len(jobs)} object files ({time.time() - t0:.0f} s)", flush=True)
    new_rows, pixels = [], {}
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for n, built in enumerate(pool.map(build_file, jobs), 1):
            for row, px in built:
                new_rows.append(row)
                if px:
                    pixels[(row["iff"], row["objd_id"])] = px
            if n % 200 == 0:
                print(f"  {n} / {len(jobs)} files ({time.time() - t0:.0f} s)", flush=True)
    if labels:  # replace those objects' rows, keep the rest
        found = {r["label"] for r in new_rows}
        missing = labels - found
        if missing:
            print("not found:", ", ".join(sorted(missing)))
        rows = [r for r in read_results(results) if r["label"] not in found] + new_rows
    else:
        rows = new_rows
    for pic, r in best_rows(rows).items():
        px = pixels.get((r["iff"], r["objd_id"]))
        if px is not None:
            (args.work / "hd" / f"{pic}@3.bgra").write_bytes(px)
    write_results(results, rows)
    errs = np.array([error_of(r) for r in new_rows if error_of(r) >= 0])
    print(f"built {len(errs)} of {len(new_rows)} pictures in {time.time() - t0:.0f} s"
          + (f"; colour error (worse half) median {np.median(errs):.1f}, max {errs.max():.1f}" if len(errs) else ""))
    if args.install:
        install(args.work, args.game, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
