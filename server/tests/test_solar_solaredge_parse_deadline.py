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


def test_resource_refusal_preserves_known_cause_without_trusting_bad_envelopes(monkeypatch):
    import solar_solaredge_pdf as primitives
    from test_solar_solaredge_pdf import _pdf, HELVETICA
    monkeypatch.setattr(primitives, 'MAX_LETTERS', 0)
    value = worker.verdict_bytes(_pdf(b'BT /F1 4 Tf (1.1) Tj ET', resources=HELVETICA))
    assert worker.decode_verdict(0, value) is None
    assert worker.decode_refusal(0, value) == 'REPORT_RESOURCE_LIMIT'
    assert worker.decode_refusal(1, value) is None
    for altered in [value[:-1], value[:-1] + b',"extra":1}',
                    value.replace(b'REPORT_RESOURCE_LIMIT', b'FOREIGN_REASON'),
                    value[:-1] + b',"refusal":"REPORT_RESOURCE_LIMIT"}']:
        assert worker.decode_refusal(0, altered) is None


def test_resource_refusal_from_actual_child_is_named_and_reaped(monkeypatch, children):
    from test_solar_solaredge_pdf import _pdf, HELVETICA
    code = ('import sys;sys.path.insert(0,' + repr(str(SERVER)) + ');'
            'import solar_solaredge_pdf as p;p.MAX_LETTERS=0;'
            'import solar_solaredge_parse_worker as w;raise SystemExit(w.main())')
    monkeypatch.setattr(report, '_worker_argv', lambda: [sys.executable, '-B', '-c', code])
    with pytest.raises(report.SolarEdgeReportError, match='REPORT_RESOURCE_LIMIT'):
        report.parse_source(_pdf(b'BT /F1 4 Tf (1.1) Tj ET', resources=HELVETICA))
    assert children and all(child.poll() is not None for child in children)


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


