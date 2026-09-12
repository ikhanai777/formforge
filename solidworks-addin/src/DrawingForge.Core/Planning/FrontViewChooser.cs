using System;
using System.Collections.Generic;
using System.Globalization;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Options;

namespace DrawingForge.Core.Planning
{
    /// <summary>
    /// Chooses which of the six named views becomes the drawing's front view.
    /// </summary>
    /// <remarks>
    /// Drafting practice, in order of weight:
    ///
    /// 1. The front view carries the most shape information, so it looks at the
    ///    largest face: the viewing direction runs along the model's shortest
    ///    extent.
    /// 2. A body of revolution is drawn with its axis horizontal, always. That
    ///    rule beats rule 1, because a turned part dimensioned from a circular
    ///    view is unusable on a lathe.
    /// 3. Between orientations that score the same, the model's own front wins.
    ///    Designers orient models deliberately and a drawing that silently
    ///    disagrees with the model is a drawing nobody trusts.
    /// 4. Back, bottom and left are last resorts; a drawing built from them
    ///    reads as a mistake even when it is geometrically fine.
    /// </remarks>
    public static class FrontViewChooser
    {
        /// <summary>Two extents within this fraction of each other count as equal.</summary>
        public const double EqualExtentTolerance = 0.02;

        private const double ScoreAxisHorizontal = 1000.0;
        private const double ScoreRotationPenalty = 120.0;
        private const double ScoreModelFrontBonus = 60.0;
        private const double ScoreAreaWeight = 300.0;

        /// <summary>Conventional preference between orientations, highest first.</summary>
        private static readonly Dictionary<ViewOrientation, double> ConventionBonus =
            new Dictionary<ViewOrientation, double>
            {
                { ViewOrientation.Front,  50.0 },
                { ViewOrientation.Right,  30.0 },
                { ViewOrientation.Top,    25.0 },
                { ViewOrientation.Left,   10.0 },
                { ViewOrientation.Bottom,  5.0 },
                { ViewOrientation.Back,    0.0 },
            };

        public static PrincipalViewChoice Choose(PartSummary part, bool respectModelOrientation = true)
        {
            if (part == null) throw new ArgumentNullException("part");
            BoundingBox box = part.Box ?? BoundingBox.FromExtents(1, 1, 1);

            Axis? requiredHorizontal = null;
            if (part.IsRotational)
                requiredHorizontal = part.RotationAxis;
            else if (part.IsSheetMetal && box.IsPlateLike())
                requiredHorizontal = LongestOtherThan(box, box.ShortestAxis);

            PrincipalViewChoice best = null;
            double bestScore = double.NegativeInfinity;
            string bestReason = string.Empty;

            foreach (ViewFrame frame in ViewFrame.All)
            {
                foreach (double rotation in new double[] { 0.0, 90.0 })
                {
                    double score;
                    string reason;
                    PrincipalViewChoice candidate = Evaluate(box, frame, rotation, requiredHorizontal,
                                                             respectModelOrientation, out score, out reason);
                    if (score > bestScore)
                    {
                        bestScore = score;
                        best = candidate;
                        bestReason = reason;
                    }
                }
            }

            best.Rationale = bestReason;
            return best;
        }

