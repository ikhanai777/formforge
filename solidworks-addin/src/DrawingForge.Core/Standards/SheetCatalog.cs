using System;
using System.Collections.Generic;
using System.Linq;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Options;

namespace DrawingForge.Core.Standards
{
    /// <summary>
    /// One sheet size out of ASME Y14.1 (inch series) or ISO 5457 (A series).
    /// </summary>
    public sealed class SheetSize
    {
        public string Name { get; private set; }
        public DrawingStandardKind Series { get; private set; }
        public double WidthMm { get; private set; }
        public double HeightMm { get; private set; }

        /// <summary>
        /// Value for <c>swDwgPaperSizes_e</c>. -1 means "no named size, pass
        /// user-defined width and height instead".
        /// </summary>
        public int SwPaperSize { get; private set; }

        /// <summary>Border inset from the trimmed edge, per the series' own rules.</summary>
        public double MarginMm { get; private set; }

        /// <summary>Width of the title block reserved in the lower-right corner.</summary>
        public double TitleBlockWidthMm { get; private set; }

        /// <summary>Height of the title block reserved in the lower-right corner.</summary>
        public double TitleBlockHeightMm { get; private set; }

        public SheetSize(string name, DrawingStandardKind series, double widthMm, double heightMm,
                         int swPaperSize, double marginMm, double titleBlockWidthMm, double titleBlockHeightMm)
        {
            Name = name;
            Series = series;
            WidthMm = widthMm;
            HeightMm = heightMm;
            SwPaperSize = swPaperSize;
            MarginMm = marginMm;
            TitleBlockWidthMm = titleBlockWidthMm;
            TitleBlockHeightMm = titleBlockHeightMm;
        }

        public bool IsLandscape { get { return WidthMm >= HeightMm; } }

        public double AreaMm2 { get { return WidthMm * HeightMm; } }

        /// <summary>
        /// Rectangle inside the border. Views are laid out here, never outside it.
        /// </summary>
        public RectMm DrawableArea
        {
            get
            {
                return new RectMm(MarginMm, MarginMm,
                                  WidthMm - 2 * MarginMm,
                                  HeightMm - 2 * MarginMm);
            }
        }

        /// <summary>
        /// Lower-right block the title block and revision table own. The layout
        /// planner treats it as occupied.
        /// </summary>
        public RectMm TitleBlockZone
        {
            get
            {
                return new RectMm(WidthMm - MarginMm - TitleBlockWidthMm,
                                  MarginMm,
                                  TitleBlockWidthMm,
                                  TitleBlockHeightMm);
            }
        }

        public override string ToString()
        {
            return string.Format(System.Globalization.CultureInfo.InvariantCulture,
                "{0} ({1:0.#} x {2:0.#} mm)", Name, WidthMm, HeightMm);
        }
    }

    /// <summary>
    /// The sheet sizes the add-in knows, in both series, smallest first.
    /// </summary>
    /// <remarks>
    /// ASME Y14.1 flat sizes A (8.5x11) through F (28x40); ISO 5457 A4 through
    /// A0. Both series are stored in millimetres so the layout planner never
    /// has to care which one is in play.
    ///
    /// The <c>SwPaperSize</c> values are the documented <c>swDwgPaperSizes_e</c>
    /// ordinals. If a future SOLIDWORKS release renumbers them, only this table
    /// changes: the add-in always passes an explicit width and height alongside
    /// the ordinal, and falls back to user-defined sizing when the ordinal is
    /// rejected. See docs/api-notes.md.
    /// </remarks>
    public static class SheetCatalog
    {
        // swDwgPaperSizes_e ordinals.
        public const int SwPaperA4 = 0;
        public const int SwPaperA4Vertical = 1;
        public const int SwPaperA3 = 2;
        public const int SwPaperA2 = 3;
        public const int SwPaperA1 = 4;
        public const int SwPaperA0 = 5;
        public const int SwPaperA = 6;
        public const int SwPaperAVertical = 7;
        public const int SwPaperB = 8;
        public const int SwPaperC = 9;
        public const int SwPaperD = 10;
        public const int SwPaperE = 11;
        public const int SwPaperUserDefined = 12;

