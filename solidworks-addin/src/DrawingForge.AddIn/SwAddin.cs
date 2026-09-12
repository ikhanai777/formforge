using System;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using DrawingForge.AddIn.Build;
using DrawingForge.AddIn.Interop;
using DrawingForge.AddIn.Registration;
using DrawingForge.AddIn.Ui;
using DrawingForge.Core.Logging;
using DrawingForge.Core.Options;
using DrawingForge.Core.Reporting;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swpublished;

namespace DrawingForge.AddIn
{
    /// <summary>
    /// The SOLIDWORKS add-in entry point.
    /// </summary>
    /// <remarks>
    /// SOLIDWORKS loads this class by CLSID, calls <see cref="ConnectToSW"/>
    /// once, and from then on drives everything through the command callbacks
    /// named as strings in <see cref="AddCommands"/>. Renaming one of those
    /// callback methods without changing the string leaves a button that does
    /// nothing, so the names are declared as constants and used in both places.
    /// </remarks>
    [Guid(AddInGuid)]
    [ComVisible(true)]
    [ClassInterface(ClassInterfaceType.None)]
    [ProgId("DrawingForge.AddIn")]
    public class DrawingForgeAddIn : ISwAddin
    {
        public const string AddInGuid = "7C0E2B5A-7B4F-4E2E-9D3B-6E1A2F5C8D41";
        public const string AddInTitle = "DrawingForge";
        public const string AddInDescription =
            "Generates fabrication-ready drawings for every part in an assembly.";

        private const int CommandGroupId = 4721;
        private const int CommandIdGenerate = 0;
        private const int CommandIdOptions = 1;
        private const int CommandIdActiveOnly = 2;

        // Callback names SOLIDWORKS resolves by string.
        private const string CallbackGenerate = "OnGenerateForAssembly";
        private const string CallbackOptions = "OnShowOptions";
        private const string CallbackActiveOnly = "OnGenerateForActiveDocument";
        private const string CallbackEnableWhenModelOpen = "EnableWhenModelOpen";

        private ISldWorks _app;
        private ICommandManager _commandManager;
        private int _cookie;
        private BufferedLog _log;
        private DrawingOptions _options;
        private string _settingsPath;

        // ---- ISwAddin --------------------------------------------------------

        public bool ConnectToSW(object thisSw, int cookie)
        {
            _app = thisSw as ISldWorks;
            _cookie = cookie;

            if (_app == null) return false;

            _settingsPath = OptionsStore.DefaultPath;
            string warning;
            _options = OptionsStore.Load(_settingsPath, out warning);

            _log = new BufferedLog(LogPath(), LogLevel.Info);
            _log.Info(AddInTitle + " connected.");
            if (!string.IsNullOrEmpty(warning)) _log.Warn(warning);

            // Without this, SOLIDWORKS cannot find the callback methods.
            _app.SetAddinCallbackInfo(0, this, _cookie);

            try
            {
                _commandManager = _app.GetCommandManager(_cookie);
                AddCommands();
            }
            catch (COMException ex)
            {
                _log.Error("Could not build the command group", ex);
                // A missing toolbar is not a reason to refuse to load: the
                // add-in is still reachable from the Tools menu entry that
                // SOLIDWORKS creates for every add-in.
            }

            return true;
        }

        public bool DisconnectFromSW()
        {
            try
            {
                if (_commandManager != null) _commandManager.RemoveCommandGroup(CommandGroupId);
            }
            catch (COMException ex)
            {
                if (_log != null) _log.Error("Removing the command group failed", ex);
            }

            SwDispatch.Release(_commandManager);
            _commandManager = null;

            SwDispatch.Release(_app);
            _app = null;

            GC.Collect();
            GC.WaitForPendingFinalizers();
            return true;
        }

        // ---- commands --------------------------------------------------------

        private void AddCommands()
        {
            int errors = 0;
            ICommandGroup group = _commandManager.CreateCommandGroup2(
                CommandGroupId, AddInTitle, AddInDescription, AddInTitle, -1, true, ref errors);

            if (group == null)
            {
                _log.Error("CreateCommandGroup2 returned nothing (error code " +
                           errors.ToString(CultureInfo.InvariantCulture) + ").");
                return;
            }

            group.AddCommandItem2("Generate Drawings...", -1,
                                  "Draw every part in the active assembly", "Generate drawings",
                                  0, CallbackGenerate, CallbackEnableWhenModelOpen, CommandIdGenerate, 3);

            group.AddCommandItem2("Draw Active Document", -1,
                                  "Draw only the document that is open", "Draw this document",
                                  1, CallbackActiveOnly, CallbackEnableWhenModelOpen, CommandIdActiveOnly, 3);

            group.AddCommandItem2("Options...", -1,
                                  "Standard, sheet size, projection and output settings", "DrawingForge options",
                                  2, CallbackOptions, string.Empty, CommandIdOptions, 3);

            group.HasToolbar = true;
            group.HasMenu = true;
            group.Activate();
        }

