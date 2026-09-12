using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using DrawingForge.Core.Logging;
using SolidWorks.Interop.sldworks;

namespace DrawingForge.AddIn.Interop
{
    /// <summary>
    /// Document-level helpers: opening, closing, identifying and reading
    /// properties off a SOLIDWORKS document.
    /// </summary>
    internal static class SwDoc
    {
        /// <summary>
        /// Document type as <c>swDocumentTypes_e</c>.
        /// </summary>
        /// <remarks>
        /// Goes through late binding on purpose: <c>IModelDoc2.GetType()</c>
        /// collides with <see cref="object.GetType()"/>, and which one a direct
        /// call resolves to depends on the static type of the variable. That is
        /// exactly the kind of silent wrong answer that is worth one reflection
        /// call to avoid.
        /// </remarks>
        public static int TypeOf(ModelDoc2 doc, ILog log = null)
        {
            if (doc == null) return SwConst.swDocNONE;
            object result;
            if (SwDispatch.TryInvoke(doc, "GetType", new object[0], out result, log) && result != null)
            {
                try { return Convert.ToInt32(result, CultureInfo.InvariantCulture); }
                catch (InvalidCastException) { }
                catch (FormatException) { }
            }

            // Fall back to the extension, which is unambiguous.
            string path = doc.GetPathName();
            return TypeFromExtension(path);
        }

        public static int TypeFromExtension(string path)
        {
            if (string.IsNullOrEmpty(path)) return SwConst.swDocNONE;
            string extension = Path.GetExtension(path).ToUpperInvariant();
            switch (extension)
            {
                case ".SLDPRT": return SwConst.swDocPART;
                case ".SLDASM": return SwConst.swDocASSEMBLY;
                case ".SLDDRW": return SwConst.swDocDRAWING;
                default: return SwConst.swDocNONE;
            }
        }

        public static bool IsPart(ModelDoc2 doc, ILog log = null) { return TypeOf(doc, log) == SwConst.swDocPART; }
        public static bool IsAssembly(ModelDoc2 doc, ILog log = null) { return TypeOf(doc, log) == SwConst.swDocASSEMBLY; }
        public static bool IsDrawing(ModelDoc2 doc, ILog log = null) { return TypeOf(doc, log) == SwConst.swDocDRAWING; }

        /// <summary>
        /// Opens a model read-only and silently, returning null and a reason on
        /// failure rather than throwing into the middle of a batch.
        /// </summary>
        public static ModelDoc2 Open(ISldWorks app, string path, string configuration, out string failure,
                                     ILog log = null)
        {
            failure = null;
            if (app == null) { failure = "No SOLIDWORKS session."; return null; }
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
            {
                failure = "File not found: " + (path ?? "(null)");
                return null;
            }

            int docType = TypeFromExtension(path);
            if (docType == SwConst.swDocNONE)
            {
                failure = "Not a SOLIDWORKS model: " + path;
                return null;
            }

            int errors = 0;
            int warnings = 0;
            int options = SwConst.swOpenDocOptions_Silent;

            ModelDoc2 doc = app.OpenDoc6(path, docType, options, configuration ?? string.Empty,
                                         ref errors, ref warnings) as ModelDoc2;

            if (doc == null)
            {
                failure = string.Format(CultureInfo.InvariantCulture,
                    "OpenDoc6 failed for {0} (errors 0x{1:X}, warnings 0x{2:X}).", path, errors, warnings);
                log.Warn(failure);
                return null;
            }

            if (warnings != 0)
            {
                log.Debug(string.Format(CultureInfo.InvariantCulture,
                    "{0} opened with warnings 0x{1:X}.", Path.GetFileName(path), warnings));
            }

            return doc;
        }

        /// <summary>Brings a document to the front so view and annotation calls act on it.</summary>
        public static ModelDoc2 Activate(ISldWorks app, ModelDoc2 doc, ILog log = null)
        {
            if (app == null || doc == null) return doc;
            try
            {
                int errors = 0;
                object activated = app.ActivateDoc3(doc.GetTitle(), true, 0, ref errors);
                if (activated != null) return activated as ModelDoc2;
                log.Debug("ActivateDoc3 returned nothing for " + doc.GetTitle() + "; carrying on with the original handle.");
            }
            catch (System.Runtime.InteropServices.COMException ex)
            {
                log.Warn("Activating " + doc.GetTitle() + " failed: " + LogExtensions.Describe(ex));
            }
            return doc;
        }

