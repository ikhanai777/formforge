# How DrawingForge is put together

## The one decision everything else follows from

**The drawing is designed by a library that has never heard of SOLIDWORKS, and
executed by a thin layer that does nothing but make API calls.**

```
model ──► PartInspector ──► PartSummary ──► DrawingPlanner ──► DrawingPlan
                                                                    │
                                                                    ▼
                                                             DrawingBuilder
                                                                    │
                                                                    ▼
                                            SOLIDWORKS ──► drawing ──► FabricationChecklist
```

`PartSummary` and `DrawingPlan` are plain objects. Everything that makes a
drawing correct or wrong — which face is the front, what scale, which sheet,
which views, where they go, what the notes say — happens between them, in
`DrawingForge.Core`, which has no dependency on SOLIDWORKS or on anything else.

That is why 99 unit tests can assert that a first-angle drawing puts the top
view below the front view, that no view ever lands on the title block, and that
no scale off the standard's ladder is ever produced — on a machine with no CAD
software on it.

The alternative, which is how most drawing automation is written, is to make API
calls in the order a drafter would and hope. That code cannot be tested, and its
bugs are found by a machinist.

## The two assemblies

### DrawingForge.Core (netstandard2.0, no dependencies)

| Area | What lives there |
|---|---|
| `Options` | `DrawingOptions`, the enums, XML persistence |
| `Standards` | Sheet catalogue, per-standard profiles, scale ladders, the note composer |
| `Geometry` | Bounding boxes, sheet rectangles, unit conversion |
| `Planning` | Front view choice, layout, the plan model, the planner |
| `Naming` | File name patterns and sanitisation |
| `Reporting` | Run outcomes, the fabrication checklist, JSON and text reports |
| `Logging` | A log interface and three implementations |

Zero package references, on purpose. This assembly is loaded into the
SOLIDWORKS process, and every dependency there is a version conflict waiting to
happen. Settings use `XmlSerializer` from the framework; the report writer is a
120-line JSON emitter rather than a library.

### DrawingForge.AddIn (net48, references the SOLIDWORKS interop)

| Area | What lives there |
|---|---|
| `SwAddin.cs` | The COM entry point, the command group, the callbacks |
| `Registration` | The registry keys SOLIDWORKS reads to find the add-in |
| `Interop` | `SwDispatch` (version-tolerant calls), `SwDoc` (document helpers), `SwConst` |
| `Build` | Traversal, inspection, sheet setup, views, dimensions, annotations, tables, export, the batch runner |
| `Ui` | Three WinForms dialogs, laid out in code |

## The flow of one part

1. **`AssemblyTraverser`** walks the assembly, drops what nobody details
   (suppressed, envelopes, Toolbox), and collapses repeated instances into one
   drawing each.
2. **`PartInspector`** reads one model into a `PartSummary`: bounding box, mass,
   material, properties, holes, sheet metal and weldment status, whether it has
   internal features, whether it is a body of revolution.
3. **`DrawingPlanner`** designs the drawing. `FrontViewChooser` picks the
   principal view, `ScaleSelector` picks the ratio, `ViewLayoutPlanner` places
   everything, `NoteBlockComposer` writes the notes, `SheetLayout` guarantees
   nothing lands on the title block.
4. **`DrawingBuilder`** executes the plan: create the document, set the sheet up
   (projection **before** any projected view), create views parents-first,
   measure what was actually drawn, correct the scale if it overran, dimension,
   annotate, insert tables, write the title block, export.
5. **`FabricationChecklist`** compares the plan, the summary and what actually
   happened, and produces findings.
6. **`BatchRunner`** does that for every part, isolating each failure, and
   returns a `BatchReport`.

## Plan, then measure

The planner works from estimates. Some of them are good (a bounding box is
exact); some cannot be (a flat pattern's true extents are not readable without
editing the user's model).

So after the views exist, `DrawingBuilder.CorrectScaleToFit` reads each view's
real outline back with `IView::GetOutline`, and if the set overruns the border
it steps the sheet scale down the ladder and moves everything with it — scaling
about the centre of the view area, which preserves the shared centrelines that
projected views depend on. Up to three steps, then it reports and stops.

This is the part that turns "a plausible layout" into "a layout that fits", and
it is the reason the add-in can be honest about estimating rather than having to
guess perfectly.

## Failure is a first-class outcome

Three rules, and they are what makes a 300-part run usable:

1. **One part's failure never takes the batch down.** Each part is isolated; its
   failure is a row in the report, and the run continues unless the user asked
   otherwise.
2. **A degraded operation is reported, not swallowed.** If a section view could
   not be created, the cutting line is left on the sheet and the failure is
   named. If hole callouts are unavailable, the count comes back zero and the
   checklist raises it against the drawing.
3. **The gap between "drawn" and "ready to make" is made visible.** That is what
   `FabricationChecklist` is: fifteen rules, each one something that would
   otherwise trigger a phone call from the shop. A drawing with no material, no
   note block, no flat pattern on a sheet metal part, or no BOM on an assembly
   is reported as not releasable rather than counted as a success.

The results dialog sorts failures and error findings to the top, because a batch
of forty where thirty-eight are clean is only useful if the two that are not are
the first thing you see.

## What the checklist cannot claim

Counting dimensions cannot prove a part is fully defined — that needs a solver
over the model's constraint set, which SOLIDWORKS does not expose. So DF011 is a
warning that names its own limits ("read it as a smell, not a verdict") rather
than an assertion of completeness. Several other rules are similar. Where a rule
is a heuristic, it says so in its own remedy text, on the drawing's report,
where the person deciding whether to issue it will read it.