        /// <summary>Enables a command only when a model is open. 0 = off, 1 = on.</summary>
        public int EnableWhenModelOpen()
        {
            try
            {
                ModelDoc2 active = _app != null ? _app.ActiveDoc as ModelDoc2 : null;
                if (active == null) return 0;
                return SwDoc.IsDrawing(active, _log) ? 0 : 1;
            }
            catch (COMException)
            {
                return 0;
            }
        }

        public void OnShowOptions()
        {
            try
            {
                using (OptionsDialog dialog = new OptionsDialog(_options))
                {
                    if (dialog.ShowDialog() != DialogResult.OK) return;
                    _options = dialog.Options;
                    OptionsStore.Save(_options, _settingsPath);
                    _log.Info("Options saved to " + _settingsPath + ".");
                }
            }
            catch (IOException ex)
            {
                Report("Could not save the settings file", ex);
            }
            catch (UnauthorizedAccessException ex)
            {
                Report("Could not save the settings file", ex);
            }
        }

        public void OnGenerateForAssembly()
        {
            Generate(assemblyWide: true);
        }

        public void OnGenerateForActiveDocument()
        {
            Generate(assemblyWide: false);
        }

        private void Generate(bool assemblyWide)
        {
            ModelDoc2 active = null;
            try
            {
                active = _app.ActiveDoc as ModelDoc2;
            }
            catch (COMException ex)
            {
                Report("Could not reach the active document", ex);
                return;
            }

            if (active == null)
            {
                MessageBox.Show("Open a part or assembly first.", AddInTitle,
                                MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            if (SwDoc.IsDrawing(active, _log))
            {
                MessageBox.Show("This works on a part or an assembly, not on a drawing.", AddInTitle,
                                MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            DrawingOptions runOptions = OptionsStore.Clone(_options);
            if (!assemblyWide)
            {
                // "Draw this document" means exactly that: no walking into the
                // components, no assembly drawing unless this is the assembly.
                runOptions.MaxPartsPerRun = 1;
                if (SwDoc.IsAssembly(active, _log)) runOptions.GenerateAssemblyDrawing = true;
            }

            using (ProgressDialog progress = new ProgressDialog(AddInTitle))
            {
                BufferedLog runLog = new BufferedLog(LogPath(), LogLevel.Info);
                CompositeLog log = new CompositeLog(runLog,
                    new CallbackLog((level, message) => progress.Append(level, message)));

                BatchRunner runner = new BatchRunner(_app, runOptions, log);
                progress.Cancelled += (sender, args) => runner.Cancel();

                BatchReport report = null;
                progress.Run(() =>
                {
                    report = runner.Run(active, p => progress.Update(p.Percent, p.Message));
                });

                if (report == null) return;

                WriteReport(report, runOptions);
                using (ResultsDialog results = new ResultsDialog(report, runLog))
                {
                    results.ShowDialog();
                }
            }
        }

        /// <summary>Drops the run report next to the drawings so it survives the session.</summary>
        private void WriteReport(BatchReport report, DrawingOptions options)
        {
            try
            {
                string folder = !string.IsNullOrEmpty(options.OutputFolder)
                    ? options.OutputFolder
                    : Path.GetDirectoryName(report.AssemblyPath ?? string.Empty);

                if (string.IsNullOrEmpty(folder) || !Directory.Exists(folder)) return;

                string path = Path.Combine(folder,
                    "DrawingForge-report-" + DateTime.Now.ToString("yyyyMMdd-HHmmss", CultureInfo.InvariantCulture) + ".json");
                File.WriteAllText(path, report.ToJson());
                _log.Info("Run report written to " + path + ".");
            }
            catch (IOException ex)
            {
                _log.Error("Could not write the run report", ex);
            }
            catch (UnauthorizedAccessException ex)
            {
                _log.Error("Could not write the run report", ex);
            }
        }

        private void Report(string message, Exception ex)
        {
            if (_log != null) _log.Error(message, ex);
            MessageBox.Show(message + ":\n\n" + LogExtensions.Describe(ex), AddInTitle,
                            MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }

        private static string LogPath()
        {
            string appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            return Path.Combine(Path.Combine(appData, "DrawingForge"),
                                "DrawingForge-" + DateTime.Now.ToString("yyyyMMdd", CultureInfo.InvariantCulture) + ".log");
        }

        // ---- COM registration ------------------------------------------------

        /// <summary>
        /// Registers the add-in with SOLIDWORKS. Called by regasm.
        /// </summary>
        /// <remarks>
        /// SOLIDWORKS finds add-ins through two keys: one under HKLM that
        /// describes the add-in, and one under HKCU that says whether this user
        /// wants it loaded at startup. Writing the registry directly rather than
        /// using the SDK's attribute-driven helper keeps the add-in free of the
        /// SDK assembly, which has itself moved between SOLIDWORKS releases.
        /// </remarks>
        [ComRegisterFunction]
        public static void RegisterFunction(Type type)
        {
            ComRegistration.Register(type, AddInTitle, AddInDescription);
        }

        [ComUnregisterFunction]
        public static void UnregisterFunction(Type type)
        {
            ComRegistration.Unregister(type);
        }
    }
}
