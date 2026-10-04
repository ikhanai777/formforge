"""Writes test models: a twisted vase (binary STL), a torus (ASCII STL), a cube
with inverted winding (binary STL whose header starts with "solid"), and two
deflate-compressed 3MFs - a plain one, and one laid out like Bambu Studio /
OrcaSlicer files (objects in a separate part referenced through p:path).

    python tools/make_test_models.py OUTDIR
"""
import math
import os
import struct
import sys
import zipfile


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


CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD_NS = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
)
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
)


def mesh_object(obj_id, tris):
    """Indexed <object> for a triangle soup (vertices deduplicated)."""
    index, verts, out = {}, [], []
    for t in tris:
        ids = []
        for p in t:
            key = tuple(round(c, 5) for c in p)
            if key not in index:
                index[key] = len(verts)
                verts.append(key)
            ids.append(index[key])
        out.append(ids)
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % p for p in verts)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tuple(i) for i in out)
    return '<object id="%d" type="model"><mesh><vertices>%s</vertices><triangles>%s</triangles></mesh></object>' % (
        obj_id, v, t)


def write_3mf(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        for name, data in files.items():
            z.writestr(name, data)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    write_binary(os.path.join(out, "vase.stl"), vase())
    write_ascii(os.path.join(out, "torus.stl"), torus())
    write_binary(os.path.join(out, "inverted_cube.stl"), inverted_cube(), b"solid but actually binary")
    # Plain 3MF in centimetres: the vase at 1/10 scale numbers, i.e. the same size in mm.
    vase_cm = [tuple(tuple(c / 10 for c in p) for p in t) for t in vase()]
    write_3mf(os.path.join(out, "vase.3mf"), {
        "3D/3dmodel.model": '<?xml version="1.0" encoding="UTF-8"?>\n<model unit="centimeter" xmlns="%s">'
                            '<resources>%s</resources><build><item objectid="1"/></build></model>'
                            % (CORE_NS, mesh_object(1, vase_cm)),
    })
    # Bambu-style: two torus instances placed by components that live in another part.
    write_3mf(os.path.join(out, "plate.3mf"), {
        "3D/Objects/object_1.model": '<model unit="millimeter" xmlns="%s"><resources>%s</resources><build/></model>'
                                     % (CORE_NS, mesh_object(1, torus())),
        "3D/3dmodel.model": '<model unit="millimeter" xmlns="%s" xmlns:p="%s"><resources>'
                            '<object id="2" type="model"><components>'
                            '<component p:path="/3D/Objects/object_1.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 -45 0 0"/>'
                            '<component p:path="/3D/Objects/object_1.model" objectid="1" '
                            'transform="0 0 1 0 1 0 -1 0 0 45 0 40"/>'
                            '</components></object></resources>'
                            '<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 128 128 0" printable="1"/></build>'
                            '</model>' % (CORE_NS, PROD_NS),
    })
    print("wrote test models to", out)


if __name__ == "__main__":
    main()
