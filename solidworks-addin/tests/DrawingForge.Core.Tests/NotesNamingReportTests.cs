using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using DrawingForge.Core.Naming;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;
using DrawingForge.Core.Reporting;
using DrawingForge.Core.Standards;
using Xunit;

namespace DrawingForge.Core.Tests;

public class NoteBlockTests
{
    [Fact]
    public void NotesAreNumberedFromOneWithNoGaps()
    {
        var notes = NoteBlockComposer.Compose(Samples.Bracket(), Samples.AsmeOptions());
        for (int i = 0; i < notes.Count; i++)
        {
            Assert.StartsWith($"{i + 1}. ", notes[i]);
        }
    }

    [Fact]
    public void BlankNotesAreDroppedBeforeNumbering()
    {
        var numbered = NoteBlockComposer.Number(new[] { "FIRST", "", "   ", "SECOND" });
        Assert.Equal(new[] { "1. FIRST", "2. SECOND" }, numbered);
    }

    [Fact]
    public void AsmeNotesStateTheSpecUnitAndProjection()
    {
        var text = NoteBlockComposer.ComposeText(Samples.Bracket(), Samples.AsmeOptions());
        Assert.Contains("ASME Y14.5-2018", text);
        Assert.Contains("INCHES", text);
        Assert.Contains("THIRD ANGLE", text);
        Assert.Contains("NOTES:", text);
    }

    [Fact]
    public void IsoNotesStateMillimetresAndIso2768()
    {
        var text = NoteBlockComposer.ComposeText(Samples.Bracket(), Samples.IsoOptions());
        Assert.Contains("MILLIMETRES", text);
        Assert.Contains("2768", text);
        Assert.Contains("FIRST ANGLE", text);
    }

    [Fact]
    public void MaterialFallsBackToTheTitleBlockRatherThanGoingBlank()
    {
        var part = Samples.Bracket();
        part.Material = "";
        var notes = NoteBlockComposer.Compose(part, Samples.AsmeOptions());
        Assert.Contains(notes, n => n.Contains("MATERIAL: SEE TITLE BLOCK"));
    }

    [Fact]
    public void SheetMetalAddsThicknessAndBendNotes()
    {
        var notes = NoteBlockComposer.Compose(Samples.Panel(), Samples.IsoOptions());
        Assert.Contains(notes, n => n.Contains("THICKNESS 2"));
        Assert.Contains(notes, n => n.Contains("4 BEND"));
        Assert.Contains(notes, n => n.Contains("FLAT PATTERN"));
    }

    [Fact]
    public void TappedHolesAddAThreadNote()
    {
        var notes = NoteBlockComposer.Compose(Samples.Housing(), Samples.IsoOptions());
        Assert.Contains(notes, n => n.Contains("TAPPED HOLES"));
    }

    [Fact]
    public void UserNotesAreAppendedWithTokensExpanded()
    {
        var options = Samples.AsmeOptions();
        options.ExtraNotes.Add("SUPPLIED BY {Company} FOR {PartNumber}.");

        var notes = NoteBlockComposer.Compose(Samples.Bracket(), options);
        Assert.Contains(notes, n => n.Contains("SUPPLIED BY FormForge FOR BRK-1001."));
    }

    [Fact]
    public void UnknownTokensAreLeftVisibleRatherThanSilentlyDropped()
    {
        var expanded = NoteBlockComposer.Expand("SEE {Nonsense}", Samples.Bracket(), Samples.AsmeOptions());
        Assert.Equal("SEE {Nonsense}", expanded);
    }
}

public class FileNameBuilderTests
{
    [Fact]
    public void PatternTokensExpand()
    {
        var name = FileNameBuilder.Build("{PartNumber}_{Rev}", Samples.Bracket(), Samples.AsmeOptions());
        Assert.Equal("BRK-1001_B", name);
    }

    [Fact]
    public void RevSuffixVanishesWhenThereIsNoRevision()
    {
        var part = Samples.Shaft();
        var options = Samples.AsmeOptions();
        options.DefaultRevision = "";

        Assert.Equal("SHF-2002", FileNameBuilder.Build("{PartNumber}{RevSuffix}", part, options));
    }

