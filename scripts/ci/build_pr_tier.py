#!/usr/bin/env python3
"""Build the dormant Studio PR tier from admitted history and the live catalog.

No tests are collected. Unknown ownership is retained, never inferred safe from
an absence of failures. Activation requires the explicit --enabled flag.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
REPO_SLUG = "LEAF-Solar-Design/leaf-web-demo"
SCHEMA = "leaf.ci.pr-tier.v1"


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def load_input(path):
    raw = Path(path).read_bytes()
    return json.loads(raw, object_pairs_hook=unique_object), hashlib.sha256(raw).hexdigest()


def suite_ids(value, name):
    if (not isinstance(value, list)
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", v) for v in value)
            or len(value) != len(set(value))):
        raise ValueError("invalid " + name)
    return set(value)


def load_runner():
    # Reuse the runner's ownership and prerequisite rules without changing its
    # public --list document (and therefore its catalog fingerprint).
    spec = importlib.util.spec_from_file_location("leaf_pr_tier_runner", ROOT / "scripts/run-all-gates.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    previous = sys.path[:]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


def validate_catalog(catalog):
    if (not isinstance(catalog, dict) or catalog.get("schema") != "leaf.ci.catalog.v1"
            or not isinstance(catalog.get("catalog_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", catalog["catalog_sha256"])
            or not isinstance(catalog.get("suites"), list) or not catalog["suites"]):
        raise ValueError("invalid catalog")
    ids = []
    for row in catalog["suites"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not isinstance(row.get("cwd"), str) or not row["cwd"]
                or not isinstance(row.get("kind"), str)
                or not isinstance(row.get("command"), list) or not row["command"]
                or any(not isinstance(a, str) for a in row["command"])):
            raise ValueError("invalid catalog suite")
        ids.append(row["id"])
    return suite_ids(ids, "catalog suite IDs")


def build_document(proposal, failures, sentinels, catalog, input_sha256s, runner=None):
    """Pure policy construction; validate every input before any output write."""
    if not isinstance(proposal, dict) or proposal.get("schema") != SCHEMA:
        raise ValueError("invalid proposal schema")
    proposed = suite_ids(proposal.get("suite_ids"), "proposal suite_ids")
    groups = {key: suite_ids(proposal.get(key), "proposal " + key)
              for key in ("catchers", "hard_harm_sentinels", "prerequisites")}
    if proposed != set().union(*groups.values()):
        raise ValueError("proposal groups do not match suite_ids")
    sentinels = suite_ids(sentinels, "sentinels")
    if not isinstance(failures, dict) or REPO_SLUG not in failures:
        raise ValueError("missing repository failure history")
    for repo, history in failures.items():
        if (not isinstance(repo, str) or not isinstance(history, dict)
                or any(type(history.get(k)) is not int or history[k] < 0 for k in ("runs", "logs"))
                or history["logs"] > history["runs"]
                or not isinstance(history.get("fail_files"), dict)
                or any(not isinstance(path, str) or not path or type(count) is not int or count < 1
                       for path, count in history["fail_files"].items())):
            raise ValueError("invalid failure history: " + str(repo))
    ids = validate_catalog(catalog)
    dependencies = {}
    for row in catalog["suites"]:
        deps = suite_ids(row.get("prerequisites", []), "catalog prerequisites")
        if not deps <= ids:
            raise ValueError("prerequisite absent from catalog")
        if deps:
            dependencies[row["id"]] = sorted(deps)
    runner = runner or load_runner()
    reasons = {}

    def include(sid, reason):
        if sid in ids:
            reasons.setdefault(sid, set()).add(reason)

    for sid in proposed:
        include(sid, "proposal")
    for sid in sentinels:
        include(sid, "hard-harm-sentinel")
    include("web-version-restore-proof", "studio-keep")
    # An optional baseline makes a future regeneration retain every new suite,
    # even when that suite has an otherwise perfectly understood owner.
    baseline = (suite_ids(proposal["catalog_suite_ids"], "proposal catalog_suite_ids")
                if "catalog_suite_ids" in proposal else ids)
    fail_files = failures[REPO_SLUG]["fail_files"]
    for row in catalog["suites"]:
        sid = row["id"]
        if sid not in baseline:
            include(sid, "new-catalog-suite")
        if not runner.pr_tier_owned_paths(row) and row["kind"] != "vitest":
            include(sid, "unknown-ownership")
        for path in fail_files:
            if runner.pr_tier_owns(row, path):
                include(sid, "fail30:" + runner.pr_tier_path(path))
    closed = runner.pr_tier_closure(reasons, ids, dependencies)
    for sid in closed - reasons.keys():
        include(sid, "prerequisite")
    return {
        "schema": SCHEMA,
        "enabled": False,
        "generated_from": {"input_sha256s": dict(sorted(input_sha256s.items())),
                           "catalog_sha256": catalog["catalog_sha256"]},
        "catalog_suite_ids": sorted(ids),
        "suite_ids": sorted(reasons),
        "reasons": {sid: sorted(reasons[sid]) for sid in sorted(reasons)},
        "prerequisites": dict(sorted(dependencies.items())),
    }


def generate(proposal_path, failures_path, sentinels_path, catalog=None, *, enabled=False):
    documents, digests = [], {}
    for name, path in (("proposal", proposal_path), ("fail30", failures_path), ("sentinels", sentinels_path)):
        doc, digest = load_input(path)
        documents.append(doc)
        digests[name] = digest
    if catalog is None:
        proc = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/run-all-gates.py"), "--list"],
                              cwd=ROOT, capture_output=True, text=True, check=True, timeout=60)
        catalog = json.loads(proc.stdout, object_pairs_hook=unique_object)
    document = build_document(*documents, catalog, digests)
    document["enabled"] = enabled
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, default=ROOT / "pr-tier-proposal.json")
    parser.add_argument("--fail30", type=Path, default=ROOT / "fail30.json")
    parser.add_argument("--sentinels", type=Path, default=ROOT / "sentinels-strict.json")
    parser.add_argument("--output", type=Path, default=ROOT / "scripts/ci/pr-tier.json")
    parser.add_argument("--enabled", action="store_true", help="explicitly activate the generated PR tier")
    parser.add_argument("--stdout", action="store_true", help="print the document without writing files")
    parser.add_argument("--active", type=Path, metavar="POLICY",
                        help="print 1 only for a PR event with a usable, enabled tier policy; otherwise 0")
    args = parser.parse_args(argv)
    if args.active is not None:
        is_pr = os.environ.get("CODEBUILD_WEBHOOK_EVENT") in (
            "PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED")
        print(int(is_pr and load_runner().load_pr_tier(args.active, quiet=True) is not None))
        return 0
    try:
        document = generate(args.proposal, args.fail30, args.sentinels, enabled=args.enabled)
        serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("PR tier generation refused: " + str(exc), file=sys.stderr)
        return 2
    if args.stdout:
        print(serialized, end="")
    else:
        args.output.write_text(serialized, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