        private static PrincipalViewChoice Evaluate(BoundingBox box, ViewFrame frame, double rotationDegrees,
                                                    Axis? requiredHorizontal, bool respectModelOrientation,
                                                    out double score, out string reason)
        {
            bool rotated = Math.Abs(rotationDegrees) > 1e-9;

            // A 90-degree in-plane rotation swaps which model axis reads across
            // the sheet, which is how an axis that is vertical in every named
            // view can still be drawn horizontal.
            Axis screenHorizontal = rotated ? frame.VerticalAxis : frame.HorizontalAxis;
            Axis screenVertical = rotated ? frame.HorizontalAxis : frame.VerticalAxis;

            double width = box.Size(screenHorizontal);
            double height = box.Size(screenVertical);
            double depth = box.Size(frame.DepthAxis);

            score = 0;
            List<string> reasons = new List<string>();

            // Rule 1: show the largest face. Normalised so a cube scores 1/3 on
            // every orientation and a plate scores near 1 on the right one.
            double totalFaceArea = box.SizeX * box.SizeY + box.SizeY * box.SizeZ + box.SizeZ * box.SizeX;
            if (totalFaceArea > 1e-12)
            {
                double share = (width * height) / totalFaceArea;
                score += ScoreAreaWeight * share;
            }

            // Rule 2: axis of revolution (or a plate's long edge) runs horizontal.
            if (requiredHorizontal.HasValue)
            {
                if (screenHorizontal == requiredHorizontal.Value)
                {
                    score += ScoreAxisHorizontal;
                    reasons.Add(string.Format(CultureInfo.InvariantCulture,
                        "{0} axis drawn horizontal", requiredHorizontal.Value));
                }
                else if (screenVertical == requiredHorizontal.Value)
                {
                    // Showing the axis at all beats an end-on view of it.
                    score += ScoreAxisHorizontal * 0.4;
                }
            }

            if (rotated) score -= ScoreRotationPenalty;

            // Rules 3 and 4: convention.
            score += ConventionBonus[frame.Orientation];
            if (respectModelOrientation && frame.Orientation == ViewOrientation.Front && !rotated)
            {
                score += ScoreModelFrontBonus;
                reasons.Add("model's own front kept");
            }

            if (reasons.Count == 0)
            {
                reasons.Add(string.Format(CultureInfo.InvariantCulture,
                    "largest face ({0:0.#} x {1:0.#} mm) faces the reader", width, height));
            }
            if (rotated) reasons.Add("rotated 90°");

            reason = string.Join("; ", reasons.ToArray());

            return new PrincipalViewChoice
            {
                Orientation = frame.Orientation,
                RotationDegrees = rotationDegrees,
                WidthMm = width,
                HeightMm = height,
                DepthMm = depth
            };
        }

        /// <summary>Longest extent among the axes other than <paramref name="exclude"/>.</summary>
        private static Axis LongestOtherThan(BoundingBox box, Axis exclude)
        {
            Axis best = Axis.X;
            double bestSize = -1;
            foreach (Axis a in new[] { Axis.X, Axis.Y, Axis.Z })
            {
                if (a == exclude) continue;
                double size = box.Size(a);
                if (size > bestSize)
                {
                    bestSize = size;
                    best = a;
                }
            }
            return best;
        }

        /// <summary>
        /// Detects a body of revolution from its bounding box alone: two extents
        /// equal to within <see cref="EqualExtentTolerance"/> means the odd axis
        /// is the candidate axis of revolution.
        /// </summary>
        /// <remarks>
        /// The inspector prefers real evidence — a revolve feature, a cylindrical
        /// face covering most of the surface area — and only falls back to this.
        /// A square prism will fool it, which is why this returns false unless
        /// the two equal extents differ from the third.
        /// </remarks>
        public static bool LooksRotational(BoundingBox box, out Axis axis)
        {
            axis = Axis.Z;
            if (box == null) return false;

            double x = box.SizeX, y = box.SizeY, z = box.SizeZ;
            if (NearlyEqual(x, y) && !NearlyEqual(x, z)) { axis = Axis.Z; return true; }
            if (NearlyEqual(y, z) && !NearlyEqual(y, x)) { axis = Axis.X; return true; }
            if (NearlyEqual(x, z) && !NearlyEqual(x, y)) { axis = Axis.Y; return true; }
            return false;
        }

        private static bool NearlyEqual(double a, double b)
        {
            double scale = Math.Max(Math.Abs(a), Math.Abs(b));
            if (scale < 1e-9) return true;
            return Math.Abs(a - b) / scale <= EqualExtentTolerance;
        }
    }
}
