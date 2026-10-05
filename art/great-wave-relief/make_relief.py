"""Builds a printable bas-relief wall panel from a photo of a plaster relief.

    python make_relief.py reference.jpg out/  [--width 180] [--grid 0.2]

Pipeline
  1. Crop the panel from the photo and flatten the lighting (divide by the
     bright envelope), so shading is what remains.
  2. Mass layer: the two flat "sky" areas are separated from the wave masses
     with a seeded watershed. Masses become a raised slab with rounded edges
     and a gentle dome, so the big wave stands proud of the background.
  3. Detail layer: band-passed shading. On white plaster under soft light the
     brightness of the photo follows ambient occlusion, so pockets read dark and
     claws, ribs and droplets read bright; three bands keep the fine claws crisp
     and the grooves deep.
  4. The height field becomes a closed mesh (top surface, flat back, walls),
     simplified where it is flat. Two keyhole hangers are cut into the back.
  5. Writes STL (binary) and 3MF (millimetres), plus a heightmap PNG.
"""
import argparse
import os
import struct
import zipfile

import cv2
import fast_simplification
import manifold3d as mf
import numpy as np

# Panel corners in the reference photo (pixels), found from its edges.
PANEL = (30, 210, 738, 1135)  # x0, y0, x1, y1

# Seeds for the watershed, in panel pixels (x, y). Sky = the flat background.
SKY_SEEDS = [(40, 40), (150, 60), (60, 170), (20, 240), (600, 60), (680, 200), (620, 420), (520, 500),
             (660, 560), (480, 420)]
MASS_SEEDS = [(330, 140), (250, 200), (400, 250), (560, 250), (640, 330), (150, 400), (330, 600),
              (300, 680), (100, 690), (450, 800), (600, 750), (650, 880), (200, 850), (20, 330),
              (420, 640), (380, 470)]

# Heights in millimetres.
BASE = 5.0          # solid back plate under the background
MASS = 2.2          # wave masses above the background
DOME = 1.6          # extra rise towards the middle of big masses
DETAIL = 3.0        # detail scale (pockets and claws)
FLOOR = -2.0        # deepest a pocket may go below the background
# Mix of the detail cues (relative to DETAIL).
DETAIL_SHADING = 0.9  # band-passed brightness
CLAW = 0.8            # raised forms recovered from cast shadows
POCKET = 0.5          # extra depth where the photo is in shadow


def load_panel(path):
    im = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if im is None:
        raise SystemExit(f"cannot read {path}")
    x0, y0, x1, y1 = PANEL
    return im[y0:y1, x0:x1].astype(np.float32) / 255.0


def flatten_light(im, up):
    im = cv2.resize(im, (im.shape[1] * up, im.shape[0] * up), interpolation=cv2.INTER_CUBIC)
    im = cv2.bilateralFilter(im, 9, 0.06, 2.0 * up)
    k = 31 * up | 1
    env = cv2.GaussianBlur(cv2.dilate(im, np.ones((k, k), np.uint8)), (0, 0), 40 * up)
    return np.clip(im / env, 0, 1.2)


def disk(r):
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def texture_mask(s, up):
    b = cv2.GaussianBlur(s, (0, 0), 1.0 * up) - cv2.GaussianBlur(s, (0, 0), 10.0 * up)
    energy = np.sqrt(cv2.GaussianBlur(b * b, (0, 0), 6.0 * up))
    t = (energy > 0.03).astype(np.uint8)
    # The photo's panel edges are not relief: ignore the top edge and the right
    # edge above the lower streaks.
    h, w = t.shape
    t[:16 * up, :] = 0
    t[:560 * up, w - 14 * up:] = 0
    t = cv2.morphologyEx(t, cv2.MORPH_CLOSE, disk(8 * up))
    t = cv2.morphologyEx(t, cv2.MORPH_OPEN, disk(4 * up))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(t)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < 3000 * up * up:
            t[lab == i] = 0
    return t


