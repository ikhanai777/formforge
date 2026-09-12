using System;
using System.Collections.Generic;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Options;

namespace DrawingForge.Core.Planning
{
    /// <summary>
    /// How a named SOLIDWORKS view maps model axes onto the sheet.
    /// </summary>
    /// <remarks>
    /// SOLIDWORKS' standard orientations look at the model along a principal
    /// axis: the front view looks down -Z with +X to the right and +Y up. The
    /// rest follow from the third-angle unfolding of that box. Signs matter for
    /// projected-view placement, so they are recorded rather than inferred.
    /// </remarks>
    public sealed class ViewFrame
    {
        public ViewOrientation Orientation { get; private set; }

        /// <summary>Model axis that runs left-to-right on the sheet.</summary>
        public Axis HorizontalAxis { get; private set; }

        /// <summary>+1 when the axis increases to the right, -1 when it increases to the left.</summary>
        public int HorizontalSign { get; private set; }

        /// <summary>Model axis that runs bottom-to-top on the sheet.</summary>
        public Axis VerticalAxis { get; private set; }

        /// <summary>+1 when the axis increases upward.</summary>
        public int VerticalSign { get; private set; }

        /// <summary>Model axis pointing out of the sheet toward the viewer.</summary>
        public Axis DepthAxis { get; private set; }

        /// <summary>Name of the SOLIDWORKS named view, e.g. "*Front".</summary>
        public string SwViewName { get; private set; }

        private ViewFrame(ViewOrientation orientation, Axis h, int hs, Axis v, int vs, Axis d, string swName)
        {
            Orientation = orientation;
            HorizontalAxis = h;
            HorizontalSign = hs;
            VerticalAxis = v;
            VerticalSign = vs;
            DepthAxis = d;
            SwViewName = swName;
        }

        private static readonly Dictionary<ViewOrientation, ViewFrame> Frames =
            new Dictionary<ViewOrientation, ViewFrame>
            {
                { ViewOrientation.Front,  new ViewFrame(ViewOrientation.Front,  Axis.X,  1, Axis.Y,  1, Axis.Z, "*Front")  },
                { ViewOrientation.Back,   new ViewFrame(ViewOrientation.Back,   Axis.X, -1, Axis.Y,  1, Axis.Z, "*Back")   },
                { ViewOrientation.Right,  new ViewFrame(ViewOrientation.Right,  Axis.Z, -1, Axis.Y,  1, Axis.X, "*Right")  },
                { ViewOrientation.Left,   new ViewFrame(ViewOrientation.Left,   Axis.Z,  1, Axis.Y,  1, Axis.X, "*Left")   },
                { ViewOrientation.Top,    new ViewFrame(ViewOrientation.Top,    Axis.X,  1, Axis.Z, -1, Axis.Y, "*Top")    },
                { ViewOrientation.Bottom, new ViewFrame(ViewOrientation.Bottom, Axis.X,  1, Axis.Z,  1, Axis.Y, "*Bottom") },
            };

        public static ViewFrame Of(ViewOrientation orientation)
        {
            return Frames[orientation];
        }

        public static IEnumerable<ViewFrame> All
        {
            get { return Frames.Values; }
        }

        /// <summary>Width of this view of the box, in millimetres, before rotation.</summary>
        public double WidthOf(BoundingBox box) { return box.Size(HorizontalAxis); }

        /// <summary>Height of this view of the box, in millimetres, before rotation.</summary>
        public double HeightOf(BoundingBox box) { return box.Size(VerticalAxis); }

        /// <summary>Depth behind this view, in millimetres. This is what the side and top views are as wide as.</summary>
        public double DepthOf(BoundingBox box) { return box.Size(DepthAxis); }

        public override string ToString()
        {
            return SwViewName;
        }
    }

    /// <summary>Principal view choice, with the reasoning kept for the run report.</summary>
    public sealed class PrincipalViewChoice
    {
        public ViewOrientation Orientation { get; set; }

        /// <summary>In-plane rotation applied to the view, degrees counter-clockwise.</summary>
        public double RotationDegrees { get; set; }

        /// <summary>Width of the principal view on the sheet at 1:1, after rotation.</summary>
        public double WidthMm { get; set; }

        /// <summary>Height of the principal view on the sheet at 1:1, after rotation.</summary>
        public double HeightMm { get; set; }

        /// <summary>Depth of the model behind the principal view at 1:1.</summary>
        public double DepthMm { get; set; }

        /// <summary>Why this orientation won, in one line, for the run log.</summary>
        public string Rationale { get; set; }

        public ViewFrame Frame { get { return ViewFrame.Of(Orientation); } }

        public string SwViewName { get { return Frame.SwViewName; } }
    }
}
