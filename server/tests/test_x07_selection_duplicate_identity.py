import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x07_role_selection import select_modules, SelectionError


def oracle(rows):
    # Identity validation precedes window membership. Missing IDs are not merged.
    seen, selected = set(), []
    for index, row in enumerate(rows):
        if row['name'].lower() != 'panel' or row['layer'].lower() != 'modules': continue
        handle = row.get('handle')
        if isinstance(handle, str) and handle:
            if handle.lower() in seen: raise ValueError('ambiguous duplicate identity')
            seen.add(handle.lower())
        if 0 <= row['x'] <= 2 and 0 <= row['y'] <= 2: selected.append(index)
    return selected


class DuplicateIdentityTests(unittest.TestCase):
    def select(self, rows):
        return select_modules({'inserts': rows}, block='Panel', layer='MODULES', window=[0, 0, 2, 2])

    def row(self, **extra):
        return {'name': 'Panel', 'layer': 'MODULES', 'x': 1, 'y': 1, **extra}

    def test_repeated_handle_identical_or_conflicting_refuses_no_inflated_count(self):
        for row in [self.row(handle='A'), self.row(handle='a', x=100)]:
            rows = [self.row(handle='A'), row]
            with self.assertRaises(ValueError): oracle(rows)
            with self.assertRaisesRegex(SelectionError, 'duplicate matching INSERT handle'): self.select(rows)

    def test_coincident_distinct_handles_are_distinct_inserts(self):
        rows = [self.row(handle='A'), self.row(handle='B')]
        self.assertEqual(self.select(rows)['insert_indices'], oracle(rows))
        self.assertEqual(self.select(rows)['count'], 2)

    def test_missing_empty_and_nonstring_ids_count_records_without_coordinate_dedup(self):
        rows = [self.row(), self.row(handle=''), self.row(handle=None), self.row(handle=7)]
        self.assertEqual(self.select(rows)['insert_indices'], oracle(rows))
        self.assertEqual(self.select(rows)['count'], 4)

    def test_unrelated_role_handle_reuse_is_ignored_and_input_preserved(self):
        rows = [self.row(handle='A'), self.row(handle='A', name='Roof'), self.row(handle='A', layer='OTHER')]
        before = copy.deepcopy(rows)
        self.assertEqual(self.select(rows)['insert_indices'], oracle(rows))
        self.assertEqual(rows, before)
