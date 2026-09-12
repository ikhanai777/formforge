using System;
using System.Globalization;
using System.IO;
using DrawingForge.AddIn.Interop;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Logging;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;
using DrawingForge.Core.Standards;
using SolidWorks.Interop.sldworks;

namespace DrawingForge.AddIn.Build
{
    /// <summary>
    /// Creates the drawing document and puts each sheet into the right size,
    /// scale and projection convention before any view goes on it.
    /// </summary>
    /// <remarks>
    /// Order matters here. The projection convention has to be set on the sheet
    /// before projected views are created, because SOLIDWORKS decides which
    /// orthographic view a projection produces from the sheet's setting
    /// combined with where the view is dropped. Setting it afterwards would
    /// leave first-angle views laid out to a third-angle plan, which is the one
    /// mistake on a drawing that gets parts made backwards.
    /// </remarks>
    internal sealed class SheetSetup
    {
        private readonly ISldWorks _app;
        private readonly DrawingOptions _options;
        private readonly ILog _log;

        public SheetSetup(ISldWorks app, DrawingOptions options, ILog log)
        {
            _app = app;
            _options = options;
            _log = log;
        }

        /// <summary>
        /// Creates an empty drawing sized for the plan's first sheet. Returns
        /// null with a reason when the template cannot be resolved.
        /// </summary>
        public ModelDoc2 CreateDrawing(DrawingPlan plan, out string failure)
        {
            failure = null;

            string template = ResolveTemplate(plan.TemplatePath);
            if (string.IsNullOrEmpty(template))
            {
                failure = "No drawing template. Set one in the options or configure a default in SOLIDWORKS.";
                return null;
            }

            SheetSize first = plan.Sheets.Count > 0 ? plan.Sheets[0].Size : SheetCatalog.DefaultFor(plan.Standard);

            object created;
            object[] args =
            {
                template,
                first.SwPaperSize,
                Units.MmToMeter(first.WidthMm),
                Units.MmToMeter(first.HeightMm)
            };

            if (!SwDispatch.TryInvoke(_app, "NewDocument", args, out created, _log) || created == null)
            {
                failure = "NewDocument failed for template " + template + ".";
                return null;
            }

            ModelDoc2 doc = created as ModelDoc2;
            if (doc == null)
            {
                failure = "NewDocument returned something that is not a drawing.";
                return null;
            }

            _log.Info(string.Format(CultureInfo.InvariantCulture,
                "New drawing from {0} on a {1} sheet.", Path.GetFileName(template), first.Name));
            return doc;
        }

        /// <summary>
        /// The template to start from: the explicit one, else the SOLIDWORKS
        /// default for drawings.
        /// </summary>
        public string ResolveTemplate(string preferred)
        {
            if (!string.IsNullOrEmpty(preferred) && File.Exists(preferred)) return preferred;

            if (!string.IsNullOrEmpty(preferred))
                _log.Warn("Drawing template not found at " + preferred + "; falling back to the SOLIDWORKS default.");

            try
            {
                string fallback = _app.GetUserPreferenceStringValue(SwConst.swDefaultTemplateDrawing);
                if (!string.IsNullOrEmpty(fallback) && File.Exists(fallback)) return fallback;
                if (!string.IsNullOrEmpty(fallback)) return fallback;
            }
            catch (System.Runtime.InteropServices.COMException ex)
            {
                _log.Warn("Could not read the default drawing template: " + LogExtensions.Describe(ex));
            }

            return null;
        }

