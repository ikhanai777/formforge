using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Naming;
using DrawingForge.Core.Options;
using DrawingForge.Core.Standards;

namespace DrawingForge.Core.Planning
{
    /// <summary>
    /// Turns a <see cref="PartSummary"/> plus <see cref="DrawingOptions"/> into
    /// a complete <see cref="DrawingPlan"/>.
    /// </summary>
    /// <remarks>
    /// This is where the drawing is actually designed. Everything downstream
    /// just executes what comes out of here, which is why the decisions worth
    /// arguing about — sheet, scale, view set, what gets sectioned — all live
    /// in one testable place.
    /// </remarks>
    public static class DrawingPlanner
    {
        /// <summary>Minimum detail-circle radius on the model, millimetres.</summary>
        private const double MinDetailRadiusMm = 4.0;

        public static DrawingPlan Plan(PartSummary part, DrawingOptions options)
        {
            if (part == null) throw new ArgumentNullException("part");
            if (options == null) throw new ArgumentNullException("options");

            StandardProfile profile = options.Profile();
            DrawingPlan plan = new DrawingPlan
            {
                SourceModelPath = part.FilePath,
                Configuration = part.Configuration,
                PartNumber = part.PartNumber,
                Standard = options.Standard,
                Projection = options.EffectiveProjection(),
                Units = options.Units,
                TemplatePath = options.DrawingTemplatePath,
                OutputFilePath = ResolveOutputPath(part, options)
            };

            if (part.IsAssembly)
            {
                PlanAssembly(plan, part, options, profile);
            }
            else
            {
                PlanPart(plan, part, options, profile);
            }

            foreach (string note in NoteBlockComposer.Compose(part, options))
                plan.Notes.Add(note);

            FillTitleBlock(plan, part, options, profile);
            return plan;
        }

        // ---- part drawings --------------------------------------------------

        private static void PlanPart(DrawingPlan plan, PartSummary part, DrawingOptions options,
                                     StandardProfile profile)
        {
            PrincipalViewChoice principal = FrontViewChooser.Choose(part, options.RespectModelOrientation);
            plan.Diagnostics.Add(string.Format(CultureInfo.InvariantCulture,
                "Principal view: {0}{1} — {2}.",
                principal.SwViewName,
                Math.Abs(principal.RotationDegrees) > 1e-9 ? " rotated 90°" : string.Empty,
                principal.Rationale));

            LayoutRequest request = new LayoutRequest
            {
                Principal = principal,
                Projection = plan.Projection,
                IncludeTopView = options.IncludeTopView,
                IncludeSideView = options.IncludeSideView,
                IncludeIsometric = options.IncludeIsometric,
                ViewGapMm = options.ViewGapMm,
                AnnotationAllowance = options.AnnotationAllowance
            };

            // A body of revolution does not need three orthographic views: the
            // axial view plus a section says everything a lathe needs, and the
            // end view only repeats the diameters.
            if (part.IsRotational && options.IncludeTopView && options.IncludeSideView)
            {
                request.IncludeTopView = false;
                plan.Diagnostics.Add("Rotational part: the top view is dropped as it repeats the diameters.");
            }

            IReadOnlyList<Ratio> ladder = EffectiveLadder(profile, options);
            SheetSize sheet;
            LayoutResult layout = LayoutOnBestSheet(request, part, options, ladder, plan, out sheet);

            SheetPlan sheet1 = new SheetPlan
            {
                Name = "Sheet1",
                Size = sheet,
                Scale = layout.Scale,
                Projection = plan.Projection,
                SheetFormatPath = options.SheetFormatPath,
                ShowProjectionSymbol = options.ShowProjectionSymbol,
                ShowNoteBlock = options.IncludeGeneralNotes
            };

            SheetLayout sheetLayout = new SheetLayout(sheet);
            sheet1.NoteBlockXMm = sheetLayout.NotesArea.Left;
            sheet1.NoteBlockYMm = sheetLayout.NotesArea.Top;
            if (options.IncludeGeneralNotes && sheetLayout.NotesAreaIsCramped)
            {
                plan.Diagnostics.Add(string.Format(CultureInfo.InvariantCulture,
                    "Sheet {0} has little room beside the title block; the note block may need moving by hand.",
                    sheet.Name));
            }

            ApplyViewOptions(layout.Views, options);
            foreach (ViewPlan view in layout.Views) sheet1.Views.Add(view);
            foreach (string diagnostic in layout.Diagnostics) plan.Diagnostics.Add(diagnostic);

            if (part.IsWeldment && options.WeldmentCutList)
            {
                sheet1.Tables.Add(new TablePlan
                {
                    Kind = TableKind.WeldmentCutList,
                    AnchorXMm = sheetLayout.ViewArea.Left,
                    AnchorYMm = sheetLayout.ViewArea.Top
                });
            }

            plan.Sheets.Add(sheet1);

            if (layout.Overflow.Count > 0)
                AddContinuationSheet(plan, layout.Overflow, sheet, layout.Scale, options);

            if (part.IsSheetMetal && options.SheetMetalFlatPattern)
                AddFlatPatternSheet(plan, part, options, ladder, sheet, layout.Scale, sheet1);

            IList<string> overlaps = ViewLayoutPlanner.Validate(layout, sheet);
            foreach (string overlap in overlaps) plan.Diagnostics.Add(overlap);
        }

