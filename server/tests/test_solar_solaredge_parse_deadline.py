"""The SolarEdge parse runs in a child process under a hard wall-time deadline.

A deadline miss kills and reaps the child before the parse slot is released, stores nothing and
answers a named retryable refusal; the next parse runs normally. The child's verdict protocol
fails closed, and the real C14 parse through the child is unchanged.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
import solar_import_sources as sources
import solar_solaredge_parse_worker as worker
import solar_solaredge_report as report
from test_solar_import_sources import pdf
from test_solar_solaredge_report import (GRIDS, PDF, SMALL_RESULT, TENANT, body, client, post,
                                         refusal, save, seed_small, written)

SERVER = Path(__file__).resolve().parents[1]
C14_MATRICES_SHA = "49b987b05ddaf783a7fa269c3606a69e3dfc22064ddb736c94e594445eab5117"
EMPTY_VERDICT = b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":[]}'
REFUSAL_VERDICT = b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","refusal":"REPORT_PDF_UNSUPPORTED"}'
HANG = "import sys, time; sys.stdin.buffer.read(); time.sleep(600)"
ECHO = "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())"


def canonical_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def grids_verdict():
    return json.dumps({"schema": worker.VERDICT_SCHEMA, "matrices": GRIDS},
                      separators=(",", ":")).encode()


@pytest.fixture
def children(monkeypatch):
    """Every parse child started during the test, with the parse slot state at kill and reap."""
    made = []

    class Recorded(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.slot_free_at_kill = None
            self.slot_free_at_reap = None
            made.append(self)

        def kill(self):
            self.slot_free_at_kill = slot_free()
            super().kill()

        def communicate(self, *args, **kwargs):
            result = super().communicate(*args, **kwargs)
            if self.slot_free_at_kill is not None and self.slot_free_at_reap is None:
                self.slot_free_at_reap = slot_free()
            return result

    monkeypatch.setattr(report.subprocess, "Popen", Recorded)
    return made


def slot_free():
    if report._PARSE_SLOTS.acquire(blocking=False):
        report._PARSE_SLOTS.release()
        return True
    return False


def use(monkeypatch, argv, deadline=None, slots=None):
    monkeypatch.setattr(report, "_worker_argv", lambda: list(argv))
    if deadline is not None:
        monkeypatch.setattr(report, "PARSE_DEADLINE_S", deadline)
    if slots is not None:
        monkeypatch.setattr(report, "_PARSE_SLOTS", threading.BoundedSemaphore(slots))


def echo(monkeypatch, tmp_path, data, **kwargs):
    path = tmp_path / "verdict.bin"
    path.write_bytes(data)
    use(monkeypatch, [sys.executable, "-c", ECHO, str(path)], **kwargs)


def test_parse_deadline_constants():
    assert report.PARSE_DEADLINE_S == 60.0
    assert report.MAX_CONCURRENT_PARSES == 2
    assert isinstance(report._PARSE_SLOTS, threading.BoundedSemaphore)
    assert report.PARSE_BASE_NAME == worker.PARSE_BASE_NAME == "solaredge"
    assert worker.VERDICT_SCHEMA == "leaf.solar-solaredge-parse-verdict.v1"
    assert worker.REFUSAL == "REPORT_PDF_UNSUPPORTED"
    assert worker.MAX_INPUT_BYTES == sources.MAX_IMPORT_PDF_BYTES == 16_777_216
    assert worker.MAX_OUTPUT_BYTES == 67_108_864
    argv = report._worker_argv()
    assert argv == [sys.executable, "-B", os.path.abspath(worker.__file__)]
    assert Path(argv[2]) == SERVER / "solar_solaredge_parse_worker.py"


def test_parse_deadline_worker_import_is_lazy():
    result = subprocess.run([sys.executable, "-B", "-c",
        "import solar_solaredge_parse_worker, solar_solaredge_report, sys; print(sorted(m for m in sys.modules if m.split('.')[0] in ('pdfminer', 'solar_solaredge_pdf', 'solar_solaredge_parse')))"],
        cwd=SERVER, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "[]"


@pytest.mark.parametrize("data,expected", [(pdf(1), EMPTY_VERDICT),
                                           (b"%PDF-1.4\n\x00\x01garbage", REFUSAL_VERDICT)],
                         ids=["empty-page", "garbage"])
def test_parse_deadline_worker_script(data, expected):
    result = subprocess.run([sys.executable, "-B", str(SERVER / "solar_solaredge_parse_worker.py")],
                            input=data, capture_output=True, timeout=120)
    assert result.returncode == 0
    assert result.stdout == expected


def test_parse_deadline_verdict_bytes(monkeypatch):
    assert worker.verdict_bytes(pdf(1)) == EMPTY_VERDICT
    assert worker.verdict_bytes(bytearray(pdf(1))) == EMPTY_VERDICT
    assert worker.verdict_bytes(b"%PDF-1.4\n\x00\x01garbage") == REFUSAL_VERDICT
    assert worker.verdict_bytes("not bytes") == REFUSAL_VERDICT
    # Valid C14 input makes the size bound the only possible refusal here.
    with monkeypatch.context() as bounded:
        bounded.setattr(worker, "MAX_INPUT_BYTES", len(PDF) - 1)
        assert worker.verdict_bytes(PDF) == REFUSAL_VERDICT
    monkeypatch.setattr(worker, "MAX_OUTPUT_BYTES", len(EMPTY_VERDICT) - 1)
    assert worker.verdict_bytes(pdf(1)) == REFUSAL_VERDICT


VERDICT_CASES = [
    ("ok", 0, EMPTY_VERDICT, []),
    ("grids", 0, None, GRIDS),
    ("exit-1", 1, EMPTY_VERDICT, None),
    ("killed", -9, EMPTY_VERDICT, None),
    ("empty", 0, b"", None),
    ("not-bytes", 0, "{}", None),
    ("utf8", 0, b"\xff", None),
    ("json", 0, b'{"schema":', None),
    ("list", 0, b"[]", None),
    ("schema", 0, b'{"schema":"other","matrices":[]}', None),
    ("refusal", 0, REFUSAL_VERDICT, None),
    ("extra-key", 0, b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":[],"x":1}', None),
    ("matrices-object", 0, b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":{}}', None),
    ("both", 0, b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":[],"refusal":"REPORT_PDF_UNSUPPORTED"}', None),
    ("deep", 0, b"[" * 100000 + b"]" * 100000, None),
    ("nan", 0, b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":[NaN]}', None),
    ("infinity", 0, b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":[Infinity]}', None),
    ("negative-infinity", 0, b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":[-Infinity]}', None),
    ("overflow", 0, b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":[1e400]}', None),
    ("boolean", 0, b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":[{"Rows":[true]}]}', None),
    ("duplicate-key", 0, b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":[1],"matrices":[]}', None),
    ("false-status", False, EMPTY_VERDICT, None),
    ("float-status", 0.0, EMPTY_VERDICT, None),
    ("negative-zero", 0, b'{"schema":"leaf.solar-solaredge-parse-verdict.v1","matrices":[-0.0]}', [-0.0]),
]


@pytest.mark.parametrize("case,returncode,out,expected", VERDICT_CASES,
                         ids=[row[0] for row in VERDICT_CASES])
def test_parse_deadline_decode_verdict(case, returncode, out, expected):
    if out is None:
        out = grids_verdict()
    assert worker.decode_verdict(returncode, out) == expected


def test_parse_deadline_decode_verdict_size(monkeypatch):
    monkeypatch.setattr(worker, "MAX_OUTPUT_BYTES", len(EMPTY_VERDICT))
    assert worker.decode_verdict(0, EMPTY_VERDICT) == []
    monkeypatch.setattr(worker, "MAX_OUTPUT_BYTES", len(EMPTY_VERDICT) - 1)
    assert worker.decode_verdict(0, EMPTY_VERDICT) is None


def test_parse_deadline_c14_through_the_child(children):
    matrices = report.parse_source(PDF)
    assert len(matrices) == 14
    assert canonical_sha(matrices) == C14_MATRICES_SHA
    assert len(children) == 1 and children[0].returncode == 0
    assert children[0].args == report._worker_argv()


def test_parse_deadline_small_through_the_child(children):
    assert report.parse_source(pdf(1)) == []
    refusal("REPORT_PDF_UNSUPPORTED", report.parse_source, b"%PDF-1.4\n\x00\x01garbage")
    assert [child.returncode for child in children] == [0, 0]


def test_parse_deadline_hung_child_is_killed(monkeypatch, children):
    use(monkeypatch, [sys.executable, "-c", HANG], deadline=1.0)
    started = time.monotonic()
    refusal("REPORT_PARSE_TIMEOUT", report.parse_source, pdf(1))
    elapsed = time.monotonic() - started
    assert 1.0 <= elapsed < 15.0
    assert len(children) == 1
    assert children[0].returncode is not None and children[0].returncode != 0
    assert children[0].poll() is not None


def test_parse_deadline_real_parse_is_cut(monkeypatch, children):
    monkeypatch.setattr(report, "PARSE_DEADLINE_S", 0.05)
    started = time.monotonic()
    refusal("REPORT_PARSE_TIMEOUT", report.parse_source, PDF)
    assert time.monotonic() - started < 15.0
    assert len(children) == 1 and children[0].returncode is not None


def test_parse_deadline_unread_stdin(tmp_path, monkeypatch, children):
    backend, source = seed_small(tmp_path, monkeypatch)
    monkeypatch.setattr(report.solar_import_sources, "load_import_source",
                        lambda *a, **k: ({"content_sha256": "a" * 64}, b"x" * 1_048_576))
    use(monkeypatch, [sys.executable, "-c", "import time; time.sleep(30)"],
        deadline=0.1, slots=1)
    # Discriminates on Windows: pipe input blocks before communicate honours timeout.
    started = time.monotonic()
    refusal("REPORT_PARSE_TIMEOUT", save, backend, source)
    assert time.monotonic() - started < 10.0
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].slot_free_at_kill is False
    assert children[0].slot_free_at_reap is False
    assert slot_free()


def memory_builder(tmp_path, monkeypatch):
    """Freeze the builder's reads in memory so concurrent parses do no storage I/O."""
    backend, source = seed_small(tmp_path, monkeypatch)
    context = report.resolve_graph_context(backend, TENANT, "solar", "head", project_id=None)
    loaded = sources.load_import_source(backend, TENANT, "solar", source["artifact_id"],
                                        project_id=context["project_id"])
    monkeypatch.setattr(report, "resolve_graph_context", lambda *a, **k: context)
    monkeypatch.setattr(sources, "load_import_source", lambda *a, **k: loaded)

    def absent(*args, **kwargs):
        raise report.GraphValidationError("ARTIFACT_NOT_FOUND")

    monkeypatch.setattr(report.solar_artifacts, "read_artifact", absent)
    return backend, source


