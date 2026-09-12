using System;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Standards;

namespace DrawingForge.Core.Planning
{
    /// <summary>
    /// Carves a sheet into the regions the planner is allowed to put things in.
    /// </summary>
    /// <remarks>
    /// The sheet is split into a full-width bottom band that the title block
    /// and the general notes own, and everything above it, which the views own.
    /// A full-width band rather than a corner cut-out means the view layout can
    /// stay a plain rectangle problem, and no view can ever land on the title
    /// block — the single most common defect in machine-generated drawings.
    /// </remarks>
    public sealed class SheetLayout
    {
        /// <summary>Clearance between the title block band and the nearest view.</summary>
        public const double BandClearanceMm = 6.0;

        public SheetSize Sheet { get; private set; }

        public SheetLayout(SheetSize sheet)
        {
            if (sheet == null) throw new ArgumentNullException("sheet");
            Sheet = sheet;
        }

        /// <summary>Everything inside the border.</summary>
        public RectMm Drawable { get { return Sheet.DrawableArea; } }

        /// <summary>Bottom band: title block on the right, notes to its left.</summary>
        public RectMm BottomBand
        {
            get
            {
                RectMm d = Drawable;
                double height = Math.Min(Sheet.TitleBlockHeightMm, d.Height * 0.4);
                return new RectMm(d.Left, d.Bottom, d.Width, height);
            }
        }

        /// <summary>Title block, in the lower-right corner of the band.</summary>
        public RectMm TitleBlock
        {
            get
            {
                RectMm band = BottomBand;
                double width = Math.Min(Sheet.TitleBlockWidthMm, band.Width);
                return new RectMm(band.Right - width, band.Bottom, width, band.Height);
            }
        }

        /// <summary>
        /// Region the general note block goes in: the part of the bottom band
        /// left of the title block.
        /// </summary>
        public RectMm NotesArea
        {
            get
            {
                RectMm band = BottomBand;
                RectMm tb = TitleBlock;
                double width = Math.Max(0.0, tb.Left - band.Left - BandClearanceMm);
                return new RectMm(band.Left, band.Bottom, width, band.Height);
            }
        }

        /// <summary>Region the views go in: everything above the bottom band.</summary>
        public RectMm ViewArea
        {
            get
            {
                RectMm d = Drawable;
                RectMm band = BottomBand;
                double bottom = band.Top + BandClearanceMm;
                double height = d.Top - bottom;
                if (height < 0) height = 0;
                return new RectMm(d.Left, bottom, d.Width, height);
            }
        }

        /// <summary>
        /// Where the first/third angle projection symbol goes: just left of the
        /// title block, at the top of the band.
        /// </summary>
        public RectMm ProjectionSymbolZone
        {
            get
            {
                RectMm tb = TitleBlock;
                double size = Math.Min(20.0, tb.Height * 0.6);
                return new RectMm(tb.Left - BandClearanceMm - 2 * size, tb.Bottom + tb.Height - size,
                                  2 * size, size);
            }
        }

        /// <summary>True when the notes area is too small to hold a readable note block.</summary>
        public bool NotesAreaIsCramped
        {
            get { return NotesArea.Width < 60.0 || NotesArea.Height < 25.0; }
        }
    }
}
