using System.Linq;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;
using DrawingForge.Core.Standards;
using Xunit;

namespace DrawingForge.Core.Tests;

public class DrawingPlannerTests
{
    [Fact]
    public void BracketGetsOneSheetWithThreeViewsAndAnIsometric()
    {
        var plan = DrawingPlanner.Plan(Samples.Bracket(), Samples.AsmeOptions());

        Assert.Single(plan.Sheets);
        var sheet = plan.Sheets[0];
        Assert.Equal(1, sheet.Views.Count(v => v.Kind == ViewKind.Principal));
        Assert.Equal(2, sheet.Views.Count(v => v.Kind == ViewKind.Projected));
        Assert.Equal(1, sheet.Views.Count(v => v.Kind == ViewKind.Isometric));
        Assert.Equal(ProjectionAngle.Third, plan.Projection);
    }

    [Fact]
    public void IsoOptionsProduceAFirstAngleDrawing()
    {
        var plan = DrawingPlanner.Plan(Samples.Bracket(), Samples.IsoOptions());
        Assert.Equal(ProjectionAngle.First, plan.Projection);
        Assert.Contains(plan.Notes, n => n.Contains("FIRST ANGLE"));
    }

    [Fact]
    public void ProjectionOverrideBeatsTheStandardsDefault()
    {
        var options = Samples.IsoOptions();
        options.ProjectionOverride = ProjectionAngle.Third;
        var plan = DrawingPlanner.Plan(Samples.Bracket(), options);

        Assert.Equal(ProjectionAngle.Third, plan.Projection);
        Assert.All(plan.Sheets, s => Assert.Equal(ProjectionAngle.Third, s.Projection));
        Assert.Contains(plan.Notes, n => n.Contains("THIRD ANGLE"));
    }

    [Fact]
    public void EveryPlanCarriesNotesAndTitleBlockValues()
    {
        var plan = DrawingPlanner.Plan(Samples.Bracket(), Samples.AsmeOptions());

        Assert.NotEmpty(plan.Notes);
        Assert.Equal("BRK-1001", plan.TitleBlockProperties["PartNo"]);
        Assert.Equal("B", plan.TitleBlockProperties["Revision"]);
        Assert.Equal("6061-T6 Aluminum", plan.TitleBlockProperties["Material"]);
        Assert.Equal("THIRD ANGLE", plan.TitleBlockProperties["ProjectionAngle"]);
        Assert.False(string.IsNullOrEmpty(plan.TitleBlockProperties["Scale"]));
    }

    [Fact]
    public void WeightIsReportedInThePoundsOrKilogramsTheStandardImplies()
    {
        var imperial = DrawingPlanner.Plan(Samples.Bracket(), Samples.AsmeOptions());
        Assert.Contains("LB", imperial.TitleBlockProperties["Weight"]);

        var metric = DrawingPlanner.Plan(Samples.Bracket(), Samples.IsoOptions());
        Assert.Contains("KG", metric.TitleBlockProperties["Weight"]);
    }

    [Fact]
    public void HousingWithInternalFeaturesGetsASectionView()
    {
        var plan = DrawingPlanner.Plan(Samples.Housing(), Samples.IsoOptions());
        var section = plan.AllViews.FirstOrDefault(v => v.Kind == ViewKind.Section);

        Assert.NotNull(section);
        Assert.Equal("A", section!.SectionLabel);
        Assert.Equal("SECTION A-A", section.Label);
    }

    [Fact]
    public void SmallFeaturesEarnAnEnlargedDetailView()
    {
        var plan = DrawingPlanner.Plan(Samples.Housing(), Samples.IsoOptions());
        var detail = plan.AllViews.FirstOrDefault(v => v.Kind == ViewKind.Detail);

        Assert.NotNull(detail);
        Assert.False(detail!.UseSheetScale);
        Assert.NotNull(detail.Scale);
        Assert.Contains("DETAIL", detail.Label);
    }

    [Fact]
    public void SheetMetalPartGetsItsOwnFlatPatternSheetWithABendTable()
    {
        var plan = DrawingPlanner.Plan(Samples.Panel(), Samples.IsoOptions());

        Assert.True(plan.Sheets.Count >= 2);
        var flat = plan.AllViews.Single(v => v.Kind == ViewKind.FlatPattern);
        Assert.True(flat.ShowBendNotes);

        // Flat pattern dimensions come from the flat, not the formed model.
        Assert.False(flat.InsertModelDimensions);

        Assert.Contains(plan.Sheets, s => s.Tables.Any(t => t.Kind == TableKind.SheetMetalBendTable));
        Assert.Contains(plan.Notes, n => n.Contains("THICKNESS"));
    }

