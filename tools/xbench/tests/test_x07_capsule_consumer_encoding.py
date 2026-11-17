from pathlib import Path
import shutil
import tempfile
import unittest
import x07_jsx_failure_capsule as capsule
from .test_x07_capsule_consumer_json import genuine, encoded, refuses


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerEncoding(unittest.TestCase):
    def test_invalid_utf8_bom_surrogates_and_unicode_control(self):
        with tempfile.TemporaryDirectory() as directory:
            value, path = genuine(Path(directory), "process.stdout.write('雪 café 🌱')")
            raw = encoded(value)
            self.assertEqual(capsule.read_capture(path), value)
            for prefix in (b'\xff', b'\xe2\x82', b'\xef\xbb\xbf'):
                with self.subTest(prefix=prefix): refuses(self, path, prefix + raw)
            for addition in (b'"extra":"\\ud800",', b'"extra":"\\udfff",', b'"\\ud800":1,'):
                with self.subTest(addition=addition): refuses(self, path, b'{' + addition + raw[1:])
            path.write_bytes(raw); self.assertEqual(capsule.read_capture(path)['stdout'], '雪 café 🌱')
