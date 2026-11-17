import json
from pathlib import Path
import shutil
import tempfile
import unittest
import x07_jsx_failure_capsule as capsule


def genuine(root, body="process.stdout.write('OK');process.stderr.write('ERR')", limit=64):
    check = root / 'x07_consumer.cjs'
    check.write_bytes(body.encode('utf-8'))
    path = root / 'capture.json'
    return capsule.run_check(check.name, path, checks_root=root, timeout=5, stream_limit=limit), path


def encoded(value):
    return (json.dumps(value, ensure_ascii=True) + '\n').encode('utf-8')


def refuses(test, path, raw):
    path.write_bytes(raw)
    with test.assertRaises(ValueError):
        capsule.read_capture(path)
    test.assertEqual(path.read_bytes(), raw)


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerJson(unittest.TestCase):
    def test_duplicates_nonfinite_and_trailing_objects_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            value, path = genuine(Path(directory))
            raw = encoded(value)
            self.assertEqual(capsule.read_capture(path), value)
            cases = [b'{"extra":1,"extra":2,', b'{"extra":1,"ex\\u0074ra":2,',
                     b'{"extra":{"a":1,"\\u0061":2},', b'{"extra":NaN,',
                     b'{"extra":Infinity,', b'{"extra":-Infinity,', b'{"extra":1e9999,']
            for prefix in cases:
                with self.subTest(prefix=prefix):
                    refuses(self, path, prefix + raw[1:])
            refuses(self, path, raw.rstrip() + b' {}\n')
            path.write_bytes(raw)
            self.assertEqual(capsule.read_capture(path), value)