        /// <summary>
        /// Walks the sheet series and stops at the smallest sheet that holds the
        /// views at a scale worth issuing.
        /// </summary>
        private static LayoutResult LayoutOnBestSheet(LayoutRequest request, PartSummary part,
                                                      DrawingOptions options, IReadOnlyList<Ratio> ladder,
                                                      DrawingPlan plan, out SheetSize chosen)
        {
            Ratio fixedScale = options.FixedScale();

            if (options.SheetMode != SheetSelectionMode.AutoSmallestFit)
            {
                chosen = options.ResolveSheet();
                AddExtras(request, part, options, chosen, ladder, fixedScale);
                LayoutResult fixedLayout = ViewLayoutPlanner.Plan(request, chosen, ladder, fixedScale);
                if (!fixedLayout.Fits)
                {
                    plan.Diagnostics.Add(string.Format(CultureInfo.InvariantCulture,
                        "The views overrun sheet {0}. Switch to automatic sheet selection or a smaller scale.",
                        chosen.Name));
                }
                return fixedLayout;
            }

            IReadOnlyList<SheetSize> series = SheetCatalog.Series(options.Standard);

            foreach (SheetSize candidate in series)
            {
                LayoutRequest attempt = CloneRequest(request);
                AddExtras(attempt, part, options, candidate, ladder, fixedScale);
                LayoutResult result = ViewLayoutPlanner.Plan(attempt, candidate, ladder, fixedScale);

                if (result.Fits && result.Scale.Value >= options.MinAcceptableAutoScale)
                {
                    chosen = candidate;
                    plan.Diagnostics.Add(string.Format(CultureInfo.InvariantCulture,
                        "Sheet {0} at {1} — smallest sheet in the {2} series that holds the view set.",
                        candidate.Name, result.Scale, options.Standard));
                    CopyRequest(attempt, request);
                    return result;
                }
            }

            // Nothing met the scale floor: take the largest sheet and say so.
            SheetSize largest = series[series.Count - 1];
            LayoutRequest final = CloneRequest(request);
            AddExtras(final, part, options, largest, ladder, fixedScale);
            LayoutResult finalLayout = ViewLayoutPlanner.Plan(final, largest, ladder, fixedScale);
            chosen = largest;
            CopyRequest(final, request);

            plan.Diagnostics.Add(string.Format(CultureInfo.InvariantCulture,
                "No sheet in the {0} series holds this part above {1:0.###}× scale; using {2} at {3}.",
                options.Standard, options.MinAcceptableAutoScale, largest.Name, finalLayout.Scale));

            return finalLayout;
        }

        private static LayoutRequest CloneRequest(LayoutRequest source)
        {
            LayoutRequest copy = new LayoutRequest
            {
                Principal = source.Principal,
                Projection = source.Projection,
                IncludeTopView = source.IncludeTopView,
                IncludeSideView = source.IncludeSideView,
                IncludeIsometric = source.IncludeIsometric,
                ViewGapMm = source.ViewGapMm,
                AnnotationAllowance = source.AnnotationAllowance
            };
            foreach (ExtraViewRequest extra in source.Extras) copy.Extras.Add(extra);
            return copy;
        }