def mass_layer(s, up):
    h, w = s.shape
    g = cv2.GaussianBlur(s, (0, 0), 1.5 * up)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1)
    grad = np.sqrt(gx * gx + gy * gy)
    markers = np.zeros((h, w), np.int32)
    r = 6 * up
    for x, y in SKY_SEEDS:
        cv2.circle(markers, (x * up, y * up), r, 1, -1)
    for x, y in MASS_SEEDS:
        cv2.circle(markers, (x * up, y * up), r, 2, -1)
    gimg = cv2.cvtColor(np.clip(grad / np.percentile(grad, 99) * 255, 0, 255).astype(np.uint8),
                        cv2.COLOR_GRAY2BGR)
    cv2.watershed(gimg, markers)
    mass = (markers == 2).astype(np.uint8)
    # The watershed finds the smooth masses (the wave's back, the small wave in
    # the middle); the foam and the wave faces are found by their texture.
    mass |= texture_mask(s, up)
    # Clean the boundary: fill small holes, drop specks, smooth the outline.
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5 * up | 1, 5 * up | 1))
    mass = cv2.morphologyEx(mass, cv2.MORPH_CLOSE, k)
    mass = cv2.morphologyEx(mass, cv2.MORPH_OPEN, k)
    mass = (cv2.GaussianBlur(mass.astype(np.float32), (0, 0), 2.0 * up) > 0.5).astype(np.uint8)
    d = cv2.distanceTransform(mass, cv2.DIST_L2, 5)
    edge = np.clip(d / (5.0 * up), 0, 1)
    edge = np.sin(edge * np.pi / 2)            # rounded shoulder
    dome = np.clip(d / (90.0 * up), 0, 1)
    dome = dome * dome * (3 - 2 * dome)        # smoothstep
    m = MASS * edge + DOME * dome
    return cv2.GaussianBlur(m, (0, 0), 1.0 * up), mass


def shift(img, dx, dy):
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, m, (img.shape[1], img.shape[0]), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def claw_layer(s, up):
    """Raised forms recovered from their cast shadows. The photo is lit from
    above (slightly right), so every claw, rib and droplet has its shadow just
    below it. A bright pixel with shadow a few pixels below sits on a raised
    form; how far the shadow reaches says how tall."""
    b = cv2.GaussianBlur(s, (0, 0), 0.8 * up) - cv2.GaussianBlur(s, (0, 0), 12.0 * up)
    shadow = np.clip((-b - 0.02) / 0.08, 0, 1)
    lit = np.clip((b + 0.03) / 0.07, 0, 1)
    ux, uy = -0.17, 0.985                    # towards the shadow (down, a little left)
    occ = np.zeros_like(s)
    reach = 7 * up
    for k in range(1, reach + 1):
        occ = np.maximum(occ, shift(shadow, -ux * k, -uy * k) * (1.0 - 0.5 * k / reach))
    raised = lit * occ * (1.0 - shadow)
    # Close the gaps between scan steps and round the tops.
    raised = cv2.morphologyEx(raised, cv2.MORPH_CLOSE, disk(1 * up))
    profile = cv2.GaussianBlur(raised, (0, 0), 1.5 * up)
    return profile, shadow


