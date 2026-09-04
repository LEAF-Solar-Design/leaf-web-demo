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
``reconciler``     the receipt inbox's ``product-progress/latest.json``, read as
                   raw content over HTTPS and cached for
                   ``RECONCILER_CACHE_SECONDS``.
``job``            a per-job ``receipt.json`` sitting beside a job under
                   ``LEAF_JOB_RECEIPT_DIR`` (slice 11 writes these). Read
                   defensively: absent is normal today, malformed is skipped
                   with an honest reason, never an exception.

CREDENTIALS
-----------
The GitHub reads use the PLATFORM's own server-side credential, the same pair
every other server-side GitHub call reads: ``LEAF_PLATFORM_PR_REPO`` (owner/repo)
and ``LEAF_PLATFORM_PR_TOKEN``, with ``LEAF_PLATFORM_GITHUB_API`` overriding the
API root (see ``platform_customize._pr_credentials``). A browser token is never
accepted and never consulted. When either is unset the artifact sources return
``source_unavailable`` naming the missing environment variable and no rows --
they never fall back to an unauthenticated call. The token value never enters a
URL, a query string, an exception message, a log line, or a response body: it is
placed in an ``Authorization`` header and nowhere else, and the charset guard
below exists so a whitespace-bearing value cannot make urllib quote the header
(token included) into a ``ValueError`` a caller might log.

BOUNDS
------
Every HTTP call has a connect/read timeout and a hard response-byte cap, every
list is truncated to ``MAX_ROWS``, and every string field is truncated to
``MAX_FIELD``. Nothing here allocates in proportion to a remote body: the cap is
enforced by a bounded ``read(n + 1)``, not by trusting Content-Length.
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
MAX_RECONCILER_BYTES = 512 * 1024
RECONCILER_CACHE_SECONDS = 60.0

# Environment names, stated once so a refusal can quote the exact variable an
# operator must set. Same store as platform_customize's PR follow-through.
ENV_REPO = "LEAF_PLATFORM_PR_REPO"
ENV_TOKEN = "LEAF_PLATFORM_PR_TOKEN"
ENV_API_ROOT = "LEAF_PLATFORM_GITHUB_API"
ENV_RECONCILER_URL = "LEAF_RECONCILER_RECEIPT_URL"
ENV_JOB_RECEIPT_DIR = "LEAF_JOB_RECEIPT_DIR"

REASON_NO_CREDENTIAL = "source_unavailable"
REASON_UNREACHABLE = "source_unreachable"
REASON_UNREADABLE = "receipt_unreadable"

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
    code. The token is RETURNED, never logged, never formatted into a message."""
    slug = os.environ.get(ENV_REPO, "").strip()
    token = os.environ.get(ENV_TOKEN, "").strip()
    if not slug or not token:
        return REASON_NO_CREDENTIAL
    if _REPO_SLUG_RE.fullmatch(slug) is None:
        return REASON_NO_CREDENTIAL
    if _PR_TOKEN_RE.fullmatch(token) is None:
        return REASON_NO_CREDENTIAL
    return slug, token


def _api_root() -> str:
    return (os.environ.get(ENV_API_ROOT, "").strip() or "https://api.github.com").rstrip("/")


def _credential_detail() -> str:
    missing = [name for name in (ENV_REPO, ENV_TOKEN) if not os.environ.get(name, "").strip()]
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        return f"{' and '.join(missing)} {verb} not configured on this deployment"
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


def _fetch_artifacts_named(name: str) -> tuple[list[dict[str, Any]], Optional[dict[str, str]]]:
    """List the artifacts with EXACTLY this name. ``(rows, unavailable)``."""
    creds = github_credentials()
    if isinstance(creds, str):
        return [], _unavailable("github-artifacts", creds, _credential_detail())
    slug, token = creds
    url = (
        f"{_api_root()}/repos/{slug}/actions/artifacts"
        f"?per_page=100&name={urllib.parse.quote(name, safe='')}"
    )
    try:
        body = _get_json(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "leaf-platform-receipts",
            },
            cap=MAX_ARTIFACT_BYTES,
        )
    except (OSError, urllib.error.HTTPError, ValueError):
        # Deliberately no exception text: the request carried the credential,
        # and a urllib error can quote the request back at us.
        return [], _unavailable(
            "github-artifacts", REASON_UNREACHABLE,
            f"the GitHub artifacts API did not answer for {name!r}",
        )
    artifacts = body.get("artifacts") if isinstance(body, dict) else None
    if not isinstance(artifacts, list):
        return [], _unavailable(
            "github-artifacts", REASON_UNREADABLE,
            "the GitHub artifacts API returned an unexpected shape",
        )
    return [item for item in artifacts[:MAX_ROWS] if isinstance(item, dict)], None


def _artifact_row(artifact: Mapping[str, Any], kind: str, ref: str) -> dict[str, str]:
    run = artifact.get("workflow_run")
    run = run if isinstance(run, Mapping) else {}
    sha = _text(run.get("head_sha"))
    expired = bool(artifact.get("expired"))
    name = _text(artifact.get("name"))
    return {
        "kind": kind,
        "ref": ref,
        "at": _text(artifact.get("created_at")),
        "sha": sha,
        # Truncated AFTER composition: `name` is already bounded, but the
        # composed sentence must be bounded too or the cap leaks by its suffix.
        "summary": _text(
            f"{name} ({_bytes_summary(artifact.get('size_in_bytes'))})"
            + (", expired" if expired else "")
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

    def take(result: tuple[list[dict[str, Any]], Optional[dict[str, str]]], row_kind: str, ref: str,
             *, raw_artifacts: bool = True) -> None:
        found, missing = result
        if missing is not None:
            unavailable.append(missing)
        for item in found:
            rows.append(_artifact_row(item, row_kind, ref) if raw_artifacts else item)

    if kind == "pr":
        take(_fetch_artifacts_named(f"prewarm-relay-receipt-pr-{value}"),
             "prewarm-relay", f"pr:{value}")
    elif kind == "tree":
        take(_fetch_artifacts_named(f"gate-proof-{value}"), "gate-proof", f"tree:{value}")
        take(_fetch_artifacts_named(f"spec-v3-supply-set-{value}"), "supply-set", f"tree:{value}")
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
    "ENV_JOB_RECEIPT_DIR",
    "ENV_RECONCILER_URL",
    "ENV_REPO",
    "ENV_TOKEN",
    "MAX_ROWS",
    "REASON_NO_CREDENTIAL",
    "REASON_UNREACHABLE",
    "REASON_UNREADABLE",
    "ReceiptsError",
    "github_credentials",
    "parse_scope",
    "read_receipts",
    "reset_reconciler_cache",
]
