from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import sysconfig
import unittest
from unittest import mock
import zipfile

import yaml


_platform_spec = importlib.util.spec_from_file_location(
    "platform", Path(sysconfig.get_path("stdlib")) / "platform.py"
)
assert _platform_spec and _platform_spec.loader
_stdlib_platform = importlib.util.module_from_spec(_platform_spec)
sys.modules["platform"] = _stdlib_platform
_platform_spec.loader.exec_module(_stdlib_platform)
jsonschema = importlib.import_module("jsonschema")

from scripts import platform_staging_convergence as subject  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "a" * 40
APP_TREE = "b" * 40
BUILD_BLOB = "c" * 40
RELAY_BLOB = "d" * 40
TF_HEAD = "e" * 40
TF_TREE = "f" * 40
TF_BLOB = "1" * 40
BUILD_RUN = 100
RELAY_RUN = 200
FRONTIER_RUN = 306
BUILD_TAG = "prod-aaaaaaa"
SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
DIGESTS = {
    service: f"sha256:{digit * 64}"
    for service, digit in zip(SERVICES, "23456", strict=True)
}


class ArtifactRedirectTests(unittest.TestCase):
    def test_cross_host_artifact_redirect_strips_authorization(self) -> None:
        handler = subject._ArtifactRedirectHandler()
        request = subject.urllib.request.Request(
            "https://api.github.com/repos/o/r/actions/artifacts/1/zip",
            headers={"Authorization": "Bearer private"},
        )
        with mock.patch.object(subject.urllib.request.HTTPRedirectHandler, "redirect_request", return_value=request):
            redirected = handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://example.blob.core.windows.net/result?sig=redacted",
            )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def archive(filename: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as value:
        value.writestr(filename, payload)
    return buffer.getvalue()


def run(
    run_id: int,
    *,
    repository: str,
    workflow: str,
    event: str,
    head_sha: str,
    created: str,
    conclusion: str = "success",
) -> dict:
    return {
        "id": run_id,
        "run_attempt": 1,
        "event": event,
        "head_sha": head_sha,
        "head_branch": "main",
        "path": workflow,
        "status": "completed",
        "conclusion": conclusion,
        "created_at": created,
        "updated_at": created,
        "repository": {"full_name": repository},
    }


def artifact(artifact_id: int, name: str, run_id: int, head_sha: str) -> dict:
    return {
        "id": artifact_id,
        "name": name,
        "expired": False,
        "workflow_run": {"id": run_id, "head_sha": head_sha},
    }


def manifest() -> dict:
    entries = {
        service: {
            "repository": f"leaf-platform-{service}",
            "image_digest": DIGESTS[service],
            "source_revision": SOURCE,
        }
        for service in SERVICES
    }
    entries["canonical-worker"]["provenance"] = {
        "application_source_revision": SOURCE,
        "solver_source_revision": "7" * 40,
        "solver_source_sha256": "8" * 64,
    }
    entries["web"]["artifact_sha256"] = "9" * 64
    return {
        "schema": "leaf.staging-supply-set.v1",
        "source_revision": SOURCE,
        "build_tag": BUILD_TAG,
        "services": entries,
    }


def missing() -> dict:
    return {"status": "not_produced"}


def produced(value: object) -> dict:
    return {"status": "produced", "value": value}


def identity() -> dict:
    body = {
        "schema": "leaf.deployment-identity.v1",
        "environment": "staging",
        "source_revision": SOURCE,
        "services": {
            service: {"image_digest": DIGESTS[service], "source_revision": SOURCE}
            for service in SERVICES
        },
    }
    raw = (json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode()
    return {"body": body, "sha256": hashlib.sha256(raw).hexdigest()}


def service_receipt(
    service: str,
    run_id: int,
    *,
    predecessor: str,
    terminal_td: str,
    with_identity: bool = False,
) -> dict:
    requested = {
        "allow_non_forward_image": missing(),
        "app_deploy_intent": "configuration" if with_identity else "forward",
        "configuration_delta": missing(),
        "configuration_task_definition": predecessor if with_identity else "not_produced",
        "convergence_id": "not_produced",
        "deploy_strategy": "direct",
        "digest_aware_evidence": missing(),
        "digest_aware_reconcile": False,
        "expected_task_definition": predecessor,
        "hold_seconds": "0",
        "image_tag": BUILD_TAG,
        "p4a_session_identity_cutover": missing(),
        "quarantine_recovery_snapshot_identifier": missing(),
        "required_broker_task_definition": "not_produced",
        "service": service,
        "snapshot_overflow_acknowledgement": missing(),
        "source_revision": SOURCE,
        "start_from_zero": False,
        "start_from_zero_confirmation": missing(),
        "target_color": "live",
    }
    facts = {
        "schema": "leaf.platform-staging-service-facts.v1",
        "service": produced(service),
        "source": {"revision": produced(SOURCE), "tree": produced(APP_TREE)},
        "supply": produced(
            {
                "artifact_id": 1000,
                "artifact_name": f"staging-supply-set-{SOURCE}-attempt-1",
                "manifest_sha256": hashlib.sha256(canonical(manifest())).hexdigest(),
                "producer_run_id": BUILD_RUN,
                "producer_run_attempt": 1,
            }
        ),
        "predecessor_task_definition": produced(predecessor),
        "candidate": {
            "task_definition": produced(terminal_td),
            "image_digest": produced(DIGESTS[service]),
        },
        "terminal": produced(
            {
                "service": f"leaf-platform-{service}",
                "task_definition": terminal_td,
                "image_digest": produced(DIGESTS[service]),
                "capacity": {"desired": 1, "running": 1, "pending": 0},
                "primary_deployments": [
                    {
                        "task_definition": terminal_td,
                        "rollout_state": "COMPLETED",
                        "status": "PRIMARY",
                    }
                ],
                "stable_1_1_0": True,
            }
        ),
        "mutation_count": produced(1),
        "prior_job_status": produced("success"),
        "rollback": produced(
            {
                "bluegreen_step": "skipped",
                "bluegreen_detail": "not_produced",
                "direct_failure_step": "skipped",
                "direct_cancel_step": "skipped",
                "authority_result": "not_produced",
            }
        ),
        "route": missing(),
        "p4a": missing(),
        "deployment_identity": produced(identity()) if with_identity else missing(),
        "marker": missing(),
        "writer_census": missing(),
    }
    value = {
        "schema": "leaf.platform-staging-service-run.v1",
        "environment": "staging",
        "provider": {
            "repository": subject.TF_REPOSITORY,
            "workflow_path": subject.DEPLOY_WORKFLOW,
            "workflow_blob": TF_BLOB,
            "run_id": run_id,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "head_sha": TF_HEAD,
        },
        "requested": requested,
        "path": "deploy",
        "preflight_result": "skipped",
        "deploy_result": "success",
        "terminal_result": "success",
        "failed_stage": missing(),
        "facts": facts,
        "receipt_sha256": "",
    }
    value["receipt_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
    return value


class FakeProvider:
    def __init__(self) -> None:
        self.json_values: dict[tuple[str, str], object] = {}
        self.byte_values: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, str, str]] = []

    def json(self, repository: str, endpoint: str) -> object:
        self.calls.append(("GET_JSON", repository, endpoint))
        key = (repository, endpoint)
        if key not in self.json_values:
            raise subject.ContractError("PROVIDER_FIXTURE_MISSING")
        return copy.deepcopy(self.json_values[key])

    def bytes(self, repository: str, endpoint: str) -> bytes:
        self.calls.append(("GET_BYTES", repository, endpoint))
        key = (repository, endpoint)
        if key not in self.byte_values:
            raise subject.ContractError("PROVIDER_FIXTURE_MISSING")
        return self.byte_values[key]


def fixture() -> FakeProvider:
    provider = FakeProvider()
    app = subject.APP_REPOSITORY
    tf = subject.TF_REPOSITORY
    build = run(
        BUILD_RUN,
        repository=app,
        workflow=subject.BUILD_WORKFLOW,
        event="push",
        head_sha=SOURCE,
        created="2026-08-13T01:00:00Z",
    )
    relay = run(
        RELAY_RUN,
        repository=app,
        workflow=subject.RELAY_WORKFLOW,
        event="workflow_run",
        head_sha=SOURCE,
        created="2026-08-13T01:10:00Z",
    )
    provider.json_values[(app, f"/actions/runs/{BUILD_RUN}")] = build
    provider.json_values[(app, "/branches/main")] = {"commit": {"sha": SOURCE}}
    provider.json_values[(app, f"/git/commits/{SOURCE}")] = {"tree": {"sha": APP_TREE}}
    provider.json_values[(app, f"/git/trees/{APP_TREE}?recursive=1")] = {
        "tree": [
            {"path": subject.BUILD_WORKFLOW, "type": "blob", "sha": BUILD_BLOB},
            {"path": subject.RELAY_WORKFLOW, "type": "blob", "sha": RELAY_BLOB},
        ]
    }
    supply_name = f"staging-supply-set-{SOURCE}-attempt-1"
    supply_artifact = artifact(1000, supply_name, BUILD_RUN, SOURCE)
    provider.json_values[(app, f"/actions/runs/{BUILD_RUN}/artifacts?per_page=100")] = {
        "total_count": 1,
        "artifacts": [supply_artifact],
    }
    provider.byte_values[(app, "/actions/artifacts/1000/zip")] = archive(
        "staging-supply-set.json",
        (json.dumps(manifest(), indent=2, sort_keys=True) + "\n").encode(),
    )
    query = "event=workflow_run&status=completed&head_sha=" + SOURCE + "&per_page=100"
    provider.json_values[(app, f"/actions/workflows/dispatch-staging-deploys.yml/runs?{query}")] = {
        "total_count": 1,
        "workflow_runs": [relay],
    }
    relay_name = f"staging-converged-{SOURCE}-attempt-1"
    provider.json_values[(app, f"/actions/runs/{RELAY_RUN}/artifacts?per_page=100")] = {
        "total_count": 1,
        "artifacts": [artifact(2000, relay_name, RELAY_RUN, SOURCE)],
    }
    relay_receipt = {
        "schema": "leaf.staging-converged.v1",
        "source_revision": SOURCE,
        "build_run_attempt": "1",
        "build_tag": BUILD_TAG,
        "relay_run_id": RELAY_RUN,
        "services": ["web", "app"],
    }
    provider.byte_values[(app, "/actions/artifacts/2000/zip")] = archive(
        "staging-converged.json", canonical(relay_receipt)
    )
    provider.byte_values[(app, f"/actions/runs/{RELAY_RUN}/logs")] = archive(
        "0_dispatch.txt",
        b"Watching web deploy run 301.\nWatching app deploy run 302.\n",
    )

    child_specs = (
        (301, "web", "leaf-platform-web:1", "leaf-platform-web:2", False),
        (302, "app", "leaf-platform-app:1", "leaf-platform-app:2", False),
        (303, "broker", "leaf-platform-broker:1", "leaf-platform-broker:2", False),
        (304, "harness", "leaf-platform-harness:1", "leaf-platform-harness:2", False),
        (305, "canonical-worker", "leaf-platform-canonical-worker:1", "leaf-platform-canonical-worker:2", False),
        (306, "app", "leaf-platform-app:2", "leaf-platform-app:3", True),
    )
    child_runs = []
    provider.json_values[(tf, f"/git/commits/{TF_HEAD}")] = {"tree": {"sha": TF_TREE}}
    provider.json_values[(tf, f"/git/trees/{TF_TREE}?recursive=1")] = {
        "tree": [{"path": subject.DEPLOY_WORKFLOW, "type": "blob", "sha": TF_BLOB}]
    }
    for index, (run_id, service, predecessor, terminal, has_identity) in enumerate(child_specs, 20):
        created = f"2026-08-13T01:{index:02d}:00Z"
        child = run(
            run_id,
            repository=tf,
            workflow=subject.DEPLOY_WORKFLOW,
            event="workflow_dispatch",
            head_sha=TF_HEAD,
            created=created,
        )
        child_runs.append(child)
        provider.json_values[(tf, f"/actions/runs/{run_id}")] = child
        name = f"leaf-platform-staging-service-run-{run_id}-attempt-1"
        provider.json_values[(tf, f"/actions/runs/{run_id}/artifacts?per_page=100")] = {
            "total_count": 1,
            "artifacts": [artifact(run_id + 1000, name, run_id, TF_HEAD)],
        }
        provider.byte_values[(tf, f"/actions/artifacts/{run_id + 1000}/zip")] = archive(
            subject.ARTIFACT_FILE,
            canonical(
                service_receipt(
                    service,
                    run_id,
                    predecessor=predecessor,
                    terminal_td=terminal,
                    with_identity=has_identity,
                )
            ),
        )
        provider.json_values[(tf, f"/actions/runs/{run_id}/attempts/1/jobs?per_page=100")] = {
            "total_count": 1,
            "jobs": [
                {
                    "name": "Deploy",
                    "steps": [{"name": "Done", "number": 1, "conclusion": "success"}],
                }
            ],
        }
    provider.json_values[(tf, f"/actions/runs/{FRONTIER_RUN}")] = copy.deepcopy(child_runs[-1])
    provider.json_values[(tf, "/actions/workflows/deploy-leaf-platform-staging.yml/runs?event=workflow_dispatch&per_page=100")] = {
        "total_count": len(child_runs),
        "workflow_runs": child_runs,
    }
    return provider


def replace_receipt(provider: FakeProvider, run_id: int, mutate) -> None:
    key = (subject.TF_REPOSITORY, f"/actions/artifacts/{run_id + 1000}/zip")
    with zipfile.ZipFile(io.BytesIO(provider.byte_values[key])) as value:
        receipt = json.loads(value.read(subject.ARTIFACT_FILE))
    mutate(receipt)
    receipt["receipt_sha256"] = ""
    receipt["receipt_sha256"] = hashlib.sha256(canonical(receipt)).hexdigest()
    provider.byte_values[key] = archive(subject.ARTIFACT_FILE, canonical(receipt))


class ConvergenceFinalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(
            (ROOT / "contract/platform-staging-convergence.v1.schema.json").read_text(
                encoding="utf-8"
            )
        )

    def assert_reason(self, reason: str, provider: FakeProvider) -> None:
        with self.assertRaisesRegex(subject.ContractError, f"^{reason}$"):
            subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)

    def test_future_provider_shape_produces_one_complete_strict_receipt(self) -> None:
        provider = fixture()
        receipt = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        jsonschema.Draft202012Validator(self.schema).validate(receipt)
        self.assertTrue(receipt["terminal_complete"])
        self.assertEqual(receipt["relay"]["children"], {"web": 301, "app": 302})
        self.assertEqual(receipt["services"]["app"]["selected_run_id"], FRONTIER_RUN)
        self.assertEqual(len(receipt["services"]["app"]["attempts"]), 2)
        self.assertEqual(receipt["deployment_identity"]["producer_run_id"], FRONTIER_RUN)
        self.assertNotEqual(receipt["supply"]["file_sha256"], receipt["supply"]["manifest_sha256"])
        copy_receipt = copy.deepcopy(receipt)
        checksum = copy_receipt["evidence_sha256"]
        copy_receipt["evidence_sha256"] = ""
        self.assertEqual(checksum, hashlib.sha256(canonical(copy_receipt)).hexdigest())
        self.assertTrue(all(call[0].startswith("GET_") for call in provider.calls))

    def test_successful_bluegreen_cleanup_is_not_classified_as_rollback(self) -> None:
        provider = fixture()
        replace_receipt(
            provider,
            303,
            lambda value: value["facts"]["rollback"]["value"].__setitem__(
                "bluegreen_step", "success"
            ),
        )
        receipt = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        outcome = receipt["services"]["broker"]["attempts"][0]["outcome"]
        self.assertEqual(outcome["deployment_outcome"], "succeeded")
        self.assertEqual(outcome["rollback_outcome"], "not_required")
        self.assertEqual(outcome["mutation_count"], 1)

    def test_one_provider_bound_pre_mutation_failure_is_preserved_before_resume(self) -> None:
        provider = fixture()
        failed_run_id = 307
        failed_run = run(
            failed_run_id,
            repository=subject.TF_REPOSITORY,
            workflow=subject.DEPLOY_WORKFLOW,
            event="workflow_dispatch",
            head_sha=TF_HEAD,
            created="2026-08-13T01:21:30Z",
            conclusion="failure",
        )
        list_key = (
            subject.TF_REPOSITORY,
            "/actions/workflows/deploy-leaf-platform-staging.yml/runs?event=workflow_dispatch&per_page=100",
        )
        provider.json_values[list_key]["workflow_runs"].append(failed_run)
        provider.json_values[list_key]["total_count"] += 1
        name = f"leaf-platform-staging-service-run-{failed_run_id}-attempt-1"
        provider.json_values[(subject.TF_REPOSITORY, f"/actions/runs/{failed_run_id}/artifacts?per_page=100")] = {
            "total_count": 1,
            "artifacts": [artifact(1307, name, failed_run_id, TF_HEAD)],
        }
        receipt = service_receipt(
            "broker",
            failed_run_id,
            predecessor="leaf-platform-broker:1",
            terminal_td="leaf-platform-broker:1",
        )
        receipt["deploy_result"] = "failure"
        receipt["terminal_result"] = "failure"
        receipt["failed_stage"] = produced(
            {
                "primary": {"job": "Deploy", "step": "Resolve inputs", "number": 1},
                "additional": [],
                "unique": True,
            }
        )
        receipt["facts"]["candidate"] = {
            "task_definition": missing(),
            "image_digest": missing(),
        }
        receipt["facts"]["terminal"] = missing()
        receipt["facts"]["mutation_count"] = produced(0)
        receipt["facts"]["prior_job_status"] = produced("failure")
        receipt["receipt_sha256"] = ""
        receipt["receipt_sha256"] = hashlib.sha256(canonical(receipt)).hexdigest()
        provider.byte_values[(subject.TF_REPOSITORY, "/actions/artifacts/1307/zip")] = archive(
            subject.ARTIFACT_FILE, canonical(receipt)
        )
        provider.json_values[(subject.TF_REPOSITORY, f"/actions/runs/{failed_run_id}/attempts/1/jobs?per_page=100")] = {
            "total_count": 1,
            "jobs": [
                {
                    "name": "Deploy",
                    "steps": [{"name": "Resolve inputs", "number": 1, "conclusion": "failure"}],
                }
            ],
        }
        result = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        broker = result["services"]["broker"]
        self.assertEqual(len(broker["attempts"]), 2)
        self.assertEqual(broker["attempts"][0]["role"], "prior_failed_broker")
        self.assertEqual(
            broker["attempts"][0]["outcome"],
            {
                "preflight_state": "not_required",
                "mutation_state": "not_started",
                "mutation_count": 0,
                "deployment_outcome": "failed_before_mutation",
                "rollback_outcome": "not_required",
                "receipt_outcome": "verified",
                "failed_stage": "input_resolution",
                "terminal_reason_code": "pre_mutation_failure",
                "terminal_healthy": False,
            },
        )
        self.assertEqual(broker["selected_run_id"], 303)
        jsonschema.Draft202012Validator(self.schema).validate(result)

    def test_post_mutation_failure_is_normalized_only_after_exact_predecessor_restore(self) -> None:
        provider = fixture()
        failed_run_id = 307
        failed_run = run(
            failed_run_id,
            repository=subject.TF_REPOSITORY,
            workflow=subject.DEPLOY_WORKFLOW,
            event="workflow_dispatch",
            head_sha=TF_HEAD,
            created="2026-08-13T01:21:30Z",
            conclusion="failure",
        )
        list_key = (
            subject.TF_REPOSITORY,
            "/actions/workflows/deploy-leaf-platform-staging.yml/runs?event=workflow_dispatch&per_page=100",
        )
        provider.json_values[list_key]["workflow_runs"].append(failed_run)
        provider.json_values[list_key]["total_count"] += 1
        name = f"leaf-platform-staging-service-run-{failed_run_id}-attempt-1"
        provider.json_values[(subject.TF_REPOSITORY, f"/actions/runs/{failed_run_id}/artifacts?per_page=100")] = {
            "total_count": 1,
            "artifacts": [artifact(1307, name, failed_run_id, TF_HEAD)],
        }
        receipt = service_receipt(
            "broker",
            failed_run_id,
            predecessor="leaf-platform-broker:1",
            terminal_td="leaf-platform-broker:1",
        )
        receipt["deploy_result"] = "failure"
        receipt["terminal_result"] = "failure"
        receipt["failed_stage"] = produced(
            {
                "primary": {
                    "job": "Deploy",
                    "step": "Promote one ECS service and verify task health",
                    "number": 40,
                },
                "additional": [],
                "unique": True,
            }
        )
        receipt["facts"]["candidate"]["task_definition"] = produced("leaf-platform-broker:2")
        receipt["facts"]["terminal"]["value"]["image_digest"] = produced("sha256:" + "9" * 64)
        receipt["facts"]["mutation_count"] = produced(2)
        receipt["facts"]["prior_job_status"] = produced("failure")
        receipt["facts"]["rollback"]["value"]["direct_failure_step"] = "success"
        receipt["receipt_sha256"] = ""
        receipt["receipt_sha256"] = hashlib.sha256(canonical(receipt)).hexdigest()
        provider.byte_values[(subject.TF_REPOSITORY, "/actions/artifacts/1307/zip")] = archive(
            subject.ARTIFACT_FILE, canonical(receipt)
        )
        provider.json_values[(subject.TF_REPOSITORY, f"/actions/runs/{failed_run_id}/attempts/1/jobs?per_page=100")] = {
            "total_count": 1,
            "jobs": [
                {
                    "name": "Deploy",
                    "steps": [
                        {
                            "name": "Promote one ECS service and verify task health",
                            "number": 40,
                            "conclusion": "failure",
                        }
                    ],
                }
            ],
        }
        result = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        attempt = result["services"]["broker"]["attempts"][0]
        self.assertEqual(
            attempt["outcome"],
            {
                "preflight_state": "not_required",
                "mutation_state": "predecessor_restored",
                "mutation_count": 2,
                "deployment_outcome": "failed_after_mutation_rolled_back",
                "rollback_outcome": "succeeded",
                "receipt_outcome": "verified",
                "failed_stage": "service_promotion",
                "terminal_reason_code": "post_mutation_predecessor_restored",
                "terminal_healthy": True,
            },
        )
        jsonschema.Draft202012Validator(self.schema).validate(result)

    def test_raw_outcome_text_never_enters_output_and_unknown_or_conflicting_values_fail(self) -> None:
        result = subject._build_receipt(fixture(), BUILD_RUN, FRONTIER_RUN)
        encoded = canonical(result).decode()
        for forbidden in (
            "preflight_result",
            "deploy_result",
            "terminal_result",
            "prior_job_status",
            "canonical_json",
            "service_receipt_sha256",
            "requested_sha256",
            "facts_sha256",
        ):
            self.assertNotIn(forbidden, encoded)

        injected = (
            "x" * (subject.MAX_SOURCE_STRING_BYTES + 1),
            "Bearer should-not-land",
            "control\u0001text",
            "line\nbreak",
            '{"error":"raw"}',
            "https://example.invalid/provider-error",
            "Traceback (most recent call last): frame",
            "unknown-result",
        )
        for field in ("preflight_result", "deploy_result", "terminal_result"):
            for raw in injected:
                with self.subTest(field=field, raw=raw[:24]):
                    provider = fixture()
                    replace_receipt(provider, 303, lambda value, field=field, raw=raw: value.__setitem__(field, raw))
                    with self.assertRaises(subject.ContractError):
                        subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)

        provider = fixture()
        replace_receipt(
            provider,
            303,
            lambda value: value["facts"]["prior_job_status"].__setitem__("value", "stack trace"),
        )
        self.assert_reason("SERVICE_OUTCOME_LABEL_INVALID", provider)

        provider = fixture()
        replace_receipt(provider, 303, lambda value: value.__setitem__("deploy_result", "failure"))
        self.assert_reason("SERVICE_OUTCOME_CONFLICT", provider)

    def test_raw_failed_step_text_is_allowlisted_and_never_published(self) -> None:
        for raw in (
            "unknown provider step",
            "https://example.invalid/error",
            '{"stack":"frame"}',
            "line\nbreak",
            "Bearer should-not-land",
            "x" * (subject.MAX_SOURCE_STRING_BYTES + 1),
        ):
            with self.subTest(raw=raw[:24]):
                provider = fixture()
                provider.json_values[(subject.TF_REPOSITORY, "/actions/runs/303/attempts/1/jobs?per_page=100")] = {
                    "total_count": 1,
                    "jobs": [{"name": "Deploy", "steps": [{"name": raw, "number": 1, "conclusion": "failure"}]}],
                }

                def alter(value: dict) -> None:
                    value["failed_stage"] = produced(
                        {
                            "primary": {"job": "Deploy", "step": raw, "number": 1},
                            "additional": [],
                            "unique": True,
                        }
                    )

                replace_receipt(provider, 303, alter)
                with self.assertRaises(subject.ContractError):
                    subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)

    def test_raw_rollback_text_is_closed_and_cannot_override_structured_facts(self) -> None:
        for key, raw in (
            ("bluegreen_step", "unknown"),
            ("direct_failure_step", "https://example.invalid/error"),
            ("direct_cancel_step", "Traceback frame"),
            ("bluegreen_detail", '{"error":"raw"}'),
            ("bluegreen_detail", "line\nbreak"),
            ("bluegreen_detail", "Bearer should-not-land"),
            ("authority_result", "x" * (subject.MAX_SOURCE_STRING_BYTES + 1)),
        ):
            with self.subTest(key=key, raw=raw[:24]):
                provider = fixture()
                replace_receipt(
                    provider,
                    303,
                    lambda value, key=key, raw=raw: value["facts"]["rollback"]["value"].__setitem__(key, raw),
                )
                with self.assertRaises(subject.ContractError):
                    subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)

        provider = fixture()
        replace_receipt(
            provider,
            303,
            lambda value: value["facts"]["rollback"]["value"].__setitem__("direct_failure_step", "success"),
        )
        self.assert_reason("SERVICE_ROLLBACK_EVIDENCE_CONFLICT", provider)

    def test_output_string_and_artifact_limits_and_schema_enum_parity(self) -> None:
        receipt = subject._build_receipt(fixture(), BUILD_RUN, FRONTIER_RUN)
        raw = subject._receipt_bytes(receipt)
        self.assertLessEqual(len(raw), subject.MAX_RECEIPT_BYTES)

        def check(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertLessEqual(len(key.encode()), 128)
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
            elif isinstance(value, str):
                self.assertLessEqual(len(value.encode()), subject.MAX_OUTPUT_STRING_BYTES)

        check(receipt)
        with self.assertRaisesRegex(subject.ContractError, "^OUTPUT_RECEIPT_TOO_LARGE$"):
            subject._receipt_bytes({"items": ["a" * subject.MAX_OUTPUT_STRING_BYTES] * 600})

        outcome = self.schema["$defs"]["normalizedOutcome"]["properties"]
        self.assertEqual(set(outcome["preflight_state"]["enum"]), subject.PREFLIGHT_STATES)
        self.assertEqual(set(outcome["mutation_state"]["enum"]), subject.MUTATION_STATES)
        self.assertEqual(set(outcome["deployment_outcome"]["enum"]), subject.DEPLOYMENT_OUTCOMES)
        self.assertEqual(set(outcome["rollback_outcome"]["enum"]), subject.ROLLBACK_OUTCOMES)
        self.assertEqual({outcome["receipt_outcome"]["const"]}, subject.RECEIPT_OUTCOMES)
        self.assertEqual(set(outcome["failed_stage"]["enum"]), subject.FAILED_STAGE_CODES)
        self.assertEqual(set(outcome["terminal_reason_code"]["enum"]), subject.TERMINAL_REASON_CODES)

        def check_schema(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "string":
                    self.assertIn("maxLength", node)
                for item in node.values():
                    check_schema(item)
            elif isinstance(node, list):
                for item in node:
                    check_schema(item)

        check_schema(self.schema)

    def test_pr583_and_pr585_legacy_topology_is_unconfigured(self) -> None:
        provider = fixture()
        provider.json_values[(subject.TF_REPOSITORY, "/actions/runs/301/artifacts?per_page=100")] = {
            "total_count": 0,
            "artifacts": [],
        }
        self.assert_reason("UNCONFIGURED_CHILD_RECEIPTS", provider)

    def test_frontier_is_provider_discovered_when_optional_and_cli_is_decimal_only(self) -> None:
        receipt = subject._build_receipt(fixture(), BUILD_RUN, None)
        self.assertEqual(receipt["frontier_run_id"], FRONTIER_RUN)
        for bad in ("", "0", "01", "-1", "1.0", "run-1", "../1"):
            with self.subTest(bad=bad), self.assertRaises(subject.ContractError):
                subject._parse_run_id(bad, True)

    def test_cli_rejects_caller_supplied_authority_fields(self) -> None:
        parser = subject._parser()
        for flag in (
            "--source-sha", "--source-tree", "--artifact-id", "--manifest-sha",
            "--service-graph", "--relay-run-id", "--child-run-id", "--failed-stage",
            "--evidence-bundle", "--disposition",
        ):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                parser.parse_args(
                    ["--producer-build-run-id", "1", "--output", "out.json", flag, "x"]
                )

    def test_swapped_supply_archive_fails_provider_hash_and_lineage(self) -> None:
        provider = fixture()
        wrong = manifest()
        wrong["source_revision"] = "0" * 40
        provider.byte_values[(subject.APP_REPOSITORY, "/actions/artifacts/1000/zip")] = archive(
            "staging-supply-set.json", canonical(wrong)
        )
        self.assert_reason("SUPPLY_MANIFEST_INVALID", provider)

    def test_duplicate_supply_artifact_fails_closed(self) -> None:
        provider = fixture()
        key = (subject.APP_REPOSITORY, f"/actions/runs/{BUILD_RUN}/artifacts?per_page=100")
        provider.json_values[key]["artifacts"].append(copy.deepcopy(provider.json_values[key]["artifacts"][0]))
        provider.json_values[key]["total_count"] = 2
        self.assert_reason("PROVIDER_ARTIFACT_CARDINALITY", provider)

    def test_wrong_build_workflow_event_head_or_tree_fails(self) -> None:
        changes = (
            ("path", ".github/workflows/other.yml", "PROVIDER_RUN_MISMATCH"),
            ("event", "workflow_dispatch", "PROVIDER_RUN_MISMATCH"),
            ("head_sha", "0" * 40, "PROVIDER_FIXTURE_MISSING"),
        )
        for key_name, value, reason in changes:
            with self.subTest(key=key_name):
                provider = fixture()
                provider.json_values[(subject.APP_REPOSITORY, f"/actions/runs/{BUILD_RUN}")][key_name] = value
                self.assert_reason(reason, provider)

    def test_newer_main_or_active_child_blocks_current_frontier(self) -> None:
        provider = fixture()
        provider.json_values[(subject.APP_REPOSITORY, "/branches/main")]["commit"]["sha"] = "0" * 40
        self.assert_reason("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN", provider)

        provider = fixture()
        list_key = (
            subject.TF_REPOSITORY,
            "/actions/workflows/deploy-leaf-platform-staging.yml/runs?event=workflow_dispatch&per_page=100",
        )
        active = run(
            399,
            repository=subject.TF_REPOSITORY,
            workflow=subject.DEPLOY_WORKFLOW,
            event="workflow_dispatch",
            head_sha=TF_HEAD,
            created="2026-08-13T01:24:00Z",
        )
        active["status"] = "in_progress"
        active["conclusion"] = None
        provider.json_values[list_key]["workflow_runs"].append(active)
        provider.json_values[list_key]["total_count"] += 1
        self.assert_reason("ACTIVE_CHILD_RUN", provider)

    def test_relay_log_duplicate_missing_or_foreign_child_fails(self) -> None:
        for log in (
            b"Watching web deploy run 301.\n",
            b"Watching web deploy run 301.\nWatching web deploy run 399.\nWatching app deploy run 302.\n",
            b"Watching web deploy run 301.\nWatching app deploy run 301.\n",
        ):
            with self.subTest(log=log):
                provider = fixture()
                provider.byte_values[(subject.APP_REPOSITORY, f"/actions/runs/{RELAY_RUN}/logs")] = archive("logs.txt", log)
                self.assert_reason("RELAY_CHILD_CARDINALITY", provider)

    def test_service_receipt_extra_field_and_bad_checksum_fail(self) -> None:
        provider = fixture()
        replace_receipt(provider, 303, lambda value: value.__setitem__("extra", True))
        self.assert_reason("SERVICE_RECEIPT_INVALID", provider)
        provider = fixture()
        key = (subject.TF_REPOSITORY, "/actions/artifacts/1303/zip")
        with zipfile.ZipFile(io.BytesIO(provider.byte_values[key])) as value:
            receipt = json.loads(value.read(subject.ARTIFACT_FILE))
        receipt["receipt_sha256"] = "0" * 64
        provider.byte_values[key] = archive(subject.ARTIFACT_FILE, canonical(receipt))
        self.assert_reason("SERVICE_RECEIPT_CHECKSUM_INVALID", provider)

    def test_failed_step_must_match_jobs_api(self) -> None:
        provider = fixture()
        provider.json_values[(subject.TF_REPOSITORY, "/actions/runs/303/attempts/1/jobs?per_page=100")]["jobs"][0]["steps"][0]["conclusion"] = "failure"
        self.assert_reason("SERVICE_RECEIPT_FAILED_STAGE_MISMATCH", provider)

    def test_source_and_terminal_digest_mismatch_fail(self) -> None:
        provider = fixture()
        replace_receipt(
            provider,
            303,
            lambda value: value["facts"]["source"]["revision"].__setitem__("value", "0" * 40),
        )
        self.assert_reason("SERVICE_TERMINAL_CARDINALITY", provider)
        provider = fixture()
        replace_receipt(
            provider,
            303,
            lambda value: value["facts"]["terminal"]["value"]["image_digest"].__setitem__("value", "sha256:" + "0" * 64),
        )
        self.assert_reason("SERVICE_DIGEST_MISMATCH", provider)

    def test_child_supply_reference_is_preserved_and_cannot_rebind(self) -> None:
        provider = fixture()
        expected = {
            "artifact_id": 1000,
            "artifact_name": f"staging-supply-set-{SOURCE}-attempt-1",
            "manifest_sha256": hashlib.sha256(canonical(manifest())).hexdigest(),
            "producer_run_id": BUILD_RUN,
            "producer_run_attempt": 1,
        }
        replace_receipt(
            provider,
            303,
            lambda value: value["facts"].__setitem__("supply", produced(expected)),
        )
        result = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        broker = result["services"]["broker"]["attempts"][0]
        self.assertEqual(broker["supply_evidence"], produced(expected))

        provider = fixture()
        wrong = {**expected, "manifest_sha256": "0" * 64}
        replace_receipt(
            provider,
            303,
            lambda value: value["facts"].__setitem__("supply", produced(wrong)),
        )
        self.assert_reason("SERVICE_SUPPLY_EVIDENCE_MISMATCH", provider)

    def test_every_classified_child_requires_exact_source_tree_and_supply(self) -> None:
        for run_id in (301, 302, 303, 304, 305, 306):
            for field in ("revision", "tree", "supply"):
                with self.subTest(run_id=run_id, field=field):
                    provider = fixture()
                    def remove(value: dict, field: str = field) -> None:
                        if field == "supply":
                            value["facts"]["supply"] = missing()
                        else:
                            value["facts"]["source"][field] = missing()
                    replace_receipt(provider, run_id, remove)
                    reason = (
                        "SERVICE_SOURCE_TREE_MISMATCH"
                        if field == "tree"
                        else "SERVICE_SUPPLY_EVIDENCE_MISMATCH"
                        if field == "supply"
                        else "SERVICE_SOURCE_REVISION_MISMATCH"
                    )
                    self.assert_reason(reason, provider)

    def test_broken_app_predecessor_chain_fails(self) -> None:
        provider = fixture()
        replace_receipt(
            provider,
            FRONTIER_RUN,
            lambda value: value["facts"]["predecessor_task_definition"].__setitem__("value", "leaf-platform-app:999"),
        )
        self.assert_reason("SERVICE_PREDECESSOR_CHAIN_MISMATCH", provider)

    def test_duplicate_or_unclassified_matching_child_fails(self) -> None:
        provider = fixture()
        raw = run(
            307,
            repository=subject.TF_REPOSITORY,
            workflow=subject.DEPLOY_WORKFLOW,
            event="workflow_dispatch",
            head_sha=TF_HEAD,
            created="2026-08-13T01:24:30Z",
        )
        list_key = (
            subject.TF_REPOSITORY,
            "/actions/workflows/deploy-leaf-platform-staging.yml/runs?event=workflow_dispatch&per_page=100",
        )
        provider.json_values[list_key]["workflow_runs"].append(raw)
        provider.json_values[list_key]["total_count"] += 1
        name = "leaf-platform-staging-service-run-307-attempt-1"
        provider.json_values[(subject.TF_REPOSITORY, "/actions/runs/307/artifacts?per_page=100")] = {
            "total_count": 1,
            "artifacts": [artifact(1307, name, 307, TF_HEAD)],
        }
        provider.byte_values[(subject.TF_REPOSITORY, "/actions/artifacts/1307/zip")] = archive(
            subject.ARTIFACT_FILE,
            canonical(service_receipt("broker", 307, predecessor="leaf-platform-broker:2", terminal_td="leaf-platform-broker:3")),
        )
        provider.json_values[(subject.TF_REPOSITORY, "/actions/runs/307/attempts/1/jobs?per_page=100")] = {
            "total_count": 1,
            "jobs": [{"name": "Deploy", "steps": [{"name": "Done", "number": 1, "conclusion": "success"}]}],
        }
        self.assert_reason("SERVICE_TERMINAL_CARDINALITY", provider)

    def test_identity_missing_or_cross_release_fails(self) -> None:
        provider = fixture()
        replace_receipt(
            provider,
            FRONTIER_RUN,
            lambda value: value["facts"].__setitem__("deployment_identity", missing()),
        )
        self.assert_reason("DEPLOYMENT_IDENTITY_NOT_PRODUCED", provider)
        provider = fixture()
        def alter(value: dict) -> None:
            body = value["facts"]["deployment_identity"]["value"]["body"]
            body["services"]["web"]["image_digest"] = "sha256:" + "0" * 64
            raw = (json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode()
            value["facts"]["deployment_identity"]["value"]["sha256"] = hashlib.sha256(raw).hexdigest()
        replace_receipt(provider, FRONTIER_RUN, alter)
        self.assert_reason("DEPLOYMENT_IDENTITY_MISMATCH", provider)

    def test_selected_terminal_requires_one_matching_primary_completed_deployment(self) -> None:
        for primary in (
            missing(),
            [],
            [
                {
                    "task_definition": "leaf-platform-broker:999",
                    "rollout_state": "COMPLETED",
                    "status": "PRIMARY",
                }
            ],
            [
                {
                    "task_definition": "leaf-platform-broker:2",
                    "rollout_state": "IN_PROGRESS",
                    "status": "PRIMARY",
                }
            ],
        ):
            with self.subTest(primary=primary):
                provider = fixture()
                replace_receipt(
                    provider,
                    303,
                    lambda value, primary=primary: value["facts"]["terminal"]["value"].__setitem__(
                        "primary_deployments", primary
                    ),
                )
                self.assert_reason("SERVICE_TERMINAL_UNHEALTHY", provider)

    def test_secret_shaped_receipt_value_never_reaches_output(self) -> None:
        provider = fixture()
        replace_receipt(
            provider,
            303,
            lambda value: value["facts"]["rollback"]["value"].__setitem__("bluegreen_detail", "Bearer should-not-land"),
        )
        self.assert_reason("SECRET_SHAPED_EVIDENCE", provider)

    def test_provider_error_is_sanitized(self) -> None:
        provider = fixture()
        del provider.json_values[(subject.APP_REPOSITORY, f"/actions/runs/{BUILD_RUN}")]
        self.assert_reason("PROVIDER_FIXTURE_MISSING", provider)


class WorkflowAndSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow_path = ROOT / ".github/workflows/finalize-platform-staging-convergence.yml"
        cls.text = cls.workflow_path.read_text(encoding="utf-8")
        cls.doc = yaml.safe_load(cls.text)
        cls.schema = json.loads(
            (ROOT / "contract/platform-staging-convergence.v1.schema.json").read_text(encoding="utf-8")
        )

    def test_workflow_is_manual_read_only_and_accepts_only_run_ids(self) -> None:
        trigger = self.doc.get("on", self.doc.get(True))
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        self.assertEqual(
            set(trigger["workflow_dispatch"]["inputs"]),
            {"producer_build_run_id", "current_frontier_run_id"},
        )
        self.assertEqual(self.doc["permissions"], {"actions": "read", "contents": "read"})
        self.assertNotIn("environment", self.doc["jobs"]["finalize"])
        self.assertNotIn("id-token", self.text)
        self.assertNotIn("aws ", self.text.lower())
        self.assertNotIn("gh workflow run", self.text)
        self.assertNotIn("actions/workflows/dispatches", self.text)
        self.assertNotIn("cancel-run", self.text)

    def test_upload_runs_only_after_complete_validation(self) -> None:
        steps = self.doc["jobs"]["finalize"]["steps"]
        upload = next(step for step in steps if step.get("name") == "Upload the complete convergence receipt")
        self.assertEqual(upload["if"], "steps.finalize.outcome == 'success'")
        self.assertEqual(upload["uses"], "actions/upload-artifact@v4")
        self.assertEqual(upload["with"]["retention-days"], 30)
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertIn("leaf-platform-staging-convergence-build-", upload["with"]["name"])
        self.assertFalse(self.doc["concurrency"]["cancel-in-progress"])

    def test_cross_repo_credential_is_read_only_and_never_printed(self) -> None:
        finalize = next(
            step
            for step in self.doc["jobs"]["finalize"]["steps"]
            if step.get("id") == "finalize"
        )
        self.assertEqual(finalize["env"]["APP_GITHUB_TOKEN"], "${{ github.token }}")
        self.assertEqual(
            finalize["env"]["TERRAFORM_GITHUB_TOKEN"],
            "${{ secrets.TERRAFORM_REPO_TOKEN }}",
        )
        self.assertNotIn("echo $APP_GITHUB_TOKEN", finalize["run"])
        self.assertNotIn("echo $TERRAFORM_GITHUB_TOKEN", finalize["run"])

    def test_workflow_never_transports_raw_provider_or_job_text_to_receipt(self) -> None:
        lowered = self.text.lower()
        for forbidden in (
            "github_step_summary",
            "response.body",
            "exception.body",
            "traceback",
            "preflight_result",
            "deploy_result",
            "terminal_result",
            "prior_job_status",
            "canonical_json",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn("/logs", self.text)
        self.assertIn("provider receipt; body withheld", lowered)

    def test_schema_is_draft_2020_12_and_every_object_is_closed(self) -> None:
        self.assertEqual(self.schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        jsonschema.Draft202012Validator.check_schema(self.schema)

        def walk(value: object) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object" or "properties" in value:
                    self.assertIs(value.get("additionalProperties"), False, value)
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(self.schema)


if __name__ == "__main__":
    unittest.main()
