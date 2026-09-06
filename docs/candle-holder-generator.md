# The candle holder generator

Twelve holders for a tealight or a pillar: a turned body with a bored socket in
its top, turned round a plan that can be a heart, a snowflake, a scallop or a
polygon, wearing ribs or rows of bubbles, pierced into a lantern, and with a
crescent moon, an arch or a tablet standing behind the flame.

```
formforge holder --count 8 --seed 42 --style mixed --out out/holder
formforge holder --style moon --count 4 --render
formforge holder --params-only --count 12          # the sliders, no geometry
formforge holder --explain                         # the definition graph
formforge holder --style lantern --set pierce_count=10 --formats stl,step
```

The machinery -- the dataflow solver, the seed, the jitter, the proportion and
feasibility nodes -- is the one `docs/mushroom-generator.md` describes. This
page is what is different.

## The thing that does not scale

The three generators before this one make objects whose every dimension is a
ratio of one other dimension: a vase that comes out 14% taller comes out 14%
wider, and nothing is worse for it. A candle holder is the first model here
with a *job*, and the job fixes a number. A metal-cup tealight is 39 mm across
whatever size holder you put it in.

So `socket_d_mm` is deliberately absent from the proportion node. The body
grows; the socket does not. And when the body has grown in a direction that
stops the socket fitting -- a squash, a deep star, a thicker wall -- the
feasibility node widens the *holder* rather than narrowing the hole, in this
order: make it wider, then flatten the plan, and only if neither is allowed to
move does the candle get smaller. A tealight holder a tealight does not fit in
is not a smaller holder, it is a failed one.

## The styles

| Style | What it is |
| --- | --- |
| `heart` | a ribbed heart, squat, for one tealight |
| `bubble` | scalloped in plan and blown into rows of bubbles |
| `flake` | a flat six-armed star with the cup sunk into the middle |
| `moon` | a low dish with a crescent moon standing behind the flame |
| `lantern` | a square pierced box -- the flame shows through the windows |
| `flute` | the plain round one, finely fluted the whole way up |
| `bloom` | eight broad petals with a light reeding over them |
| `pillar` | tall and ribbed, bored deep for a pillar candle |
| `tower` | a hexagonal pierced tower |
| `arcade` | a reeded dish under an open arch |
| `plaque` | an octagonal dish with a plain tablet behind it |
| `reef` | rows of uneven bubbles on a lifted, rounded body |

## The plan

The body is a surface of revolution only when `plan` is `round`. Otherwise it
is turned round an outline:

| `plan` | What it draws |
| --- | --- |
| `round` | a circle |
| `heart` | the classic polar heart -- a cleft at the back, a point at the front |
| `star` | `plan_points` narrow arms out of a wide valley, like a snowflake |
| `petal` | the same count as broad scallops |
| `polygon` | a rounded `plan_points`-gon |

Each outline is normalised so its own peak is 1 and then blended toward a circle
by `plan_depth`. Written that way the slider is continuous across the choice:
`plan_depth` at 0 is a circle whatever `plan` says, so switching outlines never
jumps the shape, and turning the depth up is one continuous move from a circle
to the thing itself.

The ceiling on `plan_depth` is 0.9, not 1. The sections are splines, and a
spline driven all the way into the heart's cusp or a polygon's vertex overshoots
it -- which the kernel then reports as a solid with no volume rather than as a
sharp corner.

**The valley is what the socket has to fit inside.** This is the second domain
rule, and it is the one that surprises: a six-armed star 138 mm across has a
*valley* between the arms at 58% of that, and a 40.5 mm tealight plus two 3.5 mm
walls has to live inside the valley, not inside the arms. `plan_low` in
`formforge/generators/holder.py` computes it, and it is what turns "a snowflake
tealight holder" into a width the geometry can actually carry.

## The ribs

`rib_count` ribs stand `rib_mm` off the body, each filling `rib_width` of the
gap to the next -- 0.2 is a fine reed with a wide flat between, 0.95 a pleated
surface with barely a valley -- and `rib_sharp` runs one from a soft swell to a
reed with steep sides.

`rib_rows` is the slider that makes a second family out of the same machinery.
At 1 each rib runs the whole height, which is a fluted holder. Above 1 the rib
breaks into that many stacked cosine bumps, which is a holder that looks blown
out of bubbles. `reef` and `bubble` are that slider; `flute` and `heart` are the
same code with it left at 1.

Both cases are the same printability rule written twice. A feature that arrives
at full depth over one layer is a ledge the printer has to bridge. A single
column fades in over the first eighth of the height; a stack of bumps rises over
half a row. So:

```
rib_mm <= height_mm * 0.12 * rib_rows        # the fade at the ends
rib_mm * 2 * rib_rows <= height_mm           # half a row, for a stack
```

## The windows

`pierce_count` windows cut clean through the wall, and that is the whole of what
turns this definition into a lantern: a body with a socket bored nearly its full
height, with holes in the sides for the light. `tower` and `lantern` are that.

Two things about them are less obvious than they look.

**They are pointed, not round.** The top of a round hole is a ceiling the printer
has to bridge. The top of these is two faces at 45 degrees, which it simply
prints -- so a pierced lantern needs no supports either.

**They are wedges, not slots.** Each window is lofted between a narrow face at
the bore and a wide one out past the wall, so it takes the same *fraction of the
pitch* at every radius it passes through. The first version cut a straight
prism of fixed width instead, and where the plan is turning -- the corner of a
polygon, the tip of a star -- what a straight cut leaves beside itself is a
wedge that tapers to nothing. The DFM pass found it at 0.32 mm. That is not a
thin wall you can ask the printer to try harder at; it is a sliver, and the fix
is in the shape of the cutter rather than in a warning.