def detail_layer(s, up, mass):
    def bp(lo, hi):
        return cv2.GaussianBlur(s, (0, 0), lo * up) - cv2.GaussianBlur(s, (0, 0), hi * up)
    d = 0.3 * bp(0.8, 3.0) + 1.0 * bp(3.0, 10.0) + 0.8 * bp(10.0, 32.0)
    # Soft-limit so isolated highlights do not become spikes.
    d = np.tanh(d / 0.18) * 0.18
    claw, shadow = claw_layer(s, up)
    d = d * DETAIL_SHADING + CLAW * 0.18 * claw - POCKET * 0.18 * cv2.GaussianBlur(shadow, (0, 0), 1.0 * up)
    # The background stays flat: fade detail out beyond the masses.
    near = cv2.GaussianBlur(cv2.dilate(mass, np.ones((9 * up, 9 * up), np.uint8)).astype(np.float32),
                            (0, 0), 3.0 * up)
    # Smooth areas (the wave's back, the background) keep the plaster smooth:
    # the photo's pores and grain are not carved.
    b = cv2.GaussianBlur(s, (0, 0), 1.0 * up) - cv2.GaussianBlur(s, (0, 0), 10.0 * up)
    energy = np.sqrt(cv2.GaussianBlur(b * b, (0, 0), 3.0 * up))
    busy = cv2.GaussianBlur(np.clip((energy - 0.012) / 0.025, 0, 1), (0, 0), 2.0 * up)
    return d / 0.18 * DETAIL * near * busy


def heightmap(path, up=2):
    s = flatten_light(load_panel(path), up)
    m, mass = mass_layer(s, up)
    d = detail_layer(s, up, mass)
    hgt = np.maximum(m + d * np.clip(0.35 + m / MASS, 0, 1.4), FLOOR)
    return cv2.GaussianBlur(hgt, (0, 0), 0.6 * up), mass


def resample(hgt, width_mm, grid):
    h, w = hgt.shape
    height_mm = width_mm * h / w
    nx, ny = int(round(width_mm / grid)) + 1, int(round(height_mm / grid)) + 1
    z = cv2.resize(hgt, (nx, ny), interpolation=cv2.INTER_AREA)
    return z, width_mm, height_mm


def closed_mesh(z, width, height, base):
    """Top surface from z (rows run top to bottom of the picture), flat back at
    0, side walls. Returns vertices (N,3) and faces (M,3), outward facing."""
    ny, nx = z.shape
    xs = np.linspace(0, width, nx)
    ys = np.linspace(height, 0, ny)  # picture top is +Y
    X, Y = np.meshgrid(xs, ys)
    top = np.stack([X, Y, z + base], -1).reshape(-1, 3)
    idx = np.arange(nx * ny).reshape(ny, nx)
    a, b, c, d = idx[:-1, :-1], idx[:-1, 1:], idx[1:, :-1], idx[1:, 1:]
    # Rows go down in Y, so (a, c, b) is counter-clockwise seen from +Z.
    f_top = np.concatenate([np.stack([a, c, b], -1).reshape(-1, 3),
                            np.stack([b, c, d], -1).reshape(-1, 3)])
    # Border ring of the top surface, walked counter-clockwise seen from +Z.
    ring = np.concatenate([idx[-1, :], idx[::-1, -1][1:], idx[0, ::-1][1:], idx[:, 0][1:-1]])
    n0 = len(top)
    bottom = top[ring].copy()
    bottom[:, 2] = 0.0
    verts = np.concatenate([top, bottom])
    k = len(ring)
    i = np.arange(k)
    j = (i + 1) % k
    t0, t1, b0, b1 = ring[i], ring[j], n0 + i, n0 + j
    f_wall = np.concatenate([np.stack([t0, b0, b1], -1), np.stack([t0, b1, t1], -1)])
    # Back: fan from a centre vertex, facing -Z.
    cidx = len(verts)
    verts = np.concatenate([verts, [[width / 2, height / 2, 0.0]]])
    f_back = np.stack([np.full(k, cidx), b1, b0], -1)
    return verts, np.concatenate([f_top, f_wall, f_back]).astype(np.int64)


