"""Local page to pick by eye the view and state of catalogue objects the search got wrong.

For each picture: the game's picture; a row of the 4 views x mirrored in the state the
search chose; a row of every state (x dynamic sprites off / on when the object has them)
in the view it chose. Pick one in each row, or "none matches"; the button copies lines
for overrides.tsv (label, state, dynamic, view, mirror; empty = keep the search's choice).
The images are made from the user's game files, so the page stays on this machine (work/).

Usage:
    py catalog/states.py --game <dir> --pictures <file of picture names> [--work work/catalog]
                         [--out work/catalog_states]
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import assemble  # noqa: E402
import build  # noqa: E402
import fit  # noqa: E402
import hd_art  # noqa: E402
import hd_composite  # noqa: E402
import objects  # noqa: E402
import spr2  # noqa: E402

BG = np.array([104, 164, 184], np.float32) / 255  # the normal picture background
BOX = 200  # largest side of a shown assembly


def png(path: Path, rgb) -> None:
    a = np.concatenate([rgb, np.ones(rgb.shape[:2] + (1,), np.float32)], axis=2)
    hd_composite.write_png(path, a.shape[1], a.shape[0], (a.clip(0, 1) * 255 + 0.5).astype(np.uint8).tobytes())


def fit_box(canvas):
    s = min(1.0, BOX / max(canvas.shape[:2]))
    h, w = max(1, int(canvas.shape[0] * s)), max(1, int(canvas.shape[1] * s))
    return fit.render(canvas, s, 0, 0, h, BG, width=w)


def dynamic_of(text: str):
    """results.tsv's dynamic column -> assemble's dynamic argument."""
    if text.startswith("on:"):
        return frozenset(int(x) for x in text[3:].split(",") if x)
    return text == "True"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Pick the view and state of catalogue objects by eye.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--pictures", type=Path, required=True, help="file with the picture names to show")
    ap.add_argument("--work", type=Path, default=REPO / "work" / "catalog")
    ap.add_argument("--out", type=Path, default=REPO / "work" / "catalog_states")
    args = ap.parse_args(argv)
    (args.out / "img").mkdir(parents=True, exist_ok=True)
    wanted = build.read_list(args.pictures)
    rows = {}
    for r in build.read_results(args.work / "results.tsv"):
        if r["picture"] in wanted and int(r["view"]) >= 0:
            if r["picture"] not in rows or build.error_of(r) < build.error_of(rows[r["picture"]]):
                rows[r["picture"]] = r
    files = {r["iff"] for r in rows.values()}
    objs = {}
    for pattern in (p for k in sorted(hd_art.PACKS) for p in hd_art.PACKS[k]):
        for path in sorted(args.game.glob(pattern)):
            for name, data in spr2.iter_iffs(path):
                if name in files:
                    objs[name] = objects.ObjectFile(data, name)

    items = []
    for k, (pic, r) in enumerate(sorted(rows.items(), key=lambda kv: kv[1]["label"])):
        obj = objs.get(r["iff"])
        if obj is None:
            continue
        o, tiles = next(((o, t) for o, t in objects.catalogue_objects(obj) if o["label"] == r["label"]), (None, None))
        if o is None:
            continue
        sprites = assemble.Sprites(obj)
        view, mirror, g0, dyn0 = int(r["view"]), r["mirror"] == "True", int(r["graphic"]), dynamic_of(r["dynamic"])
        w, h, px = obj.picture(o)
        thumb = np.frombuffer(px, np.uint8).reshape(h, w, 4)[..., :3].astype(np.float32) / 255
        png(args.out / "img" / f"o{k}.png", np.repeat(np.repeat(thumb[:, :h], 5, 0), 5, 1))

        def shot(g, v, m, dyn, name):
            c = assemble.assemble(sprites, tiles, g, v, dynamic=dyn, mirror=m)
            if c is None or c[..., 3].max() <= 0.5:
                return None
            png(args.out / "img" / name, fit_box(c))
            return name

        views = []
        for v in range(4):
            for m in (False, True):
                f = shot(g0, v, m, dyn0, f"v{k}_{v}_{int(m)}.png")
                if f:
                    views.append(dict(v=v, m=m, img=f))
        has_dyn = any(t.get("dyn_n") for t in tiles)
        n = max(1, min(max(t["ngraphics"] or 1 for t in tiles), build.MAX_GRAPHICS))
        states = []
        for g in range(n):
            for dyn in ((False, True) if has_dyn else (False,)):
                f = shot(g, view, mirror, dyn, f"s{k}_{g}_{int(dyn)}.png")
                if f:
                    states.append(dict(g=g, dyn=("on" if dyn else "off") if has_dyn else "", img=f))
        items.append(dict(name=r["label"], label=html.escape(r["label"]), pic=pic, orig=f"o{k}.png", view=view, mirror=mirror,
                          views=views, states=states))
        print(f"{r['label']}: {len(views)} views, {len(states)} states")
    (args.out / "index.html").write_text(PAGE.replace("__DATA__", json.dumps(items, ensure_ascii=False)),
                                         encoding="utf-8")
    print(len(items), "objects ->", args.out / "index.html")
    return 0


PAGE = """<!doctype html><html lang="th"><head><meta charset="utf-8"><title>Pick States</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root { --bg:#f6f6f4; --ink:#1d1d1f; --mute:#6b6b70; --card:#fff; --line:#e2e2e0; --pick:#0a7cff; }
@media (prefers-color-scheme: dark) { :root { --bg:#141416; --ink:#ececee; --mute:#9a9aa2; --card:#1f1f22; --line:#2e2e33; --pick:#4ea1ff; } }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.4 system-ui, "Segoe UI", sans-serif; }
header { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line); padding:10px 16px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
header h1 { font-size:16px; margin:0; }
button { font:inherit; color:inherit; background:var(--card); border:1px solid var(--line); border-radius:6px; padding:4px 10px; cursor:pointer; }
main { padding:8px 16px 40px; }
section { border-bottom:1px solid var(--line); padding:12px 0; }
h2 { font-size:16px; margin:0 0 6px; }
h3 { font-size:13px; margin:8px 0 4px; color:var(--mute); font-weight:600; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:flex-start; }
figure { margin:0; background:var(--card); border:2px solid var(--line); border-radius:8px; padding:4px; text-align:center; cursor:pointer; width:212px; }
figure.orig { cursor:default; border-style:dashed; }
figure.orig img { image-rendering:pixelated; }
figure.on { border-color:var(--pick); box-shadow:0 0 0 2px var(--pick); }
figure img { max-width:200px; max-height:200px; }
figcaption { color:var(--mute); font-size:12px; }
.none { width:auto; padding:10px 14px; align-self:center; }
</style></head><body>
<header><h1>เลือกมุมและสถานะที่ตรงกับภาพในรายการ</h1><span id="count"></span>
<button id="copy">คัดลอกตัวเลือกทั้งหมด</button><span id="copied"></span></header><main id="list"></main>
<script>
const ITEMS = __DATA__;
const pick = (() => { try { return JSON.parse(localStorage.getItem("statepick2") || "{}"); } catch (e) { return {}; } })();
const save = () => { try { localStorage.setItem("statepick2", JSON.stringify(pick)); } catch (e) {} };
const $ = id => document.getElementById(id);
function fig(it, kind, i, o, caption) {
  const cur = (pick[it.pic] || {})[kind];
  return `<figure data-pic="${it.pic}" data-kind="${kind}" data-i="${i}" class="${cur === i ? "on" : ""}">
    <img loading="lazy" src="img/${o.img}"><figcaption>${caption}</figcaption></figure>`;
}
function render() {
  $("list").innerHTML = ITEMS.map(it => {
    const p = pick[it.pic] || {};
    const views = it.views.map((o, i) => fig(it, "view", i, o, `มุม ${o.v}${o.m ? " กลับซ้ายขวา" : ""}${o.v === it.view && o.m === it.mirror ? " (ที่ระบบเลือก)" : ""}`)).join("");
    const states = it.states.map((o, i) => fig(it, "state", i, o, `สถานะ ${o.g}${o.dyn ? " · ชิ้นซ่อนได้ " + (o.dyn === "on" ? "เปิด" : "ปิด") : ""}`)).join("");
    return `<section><h2>${it.label}</h2><div class="row"><figure class="orig"><img src="img/${it.orig}"><figcaption>ภาพในรายการเดิม</figcaption></figure>
      <figure class="none ${p.none ? "on" : ""}" data-pic="${it.pic}" data-kind="none" data-i="1">ไม่มีอันไหนตรง</figure></div>
      <h3>มุม (สถานะที่ระบบเลือก)</h3><div class="row">${views}</div>
      <h3>สถานะ (มุมที่ระบบเลือก)</h3><div class="row">${states}</div></section>`;
  }).join("");
  $("count").textContent = `เลือกแล้ว ${Object.keys(pick).length} / ${ITEMS.length}`;
}
$("list").addEventListener("click", ev => {
  const f = ev.target.closest("figure[data-pic]"); if (!f) return;
  const p = pick[f.dataset.pic] = pick[f.dataset.pic] || {};
  if (f.dataset.kind === "none") { pick[f.dataset.pic] = p.none ? {} : { none: true }; }
  else { delete p.none; const i = Number(f.dataset.i); p[f.dataset.kind] = p[f.dataset.kind] === i ? undefined : i; }
  save(); render();
});
$("copy").addEventListener("click", () => {
  const lines = ITEMS.filter(it => pick[it.pic] && Object.keys(pick[it.pic]).length).map(it => {
    const p = pick[it.pic];
    if (p.none) return `${it.name}\\tnone`;
    const s = p.state !== undefined ? it.states[p.state] : null;
    const v = p.view !== undefined ? it.views[p.view] : null;
    return [it.name, s ? s.g : "", s ? s.dyn : "", v ? v.v : "", v ? v.m : ""].join("\\t");
  });
  navigator.clipboard.writeText(lines.join("\\n")).then(() => { $("copied").textContent = "คัดลอกแล้ว"; },
    () => { $("copied").textContent = "คัดลอกไม่ได้"; });
});
render();
</script></body></html>"""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
