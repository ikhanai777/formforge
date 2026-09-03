# The mushroom generator

A generator that produces variations of a detailed mushroom and exports each one
as STL, built on the same principles a Grasshopper definition is built on.

```
formforge mushroom --count 8 --seed 42 --species mixed --out out/mushrooms
formforge mushroom --explain                       # print the definition graph
formforge mushroom --params-only --count 12        # the sliders, no geometry
formforge mushroom --species parasol --set cap_d_mm=90 --render
formforge mushroom --count 4 --formats step        # CAD only, no meshes kept
```

Every run writes three files per specimen -- `.stl` to print, `.step` to edit,
`.3mf` because it declares its own units -- plus a `variations.json` carrying
the parameter set, the bounding box and the DFM verdict for each, so a
population is reproducible from its manifest alone. `--formats` narrows that;
the STL is always written because the DFM verdict is measured on the mesh.

The STEP is the one that matters if the model is going anywhere near CAD. It is
a B-rep, not a mesh: a default toadstool arrives in SolidWorks as **one solid
with 198 faces** -- 169 planes for the gill blades, 15 spheres for the warts, 8
cones, 3 surfaces of revolution and 3 B-splines for the cap and stem. You can
select an edge and fillet it. For a single specimen with the full
bundle -- 3MF with its units declared, STEP, the standalone `source.py`, the
report and the previews -- hand any of those parameter sets to `formforge build
nature_mushroom --set cap_d_mm=70 --set seed=42`.

## The front end

`web/studio.html` is a single file with no build step and no server: open it in
a browser and you have the definition with a face on it.

* every parameter in the template, grouped by the part of the mushroom it
  belongs to, stopping exactly where the schema stops
* the model rebuilt live as you drag, in about 15,000 triangles
* the seven species as chips, each drawn from its own preset's silhouette, and
  a **Shuffle** that applies the same jitter, proportion and feasibility rules
  the Python generator applies
* the template's preconditions checked as you move -- "it topples: the cap leans
  past its own footprint", with the fix, before the kernel ever sees it
* **Download STL** for the preview mesh, and the exact `formforge build` command
  for the model on screen
* **STEP script** -- a `.py` carrying the template's own source with your slider
  positions bound in. `pip install build123d && python mushroom.py` writes a
  `mushroom.step` with the same analytic faces the CLI produces. STEP is a solid
  model and a browser has no kernel to make one, so the page hands over the
  thing that does rather than a faceted mesh with a `.step` extension

The page carries a port of the geometry, not a call to it: `hash01` is the same
expression, so the wart scatter and the lobe irregularity you see are the ones
the kernel will build for that seed. What the browser cannot do is the boolean
union, so the export is a set of closed shells that a slicer unions at slice
time rather than one B-rep solid, and there is no STEP, no 3MF and no DFM
report. That is what the command is for.

Two tests in `tests/test_generators.py::TestStudioPage` keep the page honest:
one asserts its parameters, ranges and defaults are the template's own, the
other that the source it embeds is the template's source byte for byte -- a
stale copy would export a script that builds a different mushroom from the one
on screen.

## Where Grasshopper's ideas land

Grasshopper is not a scripting language with a picture on top. What makes a
definition behave the way it does is a small number of rules, and each of them
has a home here.

| Grasshopper | Here |
| --- | --- |
| Number sliders | `param_schema` in `formforge/templates/nature_mushroom.yaml`, each with a range, a step and a label |
| Value list | `SPECIES` in `formforge/generators/mushroom.py`: seven sets of slider positions |
| Random component with a seed | the `draws` node: a seed in, a stable stream of unit numbers out |
| Expression boxes | the `proportioned` and `feasible` nodes, which keep the numbers in proportion and inside what the geometry accepts |
| The canvas: components wired into a DAG, solved in dependency order | `formforge/generators/graph.py`, about a hundred lines |
| Geometry components (curve, revolve, loft, boolean) | the build123d script inside the template |
| Data flowing one way, no hidden state | a solve is a pure function of the inputs; same seed, same mushroom |
| Bake to mesh | the sandbox build, then `export_stl` |

The split between the two halves is deliberate. The definition decides where the
sliders go; the template turns slider positions into surfaces. It is also
enforced: geometry runs in an isolated sandbox that may not import anything from
FormForge, so the geometry half is a standalone script by construction — the
`source.py` in a bundle runs in any checkout of build123d.

## The definition

```
species -----> preset ------\
seed --------> draws --------> jittered --> proportioned --> feasible --> params
variation ------------------/
```

* **preset** — the value list. `mixed` picks a species from the seed.
* **draws** — one stable draw per parameter *name* (blake2b of the seed and the
  name, not `random`), so adding a slider later does not reshuffle every
  mushroom that came before it.
* **jittered** — moves the free sliders off the preset by up to their own range
  in `JITTER`, scaled by `--variation`.
* **proportioned** — the expression box. A mushroom whose cap grew 16% while its
  stem shrank 18% is not a variation, it is a different mushroom badly drawn, so
  the dependent sliders (stem height and diameter, gill count and depth, wart
  count and size, ring width) are recomputed from the species' own ratios
  against the cap that came out of the jitter.
* **feasible** — enforces every one of the template's preconditions, so a
  variation is never refused by the geometry it was built for.
* **params** — clamps to the template schema, which is the single authority on
  what the geometry has actually been built and swept across.

