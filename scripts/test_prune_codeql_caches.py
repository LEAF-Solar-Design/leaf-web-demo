#!/usr/bin/env python3
"""Tests for scripts/prune_codeql_caches.py.

Registered as the `prune-codeql-caches` suite in scripts/run-all-gates.py the
day it shipped -- no fix-then-register debt.

Every case here is EXECUTED against the real decision function. There is no
network, no clock, no filesystem and no skipIf gate anywhere in this file, so
the executed count is identical on every runner and the suite's pinned floor is
exact rather than a min-across-environments.

The load-bearing cases are the negative controls: a guard nobody has watched
fail is not a guard. test_g1..test_g4 each mutate the input so exactly one
guard trips, and assert PruneRefused with nothing deleted.
"""

from __future__ import annotations

import pytest

from prune_codeql_caches import (
    CacheEntry,
    PrunePlan,
    PruneRefused,
    enforce_guards,
    is_not_found,
    parse_overlay_base_key,
    plan_prune,
)

# A real key, copied verbatim from the live bucket on 2026-08-31.
REAL_KEY = (
    "codeql-overlay-base-database-1-d953d79b74456ce0-python-2.26.4"
    "-89524bd318c20b12965170538fd8cffa9cb8a7fe-33416477084-1"
)
REAL_GROUP = "codeql-overlay-base-database-1-d953d79b74456ce0-python-2.26.4"

# The two keys the merge gate depends on, verbatim from the live bucket.
PLAYWRIGHT_KEY = (
    "playwright-chromium-Linux-"
    "125d47d2643e2e12190252821e945240a691bd6873ee06cff06895487f0fb32b"
)
NODE_KEY = (
    "node-cache-Linux-x64-npm-"
    "125d47d2643e2e12190252821e945240a691bd6873ee06cff06895487f0fb32b"
)


def entry(key: str, ident: int, created: str = "2026-08-31T00:00:00Z", size: int = 1) -> CacheEntry:
    return CacheEntry(id=ident, key=key, size_in_bytes=size, created_at=created)


def overlay(group: str, sha: str, ident: int, created: str, size: int = 1) -> CacheEntry:
    return entry(f"{group}-{sha}-33416477084-1", ident, created, size)


# --------------------------------------------------------------------------- #
# Key parsing: the whitelist boundary.
# --------------------------------------------------------------------------- #


def test_parses_a_real_overlay_base_key_to_its_restore_prefix():
    assert parse_overlay_base_key(REAL_KEY) == REAL_GROUP


def test_the_gate_entries_are_not_parseable_as_candidates():
    # These are the two keys the task forbids deleting. They are not rejected by
    # a name blacklist -- they simply never enter the whitelist.
    assert parse_overlay_base_key(PLAYWRIGHT_KEY) is None
    assert parse_overlay_base_key(NODE_KEY) is None


@pytest.mark.parametrize(
    "key",
    [
        "cache-trivy-2026-08-31",
        "trivy-binary-v0.70.0-Linux-X64",
        "setup-python-Linux-x64-24.04-Ubuntu-python-3.12.14-pip-abc123",
        "",
        "codeql-overlay-base-database-",
        # Right prefix, wrong trailer shape: SHA is not hex.
        "codeql-overlay-base-database-1-hash-python-2.26.4-zzzz-1-1",
        # Right prefix, run id is not numeric.
        "codeql-overlay-base-database-1-hash-python-2.26.4-abcdef1-run-1",
        # Right prefix, but nothing between prefix and trailer.
        "codeql-overlay-base-database-abcdef1-1-1",
    ],
)
def test_non_candidates_parse_to_none(key):
    assert parse_overlay_base_key(key) is None


def test_a_language_component_containing_a_dash_still_parses():
    # Trailing fields are split from the RIGHT, so a hyphenated language name
    # could never shift the parse.
    key = "codeql-overlay-base-database-1-hash-c-cpp-2.26.4-abcdef1234567-99-1"
    assert parse_overlay_base_key(key) == "codeql-overlay-base-database-1-hash-c-cpp-2.26.4"


# --------------------------------------------------------------------------- #
# The core rule: keep the newest N per restore-key group.
# --------------------------------------------------------------------------- #


def test_keeps_newest_n_per_group_and_deletes_the_rest():
    entries = [
        overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-29T00:00:00Z"),
        overlay(REAL_GROUP, "bbbbbbb", 2, "2026-08-30T00:00:00Z"),
        overlay(REAL_GROUP, "ccccccc", 3, "2026-08-31T00:00:00Z"),
        entry(PLAYWRIGHT_KEY, 99),
    ]
    plan = plan_prune(entries, keep_per_group=2)
    assert sorted(e.id for e in plan.delete) == [1]
    assert sorted(e.id for e in plan.keep) == [2, 3, 99]