def test_parse_deadline_builder_spawn_failure(tmp_path, monkeypatch, children):
    backend, source = memory_builder(tmp_path, monkeypatch)
    before = written(backend)
    use(monkeypatch, [str(tmp_path / "no-such-interpreter.exe")], slots=2)
    refusal("REPORT_PARSE_UNAVAILABLE", save, backend, source)
    assert children == []
    assert report._PARSE_SLOTS.acquire(blocking=False)
    assert report._PARSE_SLOTS.acquire(blocking=False)
    assert not report._PARSE_SLOTS.acquire(blocking=False)
    report._PARSE_SLOTS.release()
    report._PARSE_SLOTS.release()
    assert written(backend) == before


def test_parse_deadline_two_live_parses_are_busy(tmp_path, monkeypatch, children):
    backend, source = memory_builder(tmp_path, monkeypatch)
    use(monkeypatch, [sys.executable, "-c", HANG], deadline=60.0, slots=2)
    real_wait = subprocess.Popen.communicate
    ready = threading.Barrier(3)
    release = threading.Event()
    failures = []

    def held_wait(self, *args, **kwargs):
        if kwargs.get("timeout") is not None:
            ready.wait(timeout=15)
            if not release.wait(timeout=15):
                raise RuntimeError("test did not release parses")
            raise subprocess.TimeoutExpired(self.args, kwargs["timeout"])
        return real_wait(self, *args, **kwargs)

    def parse():
        try:
            save(backend, source)
        except BaseException as exc:
            failures.append(exc)

    with monkeypatch.context() as waiting:
        waiting.setattr(subprocess.Popen, "communicate", held_wait)
        threads = [threading.Thread(target=parse) for _ in range(2)]
        for thread in threads:
            thread.start()
        try:
            ready.wait(timeout=15)
            assert len(children) == 2 and all(child.poll() is None for child in children)
            started = time.monotonic()
            refusal("REPORT_BUSY", save, backend, source)
            assert time.monotonic() - started < 1.0
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads)
    assert len(failures) == 2
    assert all(isinstance(exc, report.SolarEdgeReportError)
               and exc.code == "REPORT_PARSE_TIMEOUT" for exc in failures)
    assert all(child.poll() is not None for child in children)
    echo(monkeypatch, tmp_path, grids_verdict())
    assert save(backend, source) == SMALL_RESULT
    assert len(children) == 3 and children[-1].returncode == 0
    assert slot_free()


