"""Partial X09 catalog binding; no generated-run or pair execution producer.

Optional manifest ``goldens`` pins every effective catalog name, version and
catalog_digest, plus package_digest when a signed envelope exists. It also pins
fixture_id, fixture_sha256, oracle (rule/value), and lane_review
(lane, reviewer, receipt_sha256). Fixture references must match both the manifest
and the harness's verified fixture rows. Metadata alone never proves execution.
"""

from pathlib import Path
import re

import xbench_core
import xbench_session
from benches.x09_regression import compare_pair, is_catalog_digest


LIMITS = (
    "Partial catalog-binding slice only; no overall PASS possible. Catalog and "
    "golden metadata do not prove generated runs, gate execution, nightly coverage, "
    "real pair assertions, deduplication, or seeded-break detection."
)
_MISSING = (
    "generated-run producer evidence bound to source, published package and sample fixture",
    "gate producer evidence: candidate-source-bound gate execution receipt",
    "nightly producer evidence: <=20 pairs/night schedule and seven-day coverage receipts",
    "real pair assertion execution producer evidence: tool/drawing runs and oracle results",
    "dedupe producer evidence: repeated-pair execution and deduplication receipts",
    "seeded-break producer evidence: deliberate regression and detection receipt",
)
_FIELDS = frozenset(("tools", "name", "version", "catalog_digest", "package_digest", "tool_manifest_sha256"))


def _hex(value, length=64):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _identity(row):
    return (type(row) is dict and type(row.get("name")) is str
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", row["name"]) is not None
            and type(row.get("version")) is str
            and re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", row["version"]) is not None
            and (is_catalog_digest(row.get("catalog_digest"))
                 if "catalog_digest" in row else _hex(row.get("package_digest"))))


def _signed_identity(row):
    return (type(row) is dict and type(row.get("name")) is str
            and 3 <= len(row["name"]) <= 64
            and re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", row["name"]) is not None
            and type(row.get("version")) is str
            and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
                             row["version"]) is not None
            and _hex(row.get("package_digest")))


def _catalog(status, body, signed=False):
    if (type(status) is not int or status != 200 or type(body) is not dict
            or body.get("error") is not None or body.get("ok", True) is not True
            or body.get("degraded_mode", False) is not False
            or "detail" in body or "errors" in body
            or type(body.get("tools")) is not list):
        return None
    rows = body["tools"]
    seen = set()
    for row in rows:
        if signed:
            valid = _signed_identity(row)
        else:
            valid = _identity(row) and is_catalog_digest(row.get("catalog_digest"))
        if not valid:
            return None
        if "catalog_digest" in row and not is_catalog_digest(row["catalog_digest"]):
            return None
        if "package_digest" in row and not _hex(row["package_digest"]):
            return None
        key = (row["name"], row["version"])
        if key in seen:
            return None
        seen.add(key)
        manifest_hash = row.get("tool_manifest_sha256")
        if manifest_hash is not None and not (
            _hex(manifest_hash) or (type(manifest_hash) is str
                                    and manifest_hash.startswith("sha256:")
                                    and _hex(manifest_hash[7:]))
        ):
            return None
    return rows


def _summary(status, body, rows):
    # Only known field names and validated identity/hash values leave memory.
    return {
        "status": status if type(status) is int and 100 <= status <= 599 else None,
        "count": len(rows) if rows is not None else None,
        "fields": sorted(set(body) & _FIELDS) if type(body) is dict else [],
        "tools": [dict({key: row[key] for key in
                        ("name", "version", "catalog_digest", "package_digest", "tool_manifest_sha256")
                        if key in row}, fields=sorted(set(row) & _FIELDS))
                  for row in rows] if rows is not None else [],
    }


