"""Generates src/spindle.ico (a turntable platter with an object on it).

Pure Python, no dependencies: rasterises with 4x4 supersampling and writes a
multi-size ICO of 32-bit BMP images. Run: python tools/make_icon.py
"""
import math
import os
import struct

SIZES = [16, 24, 32, 48, 64, 128]


def coverage(px, py, s):
    """RGBA (0..1) of the icon at normalised point (px, py), y down."""
    x, y = px * 2 - 1, py * 2 - 1
    # Rounded-square tile.
    r = 0.30
    qx, qy = max(abs(x) - (1 - r), 0), max(abs(y) - (1 - r), 0)
    if math.hypot(qx, qy) > r:
        return None
    col = (0.13, 0.15, 0.19)
    # Platter: an ellipse seen from above at an angle, with a rim.
    ex, ey = x / 0.78, (y - 0.42) / 0.26
    d = ex * ex + ey * ey
    if d < 1.0:
        col = (0.82, 0.84, 0.88) if ey < 0.15 else (0.55, 0.57, 0.62)
    # The object: an orange vase silhouette standing on the platter.
    t = (y + 0.62) / 1.04  # 0 at top, 1 at platter
    if 0 <= t <= 1:
        w = 0.20 + 0.18 * math.sin(math.pi * min(1, t * 1.15)) - 0.05 * t
        if abs(x) < w:
            shade = 0.75 + 0.25 * (1 - abs(x) / w) - 0.2 * (x > 0.05)
            col = (0.95 * shade, 0.45 * shade, 0.12 * shade)
    # Rotation arrow arc around the platter.
    ax, ay = x / 0.92, (y - 0.42) / 0.36
    ad = math.hypot(ax, ay)
    ang = math.atan2(ay, ax)
    if abs(ad - 1.0) < 0.09 and 0.25 < ang < 2.6:
        col = (0.36, 0.72, 1.0)
    return col + (1.0,)


def render(s):
    pixels = []
    n = 4
    for j in range(s):
        row = []
        for i in range(s):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sj in range(n):
                for si in range(n):
                    c = coverage((i + (si + 0.5) / n) / s, (j + (sj + 0.5) / n) / s, s)
                    if c:
                        for k in range(3):
                            acc[k] += c[k]
                        acc[3] += 1
            a = acc[3] / (n * n)
            if acc[3] > 0:
                rgb = [acc[k] / acc[3] for k in range(3)]
            else:
                rgb = [0, 0, 0]
            row.append((rgb, a))
        pixels.append(row)
    return pixels


def bmp_entry(s):
    px = render(s)
    header = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    data = bytearray()
    for j in reversed(range(s)):  # bottom-up
        for i in range(s):
            (r, g, b), a = px[j][i]
            data += bytes([int(b * 255 + 0.5), int(g * 255 + 0.5), int(r * 255 + 0.5), int(a * 255 + 0.5)])
    mask_row = ((s + 31) // 32) * 4
    data += bytes(mask_row * s)  # AND mask unused with 32-bit alpha
    return header + bytes(data)


def main():
    images = [bmp_entry(s) for s in SIZES]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, img in zip(SIZES, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    for img in images:
        out += img
    path = os.path.join(os.path.dirname(__file__), "..", "src", "spindle.ico")
    with open(path, "wb") as f:
        f.write(out)
    print("wrote", os.path.normpath(path), len(out), "bytes")


if __name__ == "__main__":
    main()
