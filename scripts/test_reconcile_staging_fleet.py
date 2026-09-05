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
        self.byte_values: dict[tuple[str, str], bytes] = {}

    def json(self, repository: str, endpoint: str) -> object:
        key = (repository, endpoint)
        if key not in self.json_values:
            raise subject.ContractError("PROVIDER_FIXTURE_MISSING")
        return copy.deepcopy(self.json_values[key])

    def bytes(self, repository: str, endpoint: str) -> bytes:
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

    def test_reconcile_steps_always_carry_digest_aware_reconcile(self) -> None:
        """The settled-state read is best effort, so the safety comes from the
        provider re-reading live state under the lock and skipping a service
        that is already exact."""
        plan = subject.build_plan(fixture(lagging=("broker",)))
        step = next(s for s in plan["steps"] if s["kind"] == "reconcile")
        self.assertEqual(step["inputs"]["digest_aware_reconcile"], "true")
        self.assertEqual(step["inputs"]["app_deploy_intent"], "forward")
        self.assertEqual(step["inputs"]["expected_task_definition"], "auto-live")

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

    def test_the_lane_ships_dormant(self) -> None:
        """Source-controlled and off, the same idiom as the build workflow's
        DIGEST_AWARE_CONVERGENCE_ENABLED. Arming the SCHEDULE must be its own
        one-line change, not something that rides along inside another edit."""
        self.assertEqual(self.lane()["env"]["RECONCILE_ARMED"], "false")

    def test_only_one_step_can_mutate_and_it_is_gated(self) -> None:
        steps = self.lane()["jobs"]["plan"]["steps"]
        infra_token = "${{ secrets.TERRAFORM_REPO_TOKEN }}"
        # The infra token is what makes a step able to dispatch a deploy. The
        # planner holds it to READ the provider; only one step may act with it.
        acting = [
            s
            for s in steps
            if "gh workflow run" in str(s.get("run", ""))
        ]
        self.assertEqual(len(acting), 1, "exactly one step may dispatch")
        step = acting[0]
        self.assertEqual(step["if"], "steps.arm.outputs.act == 'true'")
        self.assertEqual(step["env"]["GH_TOKEN"], infra_token)
        # And the decision it is gated on holds NO token of its own.
        decider = next(s for s in steps if s.get("id") == "arm")
        self.assertNotIn(infra_token, str(decider.get("env", {})))
        self.assertNotIn("gh ", str(decider.get("run", "")))

    def test_the_acting_step_rechecks_the_lock_and_watches_each_leg(self) -> None:
        """The plan's yield is minutes old by the time a leg dispatches, so the
        relay may have started since. It owns the product surfaces and wins."""
        steps = self.lane()["jobs"]["plan"]["steps"]
        code = next(s["run"] for s in steps if "gh workflow run" in str(s.get("run", "")))
        self.assertIn("staging_is_busy", code)
        self.assertIn("--check-idle", code)
        # Re-checked INSIDE the per-leg loop, not once before it.
        self.assertLess(code.index("for i in $(seq"), code.index("if staging_is_busy"))
        # Each leg is watched to a terminal state and anything but success stops
        # the sequence before the restamp.
        self.assertIn('if [ "$CONCLUSION" != "success" ]', code)
        # The envelope is passed through, never rebuilt.
        self.assertIn("staging-supply-evidence.b64", code)
        self.assertIn('-f "supply_evidence_b64=$EVIDENCE"', code)
        self.assertIn('-f "consumer_contract_b64=$CONTRACT"', code)
        self.assertIn("staging-consumer-contract.b64", code)

    def test_the_schedule_cannot_act_while_the_flag_is_false(self) -> None:
        """The scheduled trigger has no way to set the manual arm input, so a
        dormant flag means every scheduled run reports and nothing else."""
        lane = self.lane()
        triggers = lane[True] if True in lane else lane["on"]
        self.assertIn("schedule", triggers)
        self.assertEqual(lane["env"]["RECONCILE_ARMED"], "false")
        decider = next(
            s for s in lane["jobs"]["plan"]["steps"] if s.get("id") == "arm"
        )
        self.assertEqual(decider["env"]["ARMED_BY_DEFAULT"], "${{ env.RECONCILE_ARMED }}")
        self.assertEqual(decider["env"]["ARMED_BY_INPUT"], "${{ inputs.arm }}")


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
