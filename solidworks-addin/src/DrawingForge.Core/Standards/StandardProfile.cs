using System;
using System.Collections.Generic;
using DrawingForge.Core.Options;

namespace DrawingForge.Core.Standards
{
    /// <summary>
    /// Everything a drafting standard decides for us: default projection
    /// convention, unit, scale ladder, general tolerance text and the
    /// SOLIDWORKS detailing-standard ordinal to stamp on the document.
    /// </summary>
    public sealed class StandardProfile
    {
        public DrawingStandardKind Kind { get; private set; }

        /// <summary>Name as it should appear in a note, e.g. "ASME Y14.5-2018".</summary>
        public string DimensioningSpec { get; private set; }

        /// <summary>Name of the projection spec, e.g. "ASME Y14.3".</summary>
        public string ProjectionSpec { get; private set; }

        public ProjectionAngle DefaultProjection { get; private set; }

        public UnitSystem DefaultUnits { get; private set; }

        /// <summary>Value for <c>swDetailingStandard_e</c>.</summary>
        public int SwDetailingStandard { get; private set; }

        /// <summary>General tolerance note, already formatted for the unit system.</summary>
        public string GeneralToleranceNote { get; private set; }

        /// <summary>Edge-break note.</summary>
        public string EdgeBreakNote { get; private set; }

        /// <summary>Default surface finish note.</summary>
        public string SurfaceFinishNote { get; private set; }

        /// <summary>Reduction and enlargement ratios the standard prefers, in the
        /// order they should be tried (largest drawing first).</summary>
        public IReadOnlyList<Ratio> ScaleLadder { get; private set; }

        private StandardProfile() { }

        // swDetailingStandard_e ordinals.
        private const int SwStdAnsi = 1;
        private const int SwStdIso = 2;
        private const int SwStdDin = 3;
        private const int SwStdJis = 4;
        private const int SwStdBsi = 5;
        private const int SwStdGost = 6;

        /// <summary>
        /// Profile for a standard. <paramref name="units"/> only changes the
        /// tolerance and edge-break text; the rest of the standard is fixed.
        /// </summary>
        public static StandardProfile For(DrawingStandardKind kind, UnitSystem units)
        {
            switch (kind)
            {
                case DrawingStandardKind.Asme:
                    return new StandardProfile
                    {
                        Kind = kind,
                        DimensioningSpec = "ASME Y14.5-2018",
                        ProjectionSpec = "ASME Y14.3",
                        DefaultProjection = ProjectionAngle.Third,
                        DefaultUnits = UnitSystem.Inch,
                        SwDetailingStandard = SwStdAnsi,
                        GeneralToleranceNote = units == UnitSystem.Inch
                            ? "GENERAL TOLERANCES:  .X ±.1   .XX ±.03   .XXX ±.010   ANGLES ±0°30'"
                            : "GENERAL TOLERANCES:  X ±0.5   X.X ±0.25   X.XX ±0.13   ANGLES ±0°30'",
                        EdgeBreakNote = units == UnitSystem.Inch
                            ? "REMOVE ALL BURRS AND BREAK SHARP EDGES .010-.020."
                            : "REMOVE ALL BURRS AND BREAK SHARP EDGES 0.25-0.5.",
                        SurfaceFinishNote = units == UnitSystem.Inch
                            ? "SURFACE FINISH 125 µIN Ra OR BETTER ON ALL MACHINED SURFACES."
                            : "SURFACE ROUGHNESS Ra 3.2 OR BETTER ON ALL MACHINED SURFACES.",
                        ScaleLadder = AsmeLadder()
                    };

                case DrawingStandardKind.Jis:
                    return Metric(kind, "JIS B 0001", "JIS B 0001", SwStdJis, units,
                                  "GENERAL TOLERANCES PER JIS B 0405-m.");

                case DrawingStandardKind.Din:
                    return Metric(kind, "ISO 1101 / DIN 406", "ISO 128-30", SwStdDin, units,
                                  "GENERAL TOLERANCES PER ISO 2768-mK.");

                case DrawingStandardKind.Bsi:
                    return Metric(kind, "BS 8888:2020", "BS 8888:2020", SwStdBsi, units,
                                  "GENERAL TOLERANCES PER ISO 2768-mK.");

                case DrawingStandardKind.Gost:
                    return Metric(kind, "GOST 2.307", "GOST 2.305", SwStdGost, units,
                                  "GENERAL TOLERANCES PER GOST 30893.1-m.");

                case DrawingStandardKind.Iso:
                default:
                    return Metric(kind, "ISO 8015 / ISO 1101", "ISO 128-30", SwStdIso, units,
                                  "GENERAL TOLERANCES PER ISO 2768-mK.");
            }
        }

