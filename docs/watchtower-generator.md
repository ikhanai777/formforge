# The watchtower generator

Twelve tabletop towers: a base, storeys that taper and step in as they rise,
galleried decks carried on bracketed skirts, coursed walls with arched and
mullioned windows, and a shingled spire.

```
formforge watchtower --count 8 --seed 42 --style mixed --out out/towers
formforge watchtower --style lighthouse --count 4 --render
formforge watchtower --params-only --count 12       # the sliders, no geometry
formforge watchtower --explain                      # the definition graph
formforge watchtower --style keep --set sides=6 --formats stl,step
```

The machinery -- the dataflow solver, the seed, the jitter, the proportion and
feasibility nodes -- is the one `docs/mushroom-generator.md` describes. This
page is what is different.

## A stack, not a shape

The four generators before this one make **one** body: a mushroom is a cap on a
stem, a vase is a wall, a candle holder is a bored solid. A watchtower is a
*stack*, and that is the whole of what makes it a different problem. The
template builds each part on its own, cuts that part's own openings out of it
while it is still simple, and fuses the lot once at the end:

```
base           a bevelled disc, or the same disc with a broken edge
storey 0..n    a tapered drum, coursed, with its windows and door cut into it
skirt          a cone opening at 45 degrees, with the bracket slots cut up it
deck           a plate on top of the skirt
railing        a ring standing on the deck, with its openings cut through it
eaves          a 45-degree flare out to the roof line
roof           a cone that narrows all the way, shingled
finial         a spike
```

Cutting each part before the fuse is not tidiness, it is arithmetic. A boolean
costs in proportion to how many faces the thing being cut has, and a drum on
its own has a fraction of the faces the finished tower does. Twenty-four
windows cut out of three separate drums is a different price from twenty-four
cut out of one tower.

The sections are **polygons lofted ruled**, not splines. Every section has the
same vertex count in the same order, so each band comes out as a set of flat
quads -- which is both what a faceted tower is made of and the cheapest thing
the mesher can be handed. The default tower is 6,400 triangles and builds in
fourteen seconds; the candle holder, a far simpler object, is 90,000 and takes
fifteen. Splines are what cost.

## The thing that does not scale, again

The candle holder had one dimension that refused to scale, because a tealight
is 39 mm across whatever size holder you put it in. This generator has the
same rule pointing the other way, and it is the more useful half.

Everything *structural* scales with the height. Make a tower two-thirds the
size and it is two-thirds as wide, its roof is two-thirds as tall, its windows
are two-thirds as big. But the **detail** does not scale, because detail is
measured in nozzles:

| Fixed at any size | Why |
| --- | --- |
| `course_mm` ≥ 1.6 | a course shallower than this is a line the slicer drops |
| `course_depth_mm` ≥ 0.3 | relief under a layer height is not relief |
| `window_depth_mm` ≥ 0.8 | one perimeter deep, or the recess is a scratch |
| `gallery_t_mm` ≥ 1.8 | a deck is a floor, and a floor is three layers |
| the mullion, at 1.2 mm | a strip of wall the cut leaves standing |

So a small tower is not a big tower with everything shrunk. It is a tower with
**fewer, coarser courses** -- because the courses keep their millimetre and the
wall has less height to spend on them. That is the difference between a model
that prints and one that comes out as a smooth cone with a faint suggestion of
siding on it.

The counts are the other half of the same idea, and they *do* scale: a bigger
roof gets more shingle courses, a bigger deck more railing openings, a bigger
skirt more brackets. What scales with the size of a thing is how many features
fit on it at a fixed feature size, not how big each one is.

## The styles

| Style | What it is |
| --- | --- |
| `watchman` | the reference: three octagonal storeys, two galleries, a shingled spire |
| `keep` | square, heavily coursed, one battlemented deck under a low dome |
| `lighthouse` | twelve-sided and strongly tapered, with the gallery near the top |
| `lancet` | tall and narrow, gothic windows, a steep spire |
| `pagoda` | four six-sided storeys, wide eaves, a galleried deck at every level |
| `bastion` | squat and six-sided, two rows of loopholes, no gallery at all |
| `spindle` | very tall, very thin, and mostly roof |
| `gatehouse` | one wide storey around a big arched door |
| `belfry` | two storeys under tall mullioned openings |
| `obelisk` | heavily tapered, smooth-roofed, nothing hanging off it |
| `roundhouse` | twelve-sided and squat, galleried at both levels, domed |
| `rookery` | four six-sided storeys of small windows under a tall spire |

## The windows

Every face of every storey carries one window per row, and the window is a
**recess**, not a hole: the tower is solid, so what a cut leaves is a panel set
back into the wall, which is what the reference looks like anyway.

Two things about them are less obvious.

