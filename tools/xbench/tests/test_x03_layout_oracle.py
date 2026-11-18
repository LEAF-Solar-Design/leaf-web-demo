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
