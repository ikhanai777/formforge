"""Writes test STL files: a twisted vase (binary), a torus (ASCII) and a cube
with inverted winding (binary, header starting with "solid").

    python tools/make_test_models.py OUTDIR
"""
import math
import os
import struct
import sys


def vase(seg=160, rings=120, h=120.0):
    def radius(t, a):
        base = 28 + 14 * math.sin(math.pi * t * 1.1) - 8 * t
        return base + 2.5 * math.cos(6 * (a + t * 1.6))

    pts = []
    for j in range(rings + 1):
        t = j / rings
        row = []
        for i in range(seg):
            a = 2 * math.pi * i / seg
            r = radius(t, a)
            row.append((r * math.cos(a), r * math.sin(a), t * h))
        pts.append(row)
    tris = []
    for j in range(rings):
        for i in range(seg):
            a, b = pts[j][i], pts[j][(i + 1) % seg]
            c, d = pts[j + 1][(i + 1) % seg], pts[j + 1][i]
            tris += [(a, b, c), (a, c, d)]
    centre = (0.0, 0.0, 0.0)
    for i in range(seg):  # bottom cap (normal -Z)
        tris.append((centre, pts[0][(i + 1) % seg], pts[0][i]))
    return tris


def torus(R=30.0, r=10.0, n=64, m=32):
    def p(i, j):
        u, v = 2 * math.pi * i / n, 2 * math.pi * j / m
        return ((R + r * math.cos(v)) * math.cos(u), (R + r * math.cos(v)) * math.sin(u), r + r * math.sin(v))

    tris = []
    for i in range(n):
        for j in range(m):
            a, b, c, d = p(i, j), p(i + 1, j), p(i + 1, j + 1), p(i, j + 1)
            tris += [(a, b, c), (a, c, d)]
    return tris


def inverted_cube(s=40.0):
    v = [(x * s, y * s, z * s) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = []
    for a, b, c, d in faces:  # deliberately wound inwards
        tris += [(v[a], v[c], v[b]), (v[a], v[d], v[c])]
    return tris


def write_binary(path, tris, header=b"binary STL from make_test_models.py"):
    with open(path, "wb") as f:
        f.write(header.ljust(80, b" ")[:80])
        f.write(struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", 0, 0, 0))
            for p in t:
                f.write(struct.pack("<3f", *p))
            f.write(b"\0\0")


def write_ascii(path, tris):
    with open(path, "w") as f:
        f.write("solid torus\n")
        for t in tris:
            f.write("facet normal 0 0 0\n outer loop\n")
            for p in t:
                f.write("  vertex %.6f %.6f %.6f\n" % p)
            f.write(" endloop\nendfacet\n")
        f.write("endsolid torus\n")


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    write_binary(os.path.join(out, "vase.stl"), vase())
    write_ascii(os.path.join(out, "torus.stl"), torus())
    write_binary(os.path.join(out, "inverted_cube.stl"), inverted_cube(), b"solid but actually binary")
    print("wrote test models to", out)


if __name__ == "__main__":
    main()
