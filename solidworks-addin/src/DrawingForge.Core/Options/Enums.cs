using System;

namespace DrawingForge.Core.Options
{
    /// <summary>Which projection convention the sheet uses.</summary>
    /// <remarks>
    /// ASME Y14.3 uses third angle, ISO 128 / ISO 5456 uses first angle. The
    /// two are not cosmetic: swapping them silently mirrors the part for the
    /// shop, so the choice is always explicit on the sheet and is stamped into
    /// the projection symbol.
    /// </remarks>
    public enum ProjectionAngle
    {
        First = 1,
        Third = 3
    }

    /// <summary>Drafting standard that drives defaults for everything else.</summary>
    public enum DrawingStandardKind
    {
        Asme = 0,
        Iso = 1,
        Din = 2,
        Jis = 3,
        Bsi = 4,
        Gost = 5
    }

    /// <summary>Linear unit the drawing is dimensioned in.</summary>
    public enum UnitSystem
    {
        Millimeter = 0,
        Inch = 1
    }

    /// <summary>How the sheet size is picked for each drawing.</summary>
    public enum SheetSelectionMode
    {
        /// <summary>Always use <see cref="DrawingOptions.SheetSizeName"/>.</summary>
        Fixed = 0,

        /// <summary>Use the smallest sheet in the standard's series that holds the
        /// view set at an acceptable scale.</summary>
        AutoSmallestFit = 1,

        /// <summary>User supplied width and height.</summary>
        Custom = 2
    }

    /// <summary>How the view scale is picked.</summary>
    public enum ScaleMode
    {
        /// <summary>Largest standard scale on the ladder that still fits.</summary>
        Automatic = 0,

        /// <summary>Use <see cref="DrawingOptions.FixedScaleNumerator"/> / Denominator.</summary>
        Fixed = 1
    }

    /// <summary>Named model view used as the drawing's front view.</summary>
    /// <remarks>
    /// These correspond to the SOLIDWORKS named views "*Front", "*Back",
    /// "*Left", "*Right", "*Top", "*Bottom". Restricting the front view to
    /// these six (plus an optional in-plane rotation) keeps every view the
    /// add-in creates reproducible from the model alone.
    /// </remarks>
    public enum ViewOrientation
    {
        Front = 0,
        Back = 1,
        Left = 2,
        Right = 3,
        Top = 4,
        Bottom = 5
    }

    /// <summary>Role a view plays on the sheet.</summary>
    public enum ViewKind
    {
        /// <summary>The primary view every other view is projected from.</summary>
        Principal = 0,

        /// <summary>Orthographic view projected from the principal view.</summary>
        Projected = 1,

        Isometric = 2,
        Section = 3,
        Detail = 4,
        FlatPattern = 5,
        Auxiliary = 6,

        /// <summary>Assembly view, optionally in its exploded state.</summary>
        Assembly = 7
    }

    /// <summary>Direction a projected view sits relative to its parent.</summary>
    public enum ProjectionDirection
    {
        Up = 0,
        Down = 1,
        Left = 2,
        Right = 3
    }

    /// <summary>Hidden line / shading treatment for a view.</summary>
    public enum DisplayStyle
    {
        Wireframe = 0,
        HiddenLinesVisible = 1,
        HiddenLinesRemoved = 2,
        Shaded = 3,
        ShadedWithEdges = 4
    }

    /// <summary>Output files written for each generated drawing.</summary>
    [Flags]
    public enum ExportFormats
    {
        None = 0,
        SolidWorksDrawing = 1,
        Pdf = 2,
        Dwg = 4,
        Dxf = 8,

        /// <summary>Flat pattern only, as a DXF, for laser/waterjet nesting.</summary>
        FlatPatternDxf = 16,

        /// <summary>Step file of the referenced model, next to the drawing.</summary>
        Step = 32
    }

    /// <summary>Components the traversal deliberately drops.</summary>
    [Flags]
    public enum ComponentFilter
    {
        None = 0,
        Suppressed = 1,
        Hidden = 2,
        Envelopes = 4,
        Toolbox = 8,
        Virtual = 16,
        ExcludedFromBom = 32,

        Default = Suppressed | Envelopes | Toolbox
    }

    /// <summary>Severity of a fabrication-readiness finding.</summary>
    public enum CheckSeverity
    {
        Info = 0,
        Warning = 1,
        Error = 2
    }
}
