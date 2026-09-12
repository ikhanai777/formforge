# Drawing templates and sheet formats

DrawingForge does not ship a template. It cannot: a title block is a company's
own, and a generated drawing carrying someone else's block is worse than no
drawing.

What it needs from yours, and what it does without one.

## Without a template

The add-in falls back to the SOLIDWORKS default drawing template
(Tools ▸ Options ▸ File Locations ▸ Document Templates). Everything works —
views, dimensions, notes, the projection symbol, tables — but the sheet will
carry whatever title block that template has, and the fields the add-in writes
will only show up if that block is linked to custom properties.

## What a template needs to get the full benefit

### 1. A title block linked to custom properties, not typed text

The add-in writes these onto the **drawing** document. Link the title block
notes to them with `$PRP:"Name"`:

| Property | Contents |
|---|---|
| `PartNo` | Part number, from the model or its file name |
| `Description` | From the model's properties |
| `Revision` | From the model, or the run's default |
| `Material` | From the model's material, or its `Material` property |
| `Finish` | From the model's `Finish`/`Treatment` property |
| `Weight` | Mass, in lb or kg to match the standard |
| `DrawnBy` | From the run options |
| `DrawnDate` | Run date |
| `Company` | From the run options |
| `Scale` | Sheet scale as a ratio, e.g. `1:2` |
| `SheetSize` | `B`, `A3`, and so on |
| `SheetCount` | Number of sheets |
| `DrawingStandard` | e.g. `ASME Y14.5-2018` |
| `ProjectionAngle` | `THIRD ANGLE` or `FIRST ANGLE` |
| `Units` | `INCH` or `MM` |
| `GeneralTolerance` | The general tolerance line |
| `Configuration` | Configuration the views reference |

Any of these can be remapped to a different model property name in the options,
under the title block mapping.

If the title block is typed text rather than links, the add-in still writes the
properties — they just will not appear on the sheet, and the fabrication check
notes it (DF031).

### 2. Room in the bottom band

The layout reserves a full-width band along the bottom of the drawable area:
the title block on the right, the general notes to its left. Views never enter
it. The band is as tall as the title block (55 mm for ISO sizes, about 2.3–2.6
in for ASME), so a title block much taller than that eats into the space the
views get.

If the notes area comes out too small to be readable the run says so in its
diagnostics, and the note block may need moving by hand.

### 3. A projection symbol placeholder, if you want a nicer one

The add-in draws its own projection symbol near the title block and labels it in
words. If your sheet format already has a proper one, switch the add-in's off in
the options — the note block still states the convention either way.

## Setting the template in the add-in

Options ▸ Standard & sheet ▸ Drawing template, and optionally a sheet format
(`.slddrt`) to apply to every sheet. Leave both blank to use the SOLIDWORKS
defaults.

## A starting point

If you have no template at all, the quickest honest route is:

1. Make a drawing from the SOLIDWORKS default template at the sheet size you use
   most.
2. Edit the sheet format and replace the title block's text fields with links to
   the property names above.
3. Save the sheet format (`.slddrt`) and save the drawing as a template
   (`.drwdot`).
4. Point the add-in at both.

That is fifteen minutes of work and it is the difference between drawings that
need a pass by hand and drawings that do not.
