using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.Windows.Forms;
using DrawingForge.Core.Options;
using DrawingForge.Core.Standards;

namespace DrawingForge.AddIn.Ui
{
    /// <summary>
    /// The settings dialog.
    /// </summary>
    /// <remarks>
    /// Laid out in code rather than in a designer file so the whole dialog is
    /// readable in one place and there is no .resx to keep in step with it.
    ///
    /// Two behaviours here are worth more than they look. Changing the standard
    /// re-populates the sheet list and moves the projection default, so an ASME
    /// user never sees A3 in the list and an ISO user never accidentally issues
    /// third angle. And the dialog will not close on OK while the options would
    /// produce bad drawings — the same validation the batch runner uses, shown
    /// before the run instead of after it.
    /// </remarks>
    internal sealed class OptionsDialog : Form
    {
        private readonly DrawingOptions _options;

        // Standard
        private ComboBox _standard;
        private ComboBox _projection;
        private ComboBox _units;
        private ComboBox _sheetMode;
        private ComboBox _sheetSize;
        private NumericUpDown _customWidth;
        private NumericUpDown _customHeight;
        private ComboBox _scaleMode;
        private NumericUpDown _scaleNumerator;
        private NumericUpDown _scaleDenominator;
        private NumericUpDown _maxEnlargement;
        private NumericUpDown _maxReduction;
        private TextBox _templatePath;
        private TextBox _sheetFormatPath;

        // Views
        private CheckBox _topView;
        private CheckBox _sideView;
        private CheckBox _isometric;
        private CheckBox _sectionWhenInternal;
        private CheckBox _detailViews;
        private CheckBox _hiddenLines;
        private CheckBox _respectOrientation;
        private NumericUpDown _smallFeature;
        private NumericUpDown _detailMultiplier;
        private NumericUpDown _viewGap;
        private NumericUpDown _annotationAllowance;
        private CheckBox _flatPattern;
        private CheckBox _bendTable;
        private CheckBox _flatOnOwnSheet;
        private CheckBox _cutList;
        private CheckBox _assemblyDrawing;
        private CheckBox _bom;
        private CheckBox _balloons;
        private CheckBox _exploded;

        // Annotations
        private CheckBox _modelDimensions;
        private CheckBox _removeDuplicates;
        private CheckBox _autoArrange;
        private CheckBox _centerMarks;
        private CheckBox _centerlines;
        private CheckBox _holeCallouts;
        private NumericUpDown _precision;
        private CheckBox _generalNotes;
        private CheckBox _projectionSymbol;
        private TextBox _extraNotes;
        private TextBox _company;
        private TextBox _drawnBy;
        private TextBox _revision;

        // Output
        private CheckBox _exportSlddrw;
        private CheckBox _exportPdf;
        private CheckBox _exportDwg;
        private CheckBox _exportDxf;
        private CheckBox _exportFlatDxf;
        private CheckBox _exportStep;
        private TextBox _namePattern;
        private TextBox _outputFolder;
        private CheckBox _overwrite;
        private CheckBox _skipSuppressed;
        private CheckBox _skipHidden;
        private CheckBox _skipEnvelopes;
        private CheckBox _skipToolbox;
        private CheckBox _skipExcludedFromBom;
        private CheckBox _configurationsSeparately;
        private NumericUpDown _maxParts;
        private CheckBox _runChecks;
        private CheckBox _stopOnError;

        private Label _validation;

        /// <summary>The edited options; only meaningful after an OK result.</summary>
        public DrawingOptions Options { get { return _options; } }

