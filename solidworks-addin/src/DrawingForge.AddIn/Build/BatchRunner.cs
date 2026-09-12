using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using DrawingForge.AddIn.Interop;
using DrawingForge.Core.Logging;
using DrawingForge.Core.Naming;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;
using DrawingForge.Core.Reporting;
using SolidWorks.Interop.sldworks;

namespace DrawingForge.AddIn.Build
{
    /// <summary>Progress of a run, for the dialog to render.</summary>
    internal sealed class BatchProgress
    {
        public int Completed { get; set; }
        public int Total { get; set; }
        public string CurrentPart { get; set; }
        public string Message { get; set; }

        public int Percent
        {
            get { return Total <= 0 ? 0 : (int)Math.Round(100.0 * Completed / Total); }
        }
    }

    /// <summary>
    /// Runs the whole job: walk the assembly, draw every part, check every
    /// drawing, and report.
    /// </summary>
    /// <remarks>
    /// One part failing must never take the batch down with it. Forty drawings
    /// and one failure is a good afternoon; a crash on part three that loses the
    /// first two is not. So each part is isolated, its failure recorded against
    /// its own row in the report, and the run carries on unless the user asked
    /// it to stop on the first error.
    /// </remarks>
    internal sealed class BatchRunner
    {
        private readonly ISldWorks _app;
        private readonly DrawingOptions _options;
        private readonly ILog _log;

        private volatile bool _cancelled;

        public BatchRunner(ISldWorks app, DrawingOptions options, ILog log)
        {
            if (app == null) throw new ArgumentNullException("app");
            if (options == null) throw new ArgumentNullException("options");
            _app = app;
            _options = options;
            _log = log;
        }

        /// <summary>Asks the run to stop after the part currently in progress.</summary>
        public void Cancel()
        {
            _cancelled = true;
            _log.Info("Cancellation requested; finishing the current part first.");
        }

        /// <summary>
        /// Draws everything under <paramref name="root"/>.
        /// </summary>
        public BatchReport Run(ModelDoc2 root, Action<BatchProgress> progress)
        {
            BatchReport report = new BatchReport
            {
                AssemblyPath = root != null ? root.GetPathName() : null,
                OutputFolder = string.IsNullOrEmpty(_options.OutputFolder) ? null : _options.OutputFolder,
                StandardName = _options.Profile().DimensioningSpec,
                ProjectionAngle = _options.EffectiveProjection() == ProjectionAngle.Third ? "THIRD" : "FIRST"
            };

            IList<string> problems = _options.Validate();
            if (problems.Count > 0)
            {
                foreach (string problem in problems) _log.Error("Options: " + problem);
                report.FinishedUtc = DateTime.UtcNow;
                return report;
            }

            IList<DrawingTask> tasks = AssemblyTraverser.Collect(root, _options, _log);
            if (tasks.Count == 0)
            {
                _log.Warn("Nothing to draw. Every component was filtered out, or the document is empty.");
                report.FinishedUtc = DateTime.UtcNow;
                return report;
            }

            HashSet<string> usedNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            string rootTitle = root != null ? root.GetTitle() : null;

            for (int index = 0; index < tasks.Count; index++)
            {
                if (_cancelled)
                {
                    report.Cancelled = true;
                    _log.Warn("Run cancelled after " + index + " of " + tasks.Count + " part(s).");
                    break;
                }

                DrawingTask task = tasks[index];
                Report(progress, index, tasks.Count, task.DisplayName, "Drawing " + task.DisplayName);

                DrawingOutcome outcome = RunOne(task, usedNames, report, rootTitle);
                report.Outcomes.Add(outcome);

                if (!outcome.Succeeded && !outcome.Skipped && _options.StopOnFirstError)
                {
                    _log.Error("Stopping: " + task.DisplayName + " failed and stop-on-error is set.");
                    break;
                }
            }

            Report(progress, tasks.Count, tasks.Count, null, "Finished");
            report.FinishedUtc = DateTime.UtcNow;
            _log.Info(report.Summary());
            return report;
        }

