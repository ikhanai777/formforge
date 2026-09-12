using System.Collections.Generic;
using System.Linq;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;
using DrawingForge.Core.Reporting;
using Xunit;

namespace DrawingForge.Core.Tests;

public class FabricationChecklistTests
{
    /// <summary>A plausible successful outcome for a plan, which individual tests then break.</summary>
    private static DrawingOutcome GoodOutcome(DrawingPlan plan, int dimensionsPerView = 5)
    {
        var outcome = new DrawingOutcome
        {
            PartNumber = plan.PartNumber,
            Succeeded = true,
            SheetCount = plan.Sheets.Count,
            NoteBlockInserted = true,
            ProjectionSymbolInserted = true,
            TitleBlockFieldsWritten = plan.TitleBlockProperties.Count
        };

        foreach (var view in plan.AllViews)
        {
            outcome.ViewResults.Add(new ViewOutcome
            {
                ViewId = view.Id,
                Kind = view.Kind.ToString(),
                Created = true,
                DimensionCount = view.InsertModelDimensions ? dimensionsPerView : 0,
                HoleCallouts = view.InsertHoleCallouts ? 4 : 0
            });
        }

        foreach (var table in plan.Sheets.SelectMany(s => s.Tables))
            outcome.TablesInserted.Add(table.Kind.ToString());

        return outcome;
    }

    [Fact]
    public void AWellFormedBracketDrawingIsReleasable()
    {
        var part = Samples.Bracket();
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);

        var findings = FabricationChecklist.Run(part, plan, GoodOutcome(plan), options);