def test_groups_are_independent():
    other = "codeql-overlay-base-database-1-c801913f1ee29663-javascript-2.26.4"
    entries = [
        overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-29T00:00:00Z"),
        overlay(REAL_GROUP, "bbbbbbb", 2, "2026-08-30T00:00:00Z"),
        overlay(other, "ccccccc", 3, "2026-08-29T00:00:00Z"),
        overlay(other, "ddddddd", 4, "2026-08-30T00:00:00Z"),
    ]
    plan = plan_prune(entries, keep_per_group=1)
    assert sorted(e.id for e in plan.delete) == [1, 3]


def test_a_different_cli_version_is_a_different_group():
    # 2.26.3 and 2.26.4 caches are not interchangeable: the CLI version is part
    # of the restore key prefix, so each version keeps its own survivors.
    v3 = "codeql-overlay-base-database-1-hash-python-2.26.3"
    v4 = "codeql-overlay-base-database-1-hash-python-2.26.4"
    entries = [
        overlay(v3, "aaaaaaa", 1, "2026-08-29T00:00:00Z"),
        overlay(v4, "bbbbbbb", 2, "2026-08-30T00:00:00Z"),
    ]
    plan = plan_prune(entries, keep_per_group=1)
    assert plan.delete == []


def test_a_group_smaller_than_keep_loses_nothing():
    entries = [overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-29T00:00:00Z")]
    assert plan_prune(entries, keep_per_group=2).delete == []


def test_never_deletes_a_non_codeql_key():
    # The whole protected set, plus a shape that merely resembles a codeql key.
    protected = [
        entry(PLAYWRIGHT_KEY, 1),
        entry(NODE_KEY, 2),
        entry("cache-trivy-2026-08-31", 3),
        entry("setup-python-Linux-x64-24.04-Ubuntu-python-3.12.14-pip-abc", 4),
        entry("trivy-binary-v0.70.0-Linux-X64", 5),
        entry("codeql-overlay-base-database-1-hash-python-2.26.4-NOTASHA-x-y", 6),
    ]
    # Enough real candidates that a plan is produced at all.
    entries = protected + [
        overlay(REAL_GROUP, "aaaaaaa", 10, "2026-08-28T00:00:00Z"),
        overlay(REAL_GROUP, "bbbbbbb", 11, "2026-08-29T00:00:00Z"),
        overlay(REAL_GROUP, "ccccccc", 12, "2026-08-30T00:00:00Z"),
    ]
    plan = plan_prune(entries, keep_per_group=1)
    deleted_keys = {e.key for e in plan.delete}
    for guarded in protected:
        assert guarded.key not in deleted_keys
    assert deleted_keys == {
        f"{REAL_GROUP}-aaaaaaa-33416477084-1",
        f"{REAL_GROUP}-bbbbbbb-33416477084-1",
    }


def test_ties_on_created_at_break_deterministically_by_id():
    entries = [
        overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-30T00:00:00Z"),
        overlay(REAL_GROUP, "bbbbbbb", 2, "2026-08-30T00:00:00Z"),
    ]
    first = plan_prune(entries, keep_per_group=1)
    second = plan_prune(list(reversed(entries)), keep_per_group=1)
    assert [e.id for e in first.delete] == [e.id for e in second.delete] == [1]


def test_bytes_freed_sums_the_delete_set_only():
    entries = [
        overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-28T00:00:00Z", size=100),
        overlay(REAL_GROUP, "bbbbbbb", 2, "2026-08-29T00:00:00Z", size=200),
        entry(PLAYWRIGHT_KEY, 3, size=999),
    ]
    assert plan_prune(entries, keep_per_group=1).bytes_freed == 100


def test_empty_input_is_a_no_op_not_a_refusal():
    plan = plan_prune([], keep_per_group=2)
    assert plan.delete == [] and plan.keep == []


# --------------------------------------------------------------------------- #
# Negative controls: prove each fail-closed guard can actually fail.
# --------------------------------------------------------------------------- #


def test_g1_rejects_a_smuggled_non_candidate(monkeypatch):
    # Corrupt the parser AFTER grouping would have run, so the delete set is
    # populated but re-validation sees a non-candidate. This is the bug class G1
    # exists for: a selection pass that mis-groups something protected.
    entries = [
        overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-28T00:00:00Z"),
        overlay(REAL_GROUP, "bbbbbbb", 2, "2026-08-29T00:00:00Z"),
    ]
    import prune_codeql_caches as mod

    calls = {"n": 0}
    real = mod.parse_overlay_base_key

    def flaky(key: str):
        calls["n"] += 1
        # Group normally on the first pass, then deny on re-validation.
        return real(key) if calls["n"] <= len(entries) else None

    monkeypatch.setattr(mod, "parse_overlay_base_key", flaky)
    with pytest.raises(PruneRefused, match="G1"):
        mod.plan_prune(entries, keep_per_group=1)


def test_g2_refuses_above_the_max_deletes_backstop():
    entries = [
        overlay(REAL_GROUP, f"{i:07x}", i, f"2026-08-{(i % 28) + 1:02d}T00:00:00Z")
        for i in range(1, 12)
    ]
    with pytest.raises(PruneRefused, match="G2"):
        plan_prune(entries, keep_per_group=1, max_deletes=3)


def test_g2_allows_exactly_the_backstop():
    entries = [
        overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-28T00:00:00Z"),
        overlay(REAL_GROUP, "bbbbbbb", 2, "2026-08-29T00:00:00Z"),
        overlay(REAL_GROUP, "ccccccc", 3, "2026-08-30T00:00:00Z"),
    ]
    assert len(plan_prune(entries, keep_per_group=1, max_deletes=2).delete) == 2


def test_g3_refuses_to_empty_the_bucket():
    # G3 is unreachable through plan_prune() with keep >= 1, so drive the guard
    # directly with the plan a survivor-dropping selection bug would produce.
    a = overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-28T00:00:00Z")
    b = overlay(REAL_GROUP, "bbbbbbb", 2, "2026-08-29T00:00:00Z")
    sabotaged = PrunePlan(keep=[], delete=[a, b])
    with pytest.raises(PruneRefused, match="G3"):
        enforce_guards([a, b], {REAL_GROUP: [a, b]}, sabotaged, 1, 500)


def test_g4_refuses_when_a_group_loses_its_survivors():
    a = overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-28T00:00:00Z")
    b = overlay(REAL_GROUP, "bbbbbbb", 2, "2026-08-29T00:00:00Z")
    other = entry(PLAYWRIGHT_KEY, 3)
    # One entry survives overall, so G3 passes and G4 is the guard that fires:
    # the codeql group itself kept nobody.
    sabotaged = PrunePlan(keep=[other], delete=[a, b])
    with pytest.raises(PruneRefused, match="G4"):
        enforce_guards([a, b, other], {REAL_GROUP: [a, b]}, sabotaged, 1, 500)


def test_guards_pass_on_a_well_formed_plan():
    # Positive control: the same shape with survivors intact raises nothing, so
    # the three refusals above are attributable to the sabotage, not to the
    # harness being unable to build a passing plan at all.
    a = overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-28T00:00:00Z")
    b = overlay(REAL_GROUP, "bbbbbbb", 2, "2026-08-29T00:00:00Z")
    ok = PrunePlan(keep=[b], delete=[a])
    enforce_guards([a, b], {REAL_GROUP: [a, b]}, ok, 1, 500)


def test_keep_per_group_below_one_is_refused():
    entries = [overlay(REAL_GROUP, "aaaaaaa", 1, "2026-08-28T00:00:00Z")]
    with pytest.raises(PruneRefused, match="keep_per_group"):
        plan_prune(entries, keep_per_group=0)


# --------------------------------------------------------------------------- #
# A cache entry that vanished mid-run is success, not failure. Regression pin
# for the live 2026-08-31 run, where 2 of 153 deletes 404'd because GitHub's own
# LRU evicted them between the listing and the DELETE. Failing the job on that
# would make a scheduled prune red for doing its job.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: Not Found (HTTP 404)",
        "HTTP 404: Not Found (https://api.github.com/repos/o/r/actions/caches/1)",
    ],
)
def test_404_stderr_is_classified_as_already_gone(stderr):
    assert is_not_found(stderr) is True


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: Forbidden (HTTP 403)",
        "HTTP 500: Internal Server Error",
        "error connecting to api.github.com",
        "",
    ],
)
def test_other_stderr_is_not_already_gone(stderr):
    assert is_not_found(stderr) is False


def test_delete_entry_swallows_a_404_and_reports_not_deleted(monkeypatch):
    import prune_codeql_caches as mod

    def boom(args, timeout=60):
        raise mod.GhError(args, "gh: Not Found (HTTP 404)")

    monkeypatch.setattr(mod, "_gh", boom)
    assert mod.delete_entry("o/r", entry(REAL_KEY, 1)) is False


def test_delete_entry_reraises_a_real_failure(monkeypatch):
    # Negative control: a 403 must NOT be silently swallowed, or a token that
    # lost actions:write would look like a clean prune that freed nothing.
    import prune_codeql_caches as mod

    def boom(args, timeout=60):
        raise mod.GhError(args, "gh: Forbidden (HTTP 403)")

    monkeypatch.setattr(mod, "_gh", boom)
    with pytest.raises(mod.GhError):
        mod.delete_entry("o/r", entry(REAL_KEY, 1))


def test_delete_entry_reports_true_on_success(monkeypatch):
    import prune_codeql_caches as mod

    monkeypatch.setattr(mod, "_gh", lambda args, timeout=60: "")
    assert mod.delete_entry("o/r", entry(REAL_KEY, 1)) is True
