import ctypes
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
import x07_jsx_failure_capsule as capsule


def running(pid):
    if sys.platform == 'win32':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    stat = Path('/proc') / str(pid) / 'stat'
    if stat.exists() and stat.read_text().split(') ', 1)[1].startswith('Z'):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@unittest.skipUnless(shutil.which('node'), 'node unavailable')
class CapsuleEarlyExit(unittest.TestCase):
    def test_parent_exit_retains_descendant_ownership_and_settles_pipes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            check = root / 'x07_parent_exit.cjs'
            check.write_text("const{spawn}=require('node:child_process');const c=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'inherit'});console.log('CHILD:'+c.pid);c.unref();process.exit(0)\n", encoding='utf-8')
            started = time.monotonic()
            report = capsule.run_check(check.name, root / 'capture.json', checks_root=root, timeout=5)
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual(report['exit_code'], 0)
            self.assertFalse(report['timed_out'])
            self.assertTrue(report['owned_processes_settled'])
            pid = int(report['stdout'].strip().split(':')[1])
            deadline = time.monotonic() + 2
            while running(pid) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(running(pid), 'owned descendant survived')
