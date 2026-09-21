"""Assess actual changes, preserve deleted obligations, and emit stable receipts."""

from dataclasses import asdict
from pathlib import Path
import platform
import json

import yaml

try:
    from . import gitio, index, plan, record, scope
    from .extractors import contract_check, git_text
    from .manifest import MAX_BYTES, ManifestError, manifest_digest, validate_manifest
    from .rules import load_families
except ImportError:
    import gitio
    import index
    import plan
    import record
    import scope
    from extractors import contract_check, git_text
    from manifest import MAX_BYTES, ManifestError, manifest_digest, validate_manifest
    from rules import load_families


def changed_paths(workdir, base, head, worktree=False):
    # NUL delimiters preserve spaces and rename pairs become deletion plus addition.
    def listing(*args):
        try:
            return git_text(workdir, *args, limit=8 * MAX_BYTES)
        except ValueError as exc:
            if str(exc) == "git output exceeds byte limit":
                raise ValueError("diff too large") from exc
            raise
    output = listing("diff", "--name-status", "-z", "--find-renames", base, head, "--")
    tokens = output.split("\0") if output else []
    paths, deleted = set(), set()
    cursor = 0
    while cursor < len(tokens) and tokens[cursor]:
        status = tokens[cursor]
        cursor += 1
        count = 2 if status.startswith(("R", "C")) else 1
        names = tokens[cursor:cursor + count]
        if len(names) != count or not all(names):
            raise ValueError("malformed git diff")
        cursor += count
        paths.update(names)
        if status.startswith(("D", "R")):
            deleted.add(names[0])
    if worktree:
        output = listing("status", "--porcelain", "-z", "--untracked-files=all")
        tokens = output.split("\0") if output else []
        cursor = 0
        while cursor < len(tokens) and tokens[cursor]:
            entry = tokens[cursor]
            cursor += 1
            if len(entry) < 4 or entry[2] != " ":
                raise ValueError("malformed git status")
            status, path = entry[:2], entry[3:]
            paths.add(path)
            if "D" in status:
                deleted.add(path)
            if "R" in status or "C" in status:
                if cursor >= len(tokens) or not tokens[cursor]:
                    raise ValueError("malformed git rename")
                source = tokens[cursor]
                cursor += 1
                paths.add(source)
                if "R" in status:
                    deleted.add(source)
    if len(paths) > 5000:
        raise ValueError("diff too large")
    return sorted(paths), sorted(deleted)


def revision_manifest(workdir, sha):
    if "ASPECTS.yaml" not in gitio.ls_tree(workdir, sha):
        return None
    text = git_text(workdir, "show", f"{sha}:ASPECTS.yaml", limit=MAX_BYTES)
    return validate_manifest(yaml.safe_load(text))


def _merge_touched(head, base):
    return {key: sorted(set(head[key]) | set(base[key])) for key in head}


def reconcile(data, expected):
    # A new manifest digest creates fresh rows; older assessments stay informational.
    existing = {row["id"]: row for row in data["rows"]}
    for row in existing.values():
        row["required"] = False
    for desired in expected:
        if desired["id"] in existing:
            row = existing[desired["id"]]
            row["required"] = desired["required"]
            row["family"] = desired["family"]
        else:
            existing[desired["id"]] = desired
    data["rows"] = record.sort_rows(existing.values())


def _contract_for(row, manifests):
    for manifest in manifests:
        if manifest is None:
            continue
        for contract in manifest["contracts"]:
            for consumer in contract["consumers"]:
                repo = manifest["repository"] if consumer["repo"] == "self" else consumer["repo"]
                if row["subject"] == f"{contract['id']}/{repo}":
                    return contract, consumer, repo
    return None


def apply_evidence(rows, manifests, workdir, head):
    checks = {}
    for row in rows:
        if not row["required"]:
            continue
        if row["outcome"] in ("changed", "unchanged-compatible") and row["evidence"] is None:
            row.update(outcome="unresolved", reason="evidence required")
        if row["outcome"] == "deferred" and row["task"] is None:
            row.update(outcome="unresolved", reason="deferred needs a task id")
        if row["outcome"] == "not-applicable" and row["reason"] is None:
            row.update(outcome="unresolved", reason="not-applicable needs a reason")
        if row["subject"] == "monitor" and row["evidence"] is None and row["outcome"] not in ("deferred", "not-applicable"):
            row.update(outcome="unresolved", reason="evidence required")
        if row["family"] != "contracts" or row["outcome"] in ("deferred", "not-applicable"):
            continue
        found = _contract_for(row, manifests)
        if found is None:
            row.update(outcome="unresolved", reason="producer unreachable: contract mapping missing")
            continue
        contract, consumer, repo = found
        key = json.dumps([contract, consumer], sort_keys=True)
        if key not in checks:
            checks[key] = contract_check(contract, consumer, workdir, head, index.lookup)
        status, detail, producer_sha, consumer_sha = checks[key]
        # Strip prior generated segments so repeated checks cannot grow evidence.
        original = row["evidence"]
        authored = ";".join(part for part in (original or "").split(";") if not part.startswith("contract-check:")) or None
        keep_changed = row["outcome"] == "changed" and original is not None
        if status == "unreachable":
            row.update(outcome="unresolved", reason=f"producer unreachable: {detail}"[:1000])
            row["evidence"] = authored
            continue
        evidence = f"contract-check:{contract['id']}/{repo}:{status}:{producer_sha[:12]}:{consumer_sha[:12]}"
        if keep_changed:
            combined = authored + ";" + evidence if authored else evidence
            if len(combined) > 512:
                raise ManifestError("contract evidence exceeds 512 characters")
            row["evidence"] = combined
        else:
            row["evidence"] = evidence
            row["outcome"] = "unchanged-compatible" if status == "match" else "unresolved"
        if status == "mismatch":
            row["reason"] = detail[:900] or "contract mismatch"
        elif detail:
            row["reason"] = detail[:900]


