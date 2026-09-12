# DrawingForge

A SOLIDWORKS add-in that turns an assembly into a folder of fabrication
drawings — one per part, with the views, dimensions, notes and tables a shop
needs, in the drafting standard you pick.

```
Open ASM-9000.SLDASM  ▸  DrawingForge ▸ Generate Drawings...

  BRK-1001   created   B  1:1   14 dims   clean
  SHF-2002   created   B  1:2    9 dims   clean
  PNL-3003   created   B  1:2   11 dims   1 warn   (no bend table)
  HSG-4004   created   C  1:1   22 dims   clean
  ASM-9000   created   C  1:5    BOM + 12 balloons

  5 drawing(s) created, 0 failed, 0 skipped in 47.2s.
  4 released clean; 0 error finding(s), 1 warning(s).
```

## Status, plainly

The planning half — everything that decides what a correct drawing looks like —
is written, compiles, and is covered by **99 passing unit tests**. The
SOLIDWORKS half compiles cleanly against a declared stub of the API surface it
binds.

**It has not been run against SOLIDWORKS.** No part of this has touched a CAD
workstation. The architecture is built around that fact — see
[What to expect on the first run](#what-to-expect-on-the-first-run) — but do not
mistake "carefully built and tested where it can be tested" for "proven in a
shop". The first run should be one part, with the fabrication check on, and the
log read end to end.

## What it does

**For every part in the assembly**

- Picks the front view the way a drafter would: largest face to the reader, a
  turned part's axis horizontal (rotating the view 90° where no standard
  orientation manages it), the model's own orientation kept when it is as good.
- Lays out the front, top, side and isometric views in first or third angle,
  aligned, at a preferred scale, on the smallest sheet that holds them.
- Adds a full section when the part has internal features, an enlarged detail
  view when a feature is too small to dimension in place.
- Imports model dimensions, removes the ones repeated on another view, pushes
  any that landed on the geometry clear of it.
- Adds centre marks, centrelines and hole callouts.
- Writes the general note block: tolerancing standard, unit, projection
  convention, general tolerances, edge break, surface finish, material, part
  marking — plus sheet metal, weldment and tapped-hole notes where they apply.
- Draws the projection symbol and states the convention in words.
- Fills the title block from the model's properties.

**For sheet metal**: a flat pattern view on its own sheet, with bend notes, a
bend table, and a thickness-and-bend-count note. Flat pattern DXF export for
nesting.

**For weldments**: the cut list table.

**For the assembly itself**: an isometric — exploded if a state is saved — with
a bill of materials and balloons.

**Then it checks its own work.** Fifteen fabrication-readiness rules, each one
something that would otherwise become a phone call from the shop: no dimensions,
no material, no note block, no flat pattern on a sheet metal part, no BOM on an
assembly, tapped holes without callouts, views overlapping the title block. The
results are sorted worst-first, so the two drawings out of forty that need a
human are the first thing on screen.

**Exports** `.SLDDRW`, PDF, DWG, DXF, flat-pattern DXF and STEP, and writes a
JSON run report next to the drawings.

## Standards

Pick the standard and the add-in follows it — projection convention, sheet
series, scale ladder, tolerance block, detailing standard on the document.

| | Default projection | Sheets | Scales |
|---|---|---|---|
| ASME Y14.5 | Third angle | A–F | Y14.1 preferred |
| ISO / DIN / BSI / GOST | First angle | A4–A0 | ISO 5455 preferred |
| JIS | First angle | A4–A0 | ISO 5455 preferred |

The projection angle can be overridden per run and is honoured everywhere: the
sheet setting, the layout, the note and the symbol. Sheet size can be automatic
(smallest that fits), fixed, or a custom size. Scale can be automatic or fixed —
and a fixed scale that is not on the standard's ladder is flagged before the run
starts, not after.

[docs/standards.md](docs/standards.md) is the full record of every drafting
decision the add-in makes on its own, including the ones it deliberately does
not make for you.

## Requirements

- SOLIDWORKS 2018 or newer, 64-bit
- .NET Framework 4.8
- Visual Studio 2019 or newer, or MSBuild, to build

## Build and install

On a workstation with SOLIDWORKS:

```powershell
msbuild DrawingForge.sln /p:Configuration=Release
# for a non-default install location:
msbuild DrawingForge.sln /p:Configuration=Release /p:SolidWorksDir="D:\SW2024"

# then, from an elevated PowerShell
.\install.ps1
```

`install.ps1` runs `regasm /codebase`, which invokes the add-in's own
registration code to write the two registry keys SOLIDWORKS looks for. Start
SOLIDWORKS; DrawingForge appears under Tools ▸ Add-Ins and adds its own toolbar.

To remove it: `.\install.ps1 -Unregister`.

## Verifying without SOLIDWORKS

