using System;
using System.Collections.Generic;
using System.Globalization;
using DrawingForge.Core.Standards;

namespace DrawingForge.Core.Options
{
    /// <summary>
    /// Every choice the user makes before a run. Serialised to XML so a team
    /// can keep one file next to the project and get identical drawings from
    /// every workstation.
    /// </summary>
    /// <remarks>
    /// Public fields with public setters and a parameterless constructor: this
    /// type is round-tripped by <see cref="System.Xml.Serialization.XmlSerializer"/>,
    /// which is in the framework on every machine that can run a SOLIDWORKS
    /// add-in and needs no package reference.
    /// </remarks>
    public sealed class DrawingOptions
    {
        public DrawingOptions()
        {
            Standard = DrawingStandardKind.Asme;
            ProjectionOverride = null;
            Units = UnitSystem.Inch;
            SheetMode = SheetSelectionMode.AutoSmallestFit;
            SheetSizeName = "B";
            CustomSheetWidthMm = 420;
            CustomSheetHeightMm = 297;

            ScaleMode = ScaleMode.Automatic;
            FixedScaleNumerator = 1;
            FixedScaleDenominator = 1;
            ViewGapMm = 25.0;
            AnnotationAllowance = ScaleSelector.DefaultAnnotationAllowance;
            MaxEnlargement = 10.0;
            MaxReduction = 100.0;
            MinAcceptableAutoScale = 0.2;

            IncludeTopView = true;
            IncludeSideView = true;
            IncludeIsometric = true;
            IncludeSectionWhenInternalFeatures = true;
            IncludeDetailViewsForSmallFeatures = true;
            SmallFeatureThresholdMm = 3.0;
            DetailViewScaleMultiplier = 4.0;

            InsertModelDimensions = true;
            RemoveDuplicateDimensions = true;
            AutoArrangeDimensions = true;
            InsertCenterMarks = true;
            InsertCenterlines = true;
            InsertHoleCallouts = true;
            ShowHiddenLinesOnPrincipalView = false;
            DimensionPrecision = 2;

            IncludeGeneralNotes = true;
            ShowProjectionSymbol = true;
            ExtraNotes = new List<string>();

            SheetMetalFlatPattern = true;
            SheetMetalBendTable = true;
            FlatPatternOnSeparateSheet = true;
            WeldmentCutList = true;

            GenerateAssemblyDrawing = true;
            AssemblyBom = true;
            AssemblyBalloons = true;
            AssemblyExplodedView = true;

            ComponentFilter = ComponentFilter.Default;
            TreatConfigurationsSeparately = false;
            MaxPartsPerRun = 500;

            ExportFormats = ExportFormats.SolidWorksDrawing | ExportFormats.Pdf;
            FileNamePattern = "{PartNumber}{RevSuffix}";
            OverwriteExisting = false;
            OutputFolder = string.Empty;

            DrawingTemplatePath = string.Empty;
            SheetFormatPath = string.Empty;

            CompanyName = string.Empty;
            DrawnBy = Environment.UserName;
            DefaultRevision = "A";
            TitleBlockPropertyMap = DefaultTitleBlockMap();

            RespectModelOrientation = true;
            RunFabricationChecks = true;
            StopOnFirstError = false;
        }

        // ---- standard -------------------------------------------------------

        public DrawingStandardKind Standard { get; set; }

        /// <summary>
        /// Explicit projection convention. Null means "whatever the standard
        /// says", which is third angle for ASME and first angle for ISO.
        /// </summary>
        public ProjectionAngle? ProjectionOverride { get; set; }

        public UnitSystem Units { get; set; }

        // ---- sheet ----------------------------------------------------------

        public SheetSelectionMode SheetMode { get; set; }

        /// <summary>Sheet name used when <see cref="SheetMode"/> is Fixed.</summary>
        public string SheetSizeName { get; set; }

        public double CustomSheetWidthMm { get; set; }
        public double CustomSheetHeightMm { get; set; }

        /// <summary>Drawing template (.drwdot). Empty uses the SOLIDWORKS default template.</summary>
        public string DrawingTemplatePath { get; set; }

        /// <summary>Sheet format (.slddrt) applied to every sheet. Empty keeps the template's own.</summary>
        public string SheetFormatPath { get; set; }

        // ---- scale and layout ----------------------------------------------

        public ScaleMode ScaleMode { get; set; }
        public double FixedScaleNumerator { get; set; }
        public double FixedScaleDenominator { get; set; }
        public double ViewGapMm { get; set; }
        public double AnnotationAllowance { get; set; }

