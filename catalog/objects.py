"""Object IFFs as the catalogue builder needs them: OBJD fields, DGRP draw groups, BMP_ pictures.

OBJD: u32 version, then u16 fields (FreeSO's field list). Used here:
     1 base graphic (DGRP id of state 0)      2 number of graphics (states)
     8 master id (the tiles of a multi-tile object share it)
     9 sub index: low byte tile x, high byte tile y; -1 = the lead OBJD, which is the
       catalogue entry of a multi-tile object
    12/13 GUID    38 function flags (buy-mode category)
    39 catalogue picture: BMP_ id ("CatStrings" in FreeSO)
    54 level offset (a tile on an upper floor)
    62/63 first SPR2 id and number of the dynamic sprites (drawn only when the object
       turns them on: open drawer, TV picture, food on a plate)
DGRP (draw group): one image per (direction, zoom), each a list of sprites
    (SPR2 id, frame, x, y, z, flags with bit 0 = mirrored). Versions 20000-20004.
BMP_: a Windows BMP, 24-bit, 8-bit or 8-bit RLE. A catalogue picture is 74x37: the
    object on the normal background, then on the selected one, brightened.

Standard library only. Usage (inspect one object file):
    py catalog/objects.py <file.iff|archive.far> [name filter]
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import iff  # noqa: E402
import spr2  # noqa: E402
from bmp import FormatError, decode_bmp  # noqa: E402,F401  (one error for BMPs and object files)

PICTURE_SIZE = (74, 37)


def objd_fields(data: bytes, chunk: iff.IffChunk) -> dict:
    if chunk.size < 4 + 2 * 40:
        raise FormatError(f"OBJD #{chunk.id} too short")
    n = (chunk.size - 4) // 2
    v = struct.unpack_from(f"<{n}H", data, chunk.offset + 4)
    signed = v[9] - 0x10000 if v[9] >= 0x8000 else v[9]
    return dict(id=chunk.id, label=chunk.label, base=v[1], ngraphics=v[2], master=v[8], sub=signed,
                guid=v[12] | v[13] << 16, flags=v[38], catpic=v[39], level=v[54] if n > 54 else 0,
                dyn_base=v[62] if n > 63 else 0, dyn_n=v[63] if n > 63 else 0)


def read_dgrp(d: bytes) -> tuple[int, list[dict]]:
    """(version, images); image = {direction, zoom, sprites: [{spr, frame, ox, oy, z, flags}]}."""
    try:
        (version,) = struct.unpack_from("<H", d, 0)
        if not 20000 <= version <= 20004:
            raise FormatError(f"DGRP version {version}")
        old = version < 20003
        (count,) = struct.unpack_from("<H" if old else "<I", d, 2)
        pos = 4 if old else 6
        images = []
        for _ in range(count):
            if old:
                n, direction, zoom = struct.unpack_from("<HBB", d, pos)
                pos += 4
            else:
                direction, zoom, n = struct.unpack_from("<III", d, pos)
                pos += 12
            sprites = []
            for _ in range(n):
                if old:
                    _type, sid, frame, flags, ox, oy = struct.unpack_from("<HHHHhh", d, pos)
                    pos += 12
                    z = 0.0
                    if version == 20001:
                        (z,) = struct.unpack_from("<f", d, pos)
                        pos += 4
                else:
                    sid, frame, ox, oy, z, flags = struct.unpack_from("<IIiifI", d, pos)
                    pos += 24 + (8 if version == 20004 else 0)  # 20004: two more floats (world x, y)
                sprites.append(dict(spr=sid, frame=frame, ox=ox, oy=oy, z=z, flags=flags))
            images.append(dict(direction=direction, zoom=zoom, sprites=sprites))
    except struct.error as e:
        raise FormatError(f"DGRP truncated: {e}") from None
    return version, images


def to_bgra(rgba: bytes) -> bytes:
    out = bytearray(rgba)
    out[0::4], out[2::4] = rgba[2::4], rgba[0::4]
    return bytes(out)


class ObjectFile:
    """One object IFF: its OBJDs, DGRPs, SPR2s, palettes and BMP_s. Chunks that do not
    parse are left out."""

    def __init__(self, data: bytes, name: str = ""):
        self.name = name
        self.data = data
        self.chunks = iff.read_iff(data)
        self.pals = spr2.read_palettes(data, self.chunks)
        self.objds, self.dgrp, self.spr, self.bmp = [], {}, {}, {}
        for c in self.chunks:
            body = data[c.offset:c.offset + c.size]
            try:
                if c.type == "OBJD":
                    self.objds.append(objd_fields(data, c))
                elif c.type == "DGRP":
                    self.dgrp[c.id] = read_dgrp(body)[1]
            except FormatError:
                continue
            if c.type == "SPR2":
                self.spr[c.id] = body
            elif c.type == "BMP_":
                self.bmp[c.id] = body

    def picture(self, objd: dict):
        """(w, h, RGBA) of the OBJD's catalogue picture, or None."""
        body = self.bmp.get(objd["catpic"])
        if body is None:
            return None
        try:
            return decode_bmp(body)
        except FormatError:
            return None

    def frame(self, sid: int, index: int):
        """(spr2.Frame, palette, header x, header y) of one SPR2 frame, or None."""
        sd = self.spr.get(sid)
        if sd is None:
            return None
        try:
            offsets = spr2.frame_offsets(sd)
            if index >= len(offsets):
                return None
            off, spr2_pal = offsets[index]
            f = spr2.decode_frame(sd, off)
            _w, _h, _flags, _pal, _t, hy, hx = struct.unpack_from("<HHIHHHH", sd, off)
        except (spr2.Spr2Error, struct.error):
            return None
        pal = spr2.frame_palette(self.pals, f, spr2_pal)
        if pal is None or not f.w or not f.h:
            return None
        return f, pal, hx, hy


def catalogue_objects(obj: ObjectFile):
    """(catalogue OBJD, its tiles) for every OBJD with a picture. A multi-tile object is its
    lead OBJD (sub index -1) with the OBJDs sharing its master id; those tiles are not
    catalogue entries themselves even when they carry the picture id too. A single-tile
    object is its own tile."""
    groups: dict[int, list] = {}
    for o in obj.objds:
        if o["master"]:
            groups.setdefault(o["master"], []).append(o)
    for o in obj.objds:
        if not o["catpic"] or o["catpic"] not in obj.bmp:
            continue
        group = groups.get(o["master"], [o]) if o["master"] else [o]
        lead = [g for g in group if g["sub"] == -1]
        if lead and o is not lead[0]:
            continue
        tiles = [g for g in group if g["sub"] >= 0] if lead else [o]
        yield o, tiles or [o]


def picture_name(w: int, h: int, rgba: bytes) -> str:
    """vkproxy's name for a picture as the game uploads it (opaque BGRA)."""
    return spr2.content_name(w, h, to_bgra(rgba))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    want = argv[1].lower() if len(argv) > 1 else ""
    for name, data in spr2.iter_iffs(Path(argv[0])):
        if want not in name.lower():
            continue
        try:
            obj = ObjectFile(data, name)
        except iff.IffError as e:
            print(f"{name}: {e}")
            continue
        for o, tiles in catalogue_objects(obj):
            pic = obj.picture(o)
            size = f"{pic[0]}x{pic[1]}" if pic else "?"
            print(f"{name}  {o['label']!r}  OBJD #{o['id']}  picture BMP_ #{o['catpic']} {size}  "
                  f"{len(tiles)} tile(s)  {o['ngraphics']} graphic(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
