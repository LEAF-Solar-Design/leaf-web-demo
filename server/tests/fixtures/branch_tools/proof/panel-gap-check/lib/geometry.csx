using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

double EdgeGapInches(Extents3d ext)
{
    // v1 reference helper: distance from the extents' min corner to the WCS origin.
    // Stands in for the roof-boundary offset query; the contract gate compiles it,
    // the MVP host (P3) supplies the real boundary source.
    return ext.MinPoint.DistanceTo(Point3d.Origin);
}
