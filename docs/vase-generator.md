# The vase generator

Twelve silhouettes and four surface treatments from one definition, exported as
STL to print and STEP to edit.

```
formforge vase --count 8 --seed 42 --style mixed --out out/vases
formforge vase --style spiral --count 4 --render
formforge vase --params-only --count 12            # the sliders, no geometry
formforge vase --explain                           # the definition graph
formforge vase --style tulip --set height_mm=220 --formats stl,step
```

Every run writes `.stl`, `.step` and `.3mf` per vase plus a `variations.json`
of parameters and DFM verdicts. The machinery — the dataflow solver, the seed,
the jitter, the proportion and feasibility nodes — is the same one
`docs/mushroom-generator.md` describes; this page is what is specific to vases.

## The styles

| Style | What it is |
| --- | --- |
| `classic` | a turned urn, the shape everyone pictures |
| `amphora` | high belly, narrow neck, small mouth |
| `bottle` | wide shoulders and a long throat |
| `bud` | small, for one stem |
| `tulip` | narrow foot opening into a wide mouth |
| `hourglass` | pinched at the waist |
| `cylinder` | straight-sided, banded at the rim |
| `faceted` | hexagonal cross-section, hard shoulders |
| `crystal` | pentagon with a slow twist |
| `spiral` | flutes wound most of a turn — the vase-mode classic |
| `fluted` | a column of sharp ribs |
| `rippled` | horizontal rings up the wall |

They are not twelve sizes of one vase: each is a set of positions for the same
sliders, and `--style mixed` draws one per seed.

## The silhouette

Four control diameters — base, belly, neck, rim — with the belly and the neck
free to slide up and down the height. That is the whole silhouette, and it is
enough: belly wider than base and rim is an urn, belly narrower is a waist, a
rim well above a narrow neck is a tulip, all four equal is a cylinder.

Between the control points the profile is a **Fritsch-Carlson monotone cubic**,
not a natural cubic. The difference matters at the first slider you drag: a
natural spline through four diameters bulges past the widest of them and puts
an undercut where you asked for a straight shoulder. A monotone curve stays
inside its own control points, so the profile is the one the sliders describe.
`shoulder` sets how roundly it passes through them — 0 runs the segments nearly
straight for a turned, hard-shouldered look, 1 rounds them fully.

## The surface

Applied to the radius, in this order, on both the outer skin and the cavity:

* **facets** — a rounded polygon cross-section, 3 to 12 sides, `facet_round`
  from a sharp polygon to a circle.
* **flutes** — `lobes` vertical ribs `lobe_mm` deep, `flute_sharp` from a
  rounded flute to a cusped rib. Added to the radius rather than multiplied
  into it, so the wall stays the same thickness in the troughs as on the
  crests, which is what the nozzle cares about.
* **ripples** — horizontal rings up the wall.
* **twist** — rotation of the section from base to rim. With flutes or facets
  this is the spiral vase everyone prints; on a round section it does nothing.

## The wall

The cavity is a second loft of the same skin, inset perpendicular to the
surface rather than radially: on a sloped shoulder a purely radial inset is
thinner than it looks by the cosine of the slope. It starts at `base_mm` and
runs past the rim, so one boolean gives both the wall and the mouth.
`rim_band_mm` thickens the top 10 mm into a lip that survives handling.

Set `wall_mm` to one extrusion width and the same model prints in vase mode as
a single continuous bead.

## What the geometry costs, and why the schema stops where it does

Both skins are lofted through `SECTIONS × POINTS` points and then cut against
each other, so their product is what a build costs. The script sets both from
the design rather than from a constant: 40 bands minimum for a smooth
silhouette, six per ripple, and — the one that matters — enough that
consecutive sections stay a fraction of a flute apart when the vase twists.

That last one is not about looks. Under-sample a twisted flute and the ruled
bands skew until the cavity crosses its own outer skin; the boolean then takes
minutes and hands back the wrong solid. A 280° twist against 20 flutes took
**156 CPU-seconds and came back as two solids**. So the template states the
limit as a precondition —

```
abs(twist_deg) * max(lobes, facets, 1) <= 3600
```

— which allows a full turn on ten flutes, two turns on five, and refuses the
combinations that do not build, up front and with a reason. The generator
enforces the same rule by giving up twist rather than detail.

The lofts are ruled, not smooth, for the same reason: a C2 loft through twisted
sections bulges *between* them, and a cavity that bulges through its own outer
skin cuts to nothing — a twisted vase came out solid, 830 cm³ of it. Ruled
bands are also what the slicer sees anyway.

## Printing

This is the shape FDM prints best: no supports, one continuous perimeter, and
the only overhang is whatever the shoulder makes. Two honest caveats:

* **It will not hold water.** The wall is a spiral of beads with a seam. Use a
  test tube or a glass liner for cut flowers, or seal the inside.
* Nothing here has been physically printed. The template records
  `print_test: untested`, and every number above is a measurement of the model.

## Adding a style

Add a set of slider positions to `STYLES` in `formforge/generators/vase.py`.
The jitter, the proportions, the feasibility rules, the CLI and the tests pick
it up: `tests/test_generators.py::TestCatalog` solves every style in the
catalog across its seed range and hands each result to the template's own
validator.
