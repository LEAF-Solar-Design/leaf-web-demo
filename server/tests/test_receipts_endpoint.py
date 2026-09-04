"""Slice 12a: GET /api/receipts reads receipts that exist and fabricates none.

The load-bearing properties:

  1. NO FABRICATED ROWS. Every row this endpoint emits traces to a fixture
     artifact, a fixture reconciler document, or a fixture receipt.json. With no
     source configured the answer is an EMPTY row list plus an honest
     `source_unavailable` entry naming the missing environment variable -- never
     a placeholder row and never a silent empty.
  2. A NAME IS NOT A RECEIPT. An artifact is rendered only after three
     provenance checks -- un-expired, minted by a run of THIS repository (not a
     fork's), and minted by an allowlisted workflow -- the same three
     `.github/workflows/test-gate.yml` runs over these identical artifact names.
     An artifact that fails any of them is ABSENT, never an unverified row.
  3. NO CREDENTIAL LEAK. The token never appears in a URL, a response body, or
     an unavailable detail. Asserted against the whole serialized response.
  4. NO UNAUTHENTICATED FALLBACK. With every token unset the reader must not
     make the HTTP call at all.
  5. BOUNDED. Oversize bodies are refused rather than truncated; row, field,
     provenance-lookup and inflight caps hold; every GitHub read is cached.
  6. FAIL CLOSED on a malformed scope: 422, never a best-effort read.
  7. TWO ADMISSIONS. `job:` is tenant data and answers 404 for another tenant's
     job. `pr:`, `tree:` and `train` read the PLATFORM's private repository, so
     they require the `platform_customize` entitlement and 403 without it,
     BEFORE any outbound call, so the route is not an existence oracle.

Run:
    cd server
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_receipts_endpoint.py -q
"""
from __future__ import annotations

import http.client
import json
import re
import sys
import urllib.parse
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
import entitlements  # noqa: E402
import receipts_read as rr  # noqa: E402

FAKE_TOKEN = "ghp-fixture-token-never-real"
FAKE_FALLBACK_TOKEN = "ghp-fixture-fallback-never-real"
REPO_SLUG = "LEAF-Solar-Design/leaf-web-demo"
REPO_ID = 987654
FORK_ID = 111222

PREWARM_WORKFLOW = ".github/workflows/prewarm-staging-cutover.yml"
GATE_WORKFLOW = ".github/workflows/test-gate.yml"
BUILD_WORKFLOW = ".github/workflows/build-platform-images.yml"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (rr.ENV_REPO, rr.ENV_TOKEN, rr.ENV_FALLBACK_TOKEN, rr.ENV_API_ROOT,
                 rr.ENV_RECONCILER_URL, rr.ENV_JOB_RECEIPT_DIR):
        monkeypatch.delenv(name, raising=False)
    rr.reset_reconciler_cache()
    rr.reset_github_caches()
    yield
    rr.reset_reconciler_cache()
    rr.reset_github_caches()


def _artifact(name, *, sha="a" * 40, run_id=4242, created="2026-09-01T10:00:00Z",
              size=3072, head_repository_id=REPO_ID, repository_id=REPO_ID,
              expired=False):
    return {
        "name": name,
        "size_in_bytes": size,
        "created_at": created,
        "expired": expired,
        "archive_download_url": f"https://api.github.com/artifacts/{name}/zip",
        "workflow_run": {
            "id": run_id,
            "head_sha": sha,
            "head_branch": "main",
            "head_repository_id": head_repository_id,
            "repository_id": repository_id,
        },
    }


def _configure_github(monkeypatch, token=FAKE_TOKEN):
    monkeypatch.setenv(rr.ENV_REPO, REPO_SLUG)
    monkeypatch.setenv(rr.ENV_TOKEN, token)


def _stub_github(monkeypatch, artifacts=None, *, run_paths=None, repo_id=REPO_ID,
                 artifacts_error=None, repo_error=None, calls=None):
    """Replace the ONE bounded HTTP primitive across all three GitHub reads.

    `artifacts` maps an artifact NAME to the listing GitHub would return for it;
    `run_paths` maps a run id to the workflow path its run record carries.
    """
    listings = dict(artifacts or {})
    paths = dict(run_paths or {})

    def fake(url, *, headers, cap):
        if calls is not None:
            calls.append((url, dict(headers), cap))
        assert FAKE_TOKEN not in url, "the token must never reach a URL"
        assert FAKE_FALLBACK_TOKEN not in url, "the token must never reach a URL"
        if "/actions/artifacts" in url:
            if artifacts_error is not None:
                raise artifacts_error
            wanted = urllib.parse.unquote(url.split("name=", 1)[1].split("&", 1)[0])
            return {"artifacts": list(listings.get(wanted, []))}
        if "/actions/runs/" in url:
            run_id = int(url.rsplit("/", 1)[-1])
            if run_id not in paths:
                raise OSError("no fixture for this run")
            return {"path": paths[run_id]}
        if repo_error is not None:
            raise repo_error
        return {"id": repo_id}

    monkeypatch.setattr(rr, "_get_json", fake)


def _prewarm_fixture(monkeypatch, pr="988", **kwargs):
    name = f"prewarm-relay-receipt-pr-{pr}"
    _stub_github(monkeypatch, {name: [_artifact(name, **kwargs)]},
                 run_paths={kwargs.get("run_id", 4242): PREWARM_WORKFLOW})
    return name


# --------------------------------------------------------------------------- #
# 1. scope parsing fails closed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scope", [
    "pr:988", "tree:" + "a" * 40, "job:job-1", "train",
])
def test_valid_scopes_parse(scope):
    kind, _ = rr.parse_scope(scope)
    assert kind in {"pr", "tree", "job", "train"}


@pytest.mark.parametrize("bad", [
    "", "pr:", "pr:0", "pr:-1", "pr:abc", "tree:", "tree:zzz", "tree:" + "a" * 65,
    "job:", "job:../etc", "job:a/b", "trains", "PR:1", None, 7, ["pr:1"],
    "pr:1" + chr(0), "x" * 300,
])
def test_malformed_scopes_fail_closed(bad):
    with pytest.raises(rr.ReceiptsError):
        rr.parse_scope(bad)


