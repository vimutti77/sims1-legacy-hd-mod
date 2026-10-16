"""Phase 0 step 2: find scaling-related strings inside Sims.exe.

Extracts ASCII and UTF-16LE strings and keeps those matching keywords that
might reveal how the Legacy build picks its UI scale (config keys, cheats,
command-line switches, resolution tables).

Usage:
    python tools/exe_strings.py Sims.exe [-k extra,keywords] [-o strings.md] [--all]
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

DEFAULT_KEYWORDS = [
    "scale", "scaling", "resolution", "uiscale", "dpi", "zoom", "width", "height",
    "1920", "1080", "3840", "2160", "800x600", "1024x768", "fullscreen", "windowed",
    "borderless", "-r", "-w", "graphics", "config", ".ini", ".cfg", "registry",
    "uis", "uigraphics", "font", "cheat", "prop",
]

MIN_LEN = 4
ASCII_RE = re.compile(rb"[\x20-\x7e]{%d,}" % MIN_LEN)
UTF16_RE = re.compile(rb"(?:[\x20-\x7e]\x00){%d,}" % MIN_LEN)


def extract_strings(data: bytes) -> list[tuple[int, str, str]]:
    found = [(m.start(), "ascii", m.group().decode("ascii")) for m in ASCII_RE.finditer(data)]
    found += [(m.start(), "utf16", m.group().decode("utf-16le")) for m in UTF16_RE.finditer(data)]
    found.sort()
    return found


def matches(s: str, keywords: list[str]) -> bool:
    low = s.lower()
    return any(k in low for k in keywords)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exe", type=Path)
    ap.add_argument("-k", "--keywords", default="", help="comma-separated extra keywords")
    ap.add_argument("-o", "--output", type=Path, default=Path("strings.md"))
    ap.add_argument("--all", action="store_true", help="dump every string, not just matches")
    args = ap.parse_args()

    keywords = DEFAULT_KEYWORDS + [k.strip().lower() for k in args.keywords.split(",") if k.strip()]
    strings = extract_strings(args.exe.read_bytes())
    hits = strings if args.all else [s for s in strings if matches(s[2], keywords)]

    lines = [f"# Strings in {args.exe.name}", "",
             f"{len(hits)} of {len(strings)} strings shown", "",
             "| offset | enc | string |", "|---|---|---|"]
    for off, enc, s in hits:
        cell = s.replace("|", "\\|").replace("`", "'")
        lines.append(f"| 0x{off:08X} | {enc} | `{cell}` |")
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({len(hits)} strings)")


if __name__ == "__main__":
    main()
