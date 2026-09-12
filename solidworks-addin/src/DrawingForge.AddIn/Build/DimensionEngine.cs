using System;
using System.Collections.Generic;
using System.Globalization;
using DrawingForge.AddIn.Interop;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Logging;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;
using DrawingForge.Core.Reporting;
using SolidWorks.Interop.sldworks;

namespace DrawingForge.AddIn.Build
{
    /// <summary>
    /// Puts dimensions on the views and then cleans up after SOLIDWORKS.
    /// </summary>
    /// <remarks>
    /// Importing model items is the easy half. The half that decides whether the
    /// drawing is usable is what happens next: the same dimension arriving on
    /// two views, dimensions landing on top of the geometry they measure, and
    /// dimensions stacked on each other at the view's origin. Each of those is
    /// handled here, and each is counted so the run report can say how much
    /// cleanup the drawing needed.
    /// </remarks>
    internal sealed class DimensionEngine
    {
        /// <summary>How far outside the view outline a pushed-out dimension lands.</summary>
        private const double PushOutMarginMm = 8.0;

        private readonly ModelDoc2 _drawingModel;
        private readonly ILog _log;
        private readonly DrawingOptions _options;

        /// <summary>Full names of dimensions already placed, so repeats can be dropped.</summary>
        private readonly HashSet<string> _placed = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        public DimensionEngine(ModelDoc2 drawingModel, DrawingOptions options, ILog log)
        {
            _drawingModel = drawingModel;
            _options = options;
            _log = log;
        }

        /// <summary>
        /// Inserts model dimensions into one view and records the result on
        /// <paramref name="outcome"/>.
        /// </summary>
        public void InsertInto(View view, ViewPlan plan, ViewOutcome outcome, ViewCreator creator)
        {
            if (view == null || !plan.InsertModelDimensions) return;

            if (!creator.SelectView(view))
            {
                outcome.Failure = "Could not select the view to dimension it.";
                return;
            }

            int types = SwConst.swInsertDimensionsMarkedForDrawing;
            if (plan.InsertHoleCallouts) types |= SwConst.swInsertCThreads;
            types |= SwConst.swInsertDatums | SwConst.swInsertGTols | SwConst.swInsertSFSymbols;

            object result;
            object[][] argumentLists =
            {
                // source, types, allViews, duplicateDims, hideDuplicates, useDimPrecision
                new object[] { SwConst.swImportModelItemsFromEntireModel, types, false, false, true, false },
                new object[] { SwConst.swImportModelItemsFromEntireModel, types, false, false, true },
                new object[] { SwConst.swImportModelItemsFromEntireModel, types, false, false },
            };

            bool inserted = SwDispatch.TryInvokeAny(_drawingModel.Extension,
                                                    new[] { "InsertModelAnnotations3", "InsertModelAnnotations2" },
                                                    argumentLists, out result, _log);

            if (!inserted)
            {
                _log.Warn("Model items could not be inserted into " + outcome.SolidWorksName +
                          "; the view will need dimensioning by hand.");
            }

            IList<object> dimensions = DisplayDimensions(view);
            outcome.DimensionCount = dimensions.Count;

            if (_options.RemoveDuplicateDimensions)
                outcome.DuplicatesRemoved = RemoveDuplicates(view, dimensions);

            outcome.DimensionCount = DisplayDimensions(view).Count;

            if (_options.DimensionPrecision >= 0)
                ApplyPrecision(view, _options.DimensionPrecision);
        }

        /// <summary>
        /// Spreads the dimensions out. Uses the API's own arrangement when the
        /// release has it, and falls back to pushing anything sitting on top of
        /// the geometry clear of the view outline.
        /// </summary>
        public void Arrange(View view, ViewCreator creator)
        {
            if (view == null || !_options.AutoArrangeDimensions) return;

            if (creator.SelectView(view))
            {
                object result;
                if (SwDispatch.TryInvokeAny(_drawingModel.Extension, new[] { "AlignDimensions" },
                                            new[]
                                            {
                                                new object[] { SwConst.swAlignDimensionType_AutoArrange,
                                                               Units.MmToMeter(10.0) },
                                                new object[] { SwConst.swAlignDimensionType_AutoArrange }
                                            },
                                            out result, _log))
                {
                    return;
                }
            }

            PushDimensionsClearOfGeometry(view, creator);
        }

