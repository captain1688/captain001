"""互动符号贴图：红＋、红心、金星、666、分享箭头。

纯 Python 绘制成带透明通道的 PNG（不依赖 Pillow），带白色描边和柔和阴影，任何背景上都看得清。
生成好的图片已放在 video-pipeline/assets/symbols/，正常使用不需要重新生成；
想换成自己的设计，把同名 PNG 放进 overlay.assets_dir 指定的目录即可。
重新生成：python -m vpipe.symbols   （在 video-pipeline 目录下运行）
"""

import math
import struct
import zlib
from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "symbols"
RED = (232, 38, 52)
GOLD = (255, 190, 25)
ORANGE = (255, 112, 30)
BLUE = (36, 128, 255)


def _rrect(x, y, cx, cy, hw, hh, r):
    dx, dy = max(abs(x - cx) - hw + r, 0), max(abs(y - cy) - hh + r, 0)
    return dx * dx + dy * dy <= r * r


def _poly(x, y, pts):
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _capsule(x, y, ax, ay, bx, by, r):
    vx, vy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / (vx * vx + vy * vy)))
    px, py = ax + t * vx - x, ay + t * vy - y
    return px * px + py * py <= r * r


def plus(x, y):
    return _rrect(x, y, 0, 0, 0.78, 0.22, 0.12) or _rrect(x, y, 0, 0, 0.22, 0.78, 0.12)


def heart(x, y):
    u, v = x * 1.18, -y * 1.18 + 0.12
    return (u * u + v * v - 1) ** 3 - u * u * v ** 3 <= 0


_STAR = [(0.86 * math.cos(math.radians(-90 + i * 36)) * (1 if i % 2 == 0 else 0.42),
          0.86 * math.sin(math.radians(-90 + i * 36)) * (1 if i % 2 == 0 else 0.42) + 0.06) for i in range(10)]


def star(x, y):
    return _poly(x, y, _STAR)


def _six(x, y, cx, g=0.0):
    """一个圆润的数字 6：下方圆环 + 左上弧形笔画。g 为向外加粗量（用来画描边，不改变位置）。"""
    d = math.hypot((x - cx) * 1.05, y - 0.22)
    ring = 0.17 - g <= d <= 0.47 + g
    stem = (_capsule(x, y, cx + 0.28, -0.64, cx - 0.17, -0.42, 0.13 + g)
            or _capsule(x, y, cx - 0.17, -0.42, cx - 0.38, 0.1, 0.13 + g))
    return ring or stem


def six66(x, y, g=0.0):
    # 宽画布：x 范围 [-2.4, 2.4]，三个 6 并排
    return _six(x, y, -1.5, g) or _six(x, y, 0.0, g) or _six(x, y, 1.5, g)


def share(x, y):
    head = _poly(x, y, [(0.0, -0.86), (0.66, -0.12), (0.24, -0.12), (0.24, 0.0), (-0.24, 0.0), (-0.24, -0.12), (-0.66, -0.12)])
    shaft = _rrect(x, y, 0, 0.32, 0.24, 0.48, 0.1)
    return head or shaft


SPECS = {
    # 名称: (形状函数, 填充色, 画布宽, 画布高, x 方向半宽)
    "plus": (plus, RED, 200, 200, 1.0),
    "heart": (heart, RED, 200, 200, 1.0),
    "star": (star, GOLD, 200, 200, 1.0),
    "666": (six66, ORANGE, 360, 150, 2.4),
    "share": (share, BLUE, 200, 200, 1.0),
}


def render(name, ss=3):
    fn, color, w, h, xr = SPECS[name]
    grow = name == "666"
    yr = 1.0 * (h / w) * (xr / 1.0) if w != h else 1.0
    pad = 1.22   # 给描边和阴影留边
    px = []
    for j in range(h):
        row = bytearray()
        for i in range(w):
            fill = edge = shadow = 0
            for sj in range(ss):
                for si in range(ss):
                    x = ((i + (si + 0.5) / ss) / w * 2 - 1) * xr * pad
                    y = ((j + (sj + 0.5) / ss) / h * 2 - 1) * yr * pad
                    if fn(x, y):
                        fill += 1
                    if grow:   # 描边：笔画原地加粗（多字符图形不能按中心缩放）
                        e_in, s_in = fn(x, y, 0.09), fn(x, y - 0.08, 0.09)
                    else:      # 单个图形：围绕中心放大
                        e_in, s_in = fn(x / 1.14, y / 1.14), fn(x / 1.14, (y - 0.08) / 1.14)
                    edge += e_in
                    shadow += s_in
            n = ss * ss
            fa, ea, sa = fill / n, edge / n, shadow / n * 0.35
            # 由下往上叠：阴影（黑）→ 白色描边 → 彩色填充
            a = sa
            r = g = b = 0.0
            a2 = ea + a * (1 - ea)
            if a2 > 0:
                r = (255 * ea + r * a * (1 - ea)) / a2
                g = (255 * ea + g * a * (1 - ea)) / a2
                b = (255 * ea + b * a * (1 - ea)) / a2
            a = a2
            a3 = fa + a * (1 - fa)
            if a3 > 0:
                r = (color[0] * fa + r * a * (1 - fa)) / a3
                g = (color[1] * fa + g * a * (1 - fa)) / a3
                b = (color[2] * fa + b * a * (1 - fa)) / a3
            row += bytes((int(r + 0.5), int(g + 0.5), int(b + 0.5), int(a3 * 255 + 0.5)))
        px.append(bytes(row))
    return w, h, px


def write_png(path, w, h, rows):
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + r for r in rows)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    Path(path).write_bytes(data)


def asset_path(name, custom_dir=None):
    if custom_dir:
        p = Path(custom_dir) / f"{name}.png"
        if p.exists():
            return p
    p = ASSET_DIR / f"{name}.png"
    if not p.exists():
        ASSET_DIR.mkdir(parents=True, exist_ok=True)
        write_png(p, *render(name))
    return p


if __name__ == "__main__":
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    for name in SPECS:
        write_png(ASSET_DIR / f"{name}.png", *render(name))
        print("wrote", ASSET_DIR / f"{name}.png")