        public OptionsDialog(DrawingOptions options)
        {
            _options = OptionsStore.Clone(options ?? new DrawingOptions());

            Text = "DrawingForge options";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterScreen;
            MinimizeBox = false;
            MaximizeBox = false;
            ClientSize = new Size(660, 620);

            TabControl tabs = new TabControl { Left = 10, Top = 10, Width = 640, Height = 530 };
            tabs.TabPages.Add(BuildStandardTab());
            tabs.TabPages.Add(BuildViewsTab());
            tabs.TabPages.Add(BuildAnnotationsTab());
            tabs.TabPages.Add(BuildOutputTab());

            _validation = new Label
            {
                Left = 12,
                Top = 548,
                Width = 430,
                Height = 60,
                ForeColor = Color.Firebrick
            };

            Button ok = new Button { Text = "OK", Left = 460, Top = 552, Width = 90, Height = 28 };
            ok.Click += OnOk;

            Button cancel = new Button
            {
                Text = "Cancel",
                Left = 558,
                Top = 552,
                Width = 90,
                Height = 28,
                DialogResult = DialogResult.Cancel
            };

            Controls.Add(tabs);
            Controls.Add(_validation);
            Controls.Add(ok);
            Controls.Add(cancel);
            CancelButton = cancel;

            LoadFromOptions();
        }

        // ---- tab construction ------------------------------------------------

        private TabPage BuildStandardTab()
        {
            TabPage page = new TabPage("Standard & sheet");
            Rows cursor = new Rows(page);

            _standard = cursor.Combo("Drafting standard", EnumNames(typeof(DrawingStandardKind)));
            _standard.SelectedIndexChanged += OnStandardChanged;

            _projection = cursor.Combo("Projection angle",
                new[] { "Standard default", "First angle", "Third angle" });

            _units = cursor.Combo("Units", new[] { "Millimetres", "Inches" });
            _units.SelectedIndexChanged += (s, e) => RefreshValidation();

            cursor.Separator();

            _sheetMode = cursor.Combo("Sheet selection",
                new[] { "Smallest that fits", "Fixed size", "Custom size" });
            _sheetMode.SelectedIndexChanged += (s, e) => { SyncEnabledState(); RefreshValidation(); };

            _sheetSize = cursor.Combo("Sheet size", new string[0]);
            _customWidth = cursor.Number("Custom width (mm)", 50, 3000, 0);
            _customHeight = cursor.Number("Custom height (mm)", 50, 3000, 0);

            cursor.Separator();

            _scaleMode = cursor.Combo("Scale", new[] { "Automatic", "Fixed" });
            _scaleMode.SelectedIndexChanged += (s, e) => { SyncEnabledState(); RefreshValidation(); };
            _scaleNumerator = cursor.Number("Fixed scale numerator", 1, 1000, 0);
            _scaleDenominator = cursor.Number("Fixed scale denominator", 1, 1000, 0);
            _maxEnlargement = cursor.Number("Largest enlargement (x:1)", 1, 100, 0);
            _maxReduction = cursor.Number("Largest reduction (1:x)", 1, 1000, 0);

            cursor.Separator();

            _templatePath = cursor.FilePath("Drawing template", "Drawing templates (*.drwdot)|*.drwdot|All files (*.*)|*.*");
            _sheetFormatPath = cursor.FilePath("Sheet format", "Sheet formats (*.slddrt)|*.slddrt|All files (*.*)|*.*");

            cursor.AddTo(page);
            return page;
        }

        private TabPage BuildViewsTab()
        {
            TabPage page = new TabPage("Views");
            Rows cursor = new Rows(page);

            _topView = cursor.Check("Top view");
            _sideView = cursor.Check("Side view");
            _isometric = cursor.Check("Isometric view");
            _sectionWhenInternal = cursor.Check("Section view when the part has internal features");
            _detailViews = cursor.Check("Detail view for features smaller than the threshold");
            _hiddenLines = cursor.Check("Show hidden lines on the principal view");
            _respectOrientation = cursor.Check("Keep the model's own front orientation where sensible");

            cursor.Separator();

            _smallFeature = cursor.Number("Small feature threshold (mm)", 0, 100, 1);
            _detailMultiplier = cursor.Number("Detail view magnification (x sheet scale)", 1, 20, 1);
            _viewGap = cursor.Number("Gap between views (mm)", 5, 200, 0);
            _annotationAllowance = cursor.Number("Sheet reserved for dimensions (%)", 0, 80, 0);

            cursor.Separator();

            _flatPattern = cursor.Check("Sheet metal: add a flat pattern view");
            _flatOnOwnSheet = cursor.Check("Sheet metal: put the flat pattern on its own sheet");
            _bendTable = cursor.Check("Sheet metal: add a bend table");
            _cutList = cursor.Check("Weldments: add the cut list");

            cursor.Separator();

            _assemblyDrawing = cursor.Check("Also draw the assembly itself");
            _bom = cursor.Check("Assembly: bill of materials");
            _balloons = cursor.Check("Assembly: balloon the components");
            _exploded = cursor.Check("Assembly: use the saved exploded view");

            cursor.AddTo(page);
            return page;
        }

