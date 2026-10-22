"""生成 feiyu-qqbot 应用图标（纯 Python，无需 Pillow）。

一条胖胖的青色小鱼 + 深色圆角底，呼应「大肥鱼」人格。
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "packaging" / "app.ico"

BG = (13, 17, 23)         # #0d1117
FISH = (88, 200, 255)     # 青色鱼身
BELLY = (150, 225, 255)   # 亮色腹部
EYE = (255, 255, 255)     # 眼白
PUPIL = (13, 17, 23)      # 瞳孔
FIN = (63, 185, 200)      # 鱼鳍


def rounded_rect(px, size, radius, color):
    for y in range(size):
        for x in range(size):
            dx = min(x, size - 1 - x)
            dy = min(y, size - 1 - y)
            if dx >= radius or dy >= radius:
                px[y][x] = color
            else:
                cx, cy = radius - dx, radius - dy
                if cx * cx + cy * cy <= radius * radius:
                    px[y][x] = color


def ellipse(px, size, cx, cy, rx, ry, color):
    for y in range(max(0, int(cy - ry) - 1), min(size, int(cy + ry) + 2)):
        for x in range(max(0, int(cx - rx) - 1), min(size, int(cx + rx) + 2)):
            if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0:
                px[y][x] = color


def triangle(px, size, p1, p2, p3, color):
    def sign(a, b, c):
        return (a[0] - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (a[1] - c[1])

    minx = max(0, int(min(p[0] for p in (p1, p2, p3))) - 1)
    maxx = min(size, int(max(p[0] for p in (p1, p2, p3))) + 2)
    miny = max(0, int(min(p[1] for p in (p1, p2, p3))) - 1)
    maxy = min(size, int(max(p[1] for p in (p1, p2, p3))) + 2)
    for y in range(miny, maxy):
        for x in range(minx, maxx):
            d1 = sign((x, y), p1, p2)
            d2 = sign((x, y), p2, p3)
            d3 = sign((x, y), p3, p1)
            neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
            pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
            if not (neg and pos):
                px[y][x] = color


def render(size: int):
    px = [[BG for _ in range(size)] for _ in range(size)]
    s = size
    rounded_rect(px, s, max(2, s // 8), BG)

    cx, cy = s * 0.54, s * 0.50
    rx, ry = s * 0.30, s * 0.24

    # 尾巴（左侧三角）
    triangle(px, s, (cx - rx * 0.85, cy),
             (cx - rx * 1.85, cy - ry * 0.95),
             (cx - rx * 1.85, cy + ry * 0.95), FIN)

    # 鱼身
    ellipse(px, s, cx, cy, rx, ry, FISH)
    # 腹部亮色（下半部分稍亮）
    ellipse(px, s, cx, cy + ry * 0.35, rx * 0.78, ry * 0.52, BELLY)

    # 背鳍
    triangle(px, s, (cx - rx * 0.1, cy - ry * 0.82),
             (cx + rx * 0.35, cy - ry * 0.82),
             (cx + rx * 0.05, cy - ry * 1.45), FIN)

    # 眼睛
    ex, ey = cx + rx * 0.42, cy - ry * 0.18
    er = max(1.2, s * 0.055)
    ellipse(px, s, ex, ey, er, er, EYE)
    ellipse(px, s, ex + er * 0.25, ey, er * 0.5, er * 0.5, PUPIL)

    return px


def to_bmp(px, size):
    """32 位 BGRA 的 DIB（自下而上，无文件头）。"""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray()
        for x in range(size):
            r, g, b = px[y][x]
            row += bytes((b, g, r, 255))
        rows.append(bytes(row))
    return header + b"".join(rows)


def to_png(px, size):
    """PNG 编码（256x256 必须是 PNG，BMP 会被 Windows 忽略导致白图标）。"""
    import zlib

    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            r, g, b = px[y][x]
            raw += bytes((r, g, b, 255))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def write_ico(path: Path, sizes=(16, 24, 32, 48, 64, 128, 256)) -> Path:
    images = []
    for s in sizes:
        px = render(s)
        data = to_png(px, s) if s >= 256 else to_bmp(px, s)
        images.append((s, data))

    offset = 6 + 16 * len(images)
    entries, blobs = [], []
    for s, data in images:
        entries.append(struct.pack("<BBBBHHII",
                                   0 if s >= 256 else s,
                                   0 if s >= 256 else s,
                                   0, 0, 1, 32, len(data), offset))
        blobs.append(data)
        offset += len(data)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(images)))
        for e in entries:
            f.write(e)
        for b in blobs:
            f.write(b)
    return path


if __name__ == "__main__":
    p = write_ico(OUT)
    print(f"已生成图标: {p}  ({p.stat().st_size} 字节)")
