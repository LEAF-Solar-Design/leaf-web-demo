"""Synthetic controls for the X07 initial blocker grader."""

import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('x07', ROOT / 'benches' / 'x07.py')
x07 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(x07)


class DiscoveryEvidenceTests(unittest.TestCase):
    fixture = {'module_block': 'SYNTHETIC PANEL', 'module_layer': 'SYNTHETIC MODULES'}

    def check(self, text='', selected=True, source_matches=True, settled=True):
        return x07.first_choice_step(text, self.fixture, selected=selected,
                                     source_matches=source_matches, settled=settled, evidence='synthetic')

    def test_missing_names_on_correct_selected_source_is_failure(self):
        step = self.check('A loaded drawing without a role proposal')
        self.assertEqual(step.verdict, 'FAIL')
        self.assertEqual(step.observed['blocker'], 'ROLE_PROPOSAL_NOT_VISIBLE')

    def test_unselected_drawing_cannot_establish_failure(self):
        self.assertEqual(self.check(selected=False).verdict, 'UNREADABLE')

    def test_source_drift_cannot_establish_failure(self):
        self.assertEqual(self.check(source_matches=False).verdict, 'UNREADABLE')

    def test_unsettled_observation_cannot_establish_failure(self):
        self.assertEqual(self.check(settled=False).verdict, 'UNREADABLE')

    def test_late_proposal_is_observed_before_deciding(self):
        class Page:
            values = iter(['loading', 'SYNTHETIC PANEL on SYNTHETIC MODULES',
                           'SYNTHETIC PANEL on SYNTHETIC MODULES'])
            def wait_for_load_state(self, *args, **kwargs): pass
            def locator(self, *args): return self
            def inner_text(self): return next(self.values)
            def wait_for_timeout(self, *args): pass
        self.assertEqual(self.check(x07.settled_text(Page())).verdict, 'UNREADABLE')

    def test_continuously_changing_surface_is_unreadable(self):
        class Page:
            def wait_for_load_state(self, *args, **kwargs): pass
            def locator(self, *args): return self
            def inner_text(self): return object()
            def wait_for_timeout(self, *args): pass
        with self.assertRaises(ValueError):
            x07.settled_text(Page())

    def test_names_alone_cannot_certify_proposal_or_confirmation(self):
        self.assertEqual(self.check('Synthetic Panel on Synthetic Modules').verdict, 'UNREADABLE')

    def test_one_visible_name_does_not_satisfy_both_names(self):
        step = self.check('SYNTHETIC PANEL')
        self.assertEqual(step.observed['missing_first_choice_names'], ['module_layer'])


if __name__ == '__main__':
    unittest.main()