    [Fact]
    public void IllegalCharactersAreReplacedNotDropped()
    {
        Assert.Equal("A_B_C", FileNameBuilder.Sanitize("A/B:C"));
        Assert.DoesNotContain("\\", FileNameBuilder.Sanitize(@"x\y"));
    }

    [Fact]
    public void EmptyOrDottedNamesNeverEscape()
    {
        Assert.Equal("DRAWING", FileNameBuilder.Sanitize(""));
        Assert.Equal("DRAWING", FileNameBuilder.Sanitize("..."));
    }

    [Fact]
    public void ReservedDeviceNamesArePrefixed()
    {
        Assert.Equal("_CON", FileNameBuilder.Sanitize("CON"));
        Assert.Equal("_lpt1", FileNameBuilder.Sanitize("lpt1"));
    }

    [Fact]
    public void LongNamesAreTruncated()
    {
        var name = FileNameBuilder.Sanitize(new string('x', 400));
        Assert.True(name.Length <= FileNameBuilder.MaxBaseNameLength);
    }

    [Fact]
    public void UniquenessIsCaseInsensitiveLikeTheFileSystem()
    {
        var taken = new List<string>();
        Assert.Equal("PART", FileNameBuilder.MakeUnique("PART", taken));
        Assert.Equal("part_2", FileNameBuilder.MakeUnique("part", taken));
        Assert.Equal("PART_3", FileNameBuilder.MakeUnique("PART", taken));
    }
}

public class OptionsStoreTests
{
    [Fact]
    public void OptionsRoundTripThroughXml()
    {
        var options = Samples.AsmeOptions();
        options.ProjectionOverride = ProjectionAngle.First;
        options.ExtraNotes.Add("CUSTOM NOTE");
        options.SheetSizeName = "D";
        options.MaxEnlargement = 4;

        var copy = OptionsStore.Clone(options);

        Assert.Equal(options.Standard, copy.Standard);
        Assert.Equal(ProjectionAngle.First, copy.ProjectionOverride);
        Assert.Equal("D", copy.SheetSizeName);
        Assert.Equal(4, copy.MaxEnlargement);
        Assert.Contains("CUSTOM NOTE", copy.ExtraNotes);

        // A pre-populated collection must come back the same length, not
        // doubled: XmlSerializer appends into list properties it finds already
        // filled, which silently grows the title block map on every load.
        Assert.Equal(options.TitleBlockPropertyMap.Length, copy.TitleBlockPropertyMap.Length);
        Assert.Equal(OptionsStore.Clone(copy).TitleBlockPropertyMap.Length, copy.TitleBlockPropertyMap.Length);
        Assert.Single(copy.ExtraNotes);
    }

    [Fact]
    public void SavingAndLoadingPreservesTheFile()
    {
        string path = Path.Combine(Path.GetTempPath(), "df-" + Guid.NewGuid().ToString("N") + ".xml");
        try
        {
            var options = Samples.IsoOptions();
            options.CompanyName = "Acme Fabrication";
            OptionsStore.Save(options, path);

            var loaded = OptionsStore.Load(path, out string? warning);
            Assert.Null(warning);
            Assert.Equal("Acme Fabrication", loaded.CompanyName);
            Assert.Equal(DrawingStandardKind.Iso, loaded.Standard);
        }
        finally
        {
            if (File.Exists(path)) File.Delete(path);
        }
    }

