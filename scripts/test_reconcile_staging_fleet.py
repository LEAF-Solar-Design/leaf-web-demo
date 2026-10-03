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
import zipfile

import yaml


# Same stdlib preload the convergence suite uses, and for the same reason: the
# repo root carries a `platform/` package that shadows the stdlib module, and
# pytest loads plugins that import `platform` before this file runs. Pin the
# real module first, widen sys.path only afterwards.
_platform_spec = importlib.util.spec_from_file_location(
    "platform", Path(sysconfig.get_path("stdlib")) / "platform.py"
)
assert _platform_spec and _platform_spec.loader
_stdlib_platform = importlib.util.module_from_spec(_platform_spec)
sys.modules["platform"] = _stdlib_platform
_platform_spec.loader.exec_module(_stdlib_platform)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import reconcile_staging_fleet as subject  # noqa: E402


APP = subject.APP_REPOSITORY
TF = subject.TF_REPOSITORY
RELAY_WF = "dispatch-staging-deploys.yml"
DEPLOY_WF = "deploy-leaf-platform-staging.yml"

SOURCE = "a" * 40
RELAY_RUN = 900
BUILD_RUN = 800
ACCOUNT = "arn:aws:ecs:us-east-1:807034087062:task-definition"

# Run ids for each service's most recent settled deploy.
SETTLED = {
    "web": 701,
    "app": 702,
    "broker": 703,
    "harness": 704,
    "canonical-worker": 705,
}


def digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def image_tag(service: str) -> str:
    return f"surface-v1-{hashlib.sha256(service.encode()).hexdigest()}"


def release_digests() -> dict[str, str]:
    return {service: digest(f"release-{service}") for service in subject.SERVICE_ORDER}


