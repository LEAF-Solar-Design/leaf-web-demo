"""Synthetic catalog binding checks; no real fixtures, browser, or network."""

from contextlib import contextmanager
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benches import x09
import xbench_core


SHA = "a" * 40
DIGEST = "b" * 64
# Fictional redaction sentinel, not a stored credential.
SECRET = " ".join(("Bearer", "x09-unit-redaction-canary"))


def context(tmp_path, **changes):
    ctx = SimpleNamespace(
        source_sha=SHA, base=x09.xbench_session.STAGING_ORIGIN, out_dir=tmp_path,
        manifest={"fixtures": []}, fixtures=[], options={},
    )
    for key, value in changes.items():
        setattr(ctx, key, value)
    return ctx


def published(name="sample-tool", **changes):
    row = {"name": name, "version": "1.0.0", "catalog_digest": "sha256:" + DIGEST,
           "package_digest": DIGEST}
    row.update(changes)
    return row


def flat(count=63):
    return {"tools": [{"name": "tool_" + str(n), "version": "1.0.0",
                       "catalog_digest": "sha256:" + DIGEST,
                       "tool_manifest_sha256": "sha256:" + DIGEST}
                      for n in range(count)], "error": None, "degraded_mode": False}


def evaluate(ctx, signed=None, flat_status=200, flat_body=None, published_status=200):
    default_flat = {"tools": [{k: v for k, v in published().items() if k != "package_digest"}]}
    return x09.evaluate_catalog(ctx, flat_status, default_flat if flat_body is None else flat_body,
                                published_status, {"tools": [published()] if signed is None else signed})


def assert_six(rows):
    assert isinstance(rows, list)
    assert all(isinstance(row, xbench_core.Step) for row in rows)
    assert [row.n for row in rows] == [1, 2, 3, 4, 5, 6]
    assert {row.verdict for row in rows} == {"UNREADABLE"}
    assert xbench_core.aggregate(rows) == "UNREADABLE"
    for row, producer in zip(rows, (
        "generated-run", "gate", "nightly", "real pair assertion execution", "dedupe", "seeded-break",
    )):
        assert producer in row.evidence
        assert "missing" in row.evidence


def golden_context(tmp_path):
    ctx = context(tmp_path)
    fixture = {"id": "synthetic-sample", "sha256": "c" * 64, "bytes": 0,
               "path": "never-open-this-synthetic-path"}
    ctx.manifest = {"fixtures": [fixture], "goldens": [{
        **published(), "fixture_id": fixture["id"], "fixture_sha256": fixture["sha256"],
        "oracle": {"rule": "count", "value": 0},
        "lane_review": {"lane": "synthetic-lane", "reviewer": "synthetic-reviewer",
                        "receipt_sha256": "d" * 64},
    }]}
    ctx.fixtures = [{**fixture, "ok": True, "observed_bytes": 0,
                     "observed_sha256": fixture["sha256"]}]
    return ctx


def test_all_flat_tools_require_goldens_without_signed_packages(tmp_path):
    rows = evaluate(context(tmp_path), signed=[], flat_body=flat())
    assert_six(rows)
    assert "missing manifest goldens" in rows[0].evidence
    assert "empty signed" not in rows[0].evidence
    audit = json.loads((tmp_path / "catalog-audit.json").read_text())
    assert audit["source"] == SHA
    assert audit["flat"]["count"] == 63
    assert audit["published"]["count"] == 0
    assert "partial catalog-binding slice" in x09.LIMITS.lower()
    assert "no overall PASS possible" in x09.LIMITS


def test_unsigned_effective_catalog_metadata_can_bind(tmp_path):
    ctx = golden_context(tmp_path)
    del ctx.manifest["goldens"][0]["package_digest"]
    rows = evaluate(ctx, signed=[])
    assert_six(rows)
    assert "golden metadata bound" in rows[0].evidence


def test_effective_catalog_digest_is_required_and_pinned(tmp_path):
    body = flat(1)
    del body["tools"][0]["catalog_digest"]
    assert "identity unreadable" in evaluate(context(tmp_path), flat_body=body)[0].evidence
    ctx = golden_context(tmp_path)
    ctx.manifest["goldens"][0]["catalog_digest"] = "sha256:" + "e" * 64
    assert "actual published" in evaluate(ctx)[0].evidence


def test_both_catalogs_empty_remain_unreadable(tmp_path):
    rows = evaluate(context(tmp_path), signed=[], flat_body={"tools": []})
    assert_six(rows)
    assert "empty effective" in rows[0].evidence


@pytest.mark.parametrize("changes", [
    {"name": "Bad Name"}, {"name": "ab"}, {"name": "abc/def"}, {"name": 1},
    {"version": "01.0.0"}, {"version": "1.0"}, {"version": None},
    {"package_digest": "B" * 64}, {"package_digest": "sha256:" + DIGEST},
    {"package_digest": None}, {"tool_manifest_sha256": SECRET},
])
def test_malformed_signed_identities(tmp_path, changes):
    rows = evaluate(context(tmp_path), signed=[published(**changes)])
    assert_six(rows)
    assert "identity unreadable" in rows[0].evidence
    assert SECRET not in (tmp_path / "catalog-audit.json").read_text()


