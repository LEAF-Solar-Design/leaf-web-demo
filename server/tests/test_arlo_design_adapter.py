"""Focused process, identity and custody checks for canonical ARLO designs."""
import json
from pathlib import Path
import subprocess
import time

import pytest

import canonical_worker
from solver_adapters import arlo_design


CONTEXT = {"org_id": "verified-org", "project_id": "verified-project",
           "input_version_id": "verified-version"}


def request():
    return {"contract": "arlo_design_request_v1", "organization_id": "spoofed-org",
            "project_id": "spoofed-project", "input_version_id": "spoofed-version",
            "scenario": {}, "catalog_version": "catalog-1", "requirements_version": "rules-1",
            "catalog": [{"id": "conduit-1"}], "requirements": {},
            "placement_candidates": [], "budget": {"timeout_seconds": 1.0}}


@pytest.fixture
def solver_root(tmp_path):
    package = tmp_path / "arlo" / "design"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text('''
import argparse, json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--input')
parser.add_argument('--output')
args = parser.parse_args()
body = json.loads(Path(args.input).read_text('utf-8'))
source = {key: body[key] for key in ('organization_id', 'project_id', 'input_version_id')}
source['request_hash'] = 'a' * 64
result = {'contract': 'arlo_design_result_v1', 'request_hash': 'a' * 64,
          'status': 'complete', 'proposals': [{'source': source, 'proposal_id': 'p1'}],
          'trace': [{'stage': 'selection', 'selected_ids': ['conduit-1']}],
          'diagnostics': {}, 'production_valid': False}
Path(args.output).write_text(json.dumps(result), encoding='utf-8')
''', encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def isolate_bundle_configuration(monkeypatch):
    monkeypatch.delenv("ARLO_MODEL_BUNDLE", raising=False)


def test_process_binds_durable_identity_and_hashes_entire_result(solver_root):
    params = request()
    result = arlo_design.run(params, job_context=CONTEXT, solver_root=solver_root)
    assert params["organization_id"] == "spoofed-org"
    assert result["solver_input"]["organization_id"] == CONTEXT["org_id"]
    assert result["solver_result"]["proposals"][0]["source"]["project_id"] == CONTEXT["project_id"]
    assert result["request_sha256"] == arlo_design._sha256(params)
    assert result["input_sha256"] == arlo_design._sha256(result["solver_input"])
    assert result["result_sha256"] == arlo_design._sha256(result["solver_result"])
    canonical = canonical_worker.platform_link._canonical_jobs_module()
    canonical._validate_success({"tool_name": "arlo-design", "params": params, "attempt": 1},
        result, {"attempt": 1, "execution_path": "local", **{
            key: result[key] for key in ("solver_revision", "source_sha256", "runtime")}})
    result["solver_result"]["trace"][0]["selected_ids"] = ["altered"]
    assert result["result_sha256"] != arlo_design._sha256(result["solver_result"])


@pytest.mark.parametrize("field", list(CONTEXT))
def test_context_is_required_even_when_request_claims_identity(field):
    context = dict(CONTEXT)
    del context[field]
    with pytest.raises(ValueError, match="canonical job requires"):
        arlo_design._validated_input(request(), context)


@pytest.mark.parametrize("change", [
    {"solver_root": "untrusted"}, {"contract": "unknown"}, {"scenario": []},
    {"catalog": []}, {"budget": {"timeout_seconds": float("nan")}},
    {"budget": {"timeout_seconds": 181}},
])
def test_invalid_request_refuses_before_process(change):
    with pytest.raises(ValueError):
        arlo_design._validated_input({**request(), **change}, CONTEXT)


def test_source_change_rejected(solver_root, monkeypatch):
    original = arlo_design.descriptor
    calls = 0

    def changing_descriptor(**kwargs):
        nonlocal calls
        calls += 1
        result = original(**kwargs)
        if calls == 2:
            result["source_sha256"] = "b" * 64
        return result

    monkeypatch.setattr(arlo_design, "descriptor", changing_descriptor)
    with pytest.raises(RuntimeError, match="changed during execution"):
        arlo_design.run(request(), job_context=CONTEXT, solver_root=solver_root)


def test_operator_model_bundle_is_invoked_and_hash_bound(solver_root, monkeypatch):
    path = solver_root / "arlo/design/__main__.py"
    path.write_text(path.read_text().replace("args = parser.parse_args()",
        "parser.add_argument('--bundle', required=True)\nargs = parser.parse_args()"))
    bundle = solver_root / "model-bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text('{"smoke": true}')
    monkeypatch.setenv("ARLO_MODEL_BUNDLE", str(bundle))
    result = arlo_design.run(request(), job_context=CONTEXT, solver_root=solver_root)
    (bundle / "selection.json").write_text('{"changed": true}')
    assert arlo_design.descriptor(solver_root=solver_root)["source_sha256"] != result["source_sha256"]


def test_cancelled_process_is_stopped(solver_root):
    (solver_root / "arlo/design/__main__.py").write_text("import time\ntime.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(arlo_design.SolveCancelled):
        arlo_design.run(request(), job_context=CONTEXT, solver_root=solver_root,
                        cancelled=lambda: time.monotonic() - started > 0.4)
    assert time.monotonic() - started < 5


def test_subprocess_timeout_is_bounded(solver_root):
    (solver_root / "arlo/design/__main__.py").write_text("import time\ntime.sleep(60)\n")
    with pytest.raises(subprocess.TimeoutExpired):
        arlo_design.run(request(), job_context=CONTEXT, solver_root=solver_root, timeout_s=0.1)


def test_foreign_proposal_cannot_enter_canonical_result(solver_root):
    path = solver_root / "arlo/design/__main__.py"
    path.write_text(path.read_text().replace("source['request_hash']", "source['organization_id'] = 'foreign'\nsource['request_hash']"))
    with pytest.raises(RuntimeError, match="identity differs"):
        arlo_design.run(request(), job_context=CONTEXT, solver_root=solver_root)


def test_native_readiness_cannot_be_claimed_by_solver(solver_root):
    path = solver_root / "arlo/design/__main__.py"
    path.write_text(path.read_text().replace("'production_valid': False", "'production_valid': True"))
    with pytest.raises(RuntimeError, match="production validity"):
        arlo_design.run(request(), job_context=CONTEXT, solver_root=solver_root)


@pytest.mark.parametrize("status", ["incomplete", "cancelled"])
def test_domain_outcome_is_preserved_on_cli_exit_two(solver_root, status):
    path = solver_root / "arlo/design/__main__.py"
    path.write_text(path.read_text().replace("'status': 'complete'", f"'status': '{status}'")
                    + "\nraise SystemExit(2)\n")
    result = arlo_design.run(request(), job_context=CONTEXT, solver_root=solver_root)
    assert result["solver_result"]["status"] == status


def test_catalog_registers_existing_canonical_dispatch():
    catalog_path = Path(__file__).resolve().parents[2] / "engine/registry.json"
    entry = next(tool for tool in json.loads(catalog_path.read_text())["tools"]
                 if tool["name"] == "arlo-design")
    assert entry["canonical_only"] is True
    assert entry["capabilities"] == ["solve"]
    assert arlo_design.TOOL_NAME in canonical_worker.ADAPTERS


def test_worker_selects_arlo_and_binds_context(monkeypatch):
    from test_canonical_worker import FakeCanonicalJobs
    job = {"job_id": "job-arlo", "attempt": 1, "tool_name": "arlo-design",
           "params": request(), **CONTEXT}
    store = FakeCanonicalJobs(job)
    monkeypatch.setattr(canonical_worker.platform_link, "_canonical_jobs_module", lambda: store)
    descriptor = {"tool_name": "arlo-design", "runtime": "python-test",
                  "source_revision": "revision-test", "source_sha256": "c" * 64}
    monkeypatch.setattr(canonical_worker.arlo_design, "descriptor", lambda: descriptor)

    def run(params, *, job_context, cancelled):
        assert job_context == job
        assert cancelled() is False
        assert params == job["params"]
        return {"solver_revision": descriptor["source_revision"],
                "source_sha256": descriptor["source_sha256"], "runtime": descriptor["runtime"]}

    monkeypatch.setitem(canonical_worker.ADAPTERS, "arlo-design", run)
    assert canonical_worker.run_once("worker-arlo", tool_name="arlo-design")
    assert store.claim_requests == [("worker-arlo", 30.0, "arlo-design")]
    assert len(store.completed) == 1


def test_worker_stops_arlo_on_lost_custody(monkeypatch):
    from test_canonical_worker import FakeCanonicalJobs
    store = FakeCanonicalJobs({"job_id": "job-arlo", "attempt": 1, "tool_name": "arlo-design",
                               "params": request(), **CONTEXT}, heartbeat_result=False)
    monkeypatch.setattr(canonical_worker.platform_link, "_canonical_jobs_module", lambda: store)
    monkeypatch.setattr(canonical_worker.arlo_design, "descriptor", lambda: {
        "tool_name": "arlo-design", "runtime": "python-test", "source_revision": "r",
        "source_sha256": "c" * 64})

    def run(params, *, job_context, cancelled):
        assert store.lease_renewed.wait(1.0)
        for _ in range(10):
            if cancelled():
                raise arlo_design.SolveCancelled("lease lost")
            time.sleep(0.01)
        pytest.fail("lost lease was not propagated to ARLO")

    monkeypatch.setitem(canonical_worker.ADAPTERS, "arlo-design", run)
    assert canonical_worker.run_once("worker-arlo", lease_seconds=0.15, tool_name="arlo-design")
    assert not store.completed and not store.failed
