"""Reader for The Sims 1 skin meshes: .skn (text) and .bmf (the same fields in binary).

A skin is one piece of a Sim (a head, a body, a hand) bound to bones of the skeleton.
Fields, in order (.skn: one item a line; .bmf: strings as a length byte + characters,
counts and ints as i32, floats as f32, all little endian):
    name, texture name
    bone count, bone names
    face count, faces (3 vertex indices each)
    binding count, bindings (bone index, first vertex, vertex count, first blended
        vertex, blended vertex count)
    UV count, UVs (u, v)
    blend count, blends (.skn: weight index; .bmf: i32 weight (1.0 = 0x8000), i32 index)
    vertex count, vertices (x, y, z, normal x, y, z), in the space of the bone they are
        bound to; for a head (bone HEAD): x up, y sideways, the face towards +z

Usage:
    py tools/skn.py <file.skn|file.bmf>     counts and bounds
"""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Skin:
    name: str
    texture: str
    bones: list[str]
    faces: list[tuple[int, int, int]]
    bindings: list[tuple[int, int, int, int, int]]
    uvs: list[tuple[float, float]]
    blends: list[tuple[float, int]] = field(default_factory=list)
    vertices: list[tuple[float, float, float, float, float, float]] = field(default_factory=list)


class SkinError(ValueError):
    pass


def _check(s: Skin) -> Skin:
    n = len(s.vertices)
    if any(not 0 <= i < n for f in s.faces for i in f) or len(s.uvs) > n:
        raise SkinError(f"{s.name}: faces or UVs past the {n} vertices")
    return s


def read_skn(text: str) -> Skin:
    lines = [ln.strip() for ln in text.splitlines()]
    pos = 0

    def line() -> str:
        nonlocal pos
        while pos < len(lines) and lines[pos] == "":
            pos += 1
        if pos >= len(lines):
            raise SkinError("skn truncated")
        pos += 1
        return lines[pos - 1]

    def count() -> int:
        return int(line())

    try:
        name, texture = line(), line()
        bones = [line() for _ in range(count())]
        faces = [tuple(int(v) for v in line().split()[:3]) for _ in range(count())]
        bindings = [tuple(int(v) for v in line().split()[:5]) for _ in range(count())]
        uvs = [tuple(float(v) for v in line().split()[:2]) for _ in range(count())]
        blends = [(float(a), int(b)) for a, b in (line().split()[:2] for _ in range(count()))]
        vertices = [tuple(float(v) for v in line().split()[:6]) for _ in range(count())]
    except ValueError as e:
        if isinstance(e, SkinError):
            raise
        raise SkinError(f"bad skn: {e}") from None
    return _check(Skin(name, texture, bones, faces, bindings, uvs, blends, vertices))


def read_bmf(data: bytes) -> Skin:
    pos = 0

    def string() -> str:
        nonlocal pos
        n = data[pos]
        s = data[pos + 1:pos + 1 + n].decode("latin-1")
        pos += 1 + n
        return s

    def ints(n: int, fmt: str = "i") -> tuple:
        nonlocal pos
        v = struct.unpack_from(f"<{n}{fmt}", data, pos)
        pos += 4 * n
        return v

    def count() -> int:
        (n,) = ints(1)
        if not 0 <= n <= 1 << 20:
            raise SkinError(f"bad count {n}")
        return n

    try:
        name, texture = string(), string()
        bones = [string() for _ in range(count())]
        faces = [ints(3) for _ in range(count())]
        bindings = [ints(5) for _ in range(count())]
        uvs = [ints(2, "f") for _ in range(count())]
        blends = [(w / 0x8000, i) for w, i in (ints(2) for _ in range(count()))]
        vertices = [ints(6, "f") for _ in range(count())]
    except (struct.error, IndexError) as e:
        raise SkinError(f"bmf truncated: {e}") from None
    if pos != len(data):
        raise SkinError(f"bmf: {len(data) - pos} bytes left over")
    return _check(Skin(name, texture, bones, faces, bindings, uvs, blends, vertices))


def read_skin(name: str, data: bytes) -> Skin:
    return read_bmf(data) if name.lower().endswith(".bmf") else read_skn(data.decode("latin-1"))


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    s = read_skin(argv[0], Path(argv[0]).read_bytes())
    print(f"{s.name} (texture {s.texture}): bones {s.bones}, {len(s.faces)} faces, {len(s.vertices)} vertices, "
          f"{len(s.blends)} blends")
    for axis in range(3):
        vals = [v[axis] for v in s.vertices]
        print(f"  {'xyz'[axis]}: {min(vals):.3f} .. {max(vals):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