        private TabPage BuildAnnotationsTab()
        {
            TabPage page = new TabPage("Dimensions & notes");
            Rows cursor = new Rows(page);

            _modelDimensions = cursor.Check("Insert model dimensions");
            _removeDuplicates = cursor.Check("Remove dimensions repeated on another view");
            _autoArrange = cursor.Check("Arrange dimensions after inserting");
            _centerMarks = cursor.Check("Centre marks on holes and arcs");
            _centerlines = cursor.Check("Centrelines");
            _holeCallouts = cursor.Check("Hole callouts");
            _precision = cursor.Number("Decimal places", 0, 6, 0);

            cursor.Separator();

            _generalNotes = cursor.Check("General note block");
            _projectionSymbol = cursor.Check("Projection symbol on the sheet");
            _extraNotes = cursor.MultiLine("Extra notes (one per line)", 70);

            cursor.Separator();

            _company = cursor.Text("Company");
            _drawnBy = cursor.Text("Drawn by");
            _revision = cursor.Text("Default revision");

            cursor.AddTo(page);
            return page;
        }

        private TabPage BuildOutputTab()
        {
            TabPage page = new TabPage("Output");
            Rows cursor = new Rows(page);

            _exportSlddrw = cursor.Check("Save the SOLIDWORKS drawing (.SLDDRW)");
            _exportPdf = cursor.Check("PDF");
            _exportDwg = cursor.Check("DWG");
            _exportDxf = cursor.Check("DXF");
            _exportFlatDxf = cursor.Check("Flat pattern DXF for sheet metal");
            _exportStep = cursor.Check("STEP of the model alongside the drawing");
            foreach (CheckBox box in new[] { _exportSlddrw, _exportPdf, _exportDwg, _exportDxf,
                                             _exportFlatDxf, _exportStep })
            {
                box.CheckedChanged += (s, e) => RefreshValidation();
            }

            cursor.Separator();

            _namePattern = cursor.Text("File name pattern");
            _namePattern.TextChanged += (s, e) => RefreshValidation();
            cursor.Hint("Tokens: {PartNumber} {ModelName} {Config} {Rev} {RevSuffix} {Date} {Project}");
            _outputFolder = cursor.FolderPath("Output folder (blank = beside the model)");
            _overwrite = cursor.Check("Overwrite files that already exist");

            cursor.Separator();

            _skipSuppressed = cursor.Check("Skip suppressed components");
            _skipHidden = cursor.Check("Skip hidden components");
            _skipEnvelopes = cursor.Check("Skip envelopes");
            _skipToolbox = cursor.Check("Skip Toolbox hardware");
            _skipExcludedFromBom = cursor.Check("Skip components excluded from the BOM");
            _configurationsSeparately = cursor.Check("One drawing per configuration");
            _maxParts = cursor.Number("Stop after this many parts", 1, 5000, 0);

            cursor.Separator();

            _runChecks = cursor.Check("Run the fabrication readiness check");
            _stopOnError = cursor.Check("Stop the whole run on the first failure");

            cursor.AddTo(page);
            return page;
        }

        // ---- loading and saving ---------------------------------------------