        private static void CopyRequest(LayoutRequest source, LayoutRequest target)
        {
            target.Extras.Clear();
            foreach (ExtraViewRequest extra in source.Extras) target.Extras.Add(extra);
        }

        /// <summary>
        /// Adds the section and detail views the part earns. Called per sheet
        /// candidate because the detail view's scale depends on the sheet scale.
        /// </summary>
        private static void AddExtras(LayoutRequest request, PartSummary part, DrawingOptions options,
                                      SheetSize sheet, IReadOnlyList<Ratio> ladder, Ratio fixedScale)
        {
            request.Extras.Clear();
            PrincipalViewChoice p = request.Principal;

            if (options.IncludeSectionWhenInternalFeatures && part.HasInternalFeatures)
            {
                // Cut along whichever axis keeps the extra column narrow: a
                // vertical cutting line yields a depth-wide view, a horizontal
                // one yields a width-wide view.
                bool horizontalCut = p.DepthMm > p.WidthMm;
                request.Extras.Add(new ExtraViewRequest
                {
                    Kind = ViewKind.Section,
                    SectionIsHorizontal = horizontalCut,
                    SectionLabel = "A",
                    Label = "SECTION A-A",
                    WidthMm = horizontalCut ? p.WidthMm : p.DepthMm,
                    HeightMm = horizontalCut ? p.DepthMm : p.HeightMm
                });
            }

            if (options.IncludeDetailViewsForSmallFeatures &&
                part.SmallestFeatureMm > 0 &&
                part.SmallestFeatureMm < options.SmallFeatureThresholdMm)
            {
                // The detail view exists so a 1 mm feature is readable, so its
                // scale is a multiple of the sheet scale, snapped to the ladder.
                double sheetScaleValue = EstimateSheetScale(request, sheet, ladder, fixedScale);
                Ratio detailScale = ScaleSelector.Snap(ladder, sheetScaleValue * options.DetailViewScaleMultiplier);
                double radius = Math.Max(MinDetailRadiusMm, part.SmallestFeatureMm * 2.5);

                request.Extras.Add(new ExtraViewRequest
                {
                    Kind = ViewKind.Detail,
                    Scale = detailScale,
                    Label = string.Format(CultureInfo.InvariantCulture, "DETAIL B  ({0})", detailScale),
                    WidthMm = radius * 2.0,
                    HeightMm = radius * 2.0
                });
            }
        }

        private static double EstimateSheetScale(LayoutRequest request, SheetSize sheet,
                                                 IReadOnlyList<Ratio> ladder, Ratio fixedScale)
        {
            if (fixedScale != null) return fixedScale.Value;

            LayoutRequest core = CloneRequest(request);
            core.Extras.Clear();
            double blockW, blockH, gapW, gapH;
            ViewLayoutPlanner.TotalBlockSize(core, out blockW, out blockH, out gapW, out gapH);
            RectMm area = new SheetLayout(sheet).ViewArea;
            return ScaleSelector.Choose(ladder, blockW, blockH, area,
                                        request.AnnotationAllowance, gapW, gapH).Value;
        }

        private static void AddContinuationSheet(DrawingPlan plan, IList<ExtraViewRequest> overflow,
                                                 SheetSize sheet, Ratio scale, DrawingOptions options)
        {
            SheetPlan continuation = new SheetPlan
            {
                Name = string.Format(CultureInfo.InvariantCulture, "Sheet{0}", plan.Sheets.Count + 1),
                Size = sheet,
                Scale = scale,
                Projection = plan.Projection,
                SheetFormatPath = options.SheetFormatPath,
                ShowProjectionSymbol = false,
                ShowNoteBlock = false
            };

            RectMm area = new SheetLayout(sheet).ViewArea;
            double y = area.Top;
            foreach (ExtraViewRequest extra in overflow)
            {
                double s = extra.Scale != null ? extra.Scale.Value : scale.Value;
                double w = extra.WidthMm * s;
                double h = extra.HeightMm * s;
                continuation.Views.Add(new ViewPlan
                {
                    Kind = extra.Kind,
                    CenterXMm = area.CenterX,
                    CenterYMm = y - h / 2.0,
                    WidthMm = w,
                    HeightMm = h,
                    Scale = extra.Scale,
                    UseSheetScale = extra.Scale == null,
                    Label = extra.Label,
                    SectionLabel = extra.SectionLabel,
                    SectionIsHorizontal = extra.SectionIsHorizontal,
                    Display = extra.Display
                });
                y -= h + options.ViewGapMm;
            }

            plan.Sheets.Add(continuation);
            plan.Diagnostics.Add(string.Format(CultureInfo.InvariantCulture,
                "{0} view(s) continued on {1}.", overflow.Count, continuation.Name));
        }

