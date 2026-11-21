#!/usr/bin/env python3
"""Build the dormant Studio PR tier from admitted history and the live catalog.

No tests are collected. Unknown ownership is retained, never inferred safe from
an absence of failures. Activation requires the explicit --enabled flag.
"""

import argparse
from datetime import date
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
REPO_SLUG = "LEAF-Solar-Design/leaf-web-demo"
SCHEMA = "leaf.ci.pr-tier.v1"
CONTRACT_KEEP_PATH = Path(__file__).with_name("contract-keep.json")


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
    if not isinstance(value, list):
        raise ValueError("invalid " + name)
    ids = set()
    for sid in value:
        if not isinstance(sid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", sid):
            raise ValueError(f"invalid {name}: {sid!r}")
        if sid in ids:
            raise ValueError(f"duplicate {name}: {sid}")
        ids.add(sid)
    return ids


def validate_date(value, field):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            date.fromisoformat(value)
            return
        except ValueError:
            pass
    raise ValueError(f"invalid {field}: {value!r}")


def load_contract_keep():
    try:
        document, _ = load_input(CONTRACT_KEEP_PATH)
        if not isinstance(document, dict) or document.get("schema") != "leaf.ci.contract-keep.v1":
            raise ValueError("invalid schema")
        records = document.get("suites")
        if not isinstance(records, list) or not records:
            raise ValueError("suites must be a nonempty array")
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(f"suites[{index}] must be an object")
            fields = {"id", "reason", "added", "evidence"} ^ record.keys()
            if fields:
                raise ValueError(f"suites[{index}] invalid fields: {sorted(fields)}")
        ids = [record["id"] for record in records]
        suite_ids(ids, "suites.id")
        if ids != sorted(ids):
            raise ValueError("suites must be ordered by id")
        reasons = {}
        for record in records:
            sid, reason, evidence = record["id"], record["reason"], record["evidence"]
            validate_date(record["added"], f"{sid} added")
            if reason == "contract-keep":
                if evidence is not None:
                    raise ValueError(f"{sid} evidence must be null for contract-keep")
            elif reason == "main-broke":
                if not isinstance(evidence, dict) or not evidence:
                    raise ValueError(f"{sid} evidence must be a nonempty object for main-broke")
                if "date" in evidence:
                    validate_date(evidence["date"], f"{sid} evidence.date")
            else:
                raise ValueError(f"{sid} invalid reason: {reason!r}")
            reasons[sid] = reason
        return reasons
    except (OSError, ValueError) as exc:
        raise ValueError(f"{CONTRACT_KEEP_PATH}: {exc}") from exc


def require_contract_ids(reasons, ids, source):
    missing = reasons.keys() - ids
    if missing:
        raise ValueError(f"{CONTRACT_KEEP_PATH}: IDs absent from {source}: {', '.join(sorted(missing))}")


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


def build_document(proposal, failures, sentinels, catalog, input_sha256s, runner=None, *, contract_reasons=None):
    """Construct the tier from validated inputs without modifying the catalog or proposal."""
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
    if contract_reasons is None:
        contract_reasons = load_contract_keep()
    require_contract_ids(contract_reasons, ids, "live catalog")
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
    for sid, reason in contract_reasons.items():
        include(sid, reason)
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


def live_catalog():
    proc = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/run-all-gates.py"), "--list"],
                          cwd=ROOT, capture_output=True, text=True, check=True, timeout=60)
    return json.loads(proc.stdout, object_pairs_hook=unique_object)


def generate(proposal_path, failures_path, sentinels_path, catalog=None, *, enabled=False):
    contract_reasons = load_contract_keep()
    documents, digests = [], {}
    for name, path in (("proposal", proposal_path), ("fail30", failures_path), ("sentinels", sentinels_path)):
        doc, digest = load_input(path)
        documents.append(doc)
        digests[name] = digest
    if catalog is None:
        catalog = live_catalog()
    document = build_document(*documents, catalog, digests, contract_reasons=contract_reasons)
    document["enabled"] = enabled
    return document


