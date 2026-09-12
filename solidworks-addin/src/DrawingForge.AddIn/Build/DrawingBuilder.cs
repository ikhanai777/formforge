using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
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
    /// Executes one <see cref="DrawingPlan"/> against a live SOLIDWORKS session.
    /// </summary>
    /// <remarks>
    /// The plan is a proposal, not a promise. SOLIDWORKS knows things the
    /// planner could only estimate — the true outline of a flat pattern, how
    /// much room a section view really needs — so after the views exist they are
    /// measured and, if they overrun the sheet, the scale steps down and
    /// everything moves with it. A drawing that runs off its own border is
    /// worse than one drawn a size smaller.
    /// </remarks>
    internal sealed class DrawingBuilder
    {
        /// <summary>How many times the scale may step down to make the views fit.</summary>
        private const int MaxScaleCorrections = 3;

        private readonly ISldWorks _app;
        private readonly DrawingOptions _options;
        private readonly ILog _log;

        public DrawingBuilder(ISldWorks app, DrawingOptions options, ILog log)
        {
            _app = app;
            _options = options;
            _log = log;
        }

        public DrawingOutcome Build(DrawingPlan plan, PartSummary part, ModelDoc2 sourceModel)
        {
            DrawingOutcome outcome = new DrawingOutcome
            {
                PartNumber = plan.PartNumber,
                SourceModelPath = plan.SourceModelPath,
                Configuration = plan.Configuration,
                DrawingPath = plan.OutputFilePath,
                SheetCount = plan.Sheets.Count,
                ProjectionAngle = plan.Projection == ProjectionAngle.Third ? "THIRD" : "FIRST"
            };

            if (plan.Sheets.Count > 0)
            {
                outcome.SheetSizeName = plan.Sheets[0].Size.Name;
                outcome.Scale = plan.Sheets[0].Scale != null ? plan.Sheets[0].Scale.ToString() : "1:1";
            }

            SheetSetup setup = new SheetSetup(_app, _options, _log);
            string failure;
            ModelDoc2 drawingModel = setup.CreateDrawing(plan, out failure);
            if (drawingModel == null)
            {
                outcome.Errors.Add(failure);
                outcome.FinishedUtc = DateTime.UtcNow;
                return outcome;
            }

            DrawingDoc drawing = drawingModel as DrawingDoc;
            if (drawing == null)
            {
                outcome.Errors.Add("The new document does not expose IDrawingDoc.");
                outcome.FinishedUtc = DateTime.UtcNow;
                return outcome;
            }

            try
            {
                setup.ApplyDocumentStandards(drawingModel, plan);

                AnnotationEngine annotations =
                    new AnnotationEngine(_app, drawing, drawingModel, _options, _log);
                annotations.ConfigureAutoInsert();

                ViewCreator creator = new ViewCreator(drawing, drawingModel, _log);
                DimensionEngine dimensions = new DimensionEngine(drawingModel, _options, _log);
                TableBuilder tables = new TableBuilder(drawing, drawingModel, _options, _log);

                for (int index = 0; index < plan.Sheets.Count; index++)
                {
                    SheetPlan sheet = plan.Sheets[index];
                    if (sheet.Scale == null) sheet.Scale = new Ratio(1, 1);

                    if (index == 0)
                    {
                        setup.ApplySheetProperties(drawing, sheet);
                    }
                    else if (!setup.AddSheet(drawing, sheet))
                    {
                        outcome.Errors.Add("Sheet " + sheet.Name + " could not be added.");
                        continue;
                    }

                    BuildSheet(drawing, drawingModel, sheet, plan, part, creator, dimensions,
                               annotations, tables, setup, outcome);
                }

                outcome.TitleBlockFieldsWritten = WriteTitleBlock(drawingModel, plan);

                SwDoc.Rebuild(drawingModel, _log);

                Exporter exporter = new Exporter(_app, _options, _log);
                exporter.Export(drawingModel, plan.OutputFilePath, sourceModel, outcome);

                outcome.Succeeded = outcome.Errors.Count == 0 &&
                                    outcome.ViewResults.Any(v => v.Created);
            }
            catch (System.Runtime.InteropServices.COMException ex)
            {
                outcome.Errors.Add(LogExtensions.Describe(ex));
                _log.Error("Building " + plan.PartNumber + " failed", ex);
            }
            finally
            {
                outcome.FinishedUtc = DateTime.UtcNow;
            }

            return outcome;
        }

        private void BuildSheet(DrawingDoc drawing, ModelDoc2 drawingModel, SheetPlan sheet, DrawingPlan plan,
                                PartSummary part, ViewCreator creator, DimensionEngine dimensions,
                                AnnotationEngine annotations, TableBuilder tables, SheetSetup setup,
                                DrawingOutcome outcome)
        {
            // Parents before children: a projected, section or detail view needs
            // the view it comes from to exist first.
            foreach (ViewPlan viewPlan in OrderForCreation(sheet.Views))
            {
                ViewOutcome viewOutcome = creator.Create(viewPlan, plan.SourceModelPath, plan.Configuration);
                outcome.ViewResults.Add(viewOutcome);
            }

            CorrectScaleToFit(drawing, sheet, creator, setup, outcome);

            foreach (ViewPlan viewPlan in sheet.Views)
            {
                View view = creator.Get(viewPlan.Id);
                if (view == null) continue;

                ViewOutcome viewOutcome = outcome.ViewResults.FirstOrDefault(v => v.ViewId == viewPlan.Id);
                if (viewOutcome == null) continue;

                dimensions.InsertInto(view, viewPlan, viewOutcome, creator);
                dimensions.Arrange(view, creator);
                annotations.Annotate(view, viewPlan, viewOutcome, creator);

                if (!string.IsNullOrEmpty(viewPlan.Label) && viewPlan.Kind == ViewKind.Isometric)
                {
                    // The isometric is a reading aid, and labelling it keeps a
                    // shop from measuring off a view that is not to scale.
                    annotations.InsertNote(viewPlan.Label,
                                           viewPlan.CenterXMm - viewPlan.WidthMm / 2.0,
                                           viewPlan.CenterYMm - viewPlan.HeightMm / 2.0 - 6.0);
                }
            }

            foreach (TablePlan table in sheet.Tables)
            {
                if (tables.Insert(table, creator, outcome) && table.AutoBalloon)
                {
                    View attached = creator.Get(table.AttachedViewId);
                    int balloons = tables.AutoBalloon(attached, creator);
                    _log.Info(string.Format(CultureInfo.InvariantCulture, "{0} balloon(s) placed.", balloons));
                }
            }

            if (sheet.ShowNoteBlock && plan.Notes.Count > 0)
            {
                if (annotations.InsertNoteBlock(sheet, plan.Notes)) outcome.NoteBlockInserted = true;
                else outcome.Warnings.Add("The general note block could not be placed on " + sheet.Name + ".");
            }

            if (sheet.ShowProjectionSymbol)
            {
                if (annotations.InsertProjectionSymbol(sheet)) outcome.ProjectionSymbolInserted = true;
            }
        }

        /// <summary>
        /// Views in dependency order: anything with a parent comes after it.
        /// </summary>
        private static IEnumerable<ViewPlan> OrderForCreation(IEnumerable<ViewPlan> views)
        {
            List<ViewPlan> all = views.ToList();
            List<ViewPlan> ordered = new List<ViewPlan>();
            HashSet<string> placed = new HashSet<string>(StringComparer.Ordinal);

            foreach (ViewPlan view in all.Where(v => string.IsNullOrEmpty(v.ParentViewId)))
            {
                ordered.Add(view);
                placed.Add(view.Id);
            }

            // Repeat until nothing more can be placed; a view whose parent is
            // missing is emitted last so the failure is reported rather than
            // silently dropped.
            bool progress = true;
            while (progress)
            {
                progress = false;
                foreach (ViewPlan view in all)
                {
                    if (placed.Contains(view.Id)) continue;
                    if (view.ParentViewId != null && !placed.Contains(view.ParentViewId)) continue;
                    ordered.Add(view);
                    placed.Add(view.Id);
                    progress = true;
                }
            }

            foreach (ViewPlan view in all)
            {
                if (!placed.Contains(view.Id)) ordered.Add(view);
            }

            return ordered;
        }

        /// <summary>
        /// Measures what SOLIDWORKS actually drew and steps the sheet scale down
        /// until it fits inside the border.
        /// </summary>
        /// <remarks>
        /// Scaling every view about the centre of the view area keeps projected
        /// views aligned with their parent: a uniform scaling about a common
        /// point preserves shared centrelines, which is the one property the
        /// layout must never lose.
        /// </remarks>
        private void CorrectScaleToFit(DrawingDoc drawing, SheetPlan sheet, ViewCreator creator,
                                       SheetSetup setup, DrawingOutcome outcome)
        {
            IReadOnlyList<Ratio> ladder = _options.Profile().ScaleLadder;
            RectMm area = new SheetLayout(sheet.Size).ViewArea;

            for (int attempt = 0; attempt < MaxScaleCorrections; attempt++)
            {
                RectMm? unionOrNull = MeasuredUnion(sheet, creator);
                if (!unionOrNull.HasValue) return;         // nothing measurable, nothing to correct
                RectMm union = unionOrNull.Value;

                if (area.Contains(union, 1.0)) return;      // already fits

                Ratio next = NextSmaller(ladder, sheet.Scale);
                if (next == null)
                {
                    outcome.Warnings.Add(string.Format(CultureInfo.InvariantCulture,
                        "Views on {0} overrun the border and the scale is already at the bottom of the ladder.",
                        sheet.Name));
                    return;
                }

                double factor = next.Value / sheet.Scale.Value;
                _log.Info(string.Format(CultureInfo.InvariantCulture,
                    "Views on {0} measured {1:0.#} x {2:0.#} mm against a {3:0.#} x {4:0.#} mm area; " +
                    "stepping the scale from {5} to {6}.",
                    sheet.Name, union.Width, union.Height, area.Width, area.Height, sheet.Scale, next));

                sheet.Scale = next;
                setup.ApplySheetProperties(drawing, sheet);

                foreach (ViewPlan viewPlan in sheet.Views)
                {
                    viewPlan.CenterXMm = area.CenterX + (viewPlan.CenterXMm - area.CenterX) * factor;
                    viewPlan.CenterYMm = area.CenterY + (viewPlan.CenterYMm - area.CenterY) * factor;
                    viewPlan.WidthMm *= factor;
                    viewPlan.HeightMm *= factor;

                    View view = creator.Get(viewPlan.Id);
                    if (view != null) creator.Position(view, viewPlan.CenterXMm, viewPlan.CenterYMm);
                }

                outcome.Scale = next.ToString();
            }

            outcome.Warnings.Add("Views on " + sheet.Name +
                                 " still overrun the border after three scale reductions; check the sheet.");
        }

        /// <summary>Bounding rectangle of every measurable view on a sheet.</summary>
        private RectMm? MeasuredUnion(SheetPlan sheet, ViewCreator creator)
        {
            double left = double.MaxValue, bottom = double.MaxValue;
            double right = double.MinValue, top = double.MinValue;
            bool any = false;

            foreach (ViewPlan viewPlan in sheet.Views)
            {
                View view = creator.Get(viewPlan.Id);
                if (view == null) continue;

                RectMm? outline = creator.OutlineMm(view);
                if (!outline.HasValue) continue;
                if (outline.Value.Width < 0.05 && outline.Value.Height < 0.05) continue;

                any = true;
                left = Math.Min(left, outline.Value.Left);
                bottom = Math.Min(bottom, outline.Value.Bottom);
                right = Math.Max(right, outline.Value.Right);
                top = Math.Max(top, outline.Value.Top);
            }

            if (!any) return null;
            return new RectMm(left, bottom, right - left, top - bottom);
        }

        private static Ratio NextSmaller(IReadOnlyList<Ratio> ladder, Ratio current)
        {
            if (ladder == null || current == null) return null;
            for (int i = 0; i < ladder.Count; i++)
            {
                if (Math.Abs(ladder[i].Value - current.Value) < 1e-9)
                    return i + 1 < ladder.Count ? ladder[i + 1] : null;
            }

            // Not on the ladder: take the largest entry smaller than it.
            foreach (Ratio candidate in ladder)
            {
                if (candidate.Value < current.Value) return candidate;
            }
            return null;
        }

        /// <summary>
        /// Copies the planned title block values onto the drawing as custom
        /// properties, which is what a sheet format's linked fields read.
        /// </summary>
        private int WriteTitleBlock(ModelDoc2 drawingModel, DrawingPlan plan)
        {
            int written = 0;
            foreach (KeyValuePair<string, string> entry in plan.TitleBlockProperties)
            {
                if (string.IsNullOrEmpty(entry.Value)) continue;
                if (SwDoc.WriteProperty(drawingModel, string.Empty, entry.Key, entry.Value, _log)) written++;
            }

            _log.Info(string.Format(CultureInfo.InvariantCulture,
                "{0} title block propert{1} written.", written, written == 1 ? "y" : "ies"));
            return written;
        }
    }
}