def run(args, version):
    current = plan.enabled_manifest(args.workdir)
    if current is None:
        return 0
    if bool(args.transaction) != bool(args.target):
        raise ManifestError("--transaction and --target must be supplied together")
    base = gitio.rev_parse(args.workdir, args.base)
    worktree = args.head == "worktree"
    head = gitio.rev_parse(args.workdir, "HEAD" if worktree else args.head)
    paths, deleted = changed_paths(args.workdir, base, head, worktree)
    if worktree:
        manifest = current
    else:
        try:
            manifest = revision_manifest(args.workdir, head)
        except (ValueError, yaml.YAMLError, RecursionError) as exc:
            print("impact: manifest invalid: " + " ".join(str(exc).splitlines()) + ", skipped")
            return 0
        if manifest is None:
            print(f"impact: no ASPECTS.yaml in {args.workdir}, skipped")
            return 0
    previous = revision_manifest(args.workdir, base)
    families = load_families()
    result = scope.impact_scope(paths, manifest, families)
    scope.add_targets(result, [args.target] if args.target else [], manifest, families)
    digest = manifest_digest(manifest)
    expected = plan.rows_for_scope(result, manifest, digest, "added at check")
    touched = scope.touched(result, paths)
    if previous is not None and deleted:
        old_scope = scope.impact_scope(deleted, previous, families)
        old_rows = plan.rows_for_scope(old_scope, previous, digest, "added at check")
        required = {row["id"]: row for row in expected}
        for row in old_rows:
            if row["required"]:
                required.setdefault(row["id"], row)
        expected = record.sort_rows(required.values())
        touched = _merge_touched(touched, scope.touched(old_scope, deleted))
    if Path(args.record).exists():
        data = record.load_record(args.record)
        if (data["change_id"] != args.change_id or data["repository"] != manifest["repository"]
                or data["project_id"] != manifest["project_id"]):
            raise ManifestError("record identity does not match change/repository/project")
    else:
        data = record.new_record(args.change_id, manifest, digest, version)
    reconcile(data, expected)
    receipt_head = f"worktree:{head}" if worktree else head
    apply_evidence(data["rows"], [manifest, previous], args.workdir, receipt_head)
    data.update(base=base, head="worktree" if worktree else head, touched=touched,
                manifest_digest=digest, checker_version=version)
    required = [row for row in data["rows"] if row["required"]]
    unresolved = sum(row["outcome"] == "unresolved" for row in required)
    summary = {"required": len(required), "complete": len(required) - unresolved,
               "unresolved": unresolved, "deferred": sum(row["outcome"] == "deferred" for row in required)}
    verdict = "INCOMPLETE" if unresolved else "COMPLETE"
    receipt = record.Receipt(
        change_id=args.change_id, repository=manifest["repository"], project_id=manifest["project_id"],
        base=base, head=receipt_head, manifest_digest=digest,
        base_manifest_digest=manifest_digest(previous) if previous is not None else None,
        checker_version=version, host=platform.node(), changed_paths=paths, touched=touched,
        rows=[{key: row[key] for key in ("id", "concern", "subject", "family", "required", "outcome", "evidence", "task")}
              for row in data["rows"]], summary=summary, verdict=verdict,
        transaction={"kind": args.transaction, "target": args.target} if args.transaction else None,
    )
    record.validate_record(data)
    if Path(args.record).resolve() in (Path(args.receipt).resolve(), Path(str(args.receipt) + ".meta.json").resolve()):
        raise ManifestError("record and receipt paths must differ")
    record.save_record(args.record, data)
    record.write_receipt(args.receipt, receipt)
    if args.json:
        print(json.dumps(asdict(receipt), sort_keys=True, ensure_ascii=True))
    else:
        print(f"assessment executed base={base} head={receipt_head} manifest={digest[:12]} checker={version}")
        print(f"IMPACT: INCOMPLETE unresolved={unresolved}" if unresolved else "IMPACT: COMPLETE")
    return 1 if args.strict and unresolved else 0
