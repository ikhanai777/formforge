using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using DrawingForge.AddIn.Interop;
using DrawingForge.Core.Logging;
using DrawingForge.Core.Options;
using DrawingForge.Core.Reporting;
using SolidWorks.Interop.sldworks;

namespace DrawingForge.AddIn.Build
{
    /// <summary>
    /// Writes the drawing out in whichever formats the run asked for.
    /// </summary>
    /// <remarks>
    /// The .SLDDRW is the master. The PDF is what gets emailed; the DWG/DXF is
    /// what a laser or waterjet programmer opens. A flat-pattern DXF is exported
    /// from the model rather than the drawing, because a nesting program wants
    /// the outline and the bend lines, not a sheet with a title block on it.
    /// </remarks>
    internal sealed class Exporter
    {
        private readonly ISldWorks _app;
        private readonly DrawingOptions _options;
        private readonly ILog _log;

        public Exporter(ISldWorks app, DrawingOptions options, ILog log)
        {
            _app = app;
            _options = options;
            _log = log;
        }

        /// <summary>
        /// Saves the drawing and every requested derivative. Paths written are
        /// appended to <paramref name="outcome"/>.
        /// </summary>
        public void Export(ModelDoc2 drawingModel, string drawingPath, ModelDoc2 sourceModel,
                           DrawingOutcome outcome)
        {
            if (drawingModel == null || string.IsNullOrEmpty(drawingPath)) return;

            string folder = Path.GetDirectoryName(drawingPath);
            if (!string.IsNullOrEmpty(folder) && !Directory.Exists(folder))
            {
                Directory.CreateDirectory(folder);
                _log.Info("Created output folder " + folder + ".");
            }

            if ((_options.ExportFormats & ExportFormats.SolidWorksDrawing) != 0)
            {
                if (SaveAs(drawingModel, drawingPath)) outcome.ExportedFiles.Add(drawingPath);
                else outcome.Errors.Add("Could not save " + drawingPath + ".");
            }

            if ((_options.ExportFormats & ExportFormats.Pdf) != 0)
                ExportSimple(drawingModel, drawingPath, ".PDF", outcome);

            if ((_options.ExportFormats & ExportFormats.Dwg) != 0)
                ExportSimple(drawingModel, drawingPath, ".DWG", outcome);

            if ((_options.ExportFormats & ExportFormats.Dxf) != 0)
                ExportSimple(drawingModel, drawingPath, ".DXF", outcome);

            if ((_options.ExportFormats & ExportFormats.Step) != 0 && sourceModel != null)
            {
                string stepPath = Path.ChangeExtension(drawingPath, ".STEP");
                if (SaveAs(sourceModel, stepPath)) outcome.ExportedFiles.Add(stepPath);
            }

            if ((_options.ExportFormats & ExportFormats.FlatPatternDxf) != 0 && sourceModel != null)
                ExportFlatPatternDxf(sourceModel, drawingPath, outcome);
        }

        private void ExportSimple(ModelDoc2 drawingModel, string drawingPath, string extension,
                                  DrawingOutcome outcome)
        {
            string target = Path.ChangeExtension(drawingPath, extension);

            if (File.Exists(target) && !_options.OverwriteExisting)
            {
                outcome.Warnings.Add("Kept the existing " + Path.GetFileName(target) +
                                     "; switch overwrite on to replace it.");
                return;
            }

            if (SaveAs(drawingModel, target)) outcome.ExportedFiles.Add(target);
            else outcome.Errors.Add("Could not export " + Path.GetFileName(target) + ".");
        }

        /// <summary>
        /// Saves a document to a path, letting the extension decide the format.
        /// </summary>
        public bool SaveAs(ModelDoc2 doc, string path)
        {
            if (doc == null || string.IsNullOrEmpty(path)) return false;

            if (File.Exists(path) && !_options.OverwriteExisting)
            {
                _log.Warn("Not overwriting the existing " + Path.GetFileName(path) + ".");
                return false;
            }

            int errors = 0;
            int warnings = 0;

            try
            {
                bool saved = doc.Extension.SaveAs(path, SwConst.swSaveAsCurrentVersion,
                                                  SwConst.swSaveAsOptions_Silent,
                                                  null, ref errors, ref warnings);
                if (saved)
                {
                    _log.Info("Wrote " + path);
                    return true;
                }

                _log.Warn(string.Format(CultureInfo.InvariantCulture,
                    "SaveAs failed for {0} (errors 0x{1:X}, warnings 0x{2:X}).", path, errors, warnings));
                return false;
            }
            catch (System.Runtime.InteropServices.COMException ex)
            {
                _log.Error("SaveAs threw for " + path, ex);
                return false;
            }
        }

        /// <summary>
        /// Exports the flat pattern of a sheet metal part straight to DXF.
        /// </summary>
        /// <remarks>
        /// ExportToDWG2 takes an action code and a set of flags controlling which
        /// entities come out. The combination used here exports the flat pattern
        /// with bend lines — what a nesting program needs — and leaves out the
        /// sketches and forming tool marks that would otherwise be cut.
        /// </remarks>
        public void ExportFlatPatternDxf(ModelDoc2 partModel, string drawingPath, DrawingOutcome outcome)
        {
            string target = Path.Combine(
                Path.GetDirectoryName(drawingPath) ?? string.Empty,
                Path.GetFileNameWithoutExtension(drawingPath) + "_FLAT.DXF");

            if (File.Exists(target) && !_options.OverwriteExisting)
            {
                outcome.Warnings.Add("Kept the existing " + Path.GetFileName(target) + ".");
                return;
            }

            // action 1 = export the flat pattern; the flags request geometry plus
            // bend lines, without sketches or library features.
            const int ActionExportFlatPattern = 1;
            const int FlagsGeometryAndBendLines = 1;

            object result;
            object[][] shapes =
            {
                new object[] { target, partModel.GetPathName(), ActionExportFlatPattern,
                               true, null, false, false, FlagsGeometryAndBendLines, null },
                new object[] { target, partModel.GetPathName(), ActionExportFlatPattern,
                               true, null, false, false, FlagsGeometryAndBendLines },
            };

            if (SwDispatch.TryInvokeAny(partModel, new[] { "ExportToDWG2", "ExportToDWG" },
                                        shapes, out result, _log) &&
                result != null && Convert.ToBoolean(result, CultureInfo.InvariantCulture))
            {
                outcome.ExportedFiles.Add(target);
                _log.Info("Wrote flat pattern DXF " + target);
                return;
            }

            outcome.Warnings.Add("Flat pattern DXF export failed; the part may have no flat pattern.");
        }

        /// <summary>
        /// Makes a path unique by appending a counter, so a run never silently
        /// overwrites a drawing it wrote earlier in the same batch.
        /// </summary>
        public static string EnsureUnique(string path, ICollection<string> alreadyWritten)
        {
            if (string.IsNullOrEmpty(path)) return path;
            if (alreadyWritten == null) return path;

            string folder = Path.GetDirectoryName(path) ?? string.Empty;
            string stem = Path.GetFileNameWithoutExtension(path);
            string extension = Path.GetExtension(path);

            string candidate = path;
            int counter = 2;
            while (Contains(alreadyWritten, candidate))
            {
                candidate = Path.Combine(folder,
                    string.Format(CultureInfo.InvariantCulture, "{0}_{1}{2}", stem, counter, extension));
                counter++;
            }

            alreadyWritten.Add(candidate);
            return candidate;
        }

        private static bool Contains(IEnumerable<string> items, string value)
        {
            foreach (string item in items)
            {
                if (string.Equals(item, value, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }
    }
}