def archive(name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        handle.writestr(name, payload)
    return buffer.getvalue()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


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


def run_row(
    run_id: int,
    *,
    status: str = "completed",
    head_sha: str = SOURCE,
    service: str | None = None,
) -> dict:
    # display_title carries the provider's run-name contract, "Deploy
    # leaf-platform staging <service> (<image_tag>)". The planner uses it only
    # to choose which receipts are worth reading; the receipt still decides.
    title = (
        f"Deploy leaf-platform staging {service} (surface-v1-{run_id})"
        if service
        else "Dispatch staging deploys"
    )
    return {
        "id": run_id,
        "run_attempt": 1,
        "status": status,
        "conclusion": "success" if status == "completed" else None,
        "head_sha": head_sha,
        "display_title": title,
        "created_at": "2026-09-03T00:00:00Z",
        "updated_at": "2026-09-03T00:10:00Z",
    }


def runs_endpoint(workflow: str, query: str) -> str:
    return f"/actions/workflows/{workflow}/runs?{query}"


def relay_receipt(digests: dict[str, str]) -> dict:
    return {
        "schema": "leaf.staging-converged.v2",
        "release_source_revision": SOURCE,
        "relay_run_id": RELAY_RUN,
        "supply_set_sha256": "c" * 64,
        "candidate_supply_set": {
            "build_run_id": BUILD_RUN,
            # immutable_lookup_tag is the tag the relay itself dispatches, and
            # it is per service; the planner must never re-derive or guess one,
            # because the provider deploys whatever tag it is handed.
            "services": {
                s: {"image_digest": d, "immutable_lookup_tag": image_tag(s)}
                for s, d in digests.items()
            },
        },
    }


def service_receipt(service: str, run_id: int, image: str, task_def: str) -> dict:
    return {
        "requested": {"service": service, "app_deploy_intent": "forward"},
        "facts": {
            "terminal": {
                "status": "produced",
                "value": {
                    "image_digest": {"status": "produced", "value": image},
                    "task_definition": task_def,
                },
            }
        },
    }


def artifact_row(artifact_id: int, name: str, run_id: int) -> dict:
    return {
        "id": artifact_id,
        "name": name,
        "expired": False,
        "workflow_run": {"id": run_id, "head_sha": SOURCE},
    }


def fixture(
    *,
    relay_status: str = "completed",
    deploy_status: str = "completed",
    lagging: tuple[str, ...] = (),
    missing_receipt: tuple[str, ...] = (),
    envelope: bool = True,
    contract: bool = True,
) -> FakeProvider:
    """A quiet single-release world with every service settled on the release.

    `lagging` puts a service on a stale digest; `missing_receipt` removes its
    receipt entirely so the planner sees `unknown`.
    """
    provider = FakeProvider()
    digests = release_digests()

    # --- yield reads -------------------------------------------------------
    provider.json_values[(APP, runs_endpoint(RELAY_WF, "per_page=50"))] = {
        "total_count": 1,
        "workflow_runs": [run_row(RELAY_RUN, status=relay_status)],
    }
    provider.json_values[(TF, runs_endpoint(DEPLOY_WF, "per_page=50"))] = {
        "total_count": 1,
        "workflow_runs": [
            run_row(SETTLED["app"], status=deploy_status, service="app")
        ],
    }

    # --- newest relay release ---------------------------------------------
    provider.json_values[
        (APP, runs_endpoint(RELAY_WF, f"status=success&per_page={subject.MAX_RELAY_RUN_SCAN}"))
    ] = {"total_count": 1, "workflow_runs": [run_row(RELAY_RUN)]}
    relay_name = f"staging-converged-{SOURCE}-attempt-1"
    relay_artifacts = [artifact_row(2000, relay_name, RELAY_RUN)]
    if envelope:
        relay_artifacts.append(
            artifact_row(
                2001,
                f"staging-supply-evidence-{SOURCE}-attempt-1",
                RELAY_RUN,
            )
        )
    if contract:
        relay_artifacts.append(
            artifact_row(
                2002,
                f"staging-consumer-contract-{SOURCE}-attempt-1",
                RELAY_RUN,
            )
        )
    provider.json_values[(APP, f"/actions/runs/{RELAY_RUN}/artifacts?per_page=100")] = {
        "total_count": len(relay_artifacts),
        "artifacts": relay_artifacts,
    }
    provider.byte_values[(APP, "/actions/artifacts/2000/zip")] = archive(
        subject.RELAY_ARTIFACT_FILE, canonical(relay_receipt(digests))
    )

    # --- settled per-service state ----------------------------------------
    query = (
        "event=workflow_dispatch&status=success"
        f"&per_page={subject.MAX_DEPLOY_RUN_SCAN}"
    )
    provider.json_values[(TF, runs_endpoint(DEPLOY_WF, query))] = {
        "total_count": len(SETTLED),
        "workflow_runs": [
            run_row(SETTLED[s], service=s) for s in subject.SERVICE_ORDER
        ],
    }
    for service in subject.SERVICE_ORDER:
        run_id = SETTLED[service]
        name = f"leaf-platform-staging-service-run-{run_id}-attempt-1"
        if service in missing_receipt:
            provider.json_values[(TF, f"/actions/runs/{run_id}/artifacts?per_page=100")] = {
                "total_count": 0,
                "artifacts": [],
            }
            continue
        image = digest(f"stale-{service}") if service in lagging else digests[service]
        provider.json_values[(TF, f"/actions/runs/{run_id}/artifacts?per_page=100")] = {
            "total_count": 1,
            "artifacts": [artifact_row(run_id + 1000, name, run_id)],
        }
        provider.byte_values[(TF, f"/actions/artifacts/{run_id + 1000}/zip")] = archive(
            subject.ARTIFACT_FILE,
            canonical(
                service_receipt(service, run_id, image, f"{ACCOUNT}/leaf-platform-{service}:762")
            ),
        )
    return provider


class YieldTests(unittest.TestCase):
    def test_only_proven_oversize_dispatches_without_jobs_are_inert(self) -> None:
        # Captured provider topology from the 2026-09-04 oversize incident.
        run_id = 33830277170  # Not in the frozen incident registry.
        revision = "49e265747ca7812d6f4c45e64aba93ce2169daf4"
        path = f".github/workflows/{DEPLOY_WF}"
        cases = (
            ("oversize", 522389, {"total_count": 0, "jobs": []}, "clear"),
            ("within_limit", 512000, {"total_count": 0, "jobs": []}, "yielded"),
            ("has_job", 522389, {"total_count": 1, "jobs": [{"id": 1}]}, "yielded"),
            ("unreadable_jobs", 522389, None, "yielded"),
            ("unreadable_source", None, {"total_count": 0, "jobs": []}, "yielded"),
        )
        for label, size, jobs, expected in cases:
            with self.subTest(label=label):
                provider = fixture()
                row = run_row(run_id, status="queued", head_sha=revision)
                row.update(event="workflow_dispatch", path=path)
                provider.json_values[(TF, runs_endpoint(DEPLOY_WF, "per_page=50"))] = {
                    "workflow_runs": [row]
                }
                if jobs is not None:
                    provider.json_values[(TF, f"/actions/runs/{run_id}/jobs?per_page=1")] = jobs
                if size is not None:
                    provider.json_values[(TF, f"/contents/{path}?ref={revision}")] = {
                        "type": "file", "size": size,
                    }
                self.assertEqual(subject.yield_check(provider)["status"], expected)

    def test_frozen_incident_does_not_require_new_contents_permission(self) -> None:
        path = f".github/workflows/{DEPLOY_WF}"
        for run_id in subject.OVERSIZE_INCIDENT_RUNS:
            with self.subTest(run_id=run_id):
                provider = fixture()
                row = run_row(run_id, status="queued", head_sha=subject.OVERSIZE_INCIDENT_REVISION)
                row.update(event="workflow_dispatch", path=path)
                provider.json_values[(TF, f"/actions/runs/{run_id}/jobs?per_page=1")] = {
                    "total_count": 0, "jobs": [],
                }
                # No Contents response: Actions-only workflow credentials.
                self.assertTrue(subject._unstartable_dispatch(provider, TF, DEPLOY_WF, row))

    def test_frozen_incident_never_admits_drift_or_a_real_job(self) -> None:
        run_id = 33835703473
        path = f".github/workflows/{DEPLOY_WF}"
        for label in ("id", "revision", "path", "repository", "job", "unreadable_jobs"):
            with self.subTest(label=label):
                provider = fixture()
                row = run_row(run_id, status="queued", head_sha=subject.OVERSIZE_INCIDENT_REVISION)
                row.update(event="workflow_dispatch", path=path)
                if label == "id":
                    row["id"] += 1
                if label == "revision":
                    row["head_sha"] = "0" * 40
                if label == "path":
                    row["path"] = ".github/workflows/other.yml"
                repository = APP if label == "repository" else TF
                if label != "unreadable_jobs":
                    provider.json_values[(repository, f"/actions/runs/{row['id']}/jobs?per_page=1")] = {
                        "total_count": 1 if label == "job" else 0,
                        "jobs": [{"id": 1}] if label == "job" else [],
                    }
                self.assertFalse(subject._unstartable_dispatch(provider, repository, DEPLOY_WF, row))

    def test_yields_to_a_live_relay_and_to_any_live_staging_deploy(self) -> None:
        """This lane is strictly lower priority than the relay.

        The relay already abandons its SECOND service whenever main moves
        inside its window, so a reconciler that took the shared staging lock
        from it would worsen exactly the split it exists to close.
        """
        for status, reason in (("in_progress", "RELAY_LIVE"), ("queued", "RELAY_LIVE")):
            with self.subTest(status=status):
                plan = subject.build_plan(fixture(relay_status=status))
                self.assertEqual(plan["yield"]["status"], "yielded")
                self.assertEqual(plan["yield"]["reason"], reason)

        plan = subject.build_plan(fixture(deploy_status="in_progress"))
        self.assertEqual(plan["yield"]["reason"], "STAGING_DEPLOY_LIVE")

    def test_a_quiet_window_is_clear(self) -> None:
        plan = subject.build_plan(fixture())
        self.assertEqual(plan["yield"]["status"], "clear")


class PlanTests(unittest.TestCase):
    def test_relay_listing_stale_then_fresh_binds_newest_release(self) -> None:
        provider = fixture()
        endpoint = runs_endpoint(RELAY_WF, f"status=success&per_page={subject.MAX_RELAY_RUN_SCAN}")
        old_run = RELAY_RUN - 100
        old_source = "b" * 40
        old_receipt = relay_receipt({s: digest(f"old-{s}") for s in subject.SERVICE_ORDER})
        old_receipt.update(relay_run_id=old_run, release_source_revision=old_source)
        old_receipt["candidate_supply_set"]["build_run_id"] = BUILD_RUN - 100
        old_artifact = artifact_row(1900, f"staging-converged-{old_source}-attempt-1", old_run)
        old_artifact["workflow_run"]["head_sha"] = old_source
        provider.json_values[(APP, f"/actions/runs/{old_run}/artifacts?per_page=100")] = {
            "total_count": 1, "artifacts": [old_artifact],
        }
        provider.byte_values[(APP, "/actions/artifacts/1900/zip")] = archive(
            subject.RELAY_ARTIFACT_FILE, canonical(old_receipt)
        )
        stale = {"workflow_runs": [run_row(old_run, head_sha=old_source)]}
        fresh = provider.json_values[(APP, endpoint)]
        provider.json_sequences[(APP, endpoint)] = [stale, fresh, stale]

        plan = subject.build_plan(provider)

        self.assertEqual(plan["release"]["relay_run_id"], RELAY_RUN)
        self.assertEqual(provider.calls.count(("GET_JSON", APP, endpoint)), 3)

    def test_settled_listing_stale_then_fresh_keeps_fresh_service(self) -> None:
        provider = fixture()
        endpoint = runs_endpoint(
            DEPLOY_WF,
            f"event=workflow_dispatch&status=success&per_page={subject.MAX_DEPLOY_RUN_SCAN}",
        )
        old_run = SETTLED["broker"] - 100
        stale = {"workflow_runs": [run_row(old_run, service="broker")]}
        name = f"leaf-platform-staging-service-run-{old_run}-attempt-1"
        provider.json_values[(TF, f"/actions/runs/{old_run}/artifacts?per_page=100")] = {
            "total_count": 1, "artifacts": [artifact_row(1603, name, old_run)],
        }
        provider.byte_values[(TF, "/actions/artifacts/1603/zip")] = archive(
            subject.ARTIFACT_FILE,
            canonical(service_receipt("broker", old_run, digest("old-broker"), f"{ACCOUNT}/leaf-platform-broker:761")),
        )
        fresh = provider.json_values[(TF, endpoint)]
        provider.json_sequences[(TF, endpoint)] = [stale, fresh, stale]

        plan = subject.build_plan(provider)

        self.assertEqual(plan["services"]["broker"]["observed_from_run_id"], SETTLED["broker"])
        self.assertEqual(plan["services"]["broker"]["status"], "converged")
        self.assertEqual(provider.calls.count(("GET_JSON", TF, endpoint)), 3)

    def test_three_empty_relay_reads_fail_closed(self) -> None:
        provider = fixture()
        endpoint = runs_endpoint(RELAY_WF, f"status=success&per_page={subject.MAX_RELAY_RUN_SCAN}")
        provider.json_sequences[(APP, endpoint)] = [{"workflow_runs": []}] * 3

        with self.assertRaisesRegex(subject.ContractError, "^NO_CONVERGED_RELEASE$"):
            subject.build_plan(provider)

        self.assertEqual(provider.calls.count(("GET_JSON", APP, endpoint)), 3)

    def test_a_converged_fleet_plans_no_deploys(self) -> None:
        plan = subject.build_plan(fixture())
        self.assertEqual(plan["lagging"], [])
        self.assertEqual(
            [s["kind"] for s in plan["steps"]], ["identity_restamp"]
        )
        for service in subject.SERVICE_ORDER:
            self.assertEqual(plan["services"][service]["status"], "converged")

    def test_lagging_non_relay_services_are_ordered_cheapest_first(self) -> None:
        """Order is the measured medians (broker 5.4, harness 6.0, worker 7.8
        minutes), so a lane that loses the single staging lock partway has still
        advanced as many services as it could."""
        plan = subject.build_plan(fixture(lagging=("canonical-worker", "broker", "harness")))
        self.assertEqual(plan["lagging"], ["broker", "harness", "canonical-worker"])
        self.assertEqual(
            [s["service"] for s in plan["steps"]],
            ["broker", "harness", "canonical-worker", "app"],
        )
        self.assertEqual([s["position"] for s in plan["steps"]], [1, 2, 3, 4])

    def test_non_relay_steps_use_direct_mode_and_exact_receipt_baselines(self) -> None:
        # Provider run 33938761749 refused digest-aware broker before credentials.
        # Its next baseline check also permits auto-live only for web/app.
        plan = subject.build_plan(fixture(lagging=subject.NON_RELAY_SERVICES))
        for step in plan["steps"]:
            if step["kind"] != "reconcile":
                continue
            with self.subTest(service=step["service"]):
                inputs = step["inputs"]
                self.assertEqual(inputs["digest_aware_reconcile"], "false")
                self.assertNotIn("convergence_id", inputs)
                self.assertEqual(inputs["deploy_strategy"], "direct")
                self.assertEqual(inputs["app_deploy_intent"], "forward")
                self.assertEqual(
                    inputs["expected_task_definition"],
                    f"{ACCOUNT}/leaf-platform-{step['service']}:762",
                )

    def test_missing_non_relay_baseline_cannot_dispatch_or_restamp(self) -> None:
        for service in subject.NON_RELAY_SERVICES:
            with self.subTest(service=service):
                plan = subject.build_plan(fixture(missing_receipt=(service,)))
                self.assertFalse(plan["armable"])
                self.assertNotIn(service, [step["service"] for step in plan["steps"]])
                self.assertTrue(any(
                    reason.startswith("SERVICE_BASELINE_UNKNOWN")
                    for reason in plan["not_armable_because"]
                ))

    def test_non_relay_receipts_can_be_found_after_a_busy_product_page(self) -> None:
        provider = fixture(lagging=subject.NON_RELAY_SERVICES)
        query = (
            "event=workflow_dispatch&status=success"
            f"&per_page={subject.MAX_DEPLOY_RUN_SCAN}"
        )
        endpoint = runs_endpoint(DEPLOY_WF, query)
        rows = provider.json_values[(TF, endpoint)]["workflow_runs"]
        provider.json_values[(TF, endpoint)] = {
            "workflow_runs": [run_row(SETTLED["app"], service="app")]
            * subject.MAX_DEPLOY_RUN_SCAN,
        }
        provider.json_values[(TF, endpoint + "&page=2")] = {"workflow_runs": rows}
        plan = subject.build_plan(provider)
        self.assertTrue(plan["armable"], plan["not_armable_because"])
        self.assertEqual([s["service"] for s in plan["steps"]], [
            "broker", "harness", "canonical-worker", "app",
        ])

    def test_settled_scan_stops_at_its_bound_without_guessing_baselines(self) -> None:
        provider = fixture()
        query = (
            "event=workflow_dispatch&status=success"
            f"&per_page={subject.MAX_DEPLOY_RUN_SCAN}"
        )
        for page in range(1, subject.MAX_DEPLOY_RUN_PAGES + 1):
            endpoint = runs_endpoint(DEPLOY_WF, query)
            if page > 1:
                endpoint += f"&page={page}"
            provider.json_values[(TF, endpoint)] = {
                "workflow_runs": [run_row(SETTLED["app"], service="app")]
                * subject.MAX_DEPLOY_RUN_SCAN,
            }
        plan = subject.build_plan(provider)
        self.assertFalse(plan["armable"])
        self.assertEqual(plan["services"]["broker"]["status"], "unknown")

    def test_the_restamp_never_uses_auto_live_and_names_the_app_baseline(self) -> None:
        """The provider refuses auto-live unless app_deploy_intent is forward,
        and this is a configuration deploy, so the baseline must be an exact
        arn. It is read from the app's own receipt, not guessed and not from
        AWS: on the dad27a10 wave the app deploy recorded leaf-platform-app:762
        and the restamp was dispatched against exactly that."""
        plan = subject.build_plan(fixture())
        restamp = next(s for s in plan["steps"] if s["kind"] == "identity_restamp")
        expected = f"{ACCOUNT}/leaf-platform-app:762"
        self.assertEqual(restamp["inputs"]["expected_task_definition"], expected)
        self.assertEqual(restamp["inputs"]["configuration_task_definition"], expected)
        self.assertEqual(restamp["inputs"]["app_deploy_intent"], "configuration")
        self.assertEqual(restamp["inputs"]["deploy_strategy"], "direct")
        self.assertNotIn("auto-live", json.dumps(restamp["inputs"]))

    def test_the_restamp_is_last_and_says_why_when_the_fleet_still_lags(self) -> None:
        """The identity samples LIVE digests for all five services, so stamping
        before they land produces a body the finalizer rejects with
        DEPLOYMENT_IDENTITY_MISMATCH."""
        plan = subject.build_plan(fixture(lagging=("broker",)))
        self.assertEqual(plan["steps"][-1]["kind"], "identity_restamp")
        self.assertTrue(
            any(b.startswith("FLEET_NOT_CONVERGED") for b in plan["blockers"]),
            plan["blockers"],
        )

    def test_a_relay_surface_behind_is_the_relays_job_not_this_lanes(self) -> None:
        """web and app belong to the relay. This lane must never plan a deploy
        for them or it would race the relay for the lock."""
        plan = subject.build_plan(fixture(lagging=("web",)))
        self.assertNotIn("web", [s["service"] for s in plan["steps"]])
        self.assertTrue(
            any(b.startswith("RELAY_SURFACES_NOT_CONVERGED") for b in plan["blockers"]),
            plan["blockers"],
        )

    def test_an_unreadable_app_baseline_blocks_instead_of_guessing(self) -> None:
        plan = subject.build_plan(fixture(missing_receipt=("app",)))
        self.assertEqual(plan["services"]["app"]["status"], "unknown")
        self.assertTrue(
            any(b.startswith("RELAY_SURFACES_NOT_CONVERGED") for b in plan["blockers"]),
            plan["blockers"],
        )
        self.assertNotIn("identity_restamp", [s["kind"] for s in plan["steps"]])

    def test_plan_is_report_only_and_states_its_arming_prerequisite(self) -> None:
        """Arming is blocked on something this lane cannot do for itself: the
        provider hard-requires the supply evidence envelope's relay.workflow_path
        to be the relay's own, so every step needs the envelope the relay minted
        and does not yet publish. Stated in the plan rather than discovered later.
        """
        plan = subject.build_plan(fixture())
        self.assertEqual(plan["mode"], "report_only")
        self.assertEqual(plan["schema"], subject.PLAN_SCHEMA)
        self.assertEqual(
            plan["arming_prerequisite"]["reason"], "SUPPLY_EVIDENCE_IS_RELAY_MINTED"
        )
        for step in plan["steps"]:
            self.assertTrue(step["requires_relay_supply_evidence"])

    def test_no_step_ever_names_a_relay_owned_service_for_deployment(self) -> None:
        for lagging in ((), ("broker",), ("broker", "harness", "canonical-worker")):
            with self.subTest(lagging=lagging):
                plan = subject.build_plan(fixture(lagging=lagging))
                reconciles = [s for s in plan["steps"] if s["kind"] == "reconcile"]
                for step in reconciles:
                    self.assertIn(step["service"], subject.NON_RELAY_SERVICES)


class ArmingTests(unittest.TestCase):
    def test_every_step_carries_the_releases_own_per_service_tag(self) -> None:
        """Regression: steps used to carry an EMPTY image_tag.

        The tag came from a CLI flag the workflow never passed, so every
        constructed dispatch read `image_tag=`. The provider deploys whatever
        tag it is handed, so that is a wrong deploy rather than a failed
        dispatch. The tag is now the supply set's own immutable_lookup_tag, per
        service, which is exactly what the relay dispatches.
        """
        plan = subject.build_plan(fixture(lagging=("broker", "harness")))
        for step in plan["steps"]:
            with self.subTest(step=step["service"]):
                self.assertEqual(
                    step["inputs"]["image_tag"], image_tag(step["service"])
                )
                self.assertTrue(step["inputs"]["image_tag"])
        # The restamp is an app deploy, so it takes the APP tag even though the
        # legs before it were other services.
        restamp = next(s for s in plan["steps"] if s["kind"] == "identity_restamp")
        self.assertEqual(restamp["inputs"]["image_tag"], image_tag("app"))

    def test_a_missing_envelope_makes_the_plan_unarmable(self) -> None:
        """The single fact that decides whether a plan may be dispatched.

        Without the relay's envelope a deploy would mutate staging and produce a
        receipt the finalizer refuses (SERVICE_SUPPLY_EVIDENCE_MISMATCH): all
        cost, no proof. Absent is the ordinary answer for any release converged
        before the relay began publishing it, so it is reported, never raised.
        """
        absent = subject.build_plan(fixture(envelope=False, lagging=("broker",)))
        self.assertFalse(absent["armable"])
        self.assertFalse(absent["supply_evidence"]["present"])
        self.assertTrue(
            any(r.startswith("NO_SUPPLY_EVIDENCE") for r in absent["not_armable_because"]),
            absent["not_armable_because"],
        )
        # Still a perfectly good REPORT: the steps are unchanged.
        self.assertEqual([s["service"] for s in absent["steps"]], ["broker", "app"])

        present = subject.build_plan(fixture(lagging=("broker",)))
        self.assertTrue(present["armable"], present["not_armable_because"])
        self.assertTrue(present["supply_evidence"]["present"])
        self.assertEqual(
            present["supply_evidence"]["artifact_name"],
            f"staging-supply-evidence-{SOURCE}-attempt-1",
        )
        self.assertEqual(present["supply_evidence"]["file"], "staging-supply-evidence.b64")

    def test_both_relay_envelopes_are_required_to_arm(self) -> None:
        """The three v3 dispatch inputs travel together.

        A lane holding only the supply envelope sends digest_aware_reconcile
        with an empty consumer_contract_b64, and the provider refuses it with
        "staging consumer contract refused: digest-aware consumer contract"
        BEFORE any credential is used. Measured on run 33719323168: the deploy
        job was skipped and nothing was mutated, but the leg failed and the
        whole sequence stopped. So both halves gate armability.
        """
        missing_contract = subject.build_plan(fixture(contract=False))
        self.assertFalse(missing_contract["armable"])
        self.assertTrue(
            any(
                r.startswith("NO_CONSUMER_CONTRACT")
                for r in missing_contract["not_armable_because"]
            ),
            missing_contract["not_armable_because"],
        )
        self.assertTrue(missing_contract["supply_evidence"]["present"])
        self.assertFalse(missing_contract["consumer_contract"]["present"])

        both = subject.build_plan(fixture())
        self.assertTrue(both["armable"], both["not_armable_because"])
        self.assertEqual(
            both["consumer_contract"]["artifact_name"],
            f"staging-consumer-contract-{SOURCE}-attempt-1",
        )
        self.assertEqual(
            both["consumer_contract"]["file"], "staging-consumer-contract.b64"
        )

    def test_a_yielded_or_empty_plan_is_never_armable(self) -> None:
        yielded = subject.build_plan(fixture(relay_status="in_progress", lagging=("broker",)))
        self.assertFalse(yielded["armable"])
        self.assertTrue(any(r.startswith("YIELDED") for r in yielded["not_armable_because"]))

        relay_behind = subject.build_plan(fixture(lagging=("web",)))
        self.assertFalse(relay_behind["armable"])
        self.assertIn(
            "RELAY_SURFACES_NOT_CONVERGED", relay_behind["not_armable_because"]
        )

    def test_the_plan_never_carries_the_envelope_body_itself(self) -> None:
        """The armed lane downloads the artifact and passes it through byte for
        byte. Copying 12KB of envelope into the plan would add a re-encoding
        step between the relay and a provider that validates it exactly."""
        plan = subject.build_plan(fixture())
        blob = json.dumps(plan)
        self.assertNotIn("supply_evidence_b64", blob)
        self.assertLess(len(blob), 20000)


class LaneShapeTests(unittest.TestCase):
    """The workflow's own shape. Pinned here because the difference between
    this lane reporting and this lane mutating staging is one string."""

    @staticmethod
    def lane() -> dict:
        path = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "reconcile-staging-fleet.yml"
        )
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_the_schedule_is_armed_for_five_service_convergence(self) -> None:
        """R6 arms the schedule for the operator's five-service staging rule
        after two proven manual armed runs; reverting the flag restores reporting."""
        self.assertEqual(self.lane()["env"]["RECONCILE_ARMED"], "true")

    def test_successful_main_relay_completion_also_triggers_the_plan(self) -> None:
        lane = self.lane()
        triggers = lane[True] if True in lane else lane["on"]
        self.assertEqual(triggers["workflow_run"], {
            "workflows": ["Dispatch staging deploys"],
            "types": ["completed"],
            "branches": ["main"],
        })
        self.assertEqual(triggers["schedule"], [{"cron": "7,37 * * * *"}])
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(
            lane["jobs"]["plan"]["if"],
            "github.event_name != 'workflow_run' || "
            "github.event.workflow_run.conclusion == 'success'",
        )
        self.assertEqual(lane["concurrency"], {
            "group": "reconcile-staging-fleet",
            "cancel-in-progress": False,
        })

    def test_the_diagnostic_upload_is_best_effort_and_short_lived(self) -> None:
        """The plan artifact has no consumer in the repo: every later step
        reads the local file. Its upload sits BEFORE the arm decision and the
        one mutating step, so when the org artifact quota filled on 2026-09-06
        this one diagnostic upload took the whole lane red on every scheduled
        run for four days."""
        steps = self.lane()["jobs"]["plan"]["steps"]
        uploads = [
            s
            for s in steps
            if s.get("uses", "").startswith("actions/upload-artifact")
        ]
        self.assertTrue(uploads)
        for step in uploads:
            self.assertIs(step.get("continue-on-error"), True)
            self.assertLessEqual(int(step["with"]["retention-days"]), 3)

    def test_only_one_step_can_mutate_and_it_is_gated(self) -> None:
        steps = self.lane()["jobs"]["plan"]["steps"]
        infra_token = "${{ secrets.TERRAFORM_REPO_TOKEN }}"
        # The infra token is what makes a step able to dispatch a deploy. The
        # planner holds it to READ the provider; only one step may act with it.
        acting = [
            s
            for s in steps
            if "--execute" in str(s.get("run", ""))
        ]
        self.assertEqual(len(acting), 1, "exactly one step may dispatch")
        step = acting[0]
        self.assertEqual(step["if"], "steps.arm.outputs.act == 'true'")
        self.assertEqual(step["env"]["TERRAFORM_GITHUB_TOKEN"], infra_token)
        # And the decision it is gated on holds NO token of its own.
        decider = next(s for s in steps if s.get("id") == "arm")
        self.assertNotIn(infra_token, str(decider.get("env", {})))
        self.assertNotIn("gh ", str(decider.get("run", "")))

    def test_the_acting_step_uses_the_shared_executor(self) -> None:
        steps = self.lane()["jobs"]["plan"]["steps"]
        acting = next(s for s in steps if "--execute" in str(s.get("run", "")))
        self.assertEqual(acting["if"], "steps.arm.outputs.act == 'true'")
        self.assertEqual(acting["env"], {
            "APP_GITHUB_TOKEN": "${{ github.token }}",
            "TERRAFORM_GITHUB_TOKEN": "${{ secrets.TERRAFORM_REPO_TOKEN }}",
        })
        self.assertIn("--plan-file staging-fleet-reconcile-plan.json", acting["run"])
        self.assertIn("--timeout-seconds 4800", acting["run"])
        self.assertNotIn("gh workflow run", acting["run"])

    def test_the_schedule_acts_only_through_the_same_decider(self) -> None:
        """Scheduled and manual arming share the decider, and neither can act
        unless the plan is armable."""
        lane = self.lane()
        triggers = lane[True] if True in lane else lane["on"]
        self.assertIn("schedule", triggers)
        decider = next(
            s for s in lane["jobs"]["plan"]["steps"] if s.get("id") == "arm"
        )
        self.assertEqual(decider["env"]["ARMED_BY_DEFAULT"], "${{ env.RECONCILE_ARMED }}")
        self.assertEqual(decider["env"]["ARMED_BY_INPUT"], "${{ inputs.arm }}")
        self.assertIn('ARMABLE=$(jq -r \'.armable\' staging-fleet-reconcile-plan.json)', decider["run"])
        self.assertIn('if [ "$WANTED" = "true" ] && [ "$ARMABLE" = "true" ]; then', decider["run"])
        self.assertEqual(decider["run"], """\
set -euo pipefail
ARMABLE=$(jq -r '.armable' staging-fleet-reconcile-plan.json)
WANTED=false
if [ "$ARMED_BY_DEFAULT" = "true" ] || [ "$ARMED_BY_INPUT" = "true" ]; then
  WANTED=true
fi
# Fails CLOSED: anything that is not the exact string "true" on both
# sides leaves this reporting only.
if [ "$WANTED" = "true" ] && [ "$ARMABLE" = "true" ]; then
  echo "act=true" >> "$GITHUB_OUTPUT"
  echo "::notice::Armed and armable; acting on the plan."
else
  echo "act=false" >> "$GITHUB_OUTPUT"
  echo "::notice::Report only (armed=$WANTED, armable=$ARMABLE)."
  jq -r '.not_armable_because[]?' staging-fleet-reconcile-plan.json \\
    | sed 's/^/::notice::blocked by: /'
fi
""")
        acting = next(
            s for s in lane["jobs"]["plan"]["steps"]
            if "--execute" in str(s.get("run", ""))
        )
        self.assertEqual(acting["if"], "steps.arm.outputs.act == 'true'")
        self.assertNotIn("workflow_run", acting["run"])


