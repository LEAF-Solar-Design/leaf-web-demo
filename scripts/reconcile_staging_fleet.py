"""Resolve the staging fleet reconcile-and-restamp plan. READ ONLY.

WHY THIS EXISTS. The staging relay converges exactly two surfaces, web and
app, and says so in its own receipt: automatic_surfaces ["web","app"],
non_relay_services all "not_automatically_reconciled",
full_fleet_identity_stamped false. The convergence receipt that
scripts/platform_staging_convergence.py emits needs more than that. Its
deployment identity is a FIVE-service stamp built from LIVE ECS digests (the
provider samples running tasks and checks their health before writing the
body), so it exists only once broker, harness and canonical-worker are also
live on the release, and only after an app run with
app_deploy_intent=configuration stamps it. Nothing dispatches any of that:
the relay only ever sends app_deploy_intent=forward. Measured 2026-09-03 on
the dad27a10 wave, every one of those four runs (broker 33698179237, harness
33698719439, canonical-worker 33699214170, restamp 33699604872) was
triggering_actor=Evan-Haug, event=workflow_dispatch. This module computes, on
evidence alone, what those four dispatches should be.

WHY IT PLANS INSTEAD OF DEPLOYING, and why the plan is not per-release.
Every staging mutation funnels through one shared concurrency group
(leaf-platform-staging-ecs-mutation, shared by 39 workflows) which holds a
single slot. Measured over the 60 most recent staging deploys to 2026-09-03,
median wall clock per service: web 11.2, app 26.9, broker 5.4, harness 6.0,
canonical-worker 7.8 minutes, so all five serialized is ~57 minutes, ~63 with
the restamp, against a 19.4-minute median merge cadence on main and a lock
already busy 70.3% of a 25.2h sample. Converging EVERY release is therefore
3.2x oversubscribed: a per-release reconciler would queue forever and starve
the web and app deploys that carry the product. So the plan always targets the
NEWEST converged release and is expected to skip releases, and the lane that
runs it yields to the relay rather than competing with it.

This module NEVER dispatches and never mutates. It reads provider evidence and
returns a closed plan for a human to read, which is the milestone that comes
before arming anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

from scripts.platform_staging_convergence import (
    APP_REPOSITORY,
    ARTIFACT_FILE,
    DEPLOY_WORKFLOW,
    SERVICE_ORDER,
    TF_REPOSITORY,
    ContractError,
    GitHubProvider,
    Provider,
    _artifact_rows,
    _one_artifact,
    _positive,
    _sha40,
)

PLAN_SCHEMA = "leaf.staging-fleet-reconcile-plan.v1"
ENVIRONMENT = "staging"
RELAY_ARTIFACT_FILE = "staging-converged.json"

# The relay owns these two and reconciles them itself; this lane must never
# name them in a dispatch step or it would race the relay for the lock.
RELAY_SERVICES = ("web", "app")
# Dispatched in this order, cheapest first (medians above), so a lane that
# loses the lock partway has still advanced the most services it could.
NON_RELAY_SERVICES = ("broker", "harness", "canonical-worker")

# Bounded scan. The reconciler reads recent deploy runs to find each service's
# most recent settled state; it never walks the whole history.
MAX_DEPLOY_RUN_SCAN = 60
MAX_RELAY_RUN_SCAN = 20
# Hard ceiling on rows accepted from one listing, so a provider that ignores
# per_page cannot make this lane allocate without bound.
MAX_RUN_PAGE = 100

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# The producer's immutable lookup tag, e.g. surface-v1-<64 hex>.
IMAGE_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}$")
TASK_DEFINITION_ARN = re.compile(
    r"^arn:aws:ecs:[a-z0-9-]{1,32}:[0-9]{12}:task-definition/[A-Za-z0-9_-]{1,255}:[1-9][0-9]{0,9}$"
)

# Live-run statuses that mean the shared staging mutation lock is, or is about
# to be, held by somebody else.
BUSY_STATUSES = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
# GitHub refuses to start workflow files larger than 500 KB. Use the larger
# binary interpretation so the exception never admits an ambiguous boundary.
# https://docs.github.com/en/actions/reference/limits#workflow-file-size
MAX_WORKFLOW_BYTES = 500 * 1024

# The provider sets run-name to "Deploy leaf-platform staging <service>
# (<image_tag>)". The relay already depends on that contract to identify its
# own dispatched runs, so this reuses it as a CHEAP PREFILTER: it narrows
# which runs are worth an artifact download, and the receipt inside is still
# the authority. A title that lies costs a discarded candidate, never a wrong
# reading, because the receipt's own requested.service has to agree.
RUN_TITLE = re.compile(r"^Deploy leaf-platform staging (?P<service>[a-z-]{1,32}) \(")


def _digest(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ContractError(reason)
    return value


def _image_tag(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not IMAGE_TAG.fullmatch(value):
        raise ContractError(reason)
    return value


def _task_definition(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not TASK_DEFINITION_ARN.fullmatch(value):
        raise ContractError(reason)
    return value


def _run_page(
    provider: Provider, repository: str, workflow: str, query: str
) -> list[dict[str, Any]]:
    """ONE bounded snapshot of a workflow's run list.

    Deliberately not the convergence finalizer's _workflow_run_rows. That helper
    re-reads until two scans agree, which is right for a receipt bound to a
    CLOSED window, and impossible here: this lane reads an open-ended listing of
    a workflow that has a run live 70.3% of the time, so on a full page any new
    run pushes one off the end and the scans can never agree. Measured against
    the live provider on 2026-09-03, that is exactly what happened:
    ERROR:PROVIDER_RUN_LIST_DRIFT, every time.

    A single snapshot is the honest primitive for a REPORT. It is also what the
    provider's own prewarm self-yield uses for the same decision. Staleness is
    bounded by the read itself and costs at worst one skipped cycle, and the
    schedule brings the lane straight back.
    """
    raw = provider.json(repository, f"/actions/workflows/{workflow}/runs?{query}")
    if not isinstance(raw, dict) or not isinstance(raw.get("workflow_runs"), list):
        raise ContractError("PROVIDER_RUN_LIST_INVALID")
    rows = raw["workflow_runs"]
    if len(rows) > MAX_RUN_PAGE:
        raise ContractError("PROVIDER_RUN_LIST_INVALID")
    return rows


def _unstartable_dispatch(
    provider: Provider, repository: str, workflow: str, row: dict[str, Any]
) -> bool:
    """Prove the oversize/no-jobs incident, never infer it from a run's age.

    GitHub can retain a queued dispatch record even when its immutable workflow
    cannot start. On 2026-09-04 six such records blocked every reconcile forever.
    A real job or unreadable evidence still owns the staging lane.
    """
    path = f".github/workflows/{workflow}"
    revision = row.get("head_sha")
    if (
        row.get("status") != "queued"
        or row.get("event") != "workflow_dispatch"
        or row.get("path") != path
        or not isinstance(revision, str)
        or not re.fullmatch(r"[0-9a-f]{40}", revision)
    ):
        return False
    run_id = _positive(row.get("id"), "PROVIDER_RUN_LIST_INVALID")
    try:
        jobs = provider.json(repository, f"/actions/runs/{run_id}/jobs?per_page=1")
        if not isinstance(jobs, dict) or jobs.get("total_count") != 0 or jobs.get("jobs") != []:
            return False
        source = provider.json(repository, f"/contents/{path}?ref={revision}")
    except ContractError:
        return False
    if (
        not isinstance(source, dict)
        or source.get("type") != "file"
        or type(source.get("size")) is not int
        or source["size"] <= MAX_WORKFLOW_BYTES
    ):
        return False
    print(f"Ignoring unstartable dispatch {run_id}: immutable workflow {revision} "
          f"is {source['size']} bytes and has no jobs", file=sys.stderr)
    return True


def _live_runs(provider: Provider, repository: str, workflow: str) -> list[int]:
    """Run ids of anything not settled. Fails CLOSED: a read that cannot be
    parsed is reported as busy, never as quiet, because standing down costs one
    idle cycle while proceeding could race the relay for the staging lock."""
    rows = _run_page(provider, repository, workflow, "per_page=50")
    live: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("PROVIDER_RUN_LIST_INVALID")
        status = row.get("status")
        if not isinstance(status, str):
            raise ContractError("PROVIDER_RUN_LIST_INVALID")
        if status in BUSY_STATUSES:
            if _unstartable_dispatch(provider, repository, workflow, row):
                continue
            live.append(_positive(row.get("id"), "PROVIDER_RUN_LIST_INVALID"))
    return live


def yield_check(provider: Provider) -> dict[str, Any]:
    """Stand down whenever the relay, or any staging deploy, is live.

    This lane is strictly lower priority than the relay: the relay carries the
    product surfaces and already abandons its SECOND service whenever main
    moves inside its window, so a reconciler that took the lock from it would
    make the thing it is trying to fix worse. Same shape as the provider's own
    prewarm self-yield, including its fail-closed posture.
    """
    relay_live = _live_runs(provider, APP_REPOSITORY, "dispatch-staging-deploys.yml")
    deploy_live = _live_runs(provider, TF_REPOSITORY, "deploy-leaf-platform-staging.yml")
    if relay_live:
        return {
            "status": "yielded",
            "reason": "RELAY_LIVE",
            "detail": f"relay run(s) {sorted(relay_live)} not settled",
        }
    if deploy_live:
        return {
            "status": "yielded",
            "reason": "STAGING_DEPLOY_LIVE",
            "detail": f"staging deploy run(s) {sorted(deploy_live)} not settled",
        }
    return {"status": "clear", "reason": None, "detail": None}


def _newest_relay_release(provider: Provider) -> dict[str, Any]:
    """The newest successful relay run that actually published a receipt.

    A relay that stood down or went red published nothing, so it names no
    converged release and is skipped rather than treated as a failure.
    """
    rows = _run_page(
        provider,
        APP_REPOSITORY,
        "dispatch-staging-deploys.yml",
        f"status=success&per_page={MAX_RELAY_RUN_SCAN}",
    )
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("PROVIDER_RUN_LIST_INVALID")
        run_id = _positive(row.get("id"), "PROVIDER_RUN_LIST_INVALID")
        head_sha = row.get("head_sha")
        if not isinstance(head_sha, str):
            continue
        name = f"staging-converged-{head_sha}-attempt-{row.get('run_attempt')}"
        try:
            _artifact, raw = _one_artifact(
                provider, APP_REPOSITORY, run_id, name, RELAY_ARTIFACT_FILE
            )
        except ContractError:
            continue
        receipt = raw if isinstance(raw, dict) else None
        if receipt is None or receipt.get("schema") != "leaf.staging-converged.v2":
            continue
        supply = receipt.get("candidate_supply_set")
        if not isinstance(supply, dict):
            raise ContractError("RELAY_RECEIPT_INVALID")
        services = supply.get("services")
        if not isinstance(services, dict) or set(services) != set(SERVICE_ORDER):
            raise ContractError("RELAY_RECEIPT_INVALID")
        digests = {
            service: _digest(
                (services[service] or {}).get("image_digest"), "RELAY_RECEIPT_INVALID"
            )
            for service in SERVICE_ORDER
        }
        # The image tag is the supply set's own immutable_lookup_tag per
        # service, exactly what the relay dispatches. Never re-derived and never
        # read from a run name: the provider deploys whatever tag it is handed,
        # so a guessed one is a wrong deploy rather than a failed dispatch.
        tags = {
            service: _image_tag(
                (services[service] or {}).get("immutable_lookup_tag"),
                "RELAY_RECEIPT_INVALID",
            )
            for service in SERVICE_ORDER
        }
        return {
            "relay_run_id": run_id,
            "relay_head_sha": head_sha,
            "relay_run_attempt": _positive(
                row.get("run_attempt"), "PROVIDER_RUN_LIST_INVALID"
            ),
            "build_run_id": _positive(supply.get("build_run_id"), "RELAY_RECEIPT_INVALID"),
            "release_source_revision": _sha40(
                receipt.get("release_source_revision"), "RELAY_RECEIPT_INVALID"
            ),
            "supply_set_sha256": receipt.get("supply_set_sha256"),
            "service_digests": digests,
            "service_tags": tags,
        }
    raise ContractError("NO_CONVERGED_RELEASE")


def _relay_envelope(
    provider: Provider, release: dict[str, Any], *, prefix: str, file: str
) -> dict[str, Any]:
    """Locate the relay's published supply evidence envelope.

    Nothing else can mint one: deploy-leaf-platform-staging.yml refuses an
    envelope whose relay.workflow_path is not the relay's own file, and the
    convergence finalizer refuses any matched child whose supply evidence does
    not match the build's supply artifact. So this artifact is the ONLY route
    to a dispatch whose receipt can later be finalized, and its absence is the
    single fact that decides whether this plan can be armed.

    Absent is the ordinary answer for any release the relay converged before it
    started publishing the envelope, so it is reported, never raised. The
    content is deliberately NOT read here: the plan stays small and the armed
    lane downloads the artifact and passes it straight through, so no
    re-encoding can corrupt a value the provider validates byte for byte.
    """
    name = (
        f"{prefix}-{release['relay_head_sha']}"
        f"-attempt-{release['relay_run_attempt']}"
    )
    try:
        rows = _artifact_rows(provider, APP_REPOSITORY, release["relay_run_id"])
    except ContractError as exc:
        return {"present": False, "artifact_name": name, "reason": exc.reason}
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("name") == name and row.get("expired") is False
    ]
    if len(matches) != 1:
        return {
            "present": False,
            "artifact_name": name,
            "reason": "ENVELOPE_ARTIFACT_ABSENT",
            "detail": (
                "relay run "
                f"{release['relay_run_id']} published no usable envelope. "
                "Releases converged before the relay began publishing it cannot "
                "be armed; the next relay's release can."
            ),
        }
    return {
        "present": True,
        "artifact_name": name,
        "artifact_id": _positive(matches[0].get("id"), "PROVIDER_ARTIFACT_INVALID"),
        "relay_run_id": release["relay_run_id"],
        "file": file,
    }


def _settled_service_state(provider: Provider) -> dict[str, dict[str, Any]]:
    """Each service's most recent settled deploy, read from its own receipt.

    Deliberately evidence-based rather than an AWS read: this repository holds
    no AWS credentials, and the receipt already records the terminal digest and
    task definition the deploy landed on. It is a BEST-EFFORT view of live
    state, because a later deploy dispatched by anyone else could have moved a
    service since. That is safe here for two reasons: the plan is only read by
    a human, and every deploy step it names carries digest_aware_reconcile,
    which makes the provider skip a service that is already exact.
    """
    rows = _run_page(
        provider,
        TF_REPOSITORY,
        "deploy-leaf-platform-staging.yml",
        f"event=workflow_dispatch&status=success&per_page={MAX_DEPLOY_RUN_SCAN}",
    )
    # Pick the newest candidate per service from run metadata FIRST, then read
    # at most one artifact each. Downloading a receipt per scanned run instead
    # would be a per-item network round trip over a 60-run window to recover 5
    # facts, and broker appears about twice in such a window, so the naive shape
    # pays ~50 archive downloads on every tick of a 30-minute schedule.
    candidates: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("PROVIDER_RUN_LIST_INVALID")
        title = row.get("display_title")
        if not isinstance(title, str):
            continue
        match = RUN_TITLE.match(title)
        if match is None:
            continue
        service = match.group("service")
        if service not in SERVICE_ORDER or service in candidates:
            continue
        candidates[service] = row
        if len(candidates) == len(SERVICE_ORDER):
            break

    state: dict[str, dict[str, Any]] = {}
    for expected_service, row in candidates.items():
        run_id = _positive(row.get("id"), "PROVIDER_RUN_LIST_INVALID")
        name = (
            f"leaf-platform-staging-service-run-{run_id}"
            f"-attempt-{row.get('run_attempt')}"
        )
        try:
            _artifact, raw = _one_artifact(
                provider, TF_REPOSITORY, run_id, name, ARTIFACT_FILE
            )
        except ContractError:
            continue
        if not isinstance(raw, dict):
            continue
        requested = raw.get("requested")
        facts = raw.get("facts")
        if not isinstance(requested, dict) or not isinstance(facts, dict):
            continue
        service = requested.get("service")
        # The receipt, not the run title, decides. A mismatch means the title
        # contract drifted, so drop the candidate rather than record it under a
        # service it may not belong to.
        if service != expected_service or service in state:
            continue
        terminal = facts.get("terminal")
        if not isinstance(terminal, dict) or terminal.get("status") != "produced":
            continue
        value = terminal.get("value")
        if not isinstance(value, dict):
            continue
        image = value.get("image_digest")
        if not isinstance(image, dict) or image.get("status") != "produced":
            continue
        state[service] = {
            "run_id": run_id,
            "image_digest": _digest(image.get("value"), "SERVICE_RECEIPT_INVALID"),
            "task_definition": _task_definition(
                value.get("task_definition"), "SERVICE_RECEIPT_INVALID"
            ),
            "app_deploy_intent": requested.get("app_deploy_intent"),
        }
    return state


def _dispatch_step(service: str, image_tag: str, *, position: int) -> dict[str, Any]:
    """One forward reconcile of a non-relay service.

    digest_aware_reconcile is what makes this safe to name even when the
    best-effort read above is stale: the provider re-reads live state under the
    lock and skips a service that is already exact, so a redundant step costs a
    no-op run rather than a redeploy.
    """
    return {
        "position": position,
        "kind": "reconcile",
        "service": service,
        "workflow": DEPLOY_WORKFLOW,
        "repository": TF_REPOSITORY,
        "inputs": {
            "service": service,
            "expected_task_definition": "auto-live",
            "image_tag": image_tag,
            "app_deploy_intent": "forward",
            "digest_aware_reconcile": "true",
        },
        "requires_relay_supply_evidence": True,
    }


def _restamp_step(baseline: str, image_tag: str, *, position: int) -> dict[str, Any]:
    """The five-service identity stamp, which is the whole point of the lane.

    expected_task_definition must be an EXACT arn: the provider refuses
    auto-live unless app_deploy_intent is forward, and this is a configuration
    deploy. The arn is not guessed and needs no AWS read: it is the task
    definition the app's own most recent deploy receipt says it landed on.
    Verified on the dad27a10 wave, where the relay's app deploy 33696191244
    recorded terminal task_definition leaf-platform-app:762 and the restamp
    33699604872 was dispatched against exactly that, producing :763.

    The arn carries a blue/green COLOUR (a live read on 2026-09-03 resolved
    leaf-platform-app-alt:216, the alt family), and this is evidence of where
    the app last landed rather than a live read of where it sits now. That is
    safe to name but must not be trusted blindly: the provider resolves its
    configuration baseline from the LIVE colour and gates the family against it,
    so a colour that flipped after the app's last deploy fails closed there
    instead of stamping an identity against the wrong family.
    """
    return {
        "position": position,
        "kind": "identity_restamp",
        "service": "app",
        "workflow": DEPLOY_WORKFLOW,
        "repository": TF_REPOSITORY,
        "inputs": {
            "service": "app",
            "app_deploy_intent": "configuration",
            "expected_task_definition": baseline,
            "configuration_task_definition": baseline,
            "image_tag": image_tag,
            "deploy_strategy": "direct",
        },
        "requires_relay_supply_evidence": True,
    }


def build_plan(provider: Provider) -> dict[str, Any]:
    yielded = yield_check(provider)
    release = _newest_relay_release(provider)
    settled = _settled_service_state(provider)
    # BOTH halves. The three v3 dispatch inputs travel together, so a lane
    # holding only the supply envelope is refused at the provider's
    # "digest-aware consumer contract" gate before it reaches any credential
    # (measured on run 33719323168, deploy job skipped, nothing mutated).
    envelope = _relay_envelope(
        provider,
        release,
        prefix="staging-supply-evidence",
        file="staging-supply-evidence.b64",
    )
    contract = _relay_envelope(
        provider,
        release,
        prefix="staging-consumer-contract",
        file="staging-consumer-contract.b64",
    )

    services: dict[str, Any] = {}
    for service in SERVICE_ORDER:
        target = release["service_digests"][service]
        current = settled.get(service)
        if current is None:
            status = "unknown"
        elif current["image_digest"] == target:
            status = "converged"
        else:
            status = "lagging"
        services[service] = {
            "target_image_digest": target,
            "observed_image_digest": None if current is None else current["image_digest"],
            "observed_from_run_id": None if current is None else current["run_id"],
            "status": status,
            "owner": "relay" if service in RELAY_SERVICES else "reconciler",
        }

    # Reported in DISPATCH order, not SERVICE_ORDER, so the list and the steps
    # below cannot disagree about what happens first.
    lagging = [
        service
        for service in NON_RELAY_SERVICES
        if services[service]["status"] != "converged"
    ]

    blockers: list[str] = []
    steps: list[dict[str, Any]] = []
    position = 0
    for service in NON_RELAY_SERVICES:
        if service in lagging:
            position += 1
            steps.append(
                _dispatch_step(
                    service, release["service_tags"][service], position=position
                )
            )

    # The identity stamp reads LIVE digests for all five services, so naming it
    # while any service still lags would plan a stamp the finalizer must then
    # reject with DEPLOYMENT_IDENTITY_MISMATCH. Report the blocker instead.
    app_state = settled.get("app")
    relay_lagging = [s for s in RELAY_SERVICES if services[s]["status"] != "converged"]
    restamp: dict[str, Any] | None = None
    if relay_lagging:
        blockers.append(
            "RELAY_SURFACES_NOT_CONVERGED: "
            f"{relay_lagging} are the relay's to land, not this lane's; "
            "the next relay converges them."
        )
    elif app_state is None:
        blockers.append(
            "APP_BASELINE_UNKNOWN: no settled app deploy receipt in the scanned "
            "window, so the restamp baseline task definition cannot be read."
        )
    else:
        # The restamp is an app deploy, so it carries the APP tag: it deploys
        # the existing immutable image and re-stamps its identity.
        restamp = _restamp_step(
            app_state["task_definition"],
            release["service_tags"]["app"],
            position=position + 1,
        )
        if lagging:
            blockers.append(
                "FLEET_NOT_CONVERGED: the identity stamp samples LIVE digests for "
                f"all five services, so it is only valid once {lagging} land. The "
                "step is listed last and must not be dispatched before them."
            )
        steps.append(restamp)

    if not steps:
        blockers.append(
            "NOTHING_TO_DO: every non-relay service already matches the release "
            "and the identity baseline is unchanged."
        )

    # ARMABILITY is a separate question from correctness. A plan can be a
    # perfectly good report and still be undispatchable, and every reason it is
    # undispatchable is already a fact stated above rather than a new judgement.
    not_armable: list[str] = []
    if yielded["status"] != "clear":
        not_armable.append(f"YIELDED: {yielded['reason']}")
    if not envelope["present"]:
        not_armable.append(
            "NO_SUPPLY_EVIDENCE: without the relay's envelope a dispatch produces "
            "a receipt the finalizer refuses (SERVICE_SUPPLY_EVIDENCE_MISMATCH), "
            "so it would mutate staging and prove nothing."
        )
    if not contract["present"]:
        not_armable.append(
            "NO_CONSUMER_CONTRACT: a digest-aware dispatch is refused without it, "
            "at the provider's gate before any credential."
        )
    if relay_lagging:
        not_armable.append("RELAY_SURFACES_NOT_CONVERGED")
    if not steps:
        not_armable.append("NOTHING_TO_DO")

    return {
        "schema": PLAN_SCHEMA,
        "environment": ENVIRONMENT,
        "mode": "report_only",
        "yield": yielded,
        "release": {
            "source_revision": release["release_source_revision"],
            "relay_run_id": release["relay_run_id"],
            "build_run_id": release["build_run_id"],
            "supply_set_sha256": release["supply_set_sha256"],
        },
        "services": services,
        "lagging": lagging,
        "steps": steps,
        "blockers": blockers,
        "supply_evidence": envelope,
        "consumer_contract": contract,
        "armable": not not_armable,
        "not_armable_because": not_armable,
        # The provider hard-requires relay.workflow_path ==
        # .github/workflows/dispatch-staging-deploys.yml, so this lane can never
        # mint its own envelope and every step must carry the one the relay
        # minted for THIS release. The relay publishes it as of 2026-09-03, so
        # this is now a resolved reference rather than a standing blocker: see
        # supply_evidence above, and armable, which is false whenever it is
        # missing.
        "arming_prerequisite": {
            "reason": "SUPPLY_EVIDENCE_IS_RELAY_MINTED",
            "resolved_by": envelope,
        },
    }


def _render_summary(plan: dict[str, Any]) -> str:
    lines = [f"# Staging fleet reconcile plan ({plan['mode']})", ""]
    y = plan["yield"]
    if y["status"] != "clear":
        lines += [f"**Stood down: {y['reason']}** {y['detail']}", ""]
    rel = plan["release"]
    lines += [
        f"Release `{rel['source_revision'][:12]}` "
        f"(relay run {rel['relay_run_id']}, build {rel['build_run_id']})",
        "",
        "| service | owner | status | observed |",
        "| --- | --- | --- | --- |",
    ]
    for service in SERVICE_ORDER:
        row = plan["services"][service]
        observed = row["observed_image_digest"]
        lines.append(
            f"| {service} | {row['owner']} | {row['status']} | "
            f"{'unknown' if observed is None else observed[:19]} |"
        )
    lines += ["", f"Planned steps: {len(plan['steps'])}"]
    for step in plan["steps"]:
        lines.append(f"- {step['position']}. {step['kind']} `{step['service']}`")
    if plan["blockers"]:
        lines += ["", "Blockers:"]
        lines += [f"- {b}" for b in plan["blockers"]]
    lines += ["", f"Armable: {'yes' if plan['armable'] else 'no'}"]
    lines += [f"- blocked by: {r}" for r in plan["not_armable_because"]]
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve the staging fleet reconcile-and-restamp plan (read only)."
    )
    parser.add_argument("--output")
    parser.add_argument("--summary", required=False)
    parser.add_argument("--check-idle", action="store_true",
                        help="Check the same lane predicate before a dispatch; exit 1 if busy.")
    return parser


def main(argv: list[str]) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.check_idle and not args.output:
        parser.error("--output is required unless --check-idle is set")
    provider = GitHubProvider(
        os.environ.get("APP_GITHUB_TOKEN", ""),
        os.environ.get("TERRAFORM_GITHUB_TOKEN", ""),
    )
    try:
        if args.check_idle:
            result = yield_check(provider)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["status"] == "clear" else 1
        plan = build_plan(provider)
    except ContractError as exc:
        print(f"ERROR:{exc}", file=sys.stderr)
        return 2
    raw = json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n"
    with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(raw)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(_render_summary(plan))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