```bash
./check.sh
```

Builds the planning library, runs its 99 tests, and compile-checks the add-in's
source against
`tests/DrawingForge.AddIn.CompileCheck/Stubs/SolidWorksInteropStubs.cs`.

That last project deserves an explanation, because it is unusual. It compiles
the add-in's real source against a hand-declared stub of the SOLIDWORKS
interfaces, so that syntax, typing and dependency errors are caught on any
machine. It proves the add-in is internally consistent. It does **not** prove
the signatures match a real release — they are the add-in's own claims about the
API, not a copy of it. The stub file says so at the top, at length.

## Use

1. Open the assembly (or a single part).
2. **DrawingForge ▸ Options** — standard, projection, sheet, template, output
   folder, what to export.
3. **DrawingForge ▸ Generate Drawings** for everything, or **Draw Active
   Document** for just what is open.
4. Read the results dialog. Failures and error findings are at the top.

A drawing template with a title block linked to custom properties is worth
fifteen minutes of setup — see [templates/README.md](templates/README.md) for
the property names and why the bottom band of the sheet matters.

## How it is built

The short version: **the drawing is designed by a library that has never heard
of SOLIDWORKS, and executed by a thin layer that does nothing but make API
calls.**

```
model ─► PartInspector ─► PartSummary ─► DrawingPlanner ─► DrawingPlan
                                                               │
                                                               ▼
                                    SOLIDWORKS ◄── DrawingBuilder
                                          │
                                          ▼
                                    FabricationChecklist ─► report
```

`PartSummary` and `DrawingPlan` are plain objects. Every decision that makes a
drawing right or wrong happens between them, in a library with no dependency on
anything. That is why the test suite can assert that a first-angle drawing puts
the top view below the front view, that no view ever lands on the title block,
and that no scale off the standard's ladder is ever produced — without CAD
software.

Two more things worth knowing:

**Plan, then measure.** The planner works from estimates, some of which cannot
be exact (a flat pattern's true extents are not readable without editing the
user's model). So after the views exist, their real outlines are read back with
`IView::GetOutline`, and if the set overruns the border the scale steps down the
ladder and everything moves with it — scaled about the centre of the view area,
which preserves the shared centrelines projected views depend on.

**The API is called by name, not by signature.** SOLIDWORKS versions methods by
suffix — `InsertModelAnnotations3`, `AutoBalloon5`, `CreateSectionViewAt5` — and
binding those at compile time means the add-in builds against exactly one
release and loads on none of the others. Only a short list of genuinely stable
API is bound; everything else goes through a dispatcher that tries each known
name and shape and logs precisely what it found.
[docs/api-notes.md](docs/api-notes.md) lists every one of them.

## Known limits

- **Not yet run against SOLIDWORKS.** Said once at the top; it is the important
  one.
- **Hole detection is feature-based.** Hole Wizard holes are read properly. A
  hole cut with a plain extruded cut is not recognised as a hole, so it gets a
  dimension rather than a callout.
- **"Has internal features" is inferred** from the feature list — shells,
  cavities, blind cuts, ribs. A false positive costs an extra section view; a
  false negative costs a section the drafter adds by hand.
- **Flat pattern extents are estimated** from the formed body, then corrected
  from the drawn view. Reading the true flat would mean unsuppressing the flat
  pattern feature, which edits the user's model.
- **Toolbox detection is path-based.** A library kept somewhere unusual will not
  be recognised; turn the filter off in that case.
- **Dimension count cannot prove a part is fully defined.** DF011 is a warning
  that says so in its own remedy text rather than an assertion of completeness.
- **No GD&T is invented.** Frames already on the model are imported; a datum
  scheme is an engineering decision.

## What to expect on the first run

The design assumes the first contact with a real SOLIDWORKS will find
signature mismatches. That is what the dispatcher, the logging and the
fabrication check are for:

- Every dispatch fallback is logged with the names and shapes it tried, so a
  mismatch names itself.
- A degraded operation is reported, not swallowed — a section view that could
  not be created leaves its cutting line on the sheet and says so.
- The fabrication check runs against what actually happened, not what was
  planned, so anything that silently did not land shows up as a finding.

Run one part first. Read the log. `docs/api-notes.md` has a per-call table for
looking up whatever the log names.

## Layout

```
src/DrawingForge.Core/      the planning library — no SOLIDWORKS, no packages
src/DrawingForge.AddIn/     the COM add-in, the API layer, the dialogs
tests/DrawingForge.Core.Tests/        99 tests over the planning library
tests/DrawingForge.AddIn.CompileCheck/ compiles the add-in against API stubs
docs/standards.md           every drafting decision and why
docs/api-notes.md           every SOLIDWORKS call and how it is bound
docs/design.md              how the pieces fit
templates/README.md         what a drawing template needs to provide
```
