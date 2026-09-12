using System;
using System.Collections.Generic;
using System.Globalization;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;
using DrawingForge.Core.Standards;

namespace DrawingForge.Core.Reporting
{
    /// <summary>One finding from the fabrication-readiness pass.</summary>
    public sealed class CheckFinding
    {
        public string Code { get; set; }
        public CheckSeverity Severity { get; set; }
        public string Message { get; set; }

        /// <summary>What to do about it, in the drafter's terms.</summary>
        public string Remedy { get; set; }

        public CheckFinding() { }

        public CheckFinding(string code, CheckSeverity severity, string message, string remedy)
        {
            Code = code;
            Severity = severity;
            Message = message;
            Remedy = remedy;
        }

        public override string ToString()
        {
            return string.Format(CultureInfo.InvariantCulture, "[{0}] {1}: {2}",
                Severity.ToString().ToUpperInvariant(), Code, Message);
        }
    }

    /// <summary>
    /// Checks a finished drawing against what a shop needs before it can quote
    /// and cut.
    /// </summary>
    /// <remarks>
    /// This is the part of the add-in that earns the phrase "ready for
    /// fabrication". Generating views is easy; the question that matters is
    /// whether the result can be handed to a machinist without a phone call.
    /// Every rule here is something that would trigger that phone call.
    ///
    /// The checks are deliberately conservative about what they claim. Counting
    /// dimensions cannot prove a part is fully defined — that needs a solver
    /// over the model's constraints, which SOLIDWORKS does not expose — so the
    /// under-dimensioned rule is a warning that names its own limits rather
    /// than an assertion of completeness.
    /// </remarks>
    public static class FabricationChecklist
    {
        /// <summary>Fewest dimensions a prismatic part needs before the count looks suspicious.</summary>
        public const int MinimumDimensionsForPrismaticPart = 3;

        public static IList<CheckFinding> Run(PartSummary part, DrawingPlan plan,
                                              DrawingOutcome outcome, DrawingOptions options)
        {
            List<CheckFinding> findings = new List<CheckFinding>();
            if (plan == null || outcome == null) return findings;

            CheckViews(findings, plan, outcome);
            CheckDimensions(findings, part, plan, outcome);
            CheckHoles(findings, part, outcome);
            CheckTitleBlock(findings, part, plan, outcome);
            CheckNotes(findings, plan, outcome, options);
            CheckScale(findings, plan, options);
            CheckSheetMetal(findings, part, plan, outcome, options);
            CheckWeldment(findings, part, outcome, options);
            CheckAssembly(findings, part, outcome, options);
            CheckLayout(findings, plan);

            return findings;
        }

        /// <summary>True when nothing worse than a warning was found.</summary>
        public static bool IsReleasable(IEnumerable<CheckFinding> findings)
        {
            foreach (CheckFinding finding in findings)
            {
                if (finding.Severity == CheckSeverity.Error) return false;
            }
            return true;
        }

        public static int Count(IEnumerable<CheckFinding> findings, CheckSeverity severity)
        {
            int count = 0;
            foreach (CheckFinding finding in findings)
            {
                if (finding.Severity == severity) count++;
            }
            return count;
        }

        // ---- individual rules ------------------------------------------------