def _layout_report(order):
    layouts = {
        "A": [(100, 500, "1.1"), (140, 500, "7")],
        "B": [(100, 500, "2.1"), (600, 100, "2.2")],
        "C": [(100, 500, "3.1"), (600, 100, "3.1")],
        "0": [],
    }
    kids = " ".join(f"{3 + 2 * page} 0 R" for page in range(len(order)))
    output = [b"%PDF-1.4\n", b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
              f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {len(order)}>>endobj\n".encode()]
    for page, layout in enumerate(order):
        index = 3 + 2 * page
        rotation = "/Rotate 90" if layout == "R" else ""
        output.append((f"{index} 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 1000 1000]"
                       f"/Contents {index+1} 0 R {rotation}"
                       "/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>>>endobj\n").encode())
        commands = []
        for x, y, label in layouts.get(layout, []):
            commands.append(f".9 .95 1 rg 0 0 0 RG n {x} {y} 30 60 re B* 1 0 0 RG "
                            f"n {x+10} {y+25} 10 10 re S "
                            f"n {x+15} {y+30} m {x+22} {y+31} {x+30} {y+31} {x+35} {y+30} c S "
                            f"0 0 0 rg BT /F1 6 Tf {x+12} {y+28} Td ({label}) Tj ET\n")
        if commands:
            commands.append("BT /F1 6 Tf 940 900 Td (Legend) Tj ET\n")
        content = "".join(commands).encode()
        output.extend([f"{index+1} 0 obj<</Length {len(content)}>>stream\n".encode(),
                       content, b"\nendstream\nendobj\n"])
    output.append(b"trailer<</Root 1 0 R>>\n%%EOF\n")
    return b"".join(output)


@pytest.mark.parametrize(("order", "sizes"), [
    ("ABC", [2, 1, 1, 1, 1]),
    ("BCA", [1, 1, 1, 1, 2]),
    ("0CAA", [1, 1, 2, 2]),
])
def test_layout_report_pages_preserve_group_counts_and_optimizer_occurrences(order, sizes):
    matrices = worker.decode_verdict(0, worker.verdict_bytes(_layout_report(order)))
    assert matrices is not None
    assert [sum(p["Code"] == 1 for row in m["Rows"] for p in row["Panels"])
            for m in matrices] == sizes
    names = [matrix["Dwgname"] for matrix in matrices]
    assert len(set(names)) == len(names)
    optimizer_ids = [row["id"] for matrix in matrices for row in matrix["Optimizers"]]
    assert len(set(optimizer_ids)) == len(optimizer_ids) == sum(sizes)


def test_layout_report_refuses_an_unsupported_later_page_atomically():
    assert worker.verdict_bytes(_layout_report("AR")) == REFUSAL_VERDICT


def test_layout_report_operator_budget_is_shared_across_pages(monkeypatch):
    import solar_solaredge_pdf as extract

    monkeypatch.setattr(extract, "MAX_OPERATORS", 50)
    assert worker.decode_verdict(0, worker.verdict_bytes(_layout_report("A"))) is not None
    assert worker.decode_refusal(0, worker.verdict_bytes(_layout_report("AA"))) == worker.RESOURCE_REFUSAL


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
    assert all(isinstance(matrix["Optimizers"], list) for matrix in matrices)
    legacy = [{key: value for key, value in matrix.items() if key != "Optimizers"} for matrix in matrices]
    assert canonical_sha(legacy) == C14_MATRICES_SHA
    assert len(children) == 1 and children[0].returncode == 0
    assert children[0].args == report._worker_argv()


def test_parse_deadline_small_through_the_child(children):
    assert report.parse_source(pdf(1)) == []
    refusal("REPORT_PDF_UNSUPPORTED", report.parse_source, b"%PDF-1.4\n\x00\x01garbage")
    assert [child.returncode for child in children] == [0, 0]


def test_optimizer_numeric_label_is_independent_of_dotted_label_and_legacy_output():
    import solar_solaredge_parse as parser
    from test_solar_solaredge_parse import _page, _word
    page = _page(with_second_grid=False)
    # Same optimizer gets a numeric word before the dotted string word in the scan.
    page.letters = _word("42", 112, 522) + page.letters
    parsed, legacy = parser.parse_primitives(page)
    assert parsed.optimizers[0].vendor_labels == ["42"]
    assert parsed.optimizers[0].string_labels == ["1.1"]
    assert "Optimizers" not in legacy[0]
    modern = parsed.identity_matrices()
    assert modern[0]["Optimizers"][0]["vendor_label"] == "42"
    assert modern[0]["Optimizers"][0]["cells"] == [[0, 1]]
    assert modern[0]["Sequences"] == [2]


def test_equidistant_label_and_unproven_panel_do_not_supply_identity():
    import solar_solaredge_parse as parser
    from test_solar_solaredge_parse import _page, _word
    page = _page(with_second_grid=False)
    page.letters = _word("42", 135, 522) + page.letters
    parsed, _ = parser.parse_primitives(page)
    assert all("42" not in opt.vendor_labels for opt in parsed.optimizers)
    parsed.optimizers[1].measured_panels.clear()
    assert parsed.identity_matrices()[0]["Optimizers"][1]["cells"] == []


def measured_pair_page(axis, scale=1.0, origin=(300.0, 500.0), gap=None):
    from solar_solaredge_pdf import PagePrimitives
    from test_solar_solaredge_parse import _optimizer_path, _word, PANEL_FILL, RED
    x, y = origin
    width, height = 14.23 * scale, 24.33 * scale
    offset = (7.29 if axis == "horizontal" else 12.35) * scale
    if gap is not None:
        offset = ((width if axis == "horizontal" else height) + gap * scale) / 2
    centers = [(x - offset, y), (x + offset, y)] if axis == "horizontal" else [
        (x, y - offset), (x, y + offset)]
    paths = [(True, True, (cx - width / 2, cy - height / 2,
                          cx + width / 2, cy + height / 2), PANEL_FILL, None)
             for cx, cy in centers]
    paths.append(_optimizer_path(x, y))
    return PagePrimitives(page_width=2000.0, page_height=2000.0, paths=paths,
                          letters=_word("3.2", x, y), lines=[],
                          curves=[(x, 2000 - y, x + 10, 2000 - y,
                                   x + 20, 2000 - y, x + 30, 2000 - y, RED)])


@pytest.mark.parametrize("axis,scale,origin", [
    ("horizontal", 1.0, (300.0, 500.0)),
    ("vertical", 1.0, (300.0, 500.0)),
    ("horizontal", 1.8, (650.0, 720.0)),
    ("vertical", 1.8, (650.0, 720.0)),
])
def test_measured_pairs_emit_exact_cells_and_sequence(axis, scale, origin):
    import solar_solaredge_parse as parser
    parsed = parser.SolarEdgeParse(measured_pair_page(axis, scale, origin))
    for grid in parsed.final_grids:
        parsed._assign_row_column_coordinates(grid)
    legacy = parsed.generate_matrix_jsons()
    legacy_digest = canonical_sha(legacy)
    legacy_members = [list(o.panel_ids) for o in parsed.optimizers]
    modern = parsed.identity_matrices()
    assert len(modern) == 1
    matrix = modern[0]
    cells = matrix["Optimizers"][0]["cells"]
    expected = {(0, 0), (0, 1)} if axis == "horizontal" else {(0, 0), (1, 0)}
    assert len(cells) == 2 and set(map(tuple, cells)) == expected
    assert matrix["Sequences"] == [2]
    panels = [matrix["Rows"][r]["Panels"][c] for r, c in cells]
    assert sorted(p["Seq"] for p in panels) == [1, 2]
    assert {(p["InverterId"], p["StringInputNumber"]) for p in panels} == {(3, 2)}
    assert [o.panel_ids for o in parsed.optimizers] == legacy_members
    assert canonical_sha(parsed.generate_matrix_jsons()) == legacy_digest
    assert parsed.identity_matrices() == modern


@pytest.mark.parametrize("ambiguity", ["competing-pairs", "shared-panel", "wide-gap",
                                        "duplicate-cell"])
def test_measured_pairs_refuse_ambiguous_ownership(ambiguity):
    import solar_solaredge_parse as parser
    page = measured_pair_page("horizontal", gap=8 if ambiguity == "wide-gap" else None)
    if ambiguity == "competing-pairs":
        page.paths.insert(0, page.paths[0])
    parsed, _ = parser.parse_primitives(page)
    for grid in parsed.final_grids:
        parsed._assign_row_column_coordinates(grid)
    if ambiguity == "shared-panel":
        original = parsed.optimizers[0]
        rival = parser.Optimizer(99, original.cx, original.cy, original.color)
        parsed.optimizers.append(rival)
    if ambiguity == "duplicate-cell":
        parsed.panels[1].row = parsed.panels[0].row
        parsed.panels[1].col = parsed.panels[0].col
    modern = parsed.identity_matrices()
    assert all(not opt["cells"] for matrix in modern for opt in matrix["Optimizers"])
    assert all(not matrix["Sequences"] for matrix in modern)


@pytest.mark.parametrize("bad", ["dotted-vendor", "bool-cell", "outside", "extra", "huge-id"])
def test_worker_rejects_malformed_optimizer_identity(bad):
    matrix = {"Rows": [{"Panels": [{"Code": 1}]}], "Optimizers": [
        {"id": "pdf-optimizer-1", "vendor_label": "12", "cells": [[0, 0]]}]}
    opt = matrix["Optimizers"][0]
    if bad == "dotted-vendor":
        opt["vendor_label"] = "1.2"
    elif bad == "bool-cell":
        opt["cells"] = [[False, 0]]
    elif bad == "outside":
        opt["cells"] = [[0, 1]]
    elif bad == "extra":
        opt["unexpected"] = 1
    else:
        opt["id"] = "pdf-optimizer-" + "1" * 1000
    out = json.dumps({"schema": worker.VERDICT_SCHEMA, "matrices": [matrix]}).encode()
    assert worker.decode_verdict(0, out) is None


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
