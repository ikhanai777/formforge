using System;
using System.Collections.Generic;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Options;
using DrawingForge.Core.Standards;

namespace DrawingForge.Core.Planning
{
    /// <summary>
    /// A complete, executable description of one drawing document.
    /// </summary>
    /// <remarks>
    /// The planner produces this; the add-in's builder walks it and makes the
    /// SOLIDWORKS API calls. Nothing in here references SOLIDWORKS, so a plan
    /// can be produced, dumped to JSON and inspected on a machine that has no
    /// CAD installed — which is how the layout rules are tested.
    /// </remarks>
    public sealed class DrawingPlan
    {
        public DrawingPlan()
        {
            Sheets = new List<SheetPlan>();
            Notes = new List<string>();
            TitleBlockProperties = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            Diagnostics = new List<string>();
        }

        /// <summary>Model the drawing is made from.</summary>
        public string SourceModelPath { get; set; }

        /// <summary>Configuration the views reference.</summary>
        public string Configuration { get; set; }

        /// <summary>Part number as it goes in the title block.</summary>
        public string PartNumber { get; set; }

        public DrawingStandardKind Standard { get; set; }
        public ProjectionAngle Projection { get; set; }
        public UnitSystem Units { get; set; }

        /// <summary>Drawing template (.drwdot) to start from; empty means the SOLIDWORKS default.</summary>
        public string TemplatePath { get; set; }

        /// <summary>Where the .SLDDRW is written.</summary>
        public string OutputFilePath { get; set; }

        public IList<SheetPlan> Sheets { get; private set; }

        /// <summary>General notes, already numbered, in sheet order.</summary>
        public IList<string> Notes { get; private set; }

        /// <summary>Custom properties to write on the drawing for the title block to pick up.</summary>
        public IDictionary<string, string> TitleBlockProperties { get; private set; }

        /// <summary>Decisions worth reporting: why a scale was forced, what was skipped.</summary>
        public IList<string> Diagnostics { get; private set; }

        /// <summary>Every view across every sheet.</summary>
        public IEnumerable<ViewPlan> AllViews
        {
            get
            {
                foreach (SheetPlan sheet in Sheets)
                {
                    foreach (ViewPlan view in sheet.Views) yield return view;
                }
            }
        }

        public ViewPlan FindView(string id)
        {
            if (string.IsNullOrEmpty(id)) return null;
            foreach (ViewPlan v in AllViews)
            {
                if (string.Equals(v.Id, id, StringComparison.Ordinal)) return v;
            }
            return null;
        }
    }

    /// <summary>One sheet of a drawing.</summary>
    public sealed class SheetPlan
    {
        public SheetPlan()
        {
            Views = new List<ViewPlan>();
            Tables = new List<TablePlan>();
        }

        /// <summary>Sheet name as it appears on the tab.</summary>
        public string Name { get; set; }

        public SheetSize Size { get; set; }

        /// <summary>Sheet scale. Views that do not override it inherit it.</summary>
        public Ratio Scale { get; set; }

        public ProjectionAngle Projection { get; set; }

        /// <summary>Sheet format (.slddrt) to apply; empty keeps the template's own.</summary>
        public string SheetFormatPath { get; set; }

        /// <summary>Place the first/third angle projection symbol on this sheet.</summary>
        public bool ShowProjectionSymbol { get; set; }

        /// <summary>Put the general note block on this sheet.</summary>
        public bool ShowNoteBlock { get; set; }

        /// <summary>Lower-left corner of the note block, millimetres from the sheet origin.</summary>
        public double NoteBlockXMm { get; set; }
        public double NoteBlockYMm { get; set; }

        public IList<ViewPlan> Views { get; private set; }
        public IList<TablePlan> Tables { get; private set; }
    }

    /// <summary>One view on a sheet.</summary>
    public sealed class ViewPlan
    {
        public ViewPlan()
        {
            Id = Guid.NewGuid().ToString("N").Substring(0, 8);
            UseSheetScale = true;
            InsertModelDimensions = true;
            InsertCenterMarks = true;
            InsertCenterlines = true;
            InsertHoleCallouts = true;
            Display = DisplayStyle.HiddenLinesRemoved;
        }