def keyhole(cx, cy):
    """A keyhole hanger, cut from the back (z=0) upwards. Screw head goes in
    the round opening and slides up the slot; the wider chamber above holds the
    head. Prints face up with no supports (the roof is a short bridge)."""
    def stadium(r, length, z0, z1):
        c = mf.Manifold.cylinder(z1 - z0, r, r, 48).translate([0, 0, z0])
        c2 = c.translate([0, length, 0])
        box = mf.Manifold.cube([2 * r, length, z1 - z0]).translate([-r, 0, z0])
        return c + c2 + box
    opening = mf.Manifold.cylinder(3.6, 5.0, 5.0, 48).translate([0, 0, -0.5])   # head, 10 mm
    slot = stadium(2.4, 10.0, -0.5, 1.6)                                       # shank, 4.8 mm
    chamber = stadium(5.0, 10.0, 1.6, 3.6)                                     # head travel
    return (opening + slot + chamber).translate([cx, cy, 0])


def to_manifold(v, f):
    return mf.Manifold(mf.Mesh(vert_properties=v.astype(np.float32), tri_verts=f.astype(np.uint32)))


def write_stl(path, v, f):
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    rec = np.zeros(len(f), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, tri
    with open(path, "wb") as fh:
        fh.write(b"Great Wave relief panel".ljust(80, b" "))
        fh.write(struct.pack("<I", len(f)))
        fh.write(rec.tobytes())


def write_3mf(path, v, f, name):
    vs = "\n".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in v)
    ts = "\n".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
    model = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
<metadata name="Title">{name}</metadata>
<metadata name="Designer">FormForge</metadata>
<resources>
<basematerials id="1"><base name="Plaster white" displaycolor="#EEEBE4FF"/></basematerials>
<object id="2" type="model" name="{name}" pid="1" pindex="0">
<mesh>
<vertices>
{vs}
</vertices>
<triangles>
{ts}
</triangles>
</mesh>
</object>
</resources>
<build><item objectid="2"/></build>
</model>
"""
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("out")
    ap.add_argument("--width", type=float, default=180.0, help="panel width in mm")
    ap.add_argument("--grid", type=float, default=0.2, help="sampling step in mm")
    ap.add_argument("--triangles", type=int, default=700_000, help="target triangle count")
    ap.add_argument("--no-keyholes", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    hgt, mass = heightmap(a.image)
    cv2.imwrite(os.path.join(a.out, "heightmap.png"),
                np.clip((hgt - FLOOR) / (hgt.max() - FLOOR) * 65535, 0, 65535).astype(np.uint16))
    z, width, height = resample(hgt, a.width, a.grid)
    base = BASE - FLOOR                      # back plate under the deepest pocket
    v, f = closed_mesh(z, width, height, base)
    print(f"grid {z.shape[1]}x{z.shape[0]}, {len(f)} triangles before simplification")

    # Simplify (flat background collapses to few triangles), keeping the border.
    target = max(0.0, 1.0 - a.triangles / len(f))
    v2, f2 = fast_simplification.simplify(v.astype(np.float32), f.astype(np.int32), target_reduction=target,
                                          agg=7)
    solid = to_manifold(v2, f2)
    if solid.status() != mf.Error.NoError:
        raise SystemExit(f"mesh is not a closed solid: {solid.status()}")
    if not a.no_keyholes:
        # Two hangers a quarter of the width in from the sides, 40 mm below the top.
        for cx in (width * 0.25, width * 0.75):
            solid = solid - keyhole(cx, height - 40.0)
    mesh = solid.to_mesh()
    v3 = np.asarray(mesh.vert_properties)[:, :3].astype(np.float64)
    f3 = np.asarray(mesh.tri_verts).astype(np.int64)
    zmax = v3[:, 2].max()
    print(f"panel {width:.1f} x {height:.1f} mm, {zmax:.1f} mm thick at the highest point, "
          f"back plate {base:.1f} mm, {len(f3)} triangles, volume {solid.volume() / 1000:.1f} cm3, "
          f"genus {solid.genus()}")
    write_stl(os.path.join(a.out, "great_wave_relief.stl"), v3, f3)
    write_3mf(os.path.join(a.out, "great_wave_relief.3mf"), v3, f3, "Great Wave relief panel")


if __name__ == "__main__":
    main()
