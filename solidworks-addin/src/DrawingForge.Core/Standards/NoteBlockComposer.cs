using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using DrawingForge.Core.Options;
using DrawingForge.Core.Planning;

namespace DrawingForge.Core.Standards
{
    /// <summary>
    /// Builds the general note block — the paragraph that turns a dimensioned
    /// picture into a fabrication instruction.
    /// </summary>
    /// <remarks>
    /// A shop reads these notes before the views. They carry the things no
    /// dimension can say: which tolerancing standard applies, what the unit is,
    /// what the untoleranced dimensions are allowed to vary by, what the edges
    /// and surfaces must look like, and what the part is made of. A drawing
    /// without them is not releasable, whatever its views look like.
    ///
    /// Notes that would be empty are dropped rather than emitted blank, and the
    /// numbering is applied after that, so there are never gaps.
    /// </remarks>
    public static class NoteBlockComposer
    {
        /// <summary>Heading above the numbered notes.</summary>
        public const string Heading = "NOTES:  UNLESS OTHERWISE SPECIFIED";

        /// <summary>
        /// The numbered notes for one part, already substituted and numbered.
        /// </summary>
        public static IList<string> Compose(PartSummary part, DrawingOptions options)
        {
            if (options == null) throw new ArgumentNullException("options");
            StandardProfile profile = options.Profile();

            List<string> notes = new List<string>();

            notes.Add(string.Format(CultureInfo.InvariantCulture,
                "INTERPRET DRAWING PER {0}.", profile.DimensioningSpec));

            notes.Add(string.Format(CultureInfo.InvariantCulture,
                "DIMENSIONS ARE IN {0}.",
                options.Units == UnitSystem.Inch ? "INCHES" : "MILLIMETRES"));

            notes.Add(string.Format(CultureInfo.InvariantCulture,
                "PROJECTION: {0} ANGLE PER {1}.",
                options.EffectiveProjection() == ProjectionAngle.Third ? "THIRD" : "FIRST",
                profile.ProjectionSpec));

            notes.Add(profile.GeneralToleranceNote);
            notes.Add(profile.EdgeBreakNote);
            notes.Add(profile.SurfaceFinishNote);

            string material = part != null ? Trim(part.Material) : null;
            notes.Add(string.IsNullOrEmpty(material)
                ? "MATERIAL: SEE TITLE BLOCK."
                : "MATERIAL: " + material.ToUpperInvariant() + ".");

            string finish = part != null ? Trim(part.Finish) : null;
            if (!string.IsNullOrEmpty(finish))
                notes.Add("FINISH: " + finish.ToUpperInvariant() + ".");

            if (part != null && part.IsSheetMetal)
            {
                notes.Add(SheetMetalNote(part, options));
                notes.Add("FLAT PATTERN DIMENSIONS ARE FOR REFERENCE. FORM TO THE DIMENSIONS SHOWN ON THE FORMED VIEWS.");
            }

            if (part != null && part.IsWeldment)
            {
                notes.Add("WELD PER AWS D1.1 UNLESS OTHERWISE SPECIFIED. GRIND ALL EXPOSED WELDS FLUSH.");
                notes.Add("DIMENSIONS APPLY AFTER WELDING AND STRAIGHTENING.");
            }

            if (part != null && HasTappedHoles(part))
                notes.Add("TAPPED HOLES TO BE FREE OF CHIPS AND BURRS. THREADS TO GAUGE.");

            notes.Add("PART TO BE FREE OF OIL, GREASE, CHIPS AND FOREIGN MATTER.");

            notes.Add(string.Format(CultureInfo.InvariantCulture,
                "MARK PART NUMBER {0} AND REVISION IN APPROXIMATE LOCATION SHOWN, {1} CHARACTERS.",
                part != null ? part.PartNumber : "AS SHOWN",
                options.Units == UnitSystem.Inch ? ".12 HIGH" : "3 MM HIGH"));

            if (options.ExtraNotes != null)
            {
                foreach (string extra in options.ExtraNotes)
                {
                    string expanded = Expand(extra, part, options);
                    if (!string.IsNullOrEmpty(expanded)) notes.Add(expanded);
                }
            }

            return Number(notes);
        }

        /// <summary>The note block as one string, heading included.</summary>
        public static string ComposeText(PartSummary part, DrawingOptions options)
        {
            StringBuilder sb = new StringBuilder();
            sb.AppendLine(Heading);
            foreach (string note in Compose(part, options))
            {
                sb.AppendLine(note);
            }
            return sb.ToString().TrimEnd();
        }

        /// <summary>Numbers a list of notes "1. ", "2. ", dropping blanks first.</summary>
        public static IList<string> Number(IEnumerable<string> notes)
        {
            List<string> result = new List<string>();
            int index = 1;
            foreach (string note in notes)
            {
                string trimmed = Trim(note);
                if (string.IsNullOrEmpty(trimmed)) continue;
                result.Add(string.Format(CultureInfo.InvariantCulture, "{0}. {1}", index, trimmed));
                index++;
            }
            return result;
        }

        private static string SheetMetalNote(PartSummary part, DrawingOptions options)
        {
            if (part.SheetMetalThicknessMm <= 0)
                return "SHEET METAL PART. BEND RADII PER FLAT PATTERN.";

            if (options.Units == UnitSystem.Inch)
            {
                return string.Format(CultureInfo.InvariantCulture,
                    "MATERIAL THICKNESS {0:0.000} IN. {1} BEND(S). BEND RADII AS SHOWN ON THE FLAT PATTERN.",
                    part.SheetMetalThicknessMm / 25.4, part.BendCount);
            }

            return string.Format(CultureInfo.InvariantCulture,
                "MATERIAL THICKNESS {0:0.##} MM. {1} BEND(S). BEND RADII AS SHOWN ON THE FLAT PATTERN.",
                part.SheetMetalThicknessMm, part.BendCount);
        }

        private static bool HasTappedHoles(PartSummary part)
        {
            foreach (HoleSummary hole in part.Holes)
            {
                if (hole.IsTapped) return true;
            }
            return false;
        }

        /// <summary>
        /// Expands the tokens a user note may contain. Unknown tokens are left
        /// alone so a typo shows up on the drawing instead of vanishing.
        /// </summary>
        public static string Expand(string text, PartSummary part, DrawingOptions options)
        {
            if (string.IsNullOrEmpty(text)) return text;

            string result = text;
            result = Replace(result, "{PartNumber}", part != null ? part.PartNumber : string.Empty);
            result = Replace(result, "{ModelName}", part != null ? part.ModelName : string.Empty);
            result = Replace(result, "{Material}", part != null ? part.Material : string.Empty);
            result = Replace(result, "{Finish}", part != null ? part.Finish : string.Empty);
            result = Replace(result, "{Rev}", part != null ? part.Revision : string.Empty);
            result = Replace(result, "{Config}", part != null ? part.Configuration : string.Empty);
            result = Replace(result, "{Company}", options != null ? options.CompanyName : string.Empty);
            result = Replace(result, "{Standard}", options != null ? options.Profile().DimensioningSpec : string.Empty);
            result = Replace(result, "{Date}", DateTime.Now.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture));
            return result;
        }

        private static string Replace(string text, string token, string value)
        {
            if (text.IndexOf(token, StringComparison.OrdinalIgnoreCase) < 0) return text;
            return System.Text.RegularExpressions.Regex.Replace(
                text,
                System.Text.RegularExpressions.Regex.Escape(token),
                (value ?? string.Empty).Replace("$", "$$"),
                System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        }

        private static string Trim(string value)
        {
            return value == null ? null : value.Trim();
        }
    }
}