        /// <summary>
        /// Applies size, scale and projection to the sheet that is currently
        /// active. Returns false when SOLIDWORKS rejected the setup.
        /// </summary>
        public bool ApplySheetProperties(DrawingDoc drawing, SheetPlan plan)
        {
            if (drawing == null || plan == null) return false;

            bool firstAngle = plan.Projection == ProjectionAngle.First;
            double scale1 = plan.Scale != null ? plan.Scale.Numerator : 1.0;
            double scale2 = plan.Scale != null ? plan.Scale.Denominator : 1.0;

            string sheetFormat = plan.SheetFormatPath ?? string.Empty;
            int templateIn = string.IsNullOrEmpty(sheetFormat)
                ? SwConst.swDwgTemplateNone
                : SwConst.swDwgTemplateCustom;

            object result;
            object[][] setupShapes =
            {
                // name, paperSize, templateIn, scale1, scale2, firstAngle,
                // templateName, width, height, customPropertyView, updateProperties
                new object[] { plan.Name, plan.Size.SwPaperSize, templateIn, scale1, scale2, firstAngle,
                               sheetFormat, Units.MmToMeter(plan.Size.WidthMm),
                               Units.MmToMeter(plan.Size.HeightMm), string.Empty, true },
                new object[] { plan.Name, plan.Size.SwPaperSize, templateIn, scale1, scale2, firstAngle,
                               sheetFormat, Units.MmToMeter(plan.Size.WidthMm),
                               Units.MmToMeter(plan.Size.HeightMm), string.Empty },
                new object[] { plan.Name, plan.Size.SwPaperSize, templateIn, scale1, scale2, firstAngle,
                               sheetFormat, Units.MmToMeter(plan.Size.WidthMm),
                               Units.MmToMeter(plan.Size.HeightMm) },
            };

            if (SwDispatch.TryInvokeAny(drawing, new[] { "SetupSheet6", "SetupSheet5", "SetupSheet4" },
                                        setupShapes, out result, _log))
            {
                _log.Info(string.Format(CultureInfo.InvariantCulture,
                    "Sheet {0}: {1}, {2}, {3} angle.",
                    plan.Name, plan.Size.Name, plan.Scale, firstAngle ? "first" : "third"));
                return true;
            }

            // Older releases only expose this through the sheet object itself.
            object sheetObject;
            if (SwDispatch.TryInvoke(drawing, "GetCurrentSheet", new object[0], out sheetObject, _log) &&
                sheetObject != null)
            {
                object[][] propertyShapes =
                {
                    new object[] { plan.Size.SwPaperSize, templateIn, scale1, scale2, firstAngle,
                                   Units.MmToMeter(plan.Size.WidthMm), Units.MmToMeter(plan.Size.HeightMm) },
                };

                if (SwDispatch.TryInvokeAny(sheetObject, new[] { "SetProperties2", "SetProperties" },
                                            propertyShapes, out result, _log))
                {
                    return true;
                }
            }

            _log.Error("Could not apply sheet properties to " + plan.Name +
                       ". The projection convention may not match the plan — check the sheet before issuing.");
            return false;
        }

        /// <summary>Adds a sheet for a multi-sheet plan and makes it current.</summary>
        public bool AddSheet(DrawingDoc drawing, SheetPlan plan)
        {
            bool firstAngle = plan.Projection == ProjectionAngle.First;
            double scale1 = plan.Scale != null ? plan.Scale.Numerator : 1.0;
            double scale2 = plan.Scale != null ? plan.Scale.Denominator : 1.0;
            string sheetFormat = plan.SheetFormatPath ?? string.Empty;
            int templateIn = string.IsNullOrEmpty(sheetFormat)
                ? SwConst.swDwgTemplateNone
                : SwConst.swDwgTemplateCustom;

            object result;
            object[][] shapes =
            {
                new object[] { plan.Name, plan.Size.SwPaperSize, templateIn, scale1, scale2, firstAngle,
                               sheetFormat, Units.MmToMeter(plan.Size.WidthMm),
                               Units.MmToMeter(plan.Size.HeightMm), string.Empty, true },
                new object[] { plan.Name, plan.Size.SwPaperSize, templateIn, scale1, scale2, firstAngle,
                               sheetFormat, Units.MmToMeter(plan.Size.WidthMm),
                               Units.MmToMeter(plan.Size.HeightMm), string.Empty },
                new object[] { plan.Name, plan.Size.SwPaperSize, templateIn, scale1, scale2, firstAngle,
                               sheetFormat, Units.MmToMeter(plan.Size.WidthMm),
                               Units.MmToMeter(plan.Size.HeightMm) },
            };

            if (!SwDispatch.TryInvokeAny(drawing, new[] { "NewSheet4", "NewSheet3", "NewSheet2" },
                                         shapes, out result, _log))
            {
                _log.Error("Could not add sheet " + plan.Name + ".");
                return false;
            }

            object ignored;
            SwDispatch.TryInvoke(drawing, "ActivateSheet", new object[] { plan.Name }, out ignored, _log);
            return true;
        }

        /// <summary>
        /// Stamps the drafting standard and unit onto the drawing document.
        /// </summary>
        /// <remarks>
        /// This is what makes arrowheads, thread conventions and decimal
        /// separators match the standard the sheet claims to follow. Getting it
        /// wrong produces a drawing that says ISO in the notes and draws ANSI
        /// symbols, which is worse than either on its own.
        /// </remarks>
        public void ApplyDocumentStandards(ModelDoc2 drawingModel, DrawingPlan plan)
        {
            if (drawingModel == null) return;

            StandardProfile profile = _options.Profile();
            SetInteger(drawingModel, SwConst.swDetailingDimensionStandard, profile.SwDetailingStandard,
                       "drafting standard");

            int unit = _options.Units == UnitSystem.Inch ? SwConst.swINCHES : SwConst.swMM;
            SetInteger(drawingModel, SwConst.swUnitsLinear, unit, "linear unit");
            SetInteger(drawingModel, SwConst.swUnitsLinearDecimalPlaces, _options.DimensionPrecision,
                       "dimension precision");
        }

        private void SetInteger(ModelDoc2 doc, int preference, int value, string what)
        {
            object result;
            if (SwDispatch.TryInvoke(doc.Extension, "SetUserPreferenceInteger",
                                     new object[] { preference, 0, value }, out result, _log))
            {
                return;
            }

            _log.Warn("Could not set the " + what + " on the drawing; it keeps the template's setting.");
        }
    }
}