# --------------------------------------------------------------------------- #
# 2. every artifact kind, from a fixture that clears provenance
# --------------------------------------------------------------------------- #
def test_prewarm_relay_receipt_row_from_a_fixture_artifact(monkeypatch):
    _configure_github(monkeypatch)
    name = _prewarm_fixture(monkeypatch)

    body = rr.read_receipts("pr:988")
    assert body["contract"] == rr.CONTRACT
    assert body["unavailable"] == []
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert set(row) == {"kind", "ref", "at", "sha", "summary", "url"}
    assert row["kind"] == "prewarm-relay"
    assert row["ref"] == "pr:988"
    assert row["sha"] == "a" * 40
    assert name in row["summary"]
    assert "prewarm-staging-cutover.yml" in row["summary"], \
        "the row names the workflow whose provenance it rests on"
    assert row["url"].endswith("/actions/runs/4242")


def test_tree_scope_reads_both_the_gate_proof_and_the_supply_set(monkeypatch):
    _configure_github(monkeypatch)
    tree = "b" * 40
    gate, supply = f"gate-proof-{tree}", f"spec-v3-supply-set-{tree}"
    _stub_github(monkeypatch, {
        gate: [_artifact(gate, created="2026-08-30T08:00:00Z", run_id=11)],
        supply: [_artifact(supply, created="2026-08-30T09:00:00Z", run_id=12)],
    }, run_paths={11: GATE_WORKFLOW, 12: BUILD_WORKFLOW})
    body = rr.read_receipts(f"tree:{tree}")
    kinds = [row["kind"] for row in body["rows"]]
    assert kinds == ["supply-set", "gate-proof"], "newest first"
    assert body["unavailable"] == []


def test_an_absent_artifact_yields_no_row_and_no_fabrication(monkeypatch):
    _configure_github(monkeypatch)
    _stub_github(monkeypatch, {})
    body = rr.read_receipts("pr:1")
    assert body["rows"] == []
    assert body["unavailable"] == []


def test_reconciler_latest_json_row(monkeypatch):
    monkeypatch.setenv(rr.ENV_RECONCILER_URL,
                       "https://raw.githubusercontent.com/o/leaf-plan/main/"
                       "receipt-inbox/product-progress/latest.json")
    monkeypatch.setattr(rr, "_get_json", lambda url, *, headers, cap: {
        "receipts": [
            {"at": "2026-09-02T12:00:00Z", "sha": "c" * 40,
             "summary": "studio-standardization reconciled", "url": "https://example.com/r"},
        ]
    })
    body = rr.read_receipts("train")
    assert body["unavailable"] == []
    assert body["rows"][0]["kind"] == "reconciler"
    assert body["rows"][0]["summary"] == "studio-standardization reconciled"


def test_job_receipt_json_beside_a_job(monkeypatch, tmp_path):
    monkeypatch.setenv(rr.ENV_JOB_RECEIPT_DIR, str(tmp_path))
    job_dir = tmp_path / "job-77"
    job_dir.mkdir()
    (job_dir / "receipt.json").write_text(json.dumps({
        "at": "2026-09-03T01:02:03Z", "sha": "d" * 40, "summary": "count-by-layer finished",
        "url": "https://example.com/job/77",
    }), encoding="utf-8")

    body = rr.read_receipts("job:job-77")
    assert body["unavailable"] == []
    assert body["rows"] == [{
        "kind": "job", "ref": "job:job-77", "at": "2026-09-03T01:02:03Z",
        "sha": "d" * 40, "summary": "count-by-layer finished",
        "url": "https://example.com/job/77",
    }]


def test_a_job_with_no_receipt_yet_is_an_empty_answer_not_an_error(monkeypatch, tmp_path):
    """Slice 11 writes these; until it does, absence is the normal answer."""
    monkeypatch.setenv(rr.ENV_JOB_RECEIPT_DIR, str(tmp_path))
    body = rr.read_receipts("job:job-77")
    assert body["rows"] == []
    assert body["unavailable"] == []


def test_a_malformed_job_receipt_is_skipped_with_an_honest_reason(monkeypatch, tmp_path):
    monkeypatch.setenv(rr.ENV_JOB_RECEIPT_DIR, str(tmp_path))
    job_dir = tmp_path / "job-77"
    job_dir.mkdir()
    (job_dir / "receipt.json").write_text("{not json", encoding="utf-8")
    body = rr.read_receipts("job:job-77")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREADABLE


# --------------------------------------------------------------------------- #
# 2b. PROVENANCE: a name is not a receipt
# --------------------------------------------------------------------------- #
def test_a_fork_run_cannot_mint_a_gate_proof(monkeypatch):
    """THE fabricated-receipt case. A fork's `pull_request` run can upload an
    artifact with ANY name and ANY head SHA; rendering it as "Gate proof" would
    put an attacker-chosen commit under a heading that promises a real receipt.
    """
    _configure_github(monkeypatch)
    tree = "b" * 40
    name = f"gate-proof-{tree}"
    _stub_github(monkeypatch, {
        name: [_artifact(name, sha="e" * 40, run_id=31,
                         head_repository_id=FORK_ID, repository_id=REPO_ID)],
    }, run_paths={31: GATE_WORKFLOW})
    body = rr.read_receipts(f"tree:{tree}")
    assert body["rows"] == [], "a fork-minted artifact is absent, not unverified"
    assert "e" * 40 not in json.dumps(body)


@pytest.mark.parametrize("missing", [None, "not-an-int", True])
def test_an_artifact_with_no_usable_head_repository_id_is_dropped(monkeypatch, missing):
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-5"
    _stub_github(monkeypatch, {name: [_artifact(name, head_repository_id=missing)]},
                 run_paths={4242: PREWARM_WORKFLOW})
    assert rr.read_receipts("pr:5")["rows"] == []


def test_an_artifact_minted_by_another_workflow_is_dropped(monkeypatch):
    """Any workflow in the repo, including a workflow_dispatch, can upload a
    file called `gate-proof-<tree>`. Only the gate workflows may mint one."""
    _configure_github(monkeypatch)
    tree = "b" * 40
    name = f"gate-proof-{tree}"
    _stub_github(monkeypatch, {name: [_artifact(name, run_id=41)]},
                 run_paths={41: ".github/workflows/some-other-lane.yml"})
    assert rr.read_receipts(f"tree:{tree}")["rows"] == []


