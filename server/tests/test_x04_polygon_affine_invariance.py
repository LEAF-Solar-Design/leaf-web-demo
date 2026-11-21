"""X04 convexity is invariant under bounded nonsingular affine transforms."""
from copy import deepcopy
import pytest
from test_x04_minsert_arrays import selected


@pytest.mark.parametrize('matrix', [((1, 2), (0, 1)), ((1, 0), (-2, 1)),
    ((-1, 0), (0, 1)), ((0, 2), (0.5, 0)), ((0.125, 0), (0, 8)),
    ((2, 0.5), (-0.5, 1))])
@pytest.mark.parametrize('tilt', [False, True])
def test_affine_controls_preserve_exact_eligibility_and_highlights(matrix, tilt):
    assert matrix[0][0]*matrix[1][1] - matrix[0][1]*matrix[1][0] != 0
    convex = [(0, 0), (4, 0), (4, 2), (0, 2)]
    concave = [(0, 0), (4, 0), (1, 1), (0, 4)]
    def transform(points):
        transformed = []
        for x, y in points:
            a = matrix[0][0]*x + matrix[0][1]*y + 16
            b = matrix[1][0]*x + matrix[1][1]*y - 8
            transformed.append([a, b, 0.25*a + 0.5*b] if tilt else [a, b])
        return transformed
    intake = {'inserts': [{'handle': 'A'}], 'polylines': [
        {'handle': 'B', 'layer': 'convex', 'closed': True, 'pts': transform(convex)},
        {'handle': 'C', 'layer': 'concave', 'closed': True, 'pts': transform(concave)}]}
    before = deepcopy(intake)
    result = selected(intake)
    assert result['ok'] and result['result']['count'] == 2
    assert set(result['overlay']['highlight_handles']) == {'A', 'B'}
    assert {row['layer'] for row in result['result']['sets']} == {'0', 'convex'}
    assert intake == before