        /// <summary>Closes a document by title, swallowing the usual COM noise.</summary>
        public static void Close(ISldWorks app, string title, ILog log = null)
        {
            if (app == null || string.IsNullOrEmpty(title)) return;
            try { app.CloseDoc(title); }
            catch (System.Runtime.InteropServices.COMException ex)
            {
                log.Debug("Closing " + title + " failed: " + LogExtensions.Describe(ex));
            }
        }

        /// <summary>Forces a rebuild; returns false when SOLIDWORKS reports failure.</summary>
        public static bool Rebuild(ModelDoc2 doc, ILog log = null)
        {
            if (doc == null) return false;
            object result;
            if (SwDispatch.TryInvoke(doc, new[] { "ForceRebuild3", "EditRebuild3" },
                                     new object[] { false }, out result, log))
            {
                return result == null || Convert.ToBoolean(result, CultureInfo.InvariantCulture);
            }
            if (SwDispatch.TryInvoke(doc, "EditRebuild3", new object[0], out result, log)) return true;
            return false;
        }

        /// <summary>
        /// All custom properties of a document, with the configuration-specific
        /// ones layered over the document-level ones.
        /// </summary>
        public static IDictionary<string, string> ReadCustomProperties(ModelDoc2 doc, string configuration,
                                                                       ILog log = null)
        {
            Dictionary<string, string> values =
                new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            if (doc == null) return values;

            ReadInto(values, doc, string.Empty, log);
            if (!string.IsNullOrEmpty(configuration)) ReadInto(values, doc, configuration, log);
            return values;
        }

        private static void ReadInto(IDictionary<string, string> values, ModelDoc2 doc, string configuration,
                                     ILog log)
        {
            object manager = PropertyManager(doc, configuration, log);
            if (manager == null) return;

            object namesObject;
            if (!SwDispatch.TryGet(manager, "GetNames", out namesObject, log))
            {
                SwDispatch.TryInvoke(manager, "GetNames", new object[0], out namesObject, log);
            }

            object[] names = SwDispatch.AsObjects(namesObject);
            foreach (object nameObject in names)
            {
                string name = nameObject as string;
                if (string.IsNullOrEmpty(name)) continue;

                string resolved = ResolveProperty(manager, name, log);
                if (resolved != null) values[name] = resolved;
            }
        }

        /// <summary>
        /// One property's evaluated value — the value a title block would show,
        /// not the raw "$PRP:..." expression.
        /// </summary>
        /// <remarks>
        /// ICustomPropertyManager.Get has six generations of overload, each
        /// adding an out parameter. Reflection writes by-ref arguments back into
        /// the array it was handed, so the ladder below works for all of them
        /// without binding this add-in to one SOLIDWORKS release's signature.
        /// </remarks>
        public static string ResolveProperty(object manager, string name, ILog log = null)
        {
            if (manager == null || string.IsNullOrEmpty(name)) return null;

            // name, useCached, out value, out resolved, [out wasResolved, [out linkToProperty]]
            PropertyGetShape[] shapes =
            {
                new PropertyGetShape("Get6", new object[] { name, false, null, null, null, null }, 2, 3),
                new PropertyGetShape("Get5", new object[] { name, false, null, null, null }, 2, 3),
                new PropertyGetShape("Get4", new object[] { name, false, null, null }, 2, 3),
                new PropertyGetShape("Get3", new object[] { name, false, null, null }, 2, 3),
                new PropertyGetShape("Get2", new object[] { name, null, null }, 1, 2),
            };

            foreach (PropertyGetShape shape in shapes)
            {
                object ignored;
                if (!SwDispatch.TryInvoke(manager, shape.Method, shape.Args, out ignored, log)) continue;

                string resolved = shape.Args[shape.ResolvedIndex] as string;
                string raw = shape.Args[shape.ValueIndex] as string;
                string value = !string.IsNullOrEmpty(resolved) ? resolved : raw;
                if (value != null) return value;
            }

            // Oldest form: returns the raw value directly.
            object direct;
            if (SwDispatch.TryInvoke(manager, "Get", new object[] { name }, out direct, log))
                return direct as string;

            return null;
        }

        private struct PropertyGetShape
        {
            public readonly string Method;
            public readonly object[] Args;
            public readonly int ValueIndex;
            public readonly int ResolvedIndex;

