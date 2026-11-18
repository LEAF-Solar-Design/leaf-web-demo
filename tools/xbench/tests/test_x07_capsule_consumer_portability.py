import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import x07_jsx_failure_capsule as capsule


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class ConsumerPortability(unittest.TestCase):
    def test_documented_relocation_recipe(self):
        guide = capsule.ROOT / 'tools/xbench/X07-capsule-consumer-portability.md'
        recipe = guide.read_text(encoding='utf-8').split('```python\n', 1)[1].split('\n```', 1)[0]
        result = subprocess.run([sys.executable, '-P', '-'], input=recipe.encode('utf-8'),
                                cwd=capsule.ROOT / 'server', capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        self.assertEqual(json.loads(result.stdout), {'strict': True, 'relocated_source': True, 'tamper_refused': True})
