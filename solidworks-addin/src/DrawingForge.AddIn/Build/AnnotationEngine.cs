using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using DrawingForge.AddIn.Interop;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Logging;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;
using DrawingForge.Core.Reporting;
using DrawingForge.Core.Standards;
using SolidWorks.Interop.sldworks;

namespace DrawingForge.AddIn.Build
{
    /// <summary>
    /// Everything on the sheet that is not a view or a dimension: centre marks,
    /// centrelines, hole callouts, the general note block and the projection
    /// symbol.
    /// </summary>
    internal sealed class AnnotationEngine
    {
        private readonly ISldWorks _app;
        private readonly DrawingDoc _drawing;
        private readonly ModelDoc2 _drawingModel;
        private readonly DrawingOptions _options;
        private readonly ILog _log;

        public AnnotationEngine(ISldWorks app, DrawingDoc drawing, ModelDoc2 drawingModel,
                                DrawingOptions options, ILog log)
        {
            _app = app;
            _drawing = drawing;
            _drawingModel = drawingModel;
            _options = options;
            _log = log;
        }

        /// <summary>
        /// Turns on the document settings that make SOLIDWORKS add centre marks
        /// and centrelines as each view is created.
        /// </summary>
        /// <remarks>
        /// Doing it through the auto-insert toggles rather than after the fact is
        /// both more reliable and closer to how a drafter sets a template up:
        /// the marks land attached to the right entities instead of being
        /// scattered by a post-processing pass.
        /// </remarks>
        public void ConfigureAutoInsert()
        {
            SetToggle("swDetailingCenterMarksHoles", _options.InsertCenterMarks);
            SetToggle("swDetailingCenterMarksArcs", _options.InsertCenterMarks);
            SetToggle("swDetailingCenterMarksSlots", _options.InsertCenterMarks);
            SetToggle("swDetailingCenterLines", _options.InsertCenterlines);
            SetToggle("swDetailingDowelSymbol", false);
        }

        /// <summary>
        /// Sets one auto-insert toggle by its documented name.
        /// </summary>
        /// <remarks>
        /// The toggle ordinals are resolved from the swconst enum at runtime so
        /// that a release which renumbers or drops one fails this single setting
        /// rather than the build.
        /// </remarks>
        private void SetToggle(string toggleName, bool value)
        {
            int ordinal;
            if (!TryResolveEnum("swUserPreferenceToggle_e", toggleName, out ordinal))
            {
                _log.Debug("Preference " + toggleName + " is not available in this release.");
                return;
            }

            try { _app.SetUserPreferenceToggle(ordinal, value); }
            catch (System.Runtime.InteropServices.COMException ex)
            {
                _log.Debug("Setting " + toggleName + " failed: " + LogExtensions.Describe(ex));
            }
        }

        /// <summary>Looks an enum member up by name in the loaded swconst assembly.</summary>
        private static bool TryResolveEnum(string enumTypeName, string memberName, out int value)
        {
            value = 0;
            try
            {
                Type type = Type.GetType("SolidWorks.Interop.swconst." + enumTypeName +
                                         ", SolidWorks.Interop.swconst");
                if (type == null || !type.IsEnum) return false;
                if (!Enum.IsDefined(type, memberName)) return false;
                value = Convert.ToInt32(Enum.Parse(type, memberName), CultureInfo.InvariantCulture);
                return true;
            }
            catch (ArgumentException) { return false; }
            catch (System.IO.FileNotFoundException) { return false; }
            catch (BadImageFormatException) { return false; }
        }

        /// <summary>
        /// Adds centre marks, centrelines and hole callouts to a view, counting
        /// what actually landed.
        /// </summary>
        public void Annotate(View view, ViewPlan plan, ViewOutcome outcome, ViewCreator creator)
        {
            if (view == null) return;

            if (plan.InsertCenterMarks) outcome.CenterMarks = CountAnnotations(view, "CenterMark");
            if (plan.InsertCenterlines) outcome.Centerlines = CountAnnotations(view, "CenterLine");

            if (plan.InsertHoleCallouts) outcome.HoleCallouts = InsertHoleCallouts(view, creator);
        }