def frozen_fixture():
    from scripts.platform_release_manifest import build_v3_manifest
    import base64

    entries = {}
    for service in subject.SERVICE_ORDER:
        fingerprint = hashlib.sha256(service.encode()).hexdigest()
        entry = {
            "repository": f"leaf-platform-{service}", "image_digest": release_digests()[service],
            "immutable_lookup_tag": f"surface-v1-{fingerprint}",
            "producer_source_revision": SOURCE, "producer_source_tree": "b" * 40,
            "surface_fingerprint": fingerprint, "recipe_fingerprint": "c" * 64,
            "producer_workflow_path": ".github/workflows/build-platform-images.yml",
            "producer_workflow_blob": "d" * 40, "producer_run_id": 800000,
            "producer_run_attempt": 1,
            "provenance_subject": f"807034087062.dkr.ecr.us-east-1.amazonaws.com/leaf-platform-{service}",
            "provenance_digest": digest(service), "build_disposition": "built",
        }
        if service == "web":
            entry["artifact_sha256"] = "e" * 64
        if service == "canonical-worker":
            entry["solver_provenance"] = {"solver_source_revision": "f" * 40, "solver_source_sha256": "a" * 64}
        entries[service] = entry
    manifest = build_v3_manifest(SOURCE, "b" * 40, "800000", "1", entries)
    supply_raw = canonical(manifest) + b"\n"
    supply_hash = hashlib.sha256(supply_raw).hexdigest()
    evidence = {
        "schema": "leaf.staging-supply-dispatch-evidence.v1",
        "relay": {"repository": APP, "workflow_path": ".github/workflows/dispatch-staging-deploys.yml",
                  "run_id": RELAY_RUN, "run_attempt": 1},
        "producer": {"repository": APP, "workflow_path": ".github/workflows/build-platform-images.yml",
                     "event": "push", "run_id": 800000, "run_attempt": 1,
                     "source_revision": SOURCE, "source_tree": "b" * 40},
        "manifest": {"schema": manifest["schema"], "sha256": supply_hash, "source_revision": SOURCE,
                     "source_tree": "b" * 40, "json_b64": base64.urlsafe_b64encode(supply_raw).rstrip(b"=").decode()},
        "supply_artifact": {"id": 1000, "name": "supply", "provider_archive_sha256": "a" * 64},
    }
    body = {
        "schema": subject.convergence.CONSUMER_CONTRACT_SCHEMA, "version": 1,
        "consumer": {"deploy_workflow_path": subject.DEPLOY_WORKFLOW, "deploy_workflow_blob": "d" * 40,
                     "contract_schema_path": subject.convergence.CONSUMER_CONTRACT_SCHEMA_PATH,
                     "contract_schema_blob": "e" * 40, "contract_version": 1,
                     "pins": {"deployment_environment": "aws-apply", "digest_aware_marker": "leaf.staging-digest-aware-consumer.v1",
                              "mutation_group": "leaf-platform-staging-ecs-mutation"}},
        "producer": {"repository": TF, "workflow_path": subject.convergence.CONSUMER_CONTRACT_WORKFLOW,
                     "workflow_blob": "f" * 40, "event": "push", "branch": "main",
                     "head_sha": "d" * 40, "head_tree": "b" * 40, "run_id": 123456, "run_attempt": 1},
        "artifact": {"file": "consumer-contract.json", "name": "contract"},
    }
    body["payload_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    contract = {"schema": subject.convergence.CONSUMER_DISPATCH_SCHEMA, "contract": body,
                "artifact": {"id": 2000, "name": "contract", "producer_run_id": 123456, "producer_run_attempt": 1,
                             "provider_sha256": "a" * 64, "archive_sha256": "a" * 64, "file_sha256": "b" * 64}}
    contract["envelope_sha256"] = hashlib.sha256(canonical(contract)).hexdigest()
    encode = lambda value: base64.urlsafe_b64encode(canonical(value)).rstrip(b"=")
    receipt = {
        "schema": "leaf.staging-converged.v2", "release_source_revision": SOURCE,
        "build_run_attempt": 1, "relay_run_id": RELAY_RUN,
        "supply_set_sha256": supply_hash, "candidate_supply_set": manifest,
        "automatic_surfaces": ["web", "app"], "full_fleet_identity_stamped": False,
        "non_relay_services": {s: "not_automatically_reconciled" for s in subject.NON_RELAY_SERVICES},
        "surface_results": {
            s: {"schema": "leaf.staging-surface-result.v1", "service": s,
                "release_source_revision": SOURCE, "convergence_id": f"{SOURCE}-1-{s}",
                "candidate_image_digest": release_digests()[s], "terminal_image_digest": release_digests()[s],
                "terraform_workflow_blob": "d" * 40, "surface_receipt_sha256": "e" * 64,
                "outcome": "deployed", "aws_mutation_count": 1}
            for s in subject.RELAY_SERVICES
        },
    }
    return receipt, supply_raw, encode(evidence), encode(contract)