def test_duplicate_name_version_even_with_different_digest(tmp_path):
    rows = evaluate(context(tmp_path), signed=[published(), published(package_digest="c" * 64)])
    assert_six(rows)
    assert "identity unreadable" in rows[0].evidence


@pytest.mark.parametrize("status", [None, True, 201, 302, 401, 403, 404, 500, SECRET])
@pytest.mark.parametrize("endpoint", ["flat", "published"])
def test_non_200_is_unreadable_and_sanitized(tmp_path, status, endpoint):
    ctx = context(tmp_path)
    args = [200, flat(), 200, {"tools": [published()]}]
    args[0 if endpoint == "flat" else 2] = status
    rows = x09.evaluate_catalog(ctx, *args)
    assert_six(rows)
    assert SECRET not in repr(rows)
    assert SECRET not in (tmp_path / "catalog-audit.json").read_text()


@pytest.mark.parametrize("body", [
    None, [], SECRET, {}, {"tools": {}}, {"tools": [None]},
    {"tools": [{}]}, {"tools": [], "error": {"message": SECRET}},
    {"tools": [], "detail": SECRET}, {"tools": [], "errors": [SECRET]},
    {"tools": [], "ok": False}, {"tools": [], "degraded_mode": True},
])
@pytest.mark.parametrize("endpoint", ["flat", "published"])
def test_bad_bodies_and_error_envelopes(tmp_path, body, endpoint):
    args = [200, flat(), 200, {"tools": [published()]}]
    args[1 if endpoint == "flat" else 3] = body
    rows = x09.evaluate_catalog(context(tmp_path), *args)
    assert_six(rows)
    assert "catalog status/body/identity unreadable" in rows[0].evidence
    assert SECRET not in repr(rows)
    assert SECRET not in (tmp_path / "catalog-audit.json").read_text()


def test_audit_allowlist_discards_extra_values_and_fields(tmp_path):
    row = published(tiers=[SECRET], signature=SECRET, authorization=SECRET,
                    tool_manifest_sha256="sha256:" + DIGEST)
    rows = x09.evaluate_catalog(context(tmp_path), 200, flat(), 200,
                                {"tools": [row], "authorization": SECRET, SECRET: SECRET})
    assert_six(rows)
    raw = (tmp_path / "catalog-audit.json").read_text()
    assert SECRET not in raw
    assert "authorization" not in raw
    assert "signature" not in raw
    audit = json.loads(raw)
    assert audit["published"]["tools"] == [{
        "name": "sample-tool", "version": "1.0.0", "package_digest": DIGEST,
        "catalog_digest": "sha256:" + DIGEST,
        "tool_manifest_sha256": "sha256:" + DIGEST,
        "fields": ["catalog_digest", "name", "package_digest", "tool_manifest_sha256", "version"],
    }]


def test_flat_package_digest_is_validated_before_audit(tmp_path):
    body = flat(1)
    body["tools"][0]["package_digest"] = SECRET
    rows = evaluate(context(tmp_path), flat_body=body)
    assert_six(rows)
    assert "identity unreadable" in rows[0].evidence
    assert SECRET not in (tmp_path / "catalog-audit.json").read_text()


def test_duplicate_flat_identity_is_unreadable(tmp_path):
    body = flat(1)
    body["tools"] *= 2
    rows = evaluate(context(tmp_path), flat_body=body)
    assert_six(rows)
    assert "identity unreadable" in rows[0].evidence


def test_goldens_are_optional_but_absence_is_unreadable(tmp_path):
    rows = evaluate(context(tmp_path))
    assert_six(rows)
    assert "missing manifest goldens" in rows[0].evidence


@pytest.mark.parametrize("rule,value", [
    ("sha256", DIGEST), ("count", 0), ("exact-values", {"items": [1, True]}),
])
def test_valid_metadata_never_claims_generated_execution(tmp_path, rule, value):
    ctx = golden_context(tmp_path)
    ctx.manifest["goldens"][0]["oracle"] = {"rule": rule, "value": value}
    rows = evaluate(ctx)
    assert_six(rows)
    assert "golden metadata bound" in rows[0].evidence
    assert "missing generated-run producer evidence" in rows[0].evidence