def _goldens(ctx, published):
    manifest = ctx.manifest
    goldens = manifest.get("goldens") if type(manifest) is dict else None
    if type(goldens) is not list or not goldens:
        return "missing manifest goldens for actual published identities"
    fixtures = manifest.get("fixtures", [])
    verified = getattr(ctx, "fixtures", [])
    if type(fixtures) is not list or type(verified) is not list:
        return "missing verified sample fixture identity/hash"
    covered = set()
    actual = {(r["name"], r["version"], r.get("catalog_digest")): r for r in published}
    for golden in goldens:
        if not _identity(golden):
            return "invalid golden published identity"
        key = (golden["name"], golden["version"], golden.get("catalog_digest"))
        if key not in actual or key in covered:
            return "golden identity does not uniquely cover actual published catalog"
        if golden.get("package_digest") != actual[key].get("package_digest"):
            return "golden package digest does not match actual published envelope"
        covered.add(key)
        fid, digest = golden.get("fixture_id"), golden.get("fixture_sha256")
        if (type(fid) is not str or not fid or not _hex(digest)
                or not any(type(f) is dict and f.get("id") == fid
                           and f.get("sha256") == digest for f in fixtures)
                or not any(type(f) is dict and f.get("id") == fid and f.get("ok") is True
                           and f.get("sha256") == digest and f.get("observed_sha256") == digest
                           and type(f.get("bytes")) is int and f["bytes"] >= 0
                           and f.get("observed_bytes") == f["bytes"] for f in verified)):
            return "missing verified sample fixture identity/hash"
        oracle = golden.get("oracle")
        # Self-comparison checks oracle syntax only, never claims a tool ran.
        if (type(oracle) is not dict or "value" not in oracle
                or compare_pair(oracle, oracle["value"])["verdict"] == "UNREADABLE"):
            return "missing explicit sha256/count/exact-values oracle"
        review = golden.get("lane_review")
        if (type(review) is not dict or not _hex(review.get("receipt_sha256"))
                or any(type(review.get(k)) is not str or not review[k].strip()
                       for k in ("lane", "reviewer"))):
            return "missing lane review receipt metadata"
    if covered != set(actual):
        return "missing golden coverage for actual published identities"
    return None


def _binding_error(ctx):
    if ctx.base != xbench_session.STAGING_ORIGIN or not _hex(ctx.source_sha, 40):
        return "exact staging origin and valid source SHA required"
    options = getattr(ctx, "options", {})
    if type(options) is not dict:
        return "invalid grader options"
    if options.get("mode") == "gate" and (
        not _hex(options.get("candidate"), 40) or options["candidate"] != ctx.source_sha
    ):
        return "gate candidate must be 40 lowercase hex and equal ctx.source_sha; no candidate execution proven"
    return None


def evaluate_catalog(ctx, flat_status, flat_body, published_status, published_body):
    """Validate catalog binding and return the six frozen acceptance rows."""
    flat = _catalog(flat_status, flat_body)
    published = _catalog(published_status, published_body, signed=True)
    reason = _binding_error(ctx)
    if reason is None:
        if flat is None or published is None:
            reason = "catalog status/body/identity unreadable; no product failure inferred"
        elif not flat and not published:
            reason = "empty effective and signed catalogs; no golden or generated-run binding possible"
        else:
            # Effective tools are the primary published set. Signed packages
            # enrich matching tools, or join as package-pinned additional tools.
            effective = {(r["name"], r["version"]): dict(r) for r in flat}
            for package in published:
                key = (package["name"], package["version"])
                row = effective.setdefault(key, {"name": key[0], "version": key[1]})
                if ("package_digest" in row
                        and row["package_digest"] != package["package_digest"]):
                    reason = "conflicting package digests in published catalogs"
                    break
                row["package_digest"] = package["package_digest"]
            if reason is None:
                reason = _goldens(ctx, list(effective.values()))
    source = ctx.source_sha if _hex(ctx.source_sha, 40) else None
    audit = {"source": source, "flat": _summary(flat_status, flat_body, flat),
             "published": _summary(published_status, published_body, published)}
    try:
        xbench_core.write_json_atomic(Path(ctx.out_dir) / "catalog-audit.json", audit)
    except Exception:
        reason = "sanitized catalog-audit.json could not be persisted"
    evidence = "catalog-audit.json; " + ((reason + "; ") if reason else "golden metadata bound; ")
    return [xbench_core.Step(1, "UNREADABLE", evidence + "missing " + _MISSING[0])] + [
        xbench_core.Step(n, "UNREADABLE", "missing " + _MISSING[n - 1])
        for n in range(2, 7)
    ]


def run(ctx):
    """Read both catalogs in the shared session, whose finally closes the browser."""
    flat_status = published_status = None
    flat_body = published_body = None
    if _binding_error(ctx) is None:
        try:
            with xbench_session.signed_in(
                Path(__file__).resolve().parents[3], ctx.source_sha,
                origin=xbench_session.STAGING_ORIGIN,
            ) as (page, _info):
                flat_status, flat_body = xbench_session.api_json(
                    page, "GET", "/api/tools", origin=xbench_session.STAGING_ORIGIN)
                published_status, published_body = xbench_session.api_json(
                    page, "GET", "/api/tool-envelopes", origin=xbench_session.STAGING_ORIGIN)
        except Exception:
            # Never include session/API exceptions or authentication metadata.
            pass
    return evaluate_catalog(ctx, flat_status, flat_body, published_status, published_body)
