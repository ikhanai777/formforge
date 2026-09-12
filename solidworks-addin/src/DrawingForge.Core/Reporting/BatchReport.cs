using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;

namespace DrawingForge.Core.Reporting
{
    /// <summary>Result of one run over an assembly.</summary>
    public sealed class BatchReport
    {
        public BatchReport()
        {
            Outcomes = new List<DrawingOutcome>();
            Findings = new Dictionary<string, IList<CheckFinding>>(StringComparer.OrdinalIgnoreCase);
            StartedUtc = DateTime.UtcNow;
        }

        public string AssemblyPath { get; set; }
        public string OutputFolder { get; set; }
        public string StandardName { get; set; }
        public string ProjectionAngle { get; set; }

        public IList<DrawingOutcome> Outcomes { get; private set; }

        /// <summary>Fabrication findings, keyed by part number.</summary>
        public IDictionary<string, IList<CheckFinding>> Findings { get; private set; }

        public DateTime StartedUtc { get; set; }
        public DateTime FinishedUtc { get; set; }

        public bool Cancelled { get; set; }

        public int Succeeded
        {
            get
            {
                int n = 0;
                foreach (DrawingOutcome o in Outcomes) { if (o.Succeeded) n++; }
                return n;
            }
        }

        public int Failed
        {
            get
            {
                int n = 0;
                foreach (DrawingOutcome o in Outcomes) { if (!o.Succeeded && !o.Skipped) n++; }
                return n;
            }
        }

        public int SkippedCount
        {
            get
            {
                int n = 0;
                foreach (DrawingOutcome o in Outcomes) { if (o.Skipped) n++; }
                return n;
            }
        }

        public int ErrorFindings
        {
            get { return CountFindings(Options.CheckSeverity.Error); }
        }

        public int WarningFindings
        {
            get { return CountFindings(Options.CheckSeverity.Warning); }
        }

        private int CountFindings(Options.CheckSeverity severity)
        {
            int n = 0;
            foreach (KeyValuePair<string, IList<CheckFinding>> entry in Findings)
            {
                foreach (CheckFinding finding in entry.Value)
                {
                    if (finding.Severity == severity) n++;
                }
            }
            return n;
        }

        /// <summary>Drawings that are clean enough to release without a look.</summary>
        public IList<string> ReleasableParts()
        {
            List<string> releasable = new List<string>();
            foreach (DrawingOutcome outcome in Outcomes)
            {
                if (!outcome.Succeeded) continue;
                IList<CheckFinding> findings;
                if (!Findings.TryGetValue(outcome.PartNumber ?? string.Empty, out findings))
                {
                    releasable.Add(outcome.PartNumber);
                    continue;
                }
                if (FabricationChecklist.IsReleasable(findings)) releasable.Add(outcome.PartNumber);
            }
            return releasable;
        }

        /// <summary>One-paragraph summary for the status bar and the log.</summary>
        public string Summary()
        {
            TimeSpan duration = FinishedUtc > StartedUtc ? FinishedUtc - StartedUtc : TimeSpan.Zero;
            return string.Format(CultureInfo.InvariantCulture,
                "{0} drawing(s) created, {1} failed, {2} skipped in {3:0.#}s. {4} released clean; {5} error finding(s), {6} warning(s).",
                Succeeded, Failed, SkippedCount, duration.TotalSeconds,
                ReleasableParts().Count, ErrorFindings, WarningFindings);
        }

