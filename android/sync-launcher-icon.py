#!/usr/bin/env python3
"""Sync Android launcher mipmaps from the CamUI web icon.

No external image dependency is used; this supports the repository's 8-bit RGBA,
non-interlaced PNG icon assets.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "web" / "assets" / "icon-192.png"
RES = ROOT / "android" / "app" / "src" / "main" / "res"
TARGETS = {
    "mipmap-hdpi/ic_launcher.png": 72,
    "mipmap-xhdpi/ic_launcher.png": 96,
    "mipmap-xxhdpi/ic_launcher.png": 144,
    "mipmap-xxxhdpi/ic_launcher.png": 192,
}
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def read_png_rgba(path: Path):
    data = path.read_bytes()
    if not data.startswith(PNG_SIG):
        raise SystemExit(f"{path} is not a PNG")
    pos = 8
    width = height = bit_depth = color_type = interlace = None
    chunks = []
    while pos < len(data):
        if pos + 8 > len(data):
            raise SystemExit(f"{path} has a truncated PNG chunk")
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        cdata = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _comp, _filter, interlace = struct.unpack(">IIBBBBB", cdata)
        elif ctype == b"IDAT":
            chunks.append(cdata)
        elif ctype == b"IEND":
            break
    if width is None:
        raise SystemExit(f"{path} missing IHDR")
    if bit_depth != 8 or color_type != 6 or interlace != 0:
        raise SystemExit(f"{path} must be 8-bit RGBA non-interlaced PNG")
    raw = zlib.decompress(b"".join(chunks))
    stride = width * 4
    rows = []
    prev = bytearray(stride)
    i = 0
    for _y in range(height):
        f = raw[i]
        i += 1
        row = bytearray(raw[i:i + stride])
        i += stride
        for x in range(stride):
            left = row[x - 4] if x >= 4 else 0
            up = prev[x]
            up_left = prev[x - 4] if x >= 4 else 0
            if f == 1:
                row[x] = (row[x] + left) & 0xff
            elif f == 2:
                row[x] = (row[x] + up) & 0xff
            elif f == 3:
                row[x] = (row[x] + ((left + up) >> 1)) & 0xff
            elif f == 4:
                p = left + up - up_left
                pa = abs(p - left)
                pb = abs(p - up)
                pc = abs(p - up_left)
                pr = left if pa <= pb and pa <= pc else (up if pb <= pc else up_left)
                row[x] = (row[x] + pr) & 0xff
            elif f != 0:
                raise SystemExit(f"unsupported PNG filter {f}")
        rows.append(bytes(row))
        prev = row
    return width, height, rows


def resize_bilinear(rows, src_w: int, src_h: int, dst: int):
    if src_w == dst and src_h == dst:
        return rows
    out = []
    for y in range(dst):
        gy = (y + 0.5) * src_h / dst - 0.5
        y0 = max(0, min(src_h - 1, int(gy)))
        y1 = max(0, min(src_h - 1, y0 + 1))
        wy = max(0.0, min(1.0, gy - y0))
        row = bytearray(dst * 4)
        for x in range(dst):
            gx = (x + 0.5) * src_w / dst - 0.5
            x0 = max(0, min(src_w - 1, int(gx)))
            x1 = max(0, min(src_w - 1, x0 + 1))
            wx = max(0.0, min(1.0, gx - x0))
            i00, i10, i01, i11 = x0 * 4, x1 * 4, x0 * 4, x1 * 4
            r0, r1 = rows[y0], rows[y1]
            for c in range(4):
                top = r0[i00 + c] * (1 - wx) + r0[i10 + c] * wx
                bot = r1[i01 + c] * (1 - wx) + r1[i11 + c] * wx
                row[x * 4 + c] = int(top * (1 - wy) + bot * wy + 0.5)
        out.append(bytes(row))
    return out


def png_chunk(ctype: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xffffffff)


def write_png_rgba(path: Path, size: int, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    data = PNG_SIG + png_chunk(b"IHDR", ihdr) + png_chunk(b"IDAT", zlib.compress(raw, 9)) + png_chunk(b"IEND", b"")
    path.write_bytes(data)


def main():
    src_w, src_h, rows = read_png_rgba(SRC)
    if src_w != src_h:
        raise SystemExit(f"{SRC} must be square")
    for rel, size in TARGETS.items():
        dst = RES / rel
        if size == src_w == src_h:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(SRC.read_bytes())
        else:
            write_png_rgba(dst, size, resize_bilinear(rows, src_w, src_h, size))
        print(f"synced {dst.relative_to(ROOT)} ({size}x{size})")


if __name__ == "__main__":
    main()
