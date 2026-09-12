using System;
using System.Collections.Generic;
using DrawingForge.Core.Geometry;

namespace DrawingForge.Core.Standards
{
    /// <summary>
    /// Picks a standard scale ratio for a view block.
    /// </summary>
    /// <remarks>
    /// The rule is the one a drafter uses: take the largest preferred ratio at
    /// which the whole view block still fits inside the drawable area with room
    /// for dimensions, and never invent a ratio that is not on the standard's
    /// ladder. A scale like 1:2.7 is legal arithmetic and unacceptable drafting.
    ///
    /// The gaps between views do not scale with the model — they are sheet
    /// space, sized for dimension lines — so they are passed separately as
    /// fixed overhead rather than folded into the block size.
    /// </remarks>
    public static class ScaleSelector
    {
        /// <summary>
        /// Fraction of the drawable area left free for dimensions, leader lines
        /// and view labels. Views themselves get the rest.
        /// </summary>
        public const double DefaultAnnotationAllowance = 0.28;

        /// <summary>
        /// Largest ladder ratio at which a block of <paramref name="blockWidthMm"/>
        /// x <paramref name="blockHeightMm"/> (measured at full size), plus the
        /// fixed overhead, fits inside <paramref name="available"/>.
        /// </summary>
        /// <returns>
        /// The chosen ratio, or the smallest ratio on the ladder if even that
        /// does not fit. Callers that care should ask <see cref="Fits"/> which
        /// of the two happened.
        /// </returns>
        public static Ratio Choose(IReadOnlyList<Ratio> ladder,
                                   double blockWidthMm,
                                   double blockHeightMm,
                                   RectMm available,
                                   double annotationAllowance = DefaultAnnotationAllowance,
                                   double fixedOverheadWidthMm = 0.0,
                                   double fixedOverheadHeightMm = 0.0)
        {
            if (ladder == null || ladder.Count == 0)
                throw new ArgumentException("Scale ladder is empty.", "ladder");
            if (blockWidthMm <= 0 || blockHeightMm <= 0)
                return FullSizeOrFirst(ladder);

            foreach (Ratio candidate in ladder)
            {
                if (Fits(candidate, blockWidthMm, blockHeightMm, available,
                         annotationAllowance, fixedOverheadWidthMm, fixedOverheadHeightMm))
                {
                    return candidate;
                }
            }

            return ladder[ladder.Count - 1];
        }

        /// <summary>True when the block plus overhead fits at the given ratio.</summary>
        public static bool Fits(Ratio ratio,
                                double blockWidthMm,
                                double blockHeightMm,
                                RectMm available,
                                double annotationAllowance = DefaultAnnotationAllowance,
                                double fixedOverheadWidthMm = 0.0,
                                double fixedOverheadHeightMm = 0.0)
        {
            if (ratio == null) return false;
            double usableW, usableH;
            Usable(available, annotationAllowance, fixedOverheadWidthMm, fixedOverheadHeightMm,
                   out usableW, out usableH);
            if (usableW <= 0 || usableH <= 0) return false;
            return blockWidthMm * ratio.Value <= usableW
                && blockHeightMm * ratio.Value <= usableH;
        }

        /// <summary>
        /// How much of the usable area the block occupies at this ratio. A sheet
        /// that is technically large enough but leaves the views lost on it
        /// scores low here, which is how auto sheet selection avoids jumping to
        /// a D sheet for a bracket.
        /// </summary>
        public static double Fill(Ratio ratio,
                                  double blockWidthMm,
                                  double blockHeightMm,
                                  RectMm available,
                                  double annotationAllowance = DefaultAnnotationAllowance,
                                  double fixedOverheadWidthMm = 0.0,
                                  double fixedOverheadHeightMm = 0.0)
        {
            if (ratio == null) return 0;
            double usableW, usableH;
            Usable(available, annotationAllowance, fixedOverheadWidthMm, fixedOverheadHeightMm,
                   out usableW, out usableH);
            if (usableW <= 0 || usableH <= 0) return 0;
            double blockArea = (blockWidthMm * ratio.Value) * (blockHeightMm * ratio.Value);
            return blockArea / (usableW * usableH);
        }

        /// <summary>Space left for scaled geometry once annotations and gaps are taken out.</summary>
        public static void Usable(RectMm available, double annotationAllowance,
                                  double fixedOverheadWidthMm, double fixedOverheadHeightMm,
                                  out double usableWidthMm, out double usableHeightMm)
        {
            double allowance = annotationAllowance;
            if (allowance < 0) allowance = 0;
            if (allowance > 0.9) allowance = 0.9;
            usableWidthMm = available.Width * (1.0 - allowance) - fixedOverheadWidthMm;
            usableHeightMm = available.Height * (1.0 - allowance) - fixedOverheadHeightMm;
        }

        /// <summary>
        /// Nearest ladder ratio to an arbitrary value, for reporting a scale
        /// that came back from a document as a decimal.
        /// </summary>
        public static Ratio Snap(IReadOnlyList<Ratio> ladder, double value)
        {
            if (ladder == null || ladder.Count == 0)
                throw new ArgumentException("Scale ladder is empty.", "ladder");
            if (value <= 0) return FullSizeOrFirst(ladder);

            Ratio best = ladder[0];
            double bestError = double.MaxValue;
            foreach (Ratio r in ladder)
            {
                // Compare in log space: 2:1 and 1:2 are equally far from 1:1.
                double error = Math.Abs(Math.Log(r.Value) - Math.Log(value));
                if (error < bestError)
                {
                    bestError = error;
                    best = r;
                }
            }
            return best;
        }

        /// <summary>True when a ratio is on the ladder, i.e. is a legal sheet scale.</summary>
        public static bool IsStandard(IReadOnlyList<Ratio> ladder, Ratio ratio)
        {
            if (ratio == null || ladder == null) return false;
            foreach (Ratio r in ladder)
            {
                if (Math.Abs(r.Value - ratio.Value) < 1e-9) return true;
            }
            return false;
        }

        private static Ratio FullSizeOrFirst(IReadOnlyList<Ratio> ladder)
        {
            foreach (Ratio r in ladder)
            {
                if (r.IsFullSize) return r;
            }
            return ladder[0];
        }
    }
}
