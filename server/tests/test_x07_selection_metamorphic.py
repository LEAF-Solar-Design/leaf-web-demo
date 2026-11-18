"""Seeded source checks with an independently written brute-force oracle."""
import copy
from pathlib import Path
import random
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_role_selection import select_modules


def oracle(rows, window):
    lo_x, lo_y, hi_x, hi_y = window
    return [row['handle'] for row in rows if row['name'].lower() == 'panel' and row['layer'].lower() == 'modules'
            and lo_x <= row['x'] <= hi_x and lo_y <= row['y'] <= hi_y]


class SelectionMetamorphicTests(unittest.TestCase):
    def setUp(self):
        rng = random.Random(707)
        self.rows = [{'name': 'Panel', 'layer': 'MODULES', 'x': rng.randint(-40, 40),
                      'y': rng.randint(-40, 40), 'handle': str(i)} for i in range(200)]
        self.window = [-10, -15, 20, 25]

    def selected(self, rows, window):
        result = select_modules({'inserts': rows}, block='PANEL', layer='modules', window=window)
        self.assertEqual(result['handles'], oracle(rows, window))
        self.assertEqual(result['count'], len(oracle(rows, window)))
        return result

    def test_translation_invariance_with_seeded_integer_offsets(self):
        original = self.selected(self.rows, self.window)
        for dx, dy in [(100, -200), (-1024, 1024), (0, 7)]:
            translated = [{**row, 'x': row['x'] + dx, 'y': row['y'] + dy} for row in self.rows]
            window = [self.window[0] + dx, self.window[1] + dy, self.window[2] + dx, self.window[3] + dy]
            self.assertEqual(self.selected(translated, window)['handles'], original['handles'])

    def test_row_permutation_preserves_count_and_selected_identity_set(self):
        first = self.selected(self.rows, self.window)
        for seed in [17, 29, 71]:
            rows = copy.deepcopy(self.rows); random.Random(seed).shuffle(rows)
            reordered = self.selected(rows, self.window)
            self.assertEqual(reordered['count'], first['count'])
            self.assertEqual(set(reordered['handles']), set(first['handles']))

    def test_inclusive_edges_and_corners_match_independent_membership(self):
        positions = [(-10, -15), (-10, 25), (20, -15), (20, 25), (-10, 0), (20, 0), (0, -15), (0, 25),
                     (-10.001, 0), (20.001, 0), (0, -15.001), (0, 25.001)]
        rows = [{'name': 'Panel', 'layer': 'MODULES', 'x': x, 'y': y, 'handle': str(i)} for i, (x, y) in enumerate(positions)]
        self.assertEqual(self.selected(rows, self.window)['handles'], list(map(str, range(8))))

    def test_unrelated_roles_never_change_membership_or_input(self):
        before = copy.deepcopy(self.rows)
        expected = self.selected(self.rows, self.window)
        extra = [{'name': 'Roof', 'layer': 'MODULES', 'x': 0, 'y': 0, 'handle': 'roof'},
                 {'name': 'Panel', 'layer': 'OBSTRUCTION', 'x': 0, 'y': 0, 'handle': 'other-layer'}]
        result = self.selected(self.rows + extra, self.window)
        self.assertEqual(result['handles'], expected['handles'])
        self.assertEqual(self.rows, before)