The proportion node also spends a budget. Every gill and every wart is a solid
in one boolean union, and each one costs more on a bigger cap, so gill and wart
counts are capped by `1000 / cap_d` and `1600 / cap_d` as well as by the
circumference. A 90 mm cap therefore gets coarser gills than a 60 mm one — and
builds inside the sandbox's 30 CPU-second ceiling instead of being killed at it,
which is the difference between a coarser specimen and no specimen.

`--set key=value` pins a slider. Pins land in `preset`, before the proportion
node, so pinning a 100 mm cap grows the stem, the gill count and the warts with
it. Feasibility still outranks a pin: a pinned value the geometry cannot build
is moved, rather than being sent to the kernel to fail.

## The geometry

`nature_mushroom` is one parametric definition, in eight stages:

1. **Cap profile.** A meridian curve, `(1 - s^fullness)^shoulder`, which covers
   the whole family in two sliders: 2.0/0.5 is a hemisphere, 1.0/1.0 a cone,
   4.0/0.4 a parasol. A dish term drops the centre below the rim to make a
   funnel; an umbo term adds the central boss. The underside is the same curve
   offset **along its own normal**, not vertically, so a steep margin keeps its
   thickness instead of thinning to nothing. Both curves are resampled by arc
   length before they become splines, because a spline through evenly spaced
   points overshoots wherever the curve is steep and the cap margin is a cliff.
2. **Gill zone.** The underside is flattened to a cone between the stem and the
   margin — a `Line` in the meridian profile rather than two points on a spline,
   so the revolve produces an analytic cone. It runs 2 mm wider than the blades
   at each end, so no blade ends exactly where the surface changes type. Both
   details are about cost: a blade meeting an analytic cone well inside its
   edges is a third cheaper to fuse than the same blade meeting a revolved
   spline at a tangency, and with 36 blades that difference is most of the
   build.
3. **Lobed margin.** The cap is trimmed by a wavy cone, each lobe's depth drawn
   from the seed. The cone matters: a vertical prism meets the underside at
   about 30 degrees and leaves a feather edge all the way round the margin —
   3% of the surface under the 0.8 mm a nozzle can extrude, and a hard DFM
   failure. A cone opening at 32 degrees meets it square.
4. **Underside.** Full-length gills alternating with lamellulae — the short
   gills that start halfway out — as a polar array of extruded blades; or
   concentric grooving that stands in for a bolete's pore layer, because a real
   pore is smaller than a 0.4 mm nozzle can print; or nothing.
5. **Stem.** Circles lofted along a leaning axis, with a basal bulb, a taper
   (negative widens upward, as a bolete does) and a flare where it meets the cap.
6. **Ring.** A straight-sided flange revolved around the stem, clamped below the
   cap so it is never swallowed by it. It was a spline through three points
   until the DFM check measured 0.35 mm where the spline pinched between them.
7. **Warts.** Spheres scattered over the crown by the seed, sunk so that only
   `wart_flatten` of each is proud. Sunk spheres rather than scaled ones: a
   non-uniform scale turns an analytic sphere into a spline surface and roughly
   triples what the union costs. A wart that would be sunk deeper than the flesh
   is thick is shrunk, or it comes out as a pimple on the gills.
8. **Union.** The cap assembly is seated on the stem and everything is fused in
   a single boolean, which is several times faster than fusing three dozen
   blades one at a time.

## Printing

The model needs supports. The gills hang from a near-horizontal roof and no
orientation fixes that — this is a figurine, and the honest answer is supports
under the cap rather than a flattened shape that prints unaided. The defaults
keep the blades at 1.3 mm (three perimeters at a 0.4 mm nozzle) so they survive
support removal, and the base is flat with the bulb giving it a footprint.

The DFM suite is what forced three of the design decisions above: the ring was a
spline until the wall check measured 0.35 mm where it pinched, the margin trim
was a vertical prism until the feather edge it left came out at 0.09 mm across
3% of the surface, and the cap underside is a normal offset rather than a
vertical one because a vertical offset thins to nothing on a steep skirt.

It also settled where the sliders stop. Sweeping the whole declared range
(`python -m formforge.eval.check_templates --id nature_mushroom --extremes`)
found two combinations that build cleanly and then fall over: a 62 mm cap on a
6 mm stem, and a 15 mm lean on a stem whose bulb is too narrow to sit under it.
Neither is a geometry bug, so neither is fixed in the geometry — the template
states the relationship as a precondition (`stem_lean_mm * 0.75 <
stem_d_mm / 2 * (1 + stem_bulb)`) and refuses the combination up front, and the
generator widens the bulb before it shortens the lean, because the lean is most
of what makes a specimen look grown.

Nothing in this repository has been physically printed. The template records
that as `print_test: untested`, and every claim above is a measurement of the
model, not of a print.

## A second generator

`docs/vase-generator.md` covers the vase definition, which uses this same
solver, seed, jitter, proportion and feasibility machinery and differs only in
its domain. What the two have in common is named in
`formforge/generators/__init__.py` as the `Generator` catalog entry -- a
definition, a template, and a variant to start from, called a species here and
a style there -- which is what lets one CLI command and one parametrised test
cover both.

## Adding a species

Add a set of slider positions to `SPECIES`. Nothing else changes: the jitter,
the proportions, the feasibility rules and the CLI all pick it up, and
`tests/test_generators.py` will solve it across its whole seed range and check
every result against the template's schema and preconditions.
