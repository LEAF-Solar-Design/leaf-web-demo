from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from threading import Barrier, Thread
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_x07_offline_acceptance_report as fixtures

report = fixtures.report


def strict_output(raw):
    def unique(pairs):
        value = {}
        for key, child in pairs:
            if key in value: raise ValueError('duplicate output member')
            value[key] = child
        return value
    def bad_constant(value):
        raise ValueError('nonfinite output')
    text = raw.decode('utf-8')
    value, end = json.JSONDecoder(object_pairs_hook=unique, parse_constant=bad_constant).raw_decode(text)
    if text[end:].strip() or not raw.endswith(b'\n'):
        raise ValueError('output must end in real JSON whitespace newline')
    return value


class ReportCliIntegrity(unittest.TestCase):
    def call(self, root, paths, extra=()):
        args = ['report', '--source-sha', fixtures.SOURCE, '--source-root', str(root),
                '--journey', str(paths[0]), '--proof', str(paths[1]), *extra]
        output = StringIO()
        with patch.object(sys, 'argv', args), redirect_stdout(output):
            code = report.main()
        return code, strict_output(output.getvalue().encode('utf-8'))

    def test_real_cli_stdout_and_out_are_strict_json_with_newlines(self):
        script = Path(__file__).resolve().parents[2] / 'tools/xbench/x07_offline_acceptance_report.py'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, _, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
            output = root / 'published.json'
            for success in [True, False]:
                if not success: paths[1].unlink()
                result = subprocess.run([sys.executable, str(script), '--source-sha', fixtures.SOURCE,
                    '--source-root', str(root), '--journey', str(paths[0]), '--proof', str(paths[1]),
                    '--out', str(output)], capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0 if success else 2, result.stderr[:300])
                stdout = strict_output(result.stdout)
                self.assertEqual(stdout, strict_output(output.read_bytes()))
                expected = 'SYNTHETIC_PASS' if success else 'UNREADABLE'
                self.assertEqual({step['state'] for step in stdout['steps']}, {expected})

    def test_required_assertions_cannot_be_replaced_by_unrelated_truths(self):
        for number in range(1, 8):
            with self.subTest(step=number), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
                journey['steps'][number - 1]['assertions'] = {'unrelated_true': True}
                paths[0].write_text(json.dumps(journey), encoding='utf-8')
                code, value = self.call(root, paths)
                self.assertEqual(code, 2)
                self.assertEqual(value['error'], 'MISSING_OR_UNKNOWN_ASSERTIONS')

    def test_duplicate_escaped_and_nonfinite_receipt_json_refuses(self):
        for location in ['top', 'inventory', 'assertion']:
            for kind in ['duplicate', 'escaped_duplicate', 'NaN', 'Infinity']:
                with self.subTest(location=location, kind=kind), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
                    raw = json.dumps(journey)
                    key = {'top': 'source_sha', 'inventory': 'server/fixture.py', 'assertion': 'fresh_proposal'}[location]
                    token = json.dumps(key) + ':'
                    if kind in ['duplicate', 'escaped_duplicate']:
                        alias = json.dumps(key) if kind == 'duplicate' else '"\\u%04x%s"' % (ord(key[0]), key[1:])
                        raw = raw.replace(token, alias + ':false,' + token, 1)
                    else:
                        # An otherwise ignored field still must obey JSON grammar.
                        if location == 'top': raw = raw[:-1] + ',"unused":' + kind + '}'
                        else: raw = raw.replace(token, '"nonfinite":' + kind + ',' + token, 1)
                    paths[0].write_text(raw, encoding='utf-8')
                    code, value = self.call(root, paths)
                    self.assertEqual(code, 2)
                    self.assertEqual({step['state'] for step in value['steps']}, {'UNREADABLE'})

    def test_exact_source_budgets_and_oversize_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, journey, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
            sizes = [(root / name).stat().st_size for name in journey['files']]
            exact = ['--max-file-bytes', str(max(sizes)), '--max-source-bytes', str(3 * sum(sizes))]
            self.assertEqual(self.call(root, paths, exact)[0], 0)
            for limits in [['--max-file-bytes', str(max(sizes) - 1)],
                           ['--max-source-bytes', str(3 * sum(sizes) - 1)]]:
                code, value = self.call(root, paths, limits)
                self.assertEqual(code, 2)
                self.assertEqual(value['error'], 'SOURCE_READ_BUDGET_EXCEEDED')

    def test_mutation_after_last_receipt_verification_is_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, _, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
            rendezvous = Barrier(2)
            def mutate():
                rendezvous.wait(timeout=10)
                (root / 'server/fixture.py').write_bytes(b'changed after verification')
                rendezvous.wait(timeout=10)
            worker = Thread(target=mutate)
            worker.start()
            original = report.verify_files
            calls = 0
            def synchronized(*args, **kwargs):
                nonlocal calls
                original(*args, **kwargs)
                calls += 1
                if calls == 2:
                    rendezvous.wait(timeout=10)
                    rendezvous.wait(timeout=10)
            try:
                with patch.object(report, 'verify_files', synchronized):
                    code, value = self.call(root, paths)
                self.assertEqual(code, 2)
                self.assertEqual(value['error'], 'SOURCE_CHANGED_DURING_AGGREGATION')
            finally:
                worker.join(timeout=10)
            self.assertFalse(worker.is_alive())

    def test_atomic_publication_failures_preserve_old_report_and_remove_temps(self):
        for fault in ['partial_write', 'replace']:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths, _, _ = fixtures.OfflineAcceptanceReport().fixtures(root)
                output = root / 'report.json'
                old = b'{"previous":"complete"}\n'
                output.write_bytes(old)
                original = report.tempfile.NamedTemporaryFile
                @contextmanager
                def interrupted(*args, **kwargs):
                    with original(*args, **kwargs) as stream:
                        def partial(data):
                            stream.write(data[:10])
                            raise OSError('injected interrupted write')
                        yield SimpleNamespace(name=stream.name, write=partial)
                control = patch.object(report.tempfile, 'NamedTemporaryFile', interrupted) if fault == 'partial_write' else patch.object(report.os, 'replace', side_effect=OSError('injected replace failure'))
                with control:
                    code, value = self.call(root, paths, ['--out', str(output)])
                self.assertEqual(code, 2)
                self.assertEqual(value['error'], 'REPORT_PUBLICATION_FAILED')
                self.assertEqual(output.read_bytes(), old)
                self.assertEqual(list(root.glob('.report.json.x07-*')), [])
