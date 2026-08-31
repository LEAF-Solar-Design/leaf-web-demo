#!/usr/bin/env python3
"""Prune stale CodeQL overlay-base databases out of the Actions cache bucket.

WHY THIS EXISTS
---------------
The repo's Actions cache bucket is a single shared 10 GiB LRU. On 2026-08-31 it
sat at 9,948,846,841 B (92.7% of the limit) across 194 entries, and 170 of those
entries (7.74 GiB, 87%) were CodeQL overlay-base databases. Nothing pruned them:
CodeQL default setup writes 4 fresh ones on every analysed commit (~1.4 GB/day
measured over the 2026-08-24..08-31 window) and they leave only by GitHub's
7-day idle TTL or by LRU eviction.

That crowding is not cosmetic. `.github/workflows/test-gate.yml` -- the merge
gate -- restores one 279 MB entry, key `playwright-chromium-Linux-125d47d2...`,
on refs/heads/main. All 8 shards read it every run, and that read traffic is the
only thing keeping it at the top of the LRU. If CodeQL churn evicts it, shard 7
(the critical path, ~155s with ~58s of slack) pays a ~33s cold save and all 8
shards re-download Chromium at ~9s each. So the gate's headroom depends on this
bucket having room.

WHY PRUNING IS THE FIX, AND NOT REDUCING PRODUCTION
---------------------------------------------------
CodeQL here is GitHub *default setup*, not a workflow file, so there is no
analysis workflow in this repo to tune. The two source-side levers both come up
empty:

  * The `schedule` is already `weekly`. The churn is push/PR-driven, not
    schedule-driven, so loosening it saves nothing.
  * The configured language list reads `actions, c-cpp, csharp, javascript,
    javascript-typescript, python, typescript` -- 7 entries. `javascript` and
    `typescript` look redundant with `javascript-typescript`, but they are
    aliases that already collapse into ONE analysis: the live cache holds
    exactly one javascript group hash (c801913f1ee29663), not three. Removing
    them would be cosmetic and would save zero bytes. Cutting a real language
    would cut scanning coverage, which is out of scope.

So the durable fix is to prune, on a schedule, the entries that are already
unreachable. This is not a one-off cleanup: `.github/workflows/prune-codeql-
caches.yml` runs it every 6 hours, which bounds accumulation between runs to
roughly 350 MB.

WHY DELETING THESE IS SAFE, FROM THE CODEQL ACTION SOURCE
----------------------------------------------------------
Confirmed against github/codeql-action `main`, not assumed:

  * `src/overlay/caching.ts:getCacheRestoreKeyPrefix()` returns a restore key
    prefix that deliberately EXCLUDES the commit SHA -- its own comment says
    this "allows us to restore the most recent compatible overlay-base
    database". Actions partial-key restore returns the newest entry matching the
    prefix. So for a given (cache version, components hash, languages, CodeQL
    CLI version) the newest entry is the ONLY one that can ever be restored.
    Every older SHA in that group is already unreachable dead weight.
  * `src/init-action.ts:492-498` -- when the download returns nothing, the
    action sets `config.overlayDatabaseMode = OverlayDatabaseMode.None` and logs
    "No overlay-base database found in cache, reverting overlay database mode to
    none." A missing cache is a slower cold analysis, never a failed one and
    never a narrower one. Alerts and coverage are unaffected.

That is the whole safety argument: we delete only entries that partial-key
restore can no longer reach, and even a wrong deletion would cost time, not
correctness.

WHAT THIS WILL AND WILL NOT DELETE
-----------------------------------
Deletion is WHITELISTED, not blacklisted. An entry is a deletion candidate only
if its key passes `parse_overlay_base_key()` -- the strict
`codeql-overlay-base-database-<ver>-<hash>-<langs>-<cliver>-<sha>-<runid>-
<attempt>` shape, with the trailing three fields validated as hex SHA / digits /
digits. Everything else is kept by construction, with no enumeration of
protected names needed: `playwright-chromium-*`, `node-cache-*`, `setup-python-*`,
`cache-trivy-*` and anything added later are all outside the whitelist and are
never candidates. `test_never_deletes_a_non_codeql_key` pins that.

FAIL-CLOSED GUARDS (all raise PruneRefused and delete NOTHING)
---------------------------------------------------------------
  G1  Every id in the delete set re-validates as an overlay-base key on a
      second, independent pass. A selection bug cannot smuggle an entry past
      this even if it corrupts the grouping.
  G2  len(delete) <= max_deletes. A runaway backstop, not the primary guard --
      G1/G3/G4 are. Default 500 covers the ~154-entry first run with margin.
  G3  len(delete) < len(entries). Never empty the bucket.
  G4  Every group that had any entry retains min(keep, group size) survivors.

Cost: O(n log n) in the number of cache entries (n ~ 200; the sort dominates and
is microseconds). The real cost is one DELETE round trip per pruned entry --
~154 on the first run, ~15 in steady state -- issued sequentially with a
per-call timeout. There is no batch-delete endpoint; sequential is deliberate at
this volume and stated so nobody "optimises" it into an unbounded thread pool.

Exit-code contract: 0 = plan computed and (unless --dry-run) applied; 1 = a
guard refused or a delete failed. Both directions are covered by
scripts/test_prune_codeql_caches.py, registered as the `prune-codeql-caches`
suite in scripts/run-all-gates.py.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

# Distinguishes overlay-base databases from every other cache object. Mirrors
# CACHE_PREFIX in github/codeql-action src/overlay/caching.ts.
OVERLAY_BASE_PREFIX = "codeql-overlay-base-database-"

# The key's trailing three fields, appended by getCacheKey() as
# `${restoreKeyPrefix}${sha}-${runId}-${attemptId}`. Validating their SHAPE (not
# merely their count) is what makes the whitelist strict: a key that merely
# starts with the prefix but does not end in hex-digits-digits is NOT a
# candidate, and is kept.
_TRAILER = re.compile(r"^(?P<sha>[0-9a-f]{7,64})-(?P<run_id>\d+)-(?P<attempt>\d+)$")

# Backstop only. The primary guards are G1/G3/G4 in plan_prune().
DEFAULT_MAX_DELETES = 500

# Two, not one. Only the newest entry per group is ever restored, so one would
# be exactly correct -- the spare absorbs the window where a run has written a
# newer entry that is still uploading, at a cost of ~350 MB total across the 8
# live groups.
DEFAULT_KEEP_PER_GROUP = 2


class PruneRefused(RuntimeError):
    """A fail-closed guard tripped. Nothing has been deleted."""


@dataclass(frozen=True)
class CacheEntry:
    """One Actions cache entry, as returned by GET /actions/caches."""

    id: int
    key: str
    size_in_bytes: int
    created_at: str
    ref: str = ""

    @staticmethod
    def from_api(obj: dict) -> "CacheEntry":
        return CacheEntry(
            id=int(obj["id"]),
            key=str(obj["key"]),
            size_in_bytes=int(obj.get("size_in_bytes") or 0),
            created_at=str(obj.get("created_at") or ""),
            ref=str(obj.get("ref") or ""),
        )


@dataclass
class PrunePlan:
    keep: list[CacheEntry] = field(default_factory=list)
    delete: list[CacheEntry] = field(default_factory=list)

    @property
    def bytes_freed(self) -> int:
        return sum(e.size_in_bytes for e in self.delete)


def parse_overlay_base_key(key: str) -> str | None:
    """Return the restore-key prefix (the group) for an overlay-base key.

    The group is the key with the trailing `<sha>-<runId>-<attemptId>` removed,
    i.e. exactly the prefix getCacheRestoreKeyPrefix() builds. Entries sharing a
    group compete for the same partial-key restore, and only the newest wins.

    Returns None for anything that is not a strictly-shaped overlay-base key --
    which is what keeps every other cache family out of the delete set. Trailing
    fields are split from the RIGHT so a language component containing a dash
    could never shift the parse.
    """
    if not key.startswith(OVERLAY_BASE_PREFIX):
        return None
    parts = key.rsplit("-", 3)
    if len(parts) != 4:
        return None
    head = parts[0]
    if not _TRAILER.match("-".join(parts[1:])):
        return None
    # A group must carry something past the prefix (cache version, components
    # hash, languages, CLI version). A bare prefix is malformed; keep it rather
    # than group everything under "".
    if not head.startswith(OVERLAY_BASE_PREFIX) or head == OVERLAY_BASE_PREFIX:
        return None
    if not head[len(OVERLAY_BASE_PREFIX):]:
        return None
    return head


def plan_prune(
    entries: list[CacheEntry],
    keep_per_group: int = DEFAULT_KEEP_PER_GROUP,
    max_deletes: int = DEFAULT_MAX_DELETES,
) -> PrunePlan:
    """Decide what to delete. Pure: no network, no clock, no filesystem.

    Raises PruneRefused if any fail-closed guard trips, in which case the caller
    must delete nothing.
    """
    if keep_per_group < 1:
        raise PruneRefused(f"keep_per_group must be >= 1, got {keep_per_group}")

    groups: dict[str, list[CacheEntry]] = {}
    plan = PrunePlan()

    for entry in entries:
        group = parse_overlay_base_key(entry.key)
        if group is None:
            plan.keep.append(entry)  # Not a candidate. Kept by construction.
            continue
        groups.setdefault(group, []).append(entry)

    for group, members in groups.items():
        # Newest first. created_at is RFC3339 from the API and sorts
        # lexicographically; id descending breaks ties deterministically so the
        # same input always yields the same plan.
        members.sort(key=lambda e: (e.created_at, e.id), reverse=True)
        plan.keep.extend(members[:keep_per_group])
        plan.delete.extend(members[keep_per_group:])

    enforce_guards(entries, groups, plan, keep_per_group, max_deletes)
    return plan


def enforce_guards(
    entries: list[CacheEntry],
    groups: dict[str, list[CacheEntry]],
    plan: PrunePlan,
    keep_per_group: int,
    max_deletes: int,
) -> None:
    """Raise PruneRefused unless the plan satisfies every fail-closed guard.

    Split out of plan_prune() so the tests can drive each guard with a
    deliberately sabotaged plan. G3 and G4 are unreachable through plan_prune()
    with keep_per_group >= 1 -- that is exactly why they are here, as backstops
    against a future edit to the selection pass, and exactly why they need a
    negative control that can call them directly. A guard nobody has watched
    fail is not a guard.
    """
    # G1: re-validate independently of the grouping pass.
    smuggled = [e for e in plan.delete if parse_overlay_base_key(e.key) is None]
    if smuggled:
        raise PruneRefused(
            "G1: delete set contains non-overlay-base keys: "
            + ", ".join(sorted(e.key for e in smuggled)[:5])
        )

    # G2: runaway backstop.
    if len(plan.delete) > max_deletes:
        raise PruneRefused(
            f"G2: would delete {len(plan.delete)} entries, above the "
            f"--max-deletes backstop of {max_deletes}"
        )

    # G3: never empty the bucket.
    if entries and len(plan.delete) >= len(entries):
        raise PruneRefused(
            f"G3: would delete all {len(plan.delete)} of {len(entries)} entries"
        )

    # G4: every non-empty group keeps its survivors.
    kept_ids = {e.id for e in plan.keep}
    for group, members in groups.items():
        survivors = sum(1 for e in members if e.id in kept_ids)
        expected = min(keep_per_group, len(members))
        if survivors != expected:
            raise PruneRefused(
                f"G4: group {group} kept {survivors} of an expected {expected}"
            )


# --------------------------------------------------------------------------- #
# I/O. Kept strictly out of the decision logic above so the tests never need a
# network, a token, or a fake server.
# --------------------------------------------------------------------------- #


class GhError(RuntimeError):
    def __init__(self, args: list[str], stderr: str) -> None:
        super().__init__(f"gh {' '.join(args)} failed: {stderr.strip()}")
        self.stderr = stderr


def is_not_found(stderr: str) -> bool:
    """True when gh reported HTTP 404 for a cache id.

    A cache entry can vanish between the listing and its DELETE -- GitHub's own
    LRU evicts under pressure, and a concurrent run can prune the same id. That
    is the DESIRED end state reached by another route, not a failure, so the
    prune treats it as idempotent success. Without this, a scheduled job that
    races routine eviction goes red for doing its job correctly, which is how a
    prune ends up disabled for crying wolf. Observed live on 2026-08-31: 2 of
    153 deletes 404'd in a single run.
    """
    return "HTTP 404" in stderr or "Not Found" in stderr


def _gh(args: list[str], timeout: int = 60) -> str:
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise GhError(args, proc.stderr)
    return proc.stdout


def list_caches(repo: str) -> list[CacheEntry]:
    raw = _gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/actions/caches?per_page=100",
            "--jq",
            ".actions_caches[]",
        ],
        timeout=180,
    )
    return [
        CacheEntry.from_api(json.loads(line))
        for line in raw.splitlines()
        if line.strip()
    ]


def usage_bytes(repo: str) -> tuple[int, int]:
    obj = json.loads(_gh(["api", f"repos/{repo}/actions/cache/usage"]))
    return int(obj["active_caches_size_in_bytes"]), int(obj["active_caches_count"])


def delete_entry(repo: str, entry: CacheEntry) -> bool:
    """Delete one entry. Returns True if we deleted it, False if it was already
    gone (see is_not_found). Raises GhError on any other failure."""
    try:
        _gh(["api", "-X", "DELETE", f"repos/{repo}/actions/caches/{entry.id}"], timeout=60)
    except GhError as exc:
        if is_not_found(exc.stderr):
            return False
        raise
    return True


def _gib(n: int) -> str:
    return f"{n / (1024 ** 3):.2f} GiB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP_PER_GROUP)
    ap.add_argument("--max-deletes", type=int, default=DEFAULT_MAX_DELETES)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    before_bytes, before_count = usage_bytes(args.repo)
    print(f"before: {_gib(before_bytes)} across {before_count} entries")

    entries = list_caches(args.repo)
    print(f"listed: {len(entries)} entries")

    try:
        plan = plan_prune(entries, args.keep, args.max_deletes)
    except PruneRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    print(
        f"plan: delete {len(plan.delete)}, keep {len(plan.keep)}, "
        f"freeing {_gib(plan.bytes_freed)}"
    )
    if args.dry_run:
        for entry in sorted(plan.delete, key=lambda e: e.key):
            print(f"  would delete {entry.key}")
        return 0

    deleted = already_gone = failures = 0
    for entry in plan.delete:
        try:
            if delete_entry(args.repo, entry):
                deleted += 1
            else:
                already_gone += 1
        except Exception as exc:  # noqa: BLE001 - report and keep going
            failures += 1
            print(f"  delete failed for {entry.key}: {exc}", file=sys.stderr)

    print(f"deleted {deleted}, already gone {already_gone}, failed {failures}")

    # NOTE: repos/{repo}/actions/cache/usage lags the actual bucket by minutes
    # -- immediately after the 2026-08-31 run it still reported the pre-prune
    # 9,543,187,664 B / 187 entries while a fresh listing showed 33 entries and
    # 1.85 GiB. So report the listing, which is authoritative now, and the usage
    # endpoint alongside it flagged as possibly stale. Do not gate anything on
    # the usage number in the seconds after a prune.
    remaining = list_caches(args.repo)
    remaining_bytes = sum(e.size_in_bytes for e in remaining)
    print(f"after (listing): {_gib(remaining_bytes)} across {len(remaining)} entries")
    lagging_bytes, lagging_count = usage_bytes(args.repo)
    print(
        f"after (usage api, may lag): {_gib(lagging_bytes)} "
        f"across {lagging_count} entries"
    )

    if failures:
        print(f"{failures} deletes failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
