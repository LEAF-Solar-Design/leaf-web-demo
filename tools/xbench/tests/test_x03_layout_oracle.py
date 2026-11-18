import pytest
from shapely.geometry import Polygon, box

from benches.x03_layout_oracle import layout_quality


@pytest.mark.parametrize("width,setback", [(10, 0), (12, 1), (16, 2)])
def test_panels_fit_usable_area(width, setback):
    hole = box(5, 5, 7, 7)
    area = Polygon(box(0, 0, width, width).exterior.coords, [hole.exterior.coords])
    first = box(setback, setback, setback+1, setback+1)
    adjacent = box(setback+1, setback, setback+2, setback+1)
    assert layout_quality([first, adjacent], area, setback) == "PASS"
    assert layout_quality([first, first], area, setback) == "FAIL"
    assert layout_quality([hole], area, setback) == "FAIL"
    assert layout_quality([first], area, setback+0.01) == "FAIL"
    assert layout_quality([None], area, setback) == "INCOMPLETE"


@pytest.mark.parametrize("angle,pitch", [(0, 2), (90, 3), (32, 4)])
def test_rotated_rows_and_pitch(angle, pitch):
    from shapely.affinity import rotate
    from benches.x03_layout_oracle import row_layout_quality
    rows = [[rotate(box(x, y, x+2, y+1), angle, origin=(0, 0)) for x in (0, 3)] for y in (0, pitch)]
    def grade(orientation, minimum):
        return row_layout_quality(rows, box(-20, -20, 20, 20), 0, orientation=orientation,
                                  row_axis=angle, min_pitch=minimum)
    assert grade(angle, pitch) == "PASS"
    assert grade(angle+90, pitch) == "FAIL"
    assert grade(angle, pitch+0.01) == "FAIL"
    assert grade(None, pitch) == "INCOMPLETE"


@pytest.mark.parametrize("unit,panel_start,panel_end", [
    ("m", 0.3048, 0.6096), ("mm", 304.8, 609.6), ("ft", 1, 2),
])
def test_mixed_unit_layout(unit, panel_start, panel_end):
    from benches.x03_layout_oracle import measured_layout_quality
    # One square foot, one foot from the boundary, on a site recorded in metres.
    area = box(0, 0, 1, 1)
    panels = [box(panel_start, panel_start, panel_end, panel_end)]
    def grade(distance, units):
        return measured_layout_quality(panels, area, distance, panel_unit=units,
                                       area_unit="m", setback_unit="ft")
    assert grade(1, unit) == "PASS"
    assert grade(1.01, unit) == "FAIL"
    assert grade(1, "kg") == "FAIL"
    assert grade(1, None) == "INCOMPLETE"


@pytest.mark.parametrize("panel,ballast,share,local_mass", [(40, 20, 0.5, 30), (30, 10, 0.75, 30), (12, 8, 1, 20)])
def test_roof_load_on_individual_zones(panel, ballast, share, local_mass):
    from benches.x03_layout_oracle import zone_load_quality
    zones = {"edge": dict(area=2, limit=local_mass/2), "center": dict(area=100, limit=20)}
    def check(limits, ballast_record):
        return zone_load_quality({"rack": panel}, ballast_record,
                                 {"rack": {"edge": share, "center": 1-share}}, limits)
    assert check(zones, {"rack": ballast}) == "PASS"
    zones["edge"]["limit"] -= 0.01
    assert check(zones, {"rack": ballast}) == "FAIL"
    assert check(zones, {}) == "INCOMPLETE"
