using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using DrawingForge.Core.Logging;
using DrawingForge.Core.Options;
using DrawingForge.AddIn.Interop;
using SolidWorks.Interop.sldworks;

namespace DrawingForge.AddIn.Build
{
    /// <summary>One model that needs a drawing.</summary>
    internal sealed class DrawingTask
    {
        public string FilePath { get; set; }
        public string Configuration { get; set; }
        public string DisplayName { get; set; }
        public bool IsAssembly { get; set; }

        /// <summary>How many times it appears in the assembly.</summary>
        public int Quantity { get; set; }

        /// <summary>Depth in the assembly tree; 0 is the top-level model itself.</summary>
        public int Depth { get; set; }

        public DrawingTask()
        {
            Quantity = 1;
            Configuration = string.Empty;
        }

        /// <summary>Key used to collapse repeated instances into one drawing.</summary>
        public string Key(bool configurationsAreSeparate)
        {
            string path = (FilePath ?? string.Empty).ToUpperInvariant();
            if (!configurationsAreSeparate) return path;
            return path + "|" + (Configuration ?? string.Empty).ToUpperInvariant();
        }

        public override string ToString()
        {
            return string.Format(CultureInfo.InvariantCulture, "{0} [{1}] x{2}",
                DisplayName, Configuration, Quantity);
        }
    }

    /// <summary>
    /// Walks an assembly and works out which models need a drawing.
    /// </summary>
    /// <remarks>
    /// Repeated instances collapse into one drawing — a bolt used forty times
    /// is one part — and by default the walk drops the things nobody details:
    /// suppressed components, envelopes, and Toolbox hardware that is bought
    /// rather than made.
    /// </remarks>
    internal static class AssemblyTraverser
    {
        /// <summary>Path fragments that mark a component as bought-in library hardware.</summary>
        private static readonly string[] ToolboxMarkers =
        {
            @"\TOOLBOX\", @"\SOLIDWORKS DATA\BROWSER\", @"\DESIGN LIBRARY\TOOLBOX\"
        };

        /// <summary>
        /// Every model under <paramref name="root"/> that should get a drawing,
        /// in traversal order, deduplicated.
        /// </summary>
        public static IList<DrawingTask> Collect(ModelDoc2 root, DrawingOptions options, ILog log)
        {
            List<DrawingTask> tasks = new List<DrawingTask>();
            if (root == null) return tasks;

            Dictionary<string, DrawingTask> seen =
                new Dictionary<string, DrawingTask>(StringComparer.OrdinalIgnoreCase);

            bool rootIsAssembly = SwDoc.IsAssembly(root, log);

            if (!rootIsAssembly)
            {
                DrawingTask single = new DrawingTask
                {
                    FilePath = root.GetPathName(),
                    Configuration = ActiveConfigurationName(root, log),
                    DisplayName = Path.GetFileNameWithoutExtension(root.GetPathName() ?? root.GetTitle()),
                    IsAssembly = false,
                    Depth = 0
                };
                tasks.Add(single);
                return tasks;
            }

            AssemblyDoc assembly = root as AssemblyDoc;
            if (assembly == null)
            {
                log.Warn("The active document reports as an assembly but does not expose IAssemblyDoc.");
                return tasks;
            }

            if (options.GenerateAssemblyDrawing)
            {
                tasks.Add(new DrawingTask
                {
                    FilePath = root.GetPathName(),
                    Configuration = ActiveConfigurationName(root, log),
                    DisplayName = Path.GetFileNameWithoutExtension(root.GetPathName() ?? root.GetTitle()),
                    IsAssembly = true,
                    Depth = 0
                });
            }

            object componentsObject;
            if (!SwDispatch.TryInvoke(assembly, "GetComponents", new object[] { false },
                                      out componentsObject, log))
            {
                log.Error("Could not read the component list from the assembly.");
                return tasks;
            }

            object[] components = SwDispatch.AsObjects(componentsObject);
            log.Info(string.Format(CultureInfo.InvariantCulture,
                "Assembly contains {0} component instance(s).", components.Length));

            int dropped = 0;
            foreach (object componentObject in components)
            {
                Component2 component = componentObject as Component2;
                if (component == null) continue;

                string skipReason;
                if (ShouldSkip(component, options, out skipReason))
                {
                    dropped++;
                    log.Debug("Skipped " + SafeName(component) + ": " + skipReason);
                    continue;
                }

                string path = component.GetPathName();
                if (string.IsNullOrEmpty(path)) { dropped++; continue; }

                bool isAssembly = SwDoc.TypeFromExtension(path) == SwConst.swDocASSEMBLY;
                if (isAssembly && !options.GenerateAssemblyDrawing)
                {
                    // Sub-assemblies still need walking through; they just do not
                    // get a drawing of their own.
                    continue;
                }

                DrawingTask task = new DrawingTask
                {
                    FilePath = path,
                    Configuration = component.ReferencedConfiguration ?? string.Empty,
                    DisplayName = Path.GetFileNameWithoutExtension(path),
                    IsAssembly = isAssembly,
                    Depth = DepthOf(component)
                };

                string key = task.Key(options.TreatConfigurationsSeparately);
                DrawingTask existing;
                if (seen.TryGetValue(key, out existing))
                {
                    existing.Quantity++;
                    continue;
                }

                seen[key] = task;
                tasks.Add(task);

                if (tasks.Count >= options.MaxPartsPerRun)
                {
                    log.Warn(string.Format(CultureInfo.InvariantCulture,
                        "Stopped at the {0} part limit. Raise it in the options if the whole assembly is wanted.",
                        options.MaxPartsPerRun));
                    break;
                }
            }

            log.Info(string.Format(CultureInfo.InvariantCulture,
                "{0} unique model(s) to draw; {1} instance(s) filtered out.", tasks.Count, dropped));
            return tasks;
        }

