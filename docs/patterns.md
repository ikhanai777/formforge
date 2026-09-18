# Pattern panels

A pattern panel is a relief surface — waves, dunes, ridges, a gyroid, a Voronoi
field — generated at whatever size the wall is, and cut into tiles that fit the
build plate and join back together.

It exists because the template registry structurally cannot answer the request.
A template is a parametric script with a dozen named dimensions; "a 1.8 metre
wave wall, in however many pieces my printer needs" has no dimensions to name,
needs a surface with a quarter of a million distinct heights, and needs the
pieces to be pieces *of one thing*. Those are different problems with a
different solution, which is why this is a separate subsystem that borrows the
printer profiles, the DFM thresholds and the validation engine and shares
nothing else.

```
formforge pattern dunes --size 900x600 --joint key --mount keyhole --base 5
```

## The one decision everything else follows from

**A pattern is a function of a point on the wall. A tile is a window onto it.**

```python
h = f(x, y)        # world millimetres in, height in [0, 1] out
```

Nothing in a pattern knows about tiles. Tile B2 samples the same `f` over its
own rectangle that tile B3 samples over the one next to it, so the crest of a
dune that leaves B2 at x = 400 mm arrives in B3 at x = 400 mm because both asked
the same function about the same point. There is no seam-matching pass, no
blend, no stitching — and therefore no seam-matching pass that can fail.

Everything else here is a consequence of protecting that property:

* **Noise is hashed, not generated.** `formforge/patterns/noise.py` derives every
  value from an integer hash of the lattice cell. A `numpy.random` generator
  would make the field depend on the order tiles were built in, which is exactly
  the bug that produces a panel that does not line up — and produces it
  silently, months after the code was written, on somebody's living room floor.
* **Normalisation is measured once, over the whole panel.** `fbm()` has no
  closed-form range, so mapping it onto [0, 1] means measuring it. Measure it
  per tile and every tile gets its own mapping: the flattest tile stretches to
  full relief, the seam steps by millimetres, the panel is scrap. `PatternField.
  calibrate()` measures once and every tile shares the result, which is why the
  field is a stateful object rather than a bare function.
* **Rotation rotates the sample point, not the pattern.** A wave train at 25°
  is still one global function, asked about a rotated point.

The test that matters most in `tests/test_patterns.py` is the one that looks
like it is testing nothing: sampling a window has to give bit-identical results
to sampling the panel and slicing out the same region. Every way this feature
can break is a way that assertion can break.

### Where normalisation is approximate, and what it costs

The calibration pass samples 192 × 192 over the panel and takes the half and
99.5th percentiles as the range, rather than the minimum and maximum. Fractal
terrain spends most of its area near the middle of its range and reaches the
extremes on a handful of peaks; normalising to min/max leaves a whole hillside
using a third of the relief while the numbers claim it used all of it.

The cost is that roughly 1% of the panel lands outside [0, 1] after the mapping.
Those values are folded back by `_saturate()`, which is the identity across the
middle 96% and decays exponentially towards the bound outside it. It is strictly
increasing, so two peaks of different heights stay different heights — a hard
clip would merge them into one flat mesa, and a flat mesa is the most obvious
"generated" artifact there is on a relief print.

The consequence worth knowing: the extreme 1% of a panel is compressed by up to
2% of the relief depth. On 8 mm of dune that is 0.16 mm, which is under one
layer.

## Meshing: the deliberate exception

The rest of FormForge refuses to generate geometry directly and drives a CAD
kernel instead. Patterns do generate geometry directly, and the reason is
specific rather than convenient: a 300 × 200 mm surface sampled at 0.5 mm is
240 000 heights. There is no B-rep formulation of that which is not the mesh
with extra steps, and OCCT would spend minutes tessellating a surface numpy
produces exactly, at the requested spacing, in milliseconds.

What is not given up is watertightness. A tile is built as a closed sandwich:

```
top surface (nx × ny grid)  +  flat back  +  four skirt walls
```

Every boundary vertex of the top is shared with its skirt and every skirt vertex
with the back, so there is nothing to seal and no repair pass to run. The only
operations that can break it are the boolean clips — the footprint and the back
pockets — and each is checked for watertightness, single-bodiedness and positive
volume immediately afterwards rather than at export time, next to the operation
that caused it.

The booleans go through manifold3d, already a dependency via trimesh. Exact
joinery is the point of the whole feature, and a dovetail flank rasterised onto
the height grid would be a staircase that does not slide into its socket.

### Sampling pitch

Default is **twice the nozzle width**, capped by a per-tile triangle budget of
400 000. A 0.4 mm nozzle lays a 0.4 mm road, so detail finer than about 0.8 mm
cannot survive being printed and sampling it only quadruples the file size. The
difference between 0.4 mm and 0.15 mm sampling on a 250 mm tile is 300 000
triangles against 2.2 million — the second is a 200 MB STL a slicer struggles to
open, for detail no machine can produce. `--resolution` overrides for anyone who
disagrees; when the budget forces a coarser pitch than requested, the pitch that
was actually used is reported rather than quietly substituted.

## Joining

Cutting the panel up is trivial. Nine loose slabs of PLA are not a wall panel,
and the joint has to satisfy three things at once.

**It has to be assemblable.** This is the constraint that eliminates the obvious
answer. An in-plane dovetail locks beautifully across one axis and makes a grid
impossible: a tile with dovetails on its right *and* its top edge would have to
slide in two directions at once to go in. So the default is a key on the back,
fitted after the tiles are already laid out — and `dovetail` here means
dovetails on the vertical seams with keys on the horizontal ones, so columns
slide together and then stack.