    [Fact]
    public void ACorruptFileFallsBackToDefaultsAndSaysWhy()
    {
        string path = Path.Combine(Path.GetTempPath(), "df-" + Guid.NewGuid().ToString("N") + ".xml");
        File.WriteAllText(path, "this is not xml");
        try
        {
            var loaded = OptionsStore.Load(path, out string? warning);
            Assert.NotNull(warning);
            Assert.Equal(new DrawingOptions().Standard, loaded.Standard);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void MissingFileIsNotAnError()
    {
        var loaded = OptionsStore.Load(Path.Combine(Path.GetTempPath(), "definitely-not-here.xml"), out string? warning);
        Assert.Null(warning);
        Assert.NotNull(loaded);
    }
}

public class OptionsValidationTests
{
    [Fact]
    public void DefaultOptionsAreValid()
    {
        Assert.Empty(new DrawingOptions().Validate());
    }

    [Fact]
    public void AnUnknownSheetNameIsReported()
    {
        var options = new DrawingOptions { SheetMode = SheetSelectionMode.Fixed, SheetSizeName = "Q" };
        Assert.Contains(options.Validate(), p => p.Contains("Sheet size"));
    }

    [Fact]
    public void AnOffLadderFixedScaleIsReported()
    {
        var options = new DrawingOptions
        {
            ScaleMode = ScaleMode.Fixed,
            FixedScaleNumerator = 1,
            FixedScaleDenominator = 3
        };
        Assert.Contains(options.Validate(), p => p.Contains("preferred scale"));
    }

    [Fact]
    public void NoExportFormatIsReported()
    {
        var options = new DrawingOptions { ExportFormats = ExportFormats.None };
        Assert.Contains(options.Validate(), p => p.Contains("No export format"));
    }
}

public class JsonWriterTests
{
    [Fact]
    public void ReportJsonParses()
    {
        var report = BuildReport();
        string json = report.ToJson();

        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;

        Assert.Equal("ASME Y14.5-2018", root.GetProperty("standard").GetString());
        Assert.Equal(1, root.GetProperty("succeeded").GetInt32());

        var drawing = root.GetProperty("drawings")[0];
        Assert.Equal("BRK-1001", drawing.GetProperty("partNumber").GetString());
        Assert.Equal(2, drawing.GetProperty("views").GetArrayLength());
        Assert.Equal("DF010", drawing.GetProperty("findings")[0].GetProperty("code").GetString());
    }

    [Fact]
    public void QuotingEscapesControlCharacters()
    {
        Assert.Equal("\"a\\nb\"", JsonWriter.Quote("a\nb"));
        Assert.Equal("\"say \\\"hi\\\"\"", JsonWriter.Quote("say \"hi\""));
        Assert.Equal("\"\\u0001\"", JsonWriter.Quote("\u0001"));
    }

    [Fact]
    public void EmptyCollectionsStillParse()
    {
        var report = new BatchReport { AssemblyPath = "x.SLDASM" };
        using var document = JsonDocument.Parse(report.ToJson());
        Assert.Equal(0, document.RootElement.GetProperty("drawings").GetArrayLength());
    }

    [Fact]
    public void TextReportMentionsEveryPartAndItsState()
    {
        string text = BuildReport().ToText();
        Assert.Contains("BRK-1001", text);
        Assert.Contains("created", text);
        Assert.Contains("DF010", text);
    }

    private static BatchReport BuildReport()
    {
        var report = new BatchReport
        {
            AssemblyPath = @"C:\proj\ASM-9000.SLDASM",
            StandardName = "ASME Y14.5-2018",
            ProjectionAngle = "THIRD",
            FinishedUtc = DateTime.UtcNow.AddSeconds(12)
        };

        var outcome = new DrawingOutcome
        {
            PartNumber = "BRK-1001",
            SourceModelPath = @"C:\proj\BRK-1001.SLDPRT",
            DrawingPath = @"C:\proj\BRK-1001.SLDDRW",
            Succeeded = true,
            SheetCount = 1,
            SheetSizeName = "B",
            Scale = "1:1",
            FinishedUtc = DateTime.UtcNow
        };
        outcome.ViewResults.Add(new ViewOutcome { ViewId = "v1", Kind = "Principal", Created = true, DimensionCount = 6 });
        outcome.ViewResults.Add(new ViewOutcome { ViewId = "v2", Kind = "Projected", Created = true, DimensionCount = 3 });
        outcome.ExportedFiles.Add(@"C:\proj\BRK-1001.PDF");
        report.Outcomes.Add(outcome);

        report.Findings["BRK-1001"] = new List<CheckFinding>
        {
            new CheckFinding("DF010", CheckSeverity.Error, "no dimensions", "add some")
        };

        return report;
    }
}
