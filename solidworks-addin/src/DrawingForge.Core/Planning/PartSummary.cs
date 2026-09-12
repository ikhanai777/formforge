using System;
using System.Collections.Generic;
using DrawingForge.Core.Geometry;

namespace DrawingForge.Core.Planning
{
    /// <summary>
    /// Everything the planner needs to know about one model. Produced by the
    /// add-in's part inspector from a live SOLIDWORKS document; consumed by the
    /// planner, which never touches the SOLIDWORKS API.
    /// </summary>
    /// <remarks>
    /// This split is the point of the architecture: the decisions that make a
    /// drawing correct — which face is the front, what scale, which sheet,
    /// which views, which notes — are made against this plain object and are
    /// therefore testable without SOLIDWORKS running.
    /// </remarks>
    public sealed class PartSummary
    {
        public PartSummary()
        {
            Holes = new List<HoleSummary>();
            CustomProperties = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            CutListItems = new List<string>();
            BomItems = new List<BomItemSummary>();
            Configuration = string.Empty;
        }

        /// <summary>Full path of the model document.</summary>
        public string FilePath { get; set; }

        /// <summary>File name without extension; the default part number.</summary>
        public string ModelName { get; set; }

        /// <summary>Configuration the drawing is made from.</summary>
        public string Configuration { get; set; }

        /// <summary>True when the model is an assembly rather than a part.</summary>
        public bool IsAssembly { get; set; }

        /// <summary>Bounding box in model space, millimetres.</summary>
        public BoundingBox Box { get; set; }

        /// <summary>Mass in grams, 0 when unknown.</summary>
        public double MassGrams { get; set; }

        /// <summary>Material name as set on the model, empty when none.</summary>
        public string Material { get; set; }

        /// <summary>Surface treatment from the model's properties, empty when none.</summary>
        public string Finish { get; set; }

        /// <summary>Custom properties read off the model (configuration-specific merged over document).</summary>
        public IDictionary<string, string> CustomProperties { get; private set; }

        /// <summary>True when the part has a sheet metal body with a flat pattern.</summary>
        public bool IsSheetMetal { get; set; }

        /// <summary>Sheet metal thickness in millimetres, 0 when not sheet metal.</summary>
        public double SheetMetalThicknessMm { get; set; }

        /// <summary>Number of bends in the flat pattern.</summary>
        public int BendCount { get; set; }

        /// <summary>Flat pattern extents in millimetres, null when not sheet metal.</summary>
        public BoundingBox FlatPatternBox { get; set; }

        /// <summary>True when the part is a weldment with a cut list.</summary>
        public bool IsWeldment { get; set; }

        /// <summary>Cut list item descriptions, for the cut list table decision.</summary>
        public IList<string> CutListItems { get; private set; }

        /// <summary>Holes found on the model, used for callouts and section decisions.</summary>
        public IList<HoleSummary> Holes { get; private set; }

        /// <summary>
        /// True when the part has geometry no outside view can show: pockets,
        /// bores that do not break a visible face, internal ribs. Drives the
        /// section view decision.
        /// </summary>
        public bool HasInternalFeatures { get; set; }

        /// <summary>
        /// True when the part is a body of revolution. A turned part is
        /// dimensioned from a single view with the axis horizontal, not from a
        /// three-view set.
        /// </summary>
        public bool IsRotational { get; set; }

        /// <summary>Axis of revolution when <see cref="IsRotational"/>.</summary>
        public Axis RotationAxis { get; set; }

        /// <summary>
        /// Number of distinct model dimensions marked for drawing. Zero means
        /// inserting model items will produce nothing and the drawing needs a
        /// DimXpert or reference-dimension pass instead.
        /// </summary>
        public int DimensionsMarkedForDrawing { get; set; }

        /// <summary>True when the model carries a DimXpert scheme we can import.</summary>
        public bool HasDimXpertScheme { get; set; }

        /// <summary>Components for the assembly BOM; empty for parts.</summary>
        public IList<BomItemSummary> BomItems { get; private set; }

        /// <summary>True when the assembly configuration has an exploded view saved.</summary>
        public bool HasExplodedView { get; set; }

        /// <summary>Smallest feature size in millimetres; drives detail view decisions.</summary>
        public double SmallestFeatureMm { get; set; }

        /// <summary>Convenience: the part number, from properties when present.</summary>
        public string PartNumber
        {
            get
            {
                string value;
                if (CustomProperties.TryGetValue("PartNo", out value) && !string.IsNullOrEmpty(value)) return value;
                if (CustomProperties.TryGetValue("PartNumber", out value) && !string.IsNullOrEmpty(value)) return value;
                if (CustomProperties.TryGetValue("Number", out value) && !string.IsNullOrEmpty(value)) return value;
                return ModelName;
            }
        }

        /// <summary>Convenience: revision from properties, empty when absent.</summary>
        public string Revision
        {
            get
            {
                string value;
                if (CustomProperties.TryGetValue("Revision", out value) && !string.IsNullOrEmpty(value)) return value;
                if (CustomProperties.TryGetValue("Rev", out value) && !string.IsNullOrEmpty(value)) return value;
                return string.Empty;
            }
        }

        /// <summary>Convenience: description from properties, falls back to the model name.</summary>
        public string Description
        {
            get
            {
                string value;
                if (CustomProperties.TryGetValue("Description", out value) && !string.IsNullOrEmpty(value)) return value;
                return ModelName;
            }
        }
    }

    /// <summary>One hole, as the inspector understands it.</summary>
    public sealed class HoleSummary
    {
        /// <summary>Nominal diameter in millimetres.</summary>
        public double DiameterMm { get; set; }

        /// <summary>Depth in millimetres; 0 for a through hole.</summary>
        public double DepthMm { get; set; }

        /// <summary>True when the hole goes all the way through.</summary>
        public bool IsThrough { get; set; }

        /// <summary>True when the hole came from the Hole Wizard, so it carries a callout.</summary>
        public bool IsWizardHole { get; set; }

        /// <summary>True when the hole is tapped.</summary>
        public bool IsTapped { get; set; }

        /// <summary>Thread designation when tapped, e.g. "M6x1.0" or "1/4-20 UNC".</summary>
        public string ThreadDesignation { get; set; }

        /// <summary>Axis the hole is drilled along, in model space.</summary>
        public Axis Axis { get; set; }

        /// <summary>Number of instances of this hole (from a pattern).</summary>
        public int InstanceCount { get; set; }

        public HoleSummary()
        {
            InstanceCount = 1;
            ThreadDesignation = string.Empty;
        }
    }

    /// <summary>One row of an assembly BOM.</summary>
    public sealed class BomItemSummary
    {
        public string ItemNumber { get; set; }
        public string PartNumber { get; set; }
        public string Description { get; set; }
        public int Quantity { get; set; }
        public string Material { get; set; }
        public bool IsPurchased { get; set; }
    }
}