        private DrawingOutcome RunOne(DrawingTask task, ICollection<string> usedNames, BatchReport report,
                                      string rootTitle)
        {
            DrawingOutcome outcome = new DrawingOutcome
            {
                PartNumber = task.DisplayName,
                SourceModelPath = task.FilePath,
                Configuration = task.Configuration
            };

            ModelDoc2 model = null;
            bool weOpenedIt = false;

            try
            {
                string failure;
                model = FindOpenDocument(task.FilePath);
                if (model == null)
                {
                    model = SwDoc.Open(_app, task.FilePath, task.Configuration, out failure, _log);
                    weOpenedIt = model != null;
                    if (model == null)
                    {
                        outcome.Skipped = true;
                        outcome.SkipReason = failure;
                        _log.Warn("Skipped " + task.DisplayName + ": " + failure);
                        return outcome;
                    }
                }

                PartSummary summary = PartInspector.Inspect(model, task, _log);
                DrawingPlan plan = DrawingPlanner.Plan(summary, _options);

                plan.OutputFilePath = UniquePath(plan.OutputFilePath, usedNames);
                outcome.DrawingPath = plan.OutputFilePath;

                foreach (string diagnostic in plan.Diagnostics) _log.Info(diagnostic);

                if (File.Exists(plan.OutputFilePath) && !_options.OverwriteExisting)
                {
                    outcome.Skipped = true;
                    outcome.SkipReason = "A drawing already exists at " + plan.OutputFilePath +
                                         " and overwrite is off.";
                    _log.Info("Skipped " + task.DisplayName + ": " + outcome.SkipReason);
                    return outcome;
                }

                DrawingBuilder builder = new DrawingBuilder(_app, _options, _log);
                DrawingOutcome built = builder.Build(plan, summary, model);

                built.PartNumber = plan.PartNumber;
                built.SourceModelPath = task.FilePath;
                built.Configuration = task.Configuration;

                if (_options.RunFabricationChecks)
                {
                    IList<CheckFinding> findings =
                        FabricationChecklist.Run(summary, plan, built, _options);
                    report.Findings[built.PartNumber ?? task.DisplayName] = findings;

                    int errors = FabricationChecklist.Count(findings, CheckSeverity.Error);
                    int warnings = FabricationChecklist.Count(findings, CheckSeverity.Warning);
                    if (errors > 0 || warnings > 0)
                    {
                        _log.Warn(string.Format(CultureInfo.InvariantCulture,
                            "{0}: {1} fabrication error(s), {2} warning(s).",
                            built.PartNumber, errors, warnings));
                    }
                }

                return built;
            }
            catch (System.Runtime.InteropServices.COMException ex)
            {
                outcome.Errors.Add(LogExtensions.Describe(ex));
                _log.Error("Failed on " + task.DisplayName, ex);
                return outcome;
            }
            catch (IOException ex)
            {
                outcome.Errors.Add(ex.Message);
                _log.Error("Failed on " + task.DisplayName, ex);
                return outcome;
            }
            catch (UnauthorizedAccessException ex)
            {
                outcome.Errors.Add(ex.Message);
                _log.Error("Failed on " + task.DisplayName, ex);
                return outcome;
            }
            finally
            {
                outcome.FinishedUtc = DateTime.UtcNow;

                // Close only what this run opened, and never the document the
                // user was working in.
                if (weOpenedIt && model != null)
                {
                    string title = model.GetTitle();
                    if (!string.Equals(title, rootTitle, StringComparison.OrdinalIgnoreCase))
                        SwDoc.Close(_app, title, _log);
                }
            }
        }

        /// <summary>
        /// Finds an already-open document by path so the run reuses it instead of
        /// opening a second copy.
        /// </summary>
        private ModelDoc2 FindOpenDocument(string path)
        {
            if (string.IsNullOrEmpty(path)) return null;

            object firstDoc;
            if (!SwDispatch.TryInvoke(_app, new[] { "GetFirstDocument2", "GetFirstDocument" },
                                      new object[0], out firstDoc, _log))
            {
                return null;
            }

            int guard = 0;
            while (firstDoc != null && guard++ < 5000)
            {
                ModelDoc2 candidate = firstDoc as ModelDoc2;
                if (candidate != null &&
                    string.Equals(candidate.GetPathName(), path, StringComparison.OrdinalIgnoreCase))
                {
                    return candidate;
                }

                object next;
                if (!SwDispatch.TryInvoke(firstDoc, new[] { "GetNext", "GetNext2" }, new object[0],
                                          out next, _log))
                {
                    return null;
                }
                firstDoc = next;
            }

            return null;
        }

        /// <summary>Keeps two parts with the same name from overwriting each other.</summary>
        private static string UniquePath(string path, ICollection<string> usedNames)
        {
            if (string.IsNullOrEmpty(path)) return path;

            string folder = Path.GetDirectoryName(path) ?? string.Empty;
            string stem = Path.GetFileNameWithoutExtension(path);
            string extension = Path.GetExtension(path);

            string unique = FileNameBuilder.MakeUnique(stem, usedNames as ICollection<string>);
            return Path.Combine(folder, unique + extension);
        }

        private static void Report(Action<BatchProgress> progress, int completed, int total,
                                   string part, string message)
        {
            if (progress == null) return;
            progress(new BatchProgress
            {
                Completed = completed,
                Total = total,
                CurrentPart = part,
                Message = message
            });
        }
    }
}
