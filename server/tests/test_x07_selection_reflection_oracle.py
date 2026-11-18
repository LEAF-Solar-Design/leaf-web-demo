from pathlib import Path
import random
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_role_selection import select_modules


class SelectionReflectionOracle(unittest.TestCase):
    def test_axis_swaps_and_reflections_keep_exact_cell_oracle(self):
        randomizer = random.Random(407)
        points = [(x, y, True) for x, y in [(-2, -1), (-2, 4), (3, -1), (3, 4), (0, 0), (0, -5), (0, 5)]]
        points += [(randomizer.randint(-20, 20), randomizer.randint(-20, 20), index % 3 != 0) for index in range(200)]
        windows = [[-2, -1, 3, 4], [0, 0, 0, 0], [0, -5, 0, 5], [-5, 0, 5, 0]]
        for _ in range(4):
            xs = sorted([randomizer.randint(-20, 20), randomizer.randint(-20, 20)])
            ys = sorted([randomizer.randint(-20, 20), randomizer.randint(-20, 20)])
            windows.append([xs[0], ys[0], xs[1], ys[1]])
        for window in windows:
            cells = {(x, y) for x in range(window[0], window[2] + 1) for y in range(window[1], window[3] + 1)}
            expected = [index for index, (x, y, matching) in enumerate(points) if matching and (x, y) in cells]
            for swap in [False, True]:
                for sx, sy in [(1, 1), (-1, 1), (1, -1), (-1, -1)]:
                    with self.subTest(window=window, swap=swap, sx=sx, sy=sy):
                        def transform(x, y): return (sx * y, sy * x) if swap else (sx * x, sy * y)
                        corners = [transform(x, y) for x in [window[0], window[2]] for y in [window[1], window[3]]]
                        bounds = [min(x for x, y in corners), min(y for x, y in corners), max(x for x, y in corners), max(y for x, y in corners)]
                        rows = []
                        for index, (x, y, matching) in enumerate(points):
                            tx, ty = transform(x, y)
                            rows.append({'name': 'pAnEl' if matching else 'Other', 'layer': 'M', 'handle': format(index + 1, 'X'), 'x': tx, 'y': ty})
                        selected = select_modules({'inserts': rows}, block='Panel', layer='m', window=bounds)
                        self.assertEqual(selected['insert_indices'], expected)
                        self.assertEqual(selected['count'], len(expected))