        /// <summary>Whether the filter options exclude this component, and why.</summary>
        public static bool ShouldSkip(Component2 component, DrawingOptions options, out string reason)
        {
            reason = null;
            if (component == null) { reason = "null component"; return true; }

            if ((options.ComponentFilter & ComponentFilter.Suppressed) != 0)
            {
                object suppression;
                if (SwDispatch.TryInvoke(component, "GetSuppression", new object[0], out suppression) &&
                    suppression != null)
                {
                    try
                    {
                        if (Convert.ToInt32(suppression, CultureInfo.InvariantCulture) == SwConst.swComponentSuppressed)
                        {
                            reason = "suppressed";
                            return true;
                        }
                    }
                    catch (InvalidCastException) { }
                    catch (FormatException) { }
                }
            }

            if ((options.ComponentFilter & ComponentFilter.Hidden) != 0)
            {
                if (SwDispatch.InvokeBool(component, new[] { "IsHidden" }, new object[] { true }, false))
                {
                    reason = "hidden";
                    return true;
                }
            }

            if ((options.ComponentFilter & ComponentFilter.Envelopes) != 0)
            {
                if (SwDispatch.InvokeBool(component, new[] { "IsEnvelope" }, new object[0], false))
                {
                    reason = "envelope";
                    return true;
                }
            }

            if ((options.ComponentFilter & ComponentFilter.Virtual) != 0)
            {
                if (SwDispatch.GetBool(component, "IsVirtual", false))
                {
                    reason = "virtual component";
                    return true;
                }
            }

            if ((options.ComponentFilter & ComponentFilter.ExcludedFromBom) != 0)
            {
                if (SwDispatch.GetBool(component, "ExcludeFromBOM", false))
                {
                    reason = "excluded from the BOM";
                    return true;
                }
            }

            if ((options.ComponentFilter & ComponentFilter.Toolbox) != 0 && IsToolbox(component))
            {
                reason = "Toolbox hardware";
                return true;
            }

            return false;
        }

        /// <summary>
        /// Whether a component is library hardware.
        /// </summary>
        /// <remarks>
        /// There is no single API flag for this that works across installs, so
        /// this checks the path against the usual Toolbox locations. A shop that
        /// keeps its library somewhere else will need the Toolbox filter turned
        /// off; the alternative — detailing every washer in the assembly — is
        /// worse than the occasional false negative.
        /// </remarks>
        public static bool IsToolbox(Component2 component)
        {
            if (component == null) return false;

            if (SwDispatch.GetBool(component, "IsToolboxComponent", false)) return true;

            string path = component.GetPathName();
            if (string.IsNullOrEmpty(path)) return false;

            string upper = path.ToUpperInvariant();
            foreach (string marker in ToolboxMarkers)
            {
                if (upper.IndexOf(marker, StringComparison.Ordinal) >= 0) return true;
            }
            return false;
        }

        private static int DepthOf(Component2 component)
        {
            string name = SafeName(component);
            if (string.IsNullOrEmpty(name)) return 1;

            int depth = 1;
            foreach (char c in name)
            {
                if (c == '/') depth++;
            }
            return depth;
        }

        private static string SafeName(Component2 component)
        {
            if (component == null) return string.Empty;
            object name;
            if (SwDispatch.TryGet(component, "Name2", out name) && name is string) return (string)name;
            try { return component.Name2 ?? string.Empty; }
            catch (System.Runtime.InteropServices.COMException) { return string.Empty; }
        }

        /// <summary>Name of the document's active configuration.</summary>
        public static string ActiveConfigurationName(ModelDoc2 doc, ILog log = null)
        {
            if (doc == null) return string.Empty;

            object configuration;
            if (SwDispatch.TryGet(doc, "ConfigurationManager", out configuration, log) && configuration != null)
            {
                object active;
                if (SwDispatch.TryGet(configuration, "ActiveConfiguration", out active, log) && active != null)
                {
                    object name;
                    if (SwDispatch.TryGet(active, "Name", out name, log) && name is string) return (string)name;
                }
            }

            object fallback;
            if (SwDispatch.TryInvoke(doc, "GetActiveConfiguration", new object[0], out fallback, log))
            {
                object name;
                if (SwDispatch.TryGet(fallback, "Name", out name, log) && name is string) return (string)name;
            }

            return string.Empty;
        }
    }
}
