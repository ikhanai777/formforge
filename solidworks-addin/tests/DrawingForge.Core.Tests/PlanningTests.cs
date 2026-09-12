using System.Linq;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;
using DrawingForge.Core.Standards;
using Xunit;

namespace DrawingForge.Core.Tests;

public class SheetLayoutTests
{
    [Fact]
    public void ViewAreaNeverTouchesTheTitleBlock()
    {
        foreach (var sheet in SheetCatalog.All)
        {
            var layout = new SheetLayout(sheet);
            Assert.False(layout.ViewArea.Intersects(layout.TitleBlock),
                $"{sheet.Name}: the view area overlaps the title block");
        }
    }

    [Fact]
    public void NotesSitLeftOfTheTitleBlockInTheSameBand()
    {
        var layout = new SheetLayout(SheetCatalog.ByName("A3")!);
        Assert.True(layout.NotesArea.Right <= layout.TitleBlock.Left + 1e-9);
        Assert.Equal(layout.BottomBand.Bottom, layout.NotesArea.Bottom, 6);
        Assert.False(layout.NotesAreaIsCramped);
    }

    [Fact]
    public void EveryRegionStaysInsideTheBorder()
    {
        foreach (var sheet in SheetCatalog.All)
        {
            var layout = new SheetLayout(sheet);
            Assert.True(layout.Drawable.Contains(layout.ViewArea, 1e-6), sheet.Name);
            Assert.True(layout.Drawable.Contains(layout.BottomBand, 1e-6), sheet.Name);
            Assert.True(layout.Drawable.Contains(layout.TitleBlock, 1e-6), sheet.Name);
        }
    }
}

public class FrontViewChooserTests
{
    [Fact]
    public void PlateIsViewedOnItsLargeFace()
    {
        // 200 x 150 x 5: the front view must show 200 x 150, not an edge.
        var part = new PartSummary { Box = BoundingBox.FromExtents(200, 150, 5) };
        var choice = FrontViewChooser.Choose(part);
        Assert.Equal(200.0, choice.WidthMm, 6);
        Assert.Equal(150.0, choice.HeightMm, 6);
        Assert.Equal(5.0, choice.DepthMm, 6);
    }

    [Fact]
    public void ShaftAlongXKeepsTheModelFrontWithTheAxisHorizontal()
    {
        var part = Samples.Shaft();
        var choice = FrontViewChooser.Choose(part);
        Assert.Equal(ViewOrientation.Front, choice.Orientation);
        Assert.Equal(0.0, choice.RotationDegrees, 6);
        Assert.Equal(240.0, choice.WidthMm, 6);   // the axis reads across the sheet
        Assert.Contains("horizontal", choice.Rationale);
    }

    [Fact]
    public void ShaftAlongYIsRotatedSoTheAxisStillReadsHorizontal()
    {
        var part = new PartSummary
        {
            Box = BoundingBox.FromExtents(30, 240, 30),
            IsRotational = true,
            RotationAxis = Axis.Y
        };
        var choice = FrontViewChooser.Choose(part);

        // No named view puts Y across the sheet, so the view is turned 90°.
        Assert.Equal(90.0, choice.RotationDegrees, 6);
        Assert.Equal(240.0, choice.WidthMm, 6);
        Assert.Equal(30.0, choice.HeightMm, 6);
    }

    [Fact]
    public void ShaftAlongZUsesTheRightViewWhichPutsZAcrossTheSheet()
    {
        var part = new PartSummary
        {
            Box = BoundingBox.FromExtents(30, 30, 240),
            IsRotational = true,
            RotationAxis = Axis.Z
        };
        var choice = FrontViewChooser.Choose(part);
        Assert.Equal(240.0, choice.WidthMm, 6);
        Assert.Equal(0.0, choice.RotationDegrees, 6);
        Assert.True(choice.Orientation is ViewOrientation.Right or ViewOrientation.Left);
    }

    [Fact]
    public void CubeKeepsTheModelsOwnFront()
    {
        var part = new PartSummary { Box = BoundingBox.FromExtents(50, 50, 50) };
        var choice = FrontViewChooser.Choose(part, respectModelOrientation: true);
        Assert.Equal(ViewOrientation.Front, choice.Orientation);
        Assert.Equal(0.0, choice.RotationDegrees, 6);
    }

    [Fact]
    public void RotationalDetectionIgnoresACube()
    {
        Assert.False(FrontViewChooser.LooksRotational(BoundingBox.FromExtents(50, 50, 50), out _));
        Assert.True(FrontViewChooser.LooksRotational(BoundingBox.FromExtents(200, 40, 40), out var axis));
        Assert.Equal(Axis.X, axis);
    }
}

public class ViewLayoutPlannerTests
{
    private static LayoutRequest Request(ProjectionAngle angle, double w = 120, double h = 80, double d = 20)
    {
        return new LayoutRequest
        {
            Principal = new PrincipalViewChoice
            {
                Orientation = ViewOrientation.Front,
                WidthMm = w,
                HeightMm = h,
                DepthMm = d,
                Rationale = "test"
            },
            Projection = angle,
            IncludeTopView = true,
            IncludeSideView = true,
            IncludeIsometric = true,
            ViewGapMm = 25
        };
    }

    private static System.Collections.Generic.IReadOnlyList<Ratio> Ladder =>
        StandardProfile.For(DrawingStandardKind.Iso, UnitSystem.Millimeter).ScaleLadder;

