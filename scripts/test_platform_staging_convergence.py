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

# The subject imports `scripts.platform_release_manifest`, so the repo root has
# to be importable. Adding it HERE rather than through the cwd or PYTHONPATH is
# deliberate: the repo root carries a `platform/` package that shadows the
# stdlib module, and pytest loads its plugins (pytest-cov imports `platform`)
# before this file runs. Widening the path only now, after the stdlib preload
# above has pinned the real module, lets this suite run under pytest from the
# scripts directory the way every other scripts suite does.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import platform_staging_convergence as subject  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "a" * 40
APP_TREE = "b" * 40
BUILD_BLOB = "c" * 40
RELAY_BLOB = "d" * 40
TF_HEAD = "e" * 40
TF_TREE = "f" * 40
TF_BLOB = "1" * 40
CONTRACT_WORKFLOW_BLOB = "7" * 40
CONTRACT_SCHEMA_BLOB = "8" * 40
CONTRACT_RUN = 250
CONTRACT_ARTIFACT = 1250
BUILD_RUN = 100
RELAY_RUN = 200
FRONTIER_RUN = 306
BUILD_TAG = "prod-aaaaaaa"
SERVICES = ("app", "broker", "canonical-worker", "harness", "web")
DIGESTS = {
    service: f"sha256:{digit * 64}"
    for service, digit in zip(SERVICES, "23456", strict=True)
}
CHILD_WINDOW_EXACT_ENDPOINT = (
    "/actions/workflows/deploy-leaf-platform-staging.yml/runs?"
    "event=workflow_dispatch&created=2026-08-13T01%3A10%3A00Z.."
    "2026-08-13T01%3A25%3A00Z&per_page=100"
)
CHILD_WINDOW_OPEN_ENDPOINT = (
    "/actions/workflows/deploy-leaf-platform-staging.yml/runs?"
    "event=workflow_dispatch&created=%3E%3D2026-08-13T01%3A10%3A00Z&per_page=100"
)


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


class GitHubProviderRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = subject.GitHubProvider("app-token", "terraform-token")
        self.response = mock.MagicMock()
        self.response.__enter__.return_value.read.return_value = b"{}"

    @staticmethod
    def http_error(code: int, retry_after: str | None = None) -> subject.urllib.error.HTTPError:
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        return subject.urllib.error.HTTPError("https://provider.invalid", code, "provider failure", headers, None)

    def request_with(self, *results: object) -> tuple[bytes, mock.Mock, mock.Mock]:
        opener = mock.Mock()
        opener.open.side_effect = list(results)
        with (
            mock.patch.object(subject.urllib.request, "build_opener", return_value=opener),
            mock.patch.object(subject.time, "sleep") as sleep,
        ):
            value = self.provider.bytes(subject.APP_REPOSITORY, "/actions/runs/1")
        return value, opener, sleep

    def test_transient_network_failure_retries_with_bounded_backoff(self) -> None:
        value, opener, sleep = self.request_with(subject.urllib.error.URLError("temporary"), self.response)

        self.assertEqual(value, b"{}")
        self.assertEqual(opener.open.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_transient_failure_exhaustion_is_sanitized_and_fail_closed(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = [subject.urllib.error.URLError("temporary")] * subject.MAX_PROVIDER_READ_ATTEMPTS
        with (
            mock.patch.object(subject.urllib.request, "build_opener", return_value=opener),
            mock.patch.object(subject.time, "sleep") as sleep,
            self.assertRaisesRegex(subject.ContractError, "^PROVIDER_READ_FAILED$"),
        ):
            self.provider.bytes(subject.APP_REPOSITORY, "/actions/runs/1")

        self.assertEqual(opener.open.call_count, subject.MAX_PROVIDER_READ_ATTEMPTS)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_deterministic_http_error_does_not_retry(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = self.http_error(404)
        with (
            mock.patch.object(subject.urllib.request, "build_opener", return_value=opener),
            mock.patch.object(subject.time, "sleep") as sleep,
            self.assertRaisesRegex(subject.ContractError, "^PROVIDER_READ_FAILED$"),
        ):
            self.provider.bytes(subject.APP_REPOSITORY, "/actions/runs/1")

        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_retry_after_controls_throttle_delay_with_a_finite_cap(self) -> None:
        for code, header, expected in ((429, "7", 7), (403, "999", subject.MAX_PROVIDER_RETRY_DELAY_SECONDS)):
            with self.subTest(code=code, header=header):
                value, opener, sleep = self.request_with(self.http_error(code, header), self.response)
                self.assertEqual(value, b"{}")
                self.assertEqual(opener.open.call_count, 2)
                sleep.assert_called_once_with(expected)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _fixed_member(filename: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def archive(filename: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as value:
        value.writestr(_fixed_member(filename), payload)
    return buffer.getvalue()


def archive_members(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as value:
        for filename, payload in entries.items():
            value.writestr(_fixed_member(filename), payload)
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


def v3_manifest() -> dict:
    entries = {}
    for index, service in enumerate(SERVICES, 1):
        fingerprint = format(index, "064x")
        entry = {
            "repository": f"leaf-platform-{service}",
            "image_digest": DIGESTS[service],
            "immutable_lookup_tag": f"surface-v1-{fingerprint}",
            "producer_source_revision": SOURCE,
            "producer_source_tree": APP_TREE,
            "surface_fingerprint": fingerprint,
            "recipe_fingerprint": format(index + 10, "064x"),
            "producer_workflow_path": subject.BUILD_WORKFLOW,
            "producer_workflow_blob": BUILD_BLOB,
            "producer_run_id": 31415926535,
            "producer_run_attempt": 1,
            "provenance_subject": (
                "807034087062.dkr.ecr.us-east-1.amazonaws.com/"
                f"leaf-platform-{service}"
            ),
            "provenance_digest": "sha256:" + format(index + 20, "064x"),
            "build_disposition": "reused",
        }
        if service == "canonical-worker":
            entry["solver_provenance"] = {
                "solver_source_revision": "7" * 40,
                "solver_source_sha256": "8" * 64,
            }
        if service == "web":
            entry["artifact_sha256"] = "9" * 64
        entries[service] = entry
    return {
        "schema": "leaf.staging-supply-set.v3",
        "release_source_revision": SOURCE,
        "release_source_tree": APP_TREE,
        "build_run_id": 31415926535,
        "build_run_attempt": 1,
        "services": entries,
    }


def missing() -> dict:
    return {"status": "not_produced"}


def produced(value: object) -> dict:
    return {"status": "produced", "value": value}


def consumer_contract_evidence(
    *, head_sha: str = TF_HEAD, tree_sha: str = TF_TREE,
    run_id: int = CONTRACT_RUN, artifact_id: int = CONTRACT_ARTIFACT,
) -> tuple[dict, bytes, dict]:
    name = f"leaf-platform-staging-consumer-contract-run-{run_id}-attempt-1"
    unsigned = {
        "artifact": {"file": "consumer-contract.json", "name": name},
        "consumer": {
            "contract_schema_path": subject.CONSUMER_CONTRACT_SCHEMA_PATH,
            "contract_schema_blob": CONTRACT_SCHEMA_BLOB,
            "contract_version": 1,
            "deploy_workflow_path": subject.DEPLOY_WORKFLOW,
            "deploy_workflow_blob": TF_BLOB,
            "pins": {
                "deployment_environment": "aws-apply",
                "digest_aware_marker": "leaf.staging-digest-aware-consumer.v1",
                "mutation_group": "leaf-platform-staging-ecs-mutation",
            },
        },
        "producer": {
            "repository": subject.TF_REPOSITORY,
            "workflow_path": subject.CONSUMER_CONTRACT_WORKFLOW,
            "workflow_blob": CONTRACT_WORKFLOW_BLOB,
            "run_id": run_id,
            "run_attempt": 1,
            "event": "push",
            "branch": "main",
            "head_sha": head_sha,
            "head_tree": tree_sha,
        },
        "schema": subject.CONSUMER_CONTRACT_SCHEMA,
        "version": 1,
    }
    contract = copy.deepcopy(unsigned)
    contract["payload_sha256"] = hashlib.sha256(canonical(unsigned)).hexdigest()
    raw_contract = canonical(contract) + b"\n"
    zip_raw = archive("consumer-contract.json", raw_contract)
    archive_sha = hashlib.sha256(zip_raw).hexdigest()
    dispatch = {
        "artifact": {
            "id": artifact_id,
            "name": name,
            "producer_run_id": run_id,
            "producer_run_attempt": 1,
            "provider_sha256": archive_sha,
            "archive_sha256": archive_sha,
            "file_sha256": hashlib.sha256(raw_contract).hexdigest(),
        },
        "contract": contract,
        "schema": subject.CONSUMER_DISPATCH_SCHEMA,
    }
    dispatch["envelope_sha256"] = hashlib.sha256(canonical(dispatch)).hexdigest()
    import base64

    encoded = base64.urlsafe_b64encode(canonical(dispatch)).rstrip(b"=")
    slot = {
        "status": "produced",
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "utf8_bytes": len(encoded),
    }
    return contract, zip_raw, slot


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
    _, _, contract_slot = consumer_contract_evidence()
    requested = {
        "allow_non_forward_image": missing(),
        "app_deploy_intent": "configuration" if with_identity else "forward",
        "configuration_delta": missing(),
        "configuration_task_definition": predecessor if with_identity else "not_produced",
        "convergence_id": "not_produced",
        "deploy_mode": "normal",
        "consumer_contract": contract_slot,
        "deploy_mode": "normal",
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
        "supply_evidence": missing(),
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
        self.json_sequences: dict[tuple[str, str], list[object]] = {}
        self.byte_values: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, str, str]] = []

    def json(self, repository: str, endpoint: str) -> object:
        self.calls.append(("GET_JSON", repository, endpoint))
        key = (repository, endpoint)
        if key in self.json_sequences:
            values = self.json_sequences[key]
            if not values:
                raise subject.ContractError("PROVIDER_FIXTURE_MISSING")
            return copy.deepcopy(values.pop(0))
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
    contract, contract_zip, _ = consumer_contract_evidence()
    contract_run = run(
        CONTRACT_RUN,
        repository=tf,
        workflow=subject.CONSUMER_CONTRACT_WORKFLOW,
        event="push",
        head_sha=TF_HEAD,
        created="2026-08-13T00:55:00Z",
    )
    contract_query = "branch=main&event=push&status=success&per_page=100"
    provider.json_values[(tf, f"/actions/workflows/publish-leaf-platform-staging-consumer-contract.yml/runs?{contract_query}")] = {
        "total_count": 1,
        "workflow_runs": [contract_run],
    }
    contract_name = contract["artifact"]["name"]
    contract_row = artifact(
        CONTRACT_ARTIFACT, contract_name, CONTRACT_RUN, TF_HEAD
    )
    contract_row["digest"] = "sha256:" + hashlib.sha256(contract_zip).hexdigest()
    contract_row["workflow_run"]["head_branch"] = "main"
    provider.json_values[(tf, f"/actions/runs/{CONTRACT_RUN}/artifacts?per_page=100")] = {
        "total_count": 1,
        "artifacts": [contract_row],
    }
    provider.byte_values[(tf, f"/actions/artifacts/{CONTRACT_ARTIFACT}/zip")] = contract_zip
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
    child_list = {
        "total_count": len(child_runs),
        "workflow_runs": child_runs,
    }
    provider.json_values[(tf, CHILD_WINDOW_EXACT_ENDPOINT)] = child_list
    provider.json_values[(tf, CHILD_WINDOW_OPEN_ENDPOINT)] = copy.deepcopy(child_list)
    return provider


_ABSENT = object()


def add_child_row(provider: FakeProvider, row: dict, *, open_window_only: bool) -> None:
    """Append one run row to the child listing(s).

    A caller-supplied frontier bounds the API window server side, so a run
    created after it only ever appears in the OPEN listing. open_window_only
    reproduces that asymmetry, which is what production looks like and what the
    single-release fixture otherwise cannot show.
    """
    endpoints = [CHILD_WINDOW_OPEN_ENDPOINT]
    if not open_window_only:
        endpoints.append(CHILD_WINDOW_EXACT_ENDPOINT)
    for endpoint in endpoints:
        listing = provider.json_values[(subject.TF_REPOSITORY, endpoint)]
        listing["workflow_runs"].append(copy.deepcopy(row))
        listing["total_count"] += 1


def active_child_row(created: str, run_id: int = 399) -> dict:
    row = run(
        run_id,
        repository=subject.TF_REPOSITORY,
        workflow=subject.DEPLOY_WORKFLOW,
        event="workflow_dispatch",
        head_sha=TF_HEAD,
        created=created,
    )
    row["status"] = "in_progress"
    row["conclusion"] = None
    return row


def add_identity_child(provider: FakeProvider, run_id: int, created: str) -> None:
    """A second completed app run carrying a produced deployment identity."""
    tf = subject.TF_REPOSITORY
    row = run(
        run_id,
        repository=tf,
        workflow=subject.DEPLOY_WORKFLOW,
        event="workflow_dispatch",
        head_sha=TF_HEAD,
        created=created,
    )
    provider.json_values[(tf, f"/actions/runs/{run_id}")] = copy.deepcopy(row)
    name = f"leaf-platform-staging-service-run-{run_id}-attempt-1"
    provider.json_values[(tf, f"/actions/runs/{run_id}/artifacts?per_page=100")] = {
        "total_count": 1,
        "artifacts": [artifact(run_id + 1000, name, run_id, TF_HEAD)],
    }
    provider.byte_values[(tf, f"/actions/artifacts/{run_id + 1000}/zip")] = archive(
        subject.ARTIFACT_FILE,
        canonical(
            service_receipt(
                "app",
                run_id,
                predecessor="leaf-platform-app:2",
                terminal_td="leaf-platform-app:3",
                with_identity=True,
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
    add_child_row(provider, row, open_window_only=True)


def replace_receipt(provider: FakeProvider, run_id: int, mutate) -> None:
    key = (subject.TF_REPOSITORY, f"/actions/artifacts/{run_id + 1000}/zip")
    with zipfile.ZipFile(io.BytesIO(provider.byte_values[key])) as value:
        receipt = json.loads(value.read(subject.ARTIFACT_FILE))
    mutate(receipt)
    receipt["receipt_sha256"] = ""
    receipt["receipt_sha256"] = hashlib.sha256(canonical(receipt)).hexdigest()
    provider.byte_values[key] = archive(subject.ARTIFACT_FILE, canonical(receipt))


def replace_consumer_contract(provider: FakeProvider, mutate) -> None:
    key = (
        subject.TF_REPOSITORY,
        f"/actions/artifacts/{CONTRACT_ARTIFACT}/zip",
    )
    with zipfile.ZipFile(io.BytesIO(provider.byte_values[key])) as value:
        contract = json.loads(value.read(subject.CONSUMER_CONTRACT_FILE))
    mutate(contract)
    contract.pop("payload_sha256", None)
    contract["payload_sha256"] = hashlib.sha256(canonical(contract)).hexdigest()
    zip_raw = archive(subject.CONSUMER_CONTRACT_FILE, canonical(contract) + b"\n")
    provider.byte_values[key] = zip_raw
    rows = provider.json_values[
        (
            subject.TF_REPOSITORY,
            f"/actions/runs/{CONTRACT_RUN}/artifacts?per_page=100",
        )
    ]["artifacts"]
    rows[0]["digest"] = "sha256:" + hashlib.sha256(zip_raw).hexdigest()


def intermediate_app_identity_fixture() -> FakeProvider:
    """Real readiness-config topology: relay app, identity, then new frontier."""
    provider = fixture()
    tf = subject.TF_REPOSITORY
    frontier_id = FRONTIER_RUN + 1
    frontier = run(
        frontier_id, repository=tf, workflow=subject.DEPLOY_WORKFLOW,
        event="workflow_dispatch", head_sha=TF_HEAD,
        created="2026-08-13T01:26:00Z",
    )
    provider.json_values[(tf, f"/actions/runs/{frontier_id}")] = frontier
    name = f"leaf-platform-staging-service-run-{frontier_id}-attempt-1"
    provider.json_values[(tf, f"/actions/runs/{frontier_id}/artifacts?per_page=100")] = {
        "total_count": 1,
        "artifacts": [artifact(frontier_id + 1000, name, frontier_id, TF_HEAD)],
    }
    receipt = service_receipt(
        "app", frontier_id, predecessor="leaf-platform-app:3",
        terminal_td="leaf-platform-app:4", with_identity=True,
    )
    provider.byte_values[(tf, f"/actions/artifacts/{frontier_id + 1000}/zip")] = archive(
        subject.ARTIFACT_FILE, canonical(receipt),
    )
    provider.json_values[(tf, f"/actions/runs/{frontier_id}/attempts/1/jobs?per_page=100")] = copy.deepcopy(
        provider.json_values[(tf, f"/actions/runs/{FRONTIER_RUN}/attempts/1/jobs?per_page=100")]
    )
    children = copy.deepcopy(provider.json_values[(tf, CHILD_WINDOW_EXACT_ENDPOINT)])
    children["workflow_runs"].append(frontier)
    children["total_count"] += 1
    endpoint = CHILD_WINDOW_EXACT_ENDPOINT.replace("01%3A25%3A00Z", "01%3A26%3A00Z")
    provider.json_values[(tf, endpoint)] = children
    provider.json_values[(tf, CHILD_WINDOW_OPEN_ENDPOINT)] = copy.deepcopy(children)
    return provider


def retained_consumer_fixture() -> FakeProvider:
    """Live 5daa topology: four later children retain the earlier contract."""
    provider = fixture()
    tf = subject.TF_REPOSITORY
    head, tree = "9" * 40, "2" * 40
    contract, zip_raw, _ = consumer_contract_evidence(
        head_sha=head, tree_sha=tree,
        run_id=CONTRACT_RUN + 1, artifact_id=CONTRACT_ARTIFACT + 1,
    )
    current_run = run(
        CONTRACT_RUN + 1, repository=tf,
        workflow=subject.CONSUMER_CONTRACT_WORKFLOW, event="push",
        head_sha=head, created="2026-08-13T01:15:00Z",
    )
    query = (
        "/actions/workflows/publish-leaf-platform-staging-consumer-contract.yml/"
        "runs?branch=main&event=push&status=success&per_page=100"
    )
    listing = provider.json_values[(tf, query)]
    listing["workflow_runs"].insert(0, current_run)
    listing["total_count"] += 1
    row = artifact(CONTRACT_ARTIFACT + 1, contract["artifact"]["name"], CONTRACT_RUN + 1, head)
    row["digest"] = "sha256:" + hashlib.sha256(zip_raw).hexdigest()
    row["workflow_run"]["head_branch"] = "main"
    provider.json_values[(tf, f"/actions/runs/{CONTRACT_RUN + 1}/artifacts?per_page=100")] = {
        "total_count": 1, "artifacts": [row],
    }
    provider.byte_values[(tf, f"/actions/artifacts/{CONTRACT_ARTIFACT + 1}/zip")] = zip_raw
    for run_id in (303, 304, 305, 306):
        provider.json_values[(tf, f"/actions/runs/{run_id}")]["head_sha"] = head
        rows = provider.json_values[(tf, f"/actions/runs/{run_id}/artifacts?per_page=100")]["artifacts"]
        rows[0]["workflow_run"]["head_sha"] = head
        replace_receipt(provider, run_id, lambda value: value["provider"].__setitem__("head_sha", head))
    for endpoint in (CHILD_WINDOW_EXACT_ENDPOINT, CHILD_WINDOW_OPEN_ENDPOINT):
        for child in provider.json_values[(tf, endpoint)]["workflow_runs"]:
            if child["id"] in (303, 304, 305, 306):
                child["head_sha"] = head
    provider.json_values[(tf, f"/compare/{TF_HEAD}...{head}")] = {
        "status": "ahead", "ahead_by": 6, "behind_by": 0,
        "base_commit": {"sha": TF_HEAD}, "merge_base_commit": {"sha": TF_HEAD},
    }
    provider.json_values[(tf, f"/git/commits/{head}")] = {"tree": {"sha": tree}}
    provider.json_values[(tf, f"/git/trees/{tree}?recursive=1")] = {
        "truncated": False,
        "tree": [
            {"path": path, "type": "blob", "sha": blob}
            for path, blob in (
                (subject.DEPLOY_WORKFLOW, TF_BLOB),
                (subject.CONSUMER_CONTRACT_WORKFLOW, CONTRACT_WORKFLOW_BLOB),
                (subject.CONSUMER_CONTRACT_SCHEMA_PATH, CONTRACT_SCHEMA_BLOB),
            )
        ],
    }
    return provider


def failed_relay_fixture() -> FakeProvider:
    provider = fixture()
    app = subject.APP_REPOSITORY
    tf = subject.TF_REPOSITORY
    relay_query = "event=workflow_run&status=completed&head_sha=" + SOURCE + "&per_page=100"
    relay_list = provider.json_values[
        (app, f"/actions/workflows/dispatch-staging-deploys.yml/runs?{relay_query}")
    ]
    relay_list["workflow_runs"][0]["conclusion"] = "failure"
    provider.json_values[(app, f"/actions/runs/{RELAY_RUN}/artifacts?per_page=100")] = {
        "total_count": 0,
        "artifacts": [],
    }
    provider.byte_values[(app, f"/actions/runs/{RELAY_RUN}/logs")] = archive(
        "0_dispatch.txt",
        b"Watching web deploy run 301.\n",
    )
    provider.json_values[(app, f"/actions/runs/{RELAY_RUN}/attempts/1/jobs?per_page=100")] = {
        "total_count": 1,
        "jobs": [
            {
                "name": "dispatch",
                "steps": [
                    {
                        "name": "Deploy each staging service in turn and prove each one landed",
                        "number": 5,
                        "conclusion": "failure",
                    },
                    {
                        "name": "Publish the convergence receipt",
                        "number": 6,
                        "conclusion": "skipped",
                    },
                ],
            }
        ],
    }

    runs_key = (
        tf,
        CHILD_WINDOW_EXACT_ENDPOINT,
    )
    run_list = provider.json_values[runs_key]
    run_list["workflow_runs"] = [row for row in run_list["workflow_runs"] if row["id"] != 302]
    run_list["workflow_runs"][0]["conclusion"] = "failure"

    def fail_web(value: dict) -> None:
        value["deploy_result"] = "failure"
        value["terminal_result"] = "failure"
        value["failed_stage"] = produced(
            {
                "primary": {"job": "Deploy", "step": "Resolve inputs", "number": 1},
                "additional": [],
                "unique": True,
            }
        )
        value["facts"]["candidate"] = {
            "task_definition": missing(),
            "image_digest": missing(),
        }
        value["facts"]["terminal"] = missing()
        value["facts"]["mutation_count"] = produced(0)
        value["facts"]["prior_job_status"] = produced("failure")

    replace_receipt(provider, 301, fail_web)
    provider.json_values[(tf, "/actions/runs/301/attempts/1/jobs?per_page=100")] = {
        "total_count": 1,
        "jobs": [
            {
                "name": "Deploy",
                "steps": [{"name": "Resolve inputs", "number": 1, "conclusion": "failure"}],
            }
        ],
    }

    resumed_web = run(
        307,
        repository=tf,
        workflow=subject.DEPLOY_WORKFLOW,
        event="workflow_dispatch",
        head_sha=TF_HEAD,
        created="2026-08-13T01:20:30Z",
    )
    run_list["workflow_runs"].append(resumed_web)
    run_list["total_count"] = len(run_list["workflow_runs"])
    provider.json_values[(tf, "/actions/runs/307/artifacts?per_page=100")] = {
        "total_count": 1,
        "artifacts": [artifact(1307, "leaf-platform-staging-service-run-307-attempt-1", 307, TF_HEAD)],
    }
    provider.byte_values[(tf, "/actions/artifacts/1307/zip")] = archive(
        subject.ARTIFACT_FILE,
        canonical(
            service_receipt(
                "web",
                307,
                predecessor="leaf-platform-web:1",
                terminal_td="leaf-platform-web:2",
            )
        ),
    )
    provider.json_values[(tf, "/actions/runs/307/attempts/1/jobs?per_page=100")] = {
        "total_count": 1,
        "jobs": [
            {
                "name": "Deploy",
                "steps": [{"name": "Done", "number": 1, "conclusion": "success"}],
            }
        ],
    }
    return provider


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

    @staticmethod
    def run_rows(run_ids: list[int]) -> list[dict[str, object]]:
        return [
            run(
                run_id,
                repository=subject.TF_REPOSITORY,
                workflow=subject.DEPLOY_WORKFLOW,
                event="workflow_dispatch",
                head_sha=TF_HEAD,
                created="2026-08-14T01:00:00Z",
            )
            for run_id in run_ids
        ]

    @staticmethod
    def comparison(
        source: str,
        files: list[dict[str, str]],
        *,
        current: str = "39c17213f2573542c2b58743ace12ed5b682f2dc",
        commits: list[str] | None = None,
    ) -> dict[str, object]:
        commit_shas = commits or ["1" * 40, current]
        return {
            "status": "ahead",
            "ahead_by": len(commit_shas),
            "behind_by": 0,
            "total_commits": len(commit_shas),
            "base_commit": {"sha": source},
            "merge_base_commit": {"sha": source},
            "commits": [{"sha": sha} for sha in commit_shas],
            "files": files,
        }

    @staticmethod
    def tree_sha(commit_sha: str) -> str:
        return hashlib.sha1(("tree:" + commit_sha).encode("utf-8")).hexdigest()

    @staticmethod
    def surface_tree(
        *,
        changed: dict[str, str] | None = None,
        added: dict[str, str] | None = None,
        removed: tuple[str, ...] = (),
    ) -> dict[str, object]:
        """A tree carrying one blob per declared release input, plus mutations.

        Blob shas are derived from the path, so two unmutated trees compare
        equal and any mutation is the only thing that can move a surface.
        """

        paths: list[str] = [subject.BUILD_WORKFLOW, subject.RELAY_WORKFLOW, "docs/readme.md"]
        for sources in subject.SURFACE_INPUTS.values():
            paths.extend(sources)
        # exercise the prefix branch of _surface_blob_map, not only exact hits
        paths.extend(["web/app.jsx", "server/app.py", "harness/tool.mjs"])
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for path in paths:
            if path in seen or path in removed:
                continue
            seen.add(path)
            sha = hashlib.sha1(path.encode("utf-8")).hexdigest()
            rows.append({"path": path, "type": "blob", "mode": "100644", "sha": sha})
        for path, sha in (added or {}).items():
            rows.append({"path": path, "type": "blob", "mode": "100644", "sha": sha})
        for row in rows:
            if row["path"] in (changed or {}):
                row["sha"] = changed[row["path"]]
        return {"truncated": False, "tree": rows}

    def drift_provider(
        self,
        source: str,
        current: str,
        *,
        changed: dict[str, str] | None = None,
        added: dict[str, str] | None = None,
        removed: tuple[str, ...] = (),
    ) -> FakeProvider:
        app = subject.APP_REPOSITORY
        tree_of = self.tree_sha
        provider = FakeProvider()
        provider.json_values[(app, "/branches/main")] = {"commit": {"sha": current}}
        provider.json_values[(app, f"/compare/{source}...{current}")] = self.comparison(
            source, [], current=current
        )
        provider.json_values[(app, f"/git/commits/{source}")] = {"tree": {"sha": tree_of(source)}}
        provider.json_values[(app, f"/git/commits/{current}")] = {"tree": {"sha": tree_of(current)}}
        provider.json_values[(app, f"/git/trees/{tree_of(source)}?recursive=1")] = self.surface_tree()
        provider.json_values[(app, f"/git/trees/{tree_of(current)}?recursive=1")] = self.surface_tree(
            changed=changed, added=added, removed=removed
        )
        return provider

    def test_exact_main_records_no_drift_without_reading_a_tree(self) -> None:
        provider = FakeProvider()
        source = "7368ac85ef809957fa65fe237f3c72829580b4ee"
        provider.json_values[(subject.APP_REPOSITORY, "/branches/main")] = {"commit": {"sha": source}}

        arrival = subject._arrival_source(provider, source, require_surface_parity=False)

        self.assertEqual(arrival["relationship"], "exact")
        self.assertEqual(arrival["ahead_by"], 0)
        self.assertEqual(arrival["drifted_surfaces"], [])
        self.assertEqual(set(arrival["surfaces"].values()), {"identical"})
        # Same commit is the same tree: paying for two recursive tree reads to
        # rediscover that would be pure waste on the common path.
        self.assertEqual([call for call in provider.calls if "/git/trees/" in call[2]], [])

    def test_product_drift_on_main_is_recorded_not_refused(self) -> None:
        """The case that made finalize unrunnable: main moved by real product code."""

        source = "7368ac85ef809957fa65fe237f3c72829580b4ee"
        current = "0f9c47fe89f12b94a4d9d16c61d6c6df119356bc"
        provider = self.drift_provider(
            source, current, changed={"web/app.jsx": "9" * 40, "server/app.py": "9" * 40}
        )

        arrival = subject._arrival_source(provider, source, require_surface_parity=False)

        self.assertEqual(arrival["relationship"], "ancestor")
        self.assertEqual(arrival["current_main_sha"], current)
        self.assertEqual(arrival["ahead_by"], 2)
        # web owns web/, and server/ is a declared input of app, broker and
        # canonical-worker; harness declares neither, so it must stay clean.
        self.assertEqual(
            arrival["drifted_surfaces"], ["app", "broker", "canonical-worker", "web"]
        )
        self.assertEqual(arrival["surfaces"]["harness"], "identical")

    def test_non_surface_drift_on_main_records_every_surface_identical(self) -> None:
        source = "7368ac85ef809957fa65fe237f3c72829580b4ee"
        current = "0f9c47fe89f12b94a4d9d16c61d6c6df119356bc"
        provider = self.drift_provider(source, current, changed={"docs/readme.md": "9" * 40})

        arrival = subject._arrival_source(provider, source, require_surface_parity=False)

        self.assertEqual(arrival["ahead_by"], 2)
        self.assertEqual(arrival["drifted_surfaces"], [])
        self.assertEqual(set(arrival["surfaces"].values()), {"identical"})

    def test_an_addition_into_a_release_surface_is_caught_by_blob_comparison(self) -> None:
        """A changed-filename allowlist can miss this; comparing surfaces cannot."""

        source = "7368ac85ef809957fa65fe237f3c72829580b4ee"
        current = "0f9c47fe89f12b94a4d9d16c61d6c6df119356bc"
        provider = self.drift_provider(source, current, added={"harness/late-addition.mjs": "9" * 40})

        arrival = subject._arrival_source(provider, source, require_surface_parity=False)

        self.assertEqual(arrival["surfaces"]["harness"], "changed")
        self.assertIn("harness", arrival["drifted_surfaces"])

    def test_surface_parity_opt_in_still_refuses_any_drifted_surface(self) -> None:
        source = "7368ac85ef809957fa65fe237f3c72829580b4ee"
        current = "0f9c47fe89f12b94a4d9d16c61d6c6df119356bc"
        provider = self.drift_provider(source, current, changed={"web/app.jsx": "9" * 40})

        with self.assertRaisesRegex(subject.ContractError, "^ARRIVAL_SURFACE_PARITY_REQUIRED$"):
            subject._arrival_source(provider, source, require_surface_parity=True)

        # ...and the same evidence is accepted when the caller did not ask for it.
        provider = self.drift_provider(source, current, changed={"web/app.jsx": "9" * 40})
        self.assertEqual(
            subject._arrival_source(provider, source, require_surface_parity=False)[
                "drifted_surfaces"
            ],
            ["web"],
        )

    def test_unusable_tree_evidence_fails_closed(self) -> None:
        source = "7368ac85ef809957fa65fe237f3c72829580b4ee"
        current = "0f9c47fe89f12b94a4d9d16c61d6c6df119356bc"

        def truncated(tree: dict[str, object]) -> None:
            tree["truncated"] = True

        def missing_truncated_flag(tree: dict[str, object]) -> None:
            tree.pop("truncated")

        def submodule(tree: dict[str, object]) -> None:
            tree["tree"].append(
                {"path": "vendor/pinned", "type": "commit", "mode": "160000", "sha": "9" * 40}
            )

        def duplicate_path(tree: dict[str, object]) -> None:
            tree["tree"].append(dict(tree["tree"][0]))

        def malformed_blob_sha(tree: dict[str, object]) -> None:
            tree["tree"][0] = {**tree["tree"][0], "sha": "not-a-sha"}

        def empty_tree(tree: dict[str, object]) -> None:
            tree["tree"] = []

        def missing_tree_rows(tree: dict[str, object]) -> None:
            tree.pop("tree")

        for mutate in (
            truncated,
            missing_truncated_flag,
            submodule,
            duplicate_path,
            malformed_blob_sha,
            empty_tree,
            missing_tree_rows,
        ):
            with self.subTest(mutate=mutate.__name__):
                provider = self.drift_provider(source, current)
                tree = provider.json_values[
                    (subject.APP_REPOSITORY, f"/git/trees/{self.tree_sha(current)}?recursive=1")
                ]
                mutate(tree)
                with self.assertRaisesRegex(subject.ContractError, "^ARRIVAL_TREE_INVALID$"):
                    subject._arrival_source(provider, source, require_surface_parity=False)

    def test_a_declared_surface_input_absent_from_main_fails_closed(self) -> None:
        source = "7368ac85ef809957fa65fe237f3c72829580b4ee"
        current = "0f9c47fe89f12b94a4d9d16c61d6c6df119356bc"
        provider = self.drift_provider(source, current, removed=("deploy/nginx.conf",))

        with self.assertRaisesRegex(subject.ContractError, "^ARRIVAL_SURFACE_INPUT_ABSENT$"):
            subject._arrival_source(provider, source, require_surface_parity=False)

    def test_arrival_source_rejects_broken_or_diverged_main_ancestry(self) -> None:
        source = "7368ac85ef809957fa65fe237f3c72829580b4ee"
        current = "39c17213f2573542c2b58743ace12ed5b682f2dc"

        def diverged(value: dict[str, object]) -> None:
            value["status"] = "diverged"

        def wrong_merge_base(value: dict[str, object]) -> None:
            value["merge_base_commit"] = {"sha": "0" * 40}

        def wrong_base(value: dict[str, object]) -> None:
            value["base_commit"] = {"sha": "0" * 40}

        def behind_main(value: dict[str, object]) -> None:
            value["behind_by"] = 1

        def missing_commits(value: dict[str, object]) -> None:
            value.pop("commits")

        def empty_commits(value: dict[str, object]) -> None:
            value["commits"] = []

        def truncated_commits(value: dict[str, object]) -> None:
            value["commits"] = value["commits"][:-1]

        def overlong_commits(value: dict[str, object]) -> None:
            value["commits"] = [*value["commits"], {"sha": "2" * 40}]

        def duplicate_commit(value: dict[str, object]) -> None:
            value["commits"] = [value["commits"][0], value["commits"][0]]

        def malformed_commit(value: dict[str, object]) -> None:
            value["commits"] = [{"sha": "not-a-sha"}, value["commits"][-1]]

        def wrong_terminal(value: dict[str, object]) -> None:
            value["commits"][-1] = {"sha": "0" * 40}

        def malformed_count(value: dict[str, object]) -> None:
            value["behind_by"] = False

        for mutate in (
            diverged,
            wrong_merge_base,
            wrong_base,
            behind_main,
            missing_commits,
            empty_commits,
            truncated_commits,
            overlong_commits,
            duplicate_commit,
            malformed_commit,
            wrong_terminal,
            malformed_count,
        ):
            with self.subTest(mutate=mutate.__name__), self.assertRaisesRegex(
                subject.ContractError, "^ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN$"
            ):
                provider = FakeProvider()
                provider.json_values[(subject.APP_REPOSITORY, "/branches/main")] = {
                    "commit": {"sha": current}
                }
                comparison = self.comparison(source, [])
                mutate(comparison)
                provider.json_values[
                    (subject.APP_REPOSITORY, f"/compare/{source}...{current}")
                ] = comparison
                subject._arrival_source(provider, source, require_surface_parity=False)

    def test_run_list_growth_between_pages_restarts_to_one_stable_snapshot(self) -> None:
        provider = FakeProvider()
        base = "/actions/workflows/deploy-leaf-platform-staging.yml/runs?event=workflow_dispatch&per_page=100"
        page_two = base + "&page=2"
        old_ids = list(range(201, 100, -1))
        new_ids = [202, *old_ids]
        provider.json_sequences[(subject.TF_REPOSITORY, base)] = [
            {"total_count": len(old_ids), "workflow_runs": self.run_rows(old_ids[:100])},
            {"total_count": len(new_ids), "workflow_runs": self.run_rows(new_ids[:100])},
            {"total_count": len(new_ids), "workflow_runs": self.run_rows(new_ids[:100])},
        ]
        provider.json_sequences[(subject.TF_REPOSITORY, page_two)] = [
            {"total_count": len(new_ids), "workflow_runs": self.run_rows(new_ids[100:])},
            {"total_count": len(new_ids), "workflow_runs": self.run_rows(new_ids[100:])},
            {"total_count": len(new_ids), "workflow_runs": self.run_rows(new_ids[100:])},
        ]

        rows = subject._workflow_run_rows(
            provider,
            subject.TF_REPOSITORY,
            "deploy-leaf-platform-staging.yml",
            {"event": "workflow_dispatch", "per_page": 100},
        )

        self.assertEqual([row["id"] for row in rows], new_ids)
        self.assertEqual(provider.calls.count(("GET_JSON", subject.TF_REPOSITORY, base)), 3)

    def test_run_list_snapshot_rejects_non_append_drift_and_malformed_pages(self) -> None:
        base = "/actions/workflows/deploy-leaf-platform-staging.yml/runs?event=workflow_dispatch&per_page=100"
        page_two = base + "&page=2"
        cases = (
            (
                "deletion",
                [{"total_count": 2, "workflow_runs": self.run_rows([2, 1])}, {"total_count": 1, "workflow_runs": self.run_rows([2])}],
                [],
                "PROVIDER_RUN_LIST_DRIFT",
            ),
            (
                "reordering",
                [{"total_count": 2, "workflow_runs": self.run_rows([2, 1])}, {"total_count": 2, "workflow_runs": self.run_rows([1, 2])}],
                [],
                "PROVIDER_RUN_LIST_DRIFT",
            ),
            (
                "duplicate",
                [{"total_count": 2, "workflow_runs": self.run_rows([2, 2])}],
                [],
                "PROVIDER_RUN_DUPLICATE",
            ),
            (
                "invalid",
                [{"total_count": 1, "workflow_runs": [{"id": True}]}],
                [],
                "PROVIDER_RUN_LIST_INVALID",
            ),
            (
                "gap",
                [{"total_count": 101, "workflow_runs": self.run_rows(list(range(201, 101, -1)))}],
                [{"total_count": 101, "workflow_runs": []}],
                "PROVIDER_RUN_PAGINATION_UNPROVEN",
            ),
        )
        for name, first_pages, second_pages, reason in cases:
            with self.subTest(name=name), self.assertRaisesRegex(subject.ContractError, f"^{reason}$"):
                provider = FakeProvider()
                provider.json_sequences[(subject.TF_REPOSITORY, base)] = first_pages
                if second_pages:
                    provider.json_sequences[(subject.TF_REPOSITORY, page_two)] = second_pages
                subject._workflow_run_rows(
                    provider,
                    subject.TF_REPOSITORY,
                    "deploy-leaf-platform-staging.yml",
                    {"event": "workflow_dispatch", "per_page": 100},
                )

    def test_run_list_growth_exhaustion_fails_closed(self) -> None:
        provider = FakeProvider()
        base = "/actions/workflows/deploy-leaf-platform-staging.yml/runs?event=workflow_dispatch&per_page=100"
        page_two = base + "&page=2"
        provider.json_sequences[(subject.TF_REPOSITORY, base)] = [
            {"total_count": total, "workflow_runs": self.run_rows(list(range(total + 100, total, -1)))}
            for total in (101, 102, 103)
        ]
        provider.json_sequences[(subject.TF_REPOSITORY, page_two)] = [
            {"total_count": total, "workflow_runs": self.run_rows([1])}
            for total in (102, 103, 104)
        ]

        with self.assertRaisesRegex(subject.ContractError, "^PROVIDER_RUN_LIST_DRIFT$"):
            subject._workflow_run_rows(
                provider,
                subject.TF_REPOSITORY,
                "deploy-leaf-platform-staging.yml",
                {"event": "workflow_dispatch", "per_page": 100},
            )

        self.assertEqual(provider.calls.count(("GET_JSON", subject.TF_REPOSITORY, base)), 3)

    def test_v3_relay_binds_raw_supply_file_sha_and_exact_candidate(self) -> None:
        candidate = v3_manifest()
        raw = (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode()
        raw_sha = hashlib.sha256(raw).hexdigest()
        canonical_sha = hashlib.sha256(canonical(candidate)).hexdigest()
        self.assertNotEqual(raw_sha, canonical_sha)
        build = {"run_id": 31415926535, "run_attempt": 1, "head_sha": SOURCE}
        supply = subject._supply(
            candidate,
            build,
            APP_TREE,
            file_sha256=raw_sha,
        )
        self.assertEqual(supply["manifest_sha256"], raw_sha)
        receipt = {
            "schema": "leaf.staging-converged.v2",
            "release_source_revision": SOURCE,
            "build_run_attempt": 1,
            "relay_run_id": RELAY_RUN,
            "supply_set_sha256": raw_sha,
            "candidate_supply_set": copy.deepcopy(candidate),
            "automatic_surfaces": ["web", "app"],
            "surface_results": {"web": {}, "app": {}},
            "non_relay_services": {
                "broker": "not_automatically_reconciled",
                "harness": "not_automatically_reconciled",
                "canonical-worker": "not_automatically_reconciled",
            },
            "full_fleet_identity_stamped": False,
        }
        subject._relay_receipt(receipt, build, supply, RELAY_RUN, candidate)

        wrong_hash = copy.deepcopy(receipt)
        wrong_hash["supply_set_sha256"] = canonical_sha
        with self.assertRaisesRegex(subject.ContractError, "^RELAY_RECEIPT_LINEAGE_MISMATCH$"):
            subject._relay_receipt(wrong_hash, build, supply, RELAY_RUN, candidate)

        changed_candidate = copy.deepcopy(receipt)
        changed_candidate["candidate_supply_set"]["services"]["app"]["image_digest"] = (
            "sha256:" + "f" * 64
        )
        with self.assertRaisesRegex(subject.ContractError, "^RELAY_RECEIPT_LINEAGE_MISMATCH$"):
            subject._relay_receipt(changed_candidate, build, supply, RELAY_RUN, candidate)

    def test_future_provider_shape_produces_one_complete_strict_receipt(self) -> None:
        provider = fixture()
        receipt = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        jsonschema.Draft202012Validator(self.schema).validate(receipt)
        self.assertTrue(receipt["terminal_complete"])
        self.assertEqual(receipt["relay"]["mode"], "converged_relay")
        self.assertEqual(receipt["relay"]["artifact"]["status"], "produced")
        self.assertEqual(receipt["relay"]["failure_evidence"], missing())
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
        self.assertFalse(
            any(
                repository == subject.TF_REPOSITORY and endpoint.startswith("/git/")
                for _, repository, endpoint in provider.calls
            )
        )

    def test_child_window_avoids_provider_thousand_run_cap(self) -> None:
        provider = fixture()
        base = (
            "/actions/workflows/deploy-leaf-platform-staging.yml/runs?"
            "event=workflow_dispatch&per_page=100"
        )
        for page in range(1, 11):
            endpoint = base if page == 1 else f"{base}&page={page}"
            provider.json_values[(subject.TF_REPOSITORY, endpoint)] = {
                "total_count": 1012,
                "workflow_runs": [
                    {"id": page * 1000 + offset} for offset in range(100)
                ],
            }
        provider.json_values[(subject.TF_REPOSITORY, f"{base}&page=11")] = {
            "total_count": 0,
            "workflow_runs": [],
        }
        with self.assertRaisesRegex(subject.ContractError, "^PROVIDER_RUN_LIST_DRIFT$"):
            subject._workflow_run_rows(
                provider,
                subject.TF_REPOSITORY,
                "deploy-leaf-platform-staging.yml",
                {"event": "workflow_dispatch", "per_page": 100},
            )

        receipt = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        self.assertTrue(receipt["terminal_complete"])
        child_list_calls = [
            endpoint
            for method, repository, endpoint in provider.calls
            if method == "GET_JSON"
            and repository == subject.TF_REPOSITORY
            and endpoint.startswith(
                "/actions/workflows/deploy-leaf-platform-staging.yml/runs?"
            )
        ]
        self.assertIn(CHILD_WINDOW_EXACT_ENDPOINT, child_list_calls)
        self.assertEqual(child_list_calls[-1], CHILD_WINDOW_EXACT_ENDPOINT)

    def test_required_child_outside_verified_window_fails_closed(self) -> None:
        provider = fixture()
        rows = provider.json_values[
            (subject.TF_REPOSITORY, CHILD_WINDOW_EXACT_ENDPOINT)
        ]["workflow_runs"]
        child = next(row for row in rows if row["id"] == 301)
        child["created_at"] = "2026-08-13T01:09:59Z"
        child["updated_at"] = "2026-08-13T01:09:59Z"
        self.assert_reason("CHILD_RUN_WINDOW_MISMATCH", provider)

    def test_consumer_contract_provider_binding_fails_closed(self) -> None:
        query_key = (
            subject.TF_REPOSITORY,
            "/actions/workflows/publish-leaf-platform-staging-consumer-contract.yml/"
            "runs?branch=main&event=push&status=success&per_page=100",
        )
        artifact_key = (
            subject.TF_REPOSITORY,
            f"/actions/runs/{CONTRACT_RUN}/artifacts?per_page=100",
        )

        provider = fixture()
        duplicate = copy.deepcopy(provider.json_values[query_key]["workflow_runs"][0])
        duplicate["id"] = CONTRACT_RUN + 1
        provider.json_values[query_key]["workflow_runs"].append(duplicate)
        provider.json_values[query_key]["total_count"] = 2
        self.assert_reason("CONSUMER_CONTRACT_RUN_CARDINALITY", provider)

        provider = fixture()
        provider.json_values[artifact_key]["artifacts"][0]["workflow_run"]["head_sha"] = "0" * 40
        self.assert_reason("CONSUMER_CONTRACT_ARTIFACT_ASSOCIATION", provider)

        provider = fixture()
        provider.json_values[artifact_key]["artifacts"].append(
            copy.deepcopy(provider.json_values[artifact_key]["artifacts"][0])
        )
        provider.json_values[artifact_key]["total_count"] = 2
        self.assert_reason("CONSUMER_CONTRACT_ARTIFACT_CARDINALITY", provider)

        provider = fixture()
        provider.json_values[artifact_key]["artifacts"][0]["digest"] = "sha256:" + "0" * 64
        self.assert_reason("CONSUMER_CONTRACT_ARTIFACT_HASH_MISMATCH", provider)

        provider = fixture()
        replace_consumer_contract(
            provider,
            lambda value: value.__setitem__("extra", True),
        )
        self.assert_reason("CONSUMER_CONTRACT_INVALID", provider)

        provider = fixture()
        replace_consumer_contract(
            provider,
            lambda value: value["producer"].__setitem__("head_sha", "0" * 40),
        )
        self.assert_reason("CONSUMER_CONTRACT_PRODUCER_MISMATCH", provider)

        provider = fixture()
        replace_consumer_contract(
            provider,
            lambda value: value["consumer"].__setitem__(
                "deploy_workflow_blob", "0" * 40
            ),
        )
        self.assert_reason("SERVICE_RECEIPT_WORKFLOW_MISMATCH", provider)

        provider = fixture()
        replace_receipt(
            provider,
            303,
            lambda value: value["requested"]["consumer_contract"].__setitem__(
                "sha256", "0" * 64
            ),
        )
        self.assert_reason("SERVICE_RECEIPT_CONSUMER_CONTRACT_MISMATCH", provider)

    def test_intermediate_app_identity_before_explicit_frontier_is_preserved(self) -> None:
        provider = intermediate_app_identity_fixture()
        receipt = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN + 1)
        jsonschema.Draft202012Validator(self.schema).validate(receipt)
        self.assertTrue(receipt["terminal_complete"])
        app = receipt["services"]["app"]
        self.assertEqual(app["selected_run_id"], FRONTIER_RUN + 1)
        self.assertEqual(
            [(row["provider"]["run_id"], row["role"]) for row in app["attempts"]],
            [(302, "relay_app"), (FRONTIER_RUN, "intermediate_app_identity"),
             (FRONTIER_RUN + 1, "frontier_app_identity")],
        )
        self.assertEqual(receipt["deployment_identity"]["body_sha256"], identity()["sha256"])

    def test_intermediate_app_identity_keeps_predecessor_chain_strict(self) -> None:
        provider = intermediate_app_identity_fixture()
        replace_receipt(
            provider, FRONTIER_RUN + 1,
            lambda value: value["facts"]["predecessor_task_definition"].__setitem__(
                "value", "leaf-platform-app:2"
            ),
        )
        with self.assertRaisesRegex(subject.ContractError, "^SERVICE_PREDECESSOR_CHAIN_MISMATCH$"):
            subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN + 1)

    def test_intermediate_app_identity_rejects_other_intents(self) -> None:
        for intent in ("forward", "rollback", "authority-bootstrap"):
            with self.subTest(intent=intent):
                provider = intermediate_app_identity_fixture()
                replace_receipt(
                    provider, FRONTIER_RUN,
                    lambda value: value["requested"].__setitem__("app_deploy_intent", intent),
                )
                with self.assertRaisesRegex(subject.ContractError, "^UNCLASSIFIED_MATCHING_CHILD$"):
                    subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN + 1)

    def test_intermediate_app_identity_requires_coherent_full_identity(self) -> None:
        cases = (
            (lambda value: value["facts"].__setitem__("deployment_identity", missing()),
             "UNCLASSIFIED_MATCHING_CHILD"),
            (lambda value: value["facts"]["deployment_identity"]["value"]["body"].__setitem__("source_revision", "0" * 40),
             "DEPLOYMENT_IDENTITY_INVALID"),
            (lambda value: value["facts"]["deployment_identity"]["value"]["body"]["services"]["broker"].__setitem__("image_digest", "sha256:" + "0" * 64),
             "DEPLOYMENT_IDENTITY_MISMATCH"),
            (lambda value: value["facts"]["deployment_identity"]["value"].__setitem__("sha256", "0" * 64),
             "DEPLOYMENT_IDENTITY_CHECKSUM_INVALID"),
        )
        for mutate, reason in cases:
            with self.subTest(reason=reason):
                provider = intermediate_app_identity_fixture()
                replace_receipt(provider, FRONTIER_RUN, mutate)
                with self.assertRaisesRegex(subject.ContractError, f"^{reason}$"):
                    subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN + 1)

    def test_intermediate_app_identity_requires_selected_candidate_and_terminal_image(self) -> None:
        for slot, reason in (
            ("candidate", "SERVICE_DIGEST_MISMATCH"),
            ("terminal", "SERVICE_DIGEST_MISMATCH"),
            ("both", "SERVICE_CANDIDATE_DIGEST_MISMATCH"),
        ):
            with self.subTest(slot=slot):
                provider = intermediate_app_identity_fixture()

                def mutate(value):
                    if slot == "both":
                        value["facts"]["candidate"]["image_digest"]["value"] = "sha256:" + "0" * 64
                        value["facts"]["terminal"]["value"]["image_digest"]["value"] = "sha256:" + "0" * 64
                        return
                    evidence = value["facts"][slot]
                    if slot == "terminal":
                        evidence = evidence["value"]
                    evidence["image_digest"]["value"] = "sha256:" + "0" * 64

                replace_receipt(provider, FRONTIER_RUN, mutate)
                with self.assertRaisesRegex(subject.ContractError, f"^{reason}$"):
                    subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN + 1)

    def test_intermediate_app_identity_must_be_between_relay_and_frontier(self) -> None:
        for created in ("2026-08-13T01:20:30Z", "2026-08-13T01:26:00Z"):
            with self.subTest(created=created):
                provider = intermediate_app_identity_fixture()
                for (repository, _), value in provider.json_values.items():
                    if repository != subject.TF_REPOSITORY or not isinstance(value, dict):
                        continue
                    for row in value.get("workflow_runs", []):
                        if row["id"] == FRONTIER_RUN:
                            row["created_at"] = created
                            row["updated_at"] = created
                with self.assertRaisesRegex(subject.ContractError, "^UNCLASSIFIED_MATCHING_CHILD$"):
                    subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN + 1)

    def test_intermediate_app_identity_does_not_change_automatic_frontier_policy(self) -> None:
        provider = intermediate_app_identity_fixture()
        with self.assertRaisesRegex(subject.ContractError, "^FRONTIER_RUN_CARDINALITY$"):
            subject._build_receipt(provider, BUILD_RUN, None)

    def test_retained_consumer_matches_exact_receipt_slot(self) -> None:
        provider = retained_consumer_fixture()
        receipt = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        jsonschema.Draft202012Validator(self.schema).validate(receipt)
        self.assertTrue(receipt["terminal_complete"])
        self.assertEqual(receipt["deployment_identity"]["body_sha256"], identity()["sha256"])
        broker = receipt["services"]["broker"]["attempts"][0]
        self.assertEqual(broker["provider"]["head_sha"], "9" * 40)
        self.assertEqual(broker["provider"]["source_tree"], "2" * 40)

    def test_retained_consumer_requires_exact_slot_hash_and_size(self) -> None:
        for field, value in (("sha256", "0" * 64), ("utf8_bytes", 1)):
            with self.subTest(field=field):
                provider = retained_consumer_fixture()
                replace_receipt(
                    provider, FRONTIER_RUN,
                    lambda receipt: receipt["requested"]["consumer_contract"].__setitem__(field, value),
                )
                self.assert_reason("SERVICE_RECEIPT_CONSUMER_CONTRACT_MISMATCH", provider)

    def test_retained_consumer_requires_contained_producer_source(self) -> None:
        changes = (
            {"status": "diverged", "behind_by": 1},
            {"status": "behind", "ahead_by": 0, "behind_by": 1},
            {"merge_base_commit": {"sha": "0" * 40}},
            {"base_commit": {"sha": "0" * 40}},
            {"ahead_by": True},
        )
        for change in changes:
            with self.subTest(change=change):
                provider = retained_consumer_fixture()
                provider.json_values[(subject.TF_REPOSITORY, f"/compare/{TF_HEAD}...{'9' * 40}")].update(change)
                self.assert_reason("CONSUMER_CONTRACT_SOURCE_NOT_CONTAINED", provider)

    def test_retained_consumer_requires_all_three_protected_blobs(self) -> None:
        for path in (
            subject.DEPLOY_WORKFLOW,
            subject.CONSUMER_CONTRACT_WORKFLOW,
            subject.CONSUMER_CONTRACT_SCHEMA_PATH,
        ):
            with self.subTest(path=path):
                provider = retained_consumer_fixture()
                rows = provider.json_values[(subject.TF_REPOSITORY, f"/git/trees/{'2' * 40}?recursive=1")]["tree"]
                next(row for row in rows if row["path"] == path)["sha"] = "0" * 40
                self.assert_reason("CONSUMER_CONTRACT_BLOB_MISMATCH", provider)

    def test_retained_consumer_cannot_use_unverified_artifact(self) -> None:
        provider = retained_consumer_fixture()
        key = (subject.TF_REPOSITORY, f"/actions/runs/{CONTRACT_RUN}/artifacts?per_page=100")
        provider.json_values[key]["artifacts"][0]["digest"] = "sha256:" + "0" * 64
        self.assert_reason("SERVICE_RECEIPT_CONSUMER_CONTRACT_MISMATCH", provider)

    def test_failed_relay_and_provider_bound_resumes_produce_one_complete_receipt(self) -> None:
        provider = failed_relay_fixture()
        receipt = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        jsonschema.Draft202012Validator(self.schema).validate(receipt)

        self.assertTrue(receipt["terminal_complete"])
        self.assertEqual(receipt["relay"]["conclusion"], "failure")
        self.assertEqual(receipt["relay"]["mode"], "resumed_after_failure")
        self.assertEqual(receipt["relay"]["artifact"], missing())
        self.assertEqual(receipt["relay"]["children"], {"web": 301})
        self.assertEqual(
            receipt["relay"]["failure_evidence"]["value"]["failed_stage"],
            "service_dispatch",
        )
        web = receipt["services"]["web"]
        self.assertEqual(web["selected_run_id"], 307)
        self.assertEqual(
            [attempt["role"] for attempt in web["attempts"]],
            ["prior_failed_web", "terminal_web"],
        )
        self.assertEqual(receipt["services"]["app"]["selected_run_id"], FRONTIER_RUN)

    def test_failed_relay_rejects_convergence_artifact_or_conflicting_job_evidence(self) -> None:
        provider = failed_relay_fixture()
        relay_name = f"staging-converged-{SOURCE}-attempt-1"
        provider.json_values[(subject.APP_REPOSITORY, f"/actions/runs/{RELAY_RUN}/artifacts?per_page=100")] = {
            "total_count": 1,
            "artifacts": [artifact(999, relay_name, RELAY_RUN, SOURCE)],
        }
        self.assert_reason("FAILED_RELAY_ARTIFACT_PRESENT", provider)

        provider = failed_relay_fixture()
        jobs = provider.json_values[
            (subject.APP_REPOSITORY, f"/actions/runs/{RELAY_RUN}/attempts/1/jobs?per_page=100")
        ]
        jobs["jobs"][0]["steps"][1]["conclusion"] = "success"
        self.assert_reason("RELAY_FAILURE_EVIDENCE_INVALID", provider)

    def test_failed_relay_child_must_be_the_bound_failed_service_attempt(self) -> None:
        provider = failed_relay_fixture()
        replace_receipt(
            provider,
            301,
            lambda value: (
                value["requested"].__setitem__("service", "broker"),
                value["facts"]["service"].__setitem__("value", "broker"),
            ),
        )
        self.assert_reason("FAILED_RELAY_CHILD_INVALID", provider)

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
            CHILD_WINDOW_EXACT_ENDPOINT,
        )
        provider.json_values[list_key]["workflow_runs"].append(failed_run)
        provider.json_values[list_key]["total_count"] += 1
        name = f"leaf-platform-staging-service-run-{failed_run_id}-attempt-1"
        provider.json_values[(subject.TF_REPOSITORY, f"/actions/runs/{failed_run_id}/artifacts?per_page=100")] = {
            "total_count": 1,
            "artifacts": [artifact(1307, name, failed_run_id, TF_HEAD)],
        }
        receipt = service_receipt(
            "app",
            failed_run_id,
            predecessor="leaf-platform-app:2",
            terminal_td="leaf-platform-app:2",
        )
        receipt["deploy_result"] = "failure"
        receipt["terminal_result"] = "failure"
        receipt["requested"]["app_deploy_intent"] = "configuration"
        receipt["requested"]["hold_seconds"] = "180"
        receipt["requested"]["source_revision"] = "not_produced"
        receipt["failed_stage"] = produced(
            {
                "primary": {
                    "job": "deploy",
                    "step": "Resolve reviewed live deployment identity",
                    "number": 33,
                },
                "additional": [],
                "unique": True,
            }
        )
        receipt["facts"]["candidate"] = {
            "task_definition": missing(),
            "image_digest": produced(DIGESTS["app"]),
        }
        receipt["facts"]["mutation_count"] = produced(0)
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
                    "name": "deploy",
                    "steps": [
                        {
                            "name": "Resolve reviewed live deployment identity",
                            "number": 33,
                            "conclusion": "failure",
                        }
                    ],
                }
            ],
        }
        result = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        app = result["services"]["app"]
        self.assertEqual(len(app["attempts"]), 3)
        failed_attempt = next(
            attempt for attempt in app["attempts"] if attempt["role"] == "prior_failed_app"
        )
        self.assertEqual(
            failed_attempt["outcome"],
            {
                "preflight_state": "not_required",
                "mutation_state": "not_started",
                "mutation_count": 0,
                "deployment_outcome": "failed_before_mutation",
                "rollback_outcome": "not_required",
                "receipt_outcome": "verified",
                "failed_stage": "input_resolution",
                "terminal_reason_code": "pre_mutation_failure",
                "terminal_healthy": True,
            },
        )
        self.assertEqual(app["selected_run_id"], FRONTIER_RUN)
        jsonschema.Draft202012Validator(self.schema).validate(result)

        def terminal_differs(value: dict) -> None:
            terminal = value["facts"]["terminal"]["value"]
            terminal["task_definition"] = "leaf-platform-app:99"
            terminal["primary_deployments"][0]["task_definition"] = "leaf-platform-app:99"

        def mutation_started(value: dict) -> None:
            value["facts"]["mutation_count"] = produced(1)

        def rollback_absent(value: dict) -> None:
            value["facts"]["rollback"] = missing()

        def rollback_failed(value: dict) -> None:
            value["facts"]["rollback"]["value"]["direct_failure_step"] = "failure"

        def terminal_unhealthy(value: dict) -> None:
            value["facts"]["terminal"]["value"]["stable_1_1_0"] = False

        for mutate, reason in (
            (terminal_differs, "SERVICE_ROLLBACK_EVIDENCE_CONFLICT"),
            (mutation_started, "SERVICE_ROLLBACK_EVIDENCE_CONFLICT"),
            (rollback_absent, "SERVICE_ROLLBACK_EVIDENCE_INVALID"),
            (rollback_failed, "SERVICE_ROLLBACK_EVIDENCE_CONFLICT"),
            (terminal_unhealthy, "SERVICE_ROLLBACK_EVIDENCE_CONFLICT"),
        ):
            with self.subTest(reason=reason, mutate=mutate.__name__):
                invalid = copy.deepcopy(provider)
                replace_receipt(invalid, failed_run_id, mutate)
                self.assert_reason(reason, invalid)

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
            CHILD_WINDOW_EXACT_ENDPOINT,
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
        actual_input_failure = produced(
            {
                "primary": {
                    "job": "deploy",
                    "step": "Resolve and validate deployment inputs",
                    "number": 2,
                },
                "additional": [],
                "unique": True,
            }
        )
        self.assertEqual(
            subject._normalized_failed_stage(actual_input_failure, actual_input_failure),
            "input_resolution",
        )

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

    def test_exact_skip_accepts_only_the_producer_unreported_primary_shape(self) -> None:
        facts = service_receipt(
            "web",
            303,
            predecessor="leaf-platform-web:1",
            terminal_td="leaf-platform-web:1",
        )["facts"]
        facts["terminal"]["value"]["primary_deployments"] = {"status": "not_produced"}

        healthy, terminal_td, terminal_digest = subject._terminal_evidence(
            facts, allow_unreported_primary=True
        )
        self.assertTrue(healthy)
        self.assertEqual(terminal_td, "leaf-platform-web:1")
        self.assertEqual(terminal_digest, DIGESTS["web"])
        self.assertFalse(subject._terminal_evidence(facts)[0])
        child = {
            "terminal": facts["terminal"],
            "outcome": {"deployment_outcome": "skipped_exact"},
        }
        self.assertEqual(subject._terminal_digest(child), DIGESTS["web"])
        child["outcome"]["deployment_outcome"] = "succeeded"
        with self.assertRaises(subject.ContractError):
            subject._terminal_digest(child)

    def test_prior_relay_success_requires_the_same_convergence_and_earlier_run(self) -> None:
        selected_receipt = service_receipt(
            "web", 307, predecessor="leaf-platform-web:1", terminal_td="leaf-platform-web:1"
        )
        selected_receipt["requested"]["convergence_id"] = f"{SOURCE}-1-web"
        prior_receipt = copy.deepcopy(selected_receipt)
        self.assertEqual(
            subject._prior_relay_role(
                service="web",
                relay_attempt=2,
                run={"created_at": "2026-08-13T01:20:00Z"},
                receipt=prior_receipt,
                selected_run={"created_at": "2026-08-13T01:21:00Z"},
                selected_receipt=selected_receipt,
            ),
            "prior_relay_web",
        )
        prior_receipt["requested"]["convergence_id"] = f"{SOURCE}-2-web"
        self.assertIsNone(
            subject._prior_relay_role(
                service="web",
                relay_attempt=2,
                run={"created_at": "2026-08-13T01:20:00Z"},
                receipt=prior_receipt,
                selected_run={"created_at": "2026-08-13T01:21:00Z"},
                selected_receipt=selected_receipt,
            )
        )

    def test_pre_mutation_input_failure_needs_no_invented_predecessor(self) -> None:
        previous = {
            "role": "prior_failed_broker",
            "terminal": missing(),
            "predecessor_task_definition": missing(),
            "outcome": {"mutation_count": 0},
        }
        self.assertIsNone(subject._expected_predecessor(previous))
        previous["role"] = "terminal_broker"
        with self.assertRaises(subject.ContractError):
            subject._expected_predecessor(previous)

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

    def test_auto_detected_frontier_ignores_a_later_release_still_deploying(self) -> None:
        """The blocker: an unbounded read aborted on unrelated later activity.

        Handing the finalizer a frontier bounds its API window at
        frontier.updated_at, so a later release's deploy is filtered out server
        side. Auto-detect must query an unbounded window to FIND the frontier,
        and used to abort the moment anything was in flight anywhere after the
        relay. Measured over 25.2h to 2026-09-03, the tf deploy workflow has a
        run live 70.3% of the time, so that read failed roughly seven times in
        ten for reasons with no bearing on this release. Every finalize that
        ever succeeded (4 of 28 lifetime runs) was handed the id by hand.
        """
        provider = fixture()
        # FRONTIER_RUN is created and updated 2026-08-13T01:25:00Z.
        add_child_row(
            provider, active_child_row("2026-08-13T01:40:00Z"), open_window_only=True
        )
        receipt = subject._build_receipt(provider, BUILD_RUN, None)
        self.assertEqual(receipt["frontier_run_id"], FRONTIER_RUN)
        # Parity is the point: discovering the frontier and being handed it must
        # produce the same receipt, byte for byte.
        self.assertEqual(receipt, subject._build_receipt(fixture(), BUILD_RUN, FRONTIER_RUN))

    def test_auto_detected_frontier_still_refuses_activity_inside_its_window(self) -> None:
        """The guard is deferred, not dropped."""
        for created in ("2026-08-13T01:24:00Z", "2026-08-13T01:25:00Z"):
            with self.subTest(created=created):
                provider = fixture()
                add_child_row(
                    provider, active_child_row(created), open_window_only=True
                )
                with self.assertRaisesRegex(subject.ContractError, "^ACTIVE_CHILD_RUN$"):
                    subject._build_receipt(provider, BUILD_RUN, None)

    def test_frontier_absent_and_ambiguous_are_different_reasons(self) -> None:
        """Zero and many need different operator actions, so they say so.

        Zero was the live blocker on finalize run 33698105096: the release's
        convergence sequence had not stamped a full-fleet identity yet. Both
        used to surface as FRONTIER_RUN_CARDINALITY, whose name points at the
        wrong one of the two.
        """
        absent = fixture()
        replace_receipt(
            absent,
            FRONTIER_RUN,
            lambda value: value["facts"].__setitem__(
                "deployment_identity", {"status": "not_produced"}
            ),
        )
        with self.assertRaisesRegex(subject.ContractError, "^FRONTIER_IDENTITY_ABSENT$"):
            subject._build_receipt(absent, BUILD_RUN, None)

        ambiguous = fixture()
        add_identity_child(ambiguous, 307, "2026-08-13T01:26:00Z")
        with self.assertRaisesRegex(subject.ContractError, "^FRONTIER_RUN_CARDINALITY$"):
            subject._build_receipt(ambiguous, BUILD_RUN, None)
        # Naming one resolves it, and the second stamp falls outside the bound.
        named = fixture()
        add_identity_child(named, 307, "2026-08-13T01:26:00Z")
        self.assertEqual(
            subject._build_receipt(named, BUILD_RUN, FRONTIER_RUN)["frontier_run_id"],
            FRONTIER_RUN,
        )

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

    def test_main_that_is_not_a_descendant_or_an_active_child_blocks_the_frontier(self) -> None:
        # Main moving on is normal and is recorded, not refused. Main that does
        # NOT descend from the built sha is a different thing entirely: the
        # commit chain does not terminate at main's tip, so the built sha was
        # never on this history and no receipt may be minted for it.
        provider = fixture()
        current = "0" * 40
        provider.json_values[(subject.APP_REPOSITORY, "/branches/main")]["commit"]["sha"] = current
        provider.json_values[(subject.APP_REPOSITORY, f"/compare/{SOURCE}...{current}")] = self.comparison(
            SOURCE,
            [],
        )
        self.assert_reason("ARRIVAL_SOURCE_IS_NOT_CURRENT_MAIN", provider)

        provider = fixture()
        list_key = (
            subject.TF_REPOSITORY,
            CHILD_WINDOW_EXACT_ENDPOINT,
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

    def test_receipt_records_product_drift_and_still_validates(self) -> None:
        """Finalize at real merge cadence: main has moved, and the receipt says so."""

        app = subject.APP_REPOSITORY
        current = "0f9c47fe89f12b94a4d9d16c61d6c6df119356bc"
        provider = fixture()
        provider.json_values[(app, "/branches/main")]["commit"]["sha"] = current
        provider.json_values[(app, f"/compare/{SOURCE}...{current}")] = self.comparison(
            SOURCE, [], current=current
        )
        provider.json_values[(app, f"/git/commits/{current}")] = {
            "tree": {"sha": self.tree_sha(current)}
        }
        # The build tree stays APP_TREE (the supply manifest pins it, and
        # _workflow_blob resolves both workflows out of it), so the arrival
        # comparison reads that same tree and only main's tree is new.
        provider.json_values[(app, f"/git/trees/{APP_TREE}?recursive=1")] = self.surface_tree()
        provider.json_values[(app, f"/git/trees/{self.tree_sha(current)}?recursive=1")] = (
            self.surface_tree(changed={"web/app.jsx": "9" * 40})
        )

        receipt = subject._build_receipt(provider, BUILD_RUN, None)

        self.assertEqual(receipt["arrival_source"]["relationship"], "ancestor")
        self.assertEqual(receipt["arrival_source"]["current_main_sha"], current)
        self.assertEqual(receipt["arrival_source"]["ahead_by"], 2)
        self.assertEqual(receipt["arrival_source"]["drifted_surfaces"], ["web"])
        self.assertEqual(receipt["arrival_source"]["surfaces"]["app"], "identical")
        jsonschema.Draft202012Validator(self.schema).validate(receipt)

        # The same evidence is refused when the operator asks for the strict
        # property instead of the recorded one.
        with self.assertRaisesRegex(
            subject.ContractError, "^ARRIVAL_SURFACE_PARITY_REQUIRED$"
        ):
            subject._build_receipt(provider, BUILD_RUN, None, require_surface_parity=True)

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

    def test_relay_log_summary_and_step_may_repeat_the_same_exact_children(self) -> None:
        provider = fixture()
        children = b"Watching web deploy run 301.\nWatching app deploy run 302.\n"
        provider.byte_values[(subject.APP_REPOSITORY, f"/actions/runs/{RELAY_RUN}/logs")] = archive_members(
            {
                "0_dispatch.txt": children,
                "dispatch/5_Deploy each staging service in turn and prove each one landed.txt": children,
            }
        )
        receipt = subject._build_receipt(provider, BUILD_RUN, FRONTIER_RUN)
        self.assertEqual(receipt["relay"]["children"], {"web": 301, "app": 302})

    def test_prewarm_receipt_is_never_convergence_evidence(self) -> None:
        """A prewarm leg ends BEFORE the flip, so it is not a landed deploy.

        deploy_mode=prewarm warms the idle colour at listener weight 0 and never
        read-modifies a listener weight. Admitting the key without constraining
        its value would let a receipt for traffic that never moved mint a
        convergence receipt.
        """

        provider = fixture()
        replace_receipt(
            provider, 303, lambda value: value["requested"].__setitem__("deploy_mode", "prewarm")
        )
        self.assert_reason("SERVICE_RECEIPT_IS_A_PREWARM_NOT_A_CONVERGENCE", provider)

    def test_an_unrelated_prewarm_receipt_does_not_abort_the_finalizer(self) -> None:
        """The reason the shape-validation placement was wrong.

        A prewarm receipt for some OTHER release sits in the same provider
        window. It must simply fail to match this supply and be skipped. Refusing
        prewarm inside `_service_receipt` ran over every candidate in the window,
        so this receipt would have aborted the whole finalizer; refusing it at
        selection, after `_receipt_matches_supply`, cannot.
        """

        def foreign_prewarm(value: dict) -> None:
            value["requested"]["deploy_mode"] = "prewarm"
            value["facts"]["source"]["revision"] = {"status": "produced", "value": "9" * 40}

        provider = fixture()
        replace_receipt(provider, 303, foreign_prewarm)
        # 303 no longer carries this supply, so it is skipped and the run stops
        # later for an ordinary missing-evidence reason. The whole point is
        # WHICH reason: anything but SERVICE_RECEIPT_IS_A_PREWARM_NOT_A_
        # CONVERGENCE proves the foreign prewarm was filtered, not adopted and
        # not fatal. Under the old placement this raised the prewarm code.
        self.assert_reason("SERVICE_TERMINAL_CARDINALITY", provider)

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

    def test_service_receipt_accepts_closed_requested_evidence_summaries(self) -> None:
        receipt = service_receipt(
            "app",
            306,
            predecessor="leaf-platform-app:2",
            terminal_td="leaf-platform-app:3",
            with_identity=True,
        )
        requested = receipt["requested"]
        requested["consumer_contract"] = {
            "status": "produced",
            "sha256": "1" * 64,
            "utf8_bytes": 2312,
        }
        requested["supply_evidence"] = {
            "status": "produced",
            "sha256": "2" * 64,
            "utf8_bytes": 11982,
        }
        receipt["receipt_sha256"] = ""
        receipt["receipt_sha256"] = hashlib.sha256(canonical(receipt)).hexdigest()
        self.assertEqual(subject._service_receipt(receipt), receipt)

        for key, value in (
            ("consumer_contract", {"status": "produced", "sha256": "1" * 64, "utf8_bytes": 0}),
            ("supply_evidence", {"status": "produced", "sha256": "2" * 64, "utf8_bytes": True}),
            ("supply_evidence", {"status": "produced", "sha256": "bad", "utf8_bytes": 1}),
            ("consumer_contract", {"status": "not_produced", "extra": True}),
        ):
            with self.subTest(key=key, value=value):
                invalid = copy.deepcopy(receipt)
                invalid["requested"][key] = value
                invalid["receipt_sha256"] = ""
                invalid["receipt_sha256"] = hashlib.sha256(canonical(invalid)).hexdigest()
                with self.assertRaisesRegex(subject.ContractError, "SERVICE_RECEIPT_REQUEST_INVALID"):
                    subject._service_receipt(invalid)

    def test_service_receipt_requires_and_closes_deploy_mode(self) -> None:
        # Regression: the frozen requested_keys set omitted deploy_mode while
        # the tf producer had been emitting it, so _exact fail-closed on EVERY
        # live receipt and no service deploy could be validated (finalize run
        # 33691444128, ERROR:SERVICE_RECEIPT_REQUEST_INVALID). Admitting the
        # key is only half the fix: the tf workflow declares deploy_mode
        # `type: choice` over exactly {normal, prewarm} and re-gates it with
        # `case "$DEPLOY_MODE" in normal|prewarm)`, so the verifier mirrors
        # that closed set rather than accepting free text into a deliberately
        # exact-match contract.
        def sealed(**overrides: object) -> dict:
            receipt = service_receipt(
                "app",
                306,
                predecessor="leaf-platform-app:2",
                terminal_td="leaf-platform-app:3",
                with_identity=True,
            )
            for key, value in overrides.items():
                if value is _ABSENT:
                    del receipt["requested"][key]
                else:
                    receipt["requested"][key] = value
            receipt["receipt_sha256"] = ""
            receipt["receipt_sha256"] = hashlib.sha256(canonical(receipt)).hexdigest()
            return receipt

        # Both real provider options parse. prewarm must not fail closed here:
        # a by-design prewarm run sharing the scan window would otherwise turn
        # into a hard convergence outage at parse time.
        for mode in ("normal", "prewarm"):
            with self.subTest(mode=mode):
                receipt = sealed(deploy_mode=mode)
                self.assertEqual(subject._service_receipt(receipt), receipt)

        # A receipt that omits the key is rejected, so the verifier can never
        # silently drift back to a producer that stopped emitting it.
        with self.assertRaisesRegex(subject.ContractError, "SERVICE_RECEIPT_REQUEST_INVALID"):
            subject._service_receipt(sealed(deploy_mode=_ABSENT))

        # Well-formed strings outside the provider's option list are rejected
        # by this gate specifically, so admitting the key did not turn a frozen
        # contract into a free-text field.
        for value in ("NORMAL", "normal ", " normal", "canary", "prewarm,normal", "deploy"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(subject.ContractError, "SERVICE_RECEIPT_REQUEST_INVALID"):
                    subject._service_receipt(sealed(deploy_mode=value))

        # Wrong-typed and empty values fail CLOSED with a contract reason. The
        # unhashable cases would raise a bare TypeError out of a `not in <set>`
        # membership test, crashing the finalizer instead of naming a reason.
        for value in ("", True, None, 1, 1.5, ["normal"], {"normal": True}):
            with self.subTest(value=value):
                with self.assertRaises(subject.ContractError):
                    subject._service_receipt(sealed(deploy_mode=value))

    def test_service_receipt_deploy_mode_matches_provider_declared_set(self) -> None:
        # The verifier's closed set is a MIRROR of the provider's, not an
        # independent opinion. If tf adds a third mode, this is the line that
        # should be updated deliberately alongside the convergence semantics
        # for that mode, rather than the set quietly widening.
        self.assertEqual(subject.SERVICE_DEPLOY_MODES, {"normal", "prewarm"})

    # `x not in <set>` HASHES x, so a list- or dict-valued provider field raised
    # a bare `TypeError: unhashable type` straight out of the finalizer instead
    # of naming a reason code, the exact opposite of a gate whose whole contract
    # is to fail CLOSED. PR #945 fixed this for the deploy_mode check it
    # introduced; the three tests below cover every other closed-vocabulary
    # membership test in this file that a provider value can reach. Measured on
    # this tree 2026-09-02: all fourteen sites raised TypeError before the
    # isinstance guards, and no hashable value changed the reason code it
    # already produced (the second half of each table pins that).
    UNHASHABLE = (["x"], {"a": 1})

    def test_receipt_closed_labels_fail_closed_on_unhashable_values(self) -> None:
        def sealed(mutate, value) -> dict:
            receipt = service_receipt(
                "app",
                306,
                predecessor="leaf-platform-app:2",
                terminal_td="leaf-platform-app:3",
                with_identity=True,
            )
            mutate(receipt, value)
            # Re-seal, or the checksum gate fires before the check under test.
            receipt["receipt_sha256"] = ""
            receipt["receipt_sha256"] = hashlib.sha256(canonical(receipt)).hexdigest()
            return receipt

        # (field, reason, mutation). The two `status` rows reach the shared
        # _strict_slot helper, which every receipt slot runs through.
        fields = (
            ("requested.app_deploy_intent", "SERVICE_RECEIPT_REQUEST_INVALID",
             lambda receipt, value: receipt["requested"].__setitem__("app_deploy_intent", value)),
            ("preflight_result", "SERVICE_OUTCOME_LABEL_INVALID",
             lambda receipt, value: receipt.__setitem__("preflight_result", value)),
            ("deploy_result", "SERVICE_OUTCOME_LABEL_INVALID",
             lambda receipt, value: receipt.__setitem__("deploy_result", value)),
            ("terminal_result", "SERVICE_OUTCOME_LABEL_INVALID",
             lambda receipt, value: receipt.__setitem__("terminal_result", value)),
            ("path", "SERVICE_RECEIPT_INVALID",
             lambda receipt, value: receipt.__setitem__("path", value)),
            ("facts.prior_job_status.value", "SERVICE_OUTCOME_LABEL_INVALID",
             lambda receipt, value: receipt["facts"]["prior_job_status"].__setitem__("value", value)),
            ("facts.service.status", "SERVICE_RECEIPT_FACTS_INVALID",
             lambda receipt, value: receipt["facts"]["service"].__setitem__("status", value)),
            ("failed_stage.status", "SERVICE_RECEIPT_FAILED_STAGE_INVALID",
             lambda receipt, value: receipt["failed_stage"].__setitem__("status", value)),
        )
        for field, reason, mutate in fields:
            for value in self.UNHASHABLE:
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(subject.ContractError, reason):
                        subject._service_receipt(sealed(mutate, value))
            # The isinstance guard must not shift which gate claims an
            # already-handled value: a wrong-but-hashable type keeps the reason
            # code it produced before, so the fix widened nothing.
            for value in (None, 1, True, "bogus"):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(subject.ContractError, reason):
                        subject._service_receipt(sealed(mutate, value))

    def test_rollback_evidence_closed_labels_fail_closed_on_unhashable_values(self) -> None:
        baseline = service_receipt(
            "app", 306, predecessor="leaf-platform-app:2", terminal_td="leaf-platform-app:3"
        )["facts"]["rollback"]
        kwargs = dict(
            mutation_count=1,
            provider_conclusion="success",
            terminal_healthy=True,
            terminal_td="leaf-platform-app:3",
            predecessor_td="leaf-platform-app:2",
        )
        self.assertEqual(subject._rollback_outcome(baseline, **kwargs), "not_required")

        for field in (
            "bluegreen_step", "direct_failure_step", "direct_cancel_step",
            "bluegreen_detail", "authority_result",
        ):
            for value in (*self.UNHASHABLE, None, 1, True, "bogus"):
                with self.subTest(field=field, value=value):
                    evidence = copy.deepcopy(baseline)
                    evidence["value"][field] = value
                    with self.assertRaisesRegex(
                        subject.ContractError, "SERVICE_ROLLBACK_EVIDENCE_INVALID"
                    ):
                        subject._rollback_outcome(evidence, **kwargs)

    def test_provider_run_and_relay_step_labels_fail_closed_on_unhashable_values(self) -> None:
        def provider_run(conclusion: object) -> dict:
            return {
                "path": subject.DEPLOY_WORKFLOW,
                "id": 303,
                "run_attempt": 1,
                "event": "workflow_dispatch",
                "head_sha": TF_HEAD,
                "head_branch": "main",
                "status": "completed",
                "conclusion": conclusion,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:01Z",
            }

        parsed = subject._provider_run(
            provider_run("success"), subject.TF_REPOSITORY, subject.DEPLOY_WORKFLOW, "workflow_dispatch"
        )
        self.assertEqual(parsed["conclusion"], "success")
        # None is what the API sends for a run still in flight, so it has to
        # keep landing on the same reason code the unhashable cases now use.
        for value in (*self.UNHASHABLE, None, 1, True, "bogus"):
            with self.subTest(conclusion=value):
                with self.assertRaisesRegex(subject.ContractError, "PROVIDER_RUN_MISMATCH"):
                    subject._provider_run(
                        provider_run(value), subject.TF_REPOSITORY, subject.DEPLOY_WORKFLOW, "workflow_dispatch"
                    )

        # The relay step-name test is `not in <mapping>`, which hashes too. Its
        # miss path is `continue`, so an unhashable name has to fall through to
        # the cardinality check rather than crash the scan.
        endpoint = f"/actions/runs/{RELAY_RUN}/attempts/1/jobs?per_page=100"
        steps = [
            {"name": "Deploy each staging service in turn and prove each one landed",
             "number": 1, "conclusion": "failure"},
            {"name": "Publish the convergence receipt", "number": 2, "conclusion": "skipped"},
        ]

        def relay_provider(step_name: object) -> FakeProvider:
            provider = FakeProvider()
            jobs = copy.deepcopy(steps)
            jobs[0]["name"] = step_name
            provider.json_values[(subject.APP_REPOSITORY, endpoint)] = {
                "total_count": 1,
                "jobs": [{"name": "dispatch", "steps": jobs}],
            }
            return provider

        relay = {"run_id": RELAY_RUN, "run_attempt": 1}
        self.assertEqual(
            subject._relay_failure_evidence(relay_provider(steps[0]["name"]), relay)["status"],
            "produced",
        )
        for value in (*self.UNHASHABLE, None, 1, True, "bogus"):
            with self.subTest(step_name=value):
                with self.assertRaisesRegex(
                    subject.ContractError, "RELAY_FAILURE_EVIDENCE_INVALID"
                ):
                    subject._relay_failure_evidence(relay_provider(value), relay)

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
            CHILD_WINDOW_EXACT_ENDPOINT,
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
        inputs = trigger["workflow_dispatch"]["inputs"]
        self.assertEqual(
            set(inputs),
            {"producer_build_run_id", "current_frontier_run_id", "require_surface_parity"},
        )
        # Every free-text input is still a run identifier and nothing else. The
        # third input carries no caller text at all: it is a typed boolean whose
        # only effect is to make the verifier STRICTER, and it defaults off, so
        # the permissive reading is never something a caller has to ask for.
        for identifier in ("producer_build_run_id", "current_frontier_run_id"):
            self.assertEqual(inputs[identifier]["type"], "string")
        self.assertEqual(inputs["require_surface_parity"]["type"], "boolean")
        self.assertIs(inputs["require_surface_parity"]["default"], False)
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