    [Fact]
    public void FlatPatternCanShareTheFirstSheetWithoutLandingOnTheOtherViews()
    {
        var options = Samples.IsoOptions();
        options.FlatPatternOnSeparateSheet = false;
        var plan = DrawingPlanner.Plan(Samples.Panel(), options);

        var flat = plan.AllViews.Single(v => v.Kind == ViewKind.FlatPattern);
        var sheet = plan.Sheets.Single(s => s.Views.Contains(flat));

        foreach (var other in sheet.Views.Where(v => v != flat))
        {
            Assert.False(flat.Envelope.Intersects(other.Envelope),
                $"the flat pattern overlaps the {other.Kind} view");
        }
        Assert.True(new SheetLayout(sheet.Size).ViewArea.Contains(flat.Envelope, 1.0));
    }

    [Fact]
    public void FlatPatternFallsBackToItsOwnSheetWhenThereIsNoRoomBeside()
    {
        // Pinning the scale takes away the planner's ability to shrink the flat
        // pattern until it fits beside the formed views, which is the case where
        // sharing a sheet stops being possible.
        var options = Samples.IsoOptions();
        options.FlatPatternOnSeparateSheet = false;
        options.SheetMode = SheetSelectionMode.Fixed;
        options.SheetSizeName = "A4";
        options.ScaleMode = ScaleMode.Fixed;
        options.FixedScaleNumerator = 1;
        options.FixedScaleDenominator = 1;

        var plan = DrawingPlanner.Plan(Samples.Panel(), options);
        var flat = plan.AllViews.Single(v => v.Kind == ViewKind.FlatPattern);

        Assert.True(plan.Sheets.Count >= 2);
        Assert.DoesNotContain(flat, plan.Sheets[0].Views);
        Assert.Contains(plan.Diagnostics, d => d.Contains("its own sheet"));
    }

    [Fact]
    public void RotationalPartDropsTheTopViewThatWouldOnlyRepeatDiameters()
    {
        var plan = DrawingPlanner.Plan(Samples.Shaft(), Samples.IsoOptions());
        var projected = plan.Sheets[0].Views.Where(v => v.Kind == ViewKind.Projected).ToList();

        Assert.Single(projected);
        Assert.Equal(ProjectionDirection.Right, projected[0].Direction);
        Assert.Contains(plan.Diagnostics, d => d.Contains("Rotational part"));
    }

    [Fact]
    public void AssemblyGetsABomAndNoModelDimensions()
    {
        var plan = DrawingPlanner.Plan(Samples.Assembly(), Samples.IsoOptions());

        Assert.Contains(plan.Sheets[0].Tables, t => t.Kind == TableKind.BillOfMaterials);
        Assert.All(plan.AllViews, v => Assert.False(v.InsertModelDimensions));

        var iso = plan.AllViews.Single(v => v.Kind == ViewKind.Isometric);
        Assert.True(iso.Exploded);
        Assert.Contains("EXPLODED", iso.Label);
    }

    [Fact]
    public void AssemblyWithoutASavedExplosionSaysSoInsteadOfPretending()
    {
        var assembly = Samples.Assembly();
        assembly.HasExplodedView = false;
        var plan = DrawingPlanner.Plan(assembly, Samples.IsoOptions());

        Assert.False(plan.AllViews.Single(v => v.Kind == ViewKind.Isometric).Exploded);
        Assert.Contains(plan.Diagnostics, d => d.Contains("No exploded view"));
    }

    [Fact]
    public void AutoSheetSelectionGrowsWithThePart()
    {
        var small = new PartSummary { ModelName = "SMALL", Box = BoundingBox.FromExtents(40, 30, 10) };
        var large = new PartSummary { ModelName = "LARGE", Box = BoundingBox.FromExtents(1800, 900, 400) };

        var smallSheet = DrawingPlanner.Plan(small, Samples.IsoOptions()).Sheets[0].Size;
        var largeSheet = DrawingPlanner.Plan(large, Samples.IsoOptions()).Sheets[0].Size;

        Assert.True(largeSheet.AreaMm2 > smallSheet.AreaMm2,
            $"{large.ModelName} landed on {largeSheet.Name}, {small.ModelName} on {smallSheet.Name}");
    }