        /// <summary>
        /// Largest enlargement the planner will pick on its own. Without a cap,
        /// a 2 mm pin fills a B sheet at 100:1, which is a legal ratio and a
        /// drawing nobody would issue.
        /// </summary>
        public double MaxEnlargement { get; set; }

        /// <summary>Largest reduction the planner will pick on its own, as a denominator.</summary>
        public double MaxReduction { get; set; }

        /// <summary>
        /// When auto-selecting a sheet, do not settle for a sheet that forces a
        /// scale smaller than this if a larger sheet avoids it. 0.2 means "a
        /// bigger sheet beats going below 1:5".
        /// </summary>
        public double MinAcceptableAutoScale { get; set; }

        // ---- view set -------------------------------------------------------

        public bool IncludeTopView { get; set; }
        public bool IncludeSideView { get; set; }
        public bool IncludeIsometric { get; set; }
        public bool IncludeSectionWhenInternalFeatures { get; set; }
        public bool IncludeDetailViewsForSmallFeatures { get; set; }

        /// <summary>Features smaller than this get a detail view.</summary>
        public double SmallFeatureThresholdMm { get; set; }

        /// <summary>Detail views are drawn this many times larger than the sheet scale.</summary>
        public double DetailViewScaleMultiplier { get; set; }

        public bool ShowHiddenLinesOnPrincipalView { get; set; }

        /// <summary>Keep the model's own front orientation unless the heuristic strongly disagrees.</summary>
        public bool RespectModelOrientation { get; set; }

        // ---- dimensions and annotations -------------------------------------

        public bool InsertModelDimensions { get; set; }
        public bool RemoveDuplicateDimensions { get; set; }
        public bool AutoArrangeDimensions { get; set; }
        public bool InsertCenterMarks { get; set; }
        public bool InsertCenterlines { get; set; }
        public bool InsertHoleCallouts { get; set; }

        /// <summary>Decimal places on linear dimensions.</summary>
        public int DimensionPrecision { get; set; }

        public bool IncludeGeneralNotes { get; set; }
        public bool ShowProjectionSymbol { get; set; }

        /// <summary>Notes appended after the standard block. Tokens are expanded.</summary>
        public List<string> ExtraNotes { get; set; }

        // ---- fabrication features -------------------------------------------

        public bool SheetMetalFlatPattern { get; set; }
        public bool SheetMetalBendTable { get; set; }
        public bool FlatPatternOnSeparateSheet { get; set; }
        public bool WeldmentCutList { get; set; }

        public bool GenerateAssemblyDrawing { get; set; }
        public bool AssemblyBom { get; set; }
        public bool AssemblyBalloons { get; set; }
        public bool AssemblyExplodedView { get; set; }

        // ---- traversal ------------------------------------------------------

        public ComponentFilter ComponentFilter { get; set; }

        /// <summary>Make one drawing per component configuration rather than per file.</summary>
        public bool TreatConfigurationsSeparately { get; set; }

        /// <summary>Guard rail against pointing the add-in at a 10,000-part assembly by accident.</summary>
        public int MaxPartsPerRun { get; set; }

        // ---- output ---------------------------------------------------------

        public ExportFormats ExportFormats { get; set; }

        /// <summary>
        /// Tokens: {PartNumber} {ModelName} {Config} {Rev} {RevSuffix} {Date} {Project} {Standard}.
        /// </summary>
        public string FileNamePattern { get; set; }

        public bool OverwriteExisting { get; set; }

        /// <summary>Empty means "next to the model file".</summary>
        public string OutputFolder { get; set; }

        // ---- title block ----------------------------------------------------

        public string CompanyName { get; set; }
        public string DrawnBy { get; set; }
        public string DefaultRevision { get; set; }

        /// <summary>
        /// Drawing property name to model property name. The title block in the
        /// sheet format links to the drawing property; the add-in copies the
        /// value across from the model so the link resolves.
        /// </summary>
        /// <remarks>
        /// An array, not a List, and that is deliberate. XmlSerializer reads a
        /// list property by calling the getter and adding to whatever is already
        /// there, so a list that the constructor pre-populates comes back
        /// doubled on every load — the defaults plus the saved copy. An array
        /// has to go through the setter, so it is replaced rather than appended
        /// to. <see cref="ExtraNotes"/> can stay a list because it starts empty.
        /// </remarks>
        public PropertyMapping[] TitleBlockPropertyMap { get; set; }

        // ---- run behaviour ---------------------------------------------------

        public bool RunFabricationChecks { get; set; }
        public bool StopOnFirstError { get; set; }

        // ---- derived ---------------------------------------------------------