class _BoundedOnlyStdin:
    """A binary stdin that records each bounded read, answers it in full, and fails an unbounded one."""

    def __init__(self):
        self.requests = []

    def read(self, n=-1):
        if n is None or n < 0:
            raise AssertionError("worker made an unbounded stdin read")
        self.requests.append(n)
        return b"\x00" * n


def test_parse_deadline_worker_script_input_cap(monkeypatch):
    data = pdf(1)
    # The capped read still gets this whole valid PDF, so only the size guard refuses.
    code = ("import solar_solaredge_parse_worker as w; "
            f"w.MAX_INPUT_BYTES = {len(data) - 1}; raise SystemExit(w.main())")
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=SERVER,
                            input=data, capture_output=True, timeout=120)
    assert result.returncode == 0
    assert result.stdout == REFUSAL_VERDICT
    # The read itself is bounded: an endless stdin gets exactly one read of the cap plus one byte.
    stdin = _BoundedOnlyStdin()
    stdout = io.BytesIO()
    monkeypatch.setattr(worker, "MAX_INPUT_BYTES", 64)
    monkeypatch.setattr(worker, "sys", types.SimpleNamespace(
        stdin=types.SimpleNamespace(buffer=stdin), stdout=types.SimpleNamespace(buffer=stdout)))
    assert worker.main() == 0
    assert stdin.requests == [65]
    assert stdout.getvalue() == REFUSAL_VERDICT


