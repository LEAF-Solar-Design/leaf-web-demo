"""Sequential stdlib checks of synthetic offline support only."""

import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest


SCHEMA = "leaf.xbench.x01.offline-run.v1"
ROOT = Path(__file__).resolve().parent
TEXT_LIMIT = 16384
MAX_CAPTURE_BYTES = 65536
MAX_DIAGNOSTICS = 64
MAX_DIAGNOSTIC_BYTES = 16384
MAX_DEPTH = 8
CHECKERS = ("check_cases.py", "check_plot_layers.py", "check_unit_equivalence.py")
VECTORS = ("fill_coverage_cases.json", "module_geometry_cases.json", "tag_overlap_cases.json")


def input_hash(path):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


class _WindowsJob:
    """A suspended child cannot spawn before assignment to this private job."""

    def __init__(self):
        from ctypes import wintypes as w

        class Limits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                        ("scheduling", w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes",
                         "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io", IO),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
                ("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
                ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
                ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
                ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
                ("QueryInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               w.DWORD, ctypes.c_void_p], w.BOOL),
                ("CloseHandle", [w.HANDLE], w.BOOL),
                ("PeekNamedPipe", [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.c_void_p,
                                   ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL)):
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("cannot create child job")
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits),
                                                ctypes.sizeof(limits)):
            self.close()
            raise OSError("cannot configure child job")

    def start(self, child):
        if not self.api.AssignProcessToJobObject(self.handle, int(child._handle)):
            raise OSError("cannot contain suspended child")
        # Popen closes the primary thread handle. Resume the still-suspended
        # process only after successful assignment (no parent-exit race).
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes, resume.restype = [ctypes.c_void_p], ctypes.c_long
        if resume(int(child._handle)) != 0:
            raise OSError("cannot resume contained child")

    def read(self, child):
        import msvcrt
        available = ctypes.c_ulong()
        handle = msvcrt.get_osfhandle(child.stdout.fileno())
        if not self.api.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            if ctypes.get_last_error() == 109:  # ERROR_BROKEN_PIPE
                return b""
            raise OSError("cannot inspect child output")
        return os.read(child.stdout.fileno(), min(4096, available.value)) if available.value else None

    def close(self, deadline=None):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)
            # Basic accounting ends with total/active/terminated process counts.
            class Accounting(ctypes.Structure):
                _fields_ = [("times", ctypes.c_int64 * 4),
                            ("page_faults", ctypes.c_uint32), ("total", ctypes.c_uint32),
                            ("active", ctypes.c_uint32), ("terminated", ctypes.c_uint32)]

            deadline = time.monotonic() + 1 if deadline is None else deadline
            accounting = Accounting()
            while time.monotonic() < deadline:
                if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                          ctypes.sizeof(accounting), None):
                    break
                if not accounting.active:
                    break
                time.sleep(.005)
            self.api.CloseHandle(self.handle)
            self.handle = None


