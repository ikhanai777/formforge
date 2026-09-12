# SOLIDWORKS API notes

## The problem this design solves

The SOLIDWORKS API versions methods by suffix. `InsertModelAnnotations`,
`InsertModelAnnotations2`, `InsertModelAnnotations3` are the same operation
across three release generations; `AutoBalloon5` supersedes `AutoBalloon4`,
which supersedes `AutoBalloon3`. Argument lists change with the suffix, and
older suffixes are eventually removed.

An add-in that binds all of that at compile time builds against exactly one
SOLIDWORKS version and fails to load on every other. So DrawingForge splits its
API use in two.

## Bound at compile time

Only API that has been stable across every supported release. This is the
complete list — it is mirrored in
`tests/DrawingForge.AddIn.CompileCheck/Stubs/SolidWorksInteropStubs.cs`, which
is what lets the add-in's source be compiled on a machine with no SOLIDWORKS:

| Interface | Members used |
|---|---|
| `ISldWorks` | `ActiveDoc`, `OpenDoc6`, `ActivateDoc3`, `CloseDoc`, `NewDocument`, `GetUserPreferenceStringValue`, `SetUserPreferenceToggle`, `GetCommandManager`, `SetAddinCallbackInfo` |
| `ICommandManager` | `CreateCommandGroup2`, `RemoveCommandGroup` |
| `ICommandGroup` | `AddCommandItem2`, `HasToolbar`, `HasMenu`, `Activate` |
| `ModelDoc2` | `Extension`, `GetPathName`, `GetTitle`, `ClearSelection2` |
| `ModelDocExtension` | `SelectByID2`, `SaveAs` |
| `Component2` | `Name2`, `ReferencedConfiguration`, `GetPathName` |
| `ISwAddin` | `ConnectToSW`, `DisconnectFromSW` |
| `DrawingDoc`, `View`, `AssemblyDoc`, `PartDoc`, `CustomPropertyManager` | as types only, for casts |

Everything else is called through `SwDispatch`.

## Called through SwDispatch

`SwDispatch` tries each known name, newest first, and each known argument shape,
taking whichever the installed release actually has. A name that is not present
is a logged debug line and a fallback; a method that exists and throws is a
logged warning and a reported failure — the two are distinguished, because
"this release does not have it" and "this release rejected what we asked" need
different responses.

Reflection also writes by-ref arguments back into the array it is given, which
is how the out-parameter forms below are read without binding to one release's
signature.

