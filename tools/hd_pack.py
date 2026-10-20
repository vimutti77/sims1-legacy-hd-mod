"""Pack the installed HD files (vkproxy's content replacements) into a few big files.

The first open of a file costs ~5 ms (the antivirus scans it), so with 85,000 loose files the
first visit of a lot, floor or camera view waited seconds for the opens. vkproxy opens the
packs once, while the game loads, and reads each file with one read at an offset.

Usage:
    py tools/hd_pack.py --game <dir>             pack the loose files, then delete them
    py tools/hd_pack.py --game <dir> --backup D  ... moving them into D instead
    py tools/hd_pack.py --game <dir> --list      the packs and what they hold
    py tools/hd_pack.py --game <dir> --compact   rewrite the packs without replaced copies
Close the game first: it keeps the packs open.

Only content replacements are packed: <w>x<h>_<hash>[_f9|_w<mask>]@<scale>.bgra|.raw.
Font atlases (<w>x<h>@<scale>.bgra) and wall masks (*.wallmasks) stay loose.

A pack, <replace>/hd_<NNNN>.pack (little endian): the files' bytes one after another, then
the index, per file: u16 name length, the name (ASCII), u64 offset, u32 size, f64 mtime (the
loose file's, when packed); then the footer: u32 file count, u64 index offset, b"HDPK",
u32 version (1). A name in a later pack (by number) wins over the same name in an earlier
one, and a loose file wins over the packs, so tools install by writing loose files
(Installed.write) and pack them afterwards.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import struct
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CONTENT = re.compile(r"^\d+x\d+_[0-9a-f]{16}(_f\d+|_w\d+)?@\d+\.(bgra|raw)$")
PACK = re.compile(r"^hd_(\d{4})\.pack$")
MAGIC, VERSION = b"HDPK", 1
FOOTER = struct.Struct("<IQ4sI")
PACK_BYTES = 1 << 30  # a new pack is started past this size
MAX_NAME = 200


def read_index(path: Path) -> list[tuple[str, int, int, float]]:
    """(name, offset, size, mtime) of each file in a pack; ValueError if it is not one."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        if end < FOOTER.size:
            raise ValueError(f"{path}: too short for a pack")
        f.seek(end - FOOTER.size)
        count, index_at, magic, version = FOOTER.unpack(f.read(FOOTER.size))
        if magic != MAGIC or version != VERSION or index_at > end - FOOTER.size:
            raise ValueError(f"{path}: not a pack (version {VERSION})")
        f.seek(index_at)
        index = f.read(end - FOOTER.size - index_at)
    out, p = [], 0
    for _ in range(count):
        if p + 2 > len(index):
            raise ValueError(f"{path}: index cut short")
        (n,) = struct.unpack_from("<H", index, p)
        name = index[p + 2:p + 2 + n].decode("ascii", "replace")
        if n > MAX_NAME or p + 2 + n + 20 > len(index) or not CONTENT.match(name):
            raise ValueError(f"{path}: bad index entry {name!r}")
        off, size, mtime = struct.unpack_from("<QId", index, p + 2 + n)
        if off + size > index_at:
            raise ValueError(f"{path}: {name} lies outside the data")
        out.append((name, off, size, mtime))
        p += 2 + n + 20
    return out


def write_pack(path: Path, files) -> list[tuple[str, int, int, float]]:
    """Write a pack from (name, data, mtime) in order; its index."""
    index = []
    with open(path, "wb") as f:
        for name, data, mtime in files:
            index.append((name, f.tell(), len(data), mtime))
            f.write(data)
        index_at = f.tell()
        for name, off, size, mtime in index:
            raw = name.encode("ascii")
            f.write(struct.pack("<H", len(raw)) + raw + struct.pack("<QId", off, size, mtime))
        f.write(FOOTER.pack(len(index), index_at, MAGIC, VERSION))
    return index


