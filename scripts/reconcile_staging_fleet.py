"""Plan and execute staging fleet reconcile legs against frozen relay evidence.

Scheduled discovery targets the newest published release and yields to relays.
An in-relay execution binds local frozen files to its own run, completes the
three non-product services sequentially, and configuration-restamps app. Both
call the same planner and executor; neither rolls back landed product surfaces.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request
from typing import Any

from scripts import platform_staging_convergence as convergence
from scripts.platform_staging_convergence import (
    APP_REPOSITORY,
    ARTIFACT_FILE,
    DEPLOY_WORKFLOW,
    SERVICE_ORDER,
    STALE_LISTING_READS,
    TF_REPOSITORY,
    ContractError,
    GitHubProvider,
    Provider,
    _artifact_rows,
    _listing_top_id,
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
MAX_DEPLOY_RUN_SCAN = 100
MAX_DEPLOY_RUN_PAGES = 3
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
# Provider readback on 2026-09-05 binds these six retained records to one
# immutable, unstartable workflow. The workflow token can read Actions but may
# not read Contents. Requiring that extra permission to rediscover unchanged
# bytes would make the same incident block forever. This is not an age-based
# exemption: the run identity and live empty jobs list must still match.
OVERSIZE_INCIDENT_REVISION = "49e265747ca7812d6f4c45e64aba93ce2169daf4"
OVERSIZE_INCIDENT_BLOB = "6e9cdb70736e8897e13dc8d2286562d37333674d"
OVERSIZE_INCIDENT_BYTES = 522389
OVERSIZE_INCIDENT_RUNS = frozenset({
    33830277169, 33830283294, 33835703473,
    33835710962, 33836054329, 33836062583,
})

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

    Callers selecting the newest successful run use _freshest_run_page:
    GitHub can serve stale pages containing only older runs, so those callers
    keep the greatest run id across three snapshots. Busy-run checks read once.
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
    except ContractError:
        return False
    try:
        source = provider.json(repository, f"/contents/{path}?ref={revision}")
    except ContractError as exc:
        if not (
            repository == TF_REPOSITORY
            and workflow == "deploy-leaf-platform-staging.yml"
            and revision == OVERSIZE_INCIDENT_REVISION
            and run_id in OVERSIZE_INCIDENT_RUNS
        ):
            return False
        source = {
            "type": "file", "size": OVERSIZE_INCIDENT_BYTES,
            "sha": OVERSIZE_INCIDENT_BLOB,
        }
        # Error class/status only, never a request, header, body, or credential.
        http_status = getattr(exc.__cause__, "code", None)
        print(f"Using frozen incident blob {OVERSIZE_INCIDENT_BLOB} for run "
              f"{run_id}: contents read {exc.reason}, HTTP {http_status}", file=sys.stderr)
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


def _live_runs(provider: Provider, repository: str, workflow: str, relay_run_id: int | None = None) -> list[int]:
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
            run_id = _positive(row.get("id"), "PROVIDER_RUN_LIST_INVALID")
            if relay_run_id is not None and (
                run_id == relay_run_id
                or (run_id > relay_run_id and status in {"queued", "waiting", "requested", "pending"})
            ):
                continue
            if _unstartable_dispatch(provider, repository, workflow, row):
                continue
            live.append(_positive(row.get("id"), "PROVIDER_RUN_LIST_INVALID"))
    return live


def yield_check(provider: Provider, relay_run_id: int | None = None) -> dict[str, Any]:
    """Stand down whenever the relay, or any staging deploy, is live.

    This lane is strictly lower priority than the relay: the relay carries the
    product surfaces and already abandons its SECOND service whenever main
    moves inside its window, so a reconciler that took the lock from it would
    make the thing it is trying to fix worse. Same shape as the provider's own
    prewarm self-yield, including its fail-closed posture.
    """
    relay_live = _live_runs(provider, APP_REPOSITORY, "dispatch-staging-deploys.yml", relay_run_id)
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


def _freshest_run_page(
    provider: Provider, repository: str, workflow: str, query: str
) -> list[dict[str, Any]]:
    chosen: list[dict[str, Any]] | None = None
    chosen_id = 0
    top_ids: list[int] = []
    for _read in range(STALE_LISTING_READS):
        rows = _run_page(provider, repository, workflow, query)
        top_id = _listing_top_id(rows)
        top_ids.append(top_id)
        if chosen is None or top_id > chosen_id:
            chosen, chosen_id = rows, top_id
    if len(set(top_ids)) > 1:
        print(
            f"Stale run listing: {workflow} read top ids {top_ids}; chose {chosen_id}",
            file=sys.stderr,
        )
    assert chosen is not None
    return chosen


def _newest_relay_release(provider: Provider) -> dict[str, Any]:
    """The newest successful relay run that actually published a receipt.

    A relay that stood down or went red published nothing, so it names no
    converged release and is skipped rather than treated as a failure.
    """
    rows = _freshest_run_page(
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
    state, because a later deploy could have moved a service since. Each direct
    deploy pins this receipt's exact task definition. The provider compares it
    with live state under its mutation lock and refuses a stale baseline.
    """
    # Pick the newest candidate per service from run metadata FIRST, then read
    # at most one artifact each. Non-relay services can fall outside the first
    # page during a busy web/app release window (worker was row 96 on 2026-09-05).
    # Read at most three metadata pages, stopping as soon as all five are found.
    candidates: dict[str, dict[str, Any]] = {}
    for page in range(1, MAX_DEPLOY_RUN_PAGES + 1):
        query = f"event=workflow_dispatch&status=success&per_page={MAX_DEPLOY_RUN_SCAN}"
        if page > 1:
            query += f"&page={page}"
        rows = _freshest_run_page(provider, TF_REPOSITORY, "deploy-leaf-platform-staging.yml", query)
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
        if len(candidates) == len(SERVICE_ORDER) or len(rows) < MAX_DEPLOY_RUN_SCAN:
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


