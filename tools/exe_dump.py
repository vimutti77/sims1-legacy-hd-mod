"""Dump the image of the running Sims.exe from memory, for disassembly.

Sims.exe is wrapped in SteamStub: its .text is encrypted on disk and only readable once the game
runs. This copies the whole module (SizeOfImage bytes from its base; unreadable pages are left
zero) and lists its sections, so code can be disassembled at the addresses it runs at:

    objdump -D -b binary -m i386 -M intel --adjust-vma=<base> \
            --start-address=<va> --stop-address=<va> <dump>

The base changes every run (ASLR); vkproxy logs code addresses as offsets from it
(log_game_stack), and an offset is the same in every run.

Usage:
    py tools/exe_dump.py <out.bin> [--process Sims.exe]
"""
from __future__ import annotations

import argparse
import ctypes
import struct
import sys
from ctypes import wintypes
from pathlib import Path

PAGE = 0x1000
SCN_EXECUTE = 0x20000000


def sections(image: bytes) -> list[tuple[str, int, int, int]]:
    """(name, RVA, virtual size, characteristics) of each section of a PE image as mapped."""
    if image[:2] != b"MZ":
        raise ValueError("not a PE image")
    (pe,) = struct.unpack_from("<I", image, 0x3C)
    if image[pe:pe + 4] != b"PE\0\0":
        raise ValueError("no PE header")
    count, = struct.unpack_from("<H", image, pe + 6)
    opt_size, = struct.unpack_from("<H", image, pe + 20)
    out = []
    for i in range(count):
        o = pe + 24 + opt_size + 40 * i
        name = image[o:o + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize, rva = struct.unpack_from("<II", image, o + 8)
        (flags,) = struct.unpack_from("<I", image, o + 36)
        out.append((name, rva, vsize, flags))
    return out


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD),
                ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                ("szModule", ctypes.c_char * 256), ("szExePath", ctypes.c_char * 260)]


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]


def find_module(process: str) -> tuple[int, int, int]:
    """(pid, base, size) of the main module of the running process with this file name."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(pe)
    pid = None
    ok = k32.Process32First(snap, ctypes.byref(pe))
    while ok:
        if pe.szExeFile.decode("mbcs").lower() == process.lower():
            pid = pe.th32ProcessID
            break
        ok = k32.Process32Next(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    if pid is None:
        raise SystemExit(f"{process} is not running")
    snap = k32.CreateToolhelp32Snapshot(0x8 | 0x10, pid)  # TH32CS_SNAPMODULE | SNAPMODULE32
    me = MODULEENTRY32()
    me.dwSize = ctypes.sizeof(me)
    ok = k32.Module32First(snap, ctypes.byref(me))
    while ok:
        if me.szModule.decode("mbcs").lower() == process.lower():
            k32.CloseHandle(snap)
            return pid, me.modBaseAddr, me.modBaseSize
        ok = k32.Module32Next(snap, ctypes.byref(me))
    k32.CloseHandle(snap)
    raise SystemExit(f"no module {process} in process {pid}")


def read_image(pid: int, base: int, size: int) -> bytes:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_size_t)]
    h = k32.OpenProcess(0x0010 | 0x0400, False, pid)  # PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
    if not h:
        raise SystemExit(f"cannot open process {pid} (error {ctypes.get_last_error()})")
    out = bytearray(size)
    buf = ctypes.create_string_buffer(PAGE)
    got = ctypes.c_size_t()
    for off in range(0, size, PAGE):
        n = min(PAGE, size - off)
        if k32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got)):
            out[off:off + got.value] = buf.raw[:got.value]
    k32.CloseHandle(h)
    return bytes(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Dump the image of the running Sims.exe from memory.")
    ap.add_argument("out", type=Path)
    ap.add_argument("--process", default="Sims.exe")
    args = ap.parse_args(argv)
    pid, base, size = find_module(args.process)
    image = read_image(pid, base, size)
    args.out.write_bytes(image)
    print(f"{args.process} (pid {pid}) at {base:08x}, {size} bytes -> {args.out}")
    for name, rva, vsize, flags in sections(image):
        print(f"  {name:8} {base + rva:08x}-{base + rva + vsize:08x} (+{rva:x}){' code' if flags & SCN_EXECUTE else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
