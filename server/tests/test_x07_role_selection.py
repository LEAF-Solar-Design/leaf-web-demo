import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_role_selection import SelectionError, select_modules


class SelectionTests(unittest.TestCase):
    def select(self, rows, window=(0, 0, 2, 2)):
        return select_modules({'inserts': rows}, block='Panel', layer='MODULES', window=window)

    def row(self, x, y, **extra):
        return {'name': 'Panel', 'layer': 'MODULES', 'x': x, 'y': y, **extra}

    def test_three_repeated_copies_pick_exactly_one(self):
        rows = [self.row(x + offset, y) for offset in (0, 10, 20)
                for x, y in ((0, 0), (1, 1), (2, 2))]
        self.assertEqual(self.select(rows, (10, 0, 12, 2))['insert_indices'], [3, 4, 5])

    def test_inclusive_edges_and_no_implicit_tolerance(self):
        rows = [self.row(x, y) for x, y in ((0, 0), (0, 2), (2, 0), (2, 2),
                                            (-0.000001, 1), (2.000001, 1))]
        self.assertEqual(self.select(rows)['count'], 4)

    def test_case_insensitive_symbols_but_both_must_match(self):
        rows = [self.row(1, 1, name='pAnEl', layer='modules'), self.row(1, 1, name='Other'),
                self.row(1, 1, layer='Other')]
        self.assertEqual(self.select(rows)['count'], 1)

    def test_point_window_and_ignored_transform(self):
        self.assertEqual(self.select([self.row(1, 1, rot=5, scale=[9, 9], z=200)],
                                     (1, 1, 1, 1))['count'], 1)

    def test_invalid_windows_and_matching_origins_refuse(self):
        for window in ((2, 0, 1, 2), (0, 0, float('inf'), 2), (0, 0, True, 2), (0, 0, 2)):
            with self.subTest(window=window), self.assertRaises(SelectionError):
                self.select([], window)
        for value in (float('nan'), float('inf'), True, '1', None):
            with self.subTest(value=value), self.assertRaises(SelectionError):
                self.select([self.row(value, 1)])

    def test_no_input_mutation_or_output_alias(self):
        intake = {'inserts': [self.row(1, 1, handle='A')]}
        window = [0, 0, 2, 2]
        before = copy.deepcopy(intake)
        result = select_modules(intake, block='Panel', layer='MODULES', window=window)
        result['design_window'][0] = 90
        self.assertEqual(window, [0, 0, 2, 2])
        self.assertEqual(intake, before)
