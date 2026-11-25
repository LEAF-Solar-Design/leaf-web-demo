"""Deterministic scratch-only controls for bounded source custody reads.

These controls exercise persistent mutations at known read boundaries. Finite
metadata/digest observations cannot detect every transient restored-byte event
or changes after the final observation; this is no concurrency-security proof.
Run directly with Python; no child process, checker, or timing race is needed.
"""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import verify_report_sources as custody


class ReadHook:
    """Delegate a real file, firing once after a chosen nonempty read chunk."""

    def __init__(self, stream, callback, chunk_number):
        self.stream = stream
        self.callback = callback
        self.chunk_number = chunk_number
        self.chunks = 0
        self.reads = 0
        self.requested = 0

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def fileno(self):
        return self.stream.fileno()

    def seek(self, *args):
        return self.stream.seek(*args)

    def read(self, size):
        self.reads += 1
        self.requested += size
        # The target has exactly two HASH_BUFFER chunks. Fail deterministically
        # on extra passes/reads rather than letting a retry regression hang.
        if self.reads > 6 or self.requested > 4 * custody.HASH_BUFFER + 2:
            raise AssertionError("source read exceeded two bounded passes")
        raw = self.stream.read(size)
        if raw:
            self.chunks += 1
            if self.chunks == self.chunk_number:
                self.callback()
        return raw


class SourceReadMutationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="x01 read mutation ")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name) / "sources"
        self.root.mkdir()
        self.report_path = Path(self.scratch.name) / "report.json"
        self.target = self.root / custody.VECTORS[0]
        self.original = b"a" * custody.HASH_BUFFER + b"b" * custody.HASH_BUFFER
        self.assertLessEqual(len(self.original), 256 * 1024)
        report = custody.validator.ValidatorTests().report("FAIL")
        report["provenance"] = []
        for name, filename in custody.SOURCES.items():
            raw = self.original if filename == self.target.name else b"scratch source\n"
            (self.root / filename).write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            report["provenance"].append(dict(
                name=name, sha256_before=digest, sha256_after=digest))
        self.report_bytes = json.dumps(report).encode("utf-8")
        self.report_path.write_bytes(self.report_bytes)
        self.unrelated = self.root / "unrelated.txt"
        self.unrelated.write_bytes(b"unrelated sentinel")
        # Checkout inputs are only observed, never used as mutation targets.
        self.checkout = {Path(custody.__file__).with_name(filename):
                         Path(custody.__file__).with_name(filename).read_bytes()
                         for filename in custody.SOURCES.values()}
        self.untouched = {path: path.read_bytes() for path in self.root.iterdir()
                          if path != self.target}

    def assert_untouched(self):
        self.assertEqual(self.report_path.read_bytes(), self.report_bytes)
        for path, raw in {**self.checkout, **self.untouched}.items():
            self.assertEqual(path.read_bytes(), raw, str(path))

    @contextlib.contextmanager
    def hooked_read(self, callback, chunk_number=1):
        original_open = Path.open
        hooks = []

        def open_source(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            if path == self.target and mode == "rb":
                hook = ReadHook(stream, callback, chunk_number)
                hooks.append(hook)
                return hook
            return stream

        with patch.object(Path, "open", open_source):
            yield hooks

    def probe(self, verdict, code, callback=None, chunk_number=1):
        # Exercise the same public CLI entry point without launching a child.
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("child launch")))
            stack.enter_context(patch("os.system", side_effect=AssertionError("shell launch")))
            hooks = (stack.enter_context(self.hooked_read(callback, chunk_number))
                     if callback is not None else None)
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(custody.main([
                str(self.report_path), "--source-root", str(self.root)]), code)
            result = json.loads(output.getvalue())
        if hooks is not None:
            self.assertEqual(len(hooks), 1)
            self.assertGreaterEqual(hooks[0].chunks, chunk_number)
        self.assertEqual(result["verdict"], verdict, result)
        self.assertTrue(result["valid"])
        self.assertEqual(result["observed_verdict"], "FAIL")
        self.assertEqual(result["schema"], custody.SCHEMA)
        self.assert_untouched()
        return result

    def assert_unstable(self, result):
        self.assertEqual(result["diagnostics"], [dict(
            name="UNREADABLE", reason="SOURCE_UNSTABLE",
            input="vector:" + self.target.name)])
        source = next(item for item in result["sources"]
                      if item["input"] == "vector:" + self.target.name)
        self.assertIsNone(source["sha256_current"])
        self.assertEqual(source["vector"], self.target.name)

    def change_first_byte(self, restore_mtime=False):
        before = self.target.stat()
        with self.target.open("r+b") as stream:
            stream.write(b"z")
        if restore_mtime:
            os.utime(self.target, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(self.target.stat().st_mtime_ns, before.st_mtime_ns)

    def test_stable_digest_and_custody(self):
        self.assertEqual(custody._hash_source(self.target),
                         hashlib.sha256(self.original).hexdigest())
        self.assertEqual(self.probe("PASS", 0)["diagnostics"], [])
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_stable_with_different_cross_api_ctime(self):
        fstat = os.fstat

        def stream_stat(fd):
            stat = fstat(fd)
            return SimpleNamespace(
                st_dev=stat.st_dev, st_ino=stat.st_ino, st_size=stat.st_size,
                st_mtime_ns=stat.st_mtime_ns, st_ctime_ns=stat.st_ctime_ns + 1)

        # Force the Windows API discrepancy on every host. Each API's own
        # baseline stays stable, so all six sources must still agree.
        with patch.object(custody.os, "fstat", stream_stat):
            self.assertEqual(self.probe("PASS", 0)["diagnostics"], [])
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_stable_stale_report_is_fail_one(self):
        self.change_first_byte()
        changed = self.target.read_bytes()
        result = self.probe("FAIL", 1)
        self.assertEqual(result["diagnostics"][0]["name"], "SOURCE_CHANGED")
        self.assertEqual(result["diagnostics"][0]["input"], "vector:" + self.target.name)
        self.assertEqual(self.target.read_bytes(), changed)

    def test_in_place_change_after_chunk(self):
        self.assert_unstable(self.probe("UNREADABLE", 4, self.change_first_byte))

    def test_same_size_replacement_after_chunk(self):
        denied = []

        def replace():
            replacement = self.root / "replacement.tmp"
            replacement.write_bytes(b"z" * len(self.original))
            before = self.target.stat()
            os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
            try:
                replacement.replace(self.target)
            except PermissionError as exc:
                denied.append(exc)
                raise

        result = self.probe("UNREADABLE", 4, replace)
        if denied:
            self.assert_read_error(result)
            self.assertEqual(self.target.read_bytes(), self.original)
        else:
            self.assert_unstable(result)
            self.assertEqual(self.target.read_bytes(), b"z" * len(self.original))

    def test_post_read_pathname_identity_change(self):
        stat = Path.stat
        changed = []
        observations = []

        def path_stat(path, *args, **kwargs):
            current = stat(path, *args, **kwargs)
            if path == self.target and changed:
                observations.append(path)
                return SimpleNamespace(
                    st_dev=current.st_dev, st_ino=current.st_ino + 1,
                    st_size=current.st_size, st_mtime_ns=current.st_mtime_ns,
                    st_ctime_ns=current.st_ctime_ns)
            return current

        # Model a successful replacement's changed identity after a known
        # chunk even on Windows hosts that deny renaming an open file.
        with patch.object(Path, "stat", path_stat):
            self.assert_unstable(self.probe(
                "UNREADABLE", 4, lambda: changed.append(True)))
        self.assertTrue(observations)
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_same_size_mtime_restored_change(self):
        self.assert_unstable(self.probe(
            "UNREADABLE", 4, lambda: self.change_first_byte(restore_mtime=True)))

    def test_second_pass_when_metadata_does_not_distinguish_change(self):
        # Model metadata collisions/unavailable identity fields explicitly. The
        # changed byte was already read, so only digest disagreement catches it.
        with patch.object(custody, "_source_stamp", return_value=(None,) * 5):
            self.assert_unstable(self.probe(
                "UNREADABLE", 4, lambda: self.change_first_byte(restore_mtime=True)))

    def test_second_pass_change_is_not_retried(self):
        self.assert_unstable(self.probe(
            "UNREADABLE", 4, self.change_first_byte, chunk_number=3))

    def test_growth_is_bounded_and_not_adopted(self):
        def append():
            with self.target.open("ab") as stream:
                stream.write(b"extra")

        self.assert_unstable(self.probe("UNREADABLE", 4, append))

    def test_truncation_after_chunk(self):
        self.assert_unstable(self.probe(
            "UNREADABLE", 4, lambda: self.target.write_bytes(b"short")))

    def test_removal_after_chunk(self):
        denied = []

        def remove():
            try:
                self.target.unlink()
            except PermissionError as exc:
                denied.append(exc)
                raise

        result = self.probe("UNREADABLE", 4, remove)
        if denied:
            self.assert_read_error(result)
            self.assertEqual(self.target.read_bytes(), self.original)
        else:
            self.assert_unstable(result)
            self.assertFalse(self.target.exists())

    def assert_read_error(self, result):
        self.assertEqual(result["diagnostics"], [dict(
            name="UNREADABLE", reason="SOURCE_READ_ERROR",
            input="vector:" + self.target.name)])
        source = next(item for item in result["sources"]
                      if item["input"] == "vector:" + self.target.name)
        self.assertIsNone(source["sha256_current"])

    def test_read_error_is_named(self):
        def read_error():
            raise OSError("synthetic read failure")

        result = self.probe("UNREADABLE", 4, read_error)
        self.assert_read_error(result)

    def test_permission_seam_and_missing_source(self):
        with patch.object(custody, "_hash_source", side_effect=PermissionError):
            result = self.probe("UNREADABLE", 4)
        self.assertTrue(all(item["reason"] == "SOURCE_READ_ERROR"
                            for item in result["diagnostics"]))
        self.target.unlink()
        result = self.probe("UNREADABLE", 4)
        self.assertEqual(result["diagnostics"][0]["reason"], "SOURCE_RESOLUTION_ERROR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