        /// <summary>The standard's rules, resolved for the chosen unit system.</summary>
        public StandardProfile Profile()
        {
            return StandardProfile.For(Standard, Units);
        }

        /// <summary>Projection convention actually in force.</summary>
        public ProjectionAngle EffectiveProjection()
        {
            return ProjectionOverride ?? Profile().DefaultProjection;
        }

        /// <summary>Scale ratio when the mode is Fixed; null otherwise.</summary>
        public Ratio FixedScale()
        {
            if (ScaleMode != ScaleMode.Fixed) return null;
            if (FixedScaleNumerator <= 0 || FixedScaleDenominator <= 0) return null;
            return new Ratio(FixedScaleNumerator, FixedScaleDenominator);
        }

        /// <summary>Sheet size for a run, ignoring auto-fit (which is resolved per part).</summary>
        public SheetSize ResolveSheet()
        {
            switch (SheetMode)
            {
                case SheetSelectionMode.Custom:
                    return SheetCatalog.Custom("Custom", CustomSheetWidthMm, CustomSheetHeightMm, Standard);
                case SheetSelectionMode.Fixed:
                    return SheetCatalog.ByName(SheetSizeName) ?? SheetCatalog.DefaultFor(Standard);
                default:
                    return SheetCatalog.DefaultFor(Standard);
            }
        }

        /// <summary>
        /// Problems that would make a run produce bad drawings. Empty list means
        /// the options are usable.
        /// </summary>
        public IList<string> Validate()
        {
            List<string> problems = new List<string>();

            if (SheetMode == SheetSelectionMode.Fixed && SheetCatalog.ByName(SheetSizeName) == null)
            {
                problems.Add(string.Format(CultureInfo.InvariantCulture,
                    "Sheet size '{0}' is not a size this add-in knows.", SheetSizeName));
            }

            if (SheetMode == SheetSelectionMode.Custom &&
                (CustomSheetWidthMm <= 50 || CustomSheetHeightMm <= 50))
            {
                problems.Add("A custom sheet must be at least 50 mm on each side.");
            }

            if (ScaleMode == ScaleMode.Fixed && (FixedScaleNumerator <= 0 || FixedScaleDenominator <= 0))
            {
                problems.Add("A fixed scale needs a positive numerator and denominator.");
            }

            if (ScaleMode == ScaleMode.Fixed)
            {
                Ratio fixedScale = new Ratio(FixedScaleNumerator, FixedScaleDenominator);
                if (!ScaleSelector.IsStandard(Profile().ScaleLadder, fixedScale))
                {
                    problems.Add(string.Format(CultureInfo.InvariantCulture,
                        "Scale {0} is not a preferred scale under {1}; the shop will question it.",
                        fixedScale, Profile().DimensioningSpec));
                }
            }

            if (ViewGapMm < 5)
                problems.Add("A view gap under 5 mm leaves nowhere to put dimensions.");

            if (AnnotationAllowance < 0 || AnnotationAllowance > 0.8)
                problems.Add("The annotation allowance must be between 0 and 0.8.");

            if (ExportFormats == ExportFormats.None)
                problems.Add("No export format is selected, so the run would produce no files.");

            if (string.IsNullOrEmpty(FileNamePattern))
                problems.Add("The file name pattern is empty.");

            if (MaxPartsPerRun <= 0)
                problems.Add("The part limit must be at least 1.");

            if (DimensionPrecision < 0 || DimensionPrecision > 6)
                problems.Add("Dimension precision must be between 0 and 6 decimal places.");

            return problems;
        }

        /// <summary>
        /// Default mapping from drawing property to model property, covering the
        /// fields a title block normally links to.
        /// </summary>
        public static PropertyMapping[] DefaultTitleBlockMap()
        {
            return new[]
            {
                new PropertyMapping("PartNo", "PartNo"),
                new PropertyMapping("Description", "Description"),
                new PropertyMapping("Revision", "Revision"),
                new PropertyMapping("Material", "Material"),
                new PropertyMapping("Finish", "Finish"),
                new PropertyMapping("Weight", "Weight"),
                new PropertyMapping("Project", "Project"),
                new PropertyMapping("Vendor", "Vendor"),
                new PropertyMapping("Treatment", "Treatment")
            };
        }
    }

    /// <summary>Drawing property fed from a model property of (possibly) another name.</summary>
    public sealed class PropertyMapping
    {
        public string DrawingProperty { get; set; }
        public string ModelProperty { get; set; }

        public PropertyMapping() { }

        public PropertyMapping(string drawingProperty, string modelProperty)
        {
            DrawingProperty = drawingProperty;
            ModelProperty = modelProperty;
        }
    }
}