def _dispatch_step(
    service: str, image_tag: str, baseline: str, *, position: int
) -> dict[str, Any]:
    """One forward reconcile of a non-relay service.

    V3 supply permits digest-aware routing only for web/app. The other three
    services require direct mode and an exact reviewed baseline, never
    auto-live. The closed supply envelope still binds the selected digest;
    the provider's locked baseline check refuses intervening deployments.
    """
    return {
        "position": position,
        "kind": "reconcile",
        "service": service,
        "workflow": DEPLOY_WORKFLOW,
        "repository": TF_REPOSITORY,
        "inputs": {
            "service": service,
            "expected_task_definition": baseline,
            "image_tag": image_tag,
            "app_deploy_intent": "forward",
            "digest_aware_reconcile": "false",
            "deploy_strategy": "direct",
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
    return plan_release(release, settled, yielded, envelope, contract)


def plan_release(
    release: dict[str, Any], settled: dict[str, dict[str, Any]],
    yielded: dict[str, Any], envelope: dict[str, Any], contract: dict[str, Any],
) -> dict[str, Any]:
    """Shared planning core; callers supply the frozen release and evidence."""

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
    missing_baselines: list[str] = []
    position = 0
    for service in NON_RELAY_SERVICES:
        if service in lagging:
            current = settled.get(service)
            if current is None:
                missing_baselines.append(service)
                blockers.append(
                    f"SERVICE_BASELINE_UNKNOWN: {service} has no settled receipt; "
                    "the provider requires its exact task-definition baseline."
                )
                continue
            position += 1
            steps.append(
                _dispatch_step(
                    service, release["service_tags"][service],
                    current["task_definition"], position=position
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
    if missing_baselines:
        not_armable.append(f"SERVICE_BASELINE_UNKNOWN: {missing_baselines}")
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


def _decode_envelope(raw: bytes) -> dict[str, Any]:
    try:
        if not raw or len(raw) > 512 * 1024 or not re.fullmatch(rb"[A-Za-z0-9_-]+", raw):
            raise ValueError("invalid envelope")
        value = convergence._load_json(base64.b64decode(
            raw + b"=" * (-len(raw) % 4), altchars=b"-_", validate=True
        ))
        if not isinstance(value, dict):
            raise ValueError("invalid envelope")
        return value
    except (ValueError, UnicodeDecodeError) as exc:
        raise ContractError("FROZEN_ENVELOPE_INVALID") from exc


def bind_relay_inputs(
    relay_run_id: int, receipt: dict[str, Any], supply_raw: bytes,
    evidence_raw: bytes, contract_raw: bytes,
    *, check_current_run: bool = True,
) -> dict[str, Any]:
    """Bind local inputs without looking up any other release or artifact."""
    run_id = _positive(relay_run_id, "RELAY_RUN_ID_INVALID")
    try:
        manifest = convergence._load_json(supply_raw)
        evidence = _decode_envelope(evidence_raw)
        contract = _decode_envelope(contract_raw)
        producer = evidence["producer"]
        build = {
            "run_id": producer["run_id"], "run_attempt": producer["run_attempt"],
            "head_sha": producer["source_revision"],
        }
        supply = convergence._supply(
            manifest, build, producer["source_tree"],
            file_sha256=hashlib.sha256(supply_raw).hexdigest(),
        )
        convergence._relay_receipt(receipt, build, supply, run_id, manifest)
        relay = evidence["relay"]
        if (
            evidence["schema"] != "leaf.staging-supply-dispatch-evidence.v1"
            or relay["run_id"] != run_id
            or relay["repository"] != APP_REPOSITORY
            or relay["workflow_path"] != ".github/workflows/dispatch-staging-deploys.yml"
            or producer["repository"] != APP_REPOSITORY
            or producer["workflow_path"] != ".github/workflows/build-platform-images.yml"
            or producer["event"] != "push"
            or evidence["manifest"]["sha256"] != supply["manifest_sha256"]
            or evidence["manifest"]["source_revision"] != supply["source_revision"]
            or evidence["manifest"]["source_tree"] != supply["source_tree"]
            or evidence["manifest"]["schema"] != manifest["schema"]
            or base64.urlsafe_b64decode(evidence["manifest"]["json_b64"] + "=" * (
                -len(evidence["manifest"]["json_b64"]) % 4)) != supply_raw
        ):
            raise ContractError("FROZEN_SUPPLY_BINDING_MISMATCH")
        _positive(relay["run_attempt"], "RELAY_RUN_ID_INVALID")
        _positive(evidence["supply_artifact"]["id"], "FROZEN_SUPPLY_BINDING_MISMATCH")
        convergence._sha64(evidence["supply_artifact"]["provider_archive_sha256"], "FROZEN_SUPPLY_BINDING_MISMATCH")
        if not isinstance(evidence["supply_artifact"]["name"], str) or not evidence["supply_artifact"]["name"]:
            raise ContractError("FROZEN_SUPPLY_BINDING_MISMATCH")
        if check_current_run and os.environ.get("GITHUB_RUN_ID") and int(os.environ["GITHUB_RUN_ID"]) != run_id:
            raise ContractError("RELAY_RUN_ID_MISMATCH")
        if check_current_run and os.environ.get("GITHUB_RUN_ATTEMPT") and int(os.environ["GITHUB_RUN_ATTEMPT"]) != relay["run_attempt"]:
            raise ContractError("RELAY_RUN_ATTEMPT_MISMATCH")
        unsigned = dict(contract)
        envelope_hash = unsigned.pop("envelope_sha256")
        if (
            contract["schema"] != convergence.CONSUMER_DISPATCH_SCHEMA
            or hashlib.sha256(convergence._canonical(unsigned)).hexdigest() != envelope_hash
        ):
            raise ContractError("FROZEN_CONSUMER_CONTRACT_INVALID")
        body = contract["contract"]
        convergence._exact(contract, {"schema", "contract", "artifact", "envelope_sha256"}, "FROZEN_CONSUMER_CONTRACT_INVALID")
        convergence._exact(body, {"schema", "version", "producer", "consumer", "artifact", "payload_sha256"}, "FROZEN_CONSUMER_CONTRACT_INVALID")
        unsigned_body = dict(body)
        payload_hash = unsigned_body.pop("payload_sha256")
        if (
            body["schema"] != convergence.CONSUMER_CONTRACT_SCHEMA
            or body["version"] != 1
            or hashlib.sha256(convergence._canonical(unsigned_body)).hexdigest() != payload_hash
            or body["consumer"]["deploy_workflow_path"] != DEPLOY_WORKFLOW
        ):
            raise ContractError("FROZEN_CONSUMER_CONTRACT_INVALID")
        blob = _sha40(body["consumer"]["deploy_workflow_blob"], "FROZEN_CONSUMER_CONTRACT_INVALID")
        consumer = body["consumer"]
        if (
            consumer["contract_schema_path"] != convergence.CONSUMER_CONTRACT_SCHEMA_PATH
            or consumer["contract_version"] != 1
            or consumer["pins"] != {"deployment_environment": "aws-apply",
                                     "digest_aware_marker": "leaf.staging-digest-aware-consumer.v1",
                                     "mutation_group": "leaf-platform-staging-ecs-mutation"}
            or body["producer"]["repository"] != TF_REPOSITORY
            or body["producer"]["workflow_path"] != convergence.CONSUMER_CONTRACT_WORKFLOW
            or body["producer"]["event"] != "push" or body["producer"]["branch"] != "main"
            or body["artifact"]["file"] != "consumer-contract.json"
            or body["artifact"]["name"] != contract["artifact"]["name"]
            or body["producer"]["run_id"] != contract["artifact"]["producer_run_id"]
            or body["producer"]["run_attempt"] != contract["artifact"]["producer_run_attempt"]
            or contract["artifact"]["provider_sha256"] != contract["artifact"]["archive_sha256"]
        ):
            raise ContractError("FROZEN_CONSUMER_CONTRACT_INVALID")
        for key in ("head_sha", "head_tree", "workflow_blob"):
            _sha40(body["producer"][key], "FROZEN_CONSUMER_CONTRACT_INVALID")
        _sha40(consumer["contract_schema_blob"], "FROZEN_CONSUMER_CONTRACT_INVALID")
        for key in ("provider_sha256", "archive_sha256", "file_sha256"):
            convergence._sha64(contract["artifact"][key], "FROZEN_CONSUMER_CONTRACT_INVALID")
        for key in ("id", "producer_run_id", "producer_run_attempt"):
            _positive(contract["artifact"][key], "FROZEN_CONSUMER_CONTRACT_INVALID")
        for service in RELAY_SERVICES:
            surface = receipt["surface_results"][service]
            if not isinstance(surface, dict):
                raise ContractError("RELAY_SURFACE_RESULT_INVALID")
            if (
                surface.get("schema") != "leaf.staging-surface-result.v1"
                or surface.get("service") != service
                or surface.get("release_source_revision") != supply["source_revision"]
                or surface.get("convergence_id") != f"{supply['source_revision']}-{build['run_attempt']}-{service}"
                or surface.get("candidate_image_digest") != supply["service_digests"][service]
                or surface.get("terminal_image_digest") != supply["service_digests"][service]
                or surface.get("terraform_workflow_blob") != blob
                or not re.fullmatch(r"[0-9a-f]{64}", str(surface.get("surface_receipt_sha256", "")))
                or not ((surface.get("outcome") == "skipped" and surface.get("aws_mutation_count") == 0)
                        or (surface.get("outcome") == "deployed" and type(surface.get("aws_mutation_count")) is int
                            and surface["aws_mutation_count"] > 0))
            ):
                raise ContractError("RELAY_SURFACE_RESULT_INVALID")
        release = {
            "relay_run_id": run_id, "build_run_id": build["run_id"],
            "relay_run_attempt": relay["run_attempt"],
            "release_source_revision": supply["source_revision"],
            "supply_set_sha256": supply["manifest_sha256"],
            "service_digests": supply["service_digests"],
            "service_tags": {
                s: _image_tag(manifest["services"][s]["immutable_lookup_tag"], "SUPPLY_MANIFEST_INVALID")
                for s in SERVICE_ORDER
            },
        }
        return {
            "release": release, "supply": supply, "workflow_blob": blob,
            "evidence": evidence_raw, "contract": contract_raw,
            "supply_artifact": evidence["supply_artifact"], "producer": producer,
        }
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("FROZEN_INPUT_INVALID") from exc


def build_relay_plan(provider: Provider, context: dict[str, Any]) -> dict[str, Any]:
    release = context["release"]
    return plan_release(
        release, _settled_service_state(provider),
        yield_check(provider, release["relay_run_id"]),
        {"present": True, "local": True}, {"present": True, "local": True},
    )


def _evidence_slot(raw: bytes) -> dict[str, Any]:
    return {"status": "produced", "sha256": hashlib.sha256(raw).hexdigest(), "utf8_bytes": len(raw)}


def validate_leg(
    provider: Provider, run_id: int, step: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """A green run is insufficient: prove its baseline, supply and terminal state."""
    run = convergence._provider_run(
        provider.json(TF_REPOSITORY, f"/actions/runs/{run_id}"),
        TF_REPOSITORY, DEPLOY_WORKFLOW, "workflow_dispatch",
    )
    name = f"leaf-platform-staging-service-run-{run_id}-attempt-{run['run_attempt']}"
    artifact, raw = _one_artifact(provider, TF_REPOSITORY, run_id, name, ARTIFACT_FILE)
    receipt = convergence._service_receipt(raw)
    requested = receipt["requested"]
    rp = receipt["provider"]
    if (
        rp["run_id"] != run_id or rp["run_attempt"] != run["run_attempt"]
        or rp["head_sha"] != run["head_sha"]
        or rp["workflow_blob"] != context["workflow_blob"]
        or requested["supply_evidence"] != _evidence_slot(context["evidence"])
        or requested["consumer_contract"] != _evidence_slot(context["contract"])
        or requested["deploy_mode"] != "normal"
        or requested["digest_aware_reconcile"] is not False
        or not convergence._receipt_matches_supply(receipt, context["supply"])
    ):
        raise ContractError("SERVICE_RECEIPT_BINDING_MISMATCH")
    for key in ("service", "image_tag", "expected_task_definition", "app_deploy_intent", "deploy_strategy"):
        if requested[key] != step["inputs"][key]:
            raise ContractError("SERVICE_RECEIPT_REQUEST_MISMATCH")
    if step["kind"] == "identity_restamp" and requested["configuration_task_definition"] != step["inputs"]["configuration_task_definition"]:
        raise ContractError("SERVICE_RECEIPT_BASELINE_MISMATCH")
    facts = receipt["facts"]
    if facts["predecessor_task_definition"] != {
        "status": "produced", "value": step["inputs"]["expected_task_definition"],
    }:
        raise ContractError("SERVICE_RECEIPT_BASELINE_MISMATCH")
    producer = context["producer"]
    expected_supply = {
        "artifact_id": context["supply_artifact"]["id"],
        "artifact_name": context["supply_artifact"]["name"],
        "manifest_sha256": context["supply"]["manifest_sha256"],
        "producer_run_id": producer["run_id"],
        "producer_run_attempt": producer["run_attempt"],
    }
    if facts["supply"] != {"status": "produced", "value": expected_supply}:
        raise ContractError("SERVICE_SUPPLY_EVIDENCE_MISMATCH")
    outcome = convergence._normalized_outcome(provider, run, receipt)
    child = {
        "terminal": facts["terminal"], "outcome": outcome,
        "deployment_identity": facts["deployment_identity"], "provider": {"run_id": run_id},
    }
    if convergence._terminal_digest(child) != context["supply"]["service_digests"][step["service"]]:
        raise ContractError("SERVICE_DIGEST_MISMATCH")
    if facts["terminal"]["value"]["service"] != f"leaf-platform-{step['service']}":
        raise ContractError("SERVICE_RECEIPT_REQUEST_MISMATCH")
    terminal = _task_definition(facts["terminal"]["value"]["task_definition"], "SERVICE_TERMINAL_INVALID")
    result = {"run_id": run_id, "service": step["service"], "kind": step["kind"], "task_definition": terminal}
    if step["kind"] == "identity_restamp":
        result["identity"] = convergence._identity(child, context["supply"])
    return result


class WorkflowExecutor:
    """Single POST per leg, followed by bounded provider reads; never retries a POST."""

    def __init__(self, provider: Provider, clock=time.monotonic, sleep=time.sleep) -> None:
        self.provider, self.clock, self.sleep = provider, clock, sleep

    def dispatch(self, step: dict[str, Any], context: dict[str, Any], deadline: float) -> int:
        before = convergence._listing_top_id(_freshest_run_page(
            self.provider, TF_REPOSITORY, "deploy-leaf-platform-staging.yml", "per_page=20",
        ))
        inputs = dict(step["inputs"])
        inputs.update(
            supply_evidence_b64=context["evidence"].decode("ascii"),
            consumer_contract_b64=context["contract"].decode("ascii"),
            digest_aware_reconcile="false",
        )
        if self.clock() >= deadline:
            raise ContractError("LEG_TIMEOUT")
        request = urllib.request.Request(
            f"https://api.github.com/repos/{TF_REPOSITORY}/actions/workflows/deploy-leaf-platform-staging.yml/dispatches",
            data=json.dumps({"ref": "main", "inputs": inputs}).encode(),
            headers={
                "Authorization": "Bearer " + os.environ["TERRAFORM_GITHUB_TOKEN"],
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "leaf-staging-fleet-reconciler/1.0",
            }, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=min(45, max(1, deadline - self.clock()))) as response:
                if response.status != 204:
                    raise ContractError("LEG_DISPATCH_FAILED")
        except OSError as exc:
            raise ContractError("LEG_DISPATCH_UNRESOLVED") from exc
        resolve_by = min(deadline, self.clock() + 300)
        title = f"Deploy leaf-platform staging {step['service']} ({step['inputs']['image_tag']})"
        while self.clock() < resolve_by:
            rows = _run_page(self.provider, TF_REPOSITORY, "deploy-leaf-platform-staging.yml", "per_page=20")
            matches = [r for r in rows if _positive(r.get("id"), "PROVIDER_RUN_LIST_INVALID") > before
                       and r.get("display_title") == title and r.get("event") == "workflow_dispatch"]
            if len(matches) > 1:
                raise ContractError("LEG_RUN_AMBIGUOUS")
            if matches:
                return matches[0]["id"]
            self.sleep(min(5, max(0, resolve_by - self.clock())))
        raise ContractError("LEG_RUN_UNRESOLVED")

    def watch(self, run_id: int, deadline: float) -> None:
        while self.clock() < deadline:
            row = self.provider.json(TF_REPOSITORY, f"/actions/runs/{run_id}")
            if not isinstance(row, dict):
                raise ContractError("PROVIDER_RUN_INVALID")
            if row.get("status") == "completed":
                if row.get("conclusion") != "success":
                    raise ContractError("LEG_RUN_UNSUCCESSFUL")
                return
            self.sleep(min(20, max(0, deadline - self.clock())))
        raise ContractError("LEG_TIMEOUT")


def execute_plan(
    provider: Provider, plan: dict[str, Any], context: dict[str, Any], *,
    relay_run_id: int | None = None, timeout_seconds: int = 4800,
    executor: WorkflowExecutor | None = None,
) -> dict[str, Any]:
    executor = executor or WorkflowExecutor(provider)
    result: dict[str, Any] = {
        "schema": "leaf.staging-fleet-result.v1", "release": plan["release"],
        "status": "failed", "children": [], "restamp_frontier": None,
        "failed_leg": None, "failed_run_id": None, "reason": None,
    }
    deadline = executor.clock() + timeout_seconds
    try:
        if not plan["armable"]:
            raise ContractError("PLAN_NOT_ARMABLE")
        for step in plan["steps"]:
            result["failed_leg"] = f"{step['kind']}:{step['service']}"
            result["failed_run_id"] = None
            service = step["service"]
            if not (
                (service in NON_RELAY_SERVICES and step["kind"] == "reconcile"
                 and step["inputs"]["app_deploy_intent"] == "forward")
                or (service == "app" and step["kind"] == "identity_restamp"
                    and step["inputs"]["app_deploy_intent"] == "configuration")
            ):
                raise ContractError("LEG_NOT_ALLOWED")
            _task_definition(step["inputs"]["expected_task_definition"], "SERVICE_BASELINE_INVALID")
            if (step["inputs"]["image_tag"] != context["release"]["service_tags"][service]
                    or step["inputs"]["deploy_strategy"] != "direct"):
                raise ContractError("LEG_NOT_ALLOWED")
            if executor.clock() >= deadline:
                raise ContractError("LEG_TIMEOUT")
            idle = yield_check(provider, relay_run_id)
            if idle["status"] != "clear":
                if relay_run_id is None:
                    result.update(status="yielded", reason=idle["reason"])
                    return result
                raise ContractError(idle["reason"])
            # Re-read the exact baseline before each POST. The consumer also
            # checks under its mutation lock, closing the read/dispatch gap.
            settled = _settled_service_state(provider)
            current = settled.get(step["service"])
            if current is None or current["task_definition"] != step["inputs"]["expected_task_definition"]:
                raise ContractError("SERVICE_BASELINE_STALE")
            run_id = executor.dispatch(step, context, deadline)
            result["failed_run_id"] = run_id
            executor.watch(run_id, deadline)
            child = validate_leg(provider, run_id, step, context)
            result["children"].append(child)
            if step["kind"] == "identity_restamp":
                result["restamp_frontier"] = child
        result.update(status="success", failed_leg=None, failed_run_id=None)
    except (ContractError, OSError, ValueError, KeyError, TypeError) as exc:
        result["reason"] = exc.reason if isinstance(exc, ContractError) else "LEG_EVIDENCE_INVALID"
    return result


def _published_envelope(provider: Provider, reference: dict[str, Any]) -> bytes:
    return convergence._zip_member(
        provider.bytes(APP_REPOSITORY, f"/actions/artifacts/{reference['artifact_id']}/zip"),
        reference["file"],
    )


def scheduled_context(provider: Provider, plan: dict[str, Any]) -> dict[str, Any]:
    """Load only the envelopes named by the already resolved scheduled plan."""
    evidence = _published_envelope(provider, plan["supply_evidence"])
    contract = _published_envelope(provider, plan["consumer_contract"])
    decoded = _decode_envelope(evidence)
    manifest = decoded["manifest"]["json_b64"]
    supply_raw = base64.urlsafe_b64decode(manifest + "=" * (-len(manifest) % 4))
    release = plan["release"]
    # Retrieve THIS relay's receipt, never discover a newer release mid-plan.
    ref = plan["supply_evidence"]
    name = ref["artifact_name"].replace("staging-supply-evidence-", "staging-converged-", 1)
    _, receipt = _one_artifact(provider, APP_REPOSITORY, release["relay_run_id"], name, RELAY_ARTIFACT_FILE)
    return bind_relay_inputs(release["relay_run_id"], receipt, supply_raw, evidence, contract, check_current_run=False)


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
        description="Plan or execute staging fleet reconcile-and-restamp legs."
    )
    parser.add_argument("--output")
    parser.add_argument("--summary", required=False)
    parser.add_argument("--in-relay", action="store_true")
    parser.add_argument("--relay-run-id", type=int)
    parser.add_argument("--relay-receipt")
    parser.add_argument("--supply-set")
    parser.add_argument("--supply-evidence")
    parser.add_argument("--consumer-contract")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--plan-file")
    parser.add_argument("--timeout-seconds", type=int, default=4800)
    parser.add_argument("--check-idle", action="store_true",
                        help="Check the same lane predicate before a dispatch; exit 1 if busy.")
    return parser


def main(argv: list[str]) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.check_idle and not args.output:
        parser.error("--output is required unless --check-idle is set")
    if args.timeout_seconds <= 0 or args.timeout_seconds > 4800:
        parser.error("--timeout-seconds must be between 1 and 4800")
    if args.in_relay and not all((args.relay_run_id, args.relay_receipt, args.supply_set,
                                  args.supply_evidence, args.consumer_contract)):
        parser.error("--in-relay requires the relay run and all four frozen files")
    try:
        provider = GitHubProvider(
            os.environ.get("APP_GITHUB_TOKEN", ""),
            os.environ.get("TERRAFORM_GITHUB_TOKEN", ""),
        )
        if args.check_idle:
            result = yield_check(provider)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["status"] == "clear" else 1
        if args.in_relay:
            context = bind_relay_inputs(
                args.relay_run_id, convergence._load_json(Path(args.relay_receipt).read_bytes()),
                Path(args.supply_set).read_bytes(), Path(args.supply_evidence).read_bytes(),
                Path(args.consumer_contract).read_bytes(),
            )
            plan = build_relay_plan(provider, context)
        else:
            plan = (convergence._load_json(Path(args.plan_file).read_bytes())
                    if args.plan_file else build_plan(provider))
        if args.execute:
            if not args.in_relay:
                context = scheduled_context(provider, plan)
                if context["release"]["supply_set_sha256"] != plan["release"]["supply_set_sha256"]:
                    raise ContractError("FROZEN_SUPPLY_BINDING_MISMATCH")
            plan = execute_plan(provider, plan, context, relay_run_id=args.relay_run_id if args.in_relay else None,
                                timeout_seconds=args.timeout_seconds)
    except (ContractError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR:{exc}", file=sys.stderr)
        if args.check_idle or not args.execute:
            return 2
        plan = {"schema": "leaf.staging-fleet-result.v1", "status": "failed",
                "reason": exc.reason if isinstance(exc, ContractError) else "FROZEN_INPUT_INVALID",
                "relay_run_id": args.relay_run_id, "children": [], "restamp_frontier": None}
    raw = json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n"
    with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(raw)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(_render_summary(plan) if "services" in plan else
                         "# Staging fleet result\n\n```json\n" + raw + "```\n")
    return 1 if plan.get("status") == "failed" else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