def test_the_allowlist_is_per_receipt_kind_not_one_shared_set(monkeypatch):
    """A supply set minted by the gate workflow is not a supply set."""
    _configure_github(monkeypatch)
    tree = "b" * 40
    name = f"spec-v3-supply-set-{tree}"
    _stub_github(monkeypatch, {name: [_artifact(name, run_id=51)]},
                 run_paths={51: GATE_WORKFLOW})
    rows = rr.read_receipts(f"tree:{tree}")["rows"]
    assert [row for row in rows if row["kind"] == "supply-set"] == []
    assert GATE_WORKFLOW not in rr.MINTING_WORKFLOWS["supply-set"]


def test_a_workflow_path_with_an_at_ref_suffix_still_matches(monkeypatch):
    """Some GitHub surfaces render a workflow path with an `@ref` suffix; one
    is stripped defensively, and the match after that is exact."""
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-6"
    _stub_github(monkeypatch, {name: [_artifact(name, run_id=61)]},
                 run_paths={61: PREWARM_WORKFLOW + "@refs/heads/main"})
    assert len(rr.read_receipts("pr:6")["rows"]) == 1


def test_an_expired_artifact_is_not_rendered_as_proof(monkeypatch):
    """An expired artifact can never be downloaded again, so nothing can
    re-verify it. Same drop `test-gate.yml`'s reuse filter makes."""
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-7"
    _stub_github(monkeypatch, {name: [_artifact(name, expired=True)]},
                 run_paths={4242: PREWARM_WORKFLOW})
    assert rr.read_receipts("pr:7")["rows"] == []


def test_no_row_is_rendered_when_the_repository_identity_cannot_be_read(monkeypatch):
    """The provenance anchor. Without it nothing can be checked, so nothing is
    trusted -- the failure is honest, never optimistic."""
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-8"
    _stub_github(monkeypatch, {name: [_artifact(name)]},
                 run_paths={4242: PREWARM_WORKFLOW},
                 repo_error=OSError("repo lookup failed"))
    body = rr.read_receipts("pr:8")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREACHABLE
    assert "same-repository origin" in body["unavailable"][0]["detail"]


def test_an_unidentifiable_run_is_dropped(monkeypatch):
    """No run id means no way to read the minting workflow, so no row."""
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-9"
    artifact = _artifact(name)
    artifact["workflow_run"].pop("id")
    _stub_github(monkeypatch, {name: [artifact]})
    assert rr.read_receipts("pr:9")["rows"] == []


def test_the_cap_reports_incomplete_rather_than_a_false_empty(monkeypatch):
    """Round-2 finding 3: MAX_PROVENANCE_LOOKUPS must bound WORK, not
    correctness. Six newer same-repo artifacts from a workflow that may NOT
    mint this receipt must not spend the whole per-name lookup budget and
    collapse an older, real gate proof into a confident-looking empty answer
    -- and when the budget runs out before every candidate is examined, the
    answer must say so rather than read as a confirmed absence."""
    _configure_github(monkeypatch)
    tree = "b" * 40
    name = f"gate-proof-{tree}"
    foreign = [
        _artifact(name, run_id=9000 + i, created=f"2026-09-02T10:{i:02d}:00Z")
        for i in range(6)
    ]
    real = _artifact(name, run_id=8000, created="2026-09-01T00:00:00Z")
    _stub_github(monkeypatch, {name: foreign + [real]}, run_paths={
        **{9000 + i: ".github/workflows/some-other-lane.yml" for i in range(6)},
        8000: GATE_WORKFLOW,
    })
    body = rr.read_receipts(f"tree:{tree}")
    assert [row for row in body["rows"] if row["kind"] == "gate-proof"] == [], \
        "the real receipt sat past the lookup budget and was never reached"
    assert body["unavailable"], "the cap being hit must be visible, never a silent empty"
    assert body["unavailable"][0]["reason"] == rr.REASON_BUSY


def test_a_repeated_run_id_does_not_spend_a_second_lookup_slot(monkeypatch):
    """Two artifacts minted by the SAME run share one budget slot: the memo
    already makes the second lookup free, so it must not count against
    MAX_PROVENANCE_LOOKUPS as if it were a distinct candidate."""
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-9"
    same_run = [
        _artifact(name, run_id=42, created=f"2026-09-01T10:{i:02d}:00Z")
        for i in range(rr.MAX_PROVENANCE_LOOKUPS + 3)
    ]
    _stub_github(monkeypatch, {name: same_run}, run_paths={42: PREWARM_WORKFLOW})
    body = rr.read_receipts("pr:9")
    assert len(body["rows"]) == len(same_run), \
        "one distinct run id must verify every artifact it minted, not just the cap's worth"
    assert body["unavailable"] == []


def test_a_repeated_FAILING_run_id_does_not_retry_past_the_cap(monkeypatch):
    """Round-3 regression: the round-2 fix spent a budget slot only on a run id
    `not in attempted_run_ids`, on the premise that a repeat is free via
    `_run_workflow_path`'s own memo -- but that memo does NOT cache a failed
    lookup (a transient outage must not be cached across requests), so every
    repeat of a run id whose OWN record cannot be read cost a fresh HTTP call.
    Thirty artifacts sharing one failing run id must cost at most
    MAX_PROVENANCE_LOOKUPS run-record calls, not thirty."""
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-9"
    calls = []
    same_failing_run = [
        _artifact(name, run_id=42, created=f"2026-09-01T10:{i:02d}:00Z")
        for i in range(30)
    ]
    # No entry for run 42 in run_paths: _stub_github's fake() raises OSError
    # for any run id it has no fixture for, exactly like a real lookup failure.
    _stub_github(monkeypatch, {name: same_failing_run}, run_paths={}, calls=calls)
    body = rr.read_receipts("pr:9")
    assert body["rows"] == [], "a run whose own record cannot be read verifies nothing"
    run_calls = [c for c in calls if "/actions/runs/" in c[0]]
    assert len(run_calls) <= rr.MAX_PROVENANCE_LOOKUPS, (
        f"{len(run_calls)} run lookups for one artifact name sharing one "
        f"failing run id, cap is {rr.MAX_PROVENANCE_LOOKUPS}"
    )
    assert len(run_calls) == 1, "the failing lookup must be memoized for this call, not re-issued"


