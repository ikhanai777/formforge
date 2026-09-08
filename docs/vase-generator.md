# The vase generator

Twenty-one silhouettes, four surface treatments and nine floral reliefs from one
definition, exported as STL to print and STEP to edit.

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
There is a second vase generator alongside this one:
`docs/sculpt-vase-generator.md` builds its surface out of solid blades standing
on the wall rather than out of the wall itself.

## The front end

`web/studio.html`, the **Vase** tab: the same page the mushroom and the
sculptural vase live in, with the right-hand column rebuilt from this
template's schema. Open the file in a
browser -- no server, no build step -- and you have

* the twenty-one styles as chips, each drawn from its own silhouette
* every slider the template declares, grouped as silhouette, surface, relief and
  wall
* the vase rebuilt as you drag, as one closed shell rather than a solid with a
  cavity cut from it: outer skin, rim, cavity, floor. That is what makes the
  material readout exact rather than an estimate -- 80.4 cm3 on screen against
  79.9 cm3 out of the kernel for the spiral, a 0.6% difference that is the rim
  and the cavity overshoot, not a guess
* the preconditions checked as you move: the 45 degree rule, twist times detail,
  a cavity that does not fit inside the narrowest diameter
* **Download STL**, and **STEP script** -- a `.py` carrying this template's own
  source with your slider positions bound in, which `pip install build123d &&
  python vase.py` turns into a `vase.step` you can open in SolidWorks
* the exact `formforge build vessel_vase --set ...` command for what is on
  screen

The geometry is a port, not a call: the profile is the same Fritsch-Carlson
curve and the resolution rules are the template's own, so a twisted vase looks
banded on screen because the ruled loft the kernel builds is banded -- measuring
the crest radius up the kernel's own STL finds the same 1-2 mm of it. The
preview subdivides those bands, which costs nothing in accuracy: every point
between two sections of a ruled loft is a straight blend of them.

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
| `vineyard` | an urn with a vine running round its belly |
| `laurelled` | a laurel spray up a tall amphora |
| `fernery` | ferns the whole way up a straight-sided column |
| `posy` | open blossoms under a wide tulip mouth |
| `rosette` | coiled roses round the shoulders of a bottle |
| `fleur` | lilies on eight soft facets |
| `damascene` | an ogee damask covering the wall |
| `trellised` | a leafy lattice, corner to corner |
| `artichoke` | imbricated petals, the way a pinecone is built |

They are not twenty-one sizes of one vase: each is a set of positions for the
same sliders, and `--style mixed` draws one per seed.

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

## The relief

The last nine styles wear a **floral relief**, and it is a different kind of
thing from the four above: not a shape applied to the radius but a *field*.
`emboss_at(angle, t)` answers one question -- how far does the surface stand
proud here -- and every section is drawn through points that each ask it. That
is what makes nine motifs cost the same as one.

| Motif | What it is |
| --- | --- |
| `vine` | a stem that wanders, with a leaf on the outside of each bend |
| `laurel` | a swaying spray, leaves alternating up it |
| `fern` | a rachis with pinnae that shorten as they rise |
| `blossom` | five petals seen face on, with a boss in the middle |
| `rose` | a coiled bud: one ridge spiralling out from the centre |
| `lily` | three petals rising out of a tie band -- the fleur |
| `damask` | the ogee, with a flower held inside it |
| `trellis` | a leafy lattice, a leaf in each opening |
| `scale` | imbricated petals, the way a pinecone is built |

`emboss_lo` and `emboss_hi` put the band where you want it, from a frieze round
the belly to a covering over the whole wall. `emboss_count` is repeats around
and `emboss_rows` repeats up, and neither is a size: a flower is cast at the
size a flower is cast at, so the generator scales the *counts* with the vase
and leaves `emboss_mm` exactly where the style put it.

The relief goes on the **outer skin alone**. It is raised, never sunk, so it can
only add material to the outside: the cavity is lofted from the plain
silhouette, the wall under a crest comes out thicker rather than thinner, and
the inside of the vase stays smooth enough to clean. It is also why the relief
needs no printability rule of its own -- the steepest surface a motif can make
is its own flank, and `emboss_sharp` is the exponent that sets that angle.

### Two numbers here are measured, and both were surprises

