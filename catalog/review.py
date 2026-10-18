"""Local review page for the catalogue pictures: the game's picture next to the HD one.

Each card shows one picture (the best-fitting object that uses it), its colour error and
shape overlap, whether it is installed, and a "wrong" tick and a note box. Ticks and notes
stay in the browser (localStorage); the copy button puts the ticked or noted pictures on
the clipboard as lines of label, file, picture, "ผิด" when ticked, note.
The images are made from the user's game files, so the page stays on this machine (work/).

Usage:
    py catalog/review.py --game <dir> [--work work/catalog] [--out work/catalog_review]
                         [--pending] [--previous <dir of HD pictures reviewed before>] [--round N]
--pending: only the pictures that are not installed.
--previous: mark the pictures that changed visibly since that version (the game's
<game>/vkproxy/replace compares with what is installed); --changed-only shows only those.
--round: ticks and notes are stored per round name; a new round starts with none
(the pictures judged wrong before still show as such, from rejected.txt).

Pictures looked at on a page and found right:
    py catalog/review.py --confirm <page dir> [--picked <file>]
keeps a copy of each one's HD picture, as the page showed it, in <work>/reviewed/: every
picture on the page, or only those named in the file (lines from the copy button). Later
pages leave those pictures out until they change visibly (--all shows them anyway).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Optional

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import hd_art  # noqa: E402
import hd_composite  # noqa: E402
import iff  # noqa: E402
import objects  # noqa: E402
import spr2  # noqa: E402


def game_pictures(game: Path) -> dict[tuple[str, str], bytes]:
    """(file name, BMP_ id) -> RGBA of every 74x37 picture in the game."""
    out = {}
    for pattern in (p for k in sorted(hd_art.PACKS) for p in hd_art.PACKS[k]):
        for path in sorted(game.glob(pattern)):
            for name, data in spr2.iter_iffs(path):
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
                    if (w, h) == objects.PICTURE_SIZE:
                        out[(name, str(c.id))] = px
    return out


def visibly_changed(a_path: Path, b_path: Path) -> bool:
    if not a_path.exists():
        return True
    a = np.frombuffer(a_path.read_bytes(), np.uint8).astype(np.int16)
    b = np.frombuffer(b_path.read_bytes(), np.uint8).astype(np.int16)
    if a.shape != b.shape:
        return True
    d = np.abs(a - b)
    return bool(d.mean() > 1.5 or (d > 48).mean() > 0.01)


def already_reviewed(reviewed: Path, hd_file: Optional[Path]) -> bool:
    """Whether a picture was confirmed on a page (its copy in <work>/reviewed/) and has not
    changed visibly since. hd_file is None when there is no HD picture now; one confirmed
    without an HD picture has an empty copy."""
    if not reviewed.exists():
        return False
    if hd_file is None:
        return reviewed.stat().st_size == 0
    return not visibly_changed(reviewed, hd_file)


def picture_names(text: str) -> set[str]:
    """The picture names in text, such as lines copied from a review page."""
    return set(re.findall(r"\b\d+x\d+_[0-9a-f]{16}\b", text))


def confirm(page: Path, work: Path, only: Optional[set] = None) -> tuple[int, list[str]]:
    """(count, labels left out): every card of a review page (or those whose picture is in
    only) goes into <work>/reviewed/ with its HD picture as the page shows it. A card whose HD
    picture was rebuilt after the page was made is left out: what was looked at is not what
    would be kept."""
    m = re.search(r"const ITEMS = (\[.*\]);\r?\n", (page / "index.html").read_text(encoding="utf-8"))
    if m is None:
        raise ValueError(f"{page / 'index.html'}: not a review page")
    dest = work / "reviewed"
    dest.mkdir(parents=True, exist_ok=True)
    count, left_out = 0, []
    for it in json.loads(m.group(1)):
        if only is not None and it["pic"] not in only:
            continue
        name = f"{it['pic']}@3.bgra"
        if not it["built"]:
            (dest / name).write_bytes(b"")
            count += 1
            continue
        src = work / ("ai" if it["src"] == "AI" else "hd") / name
        shown = page / "img" / f"{it['k']}_hd.png"
        if not src.exists() or not shown.exists():
            left_out.append(it["name"])
            continue
        bgra = src.read_bytes()
        _w, _h, rgba = hd_composite.read_png(shown)
        if np.frombuffer(bgra, np.uint8).reshape(-1, 4)[:, [2, 1, 0, 3]].tobytes() != bytes(rgba):
            left_out.append(it["name"])
            continue
        (dest / name).write_bytes(bgra)
        count += 1
    return count, left_out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Local review page for the catalogue pictures.")
    ap.add_argument("--game", type=Path)
    ap.add_argument("--work", type=Path, default=REPO / "work" / "catalog")
    ap.add_argument("--out", type=Path, default=REPO / "work" / "catalog_review")
    ap.add_argument("--pending", action="store_true", help="only the pictures that are not installed")
    ap.add_argument("--previous", type=Path, help="folder of the HD pictures reviewed before")
    ap.add_argument("--round", default="3", help="name of this review round: ticks and notes are kept per round")
    ap.add_argument("--changed-only", action="store_true", help="with --previous: only the pictures that changed")
    ap.add_argument("--all", action="store_true", help="also the pictures confirmed before and unchanged since")
    ap.add_argument("--confirm", type=Path, metavar="PAGE", help="the pictures on this review page are right")
    ap.add_argument("--picked", type=Path, metavar="FILE",
                    help="with --confirm: only the pictures named in this file (lines from the copy button)")
    args = ap.parse_args(argv)
    if args.confirm:
        only = picture_names(args.picked.read_text(encoding="utf-8")) if args.picked else None
        if only is not None and not only:
            ap.error(f"{args.picked}: no picture names in it")
        count, left_out = confirm(args.confirm, args.work, only)
        print(f"{count} pictures confirmed into {args.work / 'reviewed'}")
        if left_out:
            print("changed since the page was made, not confirmed:", ", ".join(left_out))
        return 0
    if args.game is None:
        ap.error("--game is needed to make a page")
    (args.out / "img").mkdir(parents=True, exist_ok=True)
    installed = build.read_list(args.work / "installed.txt")
    rejected = build.read_list(args.work / "rejected.txt")
    ai_names = build.read_list(args.work / "ai.txt")
    originals = game_pictures(args.game)

    # one card per picture: the best-fitting row, with the other objects that share the picture
    groups: dict[str, list] = {}
    for r in build.read_results(args.work / "results.tsv"):
        groups.setdefault(r["picture"] or f"none:{r['iff']}:{r['bmp_id']}", []).append(r)
    items, hidden = [], 0
    for i, group in enumerate(groups.values()):
        group.sort(key=lambda r: build.error_of(r) if build.error_of(r) >= 0 else 999.0)
        r = group[0]
        if r["picture"] in installed and args.pending:
            continue
        px = originals.get((r["iff"], r["bmp_id"]))
        if px is None:
            continue
        key = f"p{i:04d}"
        hd_file = args.work / "hd" / f"{r['picture']}@3.bgra"
        built = build.error_of(r) >= 0 and hd_file.exists()
        upscaled = not built and r["picture"] in ai_names and (args.work / "ai" / hd_file.name).exists()
        if upscaled:  # a painted icon upscaled by the AI (ai_pictures.py)
            hd_file = args.work / "ai" / hd_file.name
        if (not args.all and r["picture"]
                and already_reviewed(args.work / "reviewed" / hd_file.name, hd_file if built or upscaled else None)):
            hidden += 1
            continue
        hd_composite.write_png(args.out / "img" / f"{key}_o.png", 74, 37, px)
        if built or upscaled:
            bgra = np.frombuffer(hd_file.read_bytes(), np.uint8).reshape(111, 222, 4)
            hd_composite.write_png(args.out / "img" / f"{key}_hd.png", 222, 111, bgra[..., [2, 1, 0, 3]].tobytes())
        changed = None
        if (built or upscaled) and args.previous is not None:
            changed = visibly_changed(args.previous / hd_file.name, hd_file)
        if args.changed_only and not changed:
            continue
        items.append(dict(k=key, name=r["label"], label=html.escape(r["label"]), iff=r["iff"],
                          pack=r["iff"].split("/")[0].replace(".far", ""), pic=r["picture"] or key,
                          built=built or upscaled, err=round(build.error_of(r), 1) if built else None,
                          iou=round(float(r.get("iou") or 0), 2), inst=r["picture"] in installed,
                          rej=r["picture"] in rejected, changed=changed, tiles=int(r["tiles"]), g=r["graphic"],
                          view=r["view"], scale=r["scale"], src="AI" if upscaled else r["source"], guid=r["guid"],
                          also=[html.escape(g["label"]) for g in group[1:]]))
    page = PAGE.replace("__ROUND__", args.round)
    if args.pending:  # nothing installed on this page
        page = page.replace('<option value="inst">ติดตั้งแล้ว</option>', "").replace('ติดตั้งแล้ว ${n("inst")} · ', "")
    (args.out / "index.html").write_text(page.replace("__DATA__", json.dumps(items, ensure_ascii=False)),
                                         encoding="utf-8")
    print(f"{len(items)} pictures ({sum(i['built'] for i in items)} built, {sum(i['inst'] for i in items)} "
          f"installed) -> {args.out / 'index.html'}"
          + (f"; {hidden} confirmed before and unchanged left out (--all shows them)" if hidden else ""))
    return 0


PAGE = """<!doctype html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Catalogue Review</title>
<style>
:root { --bg:#f6f6f4; --card:#fff; --ink:#1d1d1f; --mute:#6b6b70; --line:#e2e2e0; --ok:#1f7a3a; --warn:#a45a00; --bad:#b3261e; --pick:#fde7e5; }
@media (prefers-color-scheme: dark) { :root { --bg:#141416; --card:#1f1f22; --ink:#ececee; --mute:#9a9aa2; --line:#2e2e33; --ok:#5cc27a; --warn:#e3a14a; --bad:#ff7a70; --pick:#3a1f1d; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.4 system-ui, "Segoe UI", sans-serif; }
header { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line); padding:10px 16px; display:flex; flex-wrap:wrap; gap:8px 14px; align-items:center; }
header h1 { font-size:16px; margin:0 8px 0 0; }
header .stats { color:var(--mute); }
select, input[type=search], button { font:inherit; color:inherit; background:var(--card); border:1px solid var(--line); border-radius:6px; padding:4px 8px; }
button { cursor:pointer; }
main { display:grid; grid-template-columns:repeat(auto-fill, minmax(236px, 1fr)); gap:10px; padding:12px 16px 40px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:7px; }
.card.marked { background:var(--pick); border-color:var(--bad); }
.pics { display:flex; flex-direction:column; gap:3px; align-items:center; }
.pics img { width:222px; height:111px; image-rendering:pixelated; display:block; }
.pics img.hd { image-rendering:auto; }
.pics .none { width:222px; height:111px; display:flex; align-items:center; justify-content:center; color:var(--mute); border:1px dashed var(--line); font-size:12px; text-align:center; padding:6px; }
.tag { font-size:11px; color:var(--mute); }
.name { font-weight:600; margin-top:5px; overflow-wrap:anywhere; }
.meta { font-size:12px; color:var(--mute); }
.badge { font-size:11px; border-radius:4px; padding:0 5px; margin-right:4px; border:1px solid currentColor; }
.ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
label.mark { display:inline-flex; gap:4px; align-items:center; margin-top:4px; font-size:12px; cursor:pointer; }
textarea.note { width:100%; margin-top:4px; font:inherit; font-size:12px; color:inherit; background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:4px; min-height:34px; resize:vertical; }
</style></head><body>
<header>
  <h1>ภาพในรายการ: เดิม / HD</h1>
  <span class="stats" id="stats"></span>
  <select id="filter">
    <option value="all">ทั้งหมด</option><option value="inst">ติดตั้งแล้ว</option>
    <option value="notinst">ประกอบได้ แต่ยังไม่ติดตั้ง</option><option value="fail">ประกอบไม่ได้</option>
    <option value="marked">ที่ติ๊กว่าผิด</option><option value="noted">ที่มี note</option><option value="rej">เคยแจ้งว่าผิด</option>
    <option value="changed">ต่างจากรอบที่ตรวจไปแล้วจนมองเห็น</option>
  </select>
  <select id="sort"><option value="err">ต่างมากก่อน</option><option value="errAsc">ต่างน้อยก่อน</option><option value="name">ชื่อ</option><option value="pack">pack</option></select>
  <input type="search" id="q" placeholder="ค้นชื่อ">
  <button id="copy">คัดลอกรายชื่อที่ติ๊กหรือมี note</button>
  <span class="stats" id="copied"></span>
</header>
<main id="grid"></main>
<script>
const ITEMS = __DATA__;
const store = (() => { try { return JSON.parse(localStorage.getItem("marked__ROUND__") || "{}"); } catch (e) { return {}; } })();
const save = () => { try { localStorage.setItem("marked__ROUND__", JSON.stringify(store)); } catch (e) {} };
const $ = id => document.getElementById(id);
const notes = (() => { try { return JSON.parse(localStorage.getItem("notes__ROUND__") || "{}"); } catch (e) { return {}; } })();
const saveNotes = () => { try { localStorage.setItem("notes__ROUND__", JSON.stringify(notes)); } catch (e) {} };
const esc = t => t.replace(/&/g, "&amp;").replace(/</g, "&lt;");
function status(it) {
  if (!it.built) return ['<span class="badge bad">ประกอบไม่ได้</span>', "fail"];
  return it.inst ? ['<span class="badge ok">ติดตั้งแล้ว</span>', "inst"] : ['<span class="badge warn">ยังไม่ติดตั้ง</span>', "notinst"];
}
function card(it) {
  const [badge] = status(it);
  const hd = it.built ? `<img class="hd" loading="lazy" src="img/${it.k}_hd.png" alt="HD">`
                      : '<div class="none">ไม่มี sprite ให้ประกอบ</div>';
  const err = (it.err === null ? "" : `ต่าง ${it.err} · ทับกัน ${Math.round(it.iou * 100)}%`)
            + (it.rej ? ' <span class="badge bad">เคยแจ้งว่าผิด</span>' : "")
            + (it.changed ? ' <span class="badge warn">ภาพเปลี่ยน</span>' : "");
  return `<div class="card${store[it.pic] ? " marked" : ""}" data-k="${it.pic}">
    <div class="pics"><span class="tag">เดิม (ขยาย 3 เท่า)</span><img loading="lazy" src="img/${it.k}_o.png" alt="เดิม">
    <span class="tag">HD</span>${hd}</div>
    <div class="name">${it.label}</div>
    <div class="meta">${badge}${err}</div>
    <div class="meta">${it.pack} · ${it.tiles} ช่อง · มุม ${it.view} · สถานะ ${it.g} · ${it.src}</div>
    ${it.also.length ? `<div class="meta">ใช้ภาพเดียวกับ: ${it.also.join(", ")}</div>` : ""}
    <label class="mark"><input type="checkbox" ${store[it.pic] ? "checked" : ""}> ผิด</label>
    <textarea class="note" placeholder="note" data-k="${it.pic}">${esc(notes[it.pic] || "")}</textarea></div>`;
}
function render() {
  const f = $("filter").value, q = $("q").value.trim().toLowerCase(), s = $("sort").value;
  const pick = it => f === "all" || (f === "marked" ? store[it.pic] : f === "noted" ? notes[it.pic] : f === "rej" ? it.rej
                     : f === "changed" ? it.changed : status(it)[1] === f);
  let list = ITEMS.filter(it => pick(it)
                                && (!q || it.label.toLowerCase().includes(q) || it.iff.toLowerCase().includes(q)));
  const e = it => it.err === null ? 999 : it.err;
  list.sort(s === "err" ? (a, b) => e(b) - e(a) : s === "errAsc" ? (a, b) => e(a) - e(b)
          : s === "name" ? (a, b) => a.label.localeCompare(b.label) : (a, b) => a.pack.localeCompare(b.pack) || a.label.localeCompare(b.label));
  $("grid").innerHTML = list.map(card).join("");
  const n = k => ITEMS.filter(it => status(it)[1] === k).length;
  $("stats").textContent = `แสดง ${list.length} / ${ITEMS.length} · ติดตั้งแล้ว ${n("inst")} · ยังไม่ติดตั้ง ${n("notinst")} · ประกอบไม่ได้ ${n("fail")} · ติ๊กว่าผิด ${ITEMS.filter(it => store[it.pic]).length} · มี note ${ITEMS.filter(it => notes[it.pic]).length}`;
}
$("grid").addEventListener("input", ev => {
  if (!ev.target.matches("textarea.note")) return;
  const t = ev.target.value.trim(); if (t) notes[ev.target.dataset.k] = ev.target.value; else delete notes[ev.target.dataset.k];
  saveNotes();
});
$("grid").addEventListener("change", ev => {
  if (!ev.target.matches("input[type=checkbox]")) return;
  const c = ev.target.closest(".card"); if (!c) return;
  if (ev.target.checked) store[c.dataset.k] = true; else delete store[c.dataset.k];
  c.classList.toggle("marked", ev.target.checked); save(); render();
});
$("copy").addEventListener("click", () => {
  const text = ITEMS.filter(it => store[it.pic] || notes[it.pic]).map(it =>
    `${it.name}\\t${it.iff}\\t${it.pic}\\t${store[it.pic] ? "ผิด" : ""}\\t${(notes[it.pic] || "").replace(/\\s+/g, " ").trim()}`).join("\\n");
  navigator.clipboard.writeText(text).then(() => { $("copied").textContent = "คัดลอกแล้ว"; },
    () => { $("copied").textContent = "คัดลอกไม่ได้"; });
});
["filter", "sort"].forEach(id => $(id).addEventListener("change", render));
$("q").addEventListener("input", render);
render();
</script></body></html>"""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