        private void LoadFromOptions()
        {
            _standard.SelectedIndex = (int)_options.Standard;
            _projection.SelectedIndex = _options.ProjectionOverride.HasValue
                ? (_options.ProjectionOverride.Value == ProjectionAngle.First ? 1 : 2)
                : 0;
            _units.SelectedIndex = _options.Units == UnitSystem.Inch ? 1 : 0;

            PopulateSheetSizes();

            _sheetMode.SelectedIndex = (int)_options.SheetMode;
            SelectByText(_sheetSize, _options.SheetSizeName);
            _customWidth.Value = Clamp(_customWidth, (decimal)_options.CustomSheetWidthMm);
            _customHeight.Value = Clamp(_customHeight, (decimal)_options.CustomSheetHeightMm);

            _scaleMode.SelectedIndex = (int)_options.ScaleMode;
            _scaleNumerator.Value = Clamp(_scaleNumerator, (decimal)_options.FixedScaleNumerator);
            _scaleDenominator.Value = Clamp(_scaleDenominator, (decimal)_options.FixedScaleDenominator);
            _maxEnlargement.Value = Clamp(_maxEnlargement, (decimal)_options.MaxEnlargement);
            _maxReduction.Value = Clamp(_maxReduction, (decimal)_options.MaxReduction);

            _templatePath.Text = _options.DrawingTemplatePath ?? string.Empty;
            _sheetFormatPath.Text = _options.SheetFormatPath ?? string.Empty;

            _topView.Checked = _options.IncludeTopView;
            _sideView.Checked = _options.IncludeSideView;
            _isometric.Checked = _options.IncludeIsometric;
            _sectionWhenInternal.Checked = _options.IncludeSectionWhenInternalFeatures;
            _detailViews.Checked = _options.IncludeDetailViewsForSmallFeatures;
            _hiddenLines.Checked = _options.ShowHiddenLinesOnPrincipalView;
            _respectOrientation.Checked = _options.RespectModelOrientation;

            _smallFeature.Value = Clamp(_smallFeature, (decimal)_options.SmallFeatureThresholdMm);
            _detailMultiplier.Value = Clamp(_detailMultiplier, (decimal)_options.DetailViewScaleMultiplier);
            _viewGap.Value = Clamp(_viewGap, (decimal)_options.ViewGapMm);
            _annotationAllowance.Value = Clamp(_annotationAllowance, (decimal)(_options.AnnotationAllowance * 100));

            _flatPattern.Checked = _options.SheetMetalFlatPattern;
            _flatOnOwnSheet.Checked = _options.FlatPatternOnSeparateSheet;
            _bendTable.Checked = _options.SheetMetalBendTable;
            _cutList.Checked = _options.WeldmentCutList;

            _assemblyDrawing.Checked = _options.GenerateAssemblyDrawing;
            _bom.Checked = _options.AssemblyBom;
            _balloons.Checked = _options.AssemblyBalloons;
            _exploded.Checked = _options.AssemblyExplodedView;

            _modelDimensions.Checked = _options.InsertModelDimensions;
            _removeDuplicates.Checked = _options.RemoveDuplicateDimensions;
            _autoArrange.Checked = _options.AutoArrangeDimensions;
            _centerMarks.Checked = _options.InsertCenterMarks;
            _centerlines.Checked = _options.InsertCenterlines;
            _holeCallouts.Checked = _options.InsertHoleCallouts;
            _precision.Value = Clamp(_precision, _options.DimensionPrecision);

            _generalNotes.Checked = _options.IncludeGeneralNotes;
            _projectionSymbol.Checked = _options.ShowProjectionSymbol;
            _extraNotes.Lines = _options.ExtraNotes != null
                ? _options.ExtraNotes.ToArray()
                : new string[0];

            _company.Text = _options.CompanyName ?? string.Empty;
            _drawnBy.Text = _options.DrawnBy ?? string.Empty;
            _revision.Text = _options.DefaultRevision ?? string.Empty;

            _exportSlddrw.Checked = (_options.ExportFormats & ExportFormats.SolidWorksDrawing) != 0;
            _exportPdf.Checked = (_options.ExportFormats & ExportFormats.Pdf) != 0;
            _exportDwg.Checked = (_options.ExportFormats & ExportFormats.Dwg) != 0;
            _exportDxf.Checked = (_options.ExportFormats & ExportFormats.Dxf) != 0;
            _exportFlatDxf.Checked = (_options.ExportFormats & ExportFormats.FlatPatternDxf) != 0;
            _exportStep.Checked = (_options.ExportFormats & ExportFormats.Step) != 0;

            _namePattern.Text = _options.FileNamePattern ?? string.Empty;
            _outputFolder.Text = _options.OutputFolder ?? string.Empty;
            _overwrite.Checked = _options.OverwriteExisting;

            _skipSuppressed.Checked = (_options.ComponentFilter & ComponentFilter.Suppressed) != 0;
            _skipHidden.Checked = (_options.ComponentFilter & ComponentFilter.Hidden) != 0;
            _skipEnvelopes.Checked = (_options.ComponentFilter & ComponentFilter.Envelopes) != 0;
            _skipToolbox.Checked = (_options.ComponentFilter & ComponentFilter.Toolbox) != 0;
            _skipExcludedFromBom.Checked = (_options.ComponentFilter & ComponentFilter.ExcludedFromBom) != 0;

            _configurationsSeparately.Checked = _options.TreatConfigurationsSeparately;
            _maxParts.Value = Clamp(_maxParts, _options.MaxPartsPerRun);
            _runChecks.Checked = _options.RunFabricationChecks;
            _stopOnError.Checked = _options.StopOnFirstError;

            SyncEnabledState();
            RefreshValidation();
        }