def frozen_context():
    return subject.bind_relay_inputs(RELAY_RUN, *frozen_fixture(), check_current_run=False)


class RecordingExecutor:
    def __init__(self, failure=None, stage="watch"):
        self.events = []
        self.failure, self.stage = failure, stage

    def clock(self):
        return 0

    def dispatch(self, step, context, deadline):
        service = step["service"]
        self.events.append(("dispatch", service, copy.deepcopy(step["inputs"]),
                            context["evidence"], context["contract"]))
        if service == self.failure and self.stage == "dispatch":
            raise subject.ContractError("LEG_RUN_UNRESOLVED")
        return 10000 + len(self.events)

    def watch(self, run_id, deadline):
        self.events.append(("watch", run_id))
        if self.events[-2][1] == self.failure and self.stage == "watch":
            raise subject.ContractError("LEG_RUN_UNSUCCESSFUL")


class RelayExecutionTests(unittest.TestCase):
    def test_frozen_binding_rejects_every_cross_release_input(self):
        import base64
        original = frozen_fixture()
        for part in ("run", "receipt", "supply", "evidence", "contract", "surface"):
            receipt, supply, evidence, contract = copy.deepcopy(original)
            run_id = RELAY_RUN
            if part == "run":
                run_id += 1
            elif part == "receipt":
                receipt["supply_set_sha256"] = "0" * 64
            elif part == "supply":
                supply += b" "
            elif part == "evidence":
                decoded = subject._decode_envelope(evidence)
                decoded["relay"]["run_id"] += 1
                evidence = base64.urlsafe_b64encode(canonical(decoded)).rstrip(b"=")
            elif part == "contract":
                decoded = subject._decode_envelope(contract)
                decoded["contract"]["consumer"]["deploy_workflow_blob"] = "0" * 40
                contract = base64.urlsafe_b64encode(canonical(decoded)).rstrip(b"=")
            else:
                receipt["surface_results"]["web"]["terminal_image_digest"] = digest("wrong")
            with self.subTest(part=part), self.assertRaises(subject.ContractError):
                subject.bind_relay_inputs(run_id, receipt, supply, evidence, contract, check_current_run=False)

    def test_self_and_queued_successor_do_not_interrupt_but_other_activity_does(self):
        provider = fixture(relay_status="in_progress")
        endpoint = (APP, runs_endpoint(RELAY_WF, "per_page=50"))
        provider.json_values[endpoint]["workflow_runs"].append(run_row(RELAY_RUN + 1, status="queued"))
        self.assertEqual(subject.yield_check(provider, RELAY_RUN)["status"], "clear")
        self.assertEqual(subject.yield_check(provider)["reason"], "RELAY_LIVE")
        for status in ("in_progress", "queued"):
            provider.json_values[endpoint]["workflow_runs"] = [
                run_row(RELAY_RUN, status="in_progress"), run_row(RELAY_RUN - 1, status=status),
            ]
            self.assertEqual(subject.yield_check(provider, RELAY_RUN)["reason"], "RELAY_LIVE")
        provider.json_values[endpoint]["workflow_runs"] = [run_row(RELAY_RUN, status="in_progress")]
        provider.json_values[(TF, runs_endpoint(DEPLOY_WF, "per_page=50"))]["workflow_runs"][0]["status"] = "in_progress"
        self.assertEqual(subject.yield_check(provider, RELAY_RUN)["reason"], "STAGING_DEPLOY_LIVE")

    def test_explicit_release_never_rediscovers_or_reads_relay_artifacts(self):
        provider = fixture(relay_status="in_progress", lagging=subject.NON_RELAY_SERVICES)
        context = frozen_context()
        plan = subject.build_relay_plan(provider, context)
        self.assertTrue(plan["armable"], plan["not_armable_because"])
        self.assertEqual(plan["release"]["build_run_id"], 800000)
        self.assertEqual([s["service"] for s in plan["steps"]], ["broker", "harness", "canonical-worker", "app"])
        self.assertFalse(any(repo == APP and ("artifacts" in endpoint or "status=success" in endpoint)
                             for _, repo, endpoint in provider.calls))

    def test_sequential_legs_then_restamp_pass_envelopes_unchanged_and_never_rollback(self):
        from unittest.mock import patch
        provider = fixture(relay_status="in_progress", lagging=subject.NON_RELAY_SERVICES)
        context = frozen_context()
        plan = subject.build_relay_plan(provider, context)
        executor = RecordingExecutor()
        def valid(provider, run_id, step, context):
            return {"run_id": run_id, "service": step["service"], "kind": step["kind"]}
        with patch.object(subject, "validate_leg", side_effect=valid):
            result = subject.execute_plan(provider, plan, context, relay_run_id=RELAY_RUN, executor=executor)
        self.assertEqual(result["status"], "success")
        self.assertEqual([e[0] for e in executor.events], ["dispatch", "watch"] * 4)
        dispatches = executor.events[::2]
        self.assertEqual([e[1] for e in dispatches], ["broker", "harness", "canonical-worker", "app"])
        for event in dispatches:
            self.assertEqual(event[3:], (context["evidence"], context["contract"]))
            self.assertNotEqual(event[2]["expected_task_definition"], "auto-live")
            self.assertEqual(event[2]["deploy_strategy"], "direct")
            self.assertNotEqual(event[2]["app_deploy_intent"], "rollback")
        self.assertEqual(dispatches[-1][2]["app_deploy_intent"], "configuration")
        self.assertEqual(result["restamp_frontier"]["service"], "app")

    def test_each_failure_boundary_stops_all_later_legs(self):
        from unittest.mock import patch
        for index, service in enumerate(("broker", "harness", "canonical-worker", "app")):
            for stage in ("dispatch", "watch", "receipt", "baseline"):
                with self.subTest(service=service, stage=stage):
                    provider = fixture(relay_status="in_progress", lagging=subject.NON_RELAY_SERVICES)
                    context = frozen_context()
                    plan = subject.build_relay_plan(provider, context)
                    executor = RecordingExecutor(service, stage)
                    settled = subject._settled_service_state(provider)
                    calls = 0
                    def state(provider):
                        nonlocal calls
                        calls += 1
                        value = copy.deepcopy(settled)
                        if stage == "baseline" and calls == index + 1:
                            value[service]["task_definition"] = f"{ACCOUNT}/changed:1"
                        return value
                    def validate(provider, run_id, step, context):
                        if stage == "receipt" and step["service"] == service:
                            raise subject.ContractError("SERVICE_RECEIPT_BINDING_MISMATCH")
                        return {"run_id": run_id, "service": step["service"], "kind": step["kind"]}
                    with patch.object(subject, "_settled_service_state", side_effect=state), \
                         patch.object(subject, "validate_leg", side_effect=validate):
                        result = subject.execute_plan(provider, plan, context, relay_run_id=RELAY_RUN, executor=executor)
                    self.assertEqual(result["status"], "failed")
                    self.assertIn(service, result["failed_leg"])
                    self.assertEqual(len(result["children"]), index)
                    self.assertIsNone(result["restamp_frontier"])
                    dispatches = [e for e in executor.events if e[0] == "dispatch"]
                    self.assertEqual(len(dispatches), index + (stage != "baseline"))
                    if stage in ("watch", "receipt"):
                        self.assertIsNotNone(result["failed_run_id"])

    def test_scheduled_wrapper_keeps_the_same_plan(self):
        provider = fixture(lagging=("broker",))
        plan = subject.build_plan(provider)
        release = subject._newest_relay_release(provider)
        shared = subject.plan_release(release, subject._settled_service_state(provider),
                                      subject.yield_check(provider), plan["supply_evidence"], plan["consumer_contract"])
        self.assertEqual(plan, shared)

    def test_busy_timeout_and_rollback_plan_stop_before_any_dispatch(self):
        for failure in ("busy", "timeout", "rollback"):
            with self.subTest(failure=failure):
                provider = fixture(relay_status="in_progress", lagging=subject.NON_RELAY_SERVICES)
                context = frozen_context()
                plan = subject.build_relay_plan(provider, context)
                executor = RecordingExecutor()
                if failure == "busy":
                    provider.json_values[(TF, runs_endpoint(DEPLOY_WF, "per_page=50"))]["workflow_runs"][0]["status"] = "in_progress"
                elif failure == "rollback":
                    plan["steps"][0]["inputs"]["app_deploy_intent"] = "rollback"
                result = subject.execute_plan(provider, plan, context, relay_run_id=RELAY_RUN,
                                              executor=executor, timeout_seconds=0 if failure == "timeout" else 4800)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["children"], [])
                self.assertIsNone(result["restamp_frontier"])
                self.assertEqual(executor.events, [])