| Operation | Names tried | Notes |
|---|---|---|
| Document type | `GetType` | Late-bound on purpose: `IModelDoc2.GetType` collides with `object.GetType`, and which one a direct call resolves to depends on the static type of the variable. Falls back to the file extension. |
| Custom property read | `Get6`, `Get5`, `Get4`, `Get3`, `Get2`, `Get` | Each generation adds an out parameter. The resolved value is preferred over the raw one so `$PRP:` expressions come back evaluated. |
| Custom property manager | `CustomPropertyManager[config]` | A parameterised COM property, reached with `GetProperty` plus arguments. |
| Custom property write | `Add3`, `Add2` | |
| Material name | `GetMaterialPropertyName2`, `GetMaterialPropertyName` | Database name comes back as an out parameter. |
| Mass | `CreateMassProperty2`, `CreateMassProperty` then `.Mass` | Reported in kilograms; converted to grams on the way in. |
| Bounding box | `GetPartBox`, `GetBox`, then a union of `GetBodyBox` over `GetBodies2` | Metres in, millimetres out. |
| Feature walk | `FirstFeature`, `GetNextFeature`, `GetTypeName2`/`GetTypeName`, `GetDefinition` | |
| Sheet setup | `SetupSheet6`, `SetupSheet5`, `SetupSheet4`, then `ISheet.SetProperties2`/`SetProperties` | Three argument shapes each. **The projection angle is the fifth-from-last argument and it is a bool: true means first angle.** Getting this wrong mirrors the drawing, so it is the one call worth checking by hand against your release. |
| New sheet | `NewSheet4`, `NewSheet3`, `NewSheet2` | |
| Named view | `CreateDrawViewFromModelView3`, `CreateDrawViewFromModelView2` | Position in metres from the sheet's lower-left corner. |
| Projected view | `CreateUnfoldedViewAt3`, `CreateUnfoldedViewAt2` | Which orthographic view comes out is decided by the drop point relative to the parent **and** the sheet's projection setting, which is why the sheet is configured first. |
| Section view | `CreateSectionViewAt5`, `CreateSectionViewAt4`, `CreateSectionViewAt3` | Needs a sketched cutting line on the parent view first. Three argument shapes are tried. If none is accepted, the cutting line is left on the sheet and the failure is reported, so the section can be finished by hand. |
| Detail view | `CreateDetailViewAt4`, `CreateDetailViewAt3`, `CreateDetailViewAt2` | Needs a sketched circle first. |
| Flat pattern | `CreateFlatPatternViewFromModelView3`/`2`/`1` | |
| View scale | property `ScaleRatio`, else `ScaleDecimal` | With `UseSheetScale` 0/1. |
| View rotation | property `Angle` | Radians, counter-clockwise. |
| View display | property `DisplayMode`, else `SetDisplayMode4`/`3`/`2` | |
| View position | property `Position`, else `SetPosition` | `double[2]` in metres. |
| View outline | `GetOutline` | `double[4]` in metres. This is how the builder finds out what SOLIDWORKS really drew. |
| Model items | `InsertModelAnnotations3`, `InsertModelAnnotations2` | Three argument shapes. |
| Dimension identity | `GetDimension2`/`GetDimension`, then `FullName`/`Name` | The full name is stable across views, which is what duplicate detection needs. |
| Dimension arrangement | `AlignDimensions` | Falls back to the add-in's own push-clear-of-geometry pass. |
| Dimension precision | `SetPrecision3`/`2`/`1` | |
| Delete an annotation | `Select3`/`Select2`/`Select` then `EditDelete` | |
| Hole callouts | `InsertHoleCallout2`, `InsertHoleCallout` | Counted by diffing the view's annotations before and after. |
| Notes | `InsertNote`, then `GetAnnotation().SetPosition2`/`SetPosition` | |
| Sketch geometry | `CreateLine`, `CreateCircleByRadius`/`CreateCircle`, `InsertSketch` | Used for cutting lines, detail circles and the projection symbol. |
| BOM | `IView.InsertBomTable4`/`3`/`2`, then `IDrawingDoc.InsertBomTable4`/`3`/`2`/`1` | Hangs off the view in newer releases and the drawing in older ones, so both hosts are tried. |
| Balloons | `CreateAutoBalloonOptions` + `AutoBalloon5`/`6`, else `AutoBalloon4`/`3`/`2` | |
| Cut list | `IView.InsertWeldmentTable2`/`1`, then `IDrawingDoc.InsertWeldmentTableAnnotation2`/`1` | |
| Bend table | `IView.InsertBendTable2`/`1`, then `IDrawingDoc.InsertBendTableAnnotation2`/`1` | |
| Revision table | `InsertRevisionTable2`, `InsertRevisionTable` | |
| Hole table | `InsertHoleTable2`, `InsertHoleTable` | |
| Flat pattern DXF | `ExportToDWG2`, `ExportToDWG` | |
| Open document walk | `GetFirstDocument2`/`GetFirstDocument`, `GetNext`/`GetNext2` | Used to reuse an already-open model rather than opening a second copy. |
| Auto-insert toggles | `swUserPreferenceToggle_e` members resolved by **name** at runtime | `swDetailingCenterMarksHoles`, `...Arcs`, `...Slots`, `swDetailingCenterLines`, `swDetailingDowelSymbol`. Resolved by name so a release that renumbers or drops one loses that single setting rather than failing the build. |

## Enumeration ordinals

`Interop/SwConst.cs` holds the ordinals the add-in passes. They come from the
published documentation. The ones worth re-checking against a target release
before a production run:

* `swDwgPaperSizes_e` — the sheet-size ordinals in `SheetCatalog`. Every sheet
  call also passes an explicit width and height, so a wrong ordinal degrades to
  a wrongly *named* sheet of the right size rather than a wrong sheet.
* `swDetailingStandard_e` — ANSI 1, ISO 2, DIN 3, JIS 4, BSI 5, GOST 6.
* `swInsertAnnotation_e` — the model-items bitmask.
* `swUserPreferenceIntegerValue_e.swDetailingDimensionStandard` (68) and the
  unit preferences (49, 51).

## Threading

The work runs on the thread that opened the progress dialog, which is the thread
SOLIDWORKS called the command callback on. The dialog stays responsive because
messages are pumped between steps rather than because the work is on a worker
thread. The SOLIDWORKS API is apartment-threaded: calling it from a background
thread marshals every call across apartments, which is slower at best and
deadlocks against SOLIDWORKS' own modal dialogs at worst.

## COM lifetime

Every interface handle the add-in takes and does not hand back is released
through `SwDispatch.Release`, which swallows the usual release-time exceptions.
A batch of several hundred parts leaks SOLIDWORKS handles otherwise, and the
symptom — SOLIDWORKS refusing to close cleanly after a long run — is hard to
trace back to its cause.

## What has not been verified

The add-in has been compiled against the stub interfaces above and its planning
half is covered by unit tests. **It has not been run against SOLIDWORKS.** The
first run on a workstation should be a single part with the fabrication check
on, and the run log read end to end: every dispatch fallback and every degraded
operation is logged with the names it tried.
