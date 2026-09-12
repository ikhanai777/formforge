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
    /// Creates the views a <see cref="ViewPlan"/> asks for, on a live drawing.
    /// </summary>
    /// <remarks>
    /// Positions arrive in millimetres from the sheet's lower-left corner and
    /// go into the API in metres, which is the only unit SOLIDWORKS accepts for
    /// sheet geometry. Every creation call is checked: a view that comes back
    /// null is recorded against the plan rather than being allowed to turn into
    /// a silently incomplete drawing.
    /// </remarks>
    internal sealed class ViewCreator
    {
        private readonly DrawingDoc _drawing;
        private readonly ModelDoc2 _drawingModel;
        private readonly ILog _log;

        /// <summary>Views created so far, keyed by the plan's view id.</summary>
        private readonly Dictionary<string, View> _created =
            new Dictionary<string, View>(StringComparer.Ordinal);

        public ViewCreator(DrawingDoc drawing, ModelDoc2 drawingModel, ILog log)
        {
            if (drawing == null) throw new ArgumentNullException("drawing");
            _drawing = drawing;
            _drawingModel = drawingModel;
            _log = log;
        }

        /// <summary>Creates one view and reports what happened.</summary>
        public ViewOutcome Create(ViewPlan plan, string modelPath, string configuration)
        {
            ViewOutcome outcome = new ViewOutcome
            {
                ViewId = plan.Id,
                Kind = plan.Kind.ToString()
            };

            View view = null;
            try
            {
                switch (plan.Kind)
                {
                    case ViewKind.Principal:
                    case ViewKind.Isometric:
                    case ViewKind.Assembly:
                        view = CreateNamedView(plan, modelPath);
                        break;

                    case ViewKind.Projected:
                        view = CreateProjectedView(plan);
                        break;

                    case ViewKind.Section:
                        view = CreateSectionView(plan);
                        break;

                    case ViewKind.Detail:
                        view = CreateDetailView(plan);
                        break;

                    case ViewKind.FlatPattern:
                        view = CreateFlatPatternView(plan, modelPath, configuration);
                        break;

                    default:
                        outcome.Failure = "Unsupported view kind " + plan.Kind + ".";
                        return outcome;
                }
            }
            catch (System.Runtime.InteropServices.COMException ex)
            {
                outcome.Failure = LogExtensions.Describe(ex);
                _log.Warn("Creating " + plan.Kind + " view failed: " + outcome.Failure);
                return outcome;
            }

            if (view == null)
            {
                if (string.IsNullOrEmpty(outcome.Failure))
                    outcome.Failure = "SOLIDWORKS returned no view.";
                _log.Warn(string.Format(CultureInfo.InvariantCulture,
                    "{0} view was not created: {1}", plan.Kind, outcome.Failure));
                return outcome;
            }

            _created[plan.Id] = view;
            outcome.Created = true;
            outcome.SolidWorksName = NameOf(view);

            ApplyConfiguration(view, configuration);
            ApplyScale(view, plan);
            ApplyRotation(view, plan);
            ApplyDisplayMode(view, plan);
            Position(view, plan.CenterXMm, plan.CenterYMm);

            outcome.IsEmpty = LooksEmpty(view);
            if (outcome.IsEmpty)
            {
                _log.Warn(string.Format(CultureInfo.InvariantCulture,
                    "View {0} ({1}) has no visible geometry.", outcome.SolidWorksName, plan.Kind));
            }

            return outcome;
        }

        /// <summary>The SOLIDWORKS view created for a plan id, or null.</summary>
        public View Get(string viewId)
        {
            View view;
            return viewId != null && _created.TryGetValue(viewId, out view) ? view : null;
        }

        public IEnumerable<KeyValuePair<string, View>> CreatedViews { get { return _created; } }

        // ---- creation --------------------------------------------------------

        private View CreateNamedView(ViewPlan plan, string modelPath)
        {
            string viewName = !string.IsNullOrEmpty(plan.SwViewName)
                ? plan.SwViewName
                : ViewFrame.Of(plan.Orientation).SwViewName;

            object result;
            object[] args = { modelPath, viewName,
                              Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0 };

            if (SwDispatch.TryInvoke(_drawing,
                                     new[] { "CreateDrawViewFromModelView3", "CreateDrawViewFromModelView2" },
                                     args, out result, _log))
            {
                return result as View;
            }

            return null;
        }

        private View CreateProjectedView(ViewPlan plan)
        {
            View parent = Get(plan.ParentViewId);
            if (parent == null)
            {
                _log.Warn("A projected view was planned with no parent view available.");
                return null;
            }

            if (!SelectView(parent)) return null;

            // CreateUnfoldedViewAt3 decides which orthographic view it produces
            // from where the point sits relative to the parent, combined with
            // the sheet's own first/third angle setting. The planner has already
            // put the point on the correct side for the convention in force, so
            // there is nothing to flip here.
            object result;
            object[] args = { Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0, false };

            if (SwDispatch.TryInvoke(_drawing,
                                     new[] { "CreateUnfoldedViewAt3", "CreateUnfoldedViewAt2" },
                                     args, out result, _log))
            {
                return result as View;
            }

            return null;
        }

        private View CreateSectionView(ViewPlan plan)
        {
            View parent = Get(plan.ParentViewId);
            if (parent == null)
            {
                _log.Warn("A section view was planned with no parent view available.");
                return null;
            }

            double[] outline = Outline(parent);
            if (outline == null)
            {
                _log.Warn("Cannot read the parent view outline, so the cutting line cannot be placed.");
                return null;
            }

            double centerX = (outline[0] + outline[2]) / 2.0;
            double centerY = (outline[1] + outline[3]) / 2.0;
            double halfWidth = (outline[2] - outline[0]) / 2.0;
            double halfHeight = (outline[3] - outline[1]) / 2.0;

            // The cutting line must run past the geometry on both sides or
            // SOLIDWORKS will not accept it as a full section.
            const double Overrun = 1.2;
            double x1, y1, x2, y2;
            if (plan.SectionIsHorizontal)
            {
                x1 = centerX - halfWidth * Overrun; x2 = centerX + halfWidth * Overrun;
                y1 = y2 = centerY + Units.MmToMeter(plan.SectionOffsetMm);
            }
            else
            {
                y1 = centerY - halfHeight * Overrun; y2 = centerY + halfHeight * Overrun;
                x1 = x2 = centerX + Units.MmToMeter(plan.SectionOffsetMm);
            }

            if (!ActivateView(parent)) return null;

            object sketchManager;
            if (!SwDispatch.TryGet(_drawingModel, "SketchManager", out sketchManager, _log) || sketchManager == null)
            {
                _log.Warn("No sketch manager, so no cutting line can be drawn.");
                return null;
            }

            object lineResult;
            if (!SwDispatch.TryInvoke(sketchManager, "CreateLine",
                                      new object[] { x1, y1, 0.0, x2, y2, 0.0 }, out lineResult, _log) ||
                lineResult == null)
            {
                _log.Warn("Could not draw the cutting line for section " + plan.SectionLabel + ".");
                return null;
            }

            // Leaving the sketch open would attach the next annotation to it.
            SwDispatch.TryInvoke(sketchManager, "InsertSketch", new object[] { true }, out _, _log);

            object result;
            object[][ ] argumentLists =
            {
                new object[] { Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0,
                               false, false, null, 0 },
                new object[] { Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0,
                               false, false, null },
                new object[] { Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0 }
            };

            if (SwDispatch.TryInvokeAny(_drawing,
                                        new[] { "CreateSectionViewAt5", "CreateSectionViewAt4", "CreateSectionViewAt3" },
                                        argumentLists, out result, _log))
            {
                return result as View;
            }

            _log.Warn("This SOLIDWORKS release did not accept any known section view call; " +
                      "the cutting line is on the sheet and the section can be completed by hand.");
            return null;
        }

        private View CreateDetailView(ViewPlan plan)
        {
            View parent = Get(plan.ParentViewId);
            if (parent == null) return null;

            double[] outline = Outline(parent);
            if (outline == null) return null;

            double circleX = plan.DetailCenterXMm > 0
                ? Units.MmToMeter(plan.DetailCenterXMm)
                : (outline[0] + outline[2]) / 2.0;
            double circleY = plan.DetailCenterYMm > 0
                ? Units.MmToMeter(plan.DetailCenterYMm)
                : (outline[1] + outline[3]) / 2.0;
            double radius = plan.DetailRadiusMm > 0
                ? Units.MmToMeter(plan.DetailRadiusMm)
                : Math.Min(outline[2] - outline[0], outline[3] - outline[1]) * 0.2;

            if (!ActivateView(parent)) return null;

            object sketchManager;
            if (!SwDispatch.TryGet(_drawingModel, "SketchManager", out sketchManager, _log) || sketchManager == null)
                return null;

            object circle;
            if (!SwDispatch.TryInvoke(sketchManager, new[] { "CreateCircleByRadius", "CreateCircle" },
                                      new object[] { circleX, circleY, 0.0, radius }, out circle, _log) ||
                circle == null)
            {
                _log.Warn("Could not draw the detail circle.");
                return null;
            }

            double scale1 = plan.Scale != null ? plan.Scale.Numerator : 1.0;
            double scale2 = plan.Scale != null ? plan.Scale.Denominator : 1.0;
            string label = plan.SectionLabel ?? "B";

            object result;
            object[][] argumentLists =
            {
                new object[] { Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0,
                               SwConst.swDetailCircleStyle_Circle, 3, scale1 / scale2, label, false, false },
                new object[] { Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0,
                               SwConst.swDetailCircleStyle_Circle, 3, scale1 / scale2, label, false },
                new object[] { Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0 }
            };

            if (SwDispatch.TryInvokeAny(_drawing,
                                        new[] { "CreateDetailViewAt4", "CreateDetailViewAt3", "CreateDetailViewAt2" },
                                        argumentLists, out result, _log))
            {
                return result as View;
            }

            _log.Warn("Detail view creation is not available in this release; the detail circle is on the sheet.");
            return null;
        }

        private View CreateFlatPatternView(ViewPlan plan, string modelPath, string configuration)
        {
            object result;
            object[][] argumentLists =
            {
                new object[] { modelPath, configuration ?? string.Empty,
                               Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0,
                               plan.ShowBendNotes, true },
                new object[] { modelPath, configuration ?? string.Empty,
                               Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0,
                               plan.ShowBendNotes },
                new object[] { modelPath, configuration ?? string.Empty,
                               Units.MmToMeter(plan.CenterXMm), Units.MmToMeter(plan.CenterYMm), 0.0 }
            };

            if (SwDispatch.TryInvokeAny(_drawing,
                                        new[] { "CreateFlatPatternViewFromModelView3",
                                                "CreateFlatPatternViewFromModelView2",
                                                "CreateFlatPatternViewFromModelView" },
                                        argumentLists, out result, _log))
            {
                View view = result as View;
                if (view != null) return view;
            }

            _log.Warn("Flat pattern view creation failed. The part may have no valid flat pattern.");
            return null;
        }

        // ---- view properties -------------------------------------------------

        private void ApplyConfiguration(View view, string configuration)
        {
            if (string.IsNullOrEmpty(configuration)) return;
            SwDispatch.TrySet(view, "ReferencedConfiguration", configuration, _log);
        }

        private void ApplyScale(View view, ViewPlan plan)
        {
            if (plan.UseSheetScale || plan.Scale == null)
            {
                SwDispatch.TrySet(view, "UseSheetScale", 1, _log);
                return;
            }

            SwDispatch.TrySet(view, "UseSheetScale", 0, _log);
            if (!SwDispatch.TrySet(view, "ScaleRatio",
                                   new double[] { plan.Scale.Numerator, plan.Scale.Denominator }, _log))
            {
                SwDispatch.TrySet(view, "ScaleDecimal", plan.Scale.Value, _log);
            }
        }

        private void ApplyRotation(View view, ViewPlan plan)
        {
            if (Math.Abs(plan.RotationDegrees) < 1e-9) return;

            // IView::Angle is in radians, counter-clockwise.
            double radians = plan.RotationDegrees * Math.PI / 180.0;
            if (!SwDispatch.TrySet(view, "Angle", radians, _log))
            {
                _log.Warn(string.Format(CultureInfo.InvariantCulture,
                    "Could not rotate the view by {0}°; it will read with the axis vertical.", plan.RotationDegrees));
            }
        }

        private void ApplyDisplayMode(View view, ViewPlan plan)
        {
            int mode;
            switch (plan.Display)
            {
                case DisplayStyle.Wireframe: mode = SwConst.swViewDispWireframe; break;
                case DisplayStyle.HiddenLinesVisible: mode = SwConst.swViewDispHiddenLinesGrayed; break;
                case DisplayStyle.Shaded: mode = SwConst.swViewDispShaded; break;
                case DisplayStyle.ShadedWithEdges: mode = SwConst.swViewDispShadedWithEdges; break;
                default: mode = SwConst.swViewDispHiddenLinesRemoved; break;
            }

            if (SwDispatch.TrySet(view, "DisplayMode", mode, _log)) return;

            object ignored;
            if (SwDispatch.TryInvokeAny(view, new[] { "SetDisplayMode4", "SetDisplayMode3", "SetDisplayMode2" },
                                        new[] { new object[] { false, mode, false, false },
                                                new object[] { mode } },
                                        out ignored, _log))
            {
                return;
            }

            _log.Debug("Display mode could not be set; the view keeps the template's default.");
        }

        /// <summary>Moves a view so its centre lands on the planned point.</summary>
        public bool Position(View view, double centerXMm, double centerYMm)
        {
            double[] position = { Units.MmToMeter(centerXMm), Units.MmToMeter(centerYMm) };
            if (SwDispatch.TrySet(view, "Position", position, _log)) return true;

            object ignored;
            if (SwDispatch.TryInvoke(view, "SetPosition", new object[] { position }, out ignored, _log)) return true;

            _log.Warn("Could not move a view to its planned position.");
            return false;
        }

        /// <summary>View outline on the sheet in metres: x1, y1, x2, y2. Null when unavailable.</summary>
        public double[] Outline(View view)
        {
            object result;
            if (!SwDispatch.TryInvoke(view, "GetOutline", new object[0], out result, _log)) return null;

            double[] outline = SwDispatch.AsDoubles(result);
            if (outline == null || outline.Length < 4) return null;
            return outline;
        }

        /// <summary>Outline in millimetres as a sheet-space rectangle. Null when unavailable.</summary>
        public RectMm? OutlineMm(View view)
        {
            double[] outline = Outline(view);
            if (outline == null) return null;

            double left = Units.MeterToMm(Math.Min(outline[0], outline[2]));
            double bottom = Units.MeterToMm(Math.Min(outline[1], outline[3]));
            double width = Math.Abs(Units.MeterToMm(outline[2] - outline[0]));
            double height = Math.Abs(Units.MeterToMm(outline[3] - outline[1]));
            return new RectMm(left, bottom, width, height);
        }

        /// <summary>
        /// A view with a zero-size outline drew nothing, which normally means the
        /// configuration it references has no visible bodies.
        /// </summary>
        private bool LooksEmpty(View view)
        {
            RectMm? outline = OutlineMm(view);
            if (!outline.HasValue) return false;   // unknown is not empty
            return outline.Value.Width < 0.05 && outline.Value.Height < 0.05;
        }

        public string NameOf(View view)
        {
            object name;
            if (SwDispatch.TryGet(view, "Name", out name, _log) && name is string) return (string)name;
            if (SwDispatch.TryGet(view, "GetName2", out name, _log) && name is string) return (string)name;
            return string.Empty;
        }

        /// <summary>Selects a view so the next call acts on it.</summary>
        public bool SelectView(View view)
        {
            string name = NameOf(view);
            if (string.IsNullOrEmpty(name)) return false;

            SwDoc.ClearSelection(_drawingModel);
            if (SwDoc.Select(_drawingModel, name, SwConst.SelectTypeDrawingView, false, 0, _log)) return true;

            _log.Warn("Could not select view " + name + ".");
            return false;
        }

        /// <summary>Makes a view the active one, which sketch calls need.</summary>
        public bool ActivateView(View view)
        {
            string name = NameOf(view);
            if (string.IsNullOrEmpty(name)) return false;

            object result;
            if (SwDispatch.TryInvoke(_drawing, "ActivateView", new object[] { name }, out result, _log))
                return true;

            _log.Warn("Could not activate view " + name + ".");
            return false;
        }
    }
}
