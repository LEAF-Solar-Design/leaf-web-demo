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
