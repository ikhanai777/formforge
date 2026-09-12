using DrawingForge.Core.Geometry;
using DrawingForge.Core.Options;
using DrawingForge.Core.Standards;
using Xunit;

namespace DrawingForge.Core.Tests;

public class SheetCatalogTests
{
    [Fact]
    public void AsmeSeriesIsAscendingAndLandscape()
    {
        var series = SheetCatalog.Series(DrawingStandardKind.Asme);
        Assert.NotEmpty(series);
        for (int i = 1; i < series.Count; i++)
        {
            Assert.True(series[i].AreaMm2 > series[i - 1].AreaMm2,
                $"{series[i].Name} should be larger than {series[i - 1].Name}");
        }
        Assert.All(series, s => Assert.True(s.IsLandscape));
    }

    [Fact]
    public void AsmeBSheetIs17By11Inches()
    {
        var b = SheetCatalog.ByName("B");
        Assert.NotNull(b);
        Assert.Equal(17.0, Units.MmToInch(b!.WidthMm), 3);
        Assert.Equal(11.0, Units.MmToInch(b.HeightMm), 3);
    }

    [Fact]
    public void IsoA3Is420By297Millimetres()
    {
        var a3 = SheetCatalog.ByName("a3");
        Assert.NotNull(a3);
        Assert.Equal(420.0, a3!.WidthMm, 3);
        Assert.Equal(297.0, a3.HeightMm, 3);
    }

    [Fact]
    public void EveryNamedSheetHasADrawableAreaInsideItself()
    {
        foreach (var sheet in SheetCatalog.All)
        {
            var area = sheet.DrawableArea;
            Assert.True(area.Width > 0 && area.Height > 0, sheet.Name);
            Assert.True(area.Right <= sheet.WidthMm + 1e-9, sheet.Name);
            Assert.True(area.Top <= sheet.HeightMm + 1e-9, sheet.Name);
        }
    }

    [Fact]
    public void CustomSheetGetsMarginsAndATitleBlockThatFit()
    {
        var sheet = SheetCatalog.Custom("Strip", 600, 200, DrawingStandardKind.Iso);
        Assert.Equal(SheetCatalog.SwPaperUserDefined, sheet.SwPaperSize);
        Assert.True(sheet.MarginMm >= 6.0 && sheet.MarginMm <= 25.0);
        Assert.True(sheet.TitleBlockWidthMm <= sheet.WidthMm - 2 * sheet.MarginMm);
        Assert.True(sheet.TitleBlockHeightMm <= sheet.HeightMm - 2 * sheet.MarginMm);
    }

    [Fact]
    public void UnknownSheetNameReturnsNullRatherThanGuessing()
    {
        Assert.Null(SheetCatalog.ByName("A7"));
        Assert.Null(SheetCatalog.ByName(""));
    }
}

public class StandardProfileTests
{
    [Fact]
    public void AsmeDefaultsToThirdAngleAndIsoToFirst()
    {
        Assert.Equal(ProjectionAngle.Third,
            StandardProfile.For(DrawingStandardKind.Asme, UnitSystem.Inch).DefaultProjection);
        Assert.Equal(ProjectionAngle.First,
            StandardProfile.For(DrawingStandardKind.Iso, UnitSystem.Millimeter).DefaultProjection);
    }

    [Fact]
    public void EveryStandardHasANonEmptyLadderContainingFullSize()
    {
        foreach (DrawingStandardKind kind in System.Enum.GetValues(typeof(DrawingStandardKind)))
        {
            var profile = StandardProfile.For(kind, UnitSystem.Millimeter);
            Assert.NotEmpty(profile.ScaleLadder);
            Assert.Contains(profile.ScaleLadder, r => r.IsFullSize);
            Assert.False(string.IsNullOrWhiteSpace(profile.DimensioningSpec));
            Assert.False(string.IsNullOrWhiteSpace(profile.GeneralToleranceNote));
        }
    }

    [Fact]
    public void LadderIsOrderedLargestScaleFirst()
    {
        var ladder = StandardProfile.For(DrawingStandardKind.Iso, UnitSystem.Millimeter).ScaleLadder;
        for (int i = 1; i < ladder.Count; i++)
        {
            Assert.True(ladder[i].Value < ladder[i - 1].Value);
        }
    }

    [Fact]
    public void InchUnitsChangeTheToleranceBlockEvenUnderIso()
    {
        var metric = StandardProfile.For(DrawingStandardKind.Iso, UnitSystem.Millimeter);
        var imperial = StandardProfile.For(DrawingStandardKind.Iso, UnitSystem.Inch);
        Assert.NotEqual(metric.GeneralToleranceNote, imperial.GeneralToleranceNote);
        Assert.Contains("2768", metric.GeneralToleranceNote);
    }
}

public class ScaleSelectorTests
{
    private static readonly RectMm A3Area = new RectMm(0, 0, 380, 257);

    private static System.Collections.Generic.IReadOnlyList<Ratio> IsoLadder =>
        StandardProfile.For(DrawingStandardKind.Iso, UnitSystem.Millimeter).ScaleLadder;

    [Fact]
    public void SmallPartGetsAnEnlargementThatStillFits()
    {
        var scale = ScaleSelector.Choose(IsoLadder, 10, 8, A3Area);
        Assert.True(scale.Value > 1.0);
        Assert.True(ScaleSelector.Fits(scale, 10, 8, A3Area));
    }

    [Fact]
    public void LargePartGetsAReductionThatStillFits()
    {
        var scale = ScaleSelector.Choose(IsoLadder, 2000, 1400, A3Area);
        Assert.True(scale.Value < 1.0);
        Assert.True(ScaleSelector.Fits(scale, 2000, 1400, A3Area));
    }

    [Fact]
    public void ChosenScaleIsAlwaysOnTheLadder()
    {
        double[] sizes = { 3, 25, 120, 400, 900, 3000, 12000 };
        foreach (double size in sizes)
        {
            var scale = ScaleSelector.Choose(IsoLadder, size, size * 0.7, A3Area);
            Assert.True(ScaleSelector.IsStandard(IsoLadder, scale), $"{size} mm produced {scale}");
        }
    }

    [Fact]
    public void FixedOverheadIsSubtractedBeforeFitting()
    {
        // A block that just fits with no gaps must not fit once 200 mm of view
        // gap is demanded in the same direction.
        var withoutGap = ScaleSelector.Choose(IsoLadder, 200, 100, A3Area);
        var withGap = ScaleSelector.Choose(IsoLadder, 200, 100, A3Area, ScaleSelector.DefaultAnnotationAllowance, 200, 0);
        Assert.True(withGap.Value <= withoutGap.Value);
    }

    [Fact]
    public void SnapTreatsEnlargementAndReductionSymmetrically()
    {
        Assert.Equal(2.0, ScaleSelector.Snap(IsoLadder, 2.0).Value, 6);
        Assert.Equal(0.5, ScaleSelector.Snap(IsoLadder, 0.5).Value, 6);
        Assert.Equal(1.0, ScaleSelector.Snap(IsoLadder, 1.05).Value, 6);
    }

    [Fact]
    public void ImpossibleBlockFallsBackToTheSmallestLadderEntryAndReportsNoFit()
    {
        var scale = ScaleSelector.Choose(IsoLadder, 1e7, 1e7, A3Area);
        Assert.Equal(IsoLadder[IsoLadder.Count - 1], scale);
        Assert.False(ScaleSelector.Fits(scale, 1e7, 1e7, A3Area));
    }
}
