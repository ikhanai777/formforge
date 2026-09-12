using System;
using System.IO;
using System.Xml;
using System.Xml.Serialization;

namespace DrawingForge.Core.Options
{
    /// <summary>
    /// Loads and saves <see cref="DrawingOptions"/> as XML.
    /// </summary>
    /// <remarks>
    /// XmlSerializer rather than a JSON package: this assembly is loaded into
    /// the SOLIDWORKS process, where an extra dependency is a version conflict
    /// waiting to happen, and XmlSerializer is in the framework everywhere the
    /// add-in can run.
    ///
    /// A corrupt or unreadable settings file never stops a run. It falls back to
    /// defaults and reports why, because losing a preference is an annoyance and
    /// failing to open the dialog is a blocker.
    /// </remarks>
    public static class OptionsStore
    {
        public const string DefaultFileName = "DrawingForge.settings.xml";

        /// <summary>Per-user settings path under %APPDATA%.</summary>
        public static string DefaultPath
        {
            get
            {
                string appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
                return Path.Combine(Path.Combine(appData, "DrawingForge"), DefaultFileName);
            }
        }

        /// <summary>
        /// Reads options from <paramref name="path"/>, or returns defaults when
        /// the file is missing or unreadable.
        /// </summary>
        public static DrawingOptions Load(string path, out string warning)
        {
            warning = null;
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
                return new DrawingOptions();

            try
            {
                using (FileStream stream = File.OpenRead(path))
                {
                    XmlSerializer serializer = new XmlSerializer(typeof(DrawingOptions));
                    DrawingOptions loaded = serializer.Deserialize(stream) as DrawingOptions;
                    if (loaded == null)
                    {
                        warning = "Settings file was empty; defaults are in use.";
                        return new DrawingOptions();
                    }
                    return loaded;
                }
            }
            catch (InvalidOperationException ex)
            {
                warning = "Settings file could not be read (" + Innermost(ex).Message + "); defaults are in use.";
                return new DrawingOptions();
            }
            catch (IOException ex)
            {
                warning = "Settings file could not be opened (" + ex.Message + "); defaults are in use.";
                return new DrawingOptions();
            }
            catch (UnauthorizedAccessException ex)
            {
                warning = "Settings file could not be opened (" + ex.Message + "); defaults are in use.";
                return new DrawingOptions();
            }
        }

        /// <summary>Convenience overload that discards the warning.</summary>
        public static DrawingOptions Load(string path)
        {
            string ignored;
            return Load(path, out ignored);
        }

        /// <summary>
        /// Writes options to <paramref name="path"/>, creating the folder if
        /// needed. Writes to a temporary file first so an interrupted save
        /// cannot leave a half-written settings file behind.
        /// </summary>
        public static void Save(DrawingOptions options, string path)
        {
            if (options == null) throw new ArgumentNullException("options");
            if (string.IsNullOrEmpty(path)) throw new ArgumentException("Path is empty.", "path");

            string folder = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(folder) && !Directory.Exists(folder))
                Directory.CreateDirectory(folder);

            string temp = path + ".tmp";
            XmlWriterSettings settings = new XmlWriterSettings
            {
                Indent = true,
                IndentChars = "  ",
                Encoding = new System.Text.UTF8Encoding(false)
            };

            using (XmlWriter writer = XmlWriter.Create(temp, settings))
            {
                XmlSerializer serializer = new XmlSerializer(typeof(DrawingOptions));
                serializer.Serialize(writer, options);
            }

            if (File.Exists(path)) File.Delete(path);
            File.Move(temp, path);
        }

        /// <summary>Round-trips through XML; used by the tests and by "reset to saved".</summary>
        public static DrawingOptions Clone(DrawingOptions options)
        {
            if (options == null) throw new ArgumentNullException("options");
            using (MemoryStream buffer = new MemoryStream())
            {
                XmlSerializer serializer = new XmlSerializer(typeof(DrawingOptions));
                serializer.Serialize(buffer, options);
                buffer.Position = 0;
                return (DrawingOptions)serializer.Deserialize(buffer);
            }
        }

        private static Exception Innermost(Exception ex)
        {
            while (ex.InnerException != null) ex = ex.InnerException;
            return ex;
        }
    }
}
