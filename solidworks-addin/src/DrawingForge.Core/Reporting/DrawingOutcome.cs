using System;
using System.Collections.Generic;
using System.Globalization;

namespace DrawingForge.Core.Reporting
{
    /// <summary>What actually happened when a plan was executed.</summary>
    /// <remarks>
    /// Filled in by the add-in as it builds the drawing, then handed to the
    /// fabrication checklist. Keeping it separate from the plan is what lets
    /// the checklist catch the gap between what was asked for and what
    /// SOLIDWORKS produced — a view that came back empty, dimensions that did
    /// not import because nothing was marked for drawing.
    /// </remarks>
    public sealed class DrawingOutcome
    {
        public DrawingOutcome()
        {
            ViewResults = new List<ViewOutcome>();
            ExportedFiles = new List<string>();
            Errors = new List<string>();
            Warnings = new List<string>();
            TablesInserted = new List<string>();
            StartedUtc = DateTime.UtcNow;
        }

        public string PartNumber { get; set; }
        public string SourceModelPath { get; set; }
        public string Configuration { get; set; }
        public string DrawingPath { get; set; }

        public bool Succeeded { get; set; }
        public bool Skipped { get; set; }
        public string SkipReason { get; set; }

        public int SheetCount { get; set; }
        public string SheetSizeName { get; set; }
        public string Scale { get; set; }
        public string ProjectionAngle { get; set; }

        public IList<ViewOutcome> ViewResults { get; private set; }
        public IList<string> TablesInserted { get; private set; }
        public IList<string> ExportedFiles { get; private set; }
        public IList<string> Errors { get; private set; }
        public IList<string> Warnings { get; private set; }

        public bool NoteBlockInserted { get; set; }
        public bool ProjectionSymbolInserted { get; set; }
        public int TitleBlockFieldsWritten { get; set; }

        public DateTime StartedUtc { get; set; }
        public DateTime FinishedUtc { get; set; }

        public TimeSpan Duration
        {
            get { return FinishedUtc > StartedUtc ? FinishedUtc - StartedUtc : TimeSpan.Zero; }
        }

        /// <summary>Total dimensions across every view.</summary>
        public int TotalDimensions
        {
            get
            {
                int total = 0;
                foreach (ViewOutcome view in ViewResults) total += view.DimensionCount;
                return total;
            }
        }

        public override string ToString()
        {
            return string.Format(CultureInfo.InvariantCulture, "{0}: {1}, {2} view(s), {3} dimension(s)",
                PartNumber, Succeeded ? "ok" : (Skipped ? "skipped" : "failed"),
                ViewResults.Count, TotalDimensions);
        }
    }

    /// <summary>What one view ended up with.</summary>
    public sealed class ViewOutcome
    {
        public string ViewId { get; set; }
        public string Kind { get; set; }
        public string SolidWorksName { get; set; }
        public bool Created { get; set; }

        /// <summary>Dimensions on the view after cleanup.</summary>
        public int DimensionCount { get; set; }

        /// <summary>Dimensions removed as duplicates of another view's.</summary>
        public int DuplicatesRemoved { get; set; }

        public int CenterMarks { get; set; }
        public int Centerlines { get; set; }
        public int HoleCallouts { get; set; }

        /// <summary>True when the view came back with no geometry, which means the
        /// reference or configuration is wrong.</summary>
        public bool IsEmpty { get; set; }

        public string Failure { get; set; }
    }
}