def capture_child(command, timeout):
    """Bound execution AND pipe drain; no blocking reader survives this call.

    POSIX keeps the leader waitable until group termination, reserving its PID
    and preventing group-ID reuse. Linux adopts and reaps orphan descendants;
    other POSIX systems leave non-child reaping to their system reaper.
    """
    deadline = time.monotonic() + timeout
    raw = bytearray()
    child, job, subreaper = None, None, None
    code, timed_out, failure = None, False, None
    try:
        try:
            if os.name == "nt":
                job = _WindowsJob()
            else:
                if not all(hasattr(os, name) for name in ("waitid", "WNOWAIT", "P_PID")):
                    raise OSError("safe child observation unavailable")
                if sys.platform.startswith("linux"):
                    libc = ctypes.CDLL(None, use_errno=True)
                    previous = ctypes.c_int()
                    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
                        raise OSError("cannot inspect subreaper")
                    if libc.prctl(36, 1, 0, 0, 0) != 0:
                        raise OSError("cannot establish subreaper")
                    subreaper = (libc, previous.value)
        except (OSError, AttributeError):
            return None, False, b"", "CHILD_CONTAINMENT_ERROR"
        try:
            child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     creationflags=0x4 if job else 0,
                                     start_new_session=job is None)
        except OSError:
            return None, False, b"", "CHILD_LAUNCH_ERROR"
        try:
            if job:
                job.start(child)
            else:
                os.set_blocking(child.stdout.fileno(), False)
        except (OSError, AttributeError):
            failure = "CHILD_CONTAINMENT_ERROR"
        eof = False
        while failure is None:
            if time.monotonic() >= deadline:
                timed_out, failure = True, "CHILD_TIMEOUT"
                break
            try:
                if not eof:
                    if job:
                        chunk = job.read(child)
                    else:
                        try:
                            chunk = os.read(child.stdout.fileno(), 4096)
                        except BlockingIOError:
                            chunk = None
                    if chunk == b"":
                        eof = True
                    elif chunk:
                        remaining = MAX_CAPTURE_BYTES - len(raw)
                        raw.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            failure = "CHILD_OUTPUT_LIMIT"
                exited = (child.poll() is not None if job else
                          os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                          is not None)
                if eof and exited:
                    break
            except OSError:
                failure = "CHILD_OUTPUT_UNREADABLE"
            if failure is None:
                time.sleep(min(.005, max(0, deadline - time.monotonic())))
        # Deadline always takes precedence over a coincident read/limit error.
        if failure and time.monotonic() >= deadline:
            timed_out, failure = True, "CHILD_TIMEOUT"
    finally:
        cleanup_deadline = time.monotonic() + 1
        if child is not None:
            if job:
                job.close(cleanup_deadline)
                # Assignment may have failed; that child is still suspended.
                if child.poll() is None:
                    child.kill()
            else:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                code = child.wait(timeout=max(0, cleanup_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = failure or "CHILD_OUTPUT_UNREADABLE"
            child.stdout.close()
            if subreaper:
                while time.monotonic() < cleanup_deadline:
                    try:
                        pid, _ = os.waitpid(-child.pid, os.WNOHANG)
                    except ChildProcessError:
                        break
                    if not pid:
                        time.sleep(.005)
        if job:
            job.close()
        if subreaper:
            subreaper[0].prctl(36, subreaper[1], 0, 0, 0)
    return code, timed_out, bytes(raw), failure


def structured_diagnostics(raw):
    """Plain unittest text is valid; JSON-looking output must be safe JSON."""
    if not raw.lstrip().startswith((b"{", b"[")):
        return [], None
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (123, 91):
            depth += 1
            if depth > MAX_DEPTH:
                return [], "CHILD_DIAGNOSTIC_LIMIT"
        elif byte in (125, 93):
            depth -= 1
    try:
        def reject_constant(value):
            raise ValueError("nonfinite JSON")

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        payload = json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_object)
        # Also reject overflow such as 1e999, including outside diagnostics.
        json.dumps(payload, allow_nan=False)
    except (ValueError, TypeError, RecursionError, UnicodeError, OverflowError):
        return [], "CHILD_OUTPUT_UNREADABLE"
    if not isinstance(payload, dict):
        return [], "CHILD_DIAGNOSTICS_INVALID"
    diagnostics = payload.get("diagnostics", [])
    if not isinstance(diagnostics, list) or any(not isinstance(item, dict) for item in diagnostics):
        return [], "CHILD_DIAGNOSTICS_INVALID"
    if (len(diagnostics) > MAX_DIAGNOSTICS or
            len(json.dumps(diagnostics, allow_nan=False).encode("utf-8")) > MAX_DIAGNOSTIC_BYTES):
        return [], "CHILD_DIAGNOSTIC_LIMIT"
    return diagnostics, None


def timeout_seconds(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or not 0 < result <= 300:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError("timeout must be positive, finite and at most 300 seconds") from None
    return result


def aggregate(rows):
    """Explicit verdict precedence; rows remain available to callers."""
    verdicts = {row["verdict"] for row in rows}
    return "FAIL" if "FAIL" in verdicts else "UNREADABLE" if "UNREADABLE" in verdicts else "PASS"


def run_checks(commands=None, timeout=30, provenance_paths=None):
    """Run (checker name, argv) pairs; command injection is an internal test seam."""
    timeout = timeout_seconds(timeout)
    if commands is None:
        commands = [(name, [sys.executable, str(ROOT / name), "--self-test"])
                    for name in CHECKERS]
    if provenance_paths is None:
        provenance_paths = [("checker:" + name, ROOT / name) for name in CHECKERS]
        provenance_paths += [("vector:" + name, ROOT / name) for name in VECTORS]
    provenance_paths = list(provenance_paths)
    provenance = [dict(name=name, sha256_before=input_hash(path), sha256_after=None)
                  for name, path in provenance_paths]
    rows = []
    for name, command in commands:
        started = time.monotonic()
        code, timed_out, raw, failure = capture_child(command, timeout)
        truncated = len(raw) > TEXT_LIMIT
        output = raw[:TEXT_LIMIT].decode("utf-8", errors="replace")
        # Replacement characters can encode to more bytes than the input.
        truncated = truncated or len(output.encode("utf-8")) > TEXT_LIMIT
        output = output.encode("utf-8")[:TEXT_LIMIT].decode("utf-8", errors="ignore")
        diagnostics, parse_failure = structured_diagnostics(raw) if not failure else ([], None)
        failure = failure or parse_failure
        verdict = {0: "PASS", 1: "FAIL", 4: "UNREADABLE"}.get(code, "UNREADABLE")
        if failure:
            verdict = "UNREADABLE"
        elif code not in (0, 1, 4):
            failure = "CHILD_UNEXPECTED_EXIT"
        if failure:
            diagnostics.append(dict(name=failure, checker=name))
        if (len(diagnostics) > MAX_DIAGNOSTICS or
                len(json.dumps(diagnostics).encode("utf-8")) > MAX_DIAGNOSTIC_BYTES):
            verdict = "UNREADABLE"
            diagnostics = [dict(name="CHILD_DIAGNOSTIC_LIMIT", checker=name)]
        if not diagnostics:
            diagnostics.append(dict(name="CHILD_OUTPUT", checker=name, text=output[:2048],
                                    truncated=truncated))
        rows.append(dict(checker=name, verdict=verdict, exit_code=code,
                         timeout=timed_out, duration_seconds=time.monotonic() - started,
                         diagnostics=diagnostics, output=output, output_truncated=truncated))
    top_diagnostics = []
    for entry, (_, path) in zip(provenance, provenance_paths):
        entry["sha256_after"] = input_hash(path)
        if entry["sha256_before"] is None or entry["sha256_after"] is None:
            top_diagnostics.append(dict(name="INPUT_UNREADABLE", input=entry["name"]))
        elif entry["sha256_before"] != entry["sha256_after"]:
            top_diagnostics.append(dict(name="INPUT_CHANGED", input=entry["name"]))
    return dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                verdict="UNREADABLE" if top_diagnostics else aggregate(rows), checks=rows,
                provenance=provenance, diagnostics=top_diagnostics)


def write_atomic(path, result):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as stream:
            temporary = stream.name
            json.dump(result, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


class RunnerTests(unittest.TestCase):
    def stub(self, source):
        return [sys.executable, "-c", source]

    def run_stub(self, source, timeout=2):
        return run_checks([("stub", self.stub(source))], timeout)

    def test_all_pass(self):
        result = run_checks([(name, self.stub("pass")) for name in CHECKERS])
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(len(result["checks"]), 3)
        self.assertEqual(result["evidence_kind"], "synthetic_offline")
        self.assertIs(result["live_grade"], False)

    def test_named_failure_and_all_children_run(self):
        named = {"name": "EXACT_FAILURE", "object_ids": ["o/1"]}
        source = "import sys; print(" + repr(json.dumps(dict(diagnostics=[named]))) + "); sys.exit(1)"
        result = run_checks([("bad", self.stub(source)),
                             ("unreadable", self.stub("raise SystemExit(4)")),
                             ("last", self.stub("pass"))])
        self.assertEqual(result["verdict"], "FAIL")
        self.assertEqual(len(result["checks"]), 3)
        self.assertEqual(result["checks"][0]["diagnostics"], [named])
        self.assertEqual(result["checks"][-1]["verdict"], "PASS")

    def test_exit_four(self):
        self.assertEqual(self.run_stub("raise SystemExit(4)")["verdict"], "UNREADABLE")

    def test_timeout(self):
        result = self.run_stub("import time; time.sleep(5)", .05)
        row = result["checks"][0]
        self.assertEqual(result["verdict"], "UNREADABLE")
        self.assertTrue(row["timeout"])
        self.assertIsNotNone(row["exit_code"])
        self.assertEqual(row["diagnostics"][0]["name"], "CHILD_TIMEOUT")
        self.assertLess(row["duration_seconds"], 2)

    def test_missing_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            row = run_checks([("missing", [str(Path(directory) / "absent")])])["checks"][0]
        self.assertEqual(row["verdict"], "UNREADABLE")
        self.assertEqual(row["diagnostics"][0], dict(name="CHILD_LAUNCH_ERROR", checker="missing"))

    def test_unexpected_and_negative_exit(self):
        for source in ("raise SystemExit(7)", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"):
            with self.subTest(source=source):
                row = self.run_stub(source)["checks"][0]
                self.assertEqual(row["verdict"], "UNREADABLE")
                self.assertEqual(row["diagnostics"][0]["name"], "CHILD_UNEXPECTED_EXIT")
        self.assertEqual(aggregate([dict(verdict="PASS"), dict(verdict="UNREADABLE")]), "UNREADABLE")

    def test_plain_unittest_output(self):
        # An empty unittest suite exits 5 on newer Python versions. Exercise
        # actual successful unittest output rather than the no-tests policy.
        source = ("import unittest\n"
                  "class Smoke(unittest.TestCase):\n"
                  "    def test_smoke(self):\n"
                  "        self.assertEqual(2 + 2, 4)\n"
                  "unittest.main(argv=['stub'])\n")
        row = self.run_stub(source)["checks"][0]
        self.assertEqual(row["exit_code"], 0, row)
        self.assertEqual(row["verdict"], "PASS", row)
        self.assertEqual(row["diagnostics"][0]["checker"], "stub")
        self.assertIn("Ran 1 test", row["diagnostics"][0]["text"])
        self.assertIn("OK", row["diagnostics"][0]["text"])

    def test_invalid_timeout(self):
        for value in (0, -1, 301, True, None, "bad", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                run_checks([], value)

    def test_json_pass_does_not_override_exit(self):
        row = self.run_stub('print(\'{"verdict":"PASS","diagnostics":[]}\'); raise SystemExit(1)')["checks"][0]
        self.assertEqual(row["verdict"], "FAIL")

    def test_output_is_bounded(self):
        row = self.run_stub("print('x' * 50000)")["checks"][0]
        self.assertLessEqual(len(row["output"]), TEXT_LIMIT)
        self.assertTrue(row["output_truncated"])

    def test_named_diagnostic_survives_long_json_output(self):
        source = 'import json; print(json.dumps({"padding": "x" * 20000, "diagnostics": [{"name": "NAMED", "object_ids": ["a"]}]}))'
        row = self.run_stub(source)["checks"][0]
        self.assertEqual(row["diagnostics"], [dict(name="NAMED", object_ids=["a"])])
        self.assertTrue(row["output_truncated"])

    def test_atomic_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            result = self.run_stub("pass")
            write_atomic(path, result)
            self.assertEqual(json.loads(path.read_text()), result)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_default_provenance(self):
        result = run_checks([(name, self.stub("pass")) for name in CHECKERS])
        expected = {"checker:" + name: ROOT / name for name in CHECKERS}
        expected.update({"vector:" + name: ROOT / name for name in VECTORS})
        self.assertEqual(len(result["provenance"]), 6)
        self.assertEqual({entry["name"] for entry in result["provenance"]}, set(expected))
        for entry in result["provenance"]:
            digest = hashlib.sha256(expected[entry["name"]].read_bytes()).hexdigest()
            self.assertEqual(entry["sha256_before"], digest)
            self.assertEqual(entry["sha256_after"], digest)
        self.assertEqual(result["diagnostics"], [])

    def test_changed_provenance_overrides_pass_and_fail(self):
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "source.py"
                vector = Path(directory) / "vector.json"
                path.write_text("before", encoding="utf-8")
                vector.write_text("{}", encoding="utf-8")
                named = dict(name="CHILD_EVIDENCE", source_ids=["source/1"], vector_ids=["vector/1"])
                source = ("from pathlib import Path; Path(" + repr(str(path)) + ").write_text('after'); "
                          "print(" + repr(json.dumps(dict(diagnostics=[named]))) + "); "
                          "raise SystemExit(" + str(exit_code) + ")")
                result = run_checks([("stub", self.stub(source)), ("last", self.stub("pass"))],
                                    provenance_paths=[("checker:source.py", path),
                                                      ("vector:vector.json", vector)])
                self.assertEqual(result["verdict"], "UNREADABLE")
                self.assertEqual(result["diagnostics"], [dict(name="INPUT_CHANGED", input="checker:source.py")])
                self.assertEqual(result["checks"][0]["verdict"], "FAIL" if exit_code else "PASS")
                self.assertEqual(result["checks"][0]["diagnostics"], [named])
                self.assertEqual(len(result["checks"]), 2)
                self.assertEqual(result["checks"][1]["verdict"], "PASS")
                self.assertNotEqual(result["provenance"][0]["sha256_before"],
                                    result["provenance"][0]["sha256_after"])

    def test_deleted_and_missing_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.py"
            path.write_text("before", encoding="utf-8")
            source = "from pathlib import Path; Path(" + repr(str(path)) + ").unlink()"
            result = run_checks([("stub", self.stub(source))],
                                provenance_paths=[("checker:source.py", path)])
            self.assertEqual(result["verdict"], "UNREADABLE")
            self.assertEqual(result["checks"][0]["verdict"], "PASS")
            self.assertIsNotNone(result["provenance"][0]["sha256_before"])
            self.assertIsNone(result["provenance"][0]["sha256_after"])
            self.assertEqual(result["diagnostics"], [dict(name="INPUT_UNREADABLE", input="checker:source.py")])
            result = run_checks([], provenance_paths=[("checker:source.py", path)])
            self.assertIsNone(result["provenance"][0]["sha256_before"])
            self.assertEqual(result["verdict"], "UNREADABLE")

    def test_capture_boundary(self):
        for size in (MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES + 1):
            with self.subTest(size=size):
                row = self.run_stub("import sys; sys.stdout.write('x' * " + str(size) + ")")["checks"][0]
                self.assertEqual(row["verdict"], "PASS" if size == MAX_CAPTURE_BYTES else "UNREADABLE")
                if size > MAX_CAPTURE_BYTES:
                    self.assertEqual(row["diagnostics"][0]["name"], "CHILD_OUTPUT_LIMIT")
                self.assertLessEqual(len(row["output"].encode("utf-8")), TEXT_LIMIT)

    def test_flood_is_reaped_without_scratch_files(self):
        from unittest.mock import patch
        for source in ("import os\nwhile True: os.write(1, b'x' * 4096)",
                       "import os; os.write(1, b'x' * 1000000)"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                with patch.object(tempfile, "tempdir", directory):
                    code, timed_out, raw, failure = capture_child(self.stub(source), 2)
                self.assertIsInstance(code, int)
                self.assertFalse(timed_out)
                self.assertEqual(failure, "CHILD_OUTPUT_LIMIT")
                self.assertEqual(len(raw), MAX_CAPTURE_BYTES)
                self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertFalse(any(thread.name.startswith("x01-capture") for thread in threading.enumerate()))

    def test_diagnostic_limits(self):
        nested = 0
        for _ in range(MAX_DEPTH):
            nested = [nested]
        payloads = [dict(diagnostics=[dict(name="x")] * 65),
                    dict(diagnostics=[dict(name="x", text="x" * MAX_DIAGNOSTIC_BYTES)]),
                    dict(diagnostics=[dict(name="x", nested=nested)])]
        for payload in payloads:
            with self.subTest(payload=payload):
                row = self.run_stub("print(" + repr(json.dumps(payload)) + ")")["checks"][0]
                self.assertEqual(row["verdict"], "UNREADABLE")
                self.assertEqual(row["diagnostics"][0]["name"], "CHILD_DIAGNOSTIC_LIMIT")
        payload = dict(diagnostics=[dict(name="x")] * 64)
        row = self.run_stub("print(" + repr(json.dumps(payload)) + ")")["checks"][0]
        self.assertEqual(row["verdict"], "PASS")
        self.assertEqual(len(row["diagnostics"]), 64)

    def test_diagnostic_byte_boundary(self):
        overhead = len(json.dumps([dict(name="x", text="")]).encode("utf-8"))
        for extra in (0, 1):
            diagnostics = [dict(name="x", text="x" * (MAX_DIAGNOSTIC_BYTES - overhead + extra))]
            raw = json.dumps(dict(diagnostics=diagnostics)).encode("utf-8")
            retained, failure = structured_diagnostics(raw)
            if extra:
                self.assertEqual(failure, "CHILD_DIAGNOSTIC_LIMIT")
                self.assertEqual(retained, [])
            else:
                self.assertIsNone(failure)
                self.assertEqual(retained, diagnostics)

    def test_flood_reason_overrides_exit_one(self):
        from unittest.mock import patch
        with patch.object(sys.modules[__name__], "capture_child",
                          return_value=(1, False, b"x" * MAX_CAPTURE_BYTES, "CHILD_OUTPUT_LIMIT")):
            row = self.run_stub("pass")["checks"][0]
        self.assertEqual(row["exit_code"], 1)
        self.assertEqual(row["verdict"], "UNREADABLE")
        self.assertEqual(row["diagnostics"][0]["name"], "CHILD_OUTPUT_LIMIT")

    def test_unsafe_json_is_safe_report(self):
        for raw in ('{"diagnostics": [', '{"diagnostics": [{"n": NaN}]}',
                    '{"padding": Infinity}', '{"padding": 1e999}',
                    '{"diagnostics": "bad"}', '{"diagnostics": [1]}',
                    '[' * 2000 + '0' + ']' * 2000):
            with self.subTest(raw=raw):
                row = self.run_stub("print(" + repr(raw) + ")")["checks"][0]
                self.assertEqual(row["verdict"], "UNREADABLE")
                self.assertIn(row["diagnostics"][0]["name"],
                              ("CHILD_OUTPUT_UNREADABLE", "CHILD_DIAGNOSTICS_INVALID", "CHILD_DIAGNOSTIC_LIMIT"))
                json.dumps(row, allow_nan=False)

    def test_duplicate_child_json_keys(self):
        from validate_offline_report import validate_report
        for raw in ('{"diagnostics":[],"diagnostics":[{"name":"hidden"}]}',
                    '{"diagnostics":[{"name":"first","name":"second"}]}',
                    '{"extra":{"nested":{"x":1,"x":2}},"diagnostics":[]}'):
            with self.subTest(raw=raw):
                commands = [(name, self.stub("print(" + repr(raw) + ")")) for name in CHECKERS]
                result = run_checks(commands)
                self.assertEqual(result["verdict"], "UNREADABLE")
                for row in result["checks"]:
                    self.assertFalse(row["timeout"])
                    self.assertEqual(row["exit_code"], 0)
                    self.assertEqual(row["diagnostics"], [dict(
                        name="CHILD_OUTPUT_UNREADABLE", checker=row["checker"])])
                self.assertEqual(validate_report(result)["verdict"], "PASS")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--timeout-seconds", default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        timeout = timeout_seconds(args.timeout_seconds)
    except ValueError as error:
        print(json.dumps(dict(schema=SCHEMA, evidence_kind="synthetic_offline", live_grade=False,
                              verdict="UNREADABLE", checks=[], diagnostics=[dict(
                                  name="INVALID_TIMEOUT", message=str(error))])))
        return 4
    if args.self_test:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(RunnerTests)
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    result = run_checks(timeout=timeout)
    if args.output:
        try:
            write_atomic(args.output, result)
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
            result.setdefault("diagnostics", []).append(dict(
                name="OUTPUT_WRITE_ERROR", message=str(error)))
            if result["verdict"] == "PASS":
                result["verdict"] = "UNREADABLE"
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return {"PASS": 0, "FAIL": 1, "UNREADABLE": 4}[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