        /// <summary>The full report as JSON, for archiving next to the drawings.</summary>
        public string ToJson()
        {
            JsonWriter json = new JsonWriter();
            json.BeginObject();
            json.Property("assembly", AssemblyPath);
            json.Property("outputFolder", OutputFolder);
            json.Property("standard", StandardName);
            json.Property("projection", ProjectionAngle);
            json.Property("startedUtc", StartedUtc.ToString("o", CultureInfo.InvariantCulture));
            json.Property("finishedUtc", FinishedUtc.ToString("o", CultureInfo.InvariantCulture));
            json.Property("cancelled", Cancelled);
            json.Property("succeeded", Succeeded);
            json.Property("failed", Failed);
            json.Property("skipped", SkippedCount);

            json.PropertyName("drawings");
            json.BeginArray();
            foreach (DrawingOutcome outcome in Outcomes)
            {
                json.BeginObject();
                json.Property("partNumber", outcome.PartNumber);
                json.Property("model", outcome.SourceModelPath);
                json.Property("configuration", outcome.Configuration);
                json.Property("drawing", outcome.DrawingPath);
                json.Property("succeeded", outcome.Succeeded);
                json.Property("skipped", outcome.Skipped);
                json.Property("skipReason", outcome.SkipReason);
                json.Property("sheetCount", outcome.SheetCount);
                json.Property("sheetSize", outcome.SheetSizeName);
                json.Property("scale", outcome.Scale);
                json.Property("projection", outcome.ProjectionAngle);
                json.Property("dimensions", outcome.TotalDimensions);
                json.Property("durationSeconds", Math.Round(outcome.Duration.TotalSeconds, 2));

                json.PropertyName("views");
                json.BeginArray();
                foreach (ViewOutcome view in outcome.ViewResults)
                {
                    json.BeginObject();
                    json.Property("id", view.ViewId);
                    json.Property("kind", view.Kind);
                    json.Property("name", view.SolidWorksName);
                    json.Property("created", view.Created);
                    json.Property("empty", view.IsEmpty);
                    json.Property("dimensions", view.DimensionCount);
                    json.Property("duplicatesRemoved", view.DuplicatesRemoved);
                    json.Property("centerMarks", view.CenterMarks);
                    json.Property("centerlines", view.Centerlines);
                    json.Property("holeCallouts", view.HoleCallouts);
                    json.Property("failure", view.Failure);
                    json.EndObject();
                }
                json.EndArray();

                json.PropertyName("tables");
                json.StringArray(outcome.TablesInserted);
                json.PropertyName("exports");
                json.StringArray(outcome.ExportedFiles);
                json.PropertyName("errors");
                json.StringArray(outcome.Errors);
                json.PropertyName("warnings");
                json.StringArray(outcome.Warnings);

                IList<CheckFinding> findings;
                json.PropertyName("findings");
                json.BeginArray();
                if (Findings.TryGetValue(outcome.PartNumber ?? string.Empty, out findings))
                {
                    foreach (CheckFinding finding in findings)
                    {
                        json.BeginObject();
                        json.Property("code", finding.Code);
                        json.Property("severity", finding.Severity.ToString());
                        json.Property("message", finding.Message);
                        json.Property("remedy", finding.Remedy);
                        json.EndObject();
                    }
                }
                json.EndArray();

                json.EndObject();
            }
            json.EndArray();
            json.EndObject();
            return json.ToString();
        }

        /// <summary>The report as plain text, for the results dialog.</summary>
        public string ToText()
        {
            StringBuilder sb = new StringBuilder();
            sb.AppendLine("DrawingForge run report");
            sb.AppendLine(new string('-', 60));
            sb.AppendLine("Assembly:   " + (AssemblyPath ?? "(none)"));
            sb.AppendLine("Output:     " + (OutputFolder ?? "(model folder)"));
            sb.AppendLine("Standard:   " + (StandardName ?? "?") + ", " + (ProjectionAngle ?? "?"));
            sb.AppendLine();
            sb.AppendLine(Summary());
            sb.AppendLine();

            foreach (DrawingOutcome outcome in Outcomes)
            {
                sb.AppendLine(string.Format(CultureInfo.InvariantCulture, "{0}  [{1}]",
                    outcome.PartNumber,
                    outcome.Succeeded ? "created" : (outcome.Skipped ? "skipped" : "FAILED")));

                if (outcome.Skipped && !string.IsNullOrEmpty(outcome.SkipReason))
                    sb.AppendLine("    reason: " + outcome.SkipReason);

                if (outcome.Succeeded)
                {
                    sb.AppendLine(string.Format(CultureInfo.InvariantCulture,
                        "    {0} sheet(s), {1}, scale {2}, {3} dimension(s)",
                        outcome.SheetCount, outcome.SheetSizeName, outcome.Scale, outcome.TotalDimensions));
                    foreach (string file in outcome.ExportedFiles) sb.AppendLine("    -> " + file);
                }

                foreach (string error in outcome.Errors) sb.AppendLine("    error: " + error);
                foreach (string warning in outcome.Warnings) sb.AppendLine("    warning: " + warning);

                IList<CheckFinding> findings;
                if (Findings.TryGetValue(outcome.PartNumber ?? string.Empty, out findings))
                {
                    foreach (CheckFinding finding in findings)
                    {
                        sb.AppendLine("    " + finding);
                        if (!string.IsNullOrEmpty(finding.Remedy))
                            sb.AppendLine("        " + finding.Remedy);
                    }
                }
                sb.AppendLine();
            }

            return sb.ToString();
        }
    }
}