        private static void AddFlatPatternSheet(DrawingPlan plan, PartSummary part, DrawingOptions options,
                                                IReadOnlyList<Ratio> ladder, SheetSize sheet, Ratio sheetScale,
                                                SheetPlan firstSheet)
        {
            BoundingBox flat = part.FlatPatternBox ?? part.Box;
            if (flat == null) return;

            // The flat pattern lies in a plane: take its two largest extents.
            double[] sorted = flat.SortedExtents();
            double flatW = sorted[2];
            double flatH = sorted[1];

            RectMm area = new SheetLayout(sheet).ViewArea;
            Ratio flatScale = options.FixedScale() ??
                ScaleSelector.Choose(ladder, flatW, flatH, area, options.AnnotationAllowance);

            double flatWidthOnSheet = flatW * flatScale.Value;
            double flatHeightOnSheet = flatH * flatScale.Value;

            // Sharing sheet 1 only works if there is genuinely room left beside
            // the formed views. Dropping the flat pattern on top of them would
            // be worse than the extra sheet the user asked to avoid.
            double centerX = area.CenterX;
            double centerY = area.CenterY;
            bool ownSheet = options.FlatPatternOnSeparateSheet;

            if (!ownSheet)
            {
                if (!TryPlaceBeside(firstSheet, area, flatWidthOnSheet, flatHeightOnSheet,
                                    options.ViewGapMm, out centerX, out centerY))
                {
                    ownSheet = true;
                    plan.Diagnostics.Add(
                        "No room for the flat pattern beside the formed views, so it goes on its own sheet.");
                }
            }

            SheetPlan target;
            if (ownSheet)
            {
                target = new SheetPlan
                {
                    Name = string.Format(CultureInfo.InvariantCulture, "Sheet{0}", plan.Sheets.Count + 1),
                    Size = sheet,
                    Projection = plan.Projection,
                    SheetFormatPath = options.SheetFormatPath,
                    ShowProjectionSymbol = false,
                    ShowNoteBlock = false
                };
                plan.Sheets.Add(target);
                centerX = area.CenterX;
                centerY = area.CenterY;
            }
            else
            {
                target = firstSheet;
            }

            target.Scale = target.Scale ?? flatScale;

            target.Views.Add(new ViewPlan
            {
                Kind = ViewKind.FlatPattern,
                CenterXMm = centerX,
                CenterYMm = centerY,
                WidthMm = flatWidthOnSheet,
                HeightMm = flatHeightOnSheet,
                Scale = flatScale,
                UseSheetScale = false,
                Label = string.Format(CultureInfo.InvariantCulture, "FLAT PATTERN  ({0})", flatScale),
                ShowBendNotes = true,
                // Flat pattern dimensions come from the flat, not the model:
                // importing model items here produces formed-state numbers.
                InsertModelDimensions = false,
                InsertHoleCallouts = true,
                InsertCenterMarks = true
            });

            if (options.SheetMetalBendTable)
            {
                target.Tables.Add(new TablePlan
                {
                    Kind = TableKind.SheetMetalBendTable,
                    AnchorXMm = area.Left,
                    AnchorYMm = area.Top
                });
            }

            plan.Diagnostics.Add(string.Format(CultureInfo.InvariantCulture,
                "Sheet metal part: flat pattern added at {0} on {1}.", flatScale, target.Name));
        }

