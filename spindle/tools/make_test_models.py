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


def box(x0, y0, z0, x1, y1, z1):
    v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    out = []
    for a, b, c, d in faces:
        out += [(v[a], v[b], v[c]), (v[a], v[c], v[d])]
    return out


def prism(outline, z0, z1):
    """Extrudes a convex-ish closed outline (list of (x, y)) between z0 and z1."""
    n = len(outline)
    cx = sum(p[0] for p in outline) / n
    cy = sum(p[1] for p in outline) / n
    out = []
    for i in range(n):
        (ax, ay), (bx, by) = outline[i], outline[(i + 1) % n]
        out += [((ax, ay, z0), (bx, by, z0), (bx, by, z1)), ((ax, ay, z0), (bx, by, z1), (ax, ay, z1))]
        out.append(((cx, cy, z1), (ax, ay, z1), (bx, by, z1)))
        out.append(((cx, cy, z0), (bx, by, z0), (ax, ay, z0)))
    return out


def circle(cx, cy, r, n=48):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def gear(cx, cy, r, teeth=18):
    pts = []
    for i in range(teeth * 4):
        a = 2 * math.pi * i / (teeth * 4)
        rr = r if (i % 4) in (0, 1) else r * 0.86
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return pts


def dome(cx, cy, z0, r, n=32, m=12):
    out = []
    def p(i, j):
        u, v = 2 * math.pi * i / n, (math.pi / 2) * j / m
        return (cx + r * math.cos(v) * math.cos(u), cy + r * math.cos(v) * math.sin(u), z0 + r * math.sin(v))
    for i in range(n):
        for j in range(m):
            a, b, c, d = p(i, j), p(i + 1, j), p(i + 1, j + 1), p(i, j + 1)
            out += [(a, b, c), (a, c, d)]
        out.append(((cx, cy, z0), p(i + 1, 0), p(i, 0)))
    return out


def styled_object(obj_id, tris, name, pid=None, pindex=None, tri_attrs=None):
    """Like mesh_object, with a name, an object-level property and optional per-triangle attributes."""
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
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"%s/>' % (i[0], i[1], i[2], (tri_attrs(k) if tri_attrs else ""))
                for k, i in enumerate(out))
    props = (' pid="%d" pindex="%d"' % (pid, pindex)) if pid is not None else ""
    return '<object id="%d" name="%s" type="model"%s><mesh><vertices>%s</vertices><triangles>%s</triangles></mesh></object>' % (
        obj_id, name, props, v, t)


MAT_NS = "http://schemas.microsoft.com/3dmanufacturing/material/2015/02"


def gearbox_3mf():
    """Five parts with 3MF core/material-extension colours and metallic/roughness."""
    base = box(-45, -30, 0, 45, 30, 10)
    shaft = prism(circle(0, 0, 4, 32), 10, 70)
    gear_a = prism(gear(0, 0, 22), 18, 26)
    gear_b = prism(gear(0, 0, 14, 12), 34, 42)
    cap = dome(0, 0, 70, 7)
    resources = (
        '<m:pbmetallicdisplayproperties id="1">'
        '<m:pbmetallic name="Anodised" metallicness="0" roughness="0.55"/>'
        '<m:pbmetallic name="Steel" metallicness="1" roughness="0.25"/>'
        '<m:pbmetallic name="Brass" metallicness="1" roughness="0.35"/></m:pbmetallicdisplayproperties>'
        '<basematerials id="2" displaypropertiesid="1"><base name="Anodised" displaycolor="#2B3A55"/>'
        '<base name="Steel" displaycolor="#C8CCD2"/><base name="Brass" displaycolor="#D9A441"/></basematerials>'
        '<m:colorgroup id="3"><m:color color="#E4572E"/><m:color color="#F3E9D2"/></m:colorgroup>'
        + styled_object(10, base, "Base plate", 2, 0)
        + styled_object(11, shaft, "Shaft", 2, 1)
        + styled_object(12, gear_a, "Large gear", 2, 2)
        # Small gear: alternate faces in two colours via the colour group.
        + styled_object(13, gear_b, "Small gear", 3, 0, lambda k: ' pid="3" p1="%d"' % ((k // 2) % 2))
        + styled_object(14, cap, "Cap", 3, 0)
    )
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xmlns="%s" xmlns:m="%s">'
             '<resources>%s</resources><build>%s</build></model>'
             % (CORE_NS, MAT_NS, resources, "".join('<item objectid="%d"/>' % i for i in range(10, 15))))
    return {"3D/3dmodel.model": model}


def robot_bambu_3mf():
    """Bambu Studio layout: parts in a sub-model, filament colours from the project,
    per-part filament assignment, one modifier part, and a painted visor."""
    parts = {
        1: ("Body", box(-15, -10, 12, 15, 10, 42), 1),
        2: ("Head", box(-10, -9, 44, 10, 9, 60), 2),
        3: ("Left leg", box(-13, -6, 0, -3, 6, 12), 3),
        4: ("Right leg", box(3, -6, 0, 13, 6, 12), 3),
        5: ("Left arm", box(-23, -4, 20, -16, 4, 40), 2),
        6: ("Right arm", box(16, -4, 20, 23, 4, 40), 2),
    }
    sub = []
    for pid, (name, tris, _) in parts.items():
        # Paint the head's front face (-Y) with filament 4: an unsplit leaf with state 4 is "1C".
        attrs = (lambda k: ' paint_color="1C"' if k in (4, 5) else "") if pid == 2 else None
        sub.append(styled_object(pid, tris, name, tri_attrs=attrs))
    sub.append(styled_object(7, box(-30, -30, 0, 30, 30, 70), "Height range modifier"))
    sub_model = '<model unit="millimeter" xmlns="%s"><resources>%s</resources><build/></model>' % (CORE_NS, "".join(sub))
    comps = "".join('<component p:path="/3D/Objects/object_1.model" objectid="%d"/>' % i for i in range(1, 8))
    root = ('<model unit="millimeter" xmlns="%s" xmlns:p="%s"><resources><object id="8" type="model">'
            '<components>%s</components></object></resources>'
            '<build><item objectid="8" transform="1 0 0 0 1 0 0 0 1 128 128 0" printable="1"/></build></model>'
            % (CORE_NS, PROD_NS, comps))
    settings = ['<?xml version="1.0" encoding="UTF-8"?>\n<config><object id="8">'
                '<metadata key="name" value="Robot"/><metadata key="extruder" value="1"/>']
    for pid, (name, _, filament) in parts.items():
        settings.append('<part id="%d" subtype="normal_part"><metadata key="name" value="%s"/>'
                        '<metadata key="extruder" value="%d"/></part>' % (pid, name, filament))
    settings.append('<part id="7" subtype="modifier_part"><metadata key="name" value="Height range modifier"/></part>')
    settings.append('</object></config>')
    project = '{\n  "filament_colour": ["#F2F2F2", "#2D7DD2", "#3A3A3A", "#F45D01"],\n  "nozzle_diameter": ["0.4"]\n}'
    return {
        "3D/Objects/object_1.model": sub_model,
        "3D/3dmodel.model": root,
        "Metadata/model_settings.config": "".join(settings),
        "Metadata/project_settings.config": project,
    }


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
    write_3mf(os.path.join(out, "gearbox.3mf"), gearbox_3mf())
    write_3mf(os.path.join(out, "robot_bambu.3mf"), robot_bambu_3mf())
    print("wrote test models to", out)


if __name__ == "__main__":
    main()