@pytest.mark.parametrize("gap,expected", [
    ("other-tool", "actual published"), ("wrong-package", "actual published envelope"),
    ("duplicate", "uniquely cover"), ("coverage", "missing golden coverage"),
    ("fixture-id", "verified sample"), ("fixture-hash", "verified sample"),
    ("unverified", "verified sample"), ("observed-hash", "verified sample"),
    ("oracle", "explicit"), ("implicit-oracle", "explicit"),
    ("bad-count", "explicit"), ("bad-sha", "explicit"),
    ("review", "lane review"), ("review-hash", "lane review"),
])
def test_golden_gaps(tmp_path, gap, expected):
    ctx = golden_context(tmp_path)
    golden = ctx.manifest["goldens"][0]
    signed = [published()]
    if gap == "other-tool":
        golden["name"] = "other-tool"
    elif gap == "wrong-package":
        golden["package_digest"] = "e" * 64
    elif gap == "duplicate":
        ctx.manifest["goldens"].append(copy.deepcopy(golden))
    elif gap == "coverage":
        signed.append(published(name="other-tool"))
    elif gap == "fixture-id":
        golden["fixture_id"] = "other-sample"
    elif gap == "fixture-hash":
        golden["fixture_sha256"] = "e" * 64
    elif gap == "unverified":
        ctx.fixtures[0]["ok"] = False
    elif gap == "observed-hash":
        ctx.fixtures[0]["observed_sha256"] = "e" * 64
    elif gap == "oracle":
        del golden["oracle"]
    elif gap == "implicit-oracle":
        golden["oracle"] = {"value": 0}
    elif gap == "bad-count":
        golden["oracle"] = {"rule": "count", "value": True}
    elif gap == "bad-sha":
        golden["oracle"] = {"rule": "sha256", "value": "bad"}
    elif gap == "review":
        del golden["lane_review"]
    elif gap == "review-hash":
        golden["lane_review"]["receipt_sha256"] = "bad"
    rows = evaluate(ctx, signed=signed)
    assert_six(rows)
    assert expected in rows[0].evidence


def fake_session(monkeypatch, *, failure=None):
    events = []
    page = object()

    @contextmanager
    def signed_in(repo, source, *, origin):
        events.append(("open", repo, source, origin))
        try:
            if failure == "login":
                raise RuntimeError(SECRET)
            yield page, {"authorization": SECRET, "approve_stderr_tail": SECRET}
        finally:
            events.append("closed")

    def api_json(actual_page, method, path, *, origin):
        assert actual_page is page
        events.append((method, path, origin))
        if failure == path:
            raise RuntimeError(SECRET)
        return 200, flat() if path == "/api/tools" else {"tools": []}

    monkeypatch.setattr(x09.xbench_session, "signed_in", signed_in)
    monkeypatch.setattr(x09.xbench_session, "api_json", api_json)
    return events


@pytest.mark.parametrize("failure", [None, "login", "/api/tools", "/api/tool-envelopes"])
def test_session_source_origin_api_and_finally_closure(tmp_path, monkeypatch, failure):
    events = fake_session(monkeypatch, failure=failure)
    rows = x09.run(context(tmp_path))
    assert_six(rows)
    assert events[0] == ("open", Path(x09.__file__).resolve().parents[3], SHA,
                          x09.xbench_session.STAGING_ORIGIN)
    assert events[-1] == "closed"
    if failure is None:
        assert events[1:3] == [
            ("GET", "/api/tools", x09.xbench_session.STAGING_ORIGIN),
            ("GET", "/api/tool-envelopes", x09.xbench_session.STAGING_ORIGIN),
        ]
    assert SECRET not in repr(rows)
    assert SECRET not in (tmp_path / "catalog-audit.json").read_text()


@pytest.mark.parametrize("candidate", [None, "b" * 40, "A" * 40, "a" * 39, SECRET])
def test_gate_candidate_drift_refuses_session(tmp_path, monkeypatch, candidate):
    events = fake_session(monkeypatch)
    ctx = context(tmp_path, options={"mode": "gate", "candidate": candidate})
    rows = x09.run(ctx)
    assert_six(rows)
    assert not events
    assert "equal ctx.source_sha" in rows[0].evidence
    assert SECRET not in repr(rows)
    assert "equal ctx.source_sha" in evaluate(ctx)[0].evidence


def test_matching_gate_candidate_uses_same_source(tmp_path, monkeypatch):
    events = fake_session(monkeypatch)
    assert_six(x09.run(context(tmp_path, options={"mode": "gate", "candidate": SHA})))
    assert events[0][2] == SHA


@pytest.mark.parametrize("base", ["https://example.test", x09.xbench_session.STAGING_ORIGIN + "/",
                                  x09.xbench_session.STAGING_ORIGIN + ".evil.test"])
def test_exact_origin_required(tmp_path, monkeypatch, base):
    events = fake_session(monkeypatch)
    rows = x09.run(context(tmp_path, base=base))
    assert_six(rows)
    assert not events
    assert "exact staging origin" in rows[0].evidence


@pytest.mark.parametrize("source", [None, "A" * 40, "a" * 39, SECRET])
def test_invalid_source_never_reaches_session_or_audit(tmp_path, monkeypatch, source):
    events = fake_session(monkeypatch)
    rows = x09.run(context(tmp_path, source_sha=source))
    assert_six(rows)
    assert not events
    audit = json.loads((tmp_path / "catalog-audit.json").read_text())
    assert audit["source"] is None
    assert SECRET not in repr(rows)
    assert SECRET not in json.dumps(audit)


def test_audit_failure_does_not_echo_exception(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError(SECRET)
    monkeypatch.setattr(xbench_core, "write_json_atomic", fail)
    rows = evaluate(context(tmp_path))
    assert_six(rows)
    assert "could not be persisted" in rows[0].evidence
    assert SECRET not in repr(rows)