        /// <summary>Stable id used by child views to name their parent.</summary>
        public string Id { get; set; }

        public ViewKind Kind { get; set; }

        /// <summary>Named view for principal and isometric views.</summary>
        public ViewOrientation Orientation { get; set; }

        /// <summary>SOLIDWORKS named view, e.g. "*Front" or "*Isometric".</summary>
        public string SwViewName { get; set; }

        /// <summary>Centre of the view on the sheet, millimetres from the lower-left corner.</summary>
        public double CenterXMm { get; set; }
        public double CenterYMm { get; set; }

        /// <summary>Width and height the view occupies on the sheet at its own scale.</summary>
        public double WidthMm { get; set; }
        public double HeightMm { get; set; }

        /// <summary>View scale when <see cref="UseSheetScale"/> is false.</summary>
        public Ratio Scale { get; set; }

        public bool UseSheetScale { get; set; }

        /// <summary>In-plane rotation, degrees counter-clockwise.</summary>
        public double RotationDegrees { get; set; }

        public DisplayStyle Display { get; set; }

        /// <summary>Label under the view, e.g. "SECTION A-A" or "DETAIL B (2:1)".</summary>
        public string Label { get; set; }

        /// <summary>Parent view id for projected, section, detail and auxiliary views.</summary>
        public string ParentViewId { get; set; }

        /// <summary>Where a projected view sits relative to its parent.</summary>
        public ProjectionDirection Direction { get; set; }

        /// <summary>For a section view: the cut runs horizontally through the parent.</summary>
        public bool SectionIsHorizontal { get; set; }

        /// <summary>Offset of the cutting line from the parent's centre, millimetres on the sheet.</summary>
        public double SectionOffsetMm { get; set; }

        /// <summary>Section letter, e.g. "A" for SECTION A-A.</summary>
        public string SectionLabel { get; set; }

        /// <summary>For a detail view: circle centre on the sheet and radius, millimetres.</summary>
        public double DetailCenterXMm { get; set; }
        public double DetailCenterYMm { get; set; }
        public double DetailRadiusMm { get; set; }

        /// <summary>Show the assembly in its saved exploded state.</summary>
        public bool Exploded { get; set; }

        public bool InsertModelDimensions { get; set; }
        public bool InsertCenterMarks { get; set; }
        public bool InsertCenterlines { get; set; }
        public bool InsertHoleCallouts { get; set; }

        /// <summary>Show sheet metal bend notes on a flat pattern view.</summary>
        public bool ShowBendNotes { get; set; }

        /// <summary>Footprint including the room left for dimensions around the view.</summary>
        public RectMm Envelope
        {
            get
            {
                return RectMm.FromCenter(CenterXMm, CenterYMm, WidthMm, HeightMm);
            }
        }

        public override string ToString()
        {
            return string.Format(System.Globalization.CultureInfo.InvariantCulture,
                "{0} {1} at ({2:0.#}, {3:0.#}) {4:0.#}x{5:0.#}mm",
                Kind, SwViewName ?? Orientation.ToString(), CenterXMm, CenterYMm, WidthMm, HeightMm);
        }
    }

    /// <summary>Kinds of table the add-in can place.</summary>
    public enum TableKind
    {
        BillOfMaterials = 0,
        WeldmentCutList = 1,
        SheetMetalBendTable = 2,
        RevisionTable = 3,
        HoleTable = 4
    }

    /// <summary>A table anchored on a sheet.</summary>
    public sealed class TablePlan
    {
        public TableKind Kind { get; set; }

        /// <summary>Anchor point, millimetres from the sheet's lower-left corner.</summary>
        public double AnchorXMm { get; set; }
        public double AnchorYMm { get; set; }

        /// <summary>Table template file; empty uses the SOLIDWORKS default.</summary>
        public string TemplatePath { get; set; }

        /// <summary>View the table is attached to, when the table needs one.</summary>
        public string AttachedViewId { get; set; }

        /// <summary>Balloon every component of the attached view, for a BOM.</summary>
        public bool AutoBalloon { get; set; }
    }
}
