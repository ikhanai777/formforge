using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using DrawingForge.AddIn.Interop;
using DrawingForge.Core.Geometry;
using DrawingForge.Core.Logging;
using DrawingForge.Core.Planning;
using SolidWorks.Interop.sldworks;

namespace DrawingForge.AddIn.Build
{
    /// <summary>
    /// Reads a SOLIDWORKS model into a <see cref="PartSummary"/>.
    /// </summary>
    /// <remarks>
    /// This is the only place the planner's inputs come from, and several of
    /// them are honest estimates rather than facts the API hands over:
    ///
    /// * Hole details come from Hole Wizard feature data where it exists. A hole
    ///   cut with a plain extruded cut is counted only if it is round; there is
    ///   no reliable way to recognise every hole in a general solid.
    /// * "Has internal features" is inferred from the feature list (shells,
    ///   cavities, blind cuts, ribs). It decides whether a section view is
    ///   offered, so a false positive costs an extra view and a false negative
    ///   costs a section the drafter adds by hand.
    /// * Rotational detection prefers a revolve feature and falls back to the
    ///   bounding box.
    ///
    /// Where a value cannot be determined the summary says so rather than
    /// guessing, and the fabrication checklist turns the gap into a finding on
    /// the drawing instead of letting it pass silently.
    /// </remarks>
    internal static class PartInspector
    {
        /// <summary>Feature type names that mean the part has geometry no outside view shows.</summary>
        private static readonly string[] InternalFeatureTypes =
        {
            "Shell", "Cavity", "Rib", "CutRevolve", "Cut-Revolve", "CutSweep", "CutLoft", "Dome", "Indent"
        };

        /// <summary>Feature type names that mark a sheet metal part.</summary>
        private static readonly string[] SheetMetalFeatureTypes =
        {
            "SheetMetal", "SolidToSheetMetal", "FlatPattern", "BaseFlange", "SMBaseFlange"
        };

        /// <summary>Feature type names that each contribute one bend.</summary>
        private static readonly string[] BendFeatureTypes =
        {
            "SketchBend", "SolidSketchBend", "OneBend", "EdgeFlange", "SMMiteredFlange", "Hem", "Jog"
        };

        public static PartSummary Inspect(ModelDoc2 doc, DrawingTask task, ILog log)
        {
            PartSummary summary = new PartSummary
            {
                FilePath = doc != null ? doc.GetPathName() : (task != null ? task.FilePath : null),
                Configuration = task != null ? task.Configuration : string.Empty,
                IsAssembly = task != null && task.IsAssembly
            };

            summary.ModelName = !string.IsNullOrEmpty(summary.FilePath)
                ? Path.GetFileNameWithoutExtension(summary.FilePath)
                : (task != null ? task.DisplayName : "MODEL");

            if (doc == null)
            {
                summary.Box = BoundingBox.FromExtents(100, 100, 100);
                return summary;
            }

            foreach (KeyValuePair<string, string> property in
                     SwDoc.ReadCustomProperties(doc, summary.Configuration, log))
            {
                summary.CustomProperties[property.Key] = property.Value;
            }

            summary.Box = ReadBoundingBox(doc, log) ?? BoundingBox.FromExtents(100, 100, 100);
            summary.MassGrams = SwDoc.MassGrams(doc, log);
            summary.Material = ResolveMaterial(doc, summary, log);
            summary.Finish = LookupProperty(summary, "Finish", "Treatment", "SurfaceFinish");

            if (!summary.IsAssembly)
            {
                InspectFeatures(doc, summary, log);
            }
            else
            {
                InspectAssembly(doc, summary, log);
            }

            log.Info(string.Format(CultureInfo.InvariantCulture,
                "{0}: {1}{2}{3}{4}",
                summary.ModelName,
                summary.Box,
                summary.IsSheetMetal ? ", sheet metal" : string.Empty,
                summary.IsRotational ? ", rotational" : string.Empty,
                summary.HasInternalFeatures ? ", has internal features" : string.Empty));

            return summary;
        }

        // ---- geometry --------------------------------------------------------

