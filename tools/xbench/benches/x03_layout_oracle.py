"""Grade recorded planar panel footprints in drawing units."""
from benches.x03_quality import finite_measure


def layout_quality(panels, usable_area, setback):
    if usable_area is None or setback is None or not panels:
        return "INCOMPLETE"
    for shape in (usable_area, *panels):
        if shape is None or getattr(shape, "is_empty", True):
            return "INCOMPLETE"
        if shape.geom_type not in ("Polygon", "MultiPolygon") or not shape.is_valid:
            return "FAIL"
    if not finite_measure(setback) or setback < 0:
        return "FAIL"
    for i, panel in enumerate(panels):
        if not usable_area.covers(panel) or panel.distance(usable_area.boundary) < setback:
            return "FAIL"
        if any(panel.relate_pattern(other, "T********") for other in panels[:i]):
            return "FAIL"
    return "PASS"
