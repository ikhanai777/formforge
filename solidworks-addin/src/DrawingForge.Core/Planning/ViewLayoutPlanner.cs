using System;
using System.Collections.Generic;
using System.Globalization;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Options;
using DrawingForge.Core.Standards;

namespace DrawingForge.Core.Planning
{
    /// <summary>An extra view that shares the sheet with the principal view block.</summary>
    public sealed class ExtraViewRequest
    {
        public ViewKind Kind { get; set; }

        /// <summary>Footprint at full size, millimetres.</summary>
        public double WidthMm { get; set; }
        public double HeightMm { get; set; }

        public string Label { get; set; }

        /// <summary>Scale override; null means the view follows the sheet scale.</summary>
        public Ratio Scale { get; set; }

        /// <summary>Section letter for a section view.</summary>
        public string SectionLabel { get; set; }

        /// <summary>Section cut runs horizontally through the principal view.</summary>
        public bool SectionIsHorizontal { get; set; }

        public DisplayStyle Display { get; set; }

        public ExtraViewRequest()
        {
            Display = DisplayStyle.HiddenLinesRemoved;
        }
    }

    /// <summary>What the caller wants laid out.</summary>
    public sealed class LayoutRequest
    {
        public LayoutRequest()
        {
            Extras = new List<ExtraViewRequest>();
            ViewGapMm = 25.0;
            AnnotationAllowance = ScaleSelector.DefaultAnnotationAllowance;
        }

        public PrincipalViewChoice Principal { get; set; }
        public ProjectionAngle Projection { get; set; }

        public bool IncludeTopView { get; set; }
        public bool IncludeSideView { get; set; }
        public bool IncludeIsometric { get; set; }

        public IList<ExtraViewRequest> Extras { get; private set; }

        /// <summary>Space between neighbouring views. Does not scale with the model.</summary>
        public double ViewGapMm { get; set; }

        /// <summary>Fraction of the area reserved for dimensions.</summary>
        public double AnnotationAllowance { get; set; }
    }

    /// <summary>Result of laying a request out on a sheet.</summary>
    public sealed class LayoutResult
    {
        public LayoutResult()
        {
            Views = new List<ViewPlan>();
            Overflow = new List<ExtraViewRequest>();
            Diagnostics = new List<string>();
        }

        public Ratio Scale { get; set; }
        public IList<ViewPlan> Views { get; private set; }

        /// <summary>Extra views that did not fit and need their own sheet.</summary>
        public IList<ExtraViewRequest> Overflow { get; private set; }

        /// <summary>False when even the smallest ladder scale overruns the sheet.</summary>
        public bool Fits { get; set; }

        public IList<string> Diagnostics { get; private set; }

        public ViewPlan Principal
        {
            get
            {
                foreach (ViewPlan v in Views)
                {
                    if (v.Kind == ViewKind.Principal) return v;
                }
                return null;
            }
        }
    }

    /// <summary>
    /// Places the view set on a sheet: picks the scale, then positions every
    /// view so that projected views stay aligned with their parent and nothing
    /// overlaps anything else.
    /// </summary>
    public static class ViewLayoutPlanner
    {
        /// <summary>cos 30°, the horizontal foreshortening of an isometric view.</summary>
        private const double IsoCos30 = 0.8660254037844387;

        /// <summary>sin 30°.</summary>
        private const double IsoSin30 = 0.5;

        /// <summary>
        /// How many ladder steps the principal views may give up to keep the
        /// auxiliary views on the same sheet before the auxiliaries are moved
        /// off instead.
        /// </summary>
        public const int MaxScaleStepsSpentOnExtras = 2;

        /// <summary>Position of a ratio on the ladder; -1 when it is not on it.</summary>
        private static int LadderIndex(IReadOnlyList<Ratio> ladder, Ratio ratio)
        {
            if (ladder == null || ratio == null) return -1;
            for (int i = 0; i < ladder.Count; i++)
            {
                if (Math.Abs(ladder[i].Value - ratio.Value) < 1e-9) return i;
            }
            return -1;
        }

        /// <summary>Footprint of an isometric view of a w x d x h box, at full size.</summary>
        public static void IsometricFootprint(double w, double d, double h,
                                              out double isoWidth, out double isoHeight)
        {
            isoWidth = (w + d) * IsoCos30;
            isoHeight = (w + d) * IsoSin30 + h;
        }

