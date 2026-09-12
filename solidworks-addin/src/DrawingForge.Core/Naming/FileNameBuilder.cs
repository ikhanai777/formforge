using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;

namespace DrawingForge.Core.Naming
{
    /// <summary>
    /// Turns a name pattern into a file name that Windows, a PDM vault and a
    /// shop folder will all accept.
    /// </summary>
    public static class FileNameBuilder
    {
        /// <summary>Characters Windows forbids in a file name, plus a few a vault dislikes.</summary>
        private static readonly char[] Illegal =
            ("<>:\"/\\|?*" + new string(new[] { '\0', '\t', '\n', '\r' })).ToCharArray();

        /// <summary>Longest base name produced; leaves room for a path and extension.</summary>
        public const int MaxBaseNameLength = 120;

        /// <summary>
        /// Expands the pattern for a part. Tokens: {PartNumber} {ModelName}
        /// {Config} {Rev} {RevSuffix} {Date} {Project} {Standard}.
        /// {RevSuffix} expands to "_REV_A" when a revision exists and to nothing
        /// when it does not, so one pattern works for both.
        /// </summary>
        public static string Build(string pattern, PartSummary part, DrawingOptions options)
        {
            if (string.IsNullOrEmpty(pattern)) pattern = "{PartNumber}";

            string rev = part != null && !string.IsNullOrEmpty(part.Revision)
                ? part.Revision
                : (options != null ? options.DefaultRevision : string.Empty);

            string project = string.Empty;
            if (part != null) part.CustomProperties.TryGetValue("Project", out project);

            StringBuilder sb = new StringBuilder(pattern);
            Replace(sb, "{PartNumber}", part != null ? part.PartNumber : "PART");
            Replace(sb, "{ModelName}", part != null ? part.ModelName : "MODEL");
            Replace(sb, "{Config}", part != null ? part.Configuration : string.Empty);
            Replace(sb, "{Rev}", rev);
            Replace(sb, "{RevSuffix}", string.IsNullOrEmpty(rev) ? string.Empty : "_REV_" + rev);
            Replace(sb, "{Date}", DateTime.Now.ToString("yyyyMMdd", CultureInfo.InvariantCulture));
            Replace(sb, "{Project}", project ?? string.Empty);
            Replace(sb, "{Standard}", options != null ? options.Standard.ToString().ToUpperInvariant() : string.Empty);

            return Sanitize(sb.ToString());
        }

        /// <summary>
        /// Strips illegal characters, collapses whitespace, trims to length and
        /// refuses to return an empty or reserved name.
        /// </summary>
        public static string Sanitize(string name)
        {
            if (string.IsNullOrEmpty(name)) return "DRAWING";

            StringBuilder sb = new StringBuilder(name.Length);
            foreach (char c in name)
            {
                if (Array.IndexOf(Illegal, c) >= 0) { sb.Append('_'); continue; }
                if (char.IsControl(c)) continue;
                sb.Append(c);
            }

            string result = sb.ToString();
            while (result.IndexOf("  ", StringComparison.Ordinal) >= 0)
                result = result.Replace("  ", " ");
            result = result.Trim().Trim('.');

            if (result.Length > MaxBaseNameLength)
                result = result.Substring(0, MaxBaseNameLength).Trim();

            if (result.Length == 0) return "DRAWING";
            if (IsReservedDeviceName(result)) return "_" + result;
            return result;
        }

        /// <summary>
        /// Adds "_2", "_3" until the name is not in <paramref name="taken"/>.
        /// Case-insensitive, because the file system is.
        /// </summary>
        public static string MakeUnique(string baseName, ICollection<string> taken)
        {
            if (taken == null) return baseName;

            if (!ContainsIgnoreCase(taken, baseName))
            {
                taken.Add(baseName);
                return baseName;
            }

            for (int i = 2; i < 10000; i++)
            {
                string candidate = string.Format(CultureInfo.InvariantCulture, "{0}_{1}", baseName, i);
                if (!ContainsIgnoreCase(taken, candidate))
                {
                    taken.Add(candidate);
                    return candidate;
                }
            }

            string fallback = baseName + "_" + Guid.NewGuid().ToString("N").Substring(0, 6);
            taken.Add(fallback);
            return fallback;
        }

        private static bool ContainsIgnoreCase(IEnumerable<string> items, string value)
        {
            foreach (string item in items)
            {
                if (string.Equals(item, value, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        private static readonly string[] ReservedNames =
        {
            "CON", "PRN", "AUX", "NUL",
            "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
            "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
        };

        private static bool IsReservedDeviceName(string name)
        {
            int dot = name.IndexOf('.');
            string stem = dot >= 0 ? name.Substring(0, dot) : name;
            foreach (string reserved in ReservedNames)
            {
                if (string.Equals(stem, reserved, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        private static void Replace(StringBuilder sb, string token, string value)
        {
            string current = sb.ToString();
            int index = current.IndexOf(token, StringComparison.OrdinalIgnoreCase);
            while (index >= 0)
            {
                sb.Remove(index, token.Length);
                sb.Insert(index, value ?? string.Empty);
                current = sb.ToString();
                index = current.IndexOf(token, StringComparison.OrdinalIgnoreCase);
            }
        }
    }
}