        /// <summary>
        /// Moves any dimension whose text sits inside the view outline out past
        /// the nearest edge. Crude next to a drafter's judgement, and far better
        /// than a dimension written across the part it measures.
        /// </summary>
        private void PushDimensionsClearOfGeometry(View view, ViewCreator creator)
        {
            RectMm? outlineOrNull = creator.OutlineMm(view);
            if (!outlineOrNull.HasValue) return;
            RectMm outline = outlineOrNull.Value;

            int moved = 0;
            foreach (object displayDimension in DisplayDimensions(view))
            {
                object annotation;
                if (!SwDispatch.TryInvoke(displayDimension, "GetAnnotation", new object[0], out annotation, _log) ||
                    annotation == null)
                {
                    continue;
                }

                object positionObject;
                if (!SwDispatch.TryInvoke(annotation, "GetPosition", new object[0], out positionObject, _log))
                    continue;

                double[] position = SwDispatch.AsDoubles(positionObject);
                if (position == null || position.Length < 2) continue;

                double xMm = Units.MeterToMm(position[0]);
                double yMm = Units.MeterToMm(position[1]);

                bool insideX = xMm > outline.Left && xMm < outline.Right;
                bool insideY = yMm > outline.Bottom && yMm < outline.Top;
                if (!insideX || !insideY) continue;

                // Push to whichever edge is nearest, so a dimension keeps the
                // side of the part it belongs to.
                double toLeft = xMm - outline.Left;
                double toRight = outline.Right - xMm;
                double toBottom = yMm - outline.Bottom;
                double toTop = outline.Top - yMm;
                double nearest = Math.Min(Math.Min(toLeft, toRight), Math.Min(toBottom, toTop));

                if (nearest == toLeft) xMm = outline.Left - PushOutMarginMm;
                else if (nearest == toRight) xMm = outline.Right + PushOutMarginMm;
                else if (nearest == toBottom) yMm = outline.Bottom - PushOutMarginMm;
                else yMm = outline.Top + PushOutMarginMm;

                object ignored;
                if (SwDispatch.TryInvoke(annotation, new[] { "SetPosition2", "SetPosition" },
                                         new object[] { Units.MmToMeter(xMm), Units.MmToMeter(yMm), 0.0 },
                                         out ignored, _log))
                {
                    moved++;
                }
            }

            if (moved > 0)
            {
                _log.Debug(string.Format(CultureInfo.InvariantCulture,
                    "Moved {0} dimension(s) clear of the geometry.", moved));
            }
        }

        /// <summary>
        /// Deletes dimensions already placed on an earlier view. Returns how many
        /// went.
        /// </summary>
        private int RemoveDuplicates(View view, IList<object> dimensions)
        {
            int removed = 0;

            foreach (object displayDimension in dimensions)
            {
                string key = DimensionKey(displayDimension);
                if (string.IsNullOrEmpty(key)) continue;

                if (!_placed.Add(key))
                {
                    if (Delete(displayDimension)) removed++;
                }
            }

            if (removed > 0)
            {
                _log.Debug(string.Format(CultureInfo.InvariantCulture,
                    "Removed {0} duplicate dimension(s) already shown on another view.", removed));
            }
            return removed;
        }

        /// <summary>
        /// Identity of a model dimension. The full name — "D1@Sketch1@Part.SLDPRT"
        /// — is the same wherever the dimension is inserted, which is exactly what
        /// duplicate detection needs.
        /// </summary>
        private string DimensionKey(object displayDimension)
        {
            object dimension;
            if (!SwDispatch.TryInvokeAny(displayDimension, new[] { "GetDimension2", "GetDimension" },
                                         new[] { new object[] { 0 }, new object[0] },
                                         out dimension, _log) || dimension == null)
            {
                return null;
            }

            object fullName;
            if (SwDispatch.TryGet(dimension, "FullName", out fullName, _log) && fullName is string)
                return (string)fullName;

            if (SwDispatch.TryGet(dimension, "Name", out fullName, _log) && fullName is string)
                return (string)fullName;

            return null;
        }

        private bool Delete(object displayDimension)
        {
            object annotation;
            if (!SwDispatch.TryInvoke(displayDimension, "GetAnnotation", new object[0], out annotation, _log) ||
                annotation == null)
            {
                return false;
            }

            object selected;
            if (!SwDispatch.TryInvokeAny(annotation, new[] { "Select3", "Select2", "Select" },
                                         new[] { new object[] { false, null },
                                                 new object[] { false, 0 },
                                                 new object[] { false } },
                                         out selected, _log))
            {
                return false;
            }

            object result;
            return SwDispatch.TryInvoke(_drawingModel, new[] { "EditDelete" }, new object[0], out result, _log);
        }

        /// <summary>Sets the decimal places shown on every linear dimension of a view.</summary>
        private void ApplyPrecision(View view, int decimals)
        {
            foreach (object displayDimension in DisplayDimensions(view))
            {
                object ignored;
                SwDispatch.TryInvokeAny(displayDimension,
                                        new[] { "SetPrecision3", "SetPrecision2", "SetPrecision" },
                                        new[]
                                        {
                                            new object[] { 1, decimals, decimals, decimals, decimals },
                                            new object[] { decimals, decimals },
                                            new object[] { decimals }
                                        },
                                        out ignored, _log);
            }
        }

        /// <summary>Every display dimension attached to a view.</summary>
        public IList<object> DisplayDimensions(View view)
        {
            List<object> dimensions = new List<object>();
            if (view == null) return dimensions;

            object result;
            if (!SwDispatch.TryInvoke(view, "GetDisplayDimensions", new object[0], out result, _log))
                return dimensions;

            foreach (object item in SwDispatch.AsObjects(result))
            {
                if (item != null) dimensions.Add(item);
            }
            return dimensions;
        }
    }
}
