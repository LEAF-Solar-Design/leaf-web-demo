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
            "services": {s: {"image_digest": d} for s, d in digests.items()},
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
    provider.json_values[(APP, f"/actions/runs/{RELAY_RUN}/artifacts?per_page=100")] = {
        "total_count": 1,
        "artifacts": [artifact_row(2000, relay_name, RELAY_RUN)],
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
