"""生成高质量应用图标：渐变底 + 抗锯齿图形 + 多层高光。

关键点：
- 4x 超采样再降采样，边缘平滑无锯齿
- 256x256 用 PNG（Windows 硬性要求，用 BMP 会被忽略导致白图标）
- 小尺寸自动简化细节，避免糊成一团
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


# ---------------------------------------------------------------- 基础绘制


class Canvas:
    """浮点 RGBA 画布，支持 alpha 混合。"""

    def __init__(self, size: int):
        self.size = size
        self.px = [[[0.0, 0.0, 0.0, 0.0] for _ in range(size)] for _ in range(size)]

    def blend(self, x: int, y: int, r: float, g: float, b: float, a: float) -> None:
        if a <= 0 or not (0 <= x < self.size and 0 <= y < self.size):
            return
        a = min(1.0, a)
        d = self.px[y][x]
        na = a + d[3] * (1 - a)
        if na <= 0:
            return
        for i, v in enumerate((r, g, b)):
            d[i] = (v * a + d[i] * d[3] * (1 - a)) / na
        d[3] = na


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix(c1, c2, t: float):
    return tuple(lerp(c1[i], c2[i], t) for i in range(3))


def rounded_rect_alpha(x: float, y: float, size: float, radius: float) -> float:
    """圆角矩形内部的覆盖率（0..1），带 1px 羽化。"""
    cx, cy = size / 2, size / 2
    dx = abs(x - cx) - (size / 2 - radius)
    dy = abs(y - cy) - (size / 2 - radius)
    if dx <= 0 and dy <= 0:
        return 1.0
    if dx <= 0:
        d = dy
    elif dy <= 0:
        d = dx
    else:
        d = math.hypot(dx, dy) - radius
    return max(0.0, min(1.0, 0.5 - d))


def disc_alpha(x: float, y: float, cx: float, cy: float, rad: float) -> float:
    d = math.hypot(x - cx, y - cy) - rad
    return max(0.0, min(1.0, 0.5 - d))


def round_cap_line(canvas: Canvas, p0, p1, width: float, color, alpha: float = 1.0) -> None:
    """圆头线段（抗锯齿）。"""
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1e-6
    half = width / 2
    minx = int(max(0, min(x0, x1) - half - 2))
    maxx = int(min(canvas.size, max(x0, x1) + half + 2))
    miny = int(max(0, min(y0, y1) - half - 2))
    maxy = int(min(canvas.size, max(y0, y1) + half + 2))
    for y in range(miny, maxy):
        for x in range(minx, maxx):
            px, py = x + 0.5, y + 0.5
            t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / (length * length)))
            nx, ny = x0 + dx * t, y0 + dy * t
            d = math.hypot(px - nx, py - ny) - half
            a = max(0.0, min(1.0, 0.5 - d)) * alpha
            if a > 0:
                canvas.blend(x, y, *color, a)


# ---------------------------------------------------------------- 两个项目的图标


def render_imdeploy(size: int):
    """ImDeploy：深色渐变底 + 蓝色神经网络。"""
    s = size
    c = Canvas(s)
    radius = s * 0.22
    pad = s * 0.02
    inner = s - pad * 2

    # 背景：从靛蓝到深色的对角渐变，带圆角
    top = (24, 34, 58)
    bottom = (9, 12, 20)
    for y in range(s):
        for x in range(s):
            a = rounded_rect_alpha(x + 0.5 - pad, y + 0.5 - pad, inner, radius)
            if a <= 0:
                continue
            t = ((x / s) * 0.35 + (y / s) * 0.65)
            col = mix(top, bottom, t)
            c.blend(x, y, *col, a)

    # 顶部柔光（一点高级感）
    for y in range(s):
        for x in range(s):
            d = math.hypot((x - s * 0.32) / s, (y - s * 0.18) / s)
            a = max(0.0, 1.0 - d * 2.4) * 0.14
            if a > 0:
                c.blend(x, y, 96, 165, 250, a)

    # 神经网络节点
    cv = s / 2
    nodes = [
        (s * 0.27, s * 0.31, s * 0.085, (96, 165, 250), 1.0),   # 左上 蓝
        (s * 0.73, s * 0.31, s * 0.062, (125, 211, 252), 0.85),  # 右上 浅蓝
        (s * 0.73, s * 0.71, s * 0.078, (52, 211, 153), 1.0),    # 右下 青绿
        (s * 0.29, s * 0.72, s * 0.055, (148, 163, 184), 0.7),   # 左下 灰
    ]
    center = (cv, cv, s * 0.098)

    # 连线（先画线再画点，点盖住线头）
    link_w = max(1.2, s * 0.016)
    for nx, ny, _, col, al in nodes:
        round_cap_line(c, (cv, cv), (nx, ny), link_w, col, 0.45 * al)

    # 节点光晕
    if s >= 48:
        for nx, ny, nr, col, al in nodes:
            for gx in range(max(0, int(nx - nr * 3)), min(s, int(nx + nr * 3))):
                for gy in range(max(0, int(ny - nr * 3)), min(s, int(ny + nr * 3))):
                    d = math.hypot(gx + 0.5 - nx, gy + 0.5 - ny)
                    a = max(0.0, 1.0 - d / (nr * 3)) ** 2 * 0.35 * al
                    c.blend(gx, gy, *col, a)

    # 节点本体
    for nx, ny, nr, col, al in nodes:
        for gy in range(max(0, int(ny - nr - 2)), min(s, int(ny + nr + 2))):
            for gx in range(max(0, int(nx - nr - 2)), min(s, int(nx + nr + 2))):
                a = disc_alpha(gx + 0.5, gy + 0.5, nx, ny, nr) * al
                if a > 0:
                    c.blend(gx, gy, *col, a)

    # 中心节点（最亮，带外圈）
    cx, cy, cr = center
    for gy in range(max(0, int(cy - cr * 2)), min(s, int(cy + cr * 2))):
        for gx in range(max(0, int(cx - cr * 2)), min(s, int(cx + cr * 2))):
            d = math.hypot(gx + 0.5 - cx, gy + 0.5 - cy)
            outer = max(0.0, 1.0 - max(0.0, d - cr) / cr) * 0.5
            if outer > 0:
                c.blend(gx, gy, 147, 197, 253, outer)
    for gy in range(max(0, int(cy - cr - 2)), min(s, int(cy + cr + 2))):
        for gx in range(max(0, int(cx - cr - 2)), min(s, int(cx + cr + 2))):
            a = disc_alpha(gx + 0.5, gy + 0.5, cx, cy, cr)
            if a > 0:
                c.blend(gx, gy, 240, 249, 255, a)
    # 中心高光
    for gy in range(max(0, int(cy - cr)), min(s, int(cy + cr))):
        for gx in range(max(0, int(cx - cr)), min(s, int(cx + cr))):
            d = math.hypot(gx + 0.5 - (cx - cr * 0.3), gy + 0.5 - (cy - cr * 0.3))
            a = max(0.0, 1.0 - d / (cr * 0.9)) * 0.85
            if a > 0:
                c.blend(gx, gy, 255, 255, 255, a)

    return c


def render_fish(size: int):
    """feiyu-qqbot：渐变底 + 圆润的青色小鱼。"""
    s = size
    c = Canvas(s)
    radius = s * 0.22
    pad = s * 0.02
    inner = s - pad * 2

    top = (18, 44, 66)
    bottom = (8, 14, 24)
    for y in range(s):
        for x in range(s):
            a = rounded_rect_alpha(x + 0.5 - pad, y + 0.5 - pad, inner, radius)
            if a <= 0:
                continue
            t = (x / s) * 0.4 + (y / s) * 0.6
            c.blend(x, y, *mix(top, bottom, t), a)

    # 水面柔光
    for y in range(s):
        for x in range(s):
            d = math.hypot((x - s * 0.5) / s, (y - s * 0.12) / s)
            a = max(0.0, 1.0 - d * 2.2) * 0.16
            if a > 0:
                c.blend(x, y, 56, 189, 248, a)

    # 鱼身
    cx, cy = s * 0.55, s * 0.52
    rx, ry = s * 0.30, s * 0.235
    body_col = (56, 189, 248)
    belly_col = (186, 230, 253)

    def in_ellipse(x, y, cx, cy, rx, ry):
        return ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2

    # 尾巴（圆角三角）
    tail_pts = [(cx - rx * 0.78, cy),
                (cx - rx * 2.0, cy - ry * 1.05),
                (cx - rx * 2.0, cy + ry * 1.05)]
    for gy in range(max(0, int(cy - ry * 1.4)), min(s, int(cy + ry * 1.4))):
        for gx in range(max(0, int(cx - rx * 2.2)), min(s, int(cx - rx * 0.5))):
            px, py = gx + 0.5, gy + 0.5

            def sign(a, b, cc):
                return (a[0] - cc[0]) * (b[1] - cc[1]) - (b[0] - cc[0]) * (a[1] - cc[1])

            e = [sign((px, py), tail_pts[i], tail_pts[(i + 1) % 3]) for i in range(3)]
            if all(v <= 0 for v in e) or all(v >= 0 for v in e):
                t = max(0.0, min(1.0, (px - (cx - rx * 2.0)) / (rx * 1.2)))
                c.blend(gx, gy, *mix((44, 160, 210), (56, 189, 248), t), 0.92)

    # 背鳍
    fin_pts = [(cx - rx * 0.15, cy - ry * 0.80),
               (cx + rx * 0.42, cy - ry * 0.80),
               (cx + rx * 0.10, cy - ry * 1.55)]
    for gy in range(max(0, int(cy - ry * 1.7)), min(s, int(cy - ry * 0.6))):
        for gx in range(max(0, int(cx - rx * 0.4)), min(s, int(cx + rx * 0.6))):
            px, py = gx + 0.5, gy + 0.5

            def sign(a, b, cc):
                return (a[0] - cc[0]) * (b[1] - cc[1]) - (b[0] - cc[0]) * (a[1] - cc[1])

            e = [sign((px, py), fin_pts[i], fin_pts[(i + 1) % 3]) for i in range(3)]
            if all(v <= 0 for v in e) or all(v >= 0 for v in e):
                c.blend(gx, gy, 34, 140, 190, 0.9)

    # 鱼身（径向渐变）
    for gy in range(max(0, int(cy - ry * 1.2)), min(s, int(cy + ry * 1.2))):
        for gx in range(max(0, int(cx - rx * 1.3)), min(s, int(cx + rx * 1.3))):
            px, py = gx + 0.5, gy + 0.5
            r = in_ellipse(px, py, cx, cy, rx, ry)
            if r > 1.0:
                # 边缘抗锯齿
                a = max(0.0, min(1.0, (1.0 - r) * 0.5 * max(rx, ry)))
                if a <= 0:
                    continue
            else:
                a = 1.0
            # 上浅下深的渐变
            t = max(0.0, min(1.0, (py - (cy - ry)) / (2 * ry)))
            col = mix((125, 211, 252), belly_col, t)
            # 左侧一点暗部增加体积感
            shade = max(0.0, 1.0 - (px - (cx - rx)) / (rx * 1.1)) * 0.18
            col = mix(col, (12, 74, 110), shade)
            c.blend(gx, gy, *col, a)

    # 眼睛
    ex, ey = cx + rx * 0.40, cy - ry * 0.20
    er = max(1.0, s * 0.052)
    for gy in range(max(0, int(ey - er * 2)), min(s, int(ey + er * 2))):
        for gx in range(max(0, int(ex - er * 2)), min(s, int(ex + er * 2))):
            a = disc_alpha(gx + 0.5, gy + 0.5, ex, ey, er)
            if a > 0:
                c.blend(gx, gy, 255, 255, 255, a)
    pr = er * 0.55
    for gy in range(max(0, int(ey - er)), min(s, int(ey + er))):
        for gx in range(max(0, int(ex - er)), min(s, int(ex + er))):
            a = disc_alpha(gx + 0.5, gy + 0.5, ex + er * 0.15, ey, pr)
            if a > 0:
                c.blend(gx, gy, 8, 20, 32, a)
    # 眼神光
    if s >= 32:
        a = disc_alpha(ex - pr * 0.3, ey - pr * 0.35, 0, 0, pr * 0.32) if False else 0
        for gy in range(max(0, int(ey - er)), min(s, int(ey + er))):
            for gx in range(max(0, int(ex - er)), min(s, int(ex + er))):
                aa = disc_alpha(gx + 0.5, gy + 0.5, ex - pr * 0.25, ey - pr * 0.3, pr * 0.34)
                if aa > 0:
                    c.blend(gx, gy, 255, 255, 255, aa)

    # 嘴（小弧线）
    if s >= 48:
        round_cap_line(c, (cx + rx * 0.86, cy + ry * 0.18),
                       (cx + rx * 0.96, cy + ry * 0.30), max(1.0, s * 0.014),
                       (10, 60, 90), 0.75)

    # 气泡
    if s >= 48:
        for bx, by, br in ((cx + rx * 1.12, cy - ry * 0.85, s * 0.032),
                           (cx + rx * 1.32, cy - ry * 1.25, s * 0.022)):
            for gy in range(max(0, int(by - br * 2)), min(s, int(by + br * 2))):
                for gx in range(max(0, int(bx - br * 2)), min(s, int(bx + br * 2))):
                    a = disc_alpha(gx + 0.5, gy + 0.5, bx, by, br) * 0.55
                    if a > 0:
                        c.blend(gx, gy, 224, 242, 254, a)

    return c


# ---------------------------------------------------------------- 降采样与编码


def downsample(canvas: Canvas, target: int):
    """4x 超采样：把大画布按块平均成目标尺寸（保留 alpha）。"""
    src = canvas.size
    factor = max(1, src // target)
    out = [[(0, 0, 0, 0) for _ in range(target)] for _ in range(target)]
    for ty in range(target):
        for tx in range(target):
            r = g = b = a = 0.0
            for dy in range(factor):
                for dx in range(factor):
                    sy = ty * factor + dy
                    sx = tx * factor + dx
                    if sy >= src or sx >= src:
                        continue
                    p = canvas.px[sy][sx]
                    pa = p[3]
                    r += p[0] * pa
                    g += p[1] * pa
                    b += p[2] * pa
                    a += pa
            n = float(factor * factor)
            if a > 1e-6:
                col = (int(round(r / a)), int(round(g / a)), int(round(b / a)))
            else:
                col = (0, 0, 0)
            alpha = int(round(min(1.0, a / n) * 255))
            out[ty][tx] = (col[0], col[1], col[2], alpha)
    return out


def png_bytes(px, size):
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            r, g, b, a = px[y][x]
            raw += bytes((r, g, b, a))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def bmp_bytes(px, size):
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray()
        for x in range(size):
            r, g, b, a = px[y][x]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    return header + b"".join(rows)


def build_ico(path: Path, renderer, sizes=(16, 24, 32, 48, 64, 128, 256)) -> Path:
    images = []
    for s in sizes:
        big = renderer(s * 4)          # 4x 超采样
        small = downsample(big, s)
        data = png_bytes(small, s) if s >= 256 else bmp_bytes(small, s)
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
