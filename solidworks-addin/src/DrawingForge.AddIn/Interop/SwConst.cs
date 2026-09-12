namespace DrawingForge.AddIn.Interop
{
    /// <summary>
    /// SOLIDWORKS enumeration values as plain integers.
    /// </summary>
    /// <remarks>
    /// The add-in passes integers rather than referring to <c>swconst</c> enum
    /// members directly in most places. That is deliberate: enum member names
    /// have been renamed between releases (and some values only exist in newer
    /// ones), and a rename turns into a compile error that blocks the whole
    /// add-in from building against an older SOLIDWORKS. An integer that a
    /// release does not understand fails one call, which the caller logs and
    /// works around.
    ///
    /// Every value here is from the published API documentation. Where a value
    /// is version-sensitive it is called out in docs/api-notes.md.
    /// </remarks>
    internal static class SwConst
    {
        // swDocumentTypes_e
        public const int swDocNONE = 0;
        public const int swDocPART = 1;
        public const int swDocASSEMBLY = 2;
        public const int swDocDRAWING = 3;

        // swOpenDocOptions_e
        public const int swOpenDocOptions_Silent = 1;
        public const int swOpenDocOptions_ReadOnly = 2;
        public const int swOpenDocOptions_ViewOnly = 128;
        public const int swOpenDocOptions_LoadLightweight = 1024;

        // swSaveAsVersion_e
        public const int swSaveAsCurrentVersion = 0;

        // swSaveAsOptions_e
        public const int swSaveAsOptions_Silent = 1;
        public const int swSaveAsOptions_Copy = 2;
        public const int swSaveAsOptions_SaveReferenced = 4;
        public const int swSaveAsOptions_AvoidRebuildOnSave = 512;

        // swDocTemplateTypes_e
        public const int swDocTemplateTypeDRAWING = 3;

        // swUserPreferenceStringValue_e
        public const int swDefaultTemplateDrawing = 10;

        // swUserPreferenceIntegerValue_e
        public const int swDetailingDimensionStandard = 68;
        public const int swDetailingNewDetailViewStyle = 111;
        public const int swUnitsLinear = 49;
        public const int swUnitsLinearDecimalPlaces = 51;

        // swUserPreferenceToggle_e
        public const int swInputDimValOnCreate = 109;
        public const int swDetailingCenterMarksHoles = 105;

        // swLengthUnit_e
        public const int swMM = 0;
        public const int swCM = 1;
        public const int swMETER = 2;
        public const int swINCHES = 3;

        // swDetailingStandard_e
        public const int swDetailingStandardANSI = 1;
        public const int swDetailingStandardISO = 2;
        public const int swDetailingStandardDIN = 3;
        public const int swDetailingStandardJIS = 4;
        public const int swDetailingStandardBSI = 5;
        public const int swDetailingStandardGOST = 6;

        // swInsertAnnotation_e — the bitmask InsertModelAnnotations takes.
        public const int swInsertDimensionsMarkedForDrawing = 1;
        public const int swInsertDimensionsNotMarkedForDrawing = 2;
        public const int swInsertDatums = 4;
        public const int swInsertGTols = 8;
        public const int swInsertNotes = 16;
        public const int swInsertSFSymbols = 32;
        public const int swInsertWelds = 64;
        public const int swInsertDowelSyms = 128;
        public const int swInsertCThreads = 256;
        public const int swInsertAllTypes = 511;

        // swImportModelItemsSource_e
        public const int swImportModelItemsFromEntireModel = 1;
        public const int swImportModelItemsFromSelectedFeature = 2;

        // swDisplayMode / swViewDisplayMode_e
        public const int swViewDispWireframe = 0;
        public const int swViewDispHiddenLinesRemoved = 1;
        public const int swViewDispHiddenLinesGrayed = 2;
        public const int swViewDispShaded = 3;
        public const int swViewDispShadedWithEdges = 4;

        // swComponentSuppressionState_e
        public const int swComponentSuppressed = 0;
        public const int swComponentLightweight = 1;
        public const int swComponentFullyResolved = 2;
        public const int swComponentResolvedLightweight = 3;

        // swSelectType_e strings are used instead of ordinals in SelectByID2.
        public const string SelectTypeDrawingView = "DRAWINGVIEW";
        public const string SelectTypeSketchSegment = "SKETCHSEGMENT";
        public const string SelectTypeNote = "NOTE";

        // swCustomInfoType_e
        public const int swCustomInfoText = 30;
        public const int swCustomInfoDate = 64;
        public const int swCustomInfoNumber = 3;

        // swCustomPropertyAddOption_e
        public const int swCustomPropertyDeleteAndAdd = 1;
        public const int swCustomPropertyOnlyIfNew = 2;
        public const int swCustomPropertyReplaceValue = 3;

        // swBodyType_e
        public const int swSolidBody = 0;
        public const int swSheetBody = 1;

        // swExportDataFileType_e
        public const int swExportPdfData = 1;

        // swExportDataSheetsToExport_e
        public const int swExportData_ExportAllSheets = 1;
        public const int swExportData_ExportSpecifiedSheets = 2;

        // swBomType_e
        public const int swBomType_PartsOnly = 1;
        public const int swBomType_TopLevelOnly = 2;
        public const int swBomType_Indented = 3;

        // swBOMConfigurationAnchorType_e
        public const int swBOMConfigurationAnchor_TopLeft = 1;
        public const int swBOMConfigurationAnchor_TopRight = 2;
        public const int swBOMConfigurationAnchor_BottomLeft = 3;
        public const int swBOMConfigurationAnchor_BottomRight = 4;

        // swNumberingType_e
        public const int swNumberingType_Detailed = 1;
        public const int swNumberingType_Flat = 2;

        // swCreateDrawViewSectionStyle / section view line style
        public const int swDetailCircleStyle_Circle = 0;

        // swTableAnnotationAnchorType_e shares the BOM anchor ordinals.
        public const int swTableAnchor_TopLeft = 1;
        public const int swTableAnchor_TopRight = 2;

        // swDwgTemplates_e (the TemplateIn argument of sheet setup)
        public const int swDwgTemplateNone = 0;
        public const int swDwgTemplateCustom = 1;
        public const int swDwgTemplateAsize = 2;

        // swAlignDimensionType_e — argument to IModelDocExtension::AlignDimensions.
        public const int swAlignDimensionType_AutoArrange = 1;
        public const int swAlignDimensionType_Parallel = 2;
        public const int swAlignDimensionType_Collinear = 3;
    }
}