**The exponent stops at 1.8.** An interpolating spline drawn through a narrow
raised bump undershoots on both sides of it, and how far it undershoots depends
on the *shape* of the bump and not at all on how densely it is sampled: a stroke
a tenth of a repeat wide at exponent 2.6 loses nearly seven per cent of the
section's area whether it is drawn through twenty-two samples per repeat or
forty-four. Under 1.8, with every stroke at least fifteen hundredths of a repeat
wide, the loss is under one per cent.

**Every running element sways.** This is the stranger one. A ridge that runs
straight up the vase at a fixed angle is the single thing a ruled loft between
spline sections cannot follow. The surface between two sections is matched by
curve *parameter* rather than by angle, and a ridge that never moves pins that
parameterisation in a way that makes the band between two nearly identical
sections enclose a fifth less than it should. A laurel with a straight stem came
back with a tenth of the vase missing; the same laurel with a stem that sways by
fourteen hundredths of a repeat over a row comes back exact. Nothing else helps
-- not more points round, not more bands up, not a periodic spline, not a
smoother field. The rule lives in the source as `SWAY_MIN`, and the acceptance
test for a new motif is to build it twice, once with spline sections and once
with polyline, and check the two volumes agree.

The two directions are not symmetric, and knowing which is which is what makes
the budget spendable. **Points round are correctness** -- the section is a
spline, so too few points and the curve overshoots inward through the wall.
**Bands up are quality** -- the loft between sections is ruled, so too few bands
and the flower is merely blocky. When the surface budget binds, the bands give
way and the points never do.

### How much of this is allowed to be left

Comparing the two lofts is the acceptance test for a motif, and the number it
has to come in under is worth stating carefully, because the first bar set for
it was too tight and for the wrong reason. Measured across all nine motifs, as
what the relief adds over the same vase with `emboss = none`:

| repeats | bands | worst motif |
| --- | --- | --- |
| 6 | 64 | blossom, +1.06% |
| 7 | 56 | damask, +1.16% |
| 8 | 49 | damask, +2.10% |
| 9 | 43 | vine, +2.05% |

The trend is not the repeats themselves -- it is the bands the budget takes away
to pay for them. And the right ceiling is not "as near nothing as possible": a
2% shortfall on a 75 cm3 shell is about 0.03 mm of wall spread over the whole
surface, which is a fraction of a layer and well under the tessellation
tolerance. What the test is actually guarding against is the skin *collapsing*
somewhere -- the straight-stemmed laurel that came back 10% short, which the DFM
pass then caught as thin walls. So the ceiling is 2.5%, and the DFM pass remains
the thing that catches a collapse; this test catches it earlier and says why.

## Printing the relief

No segment of the silhouette may change radius faster than **45 degrees**, in
either direction, and the template says so as a precondition. Flaring out that
fast is an overhang; closing in that fast is a bridge across the mouth. The
range sweep is what put the rule there: a 190 mm neck under a 66 mm rim built
happily and then failed the DFM check on a 25 mm unsupported span, and a 200 mm
rim came out with a 0.15 mm edge. Neither is a geometry bug -- both are shapes
that need supports, on the one object that otherwise never does.

## The wall

The cavity is a second loft of the same skin, inset perpendicular to the
surface rather than radially: on a sloped shoulder a purely radial inset is
thinner than it looks by the cosine of the slope. It starts at `base_mm` and
runs past the rim, so one boolean gives both the wall and the mouth.
`rim_band_mm` thickens the top 10 mm into a lip that survives handling.

It prints in vase mode as it is: the slicer follows the outer surface and lays
one bead of whatever width it is set to, whatever the model's wall says. The
perpendicular compensation is capped at 2.5x for the same reason the 45 degree
rule exists -- on a near-horizontal flare it runs away and insets the cavity
past the far wall, which is how a 200 mm rim came out 0.15 mm thick.

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
* Height stops at 240 mm because that is the build volume, not the geometry:
  260 mm builds fine and then fails the DFM check against a 250 mm Z.
* Nothing here has been physically printed. The template records
  `print_test: untested`, and every number above is a measurement of the model.

## Adding a style

Add a set of slider positions to `STYLES` in `formforge/generators/vase.py`.
The jitter, the proportions, the feasibility rules, the CLI and the tests pick
it up: `tests/test_generators.py::TestCatalog` solves every style in the
catalog across its seed range and hands each result to the template's own
validator.

The other generators on the same solver are covered by
`docs/mushroom-generator.md`, `docs/vase-generator.md`,
`docs/sculpt-vase-generator.md`, `docs/candle-holder-generator.md` and
`docs/watchtower-generator.md`.
