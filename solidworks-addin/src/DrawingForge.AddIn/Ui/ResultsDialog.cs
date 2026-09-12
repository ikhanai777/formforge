using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Windows.Forms;
using DrawingForge.Core.Logging;
using DrawingForge.Core.Options;
using DrawingForge.Core.Reporting;

namespace DrawingForge.AddIn.Ui
{
    /// <summary>
    /// What the run produced, and what still needs a human.
    /// </summary>
    /// <remarks>
    /// The list is sorted so the drawings that need attention come first. A
    /// batch of forty where thirty-eight are clean is only useful if the two
    /// that are not are the first thing on screen.
    /// </remarks>
    internal sealed class ResultsDialog : Form
    {
        private readonly BatchReport _report;
        private readonly BufferedLog _log;
        private readonly ListView _list;
        private readonly TextBox _detail;

        public ResultsDialog(BatchReport report, BufferedLog log)
        {
            _report = report;
            _log = log;

            Text = "DrawingForge results";
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(900, 560);
            MinimumSize = new Size(700, 420);

            Label summary = new Label
            {
                Left = 12,
                Top = 10,
                Width = 876,
                Height = 32,
                Text = report.Summary(),
                Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
            };

            _list = new ListView
            {
                Left = 12,
                Top = 48,
                Width = 876,
                Height = 240,
                View = View.Details,
                FullRowSelect = true,
                MultiSelect = false,
                HideSelection = false,
                Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right
            };
            _list.Columns.Add("Part", 200);
            _list.Columns.Add("Result", 90);
            _list.Columns.Add("Sheet", 70);
            _list.Columns.Add("Scale", 70);
            _list.Columns.Add("Dims", 55);
            _list.Columns.Add("Findings", 90);
            _list.Columns.Add("Drawing", 280);
            _list.SelectedIndexChanged += (s, e) => ShowDetail();

            _detail = new TextBox
            {
                Left = 12,
                Top = 296,
                Width = 876,
                Height = 210,
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Both,
                WordWrap = false,
                Font = new Font(FontFamily.GenericMonospace, 8.25f),
                Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right
            };

            Button openFolder = MakeButton("Open folder", 12, 516);
            openFolder.Click += (s, e) => OpenOutputFolder();

            Button saveReport = MakeButton("Save report...", 130, 516);
            saveReport.Click += (s, e) => SaveReport();

            Button copyLog = MakeButton("Copy log", 248, 516);
            copyLog.Click += (s, e) => CopyLog();

            Button close = MakeButton("Close", 788, 516);
            close.Click += (s, e) => Close();
            close.Anchor = AnchorStyles.Bottom | AnchorStyles.Right;

            Controls.Add(summary);
            Controls.Add(_list);
            Controls.Add(_detail);
            Controls.Add(openFolder);
            Controls.Add(saveReport);
            Controls.Add(copyLog);
            Controls.Add(close);

            Populate();
        }

        private Button MakeButton(string text, int left, int top)
        {
            return new Button
            {
                Text = text,
                Left = left,
                Top = top,
                Width = 110,
                Height = 28,
                Anchor = AnchorStyles.Bottom | AnchorStyles.Left
            };
        }

        private void Populate()
        {
            List<ListViewItem> rows = new List<ListViewItem>();

            foreach (DrawingOutcome outcome in _report.Outcomes)
            {
                IList<CheckFinding> findings;
                _report.Findings.TryGetValue(outcome.PartNumber ?? string.Empty, out findings);

                int errors = findings == null ? 0 : FabricationChecklist.Count(findings, CheckSeverity.Error);
                int warnings = findings == null ? 0 : FabricationChecklist.Count(findings, CheckSeverity.Warning);

                string result = outcome.Succeeded ? "created" : (outcome.Skipped ? "skipped" : "FAILED");
                string findingText = errors > 0 || warnings > 0
                    ? string.Format(CultureInfo.InvariantCulture, "{0} err, {1} warn", errors, warnings)
                    : "clean";

                ListViewItem row = new ListViewItem(new[]
                {
                    outcome.PartNumber ?? "(unnamed)",
                    result,
                    outcome.SheetSizeName ?? "",
                    outcome.Scale ?? "",
                    outcome.TotalDimensions.ToString(CultureInfo.InvariantCulture),
                    findingText,
                    outcome.DrawingPath ?? ""
                });
                row.Tag = outcome;

                if (!outcome.Succeeded && !outcome.Skipped) row.ForeColor = Color.Firebrick;
                else if (errors > 0) row.ForeColor = Color.Firebrick;
                else if (warnings > 0) row.ForeColor = Color.DarkOrange;

                // Sort key: failures first, then error findings, then warnings.
                int severity = (!outcome.Succeeded && !outcome.Skipped) ? 0
                             : errors > 0 ? 1
                             : warnings > 0 ? 2
                             : outcome.Skipped ? 3 : 4;
                row.Name = severity.ToString(CultureInfo.InvariantCulture);
                rows.Add(row);
            }

            rows.Sort((a, b) => string.CompareOrdinal(a.Name, b.Name));
            _list.Items.AddRange(rows.ToArray());

            if (_list.Items.Count > 0) _list.Items[0].Selected = true;
        }