        private void StoreToOptions()
        {
            _options.Standard = (DrawingStandardKind)_standard.SelectedIndex;
            _options.ProjectionOverride =
                _projection.SelectedIndex == 1 ? ProjectionAngle.First :
                _projection.SelectedIndex == 2 ? ProjectionAngle.Third :
                (ProjectionAngle?)null;
            _options.Units = _units.SelectedIndex == 1 ? UnitSystem.Inch : UnitSystem.Millimeter;

            _options.SheetMode = (SheetSelectionMode)_sheetMode.SelectedIndex;
            _options.SheetSizeName = _sheetSize.SelectedItem as string ?? _options.SheetSizeName;
            _options.CustomSheetWidthMm = (double)_customWidth.Value;
            _options.CustomSheetHeightMm = (double)_customHeight.Value;

            _options.ScaleMode = (ScaleMode)_scaleMode.SelectedIndex;
            _options.FixedScaleNumerator = (double)_scaleNumerator.Value;
            _options.FixedScaleDenominator = (double)_scaleDenominator.Value;
            _options.MaxEnlargement = (double)_maxEnlargement.Value;
            _options.MaxReduction = (double)_maxReduction.Value;

            _options.DrawingTemplatePath = _templatePath.Text.Trim();
            _options.SheetFormatPath = _sheetFormatPath.Text.Trim();

            _options.IncludeTopView = _topView.Checked;
            _options.IncludeSideView = _sideView.Checked;
            _options.IncludeIsometric = _isometric.Checked;
            _options.IncludeSectionWhenInternalFeatures = _sectionWhenInternal.Checked;
            _options.IncludeDetailViewsForSmallFeatures = _detailViews.Checked;
            _options.ShowHiddenLinesOnPrincipalView = _hiddenLines.Checked;
            _options.RespectModelOrientation = _respectOrientation.Checked;

            _options.SmallFeatureThresholdMm = (double)_smallFeature.Value;
            _options.DetailViewScaleMultiplier = (double)_detailMultiplier.Value;
            _options.ViewGapMm = (double)_viewGap.Value;
            _options.AnnotationAllowance = (double)_annotationAllowance.Value / 100.0;

            _options.SheetMetalFlatPattern = _flatPattern.Checked;
            _options.FlatPatternOnSeparateSheet = _flatOnOwnSheet.Checked;
            _options.SheetMetalBendTable = _bendTable.Checked;
            _options.WeldmentCutList = _cutList.Checked;

            _options.GenerateAssemblyDrawing = _assemblyDrawing.Checked;
            _options.AssemblyBom = _bom.Checked;
            _options.AssemblyBalloons = _balloons.Checked;
            _options.AssemblyExplodedView = _exploded.Checked;

            _options.InsertModelDimensions = _modelDimensions.Checked;
            _options.RemoveDuplicateDimensions = _removeDuplicates.Checked;
            _options.AutoArrangeDimensions = _autoArrange.Checked;
            _options.InsertCenterMarks = _centerMarks.Checked;
            _options.InsertCenterlines = _centerlines.Checked;
            _options.InsertHoleCallouts = _holeCallouts.Checked;
            _options.DimensionPrecision = (int)_precision.Value;

            _options.IncludeGeneralNotes = _generalNotes.Checked;
            _options.ShowProjectionSymbol = _projectionSymbol.Checked;
            _options.ExtraNotes = new List<string>();
            foreach (string line in _extraNotes.Lines)
            {
                if (!string.IsNullOrEmpty(line.Trim())) _options.ExtraNotes.Add(line.Trim());
            }

            _options.CompanyName = _company.Text.Trim();
            _options.DrawnBy = _drawnBy.Text.Trim();
            _options.DefaultRevision = _revision.Text.Trim();

            ExportFormats formats = ExportFormats.None;
            if (_exportSlddrw.Checked) formats |= ExportFormats.SolidWorksDrawing;
            if (_exportPdf.Checked) formats |= ExportFormats.Pdf;
            if (_exportDwg.Checked) formats |= ExportFormats.Dwg;
            if (_exportDxf.Checked) formats |= ExportFormats.Dxf;
            if (_exportFlatDxf.Checked) formats |= ExportFormats.FlatPatternDxf;
            if (_exportStep.Checked) formats |= ExportFormats.Step;
            _options.ExportFormats = formats;

            _options.FileNamePattern = _namePattern.Text.Trim();
            _options.OutputFolder = _outputFolder.Text.Trim();
            _options.OverwriteExisting = _overwrite.Checked;

            ComponentFilter filter = ComponentFilter.None;
            if (_skipSuppressed.Checked) filter |= ComponentFilter.Suppressed;
            if (_skipHidden.Checked) filter |= ComponentFilter.Hidden;
            if (_skipEnvelopes.Checked) filter |= ComponentFilter.Envelopes;
            if (_skipToolbox.Checked) filter |= ComponentFilter.Toolbox;
            if (_skipExcludedFromBom.Checked) filter |= ComponentFilter.ExcludedFromBom;
            _options.ComponentFilter = filter;

            _options.TreatConfigurationsSeparately = _configurationsSeparately.Checked;
            _options.MaxPartsPerRun = (int)_maxParts.Value;
            _options.RunFabricationChecks = _runChecks.Checked;
            _options.StopOnFirstError = _stopOnError.Checked;
        }

