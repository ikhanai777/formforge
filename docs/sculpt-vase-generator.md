# The sculptural vase generator

Twelve more vases, built the other way round: a turned wall wearing solid ribs
that start and stop where you put them, a plan that can be squashed flat, and a
mouth that is cut rather than left level.

```
formforge sculpt --count 8 --seed 42 --style mixed --out out/sculpt
formforge sculpt --style blade --count 4 --render
formforge sculpt --params-only --count 12          # the sliders, no geometry
formforge sculpt --explain                         # the definition graph
formforge sculpt --style wave --set oval=0.5 --formats stl,step
```

The machinery -- the dataflow solver, the seed, the jitter, the proportion and
feasibility nodes -- is the one `docs/mushroom-generator.md` describes, and the
silhouette is the same monotone curve through four control diameters that
`docs/vase-generator.md` covers. This page is what is different.

## Why a second vase at all

`vessel_vase` makes its surface out of the wall: a flute is the wall bending in
and out, the cavity follows it, and the whole thing prints as one continuous
bead in vase mode.

This one puts things *on* the wall. A rib here is solid: the cavity behind it is
smooth, so what you get is a wall with ribs standing off it rather than a
corrugated wall. That is a different object to print -- real perimeters, not
vase mode -- and a different object to hold. Around that sit the three things
the fluted vase has no sliders for at all: ribs that start and stop where you
put them up the height, a plan squashed to an oval so the vase sits flat to a
shelf, and a mouth that is *cut* rather than left level.

## The styles

| Style | What it is |
| --- | --- |
| `pleat` | wide pleats wound half a turn, under a saddled rim |
| `blade` | deep thin blades on the body, a long throat, a beaked mouth |
| `wave` | squashed flat, with broad ridges swept across it |
| `column` | straight-sided, ribbed the whole way up |
| `urn` | the classic belly, with blades that stop at the shoulder |
| `crown` | bare below, bladed above, scooped at the rim |
| `spindle` | tall and narrow, eight deep fins wound half a turn |
| `shell` | no fins at all -- an oval turned form with a scooped mouth |
| `bud` | small, for one stem, cut on the slant |
| `flare` | a narrow foot opening into a wide fluted mouth |
| `carafe` | wide shoulders, bladed low, a beak on top |
| `drum` | squat and wide, with fourteen short ribs |

## The fins

Each rib fills `fin_width` of the gap between it and the next -- 0.25 is a
narrow blade with a wide flat between, 0.9 a pleated wall with barely a valley
-- and stands `fin_mm` off the wall. `fin_sharp` runs it from a soft swell to a
rib with steep sides and a flat crest.

Two sliders decide *where* the ribs are, and they are what most of the styles
are made of: `fin_start` and `fin_end` are the height fractions the band runs
between. Ribs over the whole wall is a column; ribs that stop at the shoulder is
an urn; ribs only above the belly is a crown; ribs on the lower body under a
bare throat is the shape everyone recognises from a fluted lamp base.

`fin_fade` is the ramp at each end of the band, and it is not decoration. A rib
that arrives at full depth over one layer is a ledge the printer has to bridge;
ramped over its own depth or more it is a 45 degree run-out, which is why the
template refuses `fin_mm > fin_fade * (fin_end - fin_start) * height_mm`.

`sweep_deg` winds the ribs round as they climb, and `sweep_ease` decides how: 0
is a constant rate, a helix, and 1 leaves the base and meets the rim running
straight up and does all the turning across the belly. That second one is the
difference between a shape that looks machined and one that looks drawn.

## The plan

`oval` squashes the whole vase across one axis, and every control diameter is
measured the wide way, so it only ever takes depth away. At 0.5 the vase is half
as deep as it is wide, which is the shape that sits flat against a wall.

It is built into the sections rather than applied to the finished solid. A
transform at the end is tempting -- an oval vase *is* a round one seen from the
side -- but it turns every analytic face into a bspline one, and the cut mouth
then costs ten times as much to take off it.

What a squash takes, it takes from everything, the wall and the ribs included.
Nothing is thickened to compensate; instead the template states what is left:
`wall_mm * (1 - oval) >= 0.9`, and a rib has to come out at least two beads
wide. The slider means what it says on the wide axis, and the rules keep the
flanks printable.

## The mouth

`rim_cut` finishes the mouth with an analytic cutter rather than a level rim:

* **`slant`** — one tilted plane. On a wide mouth it reads as a scoop; on a
  narrow throat it is a beak, and it is what a pitcher looks like.