        private static readonly SheetSize[] AsmeSheets = new[]
        {
            // Y14.1 borders: 0.25-0.5 in on the small sheets, 0.5-1.0 in from C up.
            new SheetSize("A",           DrawingStandardKind.Asme, In(11.0), In(8.5),  SwPaperA,         In(0.38), In(6.5), In(2.3)),
            new SheetSize("A-Portrait",  DrawingStandardKind.Asme, In(8.5),  In(11.0), SwPaperAVertical, In(0.38), In(6.5), In(2.3)),
            new SheetSize("B",           DrawingStandardKind.Asme, In(17.0), In(11.0), SwPaperB,         In(0.50), In(6.5), In(2.3)),
            new SheetSize("C",           DrawingStandardKind.Asme, In(22.0), In(17.0), SwPaperC,         In(0.50), In(7.5), In(2.6)),
            new SheetSize("D",           DrawingStandardKind.Asme, In(34.0), In(22.0), SwPaperD,         In(0.50), In(7.5), In(2.6)),
            new SheetSize("E",           DrawingStandardKind.Asme, In(44.0), In(34.0), SwPaperE,         In(1.00), In(7.5), In(2.6)),
            new SheetSize("F",           DrawingStandardKind.Asme, In(40.0), In(28.0), SwPaperUserDefined, In(1.00), In(7.5), In(2.6)),
        };

        private static readonly SheetSize[] IsoSheets = new[]
        {
            // ISO 5457: 20 mm filing margin on the left, 10 mm elsewhere. A single
            // 20 mm inset keeps the drawable area conservative on every edge.
            new SheetSize("A4-Portrait", DrawingStandardKind.Iso, 210.0,  297.0,  SwPaperA4Vertical, 20.0, 180.0, 55.0),
            new SheetSize("A4",          DrawingStandardKind.Iso, 297.0,  210.0,  SwPaperA4,         20.0, 180.0, 55.0),
            new SheetSize("A3",          DrawingStandardKind.Iso, 420.0,  297.0,  SwPaperA3,         20.0, 180.0, 55.0),
            new SheetSize("A2",          DrawingStandardKind.Iso, 594.0,  420.0,  SwPaperA2,         20.0, 180.0, 55.0),
            new SheetSize("A1",          DrawingStandardKind.Iso, 841.0,  594.0,  SwPaperA1,         20.0, 180.0, 55.0),
            new SheetSize("A0",          DrawingStandardKind.Iso, 1189.0, 841.0,  SwPaperA0,         20.0, 180.0, 55.0),
        };

        private static double In(double inches)
        {
            return Units.InchToMm(inches);
        }

        /// <summary>Every sheet size in both series.</summary>
        public static IReadOnlyList<SheetSize> All
        {
            get { return AsmeSheets.Concat(IsoSheets).ToList(); }
        }

        /// <summary>
        /// Landscape sizes of one series, ascending by area. This is the ladder
        /// <see cref="SheetSelectionMode.AutoSmallestFit"/> walks.
        /// </summary>
        public static IReadOnlyList<SheetSize> Series(DrawingStandardKind standard)
        {
            SheetSize[] source = UsesInchSeries(standard) ? AsmeSheets : IsoSheets;
            return source.Where(s => s.IsLandscape)
                         .OrderBy(s => s.AreaMm2)
                         .ToList();
        }

        /// <summary>ASME is the only inch series here; everything else uses ISO A sizes.</summary>
        public static bool UsesInchSeries(DrawingStandardKind standard)
        {
            return standard == DrawingStandardKind.Asme;
        }

        /// <summary>Case-insensitive lookup by name across both series.</summary>
        public static SheetSize ByName(string name)
        {
            if (string.IsNullOrEmpty(name)) return null;
            string trimmed = name.Trim();
            foreach (SheetSize s in All)
            {
                if (string.Equals(s.Name, trimmed, StringComparison.OrdinalIgnoreCase))
                    return s;
            }
            return null;
        }

        /// <summary>Default sheet when the user has expressed no preference.</summary>
        public static SheetSize DefaultFor(DrawingStandardKind standard)
        {
            return UsesInchSeries(standard) ? ByName("B") : ByName("A3");
        }

        /// <summary>
        /// A user-defined sheet. Margins scale with the sheet so that a very
        /// small or very large custom size still gets a sane border.
        /// </summary>
        public static SheetSize Custom(string name, double widthMm, double heightMm, DrawingStandardKind series)
        {
            if (widthMm <= 0 || heightMm <= 0)
                throw new ArgumentOutOfRangeException("widthMm", "Custom sheet sizes must be positive.");

            double margin = Clamp(Math.Min(widthMm, heightMm) * 0.05, 6.0, 25.0);
            double tbWidth = Math.Min(widthMm - 2 * margin, UsesInchSeries(series) ? In(6.5) : 180.0);
            double tbHeight = Math.Min(heightMm - 2 * margin, UsesInchSeries(series) ? In(2.3) : 55.0);
            return new SheetSize(name ?? "Custom", series, widthMm, heightMm,
                                 SwPaperUserDefined, margin, tbWidth, tbHeight);
        }

        private static double Clamp(double value, double min, double max)
        {
            if (value < min) return min;
            if (value > max) return max;
            return value;
        }
    }
}