        // ---- behaviour -------------------------------------------------------

        private void OnStandardChanged(object sender, EventArgs e)
        {
            PopulateSheetSizes();

            // Follow the standard's own conventions unless the user has already
            // overridden them.
            DrawingStandardKind kind = (DrawingStandardKind)_standard.SelectedIndex;
            StandardProfile profile = StandardProfile.For(kind, UnitSystem.Millimeter);

            if (_projection.SelectedIndex == 0)
            {
                _projection.Items[0] = "Standard default (" +
                    (profile.DefaultProjection == ProjectionAngle.Third ? "third" : "first") + " angle)";
                _projection.SelectedIndex = 0;
            }

            _units.SelectedIndex = profile.DefaultUnits == UnitSystem.Inch ? 1 : 0;
            RefreshValidation();
        }

        private void PopulateSheetSizes()
        {
            DrawingStandardKind kind = (DrawingStandardKind)Math.Max(0, _standard.SelectedIndex);
            string previous = _sheetSize.SelectedItem as string;

            _sheetSize.Items.Clear();
            foreach (SheetSize size in SheetCatalog.All)
            {
                if (SheetCatalog.UsesInchSeries(kind) != SheetCatalog.UsesInchSeries(size.Series)) continue;
                _sheetSize.Items.Add(size.Name);
            }

            if (previous != null && _sheetSize.Items.Contains(previous)) _sheetSize.SelectedItem = previous;
            else if (_sheetSize.Items.Count > 0)
            {
                SheetSize preferred = SheetCatalog.DefaultFor(kind);
                _sheetSize.SelectedItem = preferred != null && _sheetSize.Items.Contains(preferred.Name)
                    ? preferred.Name
                    : _sheetSize.Items[0];
            }
        }