        Assert.True(FabricationChecklist.IsReleasable(findings),
            string.Join("\n", findings.Select(f => f.ToString())));
    }

    [Fact]
    public void ADrawingWithNoDimensionsIsRejected()
    {
        var part = Samples.Bracket();
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);
        var outcome = GoodOutcome(plan, dimensionsPerView: 0);

        var findings = FabricationChecklist.Run(part, plan, outcome, options);

        Assert.Contains(findings, f => f.Code == "DF010" && f.Severity == CheckSeverity.Error);
        Assert.False(FabricationChecklist.IsReleasable(findings));
    }

    [Fact]
    public void TheNoDimensionsRemedyPointsAtTheModelWhenNothingIsMarked()
    {
        var part = Samples.Bracket();
        part.DimensionsMarkedForDrawing = 0;
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);

        var findings = FabricationChecklist.Run(part, plan, GoodOutcome(plan, 0), options);
        var finding = findings.Single(f => f.Code == "DF010");

        Assert.Contains("marked for drawing", finding.Remedy);
    }

    [Fact]
    public void AMissingViewIsAnError()
    {
        var part = Samples.Bracket();
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);
        var outcome = GoodOutcome(plan);
        outcome.ViewResults[0].Created = false;
        outcome.ViewResults[0].Failure = "CreateDrawViewFromModelView3 returned null";

        var findings = FabricationChecklist.Run(part, plan, outcome, options);
        Assert.Contains(findings, f => f.Code == "DF002" && f.Severity == CheckSeverity.Error);
    }

    [Fact]
    public void AnEmptyViewIsAnError()
    {
        var part = Samples.Bracket();
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);
        var outcome = GoodOutcome(plan);
        outcome.ViewResults[0].IsEmpty = true;

        Assert.Contains(FabricationChecklist.Run(part, plan, outcome, options),
            f => f.Code == "DF003" && f.Severity == CheckSeverity.Error);
    }

    [Fact]
    public void MissingMaterialBlocksRelease()
    {
        var part = Samples.Bracket();
        part.Material = "";
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);

        var findings = FabricationChecklist.Run(part, plan, GoodOutcome(plan), options);

        var finding = findings.Single(f => f.Code == "DF030");
        Assert.Equal(CheckSeverity.Error, finding.Severity);
        Assert.Contains("Material", finding.Message);
    }

    [Fact]
    public void AMissingNoteBlockBlocksRelease()
    {
        var part = Samples.Bracket();
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);
        var outcome = GoodOutcome(plan);
        outcome.NoteBlockInserted = false;

        Assert.Contains(FabricationChecklist.Run(part, plan, outcome, options),
            f => f.Code == "DF040" && f.Severity == CheckSeverity.Error);
    }

    [Fact]
    public void AMissingProjectionSymbolIsAWarningBecauseTheSheetCanBeReadMirrored()
    {
        var part = Samples.Bracket();
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);
        var outcome = GoodOutcome(plan);
        outcome.ProjectionSymbolInserted = false;

        var finding = FabricationChecklist.Run(part, plan, outcome, options).Single(f => f.Code == "DF041");
        Assert.Equal(CheckSeverity.Warning, finding.Severity);
        Assert.Contains("third", finding.Message);
    }

    [Fact]
    public void WizardHolesWithoutCalloutsAreFlagged()
    {
        var part = Samples.Bracket();
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);
        var outcome = GoodOutcome(plan);
        foreach (var view in outcome.ViewResults) view.HoleCallouts = 0;

        Assert.Contains(FabricationChecklist.Run(part, plan, outcome, options), f => f.Code == "DF020");
    }

    [Fact]
    public void SheetMetalWithoutAFlatPatternCannotBeCut()
    {
        var part = Samples.Panel();
        var options = Samples.IsoOptions();
        var plan = DrawingPlanner.Plan(part, options);
        var outcome = GoodOutcome(plan);
        outcome.ViewResults.RemoveAt(outcome.ViewResults.ToList().FindIndex(v => v.Kind == ViewKind.FlatPattern.ToString()));

        Assert.Contains(FabricationChecklist.Run(part, plan, outcome, options),
            f => f.Code == "DF060" && f.Severity == CheckSeverity.Error);
    }

    [Fact]
    public void SheetMetalWithAFlatPatternAndBendTablePasses()
    {
        var part = Samples.Panel();
        var options = Samples.IsoOptions();
        var plan = DrawingPlanner.Plan(part, options);

        var findings = FabricationChecklist.Run(part, plan, GoodOutcome(plan), options);
        Assert.DoesNotContain(findings, f => f.Code == "DF060");
        Assert.DoesNotContain(findings, f => f.Code == "DF062");
    }

    [Fact]
    public void AWeldmentWithoutACutListIsRejected()
    {
        var part = Samples.Bracket();
        part.IsWeldment = true;
        part.CutListItems.Add("TUBE 40x40x3 - 400 LG");

        var options = Samples.IsoOptions();
        var plan = DrawingPlanner.Plan(part, options);
        var outcome = GoodOutcome(plan);
        outcome.TablesInserted.Clear();

        Assert.Contains(FabricationChecklist.Run(part, plan, outcome, options),
            f => f.Code == "DF070" && f.Severity == CheckSeverity.Error);
    }

    [Fact]
    public void AnAssemblyWithoutABomIsRejected()
    {
        var part = Samples.Assembly();
        var options = Samples.IsoOptions();
        var plan = DrawingPlanner.Plan(part, options);
        var outcome = GoodOutcome(plan);
        outcome.TablesInserted.Clear();

        Assert.Contains(FabricationChecklist.Run(part, plan, outcome, options),
            f => f.Code == "DF080" && f.Severity == CheckSeverity.Error);
    }

    [Fact]
    public void AnAssemblyIsNotFlaggedForHavingNoDimensions()
    {
        var part = Samples.Assembly();
        var options = Samples.IsoOptions();
        var plan = DrawingPlanner.Plan(part, options);

        var findings = FabricationChecklist.Run(part, plan, GoodOutcome(plan), options);
        Assert.DoesNotContain(findings, f => f.Code == "DF010");
    }

    [Fact]
    public void OverlappingViewsAreCaughtEvenIfThePlannerProducedThem()
    {
        var part = Samples.Bracket();
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);

        // Force a collision the planner would never make.
        var views = plan.Sheets[0].Views;
        views[1].CenterXMm = views[0].CenterXMm;
        views[1].CenterYMm = views[0].CenterYMm;

        Assert.Contains(FabricationChecklist.Run(part, plan, GoodOutcome(plan), options),
            f => f.Code == "DF092");
    }

    [Fact]
    public void AViewOnTheTitleBlockIsAnError()
    {
        var part = Samples.Bracket();
        var options = Samples.AsmeOptions();
        var plan = DrawingPlanner.Plan(part, options);

        var layout = new SheetLayout(plan.Sheets[0].Size);
        var view = plan.Sheets[0].Views[0];
        view.CenterXMm = layout.TitleBlock.CenterX;
        view.CenterYMm = layout.TitleBlock.CenterY;

        Assert.Contains(FabricationChecklist.Run(part, plan, GoodOutcome(plan), options),
            f => f.Code == "DF091" && f.Severity == CheckSeverity.Error);
    }

    [Fact]
    public void ReportCountsReleasablePartsCorrectly()
    {
        var report = new BatchReport();
        report.Outcomes.Add(new DrawingOutcome { PartNumber = "GOOD", Succeeded = true });
        report.Outcomes.Add(new DrawingOutcome { PartNumber = "BAD", Succeeded = true });
        report.Findings["BAD"] = new List<CheckFinding>
        {
            new CheckFinding("DF010", CheckSeverity.Error, "no dimensions", "fix it")
        };
        report.Findings["GOOD"] = new List<CheckFinding>
        {
            new CheckFinding("DF012", CheckSeverity.Info, "nothing serious", "")
        };

        Assert.Equal(new[] { "GOOD" }, report.ReleasablePartsArray());
        Assert.Equal(1, report.ErrorFindings);
    }
}

internal static class BatchReportTestExtensions
{
    public static string[] ReleasablePartsArray(this BatchReport report)
    {
        return report.ReleasableParts().ToArray();
    }
}
