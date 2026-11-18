from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import x07_jsx_failure_capsule as capsule


class CapsulePreflight(unittest.TestCase):
    def test_missing_node_unreadable_source_and_spawn_failure_leave_no_failed_artifact(self):
        for fault in ('node', 'source', 'spawn'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                check = root / 'x07_preflight.cjs'
                check.write_text("console.log('synthetic')\n", encoding='utf-8')
                output = root / 'new.json'
                original_read, original_popen, original_run = Path.read_bytes, subprocess.Popen, capsule.run_check
                launches = []

                def read(path):
                    if fault == 'source' and path == check:
                        raise PermissionError('PRIVATE_SOURCE')
                    return original_read(path)

                def popen(command, *args, **kwargs):
                    if str(command[0]) != 'git':
                        launches.append(command)
                        raise OSError('PRIVATE_SPAWN')
                    return original_popen(command, *args, **kwargs)

                def run(name, target, **kwargs):
                    return original_run(name, target, checks_root=root, **kwargs)

                result = io.StringIO()
                node = shutil.which('node') or 'synthetic-node'
                with patch.object(capsule.shutil, 'which', return_value=None if fault == 'node' else node), patch.object(Path, 'read_bytes', read), patch.object(subprocess, 'Popen', side_effect=popen), patch.object(capsule, 'run_check', side_effect=run), patch.object(sys, 'argv', ['capsule', '--check', check.name, '--out', str(output)]), redirect_stdout(result):
                    self.assertEqual(capsule.main(), 2)
                self.assertEqual(json.loads(result.getvalue()), {'schema': 'leaf.x07.jsx-failure-capsule.v1', 'error': 'CAPSULE_UNAVAILABLE'})
                self.assertEqual(len(launches), int(fault == 'spawn'))
                self.assertFalse(output.exists())
                self.assertEqual(list(root.iterdir()), [check])

    def test_existing_output_is_preserved_before_child_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / 'x07_existing.cjs'; check.write_text('synthetic', encoding='utf-8')
            output = root / 'existing.json'; output.write_bytes(b'OLD_CAPTURE')
            with patch.object(capsule.shutil, 'which', return_value='synthetic-node'), patch.object(capsule, 'snapshot', return_value={}):
                with self.assertRaises(FileExistsError):
                    capsule.run_check(check.name, output, checks_root=root)
            self.assertEqual(output.read_bytes(), b'OLD_CAPTURE')
