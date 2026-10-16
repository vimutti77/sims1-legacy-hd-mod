"""Reader/extractor for The Sims 1 FAR archives (version 1a / 1b).

Layout (little-endian):
    header:   b"FAR!byAZ" | u32 version | u32 manifest_offset
    manifest: u32 count, then per entry:
        1a: u32 size | u32 size2 | u32 offset | u32 name_len | name
        1b: u32 size | u32 size2 | u32 offset | u16 name_len | name

Usage:
    python tools/far.py list    <file.far>
    python tools/far.py extract <file.far> <out_dir>
"""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAGIC = b"FAR!byAZ"


@dataclass
class FarEntry:
    name: str
    size: int
    offset: int


class FarError(ValueError):
    pass


def _parse_manifest(data: bytes, pos: int, count: int, name_len_fmt: str) -> list[FarEntry]:
    entries = []
    nl_size = struct.calcsize(name_len_fmt)
    for _ in range(count):
        if pos + 12 + nl_size > len(data):
            raise FarError("manifest truncated")
        size, _size2, offset = struct.unpack_from("<III", data, pos)
        pos += 12
        (name_len,) = struct.unpack_from(name_len_fmt, data, pos)
        pos += nl_size
        if name_len > 1024 or pos + name_len > len(data):
            raise FarError("bad name length")
        name = data[pos:pos + name_len].decode("latin-1")
        pos += name_len
        if offset + size > len(data):
            raise FarError(f"entry {name!r} points past end of file")
        entries.append(FarEntry(name, size, offset))
    if pos != len(data):
        raise FarError("trailing bytes after manifest")
    return entries


def read_far(data: bytes) -> list[FarEntry]:
    if data[:8] != MAGIC:
        raise FarError("not a FAR archive")
    _version, manifest_off = struct.unpack_from("<II", data, 8)
    (count,) = struct.unpack_from("<I", data, manifest_off)
    pos = manifest_off + 4
    # Try 1a (u32 name length) first, then 1b (u16).
    for fmt in ("<I", "<H"):
        try:
            return _parse_manifest(data, pos, count, fmt)
        except FarError:
            continue
    raise FarError("could not parse manifest as 1a or 1b")


def safe_join(root: Path, name: str) -> Path:
    """Join an archive member name under root, refusing path traversal."""
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".")]
    if any(p == ".." for p in parts) or (parts and ":" in parts[0]):
        raise FarError(f"unsafe member name: {name!r}")
    return root.joinpath(*parts)


def extract(path: Path, out_dir: Path) -> int:
    data = path.read_bytes()
    entries = read_far(data)
    for e in entries:
        target = safe_join(out_dir, e.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data[e.offset:e.offset + e.size])
    return len(entries)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("list", "extract"):
        print(__doc__)
        return 2
    path = Path(argv[1])
    if argv[0] == "list":
        for e in read_far(path.read_bytes()):
            print(f"{e.size:>10}  {e.name}")
        return 0
    if len(argv) < 3:
        print(__doc__)
        return 2
    n = extract(path, Path(argv[2]))
    print(f"extracted {n} files to {argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