The pillar between two windows is then a constant fraction of the pitch, and it
is checked at both ends of its run: against the width outside, and against the
socket diameter at the bore, where the same fraction is fewer millimetres.

**They start at the bore's floor.** A window lower than that is not a window at
all: it is a blind slot driven into the solid core, which is a different thing
to look at and a far more expensive thing to cut. The range sweep found it as a
timeout at sixteen windows on the default dish, and the same dish takes sixteen
in twelve seconds once they are lifted to where the cavity starts. The template
lifts them; the generator bores deeper instead, so that a lantern gets the
windows it asked for rather than a band of them near the rim.

Two more things about them are about cost rather than shape. A dozen windows are
subtracted in **one** boolean with every tool at once, not fused into a union
and then subtracted: fusing a dozen disjoint solids and cutting with the result
is two general booleans where the kernel offers one, and the union is the
expensive half -- the twelve-window `tower` went over the CPU limit that way and
builds comfortably this way. And a plan that comes to a point does not go with a
wall that is cut through, so `plan_depth` is capped at 0.6 wherever
`pierce_count` is set: that is the other half of the sliver above, the half the
wedge does not fix.

## The standing back

`back` puts an upright behind the flame: a `crescent` moon, an `arch` frame with
a hole through it, or a plain `tablet`. It is one extruded outline, pushed as
far back inside the footprint as it will go and fused into the body.

A plate standing on edge has vertical walls, so the only overhang anywhere on it
is the one the crescent's own bottom makes -- and that one is worth the
arithmetic. A circle standing on its lowest point opens outward faster than 45
degrees until it is `1 - 1/sqrt(2)` of its radius up, which is 0.293. So the body
has to be deep enough to bury that much of the moon, and the precondition says
so:

```
back == "crescent" implies height_mm >= back_h_mm * 0.15
```

0.15 rather than 0.1465 because the parameters are rounded to two decimals on
the way out. A 132 mm moon therefore wants a body at least 20 mm deep, which is
what `moon` is.

## What the geometry costs, and why the schema stops where it does

The sandbox gives one build 30 CPU-seconds and two gigabytes. The ribs are what
spend it, and the shape of the spending was measured rather than guessed --
two sweeps, one on a plain 88 mm round body and one on a 128 mm heart:

| ribs | round, 88 mm | heart, 128 mm |
| --- | --- | --- |
| 10 | 4.7 s, 10,100 triangles | -- |
| 16 | -- | 6.0 s, 23,200 |
| 18 | 5.8 s, 26,600 | -- |
| 24 | -- | 8.3 s, 49,000 |
| 26 | 8.7 s, 54,100 | -- |
| 32 | -- | 12.8 s, 98,000 |
| 34 | 13.7 s, 90,900 | -- |
| 40 | -- | over the CPU limit |

Two things in that table decided the schema.

The first is that **the count dominates and the size barely matters**. A 128 mm
heart and an 88 mm circle produce almost the same triangle count at the same
number of ribs, and triangles come out near enough as the square of the count.
That is why the ceiling on `rib_count` is 40 -- a little past where a build
lands on the CPU limit -- and why the mesh rule carries a floor term:

```
rib_count * max(2 * rib_mm / arc, 0.6) * (1 + 2 * oval) * (1 + plan_depth) <= 40
```

A shallow rib is cheaper than a deep one but it is not free: the mesher still
has to follow every crest. The finned vase's rule has no such floor, because at
fourteen ribs it never needed one. This shape goes to forty and does.

The second is that the plan is in the rule at all. A heart or a star needs more
points round the section before a rib is drawn on it, and the ribs are then paid
for on top of those. Forty ribs on a circle build; forty on a heart do not.

The feasibility node solves the same inequality at 38 rather than 40, because
`rib_mm` is rounded to two decimals on the way out and a load sitting exactly on
the limit rounds past it.

Memory is the other ceiling, and a standing back moves it. A back is a second
solid fused onto the first and both are in memory while that runs, so the
section budget drops from 5,000 points to 3,600 when there is one. `arcade` --
thirty-six ribs under an arch -- is what found that: it died with a segfault
out of the sandbox's two gigabytes at the full budget and builds at the
reduced one.

## Printing

It stands on its base with no support. The body never opens outward faster than
45 degrees, the socket is a flat-bottomed bore so the one horizontal face inside
is the floor the candle sits on, and the ribs and windows carry their own 45
degree rules above.

**On flames.** Nothing here is rated for one. PLA softens around 60 °C and a
real wick 3 mm from a printed wall is a puddle, whatever the wall is made of.
These are holders for battery tealights, or moulds and masters for something
that is not a thermoplastic. The template says so in its own `print_test`
rationale and the DFM pass will not tell you, because DFM checks whether a
shape can be printed, not whether it should be set on fire.

## Adding a style

One entry in `STYLES` in `formforge/generators/holder.py`, one line in
`STYLE_NOTE`, and it appears in the CLI, in `--explain`, and -- because
`web/studio.html` generates its preset list from `STYLES` -- as a chip in the
studio. `tests/test_generators.py` builds every style at every variation, so a
style that cannot be made feasible fails there rather than in someone's slicer.

The other generators on the same solver are covered by
`docs/mushroom-generator.md`, `docs/vase-generator.md`,
`docs/sculpt-vase-generator.md`, `docs/candle-holder-generator.md` and
`docs/watchtower-generator.md`.