        private void ShowDetail()
        {
            if (_list.SelectedItems.Count == 0) { _detail.Text = string.Empty; return; }

            DrawingOutcome outcome = _list.SelectedItems[0].Tag as DrawingOutcome;
            if (outcome == null) return;

            System.Text.StringBuilder sb = new System.Text.StringBuilder();
            sb.AppendLine(outcome.PartNumber);
            sb.AppendLine("model:   " + (outcome.SourceModelPath ?? ""));
            sb.AppendLine("drawing: " + (outcome.DrawingPath ?? ""));
            if (!string.IsNullOrEmpty(outcome.Configuration))
                sb.AppendLine("config:  " + outcome.Configuration);
            sb.AppendLine(string.Format(CultureInfo.InvariantCulture,
                "{0} sheet(s), {1}, scale {2}, {3} angle",
                outcome.SheetCount, outcome.SheetSizeName, outcome.Scale, outcome.ProjectionAngle));
            sb.AppendLine();

            foreach (ViewOutcome view in outcome.ViewResults)
            {
                sb.AppendLine(string.Format(CultureInfo.InvariantCulture,
                    "  {0,-12} {1,-16} {2,3} dim  {3,2} callout  {4}",
                    view.Kind, view.SolidWorksName ?? view.ViewId,
                    view.DimensionCount, view.HoleCallouts,
                    view.Created ? (view.IsEmpty ? "EMPTY" : "ok") : ("not created: " + view.Failure)));
            }

            if (outcome.TablesInserted.Count > 0)
            {
                sb.AppendLine();
                sb.AppendLine("  tables: " + string.Join(", ", ToArray(outcome.TablesInserted)));
            }

            if (outcome.ExportedFiles.Count > 0)
            {
                sb.AppendLine();
                foreach (string file in outcome.ExportedFiles) sb.AppendLine("  wrote " + file);
            }

            if (outcome.Skipped)
            {
                sb.AppendLine();
                sb.AppendLine("  skipped: " + outcome.SkipReason);
            }

            foreach (string error in outcome.Errors) sb.AppendLine("  error: " + error);
            foreach (string warning in outcome.Warnings) sb.AppendLine("  warning: " + warning);

            IList<CheckFinding> findings;
            if (_report.Findings.TryGetValue(outcome.PartNumber ?? string.Empty, out findings) &&
                findings.Count > 0)
            {
                sb.AppendLine();
                sb.AppendLine("  fabrication check:");
                foreach (CheckFinding finding in findings)
                {
                    sb.AppendLine("    " + finding);
                    if (!string.IsNullOrEmpty(finding.Remedy)) sb.AppendLine("      -> " + finding.Remedy);
                }
            }

            _detail.Text = sb.ToString();
        }

        private static string[] ToArray(IList<string> items)
        {
            string[] array = new string[items.Count];
            items.CopyTo(array, 0);
            return array;
        }

        private void OpenOutputFolder()
        {
            string folder = _report.OutputFolder;
            if (string.IsNullOrEmpty(folder) && _report.Outcomes.Count > 0)
            {
                string drawing = _report.Outcomes[0].DrawingPath;
                if (!string.IsNullOrEmpty(drawing)) folder = Path.GetDirectoryName(drawing);
            }

            if (string.IsNullOrEmpty(folder) || !Directory.Exists(folder))
            {
                MessageBox.Show("No output folder to open.", Text,
                                MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            try { Process.Start("explorer.exe", "\"" + folder + "\""); }
            catch (System.ComponentModel.Win32Exception ex)
            {
                MessageBox.Show("Could not open the folder: " + ex.Message, Text,
                                MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }

        private void SaveReport()
        {
            using (SaveFileDialog dialog = new SaveFileDialog())
            {
                dialog.Filter = "JSON report (*.json)|*.json|Text report (*.txt)|*.txt";
                dialog.FileName = "DrawingForge-report.json";
                if (dialog.ShowDialog() != DialogResult.OK) return;

                try
                {
                    string content = dialog.FileName.EndsWith(".txt", StringComparison.OrdinalIgnoreCase)
                        ? _report.ToText()
                        : _report.ToJson();
                    File.WriteAllText(dialog.FileName, content);
                }
                catch (IOException ex)
                {
                    MessageBox.Show("Could not write the report: " + ex.Message, Text,
                                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                }
            }
        }

        private void CopyLog()
        {
            if (_log == null) return;
            try { Clipboard.SetText(_log.ToString()); }
            catch (System.Runtime.InteropServices.ExternalException)
            {
                MessageBox.Show("The clipboard is in use by another program.", Text,
                                MessageBoxButtons.OK, MessageBoxIcon.Information);
            }
        }
    }
}
