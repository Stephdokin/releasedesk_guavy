#!/usr/bin/env python3
"""
Reads the dimensions of a photo or video without ffmpeg.

The desk never re-encodes anything, so what you upload is exactly what Zernio
receives. That makes the source file the only thing that decides quality, and
this is how the desk shows you what the source actually is.

  python3 probe.py media/.../clip.MOV
"""

import struct, sys
from pathlib import Path


def _atoms(buf, start, end):
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", buf[i:i + 4])[0]
        typ = buf[i + 4:i + 8]
        body = i + 8
        if size == 1:
            if i + 16 > end:
                return
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            body = i + 16
        elif size == 0:
            size = end - i
        if size < 8 or i + size > end:
            return
        yield typ, body, i + size
        if typ in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
            yield from _atoms(buf, body, i + size)
        i += size


def mp4(data):
    """Largest visual track wins, which skips the metadata tracks."""
    w = h = 0
    secs = None
    for typ, body, end in _atoms(data, 0, len(data)):
        if typ == b"mvhd" and secs is None:
            ver = data[body]
            if ver == 0 and body + 20 <= end:
                scale, dur = struct.unpack(">II", data[body + 12:body + 20])
            elif body + 28 <= end:
                scale, dur = struct.unpack(">IQ", data[body + 20:body + 32])
            else:
                continue
            if scale:
                secs = round(dur / scale, 1)
        elif typ == b"tkhd" and end - 8 >= body:
            tw = struct.unpack(">I", data[end - 8:end - 4])[0] / 65536.0
            th = struct.unpack(">I", data[end - 4:end])[0] / 65536.0
            # A phone shooting vertically stores the frame landscape and sets a
            # rotation in the track matrix. The stored size is not the size it
            # plays at, so honour the matrix or every phone video reads wrong.
            if end - 44 >= body:
                m = struct.unpack(">9i", data[end - 44:end - 8])
                b, c = m[1] / 65536.0, m[3] / 65536.0
                if abs(b) == 1 and abs(c) == 1:      # 90 or 270 degrees
                    tw, th = th, tw
            if tw * th > w * h:
                w, h = int(tw), int(th)
    return (w or None), (h or None), secs


def image(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return w, h, None
    if data[:3] == b"\xff\xd8\xff":                     # JPEG
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            m = data[i + 1]
            if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            ln = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h, None
            i += 2 + ln
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return w, h, None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return w, h, None
        if data[12:16] == b"VP8 ":
            w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return w, h, None
    return None, None, None


def probe(path):
    """Returns (width, height, seconds). Any part may be None."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None, None, None
    try:
        if data[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide"):
            return mp4(data)
        return image(data)
    except (struct.error, IndexError, ValueError):
        return None, None, None


if __name__ == "__main__":
    for p in sys.argv[1:]:
        w, h, s = probe(p)
        print(f"{p}: {w}x{h}" + (f", {s}s" if s else ""))