        private static void CheckViews(IList<CheckFinding> findings, DrawingPlan plan, DrawingOutcome outcome)
        {
            int orthographic = 0;
            foreach (ViewOutcome view in outcome.ViewResults)
            {
                if (!view.Created) continue;
                if (view.Kind == ViewKind.Principal.ToString() ||
                    view.Kind == ViewKind.Projected.ToString() ||
                    view.Kind == ViewKind.Section.ToString() ||
                    view.Kind == ViewKind.FlatPattern.ToString())
                {
                    orthographic++;
                }
            }

            if (orthographic == 0)
            {
                findings.Add(new CheckFinding("DF001", CheckSeverity.Error,
                    "The drawing has no orthographic view.",
                    "Check that the model opened and that the referenced configuration is not suppressed."));
            }

            foreach (ViewOutcome view in outcome.ViewResults)
            {
                if (!view.Created)
                {
                    findings.Add(new CheckFinding("DF002", CheckSeverity.Error,
                        string.Format(CultureInfo.InvariantCulture,
                            "View {0} ({1}) was not created: {2}",
                            view.ViewId, view.Kind, view.Failure ?? "no reason reported"),
                        "Create the view by hand, or rerun with that view type switched off."));
                }
                else if (view.IsEmpty)
                {
                    findings.Add(new CheckFinding("DF003", CheckSeverity.Error,
                        string.Format(CultureInfo.InvariantCulture,
                            "View {0} ({1}) came back empty.", view.ViewId, view.Kind),
                        "The configuration probably has no visible bodies. Check the model."));
                }
            }
        }

        private static void CheckDimensions(IList<CheckFinding> findings, PartSummary part,
                                            DrawingPlan plan, DrawingOutcome outcome)
        {
            if (part != null && part.IsAssembly) return;

            if (outcome.TotalDimensions == 0)
            {
                findings.Add(new CheckFinding("DF010", CheckSeverity.Error,
                    "The drawing carries no dimensions, so nothing on it can be made.",
                    part != null && part.DimensionsMarkedForDrawing == 0
                        ? "No model dimensions are marked for drawing. Mark them in the model, add a DimXpert scheme, or dimension the views by hand."
                        : "Model dimensions exist but none imported. Check that the views reference the right configuration."));
                return;
            }

            if (outcome.TotalDimensions < MinimumDimensionsForPrismaticPart)
            {
                findings.Add(new CheckFinding("DF011", CheckSeverity.Warning,
                    string.Format(CultureInfo.InvariantCulture,
                        "Only {0} dimension(s) across the whole drawing; a prismatic part normally needs at least three just for overall size.",
                        outcome.TotalDimensions),
                    "Review the drawing and add what is missing. This count cannot prove a part is fully defined — read it as a smell, not a verdict."));
            }

            foreach (ViewOutcome view in outcome.ViewResults)
            {
                if (!view.Created || view.IsEmpty) continue;
                if (view.Kind == ViewKind.Isometric.ToString()) continue;
                if (view.Kind == ViewKind.Projected.ToString() && view.DimensionCount == 0)
                {
                    findings.Add(new CheckFinding("DF012", CheckSeverity.Info,
                        string.Format(CultureInfo.InvariantCulture,
                            "Projected view {0} carries no dimensions.", view.ViewId),
                        "That is fine when the other views cover the feature; check that it is not an oversight."));
                }
            }
        }

        private static void CheckHoles(IList<CheckFinding> findings, PartSummary part, DrawingOutcome outcome)
        {
            if (part == null || part.Holes.Count == 0) return;

            int callouts = 0;
            foreach (ViewOutcome view in outcome.ViewResults) callouts += view.HoleCallouts;

            int wizardHoles = 0;
            int tappedHoles = 0;
            foreach (HoleSummary hole in part.Holes)
            {
                if (hole.IsWizardHole) wizardHoles++;
                if (hole.IsTapped) tappedHoles++;
            }

            if (wizardHoles > 0 && callouts == 0)
            {
                findings.Add(new CheckFinding("DF020", CheckSeverity.Warning,
                    string.Format(CultureInfo.InvariantCulture,
                        "The part has {0} Hole Wizard hole(s) but the drawing carries no hole callouts.",
                        wizardHoles),
                    "Insert hole callouts, or dimension the holes with diameter and depth by hand."));
            }

            if (tappedHoles > 0 && callouts < tappedHoles)
            {
                findings.Add(new CheckFinding("DF021", CheckSeverity.Warning,
                    string.Format(CultureInfo.InvariantCulture,
                        "{0} tapped hole(s) but only {1} callout(s); a thread with no designation cannot be cut.",
                        tappedHoles, callouts),
                    "Add the thread designation and class to every tapped hole."));
            }
        }

