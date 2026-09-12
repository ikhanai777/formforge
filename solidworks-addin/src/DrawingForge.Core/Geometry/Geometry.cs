using System;

namespace DrawingForge.Core.Geometry
{
    /// <summary>Model-space axis.</summary>
    public enum Axis
    {
        X = 0,
        Y = 1,
        Z = 2
    }

    /// <summary>Minimal immutable 3-vector. Millimetres throughout.</summary>
    public struct Vec3
    {
        public readonly double X;
        public readonly double Y;
        public readonly double Z;

        public Vec3(double x, double y, double z)
        {
            X = x;
            Y = y;
            Z = z;
        }

        public double Get(Axis axis)
        {
            switch (axis)
            {
                case Axis.X: return X;
                case Axis.Y: return Y;
                case Axis.Z: return Z;
                default: throw new ArgumentOutOfRangeException("axis");
            }
        }

        public double Length
        {
            get { return Math.Sqrt(X * X + Y * Y + Z * Z); }
        }

        public override string ToString()
        {
            return string.Format(System.Globalization.CultureInfo.InvariantCulture,
                "({0:0.###}, {1:0.###}, {2:0.###})", X, Y, Z);
        }
    }

    /// <summary>
    /// Axis-aligned bounding box in model space, millimetres.
    /// </summary>
    /// <remarks>
    /// SOLIDWORKS reports box corners in metres; the inspector converts once on
    /// the way in so that everything above this layer is in millimetres and the
    /// only inch conversion happens when a sheet is described in inches.
    /// </remarks>
    public sealed class BoundingBox
    {
        public Vec3 Min { get; private set; }
        public Vec3 Max { get; private set; }

        public BoundingBox(Vec3 min, Vec3 max)
        {
            Min = min;
            Max = max;
        }

        public static BoundingBox FromExtents(double dx, double dy, double dz)
        {
            return new BoundingBox(new Vec3(0, 0, 0), new Vec3(dx, dy, dz));
        }

        public double SizeX { get { return Math.Abs(Max.X - Min.X); } }
        public double SizeY { get { return Math.Abs(Max.Y - Min.Y); } }
        public double SizeZ { get { return Math.Abs(Max.Z - Min.Z); } }

        public double Size(Axis axis)
        {
            switch (axis)
            {
                case Axis.X: return SizeX;
                case Axis.Y: return SizeY;
                case Axis.Z: return SizeZ;
                default: throw new ArgumentOutOfRangeException("axis");
            }
        }

        public Vec3 Extents { get { return new Vec3(SizeX, SizeY, SizeZ); } }

        public double Diagonal { get { return Extents.Length; } }

        /// <summary>Longest edge of the box.</summary>
        public double MaxExtent { get { return Math.Max(SizeX, Math.Max(SizeY, SizeZ)); } }

        /// <summary>Shortest edge of the box.</summary>
        public double MinExtent { get { return Math.Min(SizeX, Math.Min(SizeY, SizeZ)); } }

        /// <summary>Axis with the largest extent; ties resolve X &gt; Y &gt; Z.</summary>
        public Axis LongestAxis
        {
            get
            {
                if (SizeX >= SizeY && SizeX >= SizeZ) return Axis.X;
                if (SizeY >= SizeZ) return Axis.Y;
                return Axis.Z;
            }
        }

        /// <summary>Axis with the smallest extent; ties resolve X &gt; Y &gt; Z.</summary>
        public Axis ShortestAxis
        {
            get
            {
                if (SizeX <= SizeY && SizeX <= SizeZ) return Axis.X;
                if (SizeY <= SizeZ) return Axis.Y;
                return Axis.Z;
            }
        }

        /// <summary>
        /// True when the box is plate-like: one extent much smaller than the
        /// other two. Used to steer the principal view onto the large face.
        /// </summary>
        public bool IsPlateLike(double ratio = 6.0)
        {
            double min = MinExtent;
            if (min <= 1e-9) return true;
            double[] sorted = SortedExtents();
            return sorted[1] / sorted[0] >= ratio;
        }

        /// <summary>Extents sorted ascending.</summary>
        public double[] SortedExtents()
        {
            double[] v = new double[] { SizeX, SizeY, SizeZ };
            Array.Sort(v);
            return v;
        }

        public override string ToString()
        {
            return string.Format(System.Globalization.CultureInfo.InvariantCulture,
                "{0:0.##} x {1:0.##} x {2:0.##} mm", SizeX, SizeY, SizeZ);
        }
    }

    /// <summary>A rectangle on the sheet, millimetres, origin at the sheet's lower-left corner.</summary>
    public struct RectMm
    {
        public readonly double Left;
        public readonly double Bottom;
        public readonly double Width;
        public readonly double Height;

        public RectMm(double left, double bottom, double width, double height)
        {
            Left = left;
            Bottom = bottom;
            Width = width;
            Height = height;
        }

        public double Right { get { return Left + Width; } }
        public double Top { get { return Bottom + Height; } }
        public double CenterX { get { return Left + Width / 2.0; } }
        public double CenterY { get { return Bottom + Height / 2.0; } }
        public double Area { get { return Width * Height; } }

        public static RectMm FromCenter(double cx, double cy, double w, double h)
        {
            return new RectMm(cx - w / 2.0, cy - h / 2.0, w, h);
        }

        public bool Contains(RectMm other, double tolerance = 1e-6)
        {
            return other.Left >= Left - tolerance
                && other.Bottom >= Bottom - tolerance
                && other.Right <= Right + tolerance
                && other.Top <= Top + tolerance;
        }

        public bool Intersects(RectMm other, double tolerance = 1e-6)
        {
            if (other.Left >= Right - tolerance) return false;
            if (other.Right <= Left + tolerance) return false;
            if (other.Bottom >= Top - tolerance) return false;
            if (other.Top <= Bottom + tolerance) return false;
            return true;
        }

        public RectMm Inflate(double amount)
        {
            return new RectMm(Left - amount, Bottom - amount, Width + 2 * amount, Height + 2 * amount);
        }

        public override string ToString()
        {
            return string.Format(System.Globalization.CultureInfo.InvariantCulture,
                "[{0:0.#},{1:0.#} {2:0.#}x{3:0.#}]", Left, Bottom, Width, Height);
        }
    }

    /// <summary>Unit conversion helpers. One place, so rounding is consistent.</summary>
    public static class Units
    {
        public const double MmPerInch = 25.4;
        public const double MmPerMeter = 1000.0;

        public static double InchToMm(double inches) { return inches * MmPerInch; }
        public static double MmToInch(double mm) { return mm / MmPerInch; }
        public static double MeterToMm(double meters) { return meters * MmPerMeter; }
        public static double MmToMeter(double mm) { return mm / MmPerMeter; }
    }
}
