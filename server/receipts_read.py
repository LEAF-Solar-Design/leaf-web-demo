"""Read the delivery receipts that ACTUALLY EXIST, and nothing else.

WHAT THIS IS
------------
One bounded, fail-closed reader over four receipt sources the platform already
mints. It fabricates nothing: a source that is not configured, not reachable,
or not present comes back as an ``unavailable`` entry naming the source and the
reason, and its rows are simply absent. There is no placeholder row, no
"pending" row, no synthesized timestamp. A timeline that shows three rows means
three receipts were read.

THE SOURCES
-----------
``prewarm-relay``  the prewarm relay's own receipt artifact,
                   ``prewarm-relay-receipt-pr-<n>`` (``leaf.staging-prewarm-relay.v1``,
                   minted by ``prewarm-staging-cutover.yml``). Looked up BY NAME
                   through the GitHub Actions artifacts API, which is why the
                   workflow deliberately keeps no run id in the name.
``gate-proof``     ``gate-proof-<tree>`` from ``test-gate.yml``: the proven-tree
                   artifact the build reuses instead of re-running eight shards.
``supply-set``     ``spec-v3-supply-set-<tree>`` from ``build-platform-images.yml``,
                   the producer-bound v3 image manifest.

PROVENANCE: A NAME IS NOT A RECEIPT
-----------------------------------
An artifact NAME is caller-chosen data, not evidence. A fork's ``pull_request``
run, a ``workflow_dispatch``, or any other workflow in this repository can
upload an artifact called ``gate-proof-<tree>`` carrying any content and any
head SHA it likes. Rendering such a row as "Gate proof" would fabricate exactly
the receipt this module exists not to fabricate, so every artifact clears three
checks -- the same three ``test-gate.yml``'s own gate-proof reuse filter runs,
which is this repository's audited consumer of these identical names -- before
one row is emitted:

  1. NOT EXPIRED. An expired artifact can no longer be downloaded, so nothing
     can ever re-verify it; it is dropped rather than rendered as proof.
  2. SAME-REPOSITORY ORIGIN. ``workflow_run.head_repository_id`` must equal the
     id of the repository this deployment names, read from ``GET /repos/<slug>``
     and never taken from the artifact. A fork run fails here.
  3. AN ALLOWLISTED MINTING WORKFLOW. ``GET /actions/runs/<id>`` gives the run's
     ``path``; only the workflows in ``MINTING_WORKFLOWS`` for that receipt kind
     may mint it. The path comes from the RUN record, never from anything inside
     the artifact, and one ``@ref`` suffix is stripped defensively because some
     GitHub surfaces render workflow paths that way.

An artifact that fails any check is simply absent -- no row, no "unverified"
row, no placeholder. The verified minting workflow is named in the row's own
summary, so a reader sees the provenance the row rests on.
``reconciler``     the receipt inbox's ``product-progress/latest.json``, read as
                   raw content over HTTPS and cached for
                   ``RECONCILER_CACHE_SECONDS``.
``job``            a per-job ``receipt.json`` sitting beside a job under
                   ``LEAF_JOB_RECEIPT_DIR`` (slice 11 writes these). Read
                   defensively: absent is normal today, malformed is skipped
                   with an honest reason, never an exception.

WHO MAY ASK
-----------
This module reads the PLATFORM's private CI state with the PLATFORM's own
credential, so the ``pr:``, ``tree:`` and ``train`` scopes are NOT tenant data
and must never be served on tenant authentication alone. The admission chain
lives at the route (``routers/change_to_live.py``), which requires the same
``platform_customize`` entitlement ``GET /api/platform/source`` requires. Only
``job:`` is tenant data, and it is bound to the calling tenant there. This
module itself trusts nobody: it is called only after that gate.

CREDENTIALS
-----------
``LEAF_PLATFORM_PR_REPO`` (owner/repo) names the repository, with
``LEAF_PLATFORM_GITHUB_API`` overriding the API root. The TOKEN is read from
``LEAF_RECEIPTS_GITHUB_TOKEN`` first and ``LEAF_PLATFORM_PR_TOKEN`` only as a
fallback, and that order is deliberate rather than tidy. The Actions artifacts
and runs APIs this module calls require ``Actions: read``. The PR PAT is pinned
by ``docs/ADMIN-SELF-EDIT-LANE.md`` to exactly two permissions, Pull-requests
read+write and Commit-statuses read, and deliberately carries neither Contents
nor Actions -- so on a runbook-correct deployment the fallback answers 403 and
these sources report ``source_unreachable`` until an operator sets the dedicated
READ-ONLY ``Actions: read`` token. That is the honest inert state, and widening
the PR PAT is explicitly NOT the fix: a third narrow token revokes independently,
which is the whole reason the lane keeps its tokens separate.

A browser token is never accepted and never consulted. When the repo or every
token is unset the artifact sources return ``source_unavailable`` naming the
missing environment variables and no rows -- they never fall back to an
unauthenticated call. The token value never enters a URL, a query string, an
exception message, a log line, or a response body: it is placed in an
``Authorization`` header and nowhere else, and the charset guard below exists so
a whitespace-bearing value cannot make urllib quote the header (token included)
into a ``ValueError`` a caller might log.

BOUNDS
------
Every HTTP call has a connect/read timeout and a hard response-byte cap, every
list is truncated to ``MAX_ROWS``, and every string field is truncated to
``MAX_FIELD``. Nothing here allocates in proportion to a remote body: the cap is
enforced by a bounded ``read(n + 1)``, not by trusting Content-Length.

The GitHub budget is bounded three ways, because this module spends a SHARED
credential whose 5000/hr also carries ``platform_customize``'s PR opening and
review observation:

  * EVERY lookup is cached. Artifact listings are cached per name for
    ``ARTIFACT_CACHE_SECONDS`` (failures included, so a 403 loop costs one call
    per minute, not one per request); the repository id and a run's workflow
    path are immutable, so both are cached for the process lifetime in
    bounded, FIFO-evicted maps.
  * Run-provenance lookups are capped at ``MAX_PROVENANCE_LOOKUPS`` per artifact
    name, applied to the NEWEST candidates after the free checks, so one request
    can never fan out to a hundred run reads.
  * ``MAX_INFLIGHT_GITHUB`` bounds how many requests may be inside a GitHub call
    at once. The cap is acquired NON-BLOCKING: over it, a caller gets an honest
    ``source_busy`` immediately instead of queueing. These routes are sync ``def``
    on purpose -- urllib blocks, so the threadpool is where it belongs -- and
    this semaphore is what stops the route from holding more than
    ``MAX_INFLIGHT_GITHUB`` threadpool slots no matter how hard it is called.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Optional

CONTRACT = "leaf.receipts-timeline.v1"

MAX_ROWS = 50
MAX_FIELD = 400
HTTP_TIMEOUT_S = 6.0
MAX_ARTIFACT_BYTES = 256 * 1024
MAX_RUN_BYTES = 256 * 1024
MAX_REPO_BYTES = 256 * 1024
MAX_RECONCILER_BYTES = 512 * 1024
RECONCILER_CACHE_SECONDS = 60.0
ARTIFACT_CACHE_SECONDS = 60.0

# At most this many run-provenance reads per artifact name, spent on the NEWEST
# candidates that already passed the two free checks. Bounds the fan-out of one
# request on a shared credential; an older candidate beyond the cap is dropped,
# never rendered unverified.
MAX_PROVENANCE_LOOKUPS = 5
# How many requests may be inside a GitHub call at once. Acquired NON-BLOCKING,
# so this caps threadpool occupancy instead of queueing behind it.
MAX_INFLIGHT_GITHUB = 8
# FIFO-evicted memo sizes. Both memoized values are immutable upstream (a
# repository id, a run's workflow path), so these never go stale, only cold.
MAX_MEMO_ENTRIES = 512

# Environment names, stated once so a refusal can quote the exact variable an
# operator must set. Same repository as platform_customize's PR follow-through;
# the TOKEN is deliberately a different, narrower one (see CREDENTIALS above).
ENV_REPO = "LEAF_PLATFORM_PR_REPO"
ENV_TOKEN = "LEAF_RECEIPTS_GITHUB_TOKEN"
ENV_FALLBACK_TOKEN = "LEAF_PLATFORM_PR_TOKEN"
ENV_API_ROOT = "LEAF_PLATFORM_GITHUB_API"
ENV_RECONCILER_URL = "LEAF_RECONCILER_RECEIPT_URL"
ENV_JOB_RECEIPT_DIR = "LEAF_JOB_RECEIPT_DIR"

REASON_NO_CREDENTIAL = "source_unavailable"
REASON_UNREACHABLE = "source_unreachable"
REASON_UNREADABLE = "receipt_unreadable"
REASON_BUSY = "source_busy"

# Which workflows may mint each receipt kind. Provenance, not decoration: an
# artifact minted by anything else is dropped even when its name matches
# exactly. Verified against the minting workflows in this repository, not from
# memory: prewarm-staging-cutover.yml uploads prewarm-relay-receipt-pr-<n>,
# test-gate.yml uploads gate-proof-<tree> (build-platform-images.yml is the
# second gate workflow test-gate.yml's own reuse filter allowlists), and
# build-platform-images.yml uploads spec-v3-supply-set-<tree>.
MINTING_WORKFLOWS: Mapping[str, frozenset] = {
    "prewarm-relay": frozenset({".github/workflows/prewarm-staging-cutover.yml"}),
    "gate-proof": frozenset({
        ".github/workflows/test-gate.yml",
        ".github/workflows/build-platform-images.yml",
    }),
    "supply-set": frozenset({".github/workflows/build-platform-images.yml"}),
}

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Printable ASCII, no spaces. Anything wider is not a GitHub token and would
# make urllib quote the whole header value -- token included -- into an error.
_PR_TOKEN_RE = re.compile(r"^[\x21-\x7E]+$")

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_PR_RE = re.compile(r"^[1-9][0-9]{0,6}$")
_JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

SCOPE_TRAIN = "train"


class ReceiptsError(ValueError):
    """A receipts request is malformed. Nothing malformed reaches a source."""


# --------------------------------------------------------------------------- #
# scope
# --------------------------------------------------------------------------- #
def parse_scope(scope: Any) -> tuple[str, str]:
    """``"pr:12"`` -> ``("pr", "12")``. Fails closed on anything else."""
    if not isinstance(scope, str):
        raise ReceiptsError("scope must be a string")
    token = scope.strip()
    if token == SCOPE_TRAIN:
        return SCOPE_TRAIN, ""
    if len(token) > 160 or ":" not in token:
        raise ReceiptsError("scope must be pr:<n>, tree:<sha>, job:<id> or train")
    kind, _, value = token.partition(":")
    if kind == "pr" and _PR_RE.fullmatch(value):
        return "pr", value
    if kind == "tree" and _SHA_RE.fullmatch(value):
        return "tree", value
    if kind == "job" and _JOB_RE.fullmatch(value):
        return "job", value
    raise ReceiptsError("scope must be pr:<n>, tree:<sha>, job:<id> or train")


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
def github_credentials() -> tuple[str, str] | str:
    """``(slug, token)`` when the platform credential is usable, else a reason
    code. The token is RETURNED, never logged, never formatted into a message.

    ``LEAF_RECEIPTS_GITHUB_TOKEN`` (Actions: read) wins over the PR PAT, which
    is a documented fallback that will 403 on a runbook-correct deployment. A
    token present but unusable (whitespace, wide bytes) does NOT fall through to
    the next one: a misconfigured credential is a refusal, not a hint to try
    another identity.
    """
    slug = os.environ.get(ENV_REPO, "").strip()
    if not slug or _REPO_SLUG_RE.fullmatch(slug) is None:
        return REASON_NO_CREDENTIAL
    for name in (ENV_TOKEN, ENV_FALLBACK_TOKEN):
        token = os.environ.get(name, "").strip()
        if not token:
            continue
        if _PR_TOKEN_RE.fullmatch(token) is None:
            return REASON_NO_CREDENTIAL
        return slug, token
    return REASON_NO_CREDENTIAL


def _api_root() -> str:
    return (os.environ.get(ENV_API_ROOT, "").strip() or "https://api.github.com").rstrip("/")


def _credential_detail() -> str:
    if not os.environ.get(ENV_REPO, "").strip():
        return f"{ENV_REPO} is not configured on this deployment"
    if not any(os.environ.get(name, "").strip() for name in (ENV_TOKEN, ENV_FALLBACK_TOKEN)):
        return (
            f"{ENV_TOKEN} is not configured on this deployment "
            f"(it needs GitHub 'Actions: read' and nothing else)"
        )
    return f"{ENV_REPO} or {ENV_TOKEN} is configured with an unusable value"


# --------------------------------------------------------------------------- #
# bounded HTTP
# --------------------------------------------------------------------------- #
def _get_json(url: str, *, headers: Mapping[str, str], cap: int) -> Any:
    """One bounded GET. Raises OSError/ValueError; never leaks a header value."""
    request = urllib.request.Request(url, method="GET", headers=dict(headers))
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
        # read(cap + 1) so an oversized body is DETECTED rather than silently
        # truncated into malformed JSON.
        payload = response.read(cap + 1)
    if len(payload) > cap:
        raise ValueError("response exceeded the size cap")
    return json.loads(payload.decode("utf-8"))


def _github_get(path_and_query: str, cap: int) -> Any:
    """One authenticated GitHub GET. Raises; the caller turns that into a reason.

    Fails closed on the credential and NEVER makes an unauthenticated call: a
    caller reaches here only after ``github_credentials`` returned a pair.
    """
    creds = github_credentials()
    if isinstance(creds, str):
        raise _CredentialUnavailable(creds)
    slug, token = creds
    return _get_json(
        f"{_api_root()}{path_and_query.replace('<slug>', slug)}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "leaf-platform-receipts",
        },
        cap=cap,
    )


class _CredentialUnavailable(RuntimeError):
    """The credential vanished mid-read. Carries a reason code, never a value."""


# --------------------------------------------------------------------------- #
# bounded memos and the inflight cap
# --------------------------------------------------------------------------- #
_github_lock = threading.Lock()
# name -> (monotonic_at, rows, unavailable). Failures are cached too, so a 403
# loop costs one call per ARTIFACT_CACHE_SECONDS rather than one per request.
_artifact_cache: dict[str, tuple] = {}
# slug -> repository id, and run id -> workflow path. Both immutable upstream.
_repo_id_memo: dict[str, int] = {}
_run_path_memo: dict[int, str] = {}
_inflight = threading.BoundedSemaphore(MAX_INFLIGHT_GITHUB)


def _memo_put(memo: dict, key: Any, value: Any) -> None:
    """FIFO-evicting put. Bounded so a hostile scope walk cannot grow a map."""
    with _github_lock:
        if key not in memo and len(memo) >= MAX_MEMO_ENTRIES:
            memo.pop(next(iter(memo)))
        memo[key] = value


def reset_github_caches() -> None:
    """Drop every GitHub-side memo. For tests and an operator-forced re-read."""
    with _github_lock:
        _artifact_cache.clear()
        _repo_id_memo.clear()
        _run_path_memo.clear()


def _repository_id() -> Optional[int]:
    """This deployment's repository id, from ``GET /repos/<slug>``.

    The provenance anchor. ``None`` means "cannot be established", and a caller
    that cannot establish it renders NO artifact rows: an unverifiable artifact
    is absent, never optimistically trusted.
    """
    creds = github_credentials()
    if isinstance(creds, str):
        return None
    slug = creds[0]
    with _github_lock:
        cached = _repo_id_memo.get(slug)
    if cached is not None:
        return cached
    try:
        body = _github_get("/repos/<slug>", MAX_REPO_BYTES)
    except (OSError, urllib.error.HTTPError, ValueError, _CredentialUnavailable):
        return None
    repo_id = body.get("id") if isinstance(body, Mapping) else None
    if not isinstance(repo_id, int) or isinstance(repo_id, bool):
        return None
    _memo_put(_repo_id_memo, slug, repo_id)
    return repo_id


def _run_workflow_path(run_id: int) -> Optional[str]:
    """The minting workflow's path for one run, from the RUN record.

    Never read from inside the artifact. One ``@ref`` suffix is stripped because
    some GitHub surfaces render workflow paths that way; the match after that is
    exact, so a near-miss path is a refusal rather than a prefix hit.
    """
    with _github_lock:
        cached = _run_path_memo.get(run_id)
    if cached is not None:
        return cached
    try:
        body = _github_get(f"/repos/<slug>/actions/runs/{int(run_id)}", MAX_RUN_BYTES)
    except (OSError, urllib.error.HTTPError, ValueError, _CredentialUnavailable):
        return None
    path = body.get("path") if isinstance(body, Mapping) else None
    if not isinstance(path, str) or not path:
        return None
    path = path.split("@", 1)[0]
    _memo_put(_run_path_memo, run_id, path)
    return path


def _same_repository(artifact: Mapping[str, Any], repo_id: int) -> bool:
    """True only when the run that minted this artifact ran on OUR head repo.

    A fork's ``pull_request`` run uploads with ``head_repository_id`` set to the
    FORK, which is the whole attack this check closes. A missing or non-integer
    id fails closed.
    """
    run = artifact.get("workflow_run")
    if not isinstance(run, Mapping):
        return False
    head = run.get("head_repository_id")
    if not isinstance(head, int) or isinstance(head, bool) or head != repo_id:
        return False
    owner = run.get("repository_id")
    # Present-and-wrong is a refusal; absent is tolerated, because the listing
    # is already scoped to this repository's own artifacts endpoint.
    if owner is not None and (not isinstance(owner, int) or isinstance(owner, bool)
                              or owner != repo_id):
        return False
    return True


def _fetch_artifacts_named(
    name: str, row_kind: str
) -> tuple[list[tuple[dict, str]], Optional[dict[str, str]]]:
    """The artifacts named EXACTLY this that clear provenance. ``(pairs, unavailable)``.

    Each pair is ``(artifact, verified_workflow_path)``; nothing reaches a row
    without one. Cached for ``ARTIFACT_CACHE_SECONDS`` per name, failures
    included, and bounded by the inflight cap.
    """
    creds = github_credentials()
    if isinstance(creds, str):
        return [], _unavailable("github-artifacts", creds, _credential_detail())

    now = time.monotonic()
    with _github_lock:
        entry = _artifact_cache.get(name)
        fresh = entry is not None and (now - entry[0]) < ARTIFACT_CACHE_SECONDS
    if fresh:
        # Copied out: a cached entry is shared across every concurrent reader,
        # so a caller must never be handed a reference it could mutate.
        return [(dict(art), path) for art, path in entry[1]], (
            dict(entry[2]) if entry[2] else None)

    if not _inflight.acquire(blocking=False):
        # Over the cap. An honest refusal now beats holding a threadpool slot.
        return [], _unavailable(
            "github-artifacts", REASON_BUSY,
            "too many receipt reads are already in flight; try again shortly",
        )
    try:
        found, missing = _fetch_artifacts_verified(name, row_kind)
    finally:
        _inflight.release()
    _memo_put(_artifact_cache, name, (time.monotonic(), tuple(found), missing))
    return found, missing


def _fetch_artifacts_verified(
    name: str, row_kind: str
) -> tuple[list[tuple[dict, str]], Optional[dict[str, str]]]:
    """The uncached read: list, then drop everything that fails provenance."""
    repo_id = _repository_id()
    if repo_id is None:
        return [], _unavailable(
            "github-artifacts", REASON_UNREACHABLE,
            "this deployment's repository identity could not be read, so no "
            "artifact could be checked for same-repository origin",
        )
    quoted = urllib.parse.quote(name, safe="")
    try:
        body = _github_get(
            f"/repos/<slug>/actions/artifacts?per_page=100&name={quoted}",
            MAX_ARTIFACT_BYTES,
        )
    except _CredentialUnavailable as exc:
        return [], _unavailable("github-artifacts", str(exc), _credential_detail())
    except (OSError, urllib.error.HTTPError, ValueError):
        # Deliberately no exception text: the request carried the credential,
        # and a urllib error can quote the request back at us.
        return [], _unavailable(
            "github-artifacts", REASON_UNREACHABLE,
            f"the GitHub artifacts API did not answer for {name!r}",
        )
    artifacts = body.get("artifacts") if isinstance(body, Mapping) else None
    if not isinstance(artifacts, list):
        return [], _unavailable(
            "github-artifacts", REASON_UNREADABLE,
            "the GitHub artifacts API returned an unexpected shape",
        )

    allowed = MINTING_WORKFLOWS.get(row_kind, frozenset())
    # The two FREE checks first (no extra request), newest first, so the capped
    # run lookups are spent on the candidates a reader would actually see.
    candidates = [
        item for item in artifacts
        if isinstance(item, Mapping)
        and not item.get("expired")
        and _same_repository(item, repo_id)
    ]
    candidates.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)

    verified: list[tuple[dict, str]] = []
    for item in candidates[:MAX_PROVENANCE_LOOKUPS]:
        run = item.get("workflow_run")
        run_id = run.get("id") if isinstance(run, Mapping) else None
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            continue
        path = _run_workflow_path(run_id)
        if path is None or path not in allowed:
            # Minted by something that may not mint this receipt (or by a run
            # we cannot identify). Absent, never rendered unverified.
            continue
        verified.append((dict(item), path))
    return verified, None


def _artifact_row(pair: tuple[Mapping[str, Any], str], kind: str, ref: str) -> dict[str, str]:
    """One row from an artifact that ALREADY cleared provenance in the fetch.

    The verified minting workflow rides in the summary, so the row states the
    evidence it rests on rather than asking a reader to assume it.
    """
    artifact, workflow = pair
    run = artifact.get("workflow_run")
    run = run if isinstance(run, Mapping) else {}
    name = _text(artifact.get("name"))
    return {
        "kind": kind,
        "ref": ref,
        "at": _text(artifact.get("created_at")),
        "sha": _text(run.get("head_sha")),
        # Truncated AFTER composition: `name` is already bounded, but the
        # composed sentence must be bounded too or the cap leaks by its suffix.
        "summary": _text(
            f"{name} ({_bytes_summary(artifact.get('size_in_bytes'))}), "
            f"minted by {workflow.rsplit('/', 1)[-1]}"
        ),
        "url": _run_url(artifact, run),
    }


def _run_url(artifact: Mapping[str, Any], run: Mapping[str, Any]) -> str:
    run_id = run.get("id")
    creds = github_credentials()
    if isinstance(creds, tuple) and isinstance(run_id, int) and run_id > 0:
        return f"https://github.com/{creds[0]}/actions/runs/{run_id}"
    return _text(artifact.get("archive_download_url"))


def _bytes_summary(size: Any) -> str:
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return "size unknown"
    if size < 1024:
        return f"{size} B"
    return f"{size // 1024} KiB"


# --------------------------------------------------------------------------- #
# the reconciler's latest.json, cached
# --------------------------------------------------------------------------- #
_reconciler_lock = threading.Lock()
_reconciler_cache: dict[str, Any] = {"at": 0.0, "url": "", "value": None}


def _fetch_reconciler() -> tuple[list[dict[str, str]], Optional[dict[str, str]]]:
    """``receipt-inbox/product-progress/latest.json`` from the plan repository.

    The URL is configured rather than guessed: pointing this at the wrong
    repository would either 404 forever or, worse, render another project's
    receipts as this platform's. Unset -> an honest unavailable naming the
    variable, which is the correct state on a deployment that has not wired the
    receipt inbox.
    """
    url = os.environ.get(ENV_RECONCILER_URL, "").strip()
    if not url:
        return [], _unavailable(
            "reconciler", REASON_NO_CREDENTIAL,
            f"{ENV_RECONCILER_URL} is not configured on this deployment",
        )
    if not url.startswith("https://"):
        return [], _unavailable(
            "reconciler", REASON_NO_CREDENTIAL,
            f"{ENV_RECONCILER_URL} must be an https URL",
        )

    now = time.monotonic()
    with _reconciler_lock:
        cached = _reconciler_cache
        fresh = (
            cached["value"] is not None
            and cached["url"] == url
            and (now - float(cached["at"])) < RECONCILER_CACHE_SECONDS
        )
        value = cached["value"] if fresh else None
    if value is None:
        try:
            value = _get_json(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "leaf-platform-receipts",
                },
                cap=MAX_RECONCILER_BYTES,
            )
        except (OSError, urllib.error.HTTPError, ValueError):
            return [], _unavailable(
                "reconciler", REASON_UNREACHABLE,
                "the receipt inbox did not answer",
            )
        with _reconciler_lock:
            _reconciler_cache.update({"at": now, "url": url, "value": value})

    if not isinstance(value, Mapping):
        return [], _unavailable(
            "reconciler", REASON_UNREADABLE,
            "the receipt inbox returned an unexpected shape",
        )
    entries = value.get("receipts")
    if not isinstance(entries, list):
        entries = [value]
    rows = []
    for entry in entries[:MAX_ROWS]:
        if not isinstance(entry, Mapping):
            continue
        rows.append({
            "kind": "reconciler",
            "ref": SCOPE_TRAIN,
            "at": _text(entry.get("at") or entry.get("reconciled_at") or entry.get("updated_at")),
            "sha": _text(entry.get("sha") or entry.get("commit")),
            "summary": _text(
                entry.get("summary") or entry.get("program") or entry.get("status")
                or "product-progress receipt"
            ),
            "url": _text(entry.get("url") or url),
        })
    return rows, None


def reset_reconciler_cache() -> None:
    """Drop the 60 s cache. For tests and for an operator-forced re-read."""
    with _reconciler_lock:
        _reconciler_cache.update({"at": 0.0, "url": "", "value": None})


# --------------------------------------------------------------------------- #
# per-job receipt.json
# --------------------------------------------------------------------------- #
def _fetch_job_receipt(job_id: str) -> tuple[list[dict[str, str]], Optional[dict[str, str]]]:
    """Read ``<LEAF_JOB_RECEIPT_DIR>/<job_id>/receipt.json`` if it is there.

    Slice 11 writes these; today the directory is usually unset, and that is a
    normal empty answer, not a failure. The job id is validated by
    ``parse_scope`` before it reaches here, so it cannot contain a separator or
    a traversal segment, and the resolved path is re-checked against the root.
    """
    root_raw = os.environ.get(ENV_JOB_RECEIPT_DIR, "").strip()
    if not root_raw:
        return [], _unavailable(
            "job-receipt", REASON_NO_CREDENTIAL,
            f"{ENV_JOB_RECEIPT_DIR} is not configured on this deployment",
        )
    try:
        root = Path(root_raw).resolve(strict=False)
        candidate = (root / job_id / "receipt.json").resolve(strict=False)
        if root not in candidate.parents:
            return [], None
        if not candidate.is_file():
            return [], None
        raw = candidate.read_bytes()[: MAX_RECONCILER_BYTES + 1]
        if len(raw) > MAX_RECONCILER_BYTES:
            return [], _unavailable(
                "job-receipt", REASON_UNREADABLE,
                f"the receipt for job {job_id} exceeds the size cap",
            )
        value = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return [], _unavailable(
            "job-receipt", REASON_UNREADABLE,
            f"the receipt for job {job_id} could not be read",
        )
    if not isinstance(value, Mapping):
        return [], _unavailable(
            "job-receipt", REASON_UNREADABLE,
            f"the receipt for job {job_id} is not an object",
        )
    return [{
        "kind": "job",
        "ref": f"job:{job_id}",
        "at": _text(value.get("at") or value.get("completed_at") or value.get("finished_at")),
        "sha": _text(value.get("sha") or value.get("commit")),
        "summary": _text(value.get("summary") or value.get("status") or "job receipt"),
        "url": _text(value.get("url")),
    }], None


# --------------------------------------------------------------------------- #
# the timeline
# --------------------------------------------------------------------------- #
def read_receipts(scope: str) -> dict[str, Any]:
    """Every receipt that exists for one scope, newest first.

    Never raises for a source that is missing, unset or unreachable: that comes
    back in ``unavailable`` with the source, a reason code and one honest
    sentence. Raises ``ReceiptsError`` only for a malformed scope.
    """
    kind, value = parse_scope(scope)
    rows: list[dict[str, str]] = []
    unavailable: list[dict[str, str]] = []

    def take(result: tuple[list[Any], Optional[dict[str, str]]], row_kind: str, ref: str,
             *, raw_artifacts: bool = True) -> None:
        """``raw_artifacts`` means the items are provenance-verified
        ``(artifact, workflow_path)`` pairs needing composition into a row; the
        non-artifact sources already produce finished rows."""
        found, missing = result
        if missing is not None:
            unavailable.append(missing)
        for item in found:
            rows.append(_artifact_row(item, row_kind, ref) if raw_artifacts else item)

    if kind == "pr":
        take(_fetch_artifacts_named(f"prewarm-relay-receipt-pr-{value}", "prewarm-relay"),
             "prewarm-relay", f"pr:{value}")
    elif kind == "tree":
        take(_fetch_artifacts_named(f"gate-proof-{value}", "gate-proof"),
             "gate-proof", f"tree:{value}")
        take(_fetch_artifacts_named(f"spec-v3-supply-set-{value}", "supply-set"),
             "supply-set", f"tree:{value}")
    elif kind == "job":
        take(_fetch_job_receipt(value), "job", f"job:{value}", raw_artifacts=False)
    else:
        take(_fetch_reconciler(), "reconciler", SCOPE_TRAIN, raw_artifacts=False)

    rows.sort(key=lambda row: row.get("at") or "", reverse=True)
    return {
        "contract": CONTRACT,
        "scope": f"{kind}:{value}" if value else kind,
        "rows": rows[:MAX_ROWS],
        "unavailable": unavailable,
    }


def _unavailable(source: str, reason: str, detail: str) -> dict[str, str]:
    return {"source": source, "reason": reason, "detail": _text(detail)}


def _text(value: Any) -> str:
    """One bounded display string. Never a dict, never an unbounded blob."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value)[:MAX_FIELD]


__all__ = [
    "CONTRACT",
    "ENV_API_ROOT",
    "ENV_FALLBACK_TOKEN",
    "ENV_JOB_RECEIPT_DIR",
    "ENV_RECONCILER_URL",
    "ENV_REPO",
    "ENV_TOKEN",
    "MAX_INFLIGHT_GITHUB",
    "MAX_PROVENANCE_LOOKUPS",
    "MAX_ROWS",
    "MINTING_WORKFLOWS",
    "REASON_BUSY",
    "REASON_NO_CREDENTIAL",
    "REASON_UNREACHABLE",
    "REASON_UNREADABLE",
    "ReceiptsError",
    "github_credentials",
    "parse_scope",
    "read_receipts",
    "reset_github_caches",
    "reset_reconciler_cache",
]
