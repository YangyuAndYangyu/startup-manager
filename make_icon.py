# -*- coding: utf-8 -*-
"""生成程序的图标 egg.ico —— 纯 Python，不依赖任何第三方库。

画一个「蓝底 + 白色鸡蛋」的圆角图标，4x 超采样抗锯齿。
"""
import struct
from pathlib import Path

SIZE = 64          # 图标边长
SS = 4             # 每像素 4x4 子采样
MARGIN = 6.0       # 圆角方形留白
RADIUS = 16.0      # 圆角半径

CX, CY = SIZE / 2, 33.0
EGG_RX, EGG_RY = 15.0, 19.0


def lerp(a, b, t):
    return a + (b - a) * t


def inside_round_rect(px, py):
    x0, y0, x1, y1 = MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    dx = max(x0 + RADIUS - px, px - (x1 - RADIUS), 0.0)
    dy = max(y0 + RADIUS - py, py - (y1 - RADIUS), 0.0)
    return dx * dx + dy * dy <= RADIUS * RADIUS


def inside_egg(u, v):
    """u: -1..1 横向，v: -1(尖顶)..1(圆底)。"""
    s = 1.0 - 0.42 * max(0.0, -v)          # 顶部收窄
    return (u / s) ** 2 + v ** 2 <= 1.0


def pixel_color(x, y):
    """采样一个像素，返回 (r, g, b, a)。"""
    r_sum = g_sum = b_sum = a_sum = 0.0
    for i in range(SS):
        for j in range(SS):
            px = x + (i + 0.5) / SS
            py = y + (j + 0.5) / SS
            if not inside_round_rect(px, py):
                continue
            a = 255
            t = py / SIZE
            r, g, b = lerp(37, 26, t), lerp(99, 70, t), lerp(235, 210, t)
            u = (px - CX) / EGG_RX
            v = (py - CY) / EGG_RY
            if inside_egg(u, v):
                shade = 1.0 - 0.10 * max(0.0, v)
                hl = max(0.0, 0.45 - ((u + 0.35) ** 2 + (v + 0.42) ** 2) * 2.6)
                r, g, b = 255 * shade, 252 * shade, 240 * shade
                r = min(255, r + 110 * hl)
                g = min(255, g + 100 * hl)
                b = min(255, b + 85 * hl)
            # 底部微微压暗，像站在盒子里
            sy = max(0.0, min(1.0, (py - (CY + 0.6 * EGG_RY)) / (SIZE - CY)))
            r, g, b = r * (1 - 0.16 * sy), g * (1 - 0.16 * sy), b * (1 - 0.16 * sy)
            r_sum += r
            g_sum += g
            b_sum += b
            a_sum += a
    if a_sum == 0:
        return (0, 0, 0, 0)
    n = SS * SS
    return (int(r_sum / n), int(g_sum / n), int(b_sum / n), int(a_sum / n))


def build_ico() -> bytes:
    # 像素按 ICO 要求：从上到下每行都是底朝上（BMP 顺序），这里先算好再倒序
    rows = []
    for y in range(SIZE):
        row = bytearray()
        for x in range(SIZE):
            r, g, b, a = pixel_color(x, y)
            row += bytes((b, g, r, a))          # BGRA
        rows.append(bytes(row))
    pixels = b"".join(reversed(rows))           # ICO 里图像是 bottom-up

    header_size = 40
    xor_size = SIZE * SIZE * 4
    mask_stride = ((SIZE + 31) // 32) * 4
    and_mask = b"\x00" * (mask_stride * SIZE)

    icon_dir = struct.pack("<HHH", 0, 1, 1)     # reserved, type=icon, count
    entry = struct.pack("<BBBBHHII", SIZE, SIZE, 0, 0, 1, 32,
                        header_size + xor_size + len(and_mask), 22)
    bmp = struct.pack("<IiiHHIIiiII", header_size, SIZE, SIZE * 2, 1, 32, 0,
                      xor_size, 0, 0, 0, 0)
    return icon_dir + entry + bmp + pixels + and_mask


def main():
    icon = build_ico()
    out = Path(__file__).parent / "egg.ico"
    out.write_bytes(icon)
    print(f"已生成 {out}（{len(icon)} 字节）")


if __name__ == "__main__":
    main()