    [Fact]
    public void AutoSelectionStaysInTheSeriesTheStandardUses()
    {
        var asme = DrawingPlanner.Plan(Samples.Bracket(), Samples.AsmeOptions()).Sheets[0].Size;
        Assert.Equal(DrawingStandardKind.Asme, asme.Series);

        var iso = DrawingPlanner.Plan(Samples.Bracket(), Samples.IsoOptions()).Sheets[0].Size;
        Assert.Equal(DrawingStandardKind.Iso, iso.Series);
    }

    [Fact]
    public void ChosenScaleIsAlwaysAPreferredRatio()
    {
        var options = Samples.IsoOptions();
        var ladder = options.Profile().ScaleLadder;

        foreach (var part in new[] { Samples.Bracket(), Samples.Shaft(), Samples.Panel(), Samples.Housing() })
        {
            var plan = DrawingPlanner.Plan(part, options);
            foreach (var sheet in plan.Sheets)
            {
                Assert.True(ScaleSelector.IsStandard(ladder, sheet.Scale),
                    $"{part.ModelName} {sheet.Name} got {sheet.Scale}");
            }
        }
    }

    [Fact]
    public void ScaleNeverExceedsTheEnlargementCap()
    {
        var options = Samples.IsoOptions();
        options.MaxEnlargement = 5.0;
        var tiny = new PartSummary { ModelName = "PIN", Box = BoundingBox.FromExtents(2, 2, 8) };

        var plan = DrawingPlanner.Plan(tiny, options);
        Assert.True(plan.Sheets[0].Scale.Value <= 5.0 + 1e-9, $"got {plan.Sheets[0].Scale}");
    }

    [Fact]
    public void FixedSheetAndScaleAreHonoured()
    {
        var options = Samples.AsmeOptions();
        options.SheetMode = SheetSelectionMode.Fixed;
        options.SheetSizeName = "D";
        options.ScaleMode = ScaleMode.Fixed;
        options.FixedScaleNumerator = 1;
        options.FixedScaleDenominator = 2;

        var plan = DrawingPlanner.Plan(Samples.Bracket(), options);
        Assert.Equal("D", plan.Sheets[0].Size.Name);
        Assert.Equal(0.5, plan.Sheets[0].Scale.Value, 6);
    }

    [Fact]
    public void CustomSheetSizeIsUsedVerbatim()
    {
        var options = Samples.IsoOptions();
        options.SheetMode = SheetSelectionMode.Custom;
        options.CustomSheetWidthMm = 700;
        options.CustomSheetHeightMm = 400;

        var plan = DrawingPlanner.Plan(Samples.Bracket(), options);
        Assert.Equal(700, plan.Sheets[0].Size.WidthMm, 6);
        Assert.Equal(400, plan.Sheets[0].Size.HeightMm, 6);
    }

    [Fact]
    public void OutputPathFollowsTheModelWhenNoFolderIsSet()
    {
        var plan = DrawingPlanner.Plan(Samples.Bracket(), Samples.AsmeOptions());
        Assert.EndsWith(".SLDDRW", plan.OutputFilePath);
        Assert.Contains("BRK-1001", plan.OutputFilePath);
    }

    [Fact]
    public void NoPlannedDrawingHasOverlappingViews()
    {
        foreach (var part in new[] { Samples.Bracket(), Samples.Shaft(), Samples.Panel(), Samples.Housing(), Samples.Assembly() })
        {
            foreach (var options in new[] { Samples.AsmeOptions(), Samples.IsoOptions() })
            {
                var plan = DrawingPlanner.Plan(part, options);
                foreach (var sheet in plan.Sheets)
                {
                    var layout = new SheetLayout(sheet.Size);
                    foreach (var view in sheet.Views)
                    {
                        Assert.False(view.Envelope.Intersects(layout.TitleBlock),
                            $"{part.ModelName}/{sheet.Name}: {view.Kind} lands on the title block");
                    }

                    for (int i = 0; i < sheet.Views.Count; i++)
                    {
                        for (int j = i + 1; j < sheet.Views.Count; j++)
                        {
                            Assert.False(sheet.Views[i].Envelope.Intersects(sheet.Views[j].Envelope),
                                $"{part.ModelName}/{sheet.Name}: {sheet.Views[i].Kind} overlaps {sheet.Views[j].Kind}");
                        }
                    }
                }
            }
        }
    }
}
