import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class BridgeWorkerImports(unittest.TestCase):
    def test_safe_path_isolates_real_root_platform_shadow(self):
        root = Path(__file__).resolve().parents[3]
        code = "import platform;print(platform.system())"
        unsafe = subprocess.run([sys.executable, '-B', '-c', code], cwd=root, capture_output=True, text=True, timeout=10)
        safe = subprocess.run([sys.executable, '-B', '-P', '-c', code], cwd=root, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(unsafe.returncode, 0, 'negative control must exercise the real repo-root platform package')
        self.assertEqual(safe.returncode, 0, safe.stderr)
        self.assertNotEqual(safe.stdout.strip(), '')
        self.assertEqual(safe.stderr, '')

    @unittest.skipUnless(shutil.which('node'), 'node unavailable')
    def test_complete_embedded_python_worker_uses_safe_import_path(self):
        root = Path(__file__).resolve().parents[3]
        helper = Path(__file__).with_name('x07_adapter_jsx_journey.cjs')
        extractor = "const s=require('node:fs').readFileSync(process.argv[1],'utf8');const a=s.indexOf('const python = ')+15;const b=s.indexOf('const intake =',a);process.stdout.write(JSON.stringify(Function('return ('+s.slice(a,b).trim()+')')()))"
        extraction = subprocess.run([shutil.which('node'), '-e', extractor, str(helper)], capture_output=True, text=True, timeout=10)
        self.assertEqual(extraction.returncode, 0, extraction.stderr)
        code = json.loads(extraction.stdout)
        with tempfile.TemporaryDirectory() as directory:
            context = {'customer_key': 'synthetic', 'project_key': 'worker-import', 'drawing_key': 'drawing'}
            database = str(Path(directory) / 'profiles.sqlite')
            request = {'database': database, 'bound_context': context, 'context': context, 'op': 'identity', 'value': None,
                       'intake': {'inserts': [{'name': 'Panel', 'layer': 'M', 'x': 0, 'y': 0}]},
                       'copies': [{'block': 'Panel', 'layer': 'M', 'design_window': [0, 0, 0, 0], 'oracle_count': 1}]}
            safe = subprocess.run([sys.executable, '-B', '-P', '-X', 'utf8', '-c', code], input=json.dumps(request),
                                  cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(safe.returncode, 0, safe.stderr)
            result = json.loads(safe.stdout)
            self.assertTrue(result['ok'], result)
            self.assertEqual(Path(result['result']['database']), Path(database))
            self.assertEqual(Path(result['result']['cwd']), root)
            self.assertEqual(safe.stderr, '')