        /// <summary>
        /// Footprint of the principal block at full size, excluding the gaps
        /// between views (those are fixed sheet space and are returned
        /// separately).
        /// </summary>
        public static void CoreBlockSize(LayoutRequest request,
                                         out double blockWidth, out double blockHeight,
                                         out double gapWidth, out double gapHeight)
        {
            PrincipalViewChoice p = request.Principal;
            double w = p.WidthMm, h = p.HeightMm, d = p.DepthMm;

            double isoW = 0, isoH = 0;
            if (request.IncludeIsometric) IsometricFootprint(w, d, h, out isoW, out isoH);

            double rightColumn = Math.Max(request.IncludeSideView ? d : 0.0,
                                          request.IncludeIsometric ? isoW : 0.0);
            double topRow = Math.Max(request.IncludeTopView ? d : 0.0,
                                     request.IncludeIsometric ? isoH : 0.0);

            blockWidth = w + rightColumn;
            blockHeight = h + topRow;
            gapWidth = rightColumn > 0 ? request.ViewGapMm : 0.0;
            gapHeight = topRow > 0 ? request.ViewGapMm : 0.0;
        }

        /// <summary>
        /// Total footprint including the extras column, at full size, plus the
        /// fixed gap overhead. This is what the scale is chosen against.
        /// </summary>
        public static void TotalBlockSize(LayoutRequest request,
                                          out double blockWidth, out double blockHeight,
                                          out double gapWidth, out double gapHeight)
        {
            CoreBlockSize(request, out blockWidth, out blockHeight, out gapWidth, out gapHeight);

            if (request.Extras.Count == 0) return;

            double extrasWidth = 0, extrasHeight = 0;
            foreach (ExtraViewRequest extra in request.Extras)
            {
                extrasWidth = Math.Max(extrasWidth, extra.WidthMm);
                extrasHeight += extra.HeightMm;
            }

            blockWidth += extrasWidth;
            gapWidth += request.ViewGapMm;
            blockHeight = Math.Max(blockHeight, extrasHeight);
            gapHeight = Math.Max(gapHeight, request.ViewGapMm * (request.Extras.Count - 1));
        }

        /// <summary>
        /// Lays the request out on a sheet, choosing the scale from the ladder
        /// unless <paramref name="fixedScale"/> is supplied.
        /// </summary>
        public static LayoutResult Plan(LayoutRequest request,
                                        SheetSize sheet,
                                        IReadOnlyList<Ratio> ladder,
                                        Ratio fixedScale = null)
        {
            if (request == null) throw new ArgumentNullException("request");
            if (request.Principal == null) throw new ArgumentException("No principal view chosen.", "request");
            if (sheet == null) throw new ArgumentNullException("sheet");

            LayoutResult result = new LayoutResult();
            SheetLayout layout = new SheetLayout(sheet);
            RectMm area = layout.ViewArea;

            double blockW, blockH, gapW, gapH;
            TotalBlockSize(request, out blockW, out blockH, out gapW, out gapH);

            Ratio scale = fixedScale ?? ScaleSelector.Choose(ladder, blockW, blockH, area,
                                                             request.AnnotationAllowance, gapW, gapH);
            result.Scale = scale;
            result.Fits = ScaleSelector.Fits(scale, blockW, blockH, area,
                                             request.AnnotationAllowance, gapW, gapH);

            // Extras go to a continuation sheet either when nothing fits at all,
            // or when keeping them costs the principal views more than one step
            // on the scale ladder. Dropping from 1:1 to 1:2 to fit a section
            // view is normal drafting; dropping to 1:10 to fit one is not, and
            // the shop would rather turn a page than squint.
            if (request.Extras.Count > 0 && fixedScale == null)
            {
                double coreW, coreH, coreGapW, coreGapH;
                CoreBlockSize(request, out coreW, out coreH, out coreGapW, out coreGapH);
                Ratio coreScale = ScaleSelector.Choose(ladder, coreW, coreH, area,
                                                       request.AnnotationAllowance, coreGapW, coreGapH);
                bool coreFits = ScaleSelector.Fits(coreScale, coreW, coreH, area,
                                                   request.AnnotationAllowance, coreGapW, coreGapH);

                int stepsLost = LadderIndex(ladder, scale) - LadderIndex(ladder, coreScale);
                bool tooMuchScaleLost = coreFits && stepsLost >= MaxScaleStepsSpentOnExtras;

                if (coreFits && (!result.Fits || tooMuchScaleLost))
                {
                    foreach (ExtraViewRequest extra in request.Extras) result.Overflow.Add(extra);
                    result.Diagnostics.Add(string.Format(CultureInfo.InvariantCulture,
                        "{0} auxiliary view(s) moved to a continuation sheet so the principal views could stay at {1} instead of {2}.",
                        request.Extras.Count, coreScale, scale));
                    scale = coreScale;
                    result.Scale = coreScale;
                    result.Fits = true;
                    blockW = coreW; blockH = coreH; gapW = coreGapW; gapH = coreGapH;
                    request = WithoutExtras(request);
                }
            }

            if (!result.Fits)
            {
                result.Diagnostics.Add(string.Format(CultureInfo.InvariantCulture,
                    "The view set does not fit on sheet {0} even at {1}; a larger sheet is needed.",
                    sheet.Name, scale));
            }

            PlaceViews(request, result, scale, area, gapW, gapH);
            return result;
        }