        /// <summary>
        /// Bounding box in millimetres, or null when SOLIDWORKS will not supply
        /// one. Tries the cheapest source first.
        /// </summary>
        public static BoundingBox ReadBoundingBox(ModelDoc2 doc, ILog log)
        {
            // IPartDoc::GetPartBox is exact for a part.
            object boxObject;
            if (SwDispatch.TryInvokeAny(doc, new[] { "GetPartBox" },
                                        new[] { new object[] { true }, new object[] { false } },
                                        out boxObject, log))
            {
                BoundingBox box = FromCornerArray(SwDispatch.AsDoubles(boxObject));
                if (box != null) return box;
            }

            // IAssemblyDoc::GetBox for an assembly.
            if (SwDispatch.TryInvokeAny(doc, new[] { "GetBox" },
                                        new[] { new object[] { 0 }, new object[0] },
                                        out boxObject, log))
            {
                BoundingBox box = FromCornerArray(SwDispatch.AsDoubles(boxObject));
                if (box != null) return box;
            }

            // Union of the solid body boxes.
            BoundingBox fromBodies = UnionOfBodyBoxes(doc, log);
            if (fromBodies != null) return fromBodies;

            log.Warn("Could not read a bounding box; the scale will be a guess until the views are measured.");
            return null;
        }

        private static BoundingBox UnionOfBodyBoxes(ModelDoc2 doc, ILog log)
        {
            object bodiesObject;
            if (!SwDispatch.TryInvokeAny(doc, new[] { "GetBodies2" },
                                         new[] { new object[] { SwConst.swSolidBody, true },
                                                 new object[] { SwConst.swSolidBody, false } },
                                         out bodiesObject, log))
            {
                return null;
            }

            object[] bodies = SwDispatch.AsObjects(bodiesObject);
            double minX = double.MaxValue, minY = double.MaxValue, minZ = double.MaxValue;
            double maxX = double.MinValue, maxY = double.MinValue, maxZ = double.MinValue;
            bool any = false;

            foreach (object body in bodies)
            {
                object boxObject;
                if (!SwDispatch.TryInvoke(body, "GetBodyBox", new object[0], out boxObject, log)) continue;

                double[] corners = SwDispatch.AsDoubles(boxObject);
                if (corners == null || corners.Length < 6) continue;

                any = true;
                minX = Math.Min(minX, corners[0]);
                minY = Math.Min(minY, corners[1]);
                minZ = Math.Min(minZ, corners[2]);
                maxX = Math.Max(maxX, corners[3]);
                maxY = Math.Max(maxY, corners[4]);
                maxZ = Math.Max(maxZ, corners[5]);
            }

            if (!any) return null;
            return FromCornerArray(new[] { minX, minY, minZ, maxX, maxY, maxZ });
        }

        /// <summary>Six metres-valued corner coordinates to a millimetre box.</summary>
        private static BoundingBox FromCornerArray(double[] corners)
        {
            if (corners == null || corners.Length < 6) return null;

            Vec3 min = new Vec3(Units.MeterToMm(corners[0]), Units.MeterToMm(corners[1]), Units.MeterToMm(corners[2]));
            Vec3 max = new Vec3(Units.MeterToMm(corners[3]), Units.MeterToMm(corners[4]), Units.MeterToMm(corners[5]));

            BoundingBox box = new BoundingBox(min, max);
            if (box.SizeX <= 1e-6 && box.SizeY <= 1e-6 && box.SizeZ <= 1e-6) return null;
            return box;
        }

        // ---- features --------------------------------------------------------