def test_an_unreadable_run_record_is_incomplete_not_a_confident_empty(monkeypatch):
    """A run record that cannot be read at all (a transient runs-endpoint
    failure) is inconclusive, never a confirmed absence -- the exact honesty
    defect the cap-exhaustion path exists to prevent, reached through a
    different door. It must render `source_busy`, never a silent empty."""
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-9"
    _stub_github(monkeypatch, {name: [_artifact(name, run_id=42)]}, run_paths={})
    body = rr.read_receipts("pr:9")
    assert body["rows"] == []
    assert body["unavailable"], "an unreadable run record must not read as a confirmed absence"
    assert body["unavailable"][0]["reason"] == rr.REASON_BUSY
    # Round-3 finding: the detail must name THIS door, never the budget one
    # (one artifact, one failing run, four of five slots unspent).
    detail = body["unavailable"][0]["detail"]
    assert "could not be read" in detail, detail
    assert "provenance budget" not in detail, detail


def test_both_doors_fired_name_both_causes(monkeypatch):
    """Six unreadable runs exhaust the budget AND fail to read: the detail
    names both causes, in that order, and still never reads as an absence."""
    _configure_github(monkeypatch)
    name = "prevarm-relay-receipt-pr-9".replace("prevarm", "prewarm")
    arts = [_artifact(name, run_id=7000 + i, created=f"2026-09-02T10:{i:02d}:00Z") for i in range(6)]
    _stub_github(monkeypatch, {name: arts}, run_paths={})
    body = rr.read_receipts("pr:9")
    assert body["rows"] == []
    detail = body["unavailable"][0]["detail"]
    assert "provenance budget" in detail and "could not be read" in detail, detail
    assert detail.index("provenance budget") < detail.index("could not be read")


def test_a_second_repository_cannot_be_served_this_ones_cached_rows(monkeypatch):
    """`_artifact_cache` is keyed by ``(slug, name)``, not name alone (round-2
    finding 4, previously unpinned): a bare `name` key would let a second
    repository's identical artifact name inherit the FIRST repository's
    cached rows."""
    name = "prewarm-relay-receipt-pr-9"
    _configure_github(monkeypatch)
    _stub_github(monkeypatch, {name: [_artifact(name)]}, run_paths={4242: PREWARM_WORKFLOW})
    first = rr.read_receipts("pr:9")
    assert len(first["rows"]) == 1, "repo A's row is now cached under (REPO_SLUG, name)"

    other_slug = "some-other-org/some-other-repo"
    monkeypatch.setenv(rr.ENV_REPO, other_slug)
    # repo B's listing for the SAME artifact name is genuinely empty.
    _stub_github(monkeypatch, {name: []}, repo_id=555555)
    second = rr.read_receipts("pr:9")
    assert second["rows"] == [], (
        "a bare name-only cache key would have served repo A's cached row "
        "to repo B's identical artifact name"
    )


def test_a_second_repositorys_run_id_cannot_alias_a_workflow_path(monkeypatch):
    """`_run_path_memo` is keyed by ``(slug, run_id)``, not the bare run id
    (round-2 finding 4, previously unpinned): a bare-int key would let repo
    B's run 42 -- minted by a workflow that may NOT mint this receipt --
    inherit repo A's cached path for the SAME run id."""
    name = "prewarm-relay-receipt-pr-9"
    _configure_github(monkeypatch)
    _stub_github(monkeypatch, {name: [_artifact(name, run_id=42)]},
                 run_paths={42: PREWARM_WORKFLOW})
    first = rr.read_receipts("pr:9")
    assert len(first["rows"]) == 1, "run 42's path is now memoized under (REPO_SLUG, 42)"

    other_slug = "some-other-org/some-other-repo"
    monkeypatch.setenv(rr.ENV_REPO, other_slug)
    # repo B's OWN run 42 was minted by a different, unallowlisted workflow.
    _stub_github(
        monkeypatch,
        {name: [_artifact(name, run_id=42, head_repository_id=555555, repository_id=555555)]},
        run_paths={42: ".github/workflows/some-other-lane.yml"},
        repo_id=555555,
    )
    second = rr.read_receipts("pr:9")
    assert second["rows"] == [], (
        "a bare-int run-path key would have reused repo A's cached path for "
        "run 42, verifying an artifact repo B's own run cannot mint"
    )


def test_the_minting_allowlist_names_a_real_uploader_per_kind():
    """Read from the workflow files, not from memory: a receipt kind's
    allowlist can legitimately include a workflow that only REUSES an
    artifact another run minted (gate-proof's build-platform-images.yml
    entry), so the check is "at least one allowlisted workflow really
    uploads the exact name pattern this module reads that kind by", not
    "every allowlisted file exists" (which passes even on a renamed
    artifact) and not "every allowlisted workflow uploads it" (which is
    false for a legitimate reuse-only entry)."""
    root = SERVER_DIR.parent
    # kind -> the literal (non-``${{ }}``) prefix receipts_read.py reads that
    # kind's artifact name by, taken from its own f-strings in read_receipts.
    kind_prefix = {
        "prewarm-relay": "prewarm-relay-receipt-pr-",
        "gate-proof": "gate-proof-",
        "supply-set": "spec-v3-supply-set-",
    }
    assert set(kind_prefix) == set(rr.MINTING_WORKFLOWS), \
        "this test must cover every receipt kind the module allowlists"

    upload_step = re.compile(
        r"uses:\s*actions/upload-artifact@v\d+\s*\n(?:[^\n]*\n){0,6}?\s*name:\s*(\S.*)"
    )
    for kind, prefix in kind_prefix.items():
        uploaders = []
        for workflow in sorted(rr.MINTING_WORKFLOWS[kind]):
            path = root / workflow
            assert path.is_file(), f"{workflow} is allowlisted but absent"
            text = path.read_text(encoding="utf-8")
            names = [raw.strip().split("${{", 1)[0] for raw in upload_step.findall(text)]
            if prefix in names:
                uploaders.append(workflow)
        assert uploaders, (
            f"no workflow allowlisted for {kind!r} contains an "
            f"actions/upload-artifact step named exactly {prefix!r}"
        )


# --------------------------------------------------------------------------- #
# 3. no credential -> honest empty, and NO http call at all
# --------------------------------------------------------------------------- #
def test_missing_token_returns_an_honest_empty_list_and_makes_no_call(monkeypatch):
    monkeypatch.setenv(rr.ENV_REPO, REPO_SLUG)
    called = []

    def must_not_run(*args, **kwargs):
        called.append(args)
        raise AssertionError("an unauthenticated GitHub call must never be made")

    monkeypatch.setattr(rr, "_get_json", must_not_run)

    body = rr.read_receipts("pr:988")
    assert called == []
    assert body["rows"] == []
    assert len(body["unavailable"]) == 1
    entry = body["unavailable"][0]
    assert entry["source"] == "github-artifacts"
    assert entry["reason"] == rr.REASON_NO_CREDENTIAL
    # The refusal names the exact variable an operator must set, and the
    # permission it needs -- the PR PAT deliberately does not carry Actions:read.
    assert rr.ENV_TOKEN in entry["detail"]
    assert "Actions: read" in entry["detail"]


