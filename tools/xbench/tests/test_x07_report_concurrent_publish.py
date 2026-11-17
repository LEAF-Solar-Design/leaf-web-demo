from pathlib import Path
import importlib.util
import json
import tempfile
from threading import Barrier, Thread, current_thread
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('publish_fixtures', root / 'server/tests/test_x07_offline_acceptance_report.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
report = fixtures.report


class ReportConcurrentPublish(unittest.TestCase):
    def test_two_complete_writers_and_one_replacement_failure(self):
        for fail_second in [False, True]:
            with self.subTest(fail_second=fail_second), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                paths, journey, proof = fixtures.OfflineAcceptanceReport().fixtures(source)
                second_journey, second_proof = source / 'journey-b.json', source / 'proof-b.json'
                journey['steps'][0]['passed'] = False
                journey['steps'][0]['assertions']['fresh_proposal'] = False
                second_journey.write_text(json.dumps(journey), encoding='utf-8')
                second_proof.write_text(json.dumps(proof), encoding='utf-8')
                inputs = [*paths, second_journey, second_proof]
                before = {path: path.read_bytes() for path in inputs}
                pairs = [paths, [second_journey, second_proof]]
                expected = [report.aggregate(pair[0], [pair[1]], fixtures.SOURCE, source) for pair in pairs]
                output = source / 'report.json'
                output.write_bytes(b'{"previous":true}\n')
                rendezvous = Barrier(2)
                replace = report.os.replace
                errors = {}
                def synchronized_replace(temporary, destination):
                    rendezvous.wait(timeout=10)
                    if fail_second and current_thread().name == 'writer-b':
                        raise OSError('injected writer-b replacement failure')
                    replace(temporary, destination)
                def writer(index):
                    try:
                        value = report.aggregate(pairs[index][0], [pairs[index][1]], fixtures.SOURCE, source)
                        report.check_output_path(output, set(inputs))
                        report.atomic_publish(output, json.dumps(value) + '\n')
                    except Exception as error:
                        errors[index] = error
                threads = [Thread(target=writer, args=(index,), name='writer-a' if index == 0 else 'writer-b') for index in range(2)]
                with patch.object(report.os, 'replace', synchronized_replace):
                    for thread in threads: thread.start()
                    for thread in threads: thread.join(timeout=15)
                    rendezvous.abort()
                    for thread in threads: thread.join(timeout=2)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                if fail_second:
                    self.assertEqual(set(errors), {1})
                    self.assertIsInstance(errors[1], OSError)
                    self.assertEqual(json.loads(output.read_bytes()), expected[0])
                else:
                    self.assertEqual(errors, {})
                    self.assertIn(json.loads(output.read_bytes()), expected)
                self.assertTrue(output.read_bytes().endswith(b'\n'))
                self.assertEqual(list(source.glob('.report.json.x07-*')), [])
                self.assertTrue(all(path.read_bytes() == content for path, content in before.items()))