        private static StandardProfile Metric(DrawingStandardKind kind, string dimSpec, string projSpec,
                                              int swStandard, UnitSystem units, string toleranceNote)
        {
            return new StandardProfile
            {
                Kind = kind,
                DimensioningSpec = dimSpec,
                ProjectionSpec = projSpec,
                DefaultProjection = ProjectionAngle.First,
                DefaultUnits = UnitSystem.Millimeter,
                SwDetailingStandard = swStandard,
                GeneralToleranceNote = units == UnitSystem.Inch
                    ? "GENERAL TOLERANCES:  .X ±.1   .XX ±.03   .XXX ±.010   ANGLES ±0°30'"
                    : toleranceNote,
                EdgeBreakNote = units == UnitSystem.Inch
                    ? "BREAK SHARP EDGES .010 MAX."
                    : "BREAK SHARP EDGES 0.2 MAX.",
                SurfaceFinishNote = units == UnitSystem.Inch
                    ? "SURFACE FINISH 125 µIN Ra UNLESS OTHERWISE STATED."
                    : "SURFACE ROUGHNESS Ra 3.2 UNLESS OTHERWISE STATED.",
                ScaleLadder = IsoLadder()
            };
        }

        /// <summary>
        /// ASME Y14.1 preferred scales. Enlargements first, then full size,
        /// then reductions, so a caller can walk the list and stop at the first
        /// ratio that fits.
        /// </summary>
        private static IReadOnlyList<Ratio> AsmeLadder()
        {
            return new List<Ratio>
            {
                new Ratio(100, 1), new Ratio(50, 1), new Ratio(20, 1), new Ratio(10, 1),
                new Ratio(5, 1), new Ratio(4, 1), new Ratio(2, 1),
                new Ratio(1, 1),
                new Ratio(1, 2), new Ratio(1, 4), new Ratio(1, 5), new Ratio(1, 8),
                new Ratio(1, 10), new Ratio(1, 16), new Ratio(1, 20), new Ratio(1, 40),
                new Ratio(1, 50), new Ratio(1, 100), new Ratio(1, 200)
            };
        }

        /// <summary>ISO 5455 preferred scales.</summary>
        private static IReadOnlyList<Ratio> IsoLadder()
        {
            return new List<Ratio>
            {
                new Ratio(100, 1), new Ratio(50, 1), new Ratio(20, 1), new Ratio(10, 1),
                new Ratio(5, 1), new Ratio(2, 1),
                new Ratio(1, 1),
                new Ratio(1, 2), new Ratio(1, 5), new Ratio(1, 10), new Ratio(1, 20),
                new Ratio(1, 50), new Ratio(1, 100), new Ratio(1, 200), new Ratio(1, 500),
                new Ratio(1, 1000)
            };
        }
    }

    /// <summary>A drawing scale as the ratio it must be written as on the sheet.</summary>
    public sealed class Ratio : IEquatable<Ratio>
    {
        public double Numerator { get; private set; }
        public double Denominator { get; private set; }

        public Ratio(double numerator, double denominator)
        {
            if (numerator <= 0 || denominator <= 0)
                throw new ArgumentOutOfRangeException("numerator", "Scale terms must be positive.");
            Numerator = numerator;
            Denominator = denominator;
        }

        /// <summary>Drawing size divided by model size.</summary>
        public double Value { get { return Numerator / Denominator; } }

        public bool IsFullSize { get { return Math.Abs(Value - 1.0) < 1e-9; } }

        public override string ToString()
        {
            return string.Format(System.Globalization.CultureInfo.InvariantCulture,
                "{0:0.###}:{1:0.###}", Numerator, Denominator);
        }

        public bool Equals(Ratio other)
        {
            if (ReferenceEquals(other, null)) return false;
            return Math.Abs(Numerator - other.Numerator) < 1e-9
                && Math.Abs(Denominator - other.Denominator) < 1e-9;
        }

        public override bool Equals(object obj)
        {
            return Equals(obj as Ratio);
        }

        public override int GetHashCode()
        {
            return Numerator.GetHashCode() ^ (Denominator.GetHashCode() << 1);
        }
    }
}