def test_the_dedicated_read_token_wins_over_the_pr_pat(monkeypatch):
    """The PR PAT is pinned to Pull-requests + Commit-statuses and carries no
    `Actions: read`, so it is a documented fallback, not the intended identity.
    """
    monkeypatch.setenv(rr.ENV_REPO, REPO_SLUG)
    monkeypatch.setenv(rr.ENV_FALLBACK_TOKEN, FAKE_FALLBACK_TOKEN)
    assert rr.github_credentials() == (REPO_SLUG, FAKE_FALLBACK_TOKEN)
    monkeypatch.setenv(rr.ENV_TOKEN, FAKE_TOKEN)
    assert rr.github_credentials() == (REPO_SLUG, FAKE_TOKEN)


def test_a_403_from_a_token_without_actions_read_is_honest_not_a_fabrication(monkeypatch):
    """The runbook-correct deployment state until the read token is set."""
    monkeypatch.setenv(rr.ENV_REPO, REPO_SLUG)
    monkeypatch.setenv(rr.ENV_FALLBACK_TOKEN, FAKE_FALLBACK_TOKEN)
    _stub_github(monkeypatch, repo_error=OSError("HTTP 403 Forbidden"))
    body = rr.read_receipts("pr:988")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREACHABLE


def test_missing_reconciler_url_names_its_environment_variable(monkeypatch):
    body = rr.read_receipts("train")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_NO_CREDENTIAL
    assert rr.ENV_RECONCILER_URL in body["unavailable"][0]["detail"]


def test_a_non_https_reconciler_url_is_refused(monkeypatch):
    monkeypatch.setenv(rr.ENV_RECONCILER_URL, "http://raw.example.com/latest.json")
    called = []
    monkeypatch.setattr(rr, "_get_json", lambda *a, **k: called.append(a))
    body = rr.read_receipts("train")
    assert called == []
    assert body["rows"] == []


@pytest.mark.parametrize("slug,token", [
    ("not-a-slug", FAKE_TOKEN),
    ("a/b", "token with spaces"),
    ("a/b", ""),
    ("", FAKE_TOKEN),
])
def test_an_unusable_credential_is_source_unavailable_not_a_call(monkeypatch, slug, token):
    monkeypatch.setenv(rr.ENV_REPO, slug)
    monkeypatch.setenv(rr.ENV_TOKEN, token)
    monkeypatch.setattr(rr, "_get_json", lambda *a, **k: pytest.fail("must not call"))
    assert rr.github_credentials() == rr.REASON_NO_CREDENTIAL
    body = rr.read_receipts("pr:1")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_NO_CREDENTIAL


# --------------------------------------------------------------------------- #
# 4. the token never reaches a response, a URL, or a detail
# --------------------------------------------------------------------------- #
def test_the_token_travels_only_in_the_authorization_header(monkeypatch):
    _configure_github(monkeypatch)
    calls = []
    name = "prewarm-relay-receipt-pr-5"
    _stub_github(monkeypatch, {name: [_artifact(name)]},
                 run_paths={4242: PREWARM_WORKFLOW}, calls=calls)

    body = rr.read_receipts("pr:5")
    assert calls, "the read must actually have happened"
    for url, headers, cap in calls:
        assert FAKE_TOKEN not in url
        assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
        assert cap in {rr.MAX_ARTIFACT_BYTES, rr.MAX_RUN_BYTES, rr.MAX_REPO_BYTES}
    assert FAKE_TOKEN not in json.dumps(body)


def test_an_unreachable_api_reports_a_reason_without_quoting_the_request(monkeypatch):
    _configure_github(monkeypatch)
    _stub_github(monkeypatch, artifacts_error=OSError(
        f"HTTP 401 for https://api.github.com/x?token={FAKE_TOKEN}"))
    body = rr.read_receipts("pr:5")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREACHABLE
    assert FAKE_TOKEN not in json.dumps(body)


# --------------------------------------------------------------------------- #
# 5. bounds
# --------------------------------------------------------------------------- #
def test_provenance_lookups_are_capped_per_artifact_name(monkeypatch):
    """One request can never fan out to a hundred run reads on a shared token."""
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-9"
    many = [_artifact(name, run_id=1000 + i, created=f"2026-09-01T10:{i:02d}:00Z")
            for i in range(40)]
    _stub_github(monkeypatch, {name: many},
                 run_paths={1000 + i: PREWARM_WORKFLOW for i in range(40)})
    body = rr.read_receipts("pr:9")
    assert len(body["rows"]) == rr.MAX_PROVENANCE_LOOKUPS
    # spent on the NEWEST candidates, which are the ones a reader would see
    assert body["rows"][0]["at"] == "2026-09-01T10:39:00Z"


def test_reconciler_rows_are_capped(monkeypatch):
    monkeypatch.setenv(rr.ENV_RECONCILER_URL, "https://raw.example.com/latest.json")
    monkeypatch.setattr(rr, "_get_json", lambda url, *, headers, cap: {
        "receipts": [{"at": f"2026-09-01T10:00:{i:02d}Z", "summary": str(i)}
                     for i in range(rr.MAX_ROWS + 40)]})
    assert len(rr.read_receipts("train")["rows"]) <= rr.MAX_ROWS


def test_a_field_is_truncated_not_echoed_whole(monkeypatch):
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-9"
    _stub_github(monkeypatch, {name: [_artifact("x" * 5000)]},
                 run_paths={4242: PREWARM_WORKFLOW})
    body = rr.read_receipts("pr:9")
    assert all(len(value) <= rr.MAX_FIELD for value in body["rows"][0].values()), \
        "every rendered field is bounded, including the composed summary"


def test_an_oversize_body_is_refused_rather_than_truncated(monkeypatch):
    """The cap is enforced by reading cap+1, so an oversize body is DETECTED."""
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self, n):
            return self._payload[:n]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        rr.urllib.request, "urlopen",
        lambda request, timeout=None: FakeResponse(b"[" + b"0," * 400000 + b"0]"),
    )
    with pytest.raises(ValueError):
        rr._get_json("https://example.com/x", headers={}, cap=1024)