**The mullions come out of the same cut.** A window with two bars is not one
opening plus two added strips; it is a single polygon that dips to the sill
between lights and rises over them, so what the cut leaves standing is wall
that was never touched. One face, one extrusion, one tool, however many bars.
It also means a mullion cannot be thinner than the nozzle by accident -- the
bar is a fixed 1.2 mm and the precondition sizes the window around it, which
is the fix for the first thing the DFM pass found here (0.63 mm mullions,
because the bar was a fraction of the window rather than a width).

**The recess is measured at the head, not the sill.** The wall leans in as it
rises. A recess cut to 1.6 mm at the sill has run out of wall by the top of a
12 mm window on a tower that tapers 16% over its storey, and what you get is a
window that fades out halfway up. Measuring at the head and over-cutting at the
sill costs nothing on a solid tower and is the difference between a window and
a scratch.

## What holds the galleries up

A deck that projects 7 mm past its wall is 7 mm of ceiling for the printer to
bridge, all the way round. The historical answer and the printing answer are
the same one: put a **skirt** under it. The skirt is a cone opening at 45
degrees over exactly the distance the deck reaches, so nothing on it is an
overhang, and the brackets are `bracket_count` slots cut up that cone -- what
stands between them is what looks like a bracket and does its job.

The railing is the same trick once more: a ring standing on the deck with
`rail_gaps` openings cut through it, capped top and bottom so a rail runs
round. `rail_open` at 0.4 is a pierced parapet; at 0.75 it is a row of
balusters. What is left between the openings is a printed wall and gets a
printed wall's floor of 1.4 mm, which is why turning the openness up eventually
takes openings away rather than making posts you cannot print.

## The courses

`course_mm` is the height of one course and `course_depth_mm` how far it stands
proud. Each course **ramps outward over its own height and steps back in at the
course line** -- and that order matters. Real clapboard is the other way round:
proud at the bottom of each board, tapering to flush at the top, which puts a
ledge facing *down* at every course line. Done this way the ledge faces up,
because the material above it is narrower than the material below, and a layer
narrower than the one under it is a layer that is already supported.

The roof shingles are the same thing read upside down: each course stays proud
of the cone it belongs to and steps in at its own line.

## What the range sweep found

Building every parameter at both ends of its declared range turned up four
things the styles alone never would have, and all four are the same shape of
mistake -- a rule stated where it was easy to state rather than where it
binds.

**A slot is not a wedge.** The railing openings and the bracket slots were
straight-sided tools taking a fixed number of millimetres out at every radius
they passed through, while the pitch they were taking them out of grows with
the radius. So at `rail_open` 0.85 the arithmetic said 1.5 mm of post and the
DFM pass measured 0.39, and on a four-sided tower a slot near a corner left a
wedge that tapered away to nothing. Both are cut as wedges now, bounded by two
planes through the axis, so what they leave is the same fraction of the pitch
all the way through. The precondition is still stated on the outside, where it
is legible, at 1.4 rather than 1.2 -- because the narrowest point is at the
inside of the ring, about a tenth less.

**An opening is sized against its own face.** The schema states the window
width against `width_mm`, which is the plinth. A storey that has stepped in
three times is a great deal narrower, and at `storey_step` 0.35 the top
storey's windows ran off the sides of their faces: eight slivers where the
corners used to be. The template now sizes each opening against the face it is
being cut into and drops mullions that no longer fit.

**A tool face on a solid's face leaves slivers.** The bracket slots started
exactly on the drum's face plane, which is the degenerate case for a boolean.
They start inside it now; the drum is fused on afterwards and fills it back.

**Twenty-six windows, not forty.** The cost ceiling was a guess. Twenty-four
window cuts build in fourteen seconds and thirty-six do not build at all, so
the ceiling now sits where the sandbox's thirty seconds run out. A twelve-sided
tower gets two storeys of windows rather than three.

## Printing

It stands on its base with no support anywhere. Every surface on it either
narrows going up, leans out at 45 degrees or less, or is a bridge no longer
than a window is wide. All three roof shapes -- `spire`, `bell`, `dome` --
narrow the whole way, which is the one case a printer never has to be told
about.

The one thing here that is not a print consideration is the grass on the
reference's base. That is flocking, not geometry: at 0.4 mm a nozzle cannot
make a blade of grass, and a `rock` base with a broken edge is the honest
version of the same idea.

## Adding a style

One entry in `STYLES` in `formforge/generators/watchtower.py`, one line in
`STYLE_NOTE`, and it appears in the CLI, in `--explain`, and -- because
`web/studio.html` generates its preset list from `STYLES` -- as a chip in the
studio. `tests/test_generators.py` builds every style at every variation, so a
style that cannot be made feasible fails there rather than in someone's slicer.