def test_parse_deadline_slot_outlives_the_child(tmp_path, monkeypatch, children):
    backend, source = seed_small(tmp_path, monkeypatch)
    before = written(backend)
    use(monkeypatch, [sys.executable, "-c", HANG], deadline=1.0, slots=1)
    for attempt in range(3):
        refusal("REPORT_PARSE_TIMEOUT", save, backend, source)
        child = children[attempt]
        assert child.slot_free_at_kill is False
        assert child.slot_free_at_reap is False
        assert child.returncode is not None
        assert slot_free()
    assert written(backend) == before
    echo(monkeypatch, tmp_path, grids_verdict(), deadline=60.0)
    assert save(backend, source) == SMALL_RESULT
    assert len(children) == 4 and children[3].returncode == 0
    assert slot_free()


def test_parse_deadline_interrupted_wait_still_reaps(monkeypatch, children):
    use(monkeypatch, [sys.executable, "-c", HANG], deadline=60.0)
    real = subprocess.Popen.communicate

    def interrupted(self, *args, **kwargs):
        if kwargs.get("timeout") is not None:
            raise RuntimeError("interrupted")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(RuntimeError):
        report.parse_source(pdf(1))
    assert len(children) == 1
    assert children[0].returncode is not None and children[0].poll() is not None


