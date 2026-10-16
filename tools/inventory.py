"""Phase 0 step 1: inventory the game folder.

Walks the install folder, identifies file formats by magic bytes (not just
extension) and writes a Markdown report you can paste back for analysis.

Usage:
    python tools/inventory.py "C:\\...\\The Sims Legacy Collection" [-o report.md]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

MAGICS = [
    (b"FAR!byAZ", "FAR archive"),
    (b"IFF FILE 2.5", "IFF 2.5"),
    (b"MZ", "PE executable/DLL"),
    (b"BM", "BMP image"),
    (b"\x89PNG", "PNG image"),
    (b"RIFF", "RIFF (WAV/AVI)"),
    (b"DDS ", "DDS texture"),
    (b"PK\x03\x04", "ZIP"),
    (b"XA", "XA audio"),
    (b"<?xml", "XML"),
]

# Folder / file names worth a closer look for the UI patch.
INTERESTING = ("ui", "uigraphics", "uiscripts", "font", "config", "res", ".uis", ".ini", ".cfg")


def detect(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return "unreadable"
    for magic, name in MAGICS:
        if head.startswith(magic):
            return name
    if head and all(32 <= b < 127 or b in (9, 10, 13) for b in head):
        return "text"
    return "unknown"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def build_report(root: Path) -> str:
    by_ext: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_fmt: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    dirs: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    interesting: list[str] = []
    exes: list[str] = []

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        size = p.stat().st_size
        ext = p.suffix.lower() or "(none)"
        fmt = detect(p)
        for d in (by_ext[ext], by_fmt[fmt]):
            d[0] += 1
            d[1] += size
        top = rel.parts[0] if len(rel.parts) > 1 else "."
        dirs[top][0] += 1
        dirs[top][1] += size
        low = str(rel).lower().replace("\\", "/")
        if any(k in low for k in INTERESTING):
            interesting.append(f"{rel}  ({fmt}, {human(size)})")
        if fmt == "PE executable/DLL":
            exes.append(f"{rel}  ({human(size)})")

    out = [f"# Inventory: {root.name}", ""]

    def table(title: str, rows: dict[str, list[int]]) -> None:
        out.extend([f"## {title}", "", "| name | files | size |", "|---|---:|---:|"])
        for k, (n, s) in sorted(rows.items(), key=lambda kv: -kv[1][1]):
            out.append(f"| {k} | {n} | {human(s)} |")
        out.append("")

    table("Top-level folders", dirs)
    table("By detected format", by_fmt)
    table("By extension", by_ext)
    out.extend(["## Executables / DLLs", ""] + [f"- {e}" for e in exes] + [""])
    out.extend(["## UI / config candidates", ""]
               + [f"- {i}" for i in interesting[:300]]
               + ([f"- ... and {len(interesting) - 300} more"] if len(interesting) > 300 else [])
               + [""])
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game_dir", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=Path("inventory.md"))
    args = ap.parse_args()
    report = build_report(args.game_dir)
    args.output.write_text(report, encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
