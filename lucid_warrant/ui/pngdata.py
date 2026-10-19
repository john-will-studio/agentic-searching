"""Minimal PNG reading and writing for 8-bit RGBA images (Sun Valley's sprite sheets).

Used to recolor sprites without Tk 8.7+/9's RGBA photo-data API, so the same code works on
Tk 8.6 (common on Windows) and Tk 9.
"""

from __future__ import annotations

import struct
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def read_rgba(data: bytes) -> tuple[int, int, list[bytearray]]:
    """(width, height, rows), each row being width*4 bytes of RGBA. Only 8-bit RGBA, non-interlaced."""
    if not data.startswith(SIGNATURE):
        raise ValueError("not a PNG file")
    pos, idat, header = len(SIGNATURE), bytearray(), None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if header is None:
        raise ValueError("PNG has no header")
    width, height, depth, color_type, _compression, _filter, interlace = header
    if depth != 8 or color_type != 6 or interlace:
        raise ValueError("only 8-bit RGBA, non-interlaced PNGs are supported")
    raw, stride, bpp = zlib.decompress(bytes(idat)), width * 4, 4
    rows, previous = [], bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, row = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = row[i - bpp] if i >= bpp else 0
            up = previous[i]
            if kind == 1:
                row[i] = (row[i] + left) & 0xFF
            elif kind == 2:
                row[i] = (row[i] + up) & 0xFF
            elif kind == 3:
                row[i] = (row[i] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                upper_left = previous[i - bpp] if i >= bpp else 0
                p = left + up - upper_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - upper_left)
                row[i] = (row[i] + (left if pa <= pb and pa <= pc else up if pb <= pc else upper_left)) & 0xFF
            elif kind != 0:
                raise ValueError(f"unknown PNG filter {kind}")
        rows.append(row)
        previous = row
    return width, height, rows


def write_rgba(width: int, height: int, rows: list[bytearray]) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    return (SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