@pytest.mark.parametrize("case", ["exit-3", "garbage", "valid-but-exit-1", "refusal"])
def test_parse_deadline_child_failures(tmp_path, monkeypatch, children, case):
    if case == "exit-3":
        use(monkeypatch, [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); sys.exit(3)"])
    elif case == "garbage":
        echo(monkeypatch, tmp_path, b"not json")
    elif case == "valid-but-exit-1":
        path = tmp_path / "verdict.bin"
        path.write_bytes(grids_verdict())
        use(monkeypatch, [sys.executable, "-c", ECHO.replace("read())", "read()); sys.exit(1)"), str(path)])
    else:
        echo(monkeypatch, tmp_path, REFUSAL_VERDICT)
    refusal("REPORT_PDF_UNSUPPORTED", report.parse_source, pdf(1))
    assert len(children) == 1 and children[0].returncode is not None


def test_parse_deadline_spawn_failure(tmp_path, monkeypatch):
    use(monkeypatch, [str(tmp_path / "no-such-interpreter.exe")], slots=1)
    refusal("REPORT_PARSE_UNAVAILABLE", report.parse_source, pdf(1))
    assert slot_free()


def test_parse_deadline_input_bound_spawns_nothing(monkeypatch, children):
    monkeypatch.setattr(worker, "MAX_INPUT_BYTES", len(pdf(1)) - 1)
    refusal("REPORT_PDF_UNSUPPORTED", report.parse_source, pdf(1))
    refusal("REPORT_PDF_UNSUPPORTED", report.parse_source, "not bytes")
    assert children == []


def test_parse_deadline_reuse_spawns_nothing(tmp_path, monkeypatch, children):
    backend, source = seed_small(tmp_path, monkeypatch)
    echo(monkeypatch, tmp_path, grids_verdict())
    assert save(backend, source) == SMALL_RESULT
    assert len(children) == 1
    use(monkeypatch, [sys.executable, "-c", HANG], deadline=0.01, slots=1)
    report._PARSE_SLOTS.acquire()
    assert save(backend, source) == SMALL_RESULT
    assert len(children) == 1


@pytest.mark.parametrize("case,status,code", [
    ("timeout", 503, "REPORT_PARSE_TIMEOUT"),
    ("unavailable", 503, "REPORT_PARSE_UNAVAILABLE"),
    ("unsupported", 422, "REPORT_PDF_UNSUPPORTED"),
])
def test_parse_deadline_route_refusals(tmp_path, monkeypatch, client, children, case, status, code):
    backend, source = seed_small(tmp_path, monkeypatch)
    if case == "timeout":
        use(monkeypatch, [sys.executable, "-c", HANG], deadline=1.0)
    elif case == "unavailable":
        use(monkeypatch, [str(tmp_path / "no-such-interpreter.exe")])
    else:
        echo(monkeypatch, tmp_path, REFUSAL_VERDICT)
    before = written(backend)
    response = post(client, body(source))
    assert response.status_code == status, response.text
    assert response.json()["error"]["reason_code"] == code
    assert response.json()["error"]["retryable"] is (status == 503)
    assert written(backend) == before
    assert all(child.returncode is not None for child in children)
    echo(monkeypatch, tmp_path, grids_verdict(), deadline=60.0)
    response = post(client, body(source))
    assert response.status_code == 200
    assert response.json() == {**SMALL_RESULT, "error": None, "degraded_mode": False}
