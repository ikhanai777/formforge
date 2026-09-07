# The cup holder generator

Twelve sleeves a cup drops into: a turned body with a spreading foot, a tapered
socket bored down from the rim, three horizontal registers of cast relief
divided by bead mouldings, framed panels round the body, and one or two cast
brackets at the sides.

```
formforge cupholder --count 8 --seed 42 --style mixed --out out/holders
formforge cupholder --style podstakannik --count 4 --render
formforge cupholder --params-only --count 12      # the sliders, no geometry
formforge cupholder --explain                     # the definition graph
formforge cupholder --style doric --set emboss_count=16 --formats stl,step
```

The machinery -- the dataflow solver, the seed, the jitter, the proportion and
feasibility nodes -- is the one `docs/mushroom-generator.md` describes. This
page is what is different.

## Ornament is a field, not a shape

Every generator before this one builds its detail out of *geometry*: a rib is a
lofted section, a window is a cut, a shingle course is a band of the profile.
This one builds its detail out of a **function**. `relief_at(angle, t)` answers
one question -- how far does the ornament stand proud here -- and the section at
height `t` is drawn through a hundred-odd points each of which asks it.

That is what makes ten motifs cost the same as one. `rope`, `guilloche`,
`lattice`, `scallop`, `eggdart` and `acanthus` are six lines of arithmetic each
over a cell of the pattern, `u` running across one repeat around and `v` up one
row of the register, and the machinery that turns them into a solid does not
know or care which one it is drawing.

It also settles the printing question before it is asked. Every motif returns a
number that is *added* to the radius, so the ornament is always raised, never
sunk, and the worst overhang anything on the surface can make is its own flank.
The `emboss_sharp` slider is the exponent on the cosine that makes the flank,
which makes it the overhang slider as well as the taste one.

Three things about the field are not obvious, and all three came out of things
that broke:

**The exponent has a floor of 1.** Below 1 a raised cosine meets the wall with
infinite slope, and a spline drawn through a hundred and fifty points that each
sit on such a corner overshoots into itself. The first build of the default came
out as a hundred and thirty-one disconnected slivers rather than a cup.

**A leaf never narrows to nothing.** `acanthus` tapers its lobe as it rises;
left unbounded the lobe becomes narrower than the sampling and what the spline
reconstructs is a spike. The half-width has a floor of 0.06 for the same reason
the exponent has a floor of 1.

**The seed turns a register; it does not vary a repeat.** The obvious use of a
seed here is to make each acanthus leaf slightly different. That puts a *step*
in the field at every cell boundary, and a step in the field is a corner in the
section. The seed sets a phase per register instead, which is a rotation and not
a discontinuity.

## The two rules the styles are built on

The candle holder next door has one rule that does not scale: a tealight is
39 mm across whatever size holder you put it in. This generator inherits it
wholesale -- **the socket is set by the cup** -- and adds the one the watchtower
states the other way round:

> the relief is millimetres; the repeats are millimetres of arc.

Make a holder for a 92 mm tankard rather than a 70 mm tea glass and it does not
get bigger acanthus leaves, it gets *more* of them at the same size, because a
leaf is cast at the size a leaf is cast at. So `emboss_count` and `panels` are
counts and follow the circumference; `emboss_mm`, `bead_mm`, `panel_frame` and
`wall_mm` are measured against the nozzle and stay exactly where the style put
them. The foot and the handle are parts of the body and follow its height.

## The styles

| Style | What it is |
| --- | --- |
| `podstakannik` | the tea-glass holder: tall, open-bottomed, one big pierced handle |
| `victorian` | panelled cartouches between rope and egg-and-dart, a handle each side |
| `doric` | a fluted column with a heavy foot and nothing hanging off it |
| `mug` | wide and plain, a dentil cornice and one comfortable handle |
| `sleeve` | open at both ends, one rope band -- the one to print for a paper cup |
| `chalice` | a tall spreading foot under a bellied bowl of shells |
| `lattice` | a diagonal lattice the whole way up between bead mouldings |
| `nautical` | two heavy rope bands round a plain body |
| `tankard` | squat, wide and beaded, with a big loop handle |
| `shell` | scallops in framed panels over a bellied body |
| `caddy` | tall and straight, guilloche between dentils -- for pens as much as cups |
| `laurel` | narrow bands top and bottom, two small ears, a soft waist |