        private static void CheckTitleBlock(IList<CheckFinding> findings, PartSummary part,
                                            DrawingPlan plan, DrawingOutcome outcome)
        {
            string[] required = { "PartNo", "Description", "Revision", "Material", "DrawnBy", "Scale" };
            List<string> missing = new List<string>();

            foreach (string field in required)
            {
                string value;
                if (!plan.TitleBlockProperties.TryGetValue(field, out value) || string.IsNullOrEmpty(value))
                    missing.Add(field);
            }

            if (missing.Count > 0)
            {
                CheckSeverity severity = missing.Contains("Material") || missing.Contains("PartNo")
                    ? CheckSeverity.Error
                    : CheckSeverity.Warning;

                findings.Add(new CheckFinding("DF030", severity,
                    "Title block fields with no value: " + string.Join(", ", missing.ToArray()) + ".",
                    "Set the matching custom properties on the model. A part with no material cannot be quoted."));
            }

            if (outcome.TitleBlockFieldsWritten == 0 && plan.TitleBlockProperties.Count > 0)
            {
                findings.Add(new CheckFinding("DF031", CheckSeverity.Warning,
                    "No title block properties were written to the drawing.",
                    "Check that the drawing template's title block links to custom properties rather than hard-coded text."));
            }
        }

        private static void CheckNotes(IList<CheckFinding> findings, DrawingPlan plan,
                                       DrawingOutcome outcome, DrawingOptions options)
        {
            if (options != null && !options.IncludeGeneralNotes) return;

            if (!outcome.NoteBlockInserted)
            {
                findings.Add(new CheckFinding("DF040", CheckSeverity.Error,
                    "The general note block is missing, so the drawing states no tolerance standard, unit or edge condition.",
                    "Rerun with notes switched on, or add the note block by hand."));
            }

            if (options != null && options.ShowProjectionSymbol && !outcome.ProjectionSymbolInserted)
            {
                findings.Add(new CheckFinding("DF041", CheckSeverity.Warning,
                    string.Format(CultureInfo.InvariantCulture,
                        "No projection symbol on the sheet, and the drawing is in {0} angle.",
                        plan.Projection == Options.ProjectionAngle.Third ? "third" : "first"),
                    "A sheet without the symbol can be read mirrored. Add it to the sheet format."));
            }
        }

        private static void CheckScale(IList<CheckFinding> findings, DrawingPlan plan, DrawingOptions options)
        {
            if (options == null) return;
            IReadOnlyList<Ratio> ladder = options.Profile().ScaleLadder;

            foreach (SheetPlan sheet in plan.Sheets)
            {
                if (sheet.Scale == null) continue;
                if (!ScaleSelector.IsStandard(ladder, sheet.Scale))
                {
                    findings.Add(new CheckFinding("DF050", CheckSeverity.Warning,
                        string.Format(CultureInfo.InvariantCulture,
                            "Sheet {0} is at {1}, which is not a preferred scale under {2}.",
                            sheet.Name, sheet.Scale, options.Profile().DimensioningSpec),
                        "Use a preferred ratio; an odd scale invites mis-measurement off the print."));
                }
            }
        }

        private static void CheckSheetMetal(IList<CheckFinding> findings, PartSummary part, DrawingPlan plan,
                                            DrawingOutcome outcome, DrawingOptions options)
        {
            if (part == null || !part.IsSheetMetal) return;

            bool hasFlat = false;
            foreach (ViewOutcome view in outcome.ViewResults)
            {
                if (view.Created && view.Kind == ViewKind.FlatPattern.ToString()) hasFlat = true;
            }

            if (!hasFlat)
            {
                findings.Add(new CheckFinding("DF060", CheckSeverity.Error,
                    "Sheet metal part with no flat pattern view; it cannot be cut from this drawing.",
                    "Switch the flat pattern option on, or check that the part has a valid flat pattern."));
            }

            if (part.SheetMetalThicknessMm <= 0)
            {
                findings.Add(new CheckFinding("DF061", CheckSeverity.Error,
                    "Sheet metal thickness is not stated.",
                    "Set the sheet metal parameters on the model so the note and the cut list can state gauge."));
            }

            if (part.BendCount > 0 && options != null && options.SheetMetalBendTable &&
                !outcome.TablesInserted.Contains(TableKind.SheetMetalBendTable.ToString()))
            {
                findings.Add(new CheckFinding("DF062", CheckSeverity.Warning,
                    string.Format(CultureInfo.InvariantCulture,
                        "{0} bend(s) but no bend table.", part.BendCount),
                    "Without bend direction and radius the press brake operator is guessing."));
            }
        }