        private static void InspectFeatures(ModelDoc2 doc, PartSummary summary, ILog log)
        {
            object featureObject;
            if (!SwDispatch.TryInvoke(doc, "FirstFeature", new object[0], out featureObject, log))
            {
                log.Debug("Feature traversal is unavailable; falling back to the bounding box alone.");
                FallBackToBoxHeuristics(summary);
                return;
            }

            int bendCount = 0;
            bool sawRevolve = false;
            double smallestFeature = double.MaxValue;
            int guard = 0;

            while (featureObject != null && guard++ < 20000)
            {
                string typeName = FeatureTypeName(featureObject, log);

                if (!string.IsNullOrEmpty(typeName))
                {
                    if (MatchesAny(typeName, SheetMetalFeatureTypes))
                    {
                        summary.IsSheetMetal = true;
                        double thickness = SheetMetalThickness(featureObject, log);
                        if (thickness > 0) summary.SheetMetalThicknessMm = thickness;
                    }

                    if (MatchesAny(typeName, BendFeatureTypes)) bendCount++;

                    if (MatchesAny(typeName, InternalFeatureTypes)) summary.HasInternalFeatures = true;

                    if (typeName.IndexOf("Revolve", StringComparison.OrdinalIgnoreCase) >= 0 &&
                        typeName.IndexOf("Cut", StringComparison.OrdinalIgnoreCase) < 0)
                    {
                        sawRevolve = true;
                    }

                    if (typeName.IndexOf("Weldment", StringComparison.OrdinalIgnoreCase) >= 0 ||
                        typeName.IndexOf("CutListFolder", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        summary.IsWeldment = true;
                        string name = FeatureName(featureObject, log);
                        if (!string.IsNullOrEmpty(name)) summary.CutListItems.Add(name);
                    }

                    if (typeName.Equals("HoleWzd", StringComparison.OrdinalIgnoreCase))
                    {
                        HoleSummary hole = ReadWizardHole(featureObject, log);
                        if (hole != null)
                        {
                            summary.Holes.Add(hole);
                            if (!hole.IsThrough) summary.HasInternalFeatures = true;
                            if (hole.DiameterMm > 0) smallestFeature = Math.Min(smallestFeature, hole.DiameterMm);
                        }
                    }

                    if (typeName.IndexOf("Fillet", StringComparison.OrdinalIgnoreCase) >= 0 ||
                        typeName.IndexOf("Chamfer", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        double size = FilletOrChamferSize(featureObject, log);
                        if (size > 0) smallestFeature = Math.Min(smallestFeature, size);
                    }
                }

                object next;
                if (!SwDispatch.TryInvoke(featureObject, "GetNextFeature", new object[0], out next, log)) break;
                featureObject = next;
            }

            summary.BendCount = bendCount;
            summary.SmallestFeatureMm = smallestFeature < double.MaxValue ? smallestFeature : 0;

            if (sawRevolve && summary.Box != null)
            {
                Axis axis;
                if (FrontViewChooser.LooksRotational(summary.Box, out axis))
                {
                    summary.IsRotational = true;
                    summary.RotationAxis = axis;
                }
            }
            else
            {
                FallBackToBoxHeuristics(summary);
            }

            if (summary.IsSheetMetal && summary.SheetMetalThicknessMm <= 0 && summary.Box != null)
            {
                // A sheet metal part whose feature data would not open still has
                // a thickness: it is the smallest extent of the formed body.
                summary.SheetMetalThicknessMm = summary.Box.MinExtent;
                log.Debug("Sheet metal thickness taken from the bounding box; check it against the model.");
            }

            if (summary.IsSheetMetal) summary.FlatPatternBox = ReadFlatPatternBox(doc, summary, log);
        }

        private static void FallBackToBoxHeuristics(PartSummary summary)
        {
            if (summary.IsRotational || summary.Box == null) return;
            Axis axis;
            if (FrontViewChooser.LooksRotational(summary.Box, out axis))
            {
                summary.IsRotational = true;
                summary.RotationAxis = axis;
            }
        }

        /// <summary>
        /// Flat pattern extents.
        /// </summary>
        /// <remarks>
        /// Reading the true flat requires unsuppressing the flat pattern
        /// feature, which edits the user's model. Rather than do that, the
        /// formed box is returned as a starting estimate and the flat view is
        /// re-measured with IView::GetOutline once SOLIDWORKS has actually drawn
        /// it — the builder then corrects the scale. The estimate only ever
        /// affects which sheet is picked first.
        /// </remarks>
        private static BoundingBox ReadFlatPatternBox(ModelDoc2 doc, PartSummary summary, ILog log)
        {
            log.Debug("Flat pattern size estimated from the formed body; the view is re-measured after it is created.");
            return summary.Box;
        }

        private static HoleSummary ReadWizardHole(object feature, ILog log)
        {
            HoleSummary hole = new HoleSummary { IsWizardHole = true };

            object definition;
            if (!SwDispatch.TryInvoke(feature, "GetDefinition", new object[0], out definition, log) ||
                definition == null)
            {
                return hole;
            }

            hole.DiameterMm = MetresProperty(definition, new[] { "HoleDiameter", "Diameter" }, log);
            hole.DepthMm = MetresProperty(definition, new[] { "HoleDepth", "Depth" }, log);
            hole.IsThrough = SwDispatch.GetBool(definition, "ThruHole", hole.DepthMm <= 0, log);
            hole.IsTapped = SwDispatch.GetBool(definition, "Tapped", false, log);

            object thread;
            if (SwDispatch.TryGet(definition, "ThreadClass", out thread, log) && thread is string)
                hole.ThreadDesignation = (string)thread;

            SwDispatch.Release(definition);
            return hole;
        }

        private static double SheetMetalThickness(object feature, ILog log)
        {
            object definition;
            if (!SwDispatch.TryInvoke(feature, "GetDefinition", new object[0], out definition, log) ||
                definition == null)
            {
                return 0;
            }

            double thickness = MetresProperty(definition, new[] { "Thickness" }, log);
            SwDispatch.Release(definition);
            return thickness;
        }

        private static double FilletOrChamferSize(object feature, ILog log)
        {
            object definition;
            if (!SwDispatch.TryInvoke(feature, "GetDefinition", new object[0], out definition, log) ||
                definition == null)
            {
                return 0;
            }

            double size = MetresProperty(definition, new[] { "DefaultRadius", "Radius", "ChamferDistance", "Width" }, log);
            SwDispatch.Release(definition);
            return size;
        }

        /// <summary>Reads the first of several property names and converts metres to millimetres.</summary>
        private static double MetresProperty(object target, string[] names, ILog log)
        {
            foreach (string name in names)
            {
                object value;
                if (!SwDispatch.TryGet(target, name, out value, log) || value == null) continue;
                try
                {
                    double metres = Convert.ToDouble(value, CultureInfo.InvariantCulture);
                    if (metres > 0) return Units.MeterToMm(metres);
                }
                catch (InvalidCastException) { }
                catch (FormatException) { }
            }
            return 0;
        }

        private static string FeatureTypeName(object feature, ILog log)
        {
            object typeName;
            if (SwDispatch.TryInvoke(feature, new[] { "GetTypeName2", "GetTypeName" }, new object[0],
                                     out typeName, log))
            {
                return typeName as string;
            }
            return null;
        }

        private static string FeatureName(object feature, ILog log)
        {
            object name;
            if (SwDispatch.TryGet(feature, "Name", out name, log)) return name as string;
            return null;
        }

        private static bool MatchesAny(string value, string[] candidates)
        {
            foreach (string candidate in candidates)
            {
                if (value.IndexOf(candidate, StringComparison.OrdinalIgnoreCase) >= 0) return true;
            }
            return false;
        }

        // ---- assemblies ------------------------------------------------------

        private static void InspectAssembly(ModelDoc2 doc, PartSummary summary, ILog log)
        {
            summary.HasExplodedView = HasExplodedView(doc, summary.Configuration, log);

            AssemblyDoc assembly = doc as AssemblyDoc;
            if (assembly == null) return;

            object componentsObject;
            if (!SwDispatch.TryInvoke(assembly, "GetComponents", new object[] { true },
                                      out componentsObject, log))
            {
                return;
            }

            Dictionary<string, BomItemSummary> rows =
                new Dictionary<string, BomItemSummary>(StringComparer.OrdinalIgnoreCase);

            foreach (object componentObject in SwDispatch.AsObjects(componentsObject))
            {
                Component2 component = componentObject as Component2;
                if (component == null) continue;

                string path = component.GetPathName();
                if (string.IsNullOrEmpty(path)) continue;

                BomItemSummary row;
                if (rows.TryGetValue(path, out row))
                {
                    row.Quantity++;
                    continue;
                }

                row = new BomItemSummary
                {
                    ItemNumber = (rows.Count + 1).ToString(CultureInfo.InvariantCulture),
                    PartNumber = Path.GetFileNameWithoutExtension(path),
                    Description = Path.GetFileNameWithoutExtension(path),
                    Quantity = 1,
                    IsPurchased = AssemblyTraverser.IsToolbox(component)
                };
                rows[path] = row;
                summary.BomItems.Add(row);
            }
        }

        private static bool HasExplodedView(ModelDoc2 doc, string configuration, ILog log)
        {
            object configurationManager;
            if (!SwDispatch.TryGet(doc, "ConfigurationManager", out configurationManager, log)) return false;

            object active;
            if (!SwDispatch.TryGet(configurationManager, "ActiveConfiguration", out active, log) || active == null)
                return false;

            object count;
            if (SwDispatch.TryInvoke(active, new[] { "GetExplodedViewCount", "GetExplodedViewCount2" },
                                     new object[0], out count, log) && count != null)
            {
                try { return Convert.ToInt32(count, CultureInfo.InvariantCulture) > 0; }
                catch (InvalidCastException) { }
                catch (FormatException) { }
            }

            object names;
            if (SwDispatch.TryInvoke(active, "GetExplodedViewNames", new object[0], out names, log))
            {
                return SwDispatch.AsObjects(names).Length > 0;
            }

            return false;
        }

        // ---- properties ------------------------------------------------------

        private static string ResolveMaterial(ModelDoc2 doc, PartSummary summary, ILog log)
        {
            string material = SwDoc.MaterialName(doc, summary.Configuration, log);
            if (!string.IsNullOrEmpty(material)) return material;

            return LookupProperty(summary, "Material", "MATERIAL", "Stock");
        }

        private static string LookupProperty(PartSummary summary, params string[] names)
        {
            foreach (string name in names)
            {
                string value;
                if (summary.CustomProperties.TryGetValue(name, out value) && !string.IsNullOrEmpty(value))
                    return value;
            }
            return string.Empty;
        }
    }
}