        private void SyncEnabledState()
        {
            bool fixedSheet = _sheetMode.SelectedIndex == (int)SheetSelectionMode.Fixed;
            bool customSheet = _sheetMode.SelectedIndex == (int)SheetSelectionMode.Custom;
            _sheetSize.Enabled = fixedSheet;
            _customWidth.Enabled = customSheet;
            _customHeight.Enabled = customSheet;

            bool fixedScale = _scaleMode.SelectedIndex == (int)ScaleMode.Fixed;
            _scaleNumerator.Enabled = fixedScale;
            _scaleDenominator.Enabled = fixedScale;
            _maxEnlargement.Enabled = !fixedScale;
            _maxReduction.Enabled = !fixedScale;
        }

        private void RefreshValidation()
        {
            if (_validation == null) return;
            StoreToOptions();

            IList<string> problems = _options.Validate();
            _validation.Text = problems.Count == 0
                ? string.Empty
                : string.Join(Environment.NewLine, ToArray(problems));
        }

        private void OnOk(object sender, EventArgs e)
        {
            StoreToOptions();
            IList<string> problems = _options.Validate();

            if (problems.Count > 0)
            {
                MessageBox.Show("These would produce drawings nobody should issue:" +
                                Environment.NewLine + Environment.NewLine +
                                string.Join(Environment.NewLine, ToArray(problems)),
                                Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            DialogResult = DialogResult.OK;
            Close();
        }

        private static string[] ToArray(IList<string> items)
        {
            string[] array = new string[items.Count];
            items.CopyTo(array, 0);
            return array;
        }

        private static string[] EnumNames(Type enumType)
        {
            return Enum.GetNames(enumType);
        }

        private static void SelectByText(ComboBox combo, string text)
        {
            if (string.IsNullOrEmpty(text)) return;
            for (int i = 0; i < combo.Items.Count; i++)
            {
                if (string.Equals(combo.Items[i] as string, text, StringComparison.OrdinalIgnoreCase))
                {
                    combo.SelectedIndex = i;
                    return;
                }
            }
        }

        private static decimal Clamp(NumericUpDown control, decimal value)
        {
            if (value < control.Minimum) return control.Minimum;
            if (value > control.Maximum) return control.Maximum;
            return value;
        }

        /// <summary>
        /// Lays controls out top to bottom on a scrollable panel, so adding an
        /// option is one line rather than a round of pixel arithmetic.
        /// </summary>
        private sealed class Rows
        {
            private const int LabelWidth = 250;
            private const int ControlLeft = 266;
            private const int ControlWidth = 320;
            private const int RowHeight = 26;

            private readonly Panel _panel;
            private int _y = 8;

            public Rows(TabPage page)
            {
                _panel = new Panel
                {
                    Dock = DockStyle.Fill,
                    AutoScroll = true
                };
                page.Controls.Add(_panel);
            }

            public void AddTo(TabPage page)
            {
                // The panel is already docked; this keeps the call sites
                // symmetrical and gives a place to hang future work.
                if (!page.Controls.Contains(_panel)) page.Controls.Add(_panel);
            }

            private Label Caption(string text)
            {
                Label label = new Label
                {
                    Left = 10,
                    Top = _y + 3,
                    Width = LabelWidth,
                    Height = 18,
                    Text = text
                };
                _panel.Controls.Add(label);
                return label;
            }

            public ComboBox Combo(string caption, string[] items)
            {
                Caption(caption);
                ComboBox combo = new ComboBox
                {
                    Left = ControlLeft,
                    Top = _y,
                    Width = ControlWidth,
                    DropDownStyle = ComboBoxStyle.DropDownList
                };
                combo.Items.AddRange(items);
                _panel.Controls.Add(combo);
                _y += RowHeight;
                return combo;
            }

            public NumericUpDown Number(string caption, decimal minimum, decimal maximum, int decimals)
            {
                Caption(caption);
                NumericUpDown number = new NumericUpDown
                {
                    Left = ControlLeft,
                    Top = _y,
                    Width = 100,
                    Minimum = minimum,
                    Maximum = maximum,
                    DecimalPlaces = decimals,
                    Increment = decimals > 0 ? 0.5m : 1m
                };
                _panel.Controls.Add(number);
                _y += RowHeight;
                return number;
            }

            public CheckBox Check(string caption)
            {
                CheckBox box = new CheckBox
                {
                    Left = 12,
                    Top = _y,
                    Width = 560,
                    Height = 20,
                    Text = caption
                };
                _panel.Controls.Add(box);
                _y += 24;
                return box;
            }

            public TextBox Text(string caption)
            {
                Caption(caption);
                TextBox box = new TextBox
                {
                    Left = ControlLeft,
                    Top = _y,
                    Width = ControlWidth
                };
                _panel.Controls.Add(box);
                _y += RowHeight;
                return box;
            }

            public TextBox MultiLine(string caption, int height)
            {
                Caption(caption);
                TextBox box = new TextBox
                {
                    Left = ControlLeft,
                    Top = _y,
                    Width = ControlWidth,
                    Height = height,
                    Multiline = true,
                    ScrollBars = ScrollBars.Vertical
                };
                _panel.Controls.Add(box);
                _y += height + 6;
                return box;
            }

            public TextBox FilePath(string caption, string filter)
            {
                Caption(caption);
                TextBox box = new TextBox
                {
                    Left = ControlLeft,
                    Top = _y,
                    Width = ControlWidth - 60
                };
                Button browse = new Button
                {
                    Left = ControlLeft + ControlWidth - 56,
                    Top = _y - 1,
                    Width = 56,
                    Height = 23,
                    Text = "..."
                };
                browse.Click += (s, e) =>
                {
                    using (OpenFileDialog dialog = new OpenFileDialog())
                    {
                        dialog.Filter = filter;
                        if (dialog.ShowDialog() == DialogResult.OK) box.Text = dialog.FileName;
                    }
                };
                _panel.Controls.Add(box);
                _panel.Controls.Add(browse);
                _y += RowHeight;
                return box;
            }

            public TextBox FolderPath(string caption)
            {
                Caption(caption);
                TextBox box = new TextBox
                {
                    Left = ControlLeft,
                    Top = _y,
                    Width = ControlWidth - 60
                };
                Button browse = new Button
                {
                    Left = ControlLeft + ControlWidth - 56,
                    Top = _y - 1,
                    Width = 56,
                    Height = 23,
                    Text = "..."
                };
                browse.Click += (s, e) =>
                {
                    using (FolderBrowserDialog dialog = new FolderBrowserDialog())
                    {
                        if (dialog.ShowDialog() == DialogResult.OK) box.Text = dialog.SelectedPath;
                    }
                };
                _panel.Controls.Add(box);
                _panel.Controls.Add(browse);
                _y += RowHeight;
                return box;
            }

            public void Hint(string text)
            {
                Label label = new Label
                {
                    Left = ControlLeft,
                    Top = _y,
                    Width = ControlWidth,
                    Height = 16,
                    ForeColor = SystemColors.GrayText,
                    Font = new Font(SystemFonts.DefaultFont.FontFamily, 7.5f),
                    Text = text
                };
                _panel.Controls.Add(label);
                _y += 18;
            }

            public void Separator()
            {
                Label line = new Label
                {
                    Left = 10,
                    Top = _y + 4,
                    Width = 576,
                    Height = 2,
                    BorderStyle = BorderStyle.Fixed3D
                };
                _panel.Controls.Add(line);
                _y += 14;
            }
        }
    }
}