def landed_receipt(context, step, run_id):
    service = step["service"]
    predecessor = step["inputs"]["expected_task_definition"]
    terminal_td = f"{ACCOUNT}/leaf-platform-{service}:763"
    with_identity = step["kind"] == "identity_restamp"
    produced = lambda value: {"status": "produced", "value": value}
    missing = lambda: {"status": "not_produced"}
    contract_slot = subject._evidence_slot(context["contract"])
    requested = {
        "allow_non_forward_image": missing(),
        "app_deploy_intent": "configuration" if with_identity else "forward",
        "configuration_delta": missing(),
        "configuration_task_definition": predecessor if with_identity else "not_produced",
        "convergence_id": "not_produced",
        "deploy_mode": "normal",
        "consumer_contract": contract_slot,
        "deploy_strategy": "direct",
        "digest_aware_evidence": missing(),
        "digest_aware_reconcile": False,
        "expected_task_definition": predecessor,
        "hold_seconds": "0",
        "image_tag": context["release"]["service_tags"][service],
        "p4a_session_identity_cutover": missing(),
        "quarantine_recovery_snapshot_identifier": missing(),
        "required_broker_task_definition": "not_produced",
        "service": service,
        "snapshot_overflow_acknowledgement": missing(),
        "source_revision": SOURCE,
        "start_from_zero": False,
        "start_from_zero_confirmation": missing(),
        "supply_evidence": subject._evidence_slot(context["evidence"]),
        "target_color": "live",
    }
    facts = {
        "schema": "leaf.platform-staging-service-facts.v1",
        "service": produced(service),
        "source": {"revision": produced(SOURCE), "tree": produced("b" * 40)},
        "supply": produced(
            {
                "artifact_id": 1000,
                "artifact_name": context["supply_artifact"]["name"],
                "manifest_sha256": context["supply"]["manifest_sha256"],
                "producer_run_id": context["producer"]["run_id"],
                "producer_run_attempt": 1,
            }
        ),
        "predecessor_task_definition": produced(predecessor),
        "candidate": {
            "task_definition": produced(terminal_td),
            "image_digest": produced(context["supply"]["service_digests"][service]),
        },
        "terminal": produced(
            {
                "service": f"leaf-platform-{service}",
                "task_definition": terminal_td,
                "image_digest": produced(context["supply"]["service_digests"][service]),
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
        "deployment_identity": missing(),
        "marker": missing(),
        "writer_census": missing(),
    }
    value = {
        "schema": "leaf.platform-staging-service-run.v1",
        "environment": "staging",
        "provider": {
            "repository": subject.TF_REPOSITORY,
            "workflow_path": subject.DEPLOY_WORKFLOW,
            "workflow_blob": context["workflow_blob"],
            "run_id": run_id,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "head_sha": "d" * 40,
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
    if with_identity:
        body = {
            "schema": "leaf.deployment-identity.v1", "environment": "staging", "source_revision": SOURCE,
            "services": {s: {"image_digest": context["supply"]["service_digests"][s], "source_revision": SOURCE}
                         for s in subject.SERVICE_ORDER},
        }
        raw = (json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode()
        facts["deployment_identity"] = produced({"body": body, "sha256": hashlib.sha256(raw).hexdigest()})
    value["receipt_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
    return value



class ChildReceiptTests(unittest.TestCase):
    def setup_leg(self, service="broker", restamp=False):
        context = frozen_context()
        provider = FakeProvider()
        baseline = f"{ACCOUNT}/leaf-platform-{service}:762"
        step = (subject._restamp_step(baseline, image_tag(service), position=1) if restamp else
                subject._dispatch_step(service, image_tag(service), baseline, position=1))
        run_id = 10001
        receipt = landed_receipt(context, step, run_id)
        row = run_row(run_id, head_sha="d" * 40, service=service)
        row.update(event="workflow_dispatch", head_branch="main", path=subject.DEPLOY_WORKFLOW)
        provider.json_values[(TF, f"/actions/runs/{run_id}")] = row
        name = f"leaf-platform-staging-service-run-{run_id}-attempt-1"
        provider.json_values[(TF, f"/actions/runs/{run_id}/artifacts?per_page=100")] = {
            "total_count": 1, "artifacts": [artifact_row(11001, name, run_id)],
        }
        provider.json_values[(TF, f"/actions/runs/{run_id}/attempts/1/jobs?per_page=100")] = {
            "total_count": 1, "jobs": [{"name": "Deploy", "steps": [{"name": "Promote", "number": 1, "conclusion": "success"}]}],
        }
        provider.byte_values[(TF, "/actions/artifacts/11001/zip")] = archive(subject.ARTIFACT_FILE, canonical(receipt))
        return provider, context, step, receipt, run_id

    def test_landed_child_and_final_identity_are_validated(self):
        for service, restamp in (("broker", False), ("app", True)):
            with self.subTest(service=service):
                provider, context, step, receipt, run_id = self.setup_leg(service, restamp)
                child = subject.validate_leg(provider, run_id, step, context)
                self.assertEqual(child["run_id"], run_id)
                if restamp:
                    self.assertEqual(child["identity"]["body"]["services"]["web"]["image_digest"],
                                     context["supply"]["service_digests"]["web"])

    def test_green_run_with_invalid_receipt_is_refused(self):
        for field in ("checksum", "baseline", "supply", "envelope", "contract", "run", "blob", "digest", "health", "identity", "rollback"):
            with self.subTest(field=field):
                provider, context, step, receipt, run_id = self.setup_leg("app", True)
                if field == "checksum":
                    receipt["receipt_sha256"] = "0" * 64
                elif field == "baseline":
                    receipt["facts"]["predecessor_task_definition"]["value"] = f"{ACCOUNT}/changed:1"
                elif field == "supply":
                    receipt["facts"]["supply"]["value"]["manifest_sha256"] = "0" * 64
                elif field == "envelope":
                    receipt["requested"]["supply_evidence"]["sha256"] = "0" * 64
                elif field == "contract":
                    receipt["requested"]["consumer_contract"]["sha256"] = "0" * 64
                elif field == "run":
                    receipt["provider"]["run_id"] += 1
                elif field == "blob":
                    receipt["provider"]["workflow_blob"] = "0" * 40
                elif field == "digest":
                    receipt["facts"]["terminal"]["value"]["image_digest"]["value"] = digest("wrong")
                elif field == "health":
                    receipt["facts"]["terminal"]["value"]["capacity"]["running"] = 0
                elif field == "identity":
                    receipt["facts"]["deployment_identity"]["value"]["body"]["services"]["web"]["image_digest"] = digest("wrong")
                else:
                    receipt["requested"]["app_deploy_intent"] = "rollback"
                if field != "checksum":
                    receipt["receipt_sha256"] = ""
                    receipt["receipt_sha256"] = hashlib.sha256(canonical(receipt)).hexdigest()
                provider.byte_values[(TF, "/actions/artifacts/11001/zip")] = archive(subject.ARTIFACT_FILE, canonical(receipt))
                with self.assertRaises(subject.ContractError):
                    subject.validate_leg(provider, run_id, step, context)


class DispatchBindingTests(unittest.TestCase):
    def test_post_carries_frozen_bytes_and_resolves_only_the_matching_child(self):
        from unittest.mock import patch
        context = frozen_context()
        provider = FakeProvider()
        endpoint = (TF, runs_endpoint(DEPLOY_WF, "per_page=20"))
        provider.json_values[endpoint] = {"workflow_runs": [run_row(100)]}
        step = subject._dispatch_step("broker", image_tag("broker"), f"{ACCOUNT}/leaf-platform-broker:762", position=1)
        posted = []
        class Response:
            status = 204
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        def post(request, timeout):
            posted.append(json.loads(request.data))
            matching = run_row(1001, service="broker")
            matching.update(event="workflow_dispatch",
                            display_title=f"Deploy leaf-platform staging broker ({image_tag('broker')})")
            unrelated = run_row(1002, service="harness")
            unrelated["event"] = "workflow_dispatch"
            provider.json_values[endpoint] = {"workflow_runs": [unrelated, matching]}
            return Response()
        executor = subject.WorkflowExecutor(provider, clock=lambda: 0, sleep=lambda seconds: None)
        with patch.dict(subject.os.environ, {"TERRAFORM_GITHUB_TOKEN": "fixture-token"}), \
             patch.object(subject.urllib.request, "urlopen", side_effect=post):
            self.assertEqual(executor.dispatch(step, context, 4800), 1001)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["ref"], "main")
        inputs = posted[0]["inputs"]
        self.assertEqual(inputs["supply_evidence_b64"].encode(), context["evidence"])
        self.assertEqual(inputs["consumer_contract_b64"].encode(), context["contract"])
        self.assertEqual(inputs["digest_aware_reconcile"], "false")
        self.assertEqual(inputs["expected_task_definition"], step["inputs"]["expected_task_definition"])
        self.assertEqual(inputs["deploy_strategy"], "direct")


class ValidationTests(unittest.TestCase):
    def test_malformed_provider_values_fail_closed(self) -> None:
        for bad in ("", "sha256:zz", "notadigest", None, 7, ["sha256:" + "a" * 64]):
            with self.subTest(bad=bad):
                with self.assertRaises(subject.ContractError):
                    subject._digest(bad, "X")
        for bad in ("", "leaf-platform-app:762", f"{ACCOUNT}/app:0", None, 7, {}):
            with self.subTest(bad=bad):
                with self.assertRaises(subject.ContractError):
                    subject._task_definition(bad, "X")

    def test_a_release_with_no_relay_receipt_is_named_not_assumed(self) -> None:
        provider = fixture()
        provider.json_values[(APP, f"/actions/runs/{RELAY_RUN}/artifacts?per_page=100")] = {
            "total_count": 0,
            "artifacts": [],
        }
        with self.assertRaisesRegex(subject.ContractError, "NO_CONVERGED_RELEASE"):
            subject.build_plan(provider)

    def test_summary_renders_every_service_and_blocker(self) -> None:
        plan = subject.build_plan(fixture(lagging=("broker",)))
        text = subject._render_summary(plan)
        for service in subject.SERVICE_ORDER:
            self.assertIn(service, text)
        for blocker in plan["blockers"]:
            self.assertIn(blocker.split(":")[0], text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
