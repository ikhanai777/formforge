using System;
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
    /// Inserts the tables a fabrication drawing needs: bill of materials,
    /// weldment cut list, sheet metal bend table, and the balloons that tie a
    /// BOM to the view.
    /// </summary>
    /// <remarks>
    /// Each of these has been renamed and re-argumented across SOLIDWORKS
    /// releases more than almost anything else in the API, and each of them
    /// hangs off either the drawing or the view depending on the release. So
    /// every insertion is probed against both hosts and several shapes, and a
    /// failure is reported rather than silently swallowed — a BOM that did not
    /// appear is the difference between an assembly drawing and a picture.
    /// </remarks>
    internal sealed class TableBuilder
    {
        private readonly DrawingDoc _drawing;
        private readonly ModelDoc2 _drawingModel;
        private readonly DrawingOptions _options;
        private readonly ILog _log;

        public TableBuilder(DrawingDoc drawing, ModelDoc2 drawingModel, DrawingOptions options, ILog log)
        {
            _drawing = drawing;
            _drawingModel = drawingModel;
            _options = options;
            _log = log;
        }

        /// <summary>Inserts one planned table; returns true when it landed.</summary>
        public bool Insert(TablePlan table, ViewCreator creator, DrawingOutcome outcome)
        {
            View attached = creator.Get(table.AttachedViewId);

            bool inserted;
            switch (table.Kind)
            {
                case TableKind.BillOfMaterials:
                    inserted = InsertBom(table, attached, creator);
                    break;
                case TableKind.WeldmentCutList:
                    inserted = InsertWeldmentCutList(table, attached, creator);
                    break;
                case TableKind.SheetMetalBendTable:
                    inserted = InsertBendTable(table, attached, creator);
                    break;
                case TableKind.RevisionTable:
                    inserted = InsertRevisionTable(table);
                    break;
                case TableKind.HoleTable:
                    inserted = InsertHoleTable(table, attached, creator);
                    break;
                default:
                    inserted = false;
                    break;
            }

            if (inserted)
            {
                outcome.TablesInserted.Add(table.Kind.ToString());
            }
            else
            {
                outcome.Warnings.Add(table.Kind + " could not be inserted.");
                _log.Warn(table.Kind + " could not be inserted on this release.");
            }
            return inserted;
        }

        private bool InsertBom(TablePlan table, View attached, ViewCreator creator)
        {
            if (attached != null && !creator.SelectView(attached))
                _log.Debug("The BOM's view could not be selected; inserting it unattached.");

            string template = table.TemplatePath ?? string.Empty;
            double x = Units.MmToMeter(table.AnchorXMm);
            double y = Units.MmToMeter(table.AnchorYMm);

            object[][] viewShapes =
            {
                // useAnchorPoint, bomType, configuration, numberingType, detailedCutList, template, hidden
                new object[] { false, SwConst.swBomType_TopLevelOnly, string.Empty,
                               SwConst.swNumberingType_Detailed, false, template, false },
                new object[] { false, SwConst.swBomType_TopLevelOnly, string.Empty,
                               SwConst.swNumberingType_Detailed, false, template },
            };

            object result;
            if (attached != null &&
                SwDispatch.TryInvokeAny(attached,
                                        new[] { "InsertBomTable4", "InsertBomTable3", "InsertBomTable2" },
                                        viewShapes, out result, _log) && result != null)
            {
                return true;
            }

            object[][] drawingShapes =
            {
                // useAnchorPoint, x, y, anchorType, bomType, configuration, template, hidden
                new object[] { false, x, y, SwConst.swBOMConfigurationAnchor_TopRight,
                               SwConst.swBomType_TopLevelOnly, string.Empty, template, false },
                new object[] { false, x, y, SwConst.swBOMConfigurationAnchor_TopRight,
                               SwConst.swBomType_TopLevelOnly, string.Empty, template },
                new object[] { false, x, y, SwConst.swBOMConfigurationAnchor_TopRight,
                               SwConst.swBomType_TopLevelOnly, string.Empty },
            };

            if (SwDispatch.TryInvokeAny(_drawing,
                                        new[] { "InsertBomTable4", "InsertBomTable3", "InsertBomTable2", "InsertBomTable" },
                                        drawingShapes, out result, _log) && result != null)
            {
                return true;
            }

            return false;
        }

        /// <summary>
        /// Balloons every component of a view and ties them to the BOM item
        /// numbers.
        /// </summary>
        public int AutoBalloon(View view, ViewCreator creator)
        {
            if (view == null || !_options.AssemblyBalloons) return 0;
            if (!creator.SelectView(view)) return 0;

            // Modern releases build an options object first.
            object balloonOptions;
            if (SwDispatch.TryInvoke(_drawing, "CreateAutoBalloonOptions", new object[0],
                                     out balloonOptions, _log) && balloonOptions != null)
            {
                SwDispatch.TrySet(balloonOptions, "Layout", 1, _log);
                SwDispatch.TrySet(balloonOptions, "IgnoreMultiple", true, _log);
                SwDispatch.TrySet(balloonOptions, "InsertMagneticLine", true, _log);

                object result;
                if (SwDispatch.TryInvoke(_drawing, new[] { "AutoBalloon5", "AutoBalloon6" },
                                         new object[] { balloonOptions }, out result, _log))
                {
                    return SwDispatch.AsObjects(result).Length;
                }
            }

            object legacy;
            object[][] shapes =
            {
                new object[] { 1, true, 1, 1, 1, 1, string.Empty },
                new object[] { 1, true, 1, 1, 1 },
                new object[] { 1, true },
            };

            if (SwDispatch.TryInvokeAny(_drawing, new[] { "AutoBalloon4", "AutoBalloon3", "AutoBalloon2" },
                                        shapes, out legacy, _log))
            {
                return SwDispatch.AsObjects(legacy).Length;
            }

            _log.Warn("Auto-ballooning is unavailable; the BOM will need balloons added by hand.");
            return 0;
        }

        private bool InsertWeldmentCutList(TablePlan table, View attached, ViewCreator creator)
        {
            if (attached != null) creator.SelectView(attached);

            double x = Units.MmToMeter(table.AnchorXMm);
            double y = Units.MmToMeter(table.AnchorYMm);
            string template = table.TemplatePath ?? string.Empty;

            object result;
            object[][] viewShapes =
            {
                new object[] { false, x, y, SwConst.swTableAnchor_TopLeft, template },
                new object[] { false, x, y, SwConst.swTableAnchor_TopLeft },
            };

            if (attached != null &&
                SwDispatch.TryInvokeAny(attached,
                                        new[] { "InsertWeldmentTable2", "InsertWeldmentTable" },
                                        viewShapes, out result, _log) && result != null)
            {
                return true;
            }

            object[][] drawingShapes =
            {
                new object[] { false, x, y, SwConst.swTableAnchor_TopLeft, template },
                new object[] { false, x, y, SwConst.swTableAnchor_TopLeft },
            };

            return SwDispatch.TryInvokeAny(_drawing,
                                           new[] { "InsertWeldmentTableAnnotation2", "InsertWeldmentTableAnnotation" },
                                           drawingShapes, out result, _log) && result != null;
        }

        private bool InsertBendTable(TablePlan table, View attached, ViewCreator creator)
        {
            if (attached == null)
            {
                _log.Debug("A bend table needs a flat pattern view to attach to.");
                return false;
            }
            creator.SelectView(attached);

            double x = Units.MmToMeter(table.AnchorXMm);
            double y = Units.MmToMeter(table.AnchorYMm);
            string template = table.TemplatePath ?? string.Empty;

            object result;
            object[][] shapes =
            {
                new object[] { false, x, y, SwConst.swTableAnchor_TopLeft, template },
                new object[] { false, x, y, SwConst.swTableAnchor_TopLeft },
                new object[] { template },
            };

            if (SwDispatch.TryInvokeAny(attached, new[] { "InsertBendTable2", "InsertBendTable" },
                                        shapes, out result, _log) && result != null)
            {
                return true;
            }

            return SwDispatch.TryInvokeAny(_drawing,
                                           new[] { "InsertBendTableAnnotation2", "InsertBendTableAnnotation" },
                                           shapes, out result, _log) && result != null;
        }

        private bool InsertRevisionTable(TablePlan table)
        {
            double x = Units.MmToMeter(table.AnchorXMm);
            double y = Units.MmToMeter(table.AnchorYMm);
            string template = table.TemplatePath ?? string.Empty;

            object result;
            object[][] shapes =
            {
                new object[] { false, x, y, SwConst.swTableAnchor_TopRight, template, false },
                new object[] { false, x, y, SwConst.swTableAnchor_TopRight, template },
                new object[] { template },
            };

            return SwDispatch.TryInvokeAny(_drawing,
                                           new[] { "InsertRevisionTable2", "InsertRevisionTable" },
                                           shapes, out result, _log) && result != null;
        }

        private bool InsertHoleTable(TablePlan table, View attached, ViewCreator creator)
        {
            if (attached == null) return false;
            creator.SelectView(attached);

            double x = Units.MmToMeter(table.AnchorXMm);
            double y = Units.MmToMeter(table.AnchorYMm);
            string template = table.TemplatePath ?? string.Empty;

            object result;
            object[][] shapes =
            {
                new object[] { x, y, SwConst.swTableAnchor_TopLeft, 0, 0, template },
                new object[] { x, y, SwConst.swTableAnchor_TopLeft, template },
            };

            return SwDispatch.TryInvokeAny(_drawing,
                                           new[] { "InsertHoleTable2", "InsertHoleTable" },
                                           shapes, out result, _log) && result != null;
        }
    }
}