class _TruncatedResponse:
    """A response that dies mid-transfer: http.client's real behavior for a
    connection dropped before the declared Content-Length is delivered is its
    own IncompleteRead, an HTTPException subclass that is neither OSError nor
    ValueError."""

    def read(self, n):
        raise http.client.IncompleteRead(b"partial", 40)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_transport_failure_neither_oserror_nor_valueerror_is_normalized(monkeypatch):
    """Round-2 finding 1: every caller of _get_json matches only
    (OSError, urllib.error.HTTPError, ValueError, _CredentialUnavailable), so
    an un-normalized IncompleteRead would escape every one of them uncaught."""
    monkeypatch.setattr(rr.urllib.request, "urlopen",
                        lambda request, timeout=None: _TruncatedResponse())
    with pytest.raises(OSError):
        rr._get_json("https://example.com/x", headers={}, cap=1024)


def test_a_truncated_response_is_the_honest_state_at_the_route_never_a_500(monkeypatch):
    """Pinned at the ROUTE, not just at _get_json: a truncated transfer must
    make the actual endpoint answer 200 with the honest unavailable state,
    never an unhandled 500."""
    tenant = _admit_platform(monkeypatch)
    monkeypatch.setenv(rr.ENV_RECONCILER_URL, "https://raw.example.com/latest.json")
    monkeypatch.setattr(rr.urllib.request, "urlopen",
                        lambda request, timeout=None: _TruncatedResponse())

    resp = _client(tenant).get("/api/receipts", params={"scope": "train"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREACHABLE


class _OversizeResponse:
    """A real response-shaped object serving exactly ``cap + 1`` bytes, so the
    cap is exercised through the actual bounded ``read(n)`` in ``_get_json``,
    never asserted against ``_get_json`` in isolation (round-2 finding 1's
    literal pin, previously missing at the route)."""

    def __init__(self, cap):
        self._payload = b"[" + b"0" * (cap + 1) + b"]"

    def read(self, n):
        return self._payload[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _SmallJSONResponse:
    """A real response-shaped object serving one small, well-formed body."""

    def __init__(self, obj):
        self._payload = json.dumps(obj).encode("utf-8")

    def read(self, n):
        return self._payload[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_an_oversize_reconciler_body_is_200_source_unreachable_at_the_route(monkeypatch):
    """Round-2 finding 1's literal pin: cap+1 bytes served through a REAL
    urlopen must make the ROUTE answer 200 with `source_unreachable`, never a
    truncated parse and never a 500."""
    tenant = _admit_platform(monkeypatch)
    monkeypatch.setenv(rr.ENV_RECONCILER_URL, "https://raw.example.com/latest.json")
    monkeypatch.setattr(
        rr.urllib.request, "urlopen",
        lambda request, timeout=None: _OversizeResponse(rr.MAX_RECONCILER_BYTES),
    )
    resp = _client(tenant).get("/api/receipts", params={"scope": "train"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREACHABLE


def test_an_oversize_artifact_listing_is_200_source_unreachable_at_the_route(monkeypatch):
    """The same literal pin, the artifacts path: the GitHub artifacts API
    response itself is oversize, through a real urlopen, at the actual route.
    The repo-id call is left small so this pins the ARTIFACTS read's boundary
    specifically, not the repo-identity read's."""
    tenant = _admit_platform(monkeypatch)
    _configure_github(monkeypatch)

    def fake_urlopen(request, timeout=None):
        if "/actions/artifacts" in request.full_url:
            return _OversizeResponse(rr.MAX_ARTIFACT_BYTES)
        return _SmallJSONResponse({"id": REPO_ID})

    monkeypatch.setattr(rr.urllib.request, "urlopen", fake_urlopen)
    resp = _client(tenant).get("/api/receipts", params={"scope": "pr:988"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREACHABLE


def test_an_unexpected_artifact_shape_is_unreadable_not_a_crash(monkeypatch):
    _configure_github(monkeypatch)
    monkeypatch.setattr(rr, "_get_json", lambda url, *, headers, cap: (
        {"artifacts": "nope"} if "/actions/artifacts" in url else {"id": REPO_ID}))
    body = rr.read_receipts("pr:9")
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_UNREADABLE


def test_the_reconciler_read_is_cached_for_sixty_seconds(monkeypatch):
    monkeypatch.setenv(rr.ENV_RECONCILER_URL, "https://raw.example.com/latest.json")
    hits = []

    def fake(url, *, headers, cap):
        hits.append(url)
        return {"receipts": [{"at": "2026-09-02T12:00:00Z", "summary": "one"}]}

    monkeypatch.setattr(rr, "_get_json", fake)
    assert rr.RECONCILER_CACHE_SECONDS == 60.0
    rr.read_receipts("train")
    rr.read_receipts("train")
    rr.read_receipts("train")
    assert len(hits) == 1, "the 60 s cache must not re-read on every request"


def test_twenty_train_requests_cost_a_bounded_number_of_reconciler_reads(monkeypatch):
    """Round-2 finding 2: the module docstring claims every outbound call is
    bounded and cached; pinned at the volume the finding names."""
    monkeypatch.setenv(rr.ENV_RECONCILER_URL, "https://raw.example.com/latest.json")
    hits = []

    def fake(url, *, headers, cap):
        hits.append(url)
        return {"receipts": [{"at": "2026-09-02T12:00:00Z", "summary": "one"}]}

    monkeypatch.setattr(rr, "_get_json", fake)
    for _ in range(20):
        assert len(rr.read_receipts("train")["rows"]) == 1
    assert len(hits) == 1, hits


def test_a_failing_reconciler_read_is_cached_too(monkeypatch):
    """Round-2 finding 2: a failing reconciler read must be cached exactly
    like a success -- a repeatedly-unreachable receipt inbox costs one read
    per RECONCILER_CACHE_SECONDS, never one per request."""
    monkeypatch.setenv(rr.ENV_RECONCILER_URL, "https://raw.example.com/latest.json")
    hits = []

    def fake(url, *, headers, cap):
        hits.append(url)
        raise OSError("HTTP 500")

    monkeypatch.setattr(rr, "_get_json", fake)
    for _ in range(10):
        assert rr.read_receipts("train")["unavailable"][0]["reason"] == rr.REASON_UNREACHABLE
    assert len(hits) == 1, "the failure must be cached like a success"


def test_the_reconciler_shares_the_inflight_cap(monkeypatch):
    """Round-2 finding 2: _fetch_reconciler must acquire the SAME
    MAX_INFLIGHT_GITHUB semaphore the GitHub reads do -- the module docstring
    claims every outbound read this module makes is bounded that way, and the
    reconciler read was the one call site that skipped it."""
    monkeypatch.setenv(rr.ENV_RECONCILER_URL, "https://raw.example.com/latest.json")
    held = [rr._inflight.acquire(blocking=False) for _ in range(rr.MAX_INFLIGHT_GITHUB)]
    try:
        assert all(held)
        monkeypatch.setattr(rr, "_get_json", lambda *a, **k: pytest.fail(
            "over the inflight cap, the reconciler must not be read at all"))
        body = rr.read_receipts("train")
        assert body["rows"] == []
        assert body["unavailable"][0]["reason"] == rr.REASON_BUSY
    finally:
        for _ in held:
            rr._inflight.release()
    # and the refusal is not cached as if it were an answer
    monkeypatch.setattr(rr, "_get_json", lambda url, *, headers, cap: {
        "receipts": [{"at": "2026-09-02T12:00:00Z", "summary": "one"}]})
    assert len(rr.read_receipts("train")["rows"]) == 1


def test_the_artifact_read_is_cached_so_a_loop_cannot_burn_the_shared_budget(monkeypatch):
    """The PAT's 5000/hr also carries platform_customize's PR work."""
    _configure_github(monkeypatch)
    calls = []
    name = "prewarm-relay-receipt-pr-5"
    _stub_github(monkeypatch, {name: [_artifact(name)]},
                 run_paths={4242: PREWARM_WORKFLOW}, calls=calls)
    assert rr.ARTIFACT_CACHE_SECONDS == 60.0
    for _ in range(20):
        assert len(rr.read_receipts("pr:5")["rows"]) == 1
    # repo id + artifact listing + run path, once each, for twenty requests
    assert len(calls) == 3, [url for url, _h, _c in calls]


def test_a_failing_artifact_read_is_cached_too(monkeypatch):
    """A 403 loop costs one call per minute, not one per request."""
    _configure_github(monkeypatch)
    calls = []
    _stub_github(monkeypatch, artifacts_error=OSError("HTTP 403"), calls=calls)
    for _ in range(10):
        assert rr.read_receipts("pr:5")["unavailable"][0]["reason"] == rr.REASON_UNREACHABLE
    assert len(calls) == 2, "one repo lookup and one failed listing, then cached"


def test_the_inflight_cap_refuses_instead_of_holding_a_threadpool_slot(monkeypatch):
    """Sync `def` is deliberate (urllib blocks), so this semaphore is what
    bounds how many threadpool slots the route can occupy at once."""
    _configure_github(monkeypatch)
    name = "prewarm-relay-receipt-pr-5"
    _stub_github(monkeypatch, {name: [_artifact(name)]},
                 run_paths={4242: PREWARM_WORKFLOW})
    held = [rr._inflight.acquire(blocking=False) for _ in range(rr.MAX_INFLIGHT_GITHUB)]
    try:
        assert all(held)
        body = rr.read_receipts("pr:5")
        assert body["rows"] == []
        assert body["unavailable"][0]["reason"] == rr.REASON_BUSY
    finally:
        for _ in held:
            rr._inflight.release()
    # and the refusal is not cached as if it were an answer
    assert len(rr.read_receipts("pr:5")["rows"]) == 1


def test_the_memos_are_bounded(monkeypatch):
    """A hostile scope walk cannot grow an unbounded map."""
    for i in range(rr.MAX_MEMO_ENTRIES + 50):
        rr._memo_put(rr._run_path_memo, i, PREWARM_WORKFLOW)
    assert len(rr._run_path_memo) == rr.MAX_MEMO_ENTRIES


# --------------------------------------------------------------------------- #
# 6. the endpoint
# --------------------------------------------------------------------------- #
def _client(tenant=None):
    """``tenant`` overrides ``deps.require_tenant`` for this app instance only,
    so a test can arm ``_platform_scope_gate``'s own ``deps.auth_live()`` check
    (finding 5) without also routing ``require_tenant`` itself onto the live
    JWT path it would otherwise take once that same module-level flag flips.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from envelopes import install_error_handlers
    import routers.change_to_live as router_module

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router_module.router)
    if tenant is not None:
        app.dependency_overrides[deps.require_tenant] = lambda: tenant
    return TestClient(app, raise_server_exceptions=False)


def _grant_platform(monkeypatch, granted=True):
    """Grant (or withhold) the ``platform_customize`` capability alone. Used
    only by the ``job:`` scope tests below, where the platform gate is never
    even reached -- see ``_admit_platform`` for the full R7 chain."""
    monkeypatch.setattr(
        entitlements, "entitlements_for",
        lambda tier, roles=(), elevated=False: {"platform_customize": granted},
    )


def _admit_platform(monkeypatch, *, granted=True, rollout=True, tenant=None):
    """Arm the FULL R7 admission chain ``_platform_scope_gate`` now shares
    with ``platform_customize._gate`` (finding 5): live auth, the
    ``platform_customize`` entitlement, and -- when ``rollout`` -- the R7
    internal allowlist for the returned tenant. Returns the tenant id to pass
    to ``_client()``."""
    tenant = tenant if tenant is not None else deps.DEFAULT_TENANT
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    if rollout:
        monkeypatch.setenv("LEAF_CUSTOMIZATION_R7_MODE", "internal")
        monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", str(tenant))
    else:
        monkeypatch.delenv("LEAF_CUSTOMIZATION_R7_MODE", raising=False)
        monkeypatch.delenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", raising=False)
    monkeypatch.setattr(
        entitlements, "entitlements_for",
        lambda tier, roles=(), elevated=False: {"platform_customize": granted},
    )
    return tenant


def test_endpoint_returns_rows_and_the_envelope(monkeypatch):
    tenant = _admit_platform(monkeypatch)
    _configure_github(monkeypatch)
    _prewarm_fixture(monkeypatch)
    resp = _client(tenant).get("/api/receipts", params={"scope": "pr:988"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contract"] == rr.CONTRACT
    assert body["scope"] == "pr:988"
    assert len(body["rows"]) == 1
    assert "degraded_mode" in body


def test_endpoint_is_honest_when_no_source_is_configured(monkeypatch):
    tenant = _admit_platform(monkeypatch)
    resp = _client(tenant).get("/api/receipts", params={"scope": "pr:988"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert body["unavailable"][0]["reason"] == rr.REASON_NO_CREDENTIAL


@pytest.mark.parametrize("bad", ["", "pr:0", "tree:zz", "job:a/b", "nonsense"])
def test_endpoint_fails_closed_on_a_malformed_scope(bad):
    assert _client().get("/api/receipts", params={"scope": bad}).status_code == 422


def test_endpoint_requires_a_scope():
    assert _client().get("/api/receipts").status_code == 422


# --------------------------------------------------------------------------- #
# 7. the platform scopes are not tenant data
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("scope", ["pr:988", "tree:" + "a" * 40, "train"])
def test_a_platform_scope_is_403_without_the_entitlement(monkeypatch, scope):
    """Any signed-in account of any tier would otherwise walk pr:1..N and
    tree:<sha> and read back a private repository's CI state."""
    tenant = _admit_platform(monkeypatch, granted=False)
    _configure_github(monkeypatch)
    monkeypatch.setattr(rr, "_get_json", lambda *a, **k: pytest.fail(
        "the gate must refuse BEFORE any outbound call"))
    resp = _client(tenant).get("/api/receipts", params={"scope": scope})
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["entitlement_required"] is True
    assert body["required"] == "platform_customize"
    assert "rows" not in body, "a refusal must not carry an answer"


def test_the_default_tier_does_not_carry_the_platform_capability():
    """Nothing is granted by accident: the gate is real on the shipped policy."""
    caps = entitlements.entitlements_for(entitlements.DEFAULT_TIER)
    assert caps.get("platform_customize") is not True


def test_an_unreadable_entitlement_policy_fails_closed_with_503(monkeypatch):
    tenant = deps.DEFAULT_TENANT
    monkeypatch.setattr(deps, "auth_live", lambda: True)

    def explode(*args, **kwargs):
        raise entitlements.EntitlementsError("policy unreadable")

    monkeypatch.setattr(entitlements, "entitlements_for", explode)
    resp = _client(tenant).get("/api/receipts", params={"scope": "train"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["entitlement_required"] is True


# --------------------------------------------------------------------------- #
# 7b. finding 5: the platform gate shares live-auth and the R7 rollout with
# platform_customize._gate, reused rather than re-derived
# --------------------------------------------------------------------------- #
def test_a_platform_scope_is_503_when_auth_is_not_live(monkeypatch):
    """auth_live() false means the whole platform-CI surface is dark, the same
    503 platform_customize._gate answers its own callers with."""
    tenant = _admit_platform(monkeypatch)
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    monkeypatch.setattr(rr, "_get_json", lambda *a, **k: pytest.fail(
        "the gate must refuse BEFORE any outbound call"))
    resp = _client(tenant).get("/api/receipts", params={"scope": "train"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["reason_code"] == "platform_customize_auth_required"


def test_a_platform_scope_is_refused_when_the_r7_rollout_is_off(monkeypatch):
    """Live auth and the entitlement both pass, but the tenant is not on the R7
    internal allowlist: the SAME refusal platform_customize._gate gives for a
    disabled rollout, never a silent 403 or a fabricated read."""
    tenant = _admit_platform(monkeypatch, rollout=False)
    monkeypatch.setattr(rr, "_get_json", lambda *a, **k: pytest.fail(
        "the gate must refuse BEFORE any outbound call"))
    resp = _client(tenant).get("/api/receipts", params={"scope": "train"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["reason_code"] == "platform_customize_disabled"


def test_a_malformed_tenant_identity_is_refused_the_same_way_the_sibling_gate_is(monkeypatch):
    """Round-3 regression pin (finding 5): `_platform_scope_gate` had
    re-derived `platform_customize._gate`'s chain by hand and silently
    dropped `is_valid_tenant_id`. A tenant id placed on the R7 internal
    allowlist that is NOT a canonical tenant id (uppercase + a space) must be
    refused here exactly like `platform_customize._gate` refuses it for its
    own callers -- 403 `tenant_identity_invalid` -- never served, and never
    reaching an outbound call."""
    tenant = _admit_platform(monkeypatch, tenant="Acme Corp")
    monkeypatch.setattr(rr, "_get_json", lambda *a, **k: pytest.fail(
        "the gate must refuse BEFORE any outbound call"))
    resp = _client(tenant).get("/api/receipts", params={"scope": "train"})
    assert resp.status_code == 403, resp.text
    assert resp.json()["reason_code"] == "tenant_identity_invalid"


def test_a_job_scope_needs_no_platform_entitlement(monkeypatch, tmp_path):
    """`job:` is the caller's OWN data, gated by tenancy, not by this capability."""
    _grant_platform(monkeypatch, granted=False)
    monkeypatch.setenv(rr.ENV_JOB_RECEIPT_DIR, str(tmp_path))
    job_dir = tmp_path / "job-77"
    job_dir.mkdir()
    (job_dir / "receipt.json").write_text(
        json.dumps({"summary": "mine", "at": "2026-09-03T00:00:00Z"}), encoding="utf-8")

    import deps
    import routers.jobs as jobs_router
    monkeypatch.setattr(
        jobs_router, "_job_for_tenant",
        lambda job_id, tid: {"job_id": job_id, "tenant_id": tid},
    )
    monkeypatch.setattr(jobs_router, "_bound_tenant_id", lambda tenant: str(deps.DEFAULT_TENANT))

    resp = _client().get("/api/receipts", params={"scope": "job:job-77"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows"][0]["summary"] == "mine"


def test_a_job_scope_for_another_tenants_job_is_404(monkeypatch, tmp_path):
    """The same no-existence-leak answer GET /api/jobs/{id} gives (F8)."""
    monkeypatch.setenv(rr.ENV_JOB_RECEIPT_DIR, str(tmp_path))
    job_dir = tmp_path / "job-77"
    job_dir.mkdir()
    (job_dir / "receipt.json").write_text(json.dumps({"summary": "leaked"}), encoding="utf-8")

    import routers.jobs as jobs_router
    monkeypatch.setattr(jobs_router, "_job_for_tenant", lambda job_id, tenant_id: None)

    resp = _client().get("/api/receipts", params={"scope": "job:job-77"})
    assert resp.status_code == 404
    assert "leaked" not in resp.text