        /// <summary>
        /// Finds a spot for one more view on a sheet that already has some:
        /// to the right of everything, or above it. Returns false when neither
        /// leaves the view inside the drawable area.
        /// </summary>
        private static bool TryPlaceBeside(SheetPlan sheet, RectMm area, double widthMm, double heightMm,
                                           double gapMm, out double centerXMm, out double centerYMm)
        {
            centerXMm = area.CenterX;
            centerYMm = area.CenterY;
            if (sheet == null) return false;

            double occupiedRight = area.Left;
            double occupiedTop = area.Bottom;
            foreach (ViewPlan existing in sheet.Views)
            {
                occupiedRight = Math.Max(occupiedRight, existing.Envelope.Right);
                occupiedTop = Math.Max(occupiedTop, existing.Envelope.Top);
            }

            double rightX = occupiedRight + gapMm + widthMm / 2.0;
            if (rightX + widthMm / 2.0 <= area.Right)
            {
                centerXMm = rightX;
                centerYMm = area.Bottom + heightMm / 2.0;
                return centerYMm + heightMm / 2.0 <= area.Top;
            }

            double aboveY = occupiedTop + gapMm + heightMm / 2.0;
            if (aboveY + heightMm / 2.0 <= area.Top)
            {
                centerXMm = area.Left + widthMm / 2.0;
                centerYMm = aboveY;
                return centerXMm + widthMm / 2.0 <= area.Right;
            }

            return false;
        }

        // ---- assembly drawings ----------------------------------------------

        private static void PlanAssembly(DrawingPlan plan, PartSummary part, DrawingOptions options,
                                         StandardProfile profile)
        {
            PrincipalViewChoice principal = FrontViewChooser.Choose(part, options.RespectModelOrientation);

            LayoutRequest request = new LayoutRequest
            {
                Principal = principal,
                Projection = plan.Projection,
                // An assembly drawing is an identification and installation
                // document: one orthographic view plus an isometric, with the
                // BOM doing the rest. Three fully dimensioned views of an
                // assembly are noise.
                IncludeTopView = false,
                IncludeSideView = options.IncludeSideView,
                IncludeIsometric = true,
                ViewGapMm = options.ViewGapMm,
                AnnotationAllowance = options.AnnotationAllowance
            };

            IReadOnlyList<Ratio> ladder = EffectiveLadder(profile, options);
            SheetSize sheet;
            LayoutResult layout = LayoutOnBestSheet(request, part, options, ladder, plan, out sheet);

            SheetPlan sheet1 = new SheetPlan
            {
                Name = "Sheet1",
                Size = sheet,
                Scale = layout.Scale,
                Projection = plan.Projection,
                SheetFormatPath = options.SheetFormatPath,
                ShowProjectionSymbol = options.ShowProjectionSymbol,
                ShowNoteBlock = options.IncludeGeneralNotes
            };

            SheetLayout sheetLayout = new SheetLayout(sheet);
            sheet1.NoteBlockXMm = sheetLayout.NotesArea.Left;
            sheet1.NoteBlockYMm = sheetLayout.NotesArea.Top;

            string isoViewId = null;
            foreach (ViewPlan view in layout.Views)
            {
                // Assembly views carry no model dimensions; the BOM and the
                // balloons carry the information instead.
                view.InsertModelDimensions = false;
                view.InsertHoleCallouts = false;
                view.Kind = view.Kind == ViewKind.Isometric ? ViewKind.Isometric : view.Kind;

                if (view.Kind == ViewKind.Isometric)
                {
                    isoViewId = view.Id;
                    view.Exploded = options.AssemblyExplodedView && part.HasExplodedView;
                    if (view.Exploded) view.Label = "ISOMETRIC — EXPLODED";
                }
                sheet1.Views.Add(view);
            }

            if (options.AssemblyBom)
            {
                sheet1.Tables.Add(new TablePlan
                {
                    Kind = TableKind.BillOfMaterials,
                    AnchorXMm = sheetLayout.ViewArea.Right,
                    AnchorYMm = sheetLayout.ViewArea.Top,
                    AttachedViewId = isoViewId,
                    AutoBalloon = options.AssemblyBalloons
                });
            }

            plan.Sheets.Add(sheet1);
            foreach (string diagnostic in layout.Diagnostics) plan.Diagnostics.Add(diagnostic);

            if (options.AssemblyExplodedView && !part.HasExplodedView)
            {
                plan.Diagnostics.Add(
                    "No exploded view is saved in this configuration, so the isometric shows the assembled state.");
            }
        }

        // ---- shared ---------------------------------------------------------