        private static void CheckWeldment(IList<CheckFinding> findings, PartSummary part,
                                          DrawingOutcome outcome, DrawingOptions options)
        {
            if (part == null || !part.IsWeldment) return;
            if (options != null && !options.WeldmentCutList) return;

            if (!outcome.TablesInserted.Contains(TableKind.WeldmentCutList.ToString()))
            {
                findings.Add(new CheckFinding("DF070", CheckSeverity.Error,
                    "Weldment with no cut list; the shop has no stock list to cut from.",
                    "Insert the weldment cut list, or issue the members as separate detail drawings."));
            }
        }

        private static void CheckAssembly(IList<CheckFinding> findings, PartSummary part,
                                          DrawingOutcome outcome, DrawingOptions options)
        {
            if (part == null || !part.IsAssembly) return;
            if (options == null || !options.AssemblyBom) return;

            if (!outcome.TablesInserted.Contains(TableKind.BillOfMaterials.ToString()))
            {
                findings.Add(new CheckFinding("DF080", CheckSeverity.Error,
                    "Assembly drawing with no bill of materials.",
                    "Insert the BOM; an assembly drawing without one identifies nothing."));
            }
            else if (part.BomItems.Count > 0 && options.AssemblyBalloons)
            {
                findings.Add(new CheckFinding("DF081", CheckSeverity.Info,
                    string.Format(CultureInfo.InvariantCulture,
                        "BOM has {0} item(s); check every balloon landed on a component.", part.BomItems.Count),
                    "Auto-ballooning can stack balloons on a crowded view. Spread them if they overlap."));
            }
        }

        private static void CheckLayout(IList<CheckFinding> findings, DrawingPlan plan)
        {
            foreach (SheetPlan sheet in plan.Sheets)
            {
                SheetLayout layout = new SheetLayout(sheet.Size);
                for (int i = 0; i < sheet.Views.Count; i++)
                {
                    ViewPlan a = sheet.Views[i];

                    if (!layout.Drawable.Contains(a.Envelope, 1.0))
                    {
                        findings.Add(new CheckFinding("DF090", CheckSeverity.Error,
                            string.Format(CultureInfo.InvariantCulture,
                                "View {0} on {1} runs outside the sheet border.", a.Id, sheet.Name),
                            "Use a larger sheet or a smaller scale."));
                    }

                    if (a.Envelope.Intersects(layout.TitleBlock))
                    {
                        findings.Add(new CheckFinding("DF091", CheckSeverity.Error,
                            string.Format(CultureInfo.InvariantCulture,
                                "View {0} on {1} overlaps the title block.", a.Id, sheet.Name),
                            "Move the view or use a larger sheet."));
                    }

                    for (int j = i + 1; j < sheet.Views.Count; j++)
                    {
                        if (a.Envelope.Intersects(sheet.Views[j].Envelope))
                        {
                            findings.Add(new CheckFinding("DF092", CheckSeverity.Warning,
                                string.Format(CultureInfo.InvariantCulture,
                                    "Views {0} and {1} on {2} overlap.", a.Id, sheet.Views[j].Id, sheet.Name),
                                "Increase the view gap or move to a larger sheet."));
                        }
                    }
                }
            }
        }
    }
}
