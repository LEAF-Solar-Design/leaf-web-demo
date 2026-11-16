from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('report_fixtures', root / 'server/tests/test_x07_offline_acceptance_report.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class ReportStepIdentity(unittest.TestCase):
    def test_ambiguous_or_coerced_steps_are_unreadable(self):
        for fault in ['duplicate', 'contradiction', 'passed_string', 'assertion_string', 'boolean_id']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(source)
                if fault == 'duplicate': journey['steps'][6]['n'] = 1
                elif fault == 'contradiction': journey['steps'][0]['passed'] = False
                elif fault == 'passed_string': journey['steps'][0]['passed'] = 'true'
                elif fault == 'assertion_string': journey['steps'][0]['assertions']['fresh_proposal'] = 'false'
                else: journey['steps'][0]['n'] = True
                paths[0].write_text(json.dumps(journey), encoding='utf-8')
                output = StringIO()
                args = ['report', '--source-sha', fixtures.SOURCE, '--source-root', str(source),
                        '--journey', str(paths[0]), '--proof', str(paths[1])]
                with patch.object(sys, 'argv', args), redirect_stdout(output):
                    self.assertEqual(fixtures.report.main(), 2)
                self.assertEqual({step['state'] for step in json.loads(output.getvalue())['steps']}, {'UNREADABLE'})