## The budget is memory, not triangles

Every generator here has a cost ceiling and every ceiling before this one was a
*time* ceiling -- what the sandbox's thirty seconds of CPU would buy. This one
is a **memory** ceiling, and the difference is worth stating because it inverts
which knob matters.

The loft is nearly free: 460 MB, most of which is the interpreter. What is
expensive is every boolean *after* it, because each one has to intersect its
tool with the whole ornamented skin. Measured at the default resolution:

| Step | Peak resident |
| --- | --- |
| after the loft | 463 MB |
| after boring the socket | 884 MB |
| after fusing one handle | 1,531 MB |

Against a sandbox with two gigabytes of address space, the third row is a
segfault -- which is what the first build of the default was, for a week of
wrong guesses about self-intersecting lofts before the fault handler pointed at
`_bool_op` and running the same source without the limit finished in six
seconds.

What the memory scales with is **points round the section times sections up**,
and points cost more per unit than sections do:

| Points × sections | Peak |
| --- | --- |
| 80 × 42 | 1,051 MB |
| 96 × 42 | 1,180 MB |
| 120 × 42 | 1,531 MB |
| 96 × 72 | 1,418 MB |
| 96 × 96 | 1,867 MB |

So the template caps points at 96, spends six of them per repeat, and states the
ceiling as a precondition on the product -- 4,600 when there is a handle to
fuse, 7,000 when there is not. That is the whole reason `emboss_count` stops at
16 rather than 36, and the reason `_feasible` spends its budget in the order
rows, then repeats, then the handle: a register with two rows of a motif reads
much like one with three, and a cup holder without a handle does not read like
a cup holder at all.

Making the loft non-ruled, which would give the booleans one big surface to
intersect rather than forty ruled bands, makes it **worse** by a factor of three
(4,220 MB, 78 seconds). It was worth measuring; it is not worth doing.

## The handle

A round loop is the obvious shape and would need support under the whole of its
lower flank. This one is a cast bracket: out at 45 degrees, straight up, and
back in at 45 -- 45 out being the steepest overhang a printer never has to be
told about, and 45 back in not being an overhang at all. The opening is the same
outline inset by the web, so the corners stay at 45 whatever `handle_pierce`
does.

Two things about how it meets the body:

**It is buried, not laid against.** A plate that only reaches the surface
touches a curve tangentially and the fuse leaves knife edges along the join.

**The burial follows the body.** The first version buried a straight vertical
edge deep enough to bite at the waist, which is deep enough to cross the *socket*
at the rim -- and a plate that pokes into the bore hands the fuse a solid that
laps its own cavity. The inner edge is a chain now, staying a fixed depth under
the silhouette and a fixed clearance outside the socket, whichever of the two
binds at that height.

## The waist is not free

The socket runs straight down inside the body while the outside pinches in, so
the waist is where the wall is thinnest -- and at the default a waist of 18% of
a 42 mm rim radius is 7.6 mm out of a 3.2 mm wall. The first version that built
cleanly had 0.08 mm walls where the bore had simply broken out through the side.

The fix is not a precondition, because a precondition would have to refuse a
perfectly reasonable pair of sliders. The source takes the waist as far as the
wall allows and no further, and the section clamps it again in case the curve
*between* control points dips below what the control points promised.

## Printing

It stands on its foot with no support. The foot spreads on a cone rather than a
disc, the relief is raised rather than sunk, the handle is a 45-degree bracket
rather than a loop, and the one horizontal face inside is the floor the cup
stands on. Setting `floor_mm` to zero makes it a sleeve open at both ends, which
is the version to print for a paper cup.

## Adding a style

One entry in `STYLES` in `formforge/generators/cupholder.py`, one line in
`STYLE_NOTE`, and it appears in the CLI, in `--explain`, and -- because
`web/studio.html` generates its preset list from `STYLES` -- as a chip in the
studio. `tests/test_generators.py` builds every style at every variation, so a
style that cannot be made feasible fails there rather than in someone's slicer.
