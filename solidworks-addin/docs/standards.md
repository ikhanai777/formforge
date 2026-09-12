# What the standards decide, and what DrawingForge does about it

This file is the record of every drafting decision the add-in makes on its own.
If a generated drawing does something you disagree with, the reason is here.

## Projection convention

| Standard | Default | Spec |
|---|---|---|
| ASME | Third angle | ASME Y14.3 |
| ISO, DIN, BSI, GOST | First angle | ISO 128-30 |
| JIS | First angle | JIS B 0001 |

This is the single most consequential setting on the sheet. First and third
angle differ by which side of the front view the top and side views go, and a
drawing read in the wrong convention produces a mirrored part. So:

* The sheet's projection is set **before** any projected view is created, because
  SOLIDWORKS derives which orthographic view a projection produces from the
  sheet's setting combined with where the view is dropped.
* The convention is stated in the note block in words.
* A projection symbol is drawn on the sheet, near the title block.
* If neither the symbol nor the wording could be placed, the fabrication check
  raises it (DF041).

The user can override the default per run. The override is honoured everywhere:
the sheet setting, the layout, the note and the symbol all follow it.

## Sheet sizes

ASME Y14.1 inch series and ISO 5457 A series, in millimetres internally:

| ASME | mm | ISO | mm |
|---|---|---|---|
| A | 279.4 x 215.9 | A4 | 297 x 210 |
| B | 431.8 x 279.4 | A3 | 420 x 297 |
| C | 558.8 x 431.8 | A2 | 594 x 420 |
| D | 863.6 x 558.8 | A1 | 841 x 594 |
| E | 1117.6 x 863.6 | A0 | 1189 x 841 |
| F | 1016 x 711.2 | | |

Automatic selection walks the series of the chosen standard from smallest up and
stops at the first sheet that holds the view set at a scale no smaller than
1:5 (configurable). An ASME user is never offered A3; an ISO user is never
offered a B sheet.

Borders follow each series: 0.38–1.0 in for ASME depending on size, a uniform
20 mm for ISO (ISO 5457 asks for 20 mm on the filing edge and 10 mm elsewhere;
a uniform 20 mm is the conservative reading).

## Scales

Only preferred ratios are used. ASME Y14.1 allows halving and decimal steps;
ISO 5455 is 1, 2, 5 and decades of them.

* ASME: … 5:1, 4:1, 2:1, 1:1, 1:2, 1:4, 1:5, 1:8, 1:10, 1:16, 1:20 …
* ISO: … 5:1, 2:1, 1:1, 1:2, 1:5, 1:10, 1:20, 1:50 …

The planner takes the largest ratio at which the whole view block fits, with
28% of the drawable area held back for dimensions and leaders. Enlargement is
capped (default 10:1) so a 2 mm pin does not end up at 100:1, and reduction is
capped (default 1:100).

A scale that is not on the ladder is never produced by the planner, and one
entered by hand is flagged in the options dialog before the run starts.

## The view set

| Case | Views |
|---|---|
| General prismatic part | Principal, top, side, isometric |
| Body of revolution | Principal (axis horizontal), side, isometric — the top view only repeats the diameters |
| Has internal features | The above plus a full section |
| Smallest feature below the threshold | The above plus an enlarged detail view |
| Sheet metal | The above plus a flat pattern, by default on its own sheet |
| Assembly | One orthographic view plus an isometric, exploded if a state is saved, with a BOM and balloons |

### Which face is the front

In order of weight:

1. The front view shows the largest face — the viewing direction runs along the
   model's shortest extent.
2. A body of revolution is drawn with its axis horizontal. This beats rule 1,
   because a turned part dimensioned from a circular view is unusable on a lathe.
   Where no named view puts the axis across the sheet, the view is rotated 90°.
3. Between equally good orientations, the model's own front wins. Designers
   orient models deliberately.
4. Back, bottom and left views are last resorts.

### What is never dimensioned

The isometric view. It exists to make the part readable, and dimensions on a
pictorial view are a defect, not a bonus.

The flat pattern gets hole callouts and centre marks but not model dimensions:
importing model items onto a flat pattern produces formed-state numbers, which
are wrong for the flat.

## The note block

Every drawing carries it, because it says the things no dimension can:

1. The dimensioning standard in force.
2. The unit.
3. The projection convention.
4. General tolerances for untoleranced dimensions — ASME's decimal-place block,
   or ISO 2768-mK.
5. Edge break.
6. Surface finish.
7. Material.
8. Finish, when set.
9. Sheet metal thickness, bend count and the "flat pattern is reference only"
   caveat, when the part is sheet metal.
10. Welding spec and "dimensions apply after welding", when the part is a weldment.
11. Cleanliness and part marking.

Notes with no value are dropped before numbering, so the numbering never has
gaps. User notes are appended and can use tokens — `{PartNumber}`, `{Material}`,
`{Company}` and so on.

A drawing without this block is not releasable, and the fabrication check treats
a missing one as an error (DF040).

## General tolerances

| Standard | Metric | Inch |
|---|---|---|
| ASME | X ±0.5, X.X ±0.25, X.XX ±0.13, angles ±0°30' | .X ±.1, .XX ±.03, .XXX ±.010, angles ±0°30' |
| ISO/DIN/BSI | ISO 2768-mK | falls back to the ASME decimal block |
| JIS | JIS B 0405-m | as above |
| GOST | GOST 30893.1-m | as above |

These are defaults chosen to be defensible, not universal. A shop with its own
tolerance block should put it in the sheet format's title block and can switch
the general-tolerance note off, or override it through the extra notes.

## What the add-in will not decide for you

* **Geometric tolerancing.** GD&T frames are imported from the model if they
  exist (DimXpert or annotations); none are invented. A datum scheme is an
  engineering decision, not a layout one.
* **Which dimensions matter.** Model dimensions marked for drawing are imported.
  If a model has none marked, the drawing comes out bare and the check says so
  (DF010) rather than the add-in guessing at a dimensioning scheme.
* **Fits and surface finishes on specific features.** Those belong on the model.

The honest summary: the add-in produces a correctly laid out, correctly
annotated, correctly scaled drawing with everything the model already knows. It
does not replace the engineering judgement about what to control. The
fabrication check exists to make the gap between those two visible on every
drawing instead of letting it reach the shop.