        private static void ApplyViewOptions(IEnumerable<ViewPlan> views, DrawingOptions options)
        {
            foreach (ViewPlan view in views)
            {
                if (view.Kind == ViewKind.Isometric) continue;

                view.InsertModelDimensions &= options.InsertModelDimensions;
                view.InsertCenterMarks &= options.InsertCenterMarks;
                view.InsertCenterlines &= options.InsertCenterlines;
                view.InsertHoleCallouts &= options.InsertHoleCallouts;

                if (view.Kind == ViewKind.Principal && options.ShowHiddenLinesOnPrincipalView)
                    view.Display = DisplayStyle.HiddenLinesVisible;
            }
        }

        /// <summary>
        /// The standard's ladder, capped by the user's enlargement and reduction
        /// limits.
        /// </summary>
        public static IReadOnlyList<Ratio> EffectiveLadder(StandardProfile profile, DrawingOptions options)
        {
            double maxUp = options.MaxEnlargement > 0 ? options.MaxEnlargement : double.MaxValue;
            double minDown = options.MaxReduction > 0 ? 1.0 / options.MaxReduction : 0.0;

            List<Ratio> filtered = profile.ScaleLadder
                .Where(r => r.Value <= maxUp + 1e-9 && r.Value >= minDown - 1e-9)
                .ToList();

            if (filtered.Count == 0) filtered.Add(new Ratio(1, 1));
            return filtered;
        }

        private static void FillTitleBlock(DrawingPlan plan, PartSummary part, DrawingOptions options,
                                           StandardProfile profile)
        {
            IDictionary<string, string> props = plan.TitleBlockProperties;

            props["DrawnBy"] = options.DrawnBy ?? string.Empty;
            props["DrawnDate"] = DateTime.Now.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
            props["Company"] = options.CompanyName ?? string.Empty;
            props["PartNo"] = part.PartNumber ?? string.Empty;
            props["Description"] = part.Description ?? string.Empty;
            props["Material"] = part.Material ?? string.Empty;
            props["Finish"] = part.Finish ?? string.Empty;
            props["Revision"] = !string.IsNullOrEmpty(part.Revision) ? part.Revision : (options.DefaultRevision ?? string.Empty);
            props["Configuration"] = part.Configuration ?? string.Empty;
            props["DrawingStandard"] = profile.DimensioningSpec;
            props["ProjectionAngle"] = plan.Projection == ProjectionAngle.Third ? "THIRD ANGLE" : "FIRST ANGLE";
            props["Units"] = options.Units == UnitSystem.Inch ? "INCH" : "MM";
            props["GeneralTolerance"] = profile.GeneralToleranceNote;

            if (plan.Sheets.Count > 0)
            {
                props["SheetSize"] = plan.Sheets[0].Size.Name;
                props["Scale"] = plan.Sheets[0].Scale != null ? plan.Sheets[0].Scale.ToString() : "1:1";
            }
            props["SheetCount"] = plan.Sheets.Count.ToString(CultureInfo.InvariantCulture);

            if (part.MassGrams > 0)
            {
                props["Weight"] = options.Units == UnitSystem.Inch
                    ? string.Format(CultureInfo.InvariantCulture, "{0:0.###} LB", part.MassGrams / 453.59237)
                    : string.Format(CultureInfo.InvariantCulture, "{0:0.###} KG", part.MassGrams / 1000.0);
            }

            // Anything the user mapped explicitly wins over the defaults above.
            if (options.TitleBlockPropertyMap != null)
            {
                foreach (PropertyMapping mapping in options.TitleBlockPropertyMap)
                {
                    if (mapping == null || string.IsNullOrEmpty(mapping.DrawingProperty)) continue;
                    string value;
                    if (part.CustomProperties.TryGetValue(mapping.ModelProperty ?? string.Empty, out value) &&
                        !string.IsNullOrEmpty(value))
                    {
                        props[mapping.DrawingProperty] = value;
                    }
                }
            }
        }

        private static string ResolveOutputPath(PartSummary part, DrawingOptions options)
        {
            string baseName = FileNameBuilder.Build(options.FileNamePattern, part, options);
            string folder = options.OutputFolder;

            if (string.IsNullOrEmpty(folder) && !string.IsNullOrEmpty(part.FilePath))
            {
                try
                {
                    folder = System.IO.Path.GetDirectoryName(part.FilePath);
                }
                catch (ArgumentException)
                {
                    folder = string.Empty;
                }
            }

            string fileName = baseName + ".SLDDRW";
            return string.IsNullOrEmpty(folder) ? fileName : System.IO.Path.Combine(folder, fileName);
        }
    }
}