            public PropertyGetShape(string method, object[] args, int valueIndex, int resolvedIndex)
            {
                Method = method;
                Args = args;
                ValueIndex = valueIndex;
                ResolvedIndex = resolvedIndex;
            }
        }

        /// <summary>
        /// The property manager for a configuration; an empty configuration name
        /// means the document's own properties.
        /// </summary>
        public static object PropertyManager(ModelDoc2 doc, string configuration, ILog log = null)
        {
            if (doc == null) return null;

            object manager;
            if (SwDispatch.TryGetIndexed(doc.Extension, "CustomPropertyManager",
                                         new object[] { configuration ?? string.Empty }, out manager, log))
            {
                return manager;
            }

            log.Debug("No property manager for configuration '" + configuration + "'.");
            return null;
        }

        /// <summary>Writes a custom property, replacing any existing value.</summary>
        public static bool WriteProperty(ModelDoc2 doc, string configuration, string name, string value,
                                         ILog log = null)
        {
            if (doc == null || string.IsNullOrEmpty(name)) return false;

            object manager = PropertyManager(doc, configuration, log);
            if (manager == null) return false;

            object result;
            object[] add3Args = { name, SwConst.swCustomInfoText, value ?? string.Empty,
                                  SwConst.swCustomPropertyDeleteAndAdd };
            if (SwDispatch.TryInvoke(manager, "Add3", add3Args, out result, log)) return true;

            object[] add2Args = { name, SwConst.swCustomInfoText, value ?? string.Empty, true };
            if (SwDispatch.TryInvoke(manager, "Add2", add2Args, out result, log)) return true;

            log.Warn("Could not write custom property '" + name + "'.");
            return false;
        }

        /// <summary>Mass in grams; 0 when SOLIDWORKS cannot work it out.</summary>
        public static double MassGrams(ModelDoc2 doc, ILog log = null)
        {
            if (doc == null) return 0;

            object massPropertyObject;
            if (SwDispatch.TryInvoke(doc.Extension, new[] { "CreateMassProperty2", "CreateMassProperty" },
                                     new object[0], out massPropertyObject, log) && massPropertyObject != null)
            {
                object mass;
                if (SwDispatch.TryGet(massPropertyObject, "Mass", out mass, log) && mass != null)
                {
                    try
                    {
                        // SOLIDWORKS reports mass in kilograms.
                        return Convert.ToDouble(mass, CultureInfo.InvariantCulture) * 1000.0;
                    }
                    catch (InvalidCastException) { }
                    catch (FormatException) { }
                }
                SwDispatch.Release(massPropertyObject);
            }

            log.Debug("Mass properties are unavailable for " + doc.GetTitle() + ".");
            return 0;
        }

        /// <summary>Material name set on the part, or an empty string.</summary>
        public static string MaterialName(ModelDoc2 doc, string configuration, ILog log = null)
        {
            if (doc == null) return string.Empty;

            // GetMaterialPropertyName2 takes the database name as an out
            // parameter; reflection fills it in the args array.
            object[] args = { configuration ?? string.Empty, null };
            object result;
            if (SwDispatch.TryInvoke(doc, "GetMaterialPropertyName2", args, out result, log))
                return (result as string) ?? string.Empty;

            object[] legacyArgs = { configuration ?? string.Empty };
            if (SwDispatch.TryInvoke(doc, "GetMaterialPropertyName", legacyArgs, out result, log))
                return (result as string) ?? string.Empty;

            log.Debug("No material is set on " + doc.GetTitle() + ".");
            return string.Empty;
        }

        /// <summary>Clears the selection without caring whether anything was selected.</summary>
        public static void ClearSelection(ModelDoc2 doc)
        {
            if (doc == null) return;
            try { doc.ClearSelection2(true); }
            catch (System.Runtime.InteropServices.COMException) { }
        }

        /// <summary>Selects a named object; returns false when the name is not there.</summary>
        public static bool Select(ModelDoc2 doc, string name, string type, bool append = false, int mark = 0,
                                  ILog log = null)
        {
            if (doc == null || string.IsNullOrEmpty(name)) return false;
            try
            {
                return doc.Extension.SelectByID2(name, type, 0, 0, 0, append, mark, null, 0);
            }
            catch (System.Runtime.InteropServices.COMException ex)
            {
                log.Debug("Selecting " + name + " (" + type + ") failed: " + LogExtensions.Describe(ex));
                return false;
            }
        }
    }
}
