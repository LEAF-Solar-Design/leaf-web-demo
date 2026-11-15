"""X02 grading and live orchestration contracts, without product I/O."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benches import x02
from xbench_studio import StudioError


PROJECT = {"project_id": "p", "org_id": "o"}
DRAWING = {"drawing_id": "d", "version_id": "v0"}
CAP = "private-checkout-capability-value"


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def refusal(case, **overrides):
    values = dict(status=422, error_code="GENERIC", reason_code=x02.BOUND_CODES[case],
                  message=f"Cannot use {x02.OBJECTS[case]}", next_action="Choose a valid value")
    values.update(overrides)
    return StudioError(**values)


class FakeClient:
    def __init__(self, timer):
        self.timer = timer
        self.overrides = {}
        self.calls = []
        self.releases = []
        self.checkouts = []
        self.head = "v0"
        self.rev = 0
        self.filter = False
        self.mutate = None
        self.context_missing = False
        self.expiry = None
        self.projects_deleted = []

    def drawing_context(self, project_id, version_id, drawing_id):
        assert (project_id, drawing_id) == ("p", "d")
        return {"context": {} if self.context_missing else
                {"head_version_id": self.head, "intake_sha256": "digest"},
                "intake": {"solar_design_graph": {"rev": self.rev}}}

    def checkout(self, project_id, drawing_id, *, ttl_s):
        self.checkouts.append(ttl_s)
        expires = self.timer.clock() + ttl_s
        self.expiry = expires
        return CAP + str(ttl_s), {"expires": datetime.fromtimestamp(expires, timezone.utc).isoformat()}

    def release(self, project_id, drawing_id, capability):
        self.releases.append(capability)

    def run(self, tool, **kw):
        self.calls.append((tool, kw))
        params = kw["params"]
        if tool == "count-by-layer":
            case = "3"
            assert kw["capability"] is None
        elif tool == "solar-panels-from-drawing":
            case = "4b" if self.filter else "4a"
        elif params.get("initialize", {}).get("units", {}).get("drawing_units") == "unknown":
            case = "7"
        elif kw["capability"] == CAP + "2":
            case = "8"
            assert self.timer.clock() >= self.expiry + 4
        else:
            case = "filter" if "panel_layer_contains" in params["changes"] else "initialize"
        if case == self.mutate:
            self.head = "unexpected-version"
        response = self.overrides.get(case)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        if case in ("initialize", "filter"):
            assert params["expected_rev"] == self.rev
            assert kw["version_id"] == self.head
            self.rev += 1
            self.head = "v" + str(self.rev)
            self.filter = case == "filter"
            return 202, {"job_id": self.head}
        raise refusal(case)

    def wait_job(self, job_id):
        return {"status": "complete", "result": {"output_version_id": job_id}}

    def transport(self, method, path, **kw):
        self.pdf_call = (method, path, kw)
        response = self.overrides.get("9")
        if response is not None:
            return response
        return 422, {"error": {"reason_code": "IMPORT_PDF_MALFORMED",
                               "message": "Malformed PDF", "next_action": "Upload a valid PDF"}}

    def create_project(self, name):
        self.project_name = name
        return PROJECT

    def delete_project(self, project_id):
        self.projects_deleted.append(project_id)

    def upload_drawing(self, filename, content):
        assert (filename, content) == ("rooftop_demo.dwg", b"drawing")
        return "uploaded"

    def wait_upload(self, drawing_id):
        assert drawing_id == "uploaded"

    def adopt_drawing(self, project_id, uploaded, name):
        assert (project_id, uploaded, name) == ("p", "uploaded", "rooftop_demo.dwg")
        return DRAWING


@pytest.fixture
def rig():
    timer = FakeTime()
    return FakeClient(timer), timer


def collect(rig):
    client, timer = rig
    return x02.run_cases(client, client.transport, project=PROJECT, drawing=DRAWING,
                         sleep=timer.sleep, clock=timer.clock)


def by_case(results, case):
    return next(row for row in results if row.case == case)


def test_all_correct_slice_is_unreadable(rig):
    results = collect(rig)
    assert [row.case for row in results] == list(x02.CASE_IDS)
    assert {row.case for row in results if row.seeded} == set(x02.OBJECTS)
    assert [step.verdict for step in x02.score(results)] == ["UNREADABLE"] * 6
    assert all(row.object_named and row.state_before == row.state_after
               for row in results if row.seeded)
    assert all(f"case {case}: UNREADABLE" in x02.score(results)[0].evidence
               for case in ("1", "2", "5", "6"))


def test_staging_zero_count_is_failure(rig):
    rig[0].overrides["3"] = (200, {"ok": True, "result": {"count": 0}})
    steps = x02.score(collect(rig))
    assert [step.verdict for step in steps[:2]] == ["FAIL", "FAIL"]
    assert all("case 3: FAIL" in step.evidence for step in steps[:2])


def test_state_change_fails(rig):
    rig[0].mutate = "3"
    step = x02.score(collect(rig))[2]
    assert step.verdict == "FAIL"
    assert "case 3: FAIL (drawing state changed)" in step.evidence


def test_wrong_code_fails(rig):
    rig[0].overrides["3"] = refusal("3", reason_code="WRONG")
    step = x02.score(collect(rig))[0]
    assert step.verdict == "FAIL"
    assert step.observed["3"] == "WRONG"
    assert step.expected["3"] == "LAYER_NOT_FOUND"


def test_missing_action_fails(rig):
    rig[0].overrides["3"] = refusal("3", next_action="  ")
    assert x02.score(collect(rig))[0].verdict == "FAIL"


def test_missing_object_fails(rig):
    rig[0].overrides["3"] = refusal("3", message="Not available")
    assert x02.score(collect(rig))[0].verdict == "FAIL"


def test_object_in_next_action_case_insensitive(rig):
    rig[0].overrides["3"] = refusal("3", message="Missing", next_action="Replace xbench_absent_layer")
    assert by_case(collect(rig), "3").object_named is True


def test_error_code_fallback(rig):
    rig[0].overrides["3"] = refusal("3", reason_code=None, error_code="LAYER_NOT_FOUND")
    assert by_case(collect(rig), "3").code == "LAYER_NOT_FOUND"


def test_initialize_refusal_skips_dependents(rig):
    rig[0].overrides["initialize"] = StudioError(409, "INIT_REFUSED", "Initialization blocked")
    results = collect(rig)
    for case in ("4a", "4b", "9", "8"):
        row = by_case(results, case)
        assert not row.seeded and "INIT_REFUSED" in row.note
    assert by_case(results, "7").seeded


def test_filter_refusal_skips_dependents(rig):
    rig[0].overrides["filter"] = StudioError(409, "FILTER_REFUSED", "Filter blocked")
    results = collect(rig)
    assert by_case(results, "4a").seeded
    assert all(not by_case(results, case).seeded for case in ("4b", "9", "8"))


def test_expiry_wait_and_unique_keys(rig):
    collect(rig)
    client, timer = rig
    assert client.checkouts == [900, 2]
    assert sum(timer.sleeps) == 6
    keys = [kw["idempotency_key"] for _, kw in client.calls]
    assert len(keys) == len(set(keys))
    assert client.calls[-1][1]["capability"] == CAP + "2"


def test_published_version_and_revision_used(rig):
    collect(rig)
    calls = rig[0].calls
    panels = [kw for tool, kw in calls if tool == "solar-panels-from-drawing"]
    assert [(kw["version_id"], kw["params"]["expected_rev"]) for kw in panels] == [("v1", 1), ("v2", 2)]


def test_pdf_transport_uses_current_version(rig):
    collect(rig)
    method, path, kw = rig[0].pdf_call
    assert method == "POST"
    assert path == "/api/projects/p/drawings/d/versions/v2/imports/solaredge-pdf"
    assert kw == {"headers": {"Content-Type": "application/pdf"}, "data": x02.PDF}


def test_missing_state_unreadable(rig):
    results = collect(rig)
    results = [replace(row, state_before=None, state_after=None) if row.seeded else row for row in results]
    assert x02.score(results)[2].verdict == "UNREADABLE"


def test_one_missing_state_also_differs(rig):
    results = collect(rig)
    results = [replace(row, state_after=None) if row.case == "3" else row for row in results]
    assert x02.score(results)[2].verdict == "FAIL"


@pytest.mark.parametrize("timeout_code", ["SOLVE_TIMEOUT", "solve_outcome_unknown"])
def test_complete_synthetic_results_pass_first_three_steps(timeout_code):
    results = [x02.CaseResult(case, seeded=True, status=422,
                             code=timeout_code if case == "6" else code,
                             next_action="Choose a valid value", object_named=True,
                             state_before=("head", "digest"), state_after=("head", "digest"))
               for case, code in x02.BOUND_CODES.items()]
    assert [step.verdict for step in x02.score(results)] == ["PASS"] * 3 + ["UNREADABLE"] * 3


def test_evidence_keeps_every_case_with_long_setup_error(rig):
    rig[0].overrides["initialize"] = StudioError(409, "BLOCKED", "x" * 5000)
    steps = x02.score(collect(rig))
    for step in steps[:3]:
        assert len(step.evidence) < 2000
        assert all(f"case {case}:" in step.evidence for case in x02.BOUND_CODES)


@pytest.mark.parametrize("results", [[], [x02.CaseResult("3")]])
def test_score_always_six_steps(results):
    steps = x02.score(results)
    assert [step.n for step in steps] == list(range(1, 7))
    assert all(len(step.evidence) < 2000 for step in steps)
    assert "case 10" in steps[3].evidence
    assert "case 6" in steps[4].evidence
    assert "UI" in steps[5].evidence


def test_ok_envelope_even_on_error_fails(rig):
    rig[0].overrides["3"] = refusal("3", body={"ok": True})
    assert x02.score(collect(rig))[1].verdict == "FAIL"


def test_async_failed_job_is_refusal(rig, monkeypatch):
    client = rig[0]
    client.overrides["3"] = (202, {"job_id": "failed-job"})
    original = client.wait_job
    monkeypatch.setattr(client, "wait_job", lambda job: {"status": "failed", "error": {
        "reason_code": "LAYER_NOT_FOUND", "message": "XBENCH_ABSENT_LAYER missing",
        "next_action": "Choose a layer"}} if job == "failed-job" else original(job))
    row = by_case(collect(rig), "3")
    assert (row.status, row.code) == (422, "LAYER_NOT_FOUND")


def test_transport_adapter_json_and_bytes(monkeypatch):
    replies = iter([(422, {"Content-Type": "application/problem+json"}, b'{"error": {}}'),
                    (200, {"content-type": "application/octet-stream"}, b"bytes")])
    calls = []
    def api(*args, **kw):
        calls.append((args, kw))
        return next(replies)
    monkeypatch.setattr(x02.xbench_session, "api", api)
    transport = x02._transport("page", origin="https://example.test")
    assert transport("POST", "/api/test", data=b"pdf") == (422, {"error": {}})
    assert transport("GET", "/api/test") == (200, b"bytes")
    assert calls[0][1] == {"origin": "https://example.test", "data": b"pdf"}


def test_run_writes_safe_cases_and_cleans_up(rig, monkeypatch, tmp_path):
    client, timer = rig
    fixture = tmp_path / "input.dwg"
    fixture.write_bytes(b"drawing")
    ctx = SimpleNamespace(fixtures=[], manifest={"fixtures": [{"id": "rooftop-demo", "path": str(fixture)}]},
                          out_dir=tmp_path, source_sha="a" * 40, base="https://example.test")
    @contextmanager
    def signed_in(repo, source_sha, *, origin):
        assert repo == Path(x02.__file__).resolve().parents[3]
        assert source_sha == ctx.source_sha and origin == ctx.base
        yield "page", {}
    monkeypatch.setattr(x02.xbench_session, "signed_in", signed_in)
    monkeypatch.setattr(x02, "StudioClient", lambda transport: client)
    monkeypatch.setattr(x02, "_transport", lambda page, **kw: client.transport)
    original = x02.run_cases
    monkeypatch.setattr(x02, "run_cases", lambda *args, **kw: original(
        *args, **kw, sleep=timer.sleep, clock=timer.clock))
    client.overrides["3"] = refusal("3", message="XBENCH_ABSENT_LAYER " + CAP + "900")
    client.overrides["8"] = refusal("8", message="checkout " + CAP + "2")
    steps = x02.run(ctx)
    raw = (tmp_path / "cases.json").read_text()
    assert CAP not in raw
    assert "X-Checkout-Capability" not in raw
    assert len(json.loads(raw)) == len(x02.CASE_IDS)
    assert len(steps) == 6
    assert client.checkouts == [900, 2]
    assert client.projects_deleted == ["p"]
