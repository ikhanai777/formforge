using DrawingForge.Core.Geometry;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;

namespace DrawingForge.Core.Tests;

/// <summary>Model summaries standing in for real SOLIDWORKS documents.</summary>
internal static class Samples
{
    /// <summary>A machined bracket: prismatic, a few holes, nothing hidden.</summary>
    public static PartSummary Bracket()
    {
        var part = new PartSummary
        {
            FilePath = @"C:\proj\BRK-1001.SLDPRT",
            ModelName = "BRK-1001",
            Configuration = "Default",
            Box = BoundingBox.FromExtents(120, 80, 20),
            Material = "6061-T6 Aluminum",
            MassGrams = 410,
            DimensionsMarkedForDrawing = 14,
            SmallestFeatureMm = 6.0
        };
        part.CustomProperties["PartNo"] = "BRK-1001";
        part.CustomProperties["Description"] = "MOUNTING BRACKET";
        part.CustomProperties["Revision"] = "B";
        part.Holes.Add(new HoleSummary { DiameterMm = 6.6, IsThrough = true, IsWizardHole = true, Axis = Axis.Z, InstanceCount = 4 });
        return part;
    }

    /// <summary>A turned shaft: axis along X, so the axis is already horizontal in the front view.</summary>
    public static PartSummary Shaft()
    {
        var part = new PartSummary
        {
            FilePath = @"C:\proj\SHF-2002.SLDPRT",
            ModelName = "SHF-2002",
            Configuration = "Default",
            Box = BoundingBox.FromExtents(240, 30, 30),
            Material = "1045 Steel",
            IsRotational = true,
            RotationAxis = Axis.X,
            DimensionsMarkedForDrawing = 9,
            SmallestFeatureMm = 2.0
        };
        part.CustomProperties["PartNo"] = "SHF-2002";
        part.CustomProperties["Description"] = "DRIVE SHAFT";
        return part;
    }

    /// <summary>A sheet metal chassis panel with bends.</summary>
    public static PartSummary Panel()
    {
        var part = new PartSummary
        {
            FilePath = @"C:\proj\PNL-3003.SLDPRT",
            ModelName = "PNL-3003",
            Configuration = "Default",
            Box = BoundingBox.FromExtents(300, 200, 40),
            FlatPatternBox = BoundingBox.FromExtents(360, 250, 2),
            Material = "304 Stainless",
            IsSheetMetal = true,
            SheetMetalThicknessMm = 2.0,
            BendCount = 4,
            DimensionsMarkedForDrawing = 11,
            SmallestFeatureMm = 5.0
        };
        part.CustomProperties["PartNo"] = "PNL-3003";
        part.CustomProperties["Description"] = "SIDE PANEL";
        return part;
    }

    /// <summary>A housing with a bore that no outside view shows.</summary>
    public static PartSummary Housing()
    {
        var part = new PartSummary
        {
            FilePath = @"C:\proj\HSG-4004.SLDPRT",
            ModelName = "HSG-4004",
            Configuration = "Default",
            Box = BoundingBox.FromExtents(90, 90, 60),
            Material = "Cast Iron",
            HasInternalFeatures = true,
            DimensionsMarkedForDrawing = 22,
            SmallestFeatureMm = 1.5
        };
        part.CustomProperties["PartNo"] = "HSG-4004";
        part.CustomProperties["Description"] = "BEARING HOUSING";
        part.Holes.Add(new HoleSummary { DiameterMm = 5, IsTapped = true, ThreadDesignation = "M6x1.0", IsWizardHole = true, Axis = Axis.Z, InstanceCount = 6 });
        return part;
    }

    /// <summary>A top-level assembly.</summary>
    public static PartSummary Assembly()
    {
        var part = new PartSummary
        {
            FilePath = @"C:\proj\ASM-9000.SLDASM",
            ModelName = "ASM-9000",
            Configuration = "Default",
            IsAssembly = true,
            Box = BoundingBox.FromExtents(400, 300, 250),
            HasExplodedView = true
        };
        part.CustomProperties["PartNo"] = "ASM-9000";
        part.CustomProperties["Description"] = "GEARBOX ASSEMBLY";
        part.CustomProperties["Material"] = "N/A";
        part.BomItems.Add(new BomItemSummary { ItemNumber = "1", PartNumber = "BRK-1001", Quantity = 2 });
        part.BomItems.Add(new BomItemSummary { ItemNumber = "2", PartNumber = "SHF-2002", Quantity = 1 });
        return part;
    }

    public static DrawingOptions AsmeOptions()
    {
        return new DrawingOptions
        {
            Standard = DrawingStandardKind.Asme,
            Units = UnitSystem.Inch,
            SheetMode = SheetSelectionMode.AutoSmallestFit,
            CompanyName = "FormForge",
            DrawnBy = "tests"
        };
    }

    public static DrawingOptions IsoOptions()
    {
        return new DrawingOptions
        {
            Standard = DrawingStandardKind.Iso,
            Units = UnitSystem.Millimeter,
            SheetMode = SheetSelectionMode.AutoSmallestFit,
            CompanyName = "FormForge",
            DrawnBy = "tests"
        };
    }
}