    [Fact]
    public void ThirdAngleTopViewSitsAboveTheFrontViewAndSharesItsCentreline()
    {
        var sheet = SheetCatalog.ByName("A3")!;
        var result = ViewLayoutPlanner.Plan(Request(ProjectionAngle.Third), sheet, Ladder);

        var front = result.Principal!;
        var top = result.Views.Single(v => v.Kind == ViewKind.Projected && v.Direction == ProjectionDirection.Up);

        Assert.Equal(front.CenterXMm, top.CenterXMm, 6);
        Assert.True(top.CenterYMm > front.CenterYMm, "third angle puts the top view above the front view");
    }

    [Fact]
    public void FirstAngleTopViewSitsBelowTheFrontView()
    {
        var sheet = SheetCatalog.ByName("A3")!;
        var result = ViewLayoutPlanner.Plan(Request(ProjectionAngle.First), sheet, Ladder);

        var front = result.Principal!;
        var top = result.Views.Single(v => v.Kind == ViewKind.Projected && v.Direction == ProjectionDirection.Down);

        Assert.Equal(front.CenterXMm, top.CenterXMm, 6);
        Assert.True(top.CenterYMm < front.CenterYMm, "first angle puts the top view below the front view");
    }

    [Fact]
    public void SideViewStaysOnTheFrontViewsHorizontalCentrelineInBothConventions()
    {
        foreach (var angle in new[] { ProjectionAngle.First, ProjectionAngle.Third })
        {
            var sheet = SheetCatalog.ByName("A3")!;
            var result = ViewLayoutPlanner.Plan(Request(angle), sheet, Ladder);
            var front = result.Principal!;
            var side = result.Views.Single(v => v.Kind == ViewKind.Projected && v.Direction == ProjectionDirection.Right);

            Assert.Equal(front.CenterYMm, side.CenterYMm, 6);
            Assert.True(side.CenterXMm > front.CenterXMm);
        }
    }

    [Fact]
    public void NoViewsOverlapAndAllStayOnTheSheet()
    {
        foreach (var angle in new[] { ProjectionAngle.First, ProjectionAngle.Third })
        {
            foreach (var sheet in SheetCatalog.Series(DrawingStandardKind.Iso))
            {
                var result = ViewLayoutPlanner.Plan(Request(angle), sheet, Ladder);
                Assert.True(result.Fits, $"{sheet.Name} {angle}");
                Assert.Empty(ViewLayoutPlanner.Validate(result, sheet));
            }
        }
    }

    [Fact]
    public void IsometricViewCarriesNoDimensions()
    {
        var sheet = SheetCatalog.ByName("A3")!;
        var result = ViewLayoutPlanner.Plan(Request(ProjectionAngle.Third), sheet, Ladder);
        var iso = result.Views.Single(v => v.Kind == ViewKind.Isometric);
        Assert.False(iso.InsertModelDimensions);
        Assert.False(iso.InsertHoleCallouts);
    }

    [Fact]
    public void IsometricFootprintMatchesTheStandardProjection()
    {
        ViewLayoutPlanner.IsometricFootprint(100, 100, 50, out double w, out double h);
        Assert.Equal(200 * 0.8660254, w, 4);
        Assert.Equal(200 * 0.5 + 50, h, 4);
    }

    [Fact]
    public void AnExtraViewThatCostsTooMuchScaleMovesToAContinuationSheet()
    {
        var sheet = SheetCatalog.ByName("A3")!;
        var request = Request(ProjectionAngle.Third, 100, 60, 20);
        var withoutExtra = ViewLayoutPlanner.Plan(Request(ProjectionAngle.Third, 100, 60, 20), sheet, Ladder);

        request.Extras.Add(new ExtraViewRequest
        {
            Kind = ViewKind.Section,
            WidthMm = 1000,
            HeightMm = 800,
            Label = "SECTION A-A"
        });

        var result = ViewLayoutPlanner.Plan(request, sheet, Ladder);

        Assert.True(result.Fits);
        Assert.Single(result.Overflow);
        Assert.Equal(withoutExtra.Scale.Value, result.Scale.Value, 6);
        Assert.Contains(result.Diagnostics, d => d.Contains("continuation sheet"));
    }

    [Fact]
    public void AnExtraViewThatCostsOnlyOneScaleStepStaysOnTheSheet()
    {
        var sheet = SheetCatalog.ByName("A3")!;
        var request = Request(ProjectionAngle.Third, 100, 60, 20);
        request.Extras.Add(new ExtraViewRequest
        {
            Kind = ViewKind.Section,
            WidthMm = 60,
            HeightMm = 60,
            Label = "SECTION A-A"
        });

        var result = ViewLayoutPlanner.Plan(request, sheet, Ladder);

        Assert.Empty(result.Overflow);
        Assert.Contains(result.Views, v => v.Kind == ViewKind.Section);
        Assert.Empty(ViewLayoutPlanner.Validate(result, sheet));
    }

    [Fact]
    public void ADropInTheViewSetRemovesItsSpaceFromTheBlock()
    {
        var full = Request(ProjectionAngle.Third);
        var noIso = Request(ProjectionAngle.Third);
        noIso.IncludeIsometric = false;

        ViewLayoutPlanner.TotalBlockSize(full, out double fw, out double fh, out _, out _);
        ViewLayoutPlanner.TotalBlockSize(noIso, out double nw, out double nh, out _, out _);

        Assert.True(nw <= fw);
        Assert.True(nh <= fh);
    }
}