* **`saddle`** — a cylinder laid across the mouth. It dips in the middle and
  rises at both ends, which is the heart-shaped mouth on every twisted vase.

`rim_drop_mm` is how far the low side falls below the high side. Both cutters
are placed to touch the vase at exactly `height_mm` where it reaches furthest
along +X, so a cut vase is never shorter than the height asked for.

Two rules come out of the geometry rather than from taste. A saddle is the
circle through both ends of the mouth and its middle, and the deepest such
circle is the half one -- so `rim_drop_mm <= mouth_d_mm * 0.45`, past which no
cylinder exists. And any cut has to stay above the neck: below that it stops
being a mouth and starts being a section through the vase.

The cut runs through the ribs as well as the wall, which is where the
serration along the rim comes from when `fin_end` is 1.

Both cutters are lifted 0.6 mm clear of the rim where they would otherwise be
tangent to it. That is not a fudge, it is the fix for a real failure: a cutting
surface tangent to the rim edge is the degenerate case for a boolean, and the
kernel hands back a solid with a sliver of a face along the tangency whose mesh
has a hole in it. Crossing the edge cleanly costs 0.6 mm of the drop.

## What the geometry costs, and why the schema stops where it does

Both skins are lofted and cut against each other, the same way the fluted vase
builds, and the build is not what this shape costs: eight to eleven seconds,
whatever is on the sliders. What it costs is the *mesh*. A rib is where the
surface normal swings hardest, and the mesher subdivides until it can follow it,
so the triangle count -- and the STL, and the DFM pass that has to read it --
grows with how steep each rib is against its own width, with how many there are,
and with how far the squash has pushed the curvature.

That was measured rather than guessed. Two limits come out of it, and both are
stated as preconditions:

```
fin_count * steepness * (1 + 2 * oval) <= 50
fin_count^2 * abs(sweep_deg) * (1 + 2 * oval) * max(0.5, steepness) <= 15000
```

where steepness is a rib's depth over half its own width. At the first limit a
vase meshes in about 125,000 triangles and seven seconds; a fifth past it, in
750,000 and forty. The second is the same story for a rib that also winds round
as it climbs: a swept rib crosses the mesher's grid diagonally, so no single row
of triangles can follow it however fine the row is -- sixteen ribs swept 140
degrees came out at 529,000 triangles and 25 seconds against 45,000 and two for
the same ribs standing straight.

The generator enforces both by giving up depth, and then sweep, rather than
ribs.

The rib ceiling itself -- fourteen -- is a third measurement, and it is memory
rather than time. Both skins are lofted and then live in memory together while
the boolean runs, and a section spline carrying a rib pattern costs far more of
that than a plain one: the resolution that draws a smooth vase in 740 MB draws a
sixteen-ribbed one in 1.9 GB, against the two gigabytes of address space the
sandbox gives the whole job. The kernel does not fail gracefully when it runs
out -- it segfaults, or hands back an empty shape -- so the template holds the
product of its two sampling rates under a budget, and the schema stops at the
rib count that fits.

There was a second architecture before this one, and the measurements are why it
is not here: every rib built as its own solid plate and fused onto a revolved
wall. It meshes beautifully -- 6,000 triangles, two tenths of a second -- and
the fuse is what kills it. A blade costs about a second to fuse, worse on a
shoulder, and the wall has to be cut into bands for the mesher, which every
blade is then intersected against; some shapes reached 100 CPU-seconds. Ribs
folded into the skin cost the mesh instead of the boolean, and the mesh is the
one you can bound with a slider.

## Printing

* **It is not a vase-mode print.** The ribs are solid -- the cavity behind them
  is smooth -- so it needs real perimeters. That is the trade for ribs you can
  feel.
* A cut mouth leaves the last layers as small islands. No supports needed, but a
  slower top-layer speed helps.
* Ribs cost material: a plain wall is around 109 cm³ at 200 mm tall, and the
  same vase with fourteen 6 mm ribs is around 162.
* Nothing here has been physically printed. The template records
  `print_test: untested`, and every number above is a measurement of the model.

## Adding a style

Add a set of slider positions to `STYLES` in `formforge/generators/sculpt.py`.
The jitter, the proportions, the feasibility rules, the CLI, the studio and the
tests pick it up: `tests/test_generators.py::TestCatalog` solves every style in
the catalog across its seed range and hands each result to the template's own
validator, and `TestStudioPage` fails if the browser page has not caught up.

The other generators on the same solver are covered by
`docs/mushroom-generator.md`, `docs/vase-generator.md`,
`docs/sculpt-vase-generator.md` and `docs/candle-holder-generator.md`.