**It has to print without supports.** Every joint surface is a vertical wall or
a flat floor with the tile back-down. The single exception is the keyhole, which
bridges 4.5 mm over its own slot.

**It has to fit.** Every mating pair is opened up by `clearance_mm`, applied to
the socket so the visible part keeps its drawn size. 0.25 mm suits a tuned
0.4 mm nozzle; 0.35 mm is the safer first try on an untuned one.

| Joint | Both axes | Extra parts | Resists being pulled apart | Assembly |
|---|---|---|---|---|
| `butt` | yes | none | no | glue or seam tape |
| `key` *(default)* | yes | bowtie keys | yes | lay face down, drop keys in |
| `bar` | yes | rectangular splines | no | lay face down, drop splines in |
| `dovetail` | vertical seams only | keys for the horizontal seams | yes | slide columns, then stack |
| `puzzle` | yes | none | yes | press together in plane |

The bowtie is the default because of the fourth column. A rectangular spline
aligns two tiles and does nothing to stop them separating; a bowtie has to be
pulled *through its own waist* to let go, so the seam is held closed by the key
rather than by the glue.

Joint positions are never negotiated between neighbours. Tiles are uniform, so a
seam is the same length from both sides and both tiles place their joints by
computing the same thing. Polarity is fixed by convention — **male on the right
and top edges, female on the left and bottom** — so every interior seam has
exactly one of each.

### The bug this design makes easy to write

A female socket is the *same region of space* as the male tab it mates with: the
male tile adds it outside its own edge, the female tile removes it from inside.
So the socket points the opposite way to the tab on the same edge. Getting that
backwards cuts the socket out of the empty air beside the tile, which renders
beautifully, passes watertightness, and produces two male tabs colliding at the
seam. `test_in_plane_joints_mate_without_interference` exists for exactly this:
the two footprints must not overlap, and their union must be a single connected
polygon.

## Mounting

* `magnets` — round pockets on the back, sized with clearance for disc magnets.
* `keyhole` — two stacked pockets. A shallow one shaped like the whole keyhole,
  so the screw shank has somewhere to travel, and a deep one under the round end
  only, so the head is trapped behind the lip once the tile has slid down.
  Printed back-down, that lip is a bridge over the slot.

Both are checked against the base thickness before any geometry is built: a
3 mm magnet pocket in a 3 mm base is not a pocket, it is a hole.

## Preconditions

The same division the template registry draws. A JSON Schema can only constrain
one number at a time, so "the key socket has to be shallower than the base"
has nowhere else to live, and discovering it afterwards reports *the tile fell
into three pieces* when the truth is *those two numbers cannot both be right*.

Checked before anything is built:

- the base clears the profile's minimum floor
- the base clears the key socket plus a floor under it
- the base clears the magnet pocket, or the keyhole's head depth, plus a floor
- the key spans its seam (length across > width along)
- base plus relief fits the build height
- the tile, **including whatever the joint adds**, fits the plate with a margin

That last one is why `plan_panel` takes `joint_growth_mm`. A 250 mm tile with
9 mm dovetails is a 259 mm object, and finding that out in the slicer is finding
it out too late.

A panel that comes out as a single tile has no seams, so its joint is never
built and its numbers are not checked — asking for `--joint key` on a panel that
happens to fit the plate is not an error.

## What comes out

A bundle, not a file:

```
tiles/A1.stl, A1.3mf ...    one per piece, moved to the origin, back down
parts/key.stl               the joint part, with a count in the manifest
assembly.json               positions, seams, neighbours, every parameter, warnings
ASSEMBLY.md                 the same for a human, with the grid drawn out
assembled_preview.stl       the whole panel, for checking before 40 hours of printing
panel.stl                   the one-piece version, with --single
previews/                   with --preview
```

Two things in there are load-bearing:

**Every tile carries its grid reference and an up-arrow recessed 0.6 mm into its
back.** Nine tiles of a dune field look identical face down on a table. The text
is mirrored in X, because it is cut into a face that is looked at from behind,
and the moment you need to read it is the moment the tiles are face down. The
font is a 5 × 7 bitmap rather than a real one — sixteen characters do not
justify a font dependency, and rectangular pixels are the most printable
letterform there is.

**`assembly.json` holds every parameter, and `ASSEMBLY.md` prints the command
that rebuilds the panel.** The generator is deterministic down to the seed, so a
cracked tile is one command and one reprint, not a new panel.

### 3MF is written by hand

`formforge/patterns/export.py` writes the 3MF directly — a zip with three
entries and about sixty lines. The alternative was an XML library that is not
otherwise a dependency, pulled in so a third-party exporter could write the same
three entries. For a format this small that trade is not worth making, and doing
it here means `unit="millimeter"` is set by us rather than hoped for. That
attribute is the entire reason this project prefers 3MF over STL.

## Limits worth knowing

* **Mass figures are solid volume.** `assembly.json` reports what the model
  displaces. A real print at 15% infill uses roughly a third of it; the base and
  the top skin are what actually cost.
* **The relief is a height field, so it cannot overhang.** No caves, no
  undercuts, nothing that curls back over itself. That is also why every panel
  prints without supports, and it is the right trade for wall relief.
* **Terracing and contrast are applied after normalisation**, so they compose
  with the pattern rather than with its raw range — which is what keeps them
  from changing where the seams land.
* **Patterns are not print-tested.** Like every other DFM constant in this
  repository, the numbers here are conventional maker values. `formforge
  feedback` is what turns one of them into a measurement.
