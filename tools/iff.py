"""Chunk lister for The Sims 1 IFF files (IFF FILE 2.5, also 2.0).

Layout (big-endian):
    header: 60-byte signature "IFF FILE 2.5:TYPE FOLLOWED BY SIZE\\0 JAMIE DOORNBOS & MAXIS 1"
            + u32 rsmp_offset                                  (64 bytes total)
            (a few Legacy house files say "IFF FILE 2.0:" with the same layout)
    chunk:  4-byte type | u32 size (includes 76-byte header) | u16 id | u16 flags
            | 64-byte label (NUL padded) | data

Common UI-relevant chunk types: BMP_ (bitmap), SPR#/SPR2 (sprites), STR# (strings),
PALT (palette).

Usage:
    python tools/iff.py <file.iff> [--dump <out_dir>]
"""
from __future__ import annotations

import struct
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SIGNATURE_PREFIXES = (b"IFF FILE 2.5:TYPE FOLLOWED BY SIZE", b"IFF FILE 2.0:TYPE FOLLOWED BY SIZE")
HEADER_SIZE = 64
CHUNK_HEADER_SIZE = 76


@dataclass
class IffChunk:
    type: str
    id: int
    flags: int
    label: str
    offset: int  # offset of chunk data
    size: int    # data size (without the 76-byte header)


class IffError(ValueError):
    pass


def read_iff(data: bytes) -> list[IffChunk]:
    if not data.startswith(SIGNATURE_PREFIXES):
        raise IffError("not an IFF 2.5/2.0 file")
    chunks = []
    pos = HEADER_SIZE
    while pos + CHUNK_HEADER_SIZE <= len(data):
        ctype = data[pos:pos + 4].decode("latin-1")
        size, cid, flags = struct.unpack_from(">IHH", data, pos + 4)
        if size < CHUNK_HEADER_SIZE or pos + size > len(data):
            raise IffError(f"bad chunk size at offset {pos}")
        label = data[pos + 12:pos + 76].split(b"\0", 1)[0].decode("latin-1")
        chunks.append(IffChunk(ctype, cid, flags, label, pos + CHUNK_HEADER_SIZE,
                               size - CHUNK_HEADER_SIZE))
        pos += size
    return chunks


def str_set(body: bytes) -> list[str]:
    """The strings of a STR# chunk (its first language's), formats 0 (Pascal strings),
    -1 (C strings), -2 (C string pairs: value, comment), -3 (language byte, value,
    comment as C strings) and -4 (language sets of language byte + value + comment, the
    strings' lengths 7-bit encoded)."""
    try:
        (fmt,) = struct.unpack_from("<h", body, 0)
        out = []
        if fmt == -4:
            pos = 3  # the first language set
            (count,) = struct.unpack_from("<H", body, pos)
            pos += 2

            def string() -> str:
                nonlocal pos
                n = shift = 0
                while True:
                    b = body[pos]
                    pos += 1
                    n |= (b & 0x7F) << shift
                    shift += 7
                    if not b & 0x80:
                        break
                s = body[pos:pos + n].decode("latin-1")
                pos += n
                return s

            for _ in range(count):
                pos += 1  # language
                out.append(string())
                string()  # comment
            return out
        if fmt not in (0, -1, -2, -3):
            raise IffError(f"unknown STR# format {fmt}")
        (count,) = struct.unpack_from("<H", body, 2)
        pos = 4

        def cstring() -> str:
            nonlocal pos
            end = body.index(b"\0", pos)
            s = body[pos:end].decode("latin-1")
            pos = end + 1
            return s

        for _ in range(count):
            if fmt == 0:
                n = body[pos]
                out.append(body[pos + 1:pos + 1 + n].decode("latin-1"))
                pos += 1 + n
            else:
                if fmt == -3:
                    pos += 1  # language
                out.append(cstring())
                if fmt != -1:
                    cstring()  # comment
        return out
    except (struct.error, IndexError, ValueError) as e:
        if isinstance(e, IffError):
            raise
        raise IffError(f"STR# truncated: {e}") from None


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    path = Path(argv[0])
    data = path.read_bytes()
    chunks = read_iff(data)
    print(f"{path.name}: {len(chunks)} chunks  "
          + ", ".join(f"{t}x{n}" for t, n in Counter(c.type for c in chunks).most_common()))
    for c in chunks:
        print(f"  {c.type} id={c.id:<6} size={c.size:<8} {c.label}")
    if len(argv) >= 3 and argv[1] == "--dump":
        out = Path(argv[2])
        out.mkdir(parents=True, exist_ok=True)
        for c in chunks:
            ext = ".bmp" if c.type == "BMP_" else ".bin"
            safe_type = "".join(ch if ch.isalnum() else "_" for ch in c.type)
            (out / f"{safe_type}_{c.id}{ext}").write_bytes(data[c.offset:c.offset + c.size])
        print(f"dumped to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