class Installed:
    """The content replacements in a replace folder: packs, then loose files (which win)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.packed: dict[str, tuple[Path, int, int, float]] = {}  # name -> (pack, offset, size, mtime)
        self.loose: set[str] = set()
        if not self.folder.is_dir():
            return
        for p in self.packs():
            for name, off, size, mtime in read_index(p):
                self.packed[name] = (p, off, size, mtime)
        self.loose = {p.name for p in self.folder.iterdir() if CONTENT.match(p.name) and p.is_file()}

    def packs(self) -> list[Path]:
        return sorted(p for p in self.folder.glob("hd_*.pack") if PACK.match(p.name))

    def names(self) -> set[str]:
        return self.loose | set(self.packed)

    def __contains__(self, name: str) -> bool:
        return name in self.loose or name in self.packed

    def mtime(self, name: str) -> float | None:
        if name in self.loose:
            return (self.folder / name).stat().st_mtime
        return self.packed[name][3] if name in self.packed else None

    def size(self, name: str) -> int | None:
        if name in self.loose:
            return (self.folder / name).stat().st_size
        return self.packed[name][2] if name in self.packed else None

    def read(self, name: str) -> bytes | None:
        if name in self.loose:
            return (self.folder / name).read_bytes()
        if name not in self.packed:
            return None
        pack, off, size, _ = self.packed[name]
        with open(pack, "rb") as f:
            f.seek(off)
            return f.read(size)

    def write(self, name: str, data: bytes) -> None:
        if not CONTENT.match(name):
            raise ValueError(f"not a content replacement name: {name}")
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / name).write_bytes(data)
        self.loose.add(name)

    def copy(self, src: Path, name: str) -> None:
        self.write(name, Path(src).read_bytes())

    def remove(self, names) -> int:
        """Take files out: loose ones deleted, packs rewritten without them; how many went."""
        names = set(names)
        gone = len(names & self.names())
        for name in names & self.loose:
            (self.folder / name).unlink()
            self.loose.discard(name)
        for pack in self.packs():  # every copy, or an older one would show again
            drop = names & {name for name, *_ in read_index(pack)}
            if drop:
                self._rewrite(pack, drop)
        return gone

    def _rewrite(self, pack: Path, drop: set[str]) -> None:
        """The pack again without some of its files (deleted when nothing is left)."""
        entries = read_index(pack)
        keep = [e for e in entries if e[0] not in drop]
        for name, *_ in entries:
            if self.packed.get(name, (None,))[0] == pack:
                del self.packed[name]
        if not keep:
            pack.unlink()
        else:
            tmp = pack.with_name(pack.name + ".tmp")
            with open(pack, "rb") as f:
                def files():
                    for name, off, size, mtime in keep:
                        f.seek(off)
                        yield name, f.read(size), mtime
                index = write_pack(tmp, files())
            os.replace(tmp, pack)
            for name, off, size, mtime in index:
                self.packed[name] = (pack, off, size, mtime)
        # names still in earlier packs show again
        for p in self.packs():
            if p != pack:
                for name, off, size, mtime in read_index(p):
                    if name in drop:
                        self.packed[name] = (p, off, size, mtime)

    def pack(self, backup: Path | None = None, max_bytes: int = PACK_BYTES, log=print) -> int:
        """Move the loose files into new packs (read in parallel: first opens are slow), check
        each pack against what was read, then delete the loose files (or move them into
        backup). Returns how many were packed."""
        todo = sorted(self.loose)
        if not todo:
            return 0
        numbers = [int(PACK.match(p.name).group(1)) for p in self.packs()]
        number = max(numbers, default=-1) + 1
        done = 0
        with ThreadPoolExecutor(16) as ex:
            while todo:
                chunk, total = [], 0
                while todo and (not chunk or total < max_bytes):
                    total += (self.folder / todo[0]).stat().st_size
                    chunk.append(todo.pop(0))
                pack = self.folder / f"hd_{number:04d}.pack"
                digests = {}

                def files():
                    for i in range(0, len(chunk), 256):  # bounded read-ahead
                        part = chunk[i:i + 256]
                        datas = ex.map(lambda n: (self.folder / n).read_bytes(), part)
                        for name, data in zip(part, datas):
                            digests[name] = hashlib.blake2b(data, digest_size=16).digest()
                            yield name, data, (self.folder / name).stat().st_mtime

                tmp = pack.with_name(pack.name + ".tmp")
                index = write_pack(tmp, files())
                os.replace(tmp, pack)
                with open(pack, "rb") as f:
                    for name, off, size, _ in index:
                        f.seek(off)
                        if hashlib.blake2b(f.read(size), digest_size=16).digest() != digests[name]:
                            raise IOError(f"{pack}: {name} differs from the loose file; loose files kept")
                for name, off, size, mtime in index:
                    self.packed[name] = (pack, off, size, mtime)
                    if backup is None:
                        (self.folder / name).unlink()
                    else:
                        backup.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(self.folder / name), str(backup / name))
                    self.loose.discard(name)
                done += len(index)
                log(f"{pack.name}: {len(index)} files, {total / 2**20:.0f} MB")
                number += 1
        return done

    def compact(self, log=print) -> int:
        """Rewrite each pack without the files a later pack or a loose file replaces."""
        dropped = 0
        for pack in self.packs():
            stale = {name for name, *_ in read_index(pack)
                     if name in self.loose or self.packed.get(name, (pack,))[0] != pack}
            if stale:
                self._rewrite(pack, stale)
                dropped += len(stale)
                log(f"{pack.name}: {len(stale)} replaced copies dropped")
        return dropped


def finish_install(inst: Installed, log=print) -> None:
    """After an install: the new loose files into a pack, replaced copies out of the old ones."""
    if inst.pack(log=log):
        inst.compact(log=log)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Pack the installed HD files into a few big files.")
    ap.add_argument("--game", type=Path, required=True)
    ap.add_argument("--list", action="store_true", help="the packs and what they hold")
    ap.add_argument("--compact", action="store_true", help="rewrite the packs without replaced copies")
    ap.add_argument("--backup", type=Path, help="move the packed loose files here instead of deleting them")
    args = ap.parse_args(argv)
    inst = Installed(args.game / "vkproxy" / "replace")
    if args.list:
        for p in inst.packs():
            entries = read_index(p)
            live = sum(1 for name, *_ in entries if inst.packed.get(name, (None,))[0] == p and name not in inst.loose)
            print(f"{p.name}: {len(entries)} files ({live} in use), {p.stat().st_size / 2**20:.0f} MB")
        print(f"loose: {len(inst.loose)} files")
        return 0
    if args.compact:
        print(f"dropped {inst.compact()} replaced copies")
        return 0
    n = inst.pack(args.backup)
    print(f"packed {n} files; restart the game to use them" if n else "nothing loose to pack")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