        /// <summary>
        /// Inserts hole callouts on a view.
        /// </summary>
        /// <remarks>
        /// Hole callouts are the difference between "there is a circle here" and
        /// "drill and tap M6x1.0 through" — a drawing without them is not
        /// something a shop can work from. The API for them changed name across
        /// releases, so the call is probed; when none of the forms exist the
        /// count comes back zero and the fabrication checklist raises it against
        /// the drawing rather than letting it pass.
        /// </remarks>
        private int InsertHoleCallouts(View view, ViewCreator creator)
        {
            int before = CountAnnotations(view, "HoleCallout");

            if (!creator.SelectView(view)) return before;

            object result;
            SwDispatch.TryInvokeAny(_drawing,
                                    new[] { "InsertHoleCallout2", "InsertHoleCallout" },
                                    new[] { new object[0], new object[] { true } },
                                    out result, _log);

            int after = CountAnnotations(view, "HoleCallout");
            if (after == before && before == 0)
            {
                _log.Debug("No hole callouts were added to " + creator.NameOf(view) +
                           "; the view may have no Hole Wizard holes facing the reader.");
            }
            return after;
        }

        /// <summary>
        /// Counts annotations of a given kind on a view, by their type name.
        /// </summary>
        private int CountAnnotations(View view, string typeNameFragment)
        {
            object result;
            if (!SwDispatch.TryInvokeAny(view, new[] { "GetAnnotations" },
                                         new[] { new object[0] }, out result, _log))
            {
                return 0;
            }

            int count = 0;
            foreach (object annotation in SwDispatch.AsObjects(result))
            {
                object typeObject;
                if (!SwDispatch.TryInvoke(annotation, "GetType", new object[0], out typeObject, _log)) continue;

                string name = AnnotationTypeName(typeObject);
                if (!string.IsNullOrEmpty(name) &&
                    name.IndexOf(typeNameFragment, StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    count++;
                }
            }
            return count;
        }

        /// <summary>
        /// Maps the integer returned by IAnnotation::GetType onto a readable
        /// name, using the swAnnotationType_e enum if it is loadable.
        /// </summary>
        private static string AnnotationTypeName(object typeObject)
        {
            if (typeObject == null) return null;
            string asString = typeObject as string;
            if (asString != null) return asString;

            try
            {
                int ordinal = Convert.ToInt32(typeObject, CultureInfo.InvariantCulture);
                Type type = Type.GetType("SolidWorks.Interop.swconst.swAnnotationType_e, SolidWorks.Interop.swconst");
                if (type != null && type.IsEnum && Enum.IsDefined(type, ordinal))
                    return Enum.GetName(type, ordinal);
                return ordinal.ToString(CultureInfo.InvariantCulture);
            }
            catch (InvalidCastException) { return null; }
            catch (FormatException) { return null; }
        }

        // ---- notes -----------------------------------------------------------

        /// <summary>
        /// Places the general note block on a sheet. Returns false when the note
        /// could not be created, which the checklist turns into a blocking
        /// finding — a drawing that states no tolerance standard is not
        /// releasable.
        /// </summary>
        public bool InsertNoteBlock(SheetPlan sheet, IEnumerable<string> notes)
        {
            if (notes == null) return false;

            StringBuilder text = new StringBuilder();
            text.AppendLine(NoteBlockComposer.Heading);
            int count = 0;
            foreach (string note in notes)
            {
                text.AppendLine(note);
                count++;
            }
            if (count == 0) return false;

            SheetLayout layout = new SheetLayout(sheet.Size);
            double x = sheet.NoteBlockXMm > 0 ? sheet.NoteBlockXMm : layout.NotesArea.Left;
            double y = sheet.NoteBlockYMm > 0 ? sheet.NoteBlockYMm : layout.NotesArea.Top;

            return InsertNote(text.ToString().TrimEnd(), x, y) != null;
        }

        /// <summary>
        /// States the projection convention on the sheet.
        /// </summary>
        /// <remarks>
        /// SOLIDWORKS has no API for the truncated-cone symbol itself — it lives
        /// in the sheet format — so the add-in draws it from sketch geometry and
        /// labels it. If the geometry cannot be drawn, the label alone still goes
        /// down: a sheet that names its convention in words can be read
        /// correctly, and a sheet that says nothing can be read mirrored.
        /// </remarks>
        public bool InsertProjectionSymbol(SheetPlan sheet)
        {
            SheetLayout layout = new SheetLayout(sheet.Size);
            RectMm zone = layout.ProjectionSymbolZone;

            string label = sheet.Projection == ProjectionAngle.Third
                ? "THIRD ANGLE PROJECTION"
                : "FIRST ANGLE PROJECTION";

            bool drewSymbol = DrawProjectionCones(sheet, zone);

            object note = InsertNote(label, zone.Left, zone.Bottom - 4.0);
            if (note == null && !drewSymbol)
            {
                _log.Warn("Could not place the projection symbol or its label on " + sheet.Name + ".");
                return false;
            }

            if (!drewSymbol)
            {
                _log.Info("Projection symbol geometry could not be drawn; the sheet carries the wording instead.");
            }
            return true;
        }

        /// <summary>
        /// Draws the two-circle projection symbol: a large circle and a small one,
        /// which side they sit on being what distinguishes the conventions.
        /// </summary>
        private bool DrawProjectionCones(SheetPlan sheet, RectMm zone)
        {
            object sketchManager;
            if (!SwDispatch.TryGet(_drawingModel, "SketchManager", out sketchManager, _log) || sketchManager == null)
                return false;

            // Sketching while a view is active attaches the geometry to that
            // view, which would make the symbol move with the part.
            object ignored;
            SwDispatch.TryInvoke(_drawing, "ActivateSheet", new object[] { sheet.Name }, out ignored, _log);

            double bigRadius = Math.Min(zone.Height, zone.Width / 4.0) / 2.0;
            if (bigRadius < 2.0) return false;
            double smallRadius = bigRadius * 0.6;

            double centerY = zone.CenterY;
            // Third angle puts the small circle (the near view) on the left.
            double smallX = sheet.Projection == ProjectionAngle.Third
                ? zone.Left + bigRadius * 1.2
                : zone.Right - bigRadius * 1.2;
            double bigX = sheet.Projection == ProjectionAngle.Third
                ? zone.Right - bigRadius * 1.2
                : zone.Left + bigRadius * 1.2;

            bool ok = DrawCircle(sketchManager, bigX, centerY, bigRadius);
            ok &= DrawCircle(sketchManager, smallX, centerY, smallRadius);

            // The cone's outline: two lines from the small circle's edge out to
            // the large circle's edge.
            ok &= DrawLine(sketchManager, smallX, centerY + smallRadius, bigX, centerY + bigRadius);
            ok &= DrawLine(sketchManager, smallX, centerY - smallRadius, bigX, centerY - bigRadius);

            SwDispatch.TryInvoke(sketchManager, "InsertSketch", new object[] { true }, out ignored, _log);
            return ok;
        }

        private bool DrawCircle(object sketchManager, double xMm, double yMm, double radiusMm)
        {
            object result;
            return SwDispatch.TryInvoke(sketchManager, new[] { "CreateCircleByRadius", "CreateCircle" },
                                        new object[] { Units.MmToMeter(xMm), Units.MmToMeter(yMm), 0.0,
                                                       Units.MmToMeter(radiusMm) },
                                        out result, _log) && result != null;
        }

        private bool DrawLine(object sketchManager, double x1Mm, double y1Mm, double x2Mm, double y2Mm)
        {
            object result;
            return SwDispatch.TryInvoke(sketchManager, "CreateLine",
                                        new object[] { Units.MmToMeter(x1Mm), Units.MmToMeter(y1Mm), 0.0,
                                                       Units.MmToMeter(x2Mm), Units.MmToMeter(y2Mm), 0.0 },
                                        out result, _log) && result != null;
        }

        /// <summary>Puts a note on the sheet at a point given in millimetres.</summary>
        public object InsertNote(string text, double xMm, double yMm)
        {
            if (string.IsNullOrEmpty(text)) return null;

            SwDoc.ClearSelection(_drawingModel);

            object note;
            if (!SwDispatch.TryInvokeAny(_drawingModel, new[] { "InsertNote" },
                                         new[] { new object[] { text } }, out note, _log) || note == null)
            {
                _log.Warn("Could not create a note.");
                return null;
            }

            object annotation;
            if (SwDispatch.TryInvoke(note, "GetAnnotation", new object[0], out annotation, _log) &&
                annotation != null)
            {
                object ignored;
                SwDispatch.TryInvoke(annotation, new[] { "SetPosition2", "SetPosition" },
                                     new object[] { Units.MmToMeter(xMm), Units.MmToMeter(yMm), 0.0 },
                                     out ignored, _log);

                // Left-justify so a multi-line note block reads as a block.
                SwDispatch.TryInvoke(note, "SetTextJustification", new object[] { 1 }, out ignored, _log);
            }

            SwDoc.ClearSelection(_drawingModel);
            return note;
        }
    }
}