def merge_contract_keep(tier):
    contract_reasons = load_contract_keep()
    if not isinstance(tier, dict) or tier.get("schema") != SCHEMA or type(tier.get("enabled")) is not bool:
        raise ValueError("invalid tier schema or enabled flag")
    snapshot = suite_ids(tier.get("catalog_suite_ids"), "catalog_suite_ids")
    members = suite_ids(tier.get("suite_ids"), "suite_ids")
    if not members or not members <= snapshot:
        raise ValueError("suite_ids must be a nonempty subset of catalog_suite_ids")
    reasons = tier.get("reasons")
    if not isinstance(reasons, dict) or reasons.keys() != members:
        raise ValueError("reasons keys must match suite_ids")
    for sid, values in reasons.items():
        if (not isinstance(values, list) or not values
                or any(not isinstance(value, str) or not value.strip() for value in values)
                or len(set(values)) != len(values)):
            raise ValueError(f"invalid reasons for {sid}")
    dependencies = tier.get("prerequisites", {})
    if not isinstance(dependencies, dict) or not dependencies.keys() <= snapshot:
        raise ValueError("invalid prerequisites")
    for sid, deps in dependencies.items():
        if not suite_ids(deps, f"{sid} prerequisites") <= snapshot:
            raise ValueError(f"{sid} prerequisites outside catalog_suite_ids")
    require_contract_ids(contract_reasons, validate_catalog(live_catalog()), "live catalog")
    require_contract_ids(contract_reasons, snapshot, "catalog_suite_ids")

    tier["suite_ids"] = sorted(members.union(contract_reasons))
    for sid, reason in contract_reasons.items():
        values = reasons.setdefault(sid, [])
        if reason not in values:
            values.append(reason)
    for values in reasons.values():
        values.sort()


def replace_tier(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, default=ROOT / "pr-tier-proposal.json")
    parser.add_argument("--fail30", type=Path, default=ROOT / "fail30.json")
    parser.add_argument("--sentinels", type=Path, default=ROOT / "sentinels-strict.json")
    parser.add_argument("--output", type=Path, default=ROOT / "scripts/ci/pr-tier.json")
    parser.add_argument("--sync-contract-keep", action="store_true",
                        help="merge contract memberships and reasons into the existing output tier")
    parser.add_argument("--enabled", action="store_true", help="explicitly activate the generated PR tier")
    parser.add_argument("--stdout", action="store_true", help="print the document without writing files")
    parser.add_argument("--active", type=Path, metavar="POLICY",
                        help="print 1 for a PR or baselined main push with an enabled tier; otherwise 0")
    args = parser.parse_args(argv)
    if args.active is not None:
        # The device worker sets this for a server-issued full run, which must
        # run the whole catalog.
        if os.environ.get("LEAF_CI_FULL") == "1":
            print(0)
            return 0
        is_pr = os.environ.get("CODEBUILD_WEBHOOK_EVENT") in (
            "PULL_REQUEST_CREATED", "PULL_REQUEST_UPDATED", "PULL_REQUEST_REOPENED")
        is_main = (os.environ.get("CODEBUILD_WEBHOOK_EVENT") == "PUSH"
                   and os.environ.get("CODEBUILD_WEBHOOK_HEAD_REF") == "refs/heads/main")
        active = False
        if is_pr or is_main:
            runner = load_runner()
            try:
                if is_main:
                    runner.pr_tier_base()
                active = runner.load_pr_tier(args.active, quiet=True) is not None
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        print(int(active))
        return 0
    try:
        original_digest = None
        if args.sync_contract_keep:
            document, original_digest = load_input(args.output)
            merge_contract_keep(document)
        else:
            document = generate(args.proposal, args.fail30, args.sentinels, enabled=args.enabled)
        serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
        if args.stdout:
            print(serialized, end="")
        elif args.sync_contract_keep:
            content = serialized.encode("utf-8")
            if hashlib.sha256(content).hexdigest() != original_digest:
                replace_tier(args.output, content)
        else:
            args.output.write_text(serialized, encoding="utf-8")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("PR tier generation refused: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Compatibility for importers. The activation probe only needs the tier file.
CONTRACT_KEEP = tuple(load_contract_keep())
