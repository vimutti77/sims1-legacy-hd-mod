"""Disassemble part of a memory dump of Sims.exe (tools/exe_dump.py) at offsets from its base.

vkproxy logs code addresses as offsets from the module base (log_game_stack), and offsets are
the same in every run, so this takes offsets and prints them next to each instruction (the
dump's own base, which it was relocated for, is needed to read absolute addresses in the code).

Usage:
    py tools/exe_dis.py <dump.bin> <base hex> <from offset hex> <to offset hex>
Needs GNU objdump on PATH.
"""
from __future__ import annotations

import re
import subprocess
import sys

LINE = re.compile(r"^\s*([0-9a-f]+):\t(.*)$")


def disassemble(dump: str, base: int, start: int, stop: int) -> list[str]:
    out = subprocess.run(["objdump", "-D", "-b", "binary", "-m", "i386", "-M", "intel", f"--adjust-vma={base:#x}",
                          f"--start-address={base + start:#x}", f"--stop-address={base + stop:#x}", dump],
                         capture_output=True, text=True, check=True).stdout
    lines = []
    for line in out.splitlines():
        m = LINE.match(line)
        if m:
            va = int(m.group(1), 16)
            text = re.sub(r"0x([0-9a-f]{6,8})\b",
                          lambda t: f"{t.group(0)}(+{int(t.group(1), 16) - base:x})"
                          if base <= int(t.group(1), 16) < base + 0x800000 else t.group(0), m.group(2))
            lines.append(f"+{va - base:06x}  {text}")
    return lines


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    dump, base, start, stop = argv[0], *(int(a, 16) for a in argv[1:])
    print("\n".join(disassemble(dump, base, start, stop)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
