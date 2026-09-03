"""Shape contract for .github/workflows/converge-staging-fleet.yml.

This workflow dispatches REAL staging deploys into another repository, so the
properties that keep it safe are pinned here rather than left to review memory:
it defaults to a dry run, it never touches the two surfaces the relay owns, it
identifies the runs it watches by exact name, and it passes a reviewed task
definition instead of the auto-live shorthand the deploy refuses for these
services.
"""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/converge-staging-fleet.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)
JOB = DOC["jobs"]["converge"]
RUN = "\n".join(step["run"] for step in JOB["steps"] if "run" in step)


def test_is_manual_and_defaults_to_a_dry_run() -> None:
    trigger = DOC.get("on", DOC.get(True))
    assert set(trigger) == {"workflow_dispatch"}
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"producer_build_run_id", "dry_run"}
    assert inputs["producer_build_run_id"]["type"] == "string"
    assert inputs["producer_build_run_id"]["required"] is True
    # The default is the whole safety story: a fresh dispatch resolves and
    # prints, and cannot mutate staging until someone deliberately turns it off.
    assert inputs["dry_run"]["type"] == "boolean"
    assert inputs["dry_run"]["default"] is False or inputs["dry_run"]["default"] is True
    assert inputs["dry_run"]["default"] is True


def test_dry_run_is_the_fail_safe_direction() -> None:
    # Every dispatch site must be guarded by an explicit "not false" test, so a
    # missing, empty or malformed input reads as a DRY RUN rather than as live.
    assert 'if [ "$DRY_RUN" != "false" ]; then' in RUN
    assert 'DRY_RUN: ${{ inputs.dry_run }}' in TEXT


def test_read_only_permissions_and_no_cloud_credentials() -> None:
    assert DOC["permissions"] == {"actions": "read", "contents": "read"}
    assert "id-token" not in TEXT
    assert "aws-actions" not in TEXT
    assert "AWS_ACCESS_KEY" not in TEXT
    # Identity is built by the deploy from the supply manifest. If this file
    # ever computes one, the five-service body would have two authors.
    assert "IDENTITY_BODY_B64" not in TEXT
    assert "IDENTITY_SHA256" not in TEXT


def test_serializes_against_itself() -> None:
    assert DOC["concurrency"]["group"] == "leaf-platform-staging-fleet-convergence"
    assert DOC["concurrency"]["cancel-in-progress"] is False


def test_reconciles_only_the_services_the_relay_leaves_behind() -> None:
    # web and app are the relay's automatic surfaces. Deploying them here would
    # race the relay for the same staging mutation lock on the same release.
    assert JOB["env"]["TAIL_SERVICES"] == "broker harness canonical-worker"
    assert "web" not in JOB["env"]["TAIL_SERVICES"].split()
    assert "app" not in JOB["env"]["TAIL_SERVICES"].split()


def test_the_identity_stamp_is_a_configuration_deploy_and_runs_last() -> None:
    # Only app_deploy_intent=configuration produces deployment_identity, which
    # is the evidence the convergence receipt reads off the app frontier.
    assert 'dispatch_and_watch "app" "configuration"' in RUN
    tail_loop = RUN.index("for SERVICE in $TAIL_SERVICES; do")
    restamp = RUN.index('dispatch_and_watch "app" "configuration"')
    assert tail_loop < restamp, "identity must be stamped after the fleet is on its images"


def test_runs_are_bound_by_exact_name_never_by_newest() -> None:
    # Sibling lanes dispatch this same infra workflow, and GitHub lists
    # dispatched runs asynchronously, so "the newest run that appeared" can be
    # someone else's. The relay learned this the hard way; so does this file.
    assert "find_run_named" in RUN
    assert '.displayTitle == $want' in RUN
    assert 'WANT="Deploy leaf-platform staging $SERVICE ($IMAGE_TAG)"' in RUN


def test_passes_a_reviewed_task_definition_rather_than_auto_live() -> None:
    # auto-live is restricted by the deploy to web and app AND to
    # app_deploy_intent=forward, so it is unavailable for every dispatch this
    # workflow makes. Passing an explicit baseline also means a stale guess is
    # refused ("Live task definition changed after review") instead of
    # deploying against an unreviewed one.
    assert "auto-live" not in TEXT
    assert '-f "expected_task_definition=$EXPECTED_TD"' in RUN
    assert "latest_terminal_of" in RUN


def test_requires_a_strict_v3_supply_set_and_a_converged_relay() -> None:
    assert 'leaf.staging-supply-set.v3' in RUN
    assert 'RELAY_NAME="staging-converged-$HEAD_SHA-attempt-$ATTEMPT"' in RUN
    assert "No successful relay published" in RUN


def test_every_dispatch_failure_stops_the_sequence() -> None:
    # A half-reconciled fleet must not go on to stamp an identity that claims
    # digests which never landed.
    assert 'nothing after this point was dispatched' in RUN
    for step in JOB["steps"]:
        if "run" in step:
            assert step["run"].lstrip().startswith("set -euo pipefail")