        private static LayoutRequest WithoutExtras(LayoutRequest request)
        {
            LayoutRequest copy = new LayoutRequest
            {
                Principal = request.Principal,
                Projection = request.Projection,
                IncludeTopView = request.IncludeTopView,
                IncludeSideView = request.IncludeSideView,
                IncludeIsometric = request.IncludeIsometric,
                ViewGapMm = request.ViewGapMm,
                AnnotationAllowance = request.AnnotationAllowance
            };
            return copy;
        }

        private static void PlaceViews(LayoutRequest request, LayoutResult result, Ratio scale,
                                       RectMm area, double gapW, double gapH)
        {
            PrincipalViewChoice p = request.Principal;
            double s = scale.Value;
            double gap = request.ViewGapMm;

            double w = p.WidthMm * s;
            double h = p.HeightMm * s;
            double d = p.DepthMm * s;

            double isoW = 0, isoH = 0;
            if (request.IncludeIsometric)
                IsometricFootprint(w, d, h, out isoW, out isoH);

            double rightColumn = Math.Max(request.IncludeSideView ? d : 0.0,
                                          request.IncludeIsometric ? isoW : 0.0);
            double topRow = Math.Max(request.IncludeTopView ? d : 0.0,
                                     request.IncludeIsometric ? isoH : 0.0);

            double coreW = w + (rightColumn > 0 ? gap + rightColumn : 0);
            double coreH = h + (topRow > 0 ? gap + topRow : 0);

            double extrasWidth = 0, extrasHeight = 0;
            List<double[]> extraSizes = new List<double[]>();
            foreach (ExtraViewRequest extra in request.Extras)
            {
                double es = extra.Scale != null ? extra.Scale.Value : s;
                double ew = extra.WidthMm * es;
                double eh = extra.HeightMm * es;
                extraSizes.Add(new double[] { ew, eh });
                extrasWidth = Math.Max(extrasWidth, ew);
                extrasHeight += eh;
            }
            if (extraSizes.Count > 1) extrasHeight += gap * (extraSizes.Count - 1);

            double totalW = coreW + (extrasWidth > 0 ? gap + extrasWidth : 0);
            double totalH = Math.Max(coreH, extrasHeight);

            // Centre the whole arrangement in the view area.
            double originX = area.Left + (area.Width - totalW) / 2.0;
            double originY = area.Bottom + (area.Height - totalH) / 2.0;
            if (originX < area.Left) originX = area.Left;
            if (originY < area.Bottom) originY = area.Bottom;

            double coreBottom = originY + (totalH - coreH) / 2.0;

            ViewPlan principal = new ViewPlan
            {
                Kind = ViewKind.Principal,
                Orientation = p.Orientation,
                SwViewName = p.SwViewName,
                RotationDegrees = p.RotationDegrees,
                WidthMm = w,
                HeightMm = h,
                UseSheetScale = true,
                Label = null
            };

            bool third = request.Projection == ProjectionAngle.Third;

            if (third)
            {
                principal.CenterXMm = originX + w / 2.0;
                principal.CenterYMm = coreBottom + h / 2.0;
            }
            else
            {
                // First angle puts the principal view at the top of the block:
                // the top view drops below it and the left-side view goes right.
                principal.CenterXMm = originX + w / 2.0;
                principal.CenterYMm = coreBottom + coreH - h / 2.0;
            }
            result.Views.Add(principal);

            if (request.IncludeTopView)
            {
                double cy = third
                    ? principal.CenterYMm + h / 2.0 + gap + d / 2.0
                    : principal.CenterYMm - h / 2.0 - gap - d / 2.0;

                result.Views.Add(new ViewPlan
                {
                    Kind = ViewKind.Projected,
                    ParentViewId = principal.Id,
                    Direction = third ? ProjectionDirection.Up : ProjectionDirection.Down,
                    CenterXMm = principal.CenterXMm,
                    CenterYMm = cy,
                    WidthMm = w,
                    HeightMm = d,
                    UseSheetScale = true,
                    // In third angle the view above the front is the top view;
                    // in first angle the view below it is. Either way the label
                    // is left off: an aligned projected view is not labelled.
                    Label = null
                });
            }

            if (request.IncludeSideView)
            {
                result.Views.Add(new ViewPlan
                {
                    Kind = ViewKind.Projected,
                    ParentViewId = principal.Id,
                    Direction = ProjectionDirection.Right,
                    CenterXMm = principal.CenterXMm + w / 2.0 + gap + d / 2.0,
                    CenterYMm = principal.CenterYMm,
                    WidthMm = d,
                    HeightMm = h,
                    UseSheetScale = true,
                    Label = null
                });
            }

            if (request.IncludeIsometric)
            {
                double isoCx = originX + w + gap + rightColumn / 2.0;
                double isoCy = third
                    ? coreBottom + h + gap + topRow / 2.0
                    : coreBottom + topRow / 2.0;

                result.Views.Add(new ViewPlan
                {
                    Kind = ViewKind.Isometric,
                    SwViewName = "*Isometric",
                    CenterXMm = isoCx,
                    CenterYMm = isoCy,
                    WidthMm = isoW,
                    HeightMm = isoH,
                    UseSheetScale = true,
                    Display = DisplayStyle.ShadedWithEdges,
                    // An isometric view carries no dimensions: it is there to
                    // make the part readable, and dimensioning it is a defect.
                    InsertModelDimensions = false,
                    InsertCenterMarks = false,
                    InsertCenterlines = false,
                    InsertHoleCallouts = false,
                    Label = "ISOMETRIC"
                });
            }

            // Extras stack in a column to the right of the core block, top down.
            double extraTop = originY + totalH;
            double extraLeft = originX + coreW + (extrasWidth > 0 ? gap : 0);
            for (int i = 0; i < request.Extras.Count; i++)
            {
                ExtraViewRequest extra = request.Extras[i];
                double ew = extraSizes[i][0];
                double eh = extraSizes[i][1];

                ViewPlan view = new ViewPlan
                {
                    Kind = extra.Kind,
                    CenterXMm = extraLeft + extrasWidth / 2.0,
                    CenterYMm = extraTop - eh / 2.0,
                    WidthMm = ew,
                    HeightMm = eh,
                    Scale = extra.Scale,
                    UseSheetScale = extra.Scale == null,
                    Label = extra.Label,
                    SectionLabel = extra.SectionLabel,
                    SectionIsHorizontal = extra.SectionIsHorizontal,
                    Display = extra.Display,
                    ParentViewId = (extra.Kind == ViewKind.Section || extra.Kind == ViewKind.Detail)
                        ? principal.Id : null
                };
                result.Views.Add(view);
                extraTop -= eh + gap;
            }
        }

        /// <summary>
        /// Checks that no two views overlap and that every view is inside the
        /// view area. Run on every plan before it is executed.
        /// </summary>
        public static IList<string> Validate(LayoutResult result, SheetSize sheet, double clearanceMm = 2.0)
        {
            List<string> problems = new List<string>();
            if (result == null || sheet == null) return problems;

            RectMm area = new SheetLayout(sheet).ViewArea;

            for (int i = 0; i < result.Views.Count; i++)
            {
                ViewPlan a = result.Views[i];
                if (!area.Contains(a.Envelope, clearanceMm))
                {
                    problems.Add(string.Format(CultureInfo.InvariantCulture,
                        "View {0} ({1}) falls outside the drawable area of sheet {2}.",
                        a.Id, a.Kind, sheet.Name));
                }

                for (int j = i + 1; j < result.Views.Count; j++)
                {
                    ViewPlan b = result.Views[j];
                    if (a.Envelope.Intersects(b.Envelope))
                    {
                        problems.Add(string.Format(CultureInfo.InvariantCulture,
                            "Views {0} ({1}) and {2} ({3}) overlap.", a.Id, a.Kind, b.Id, b.Kind));
                    }
                }
            }
            return problems;
        }
    }
}
