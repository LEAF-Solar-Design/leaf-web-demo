"""Plan records and the markdown block consumed by the pair driver."""

import json
import os
from pathlib import Path

try:
    from . import record, scope
    from .manifest import CONCERNS, ManifestError, load_manifest, manifest_digest
    from .rules import load_families
except ImportError:
    import record
    import scope
    from manifest import CONCERNS, ManifestError, load_manifest, manifest_digest
    from rules import load_families


def enabled_manifest(workdir):
    # The authoring validator is strict; optional plan/check integration fails open.
    gate = Path(os.environ.get("CHANGE_IMPACT_GATE_FILE", "C:/tmp/gates/CHANGE_IMPACT_OFF"))
    if os.environ.get("CHANGE_IMPACT_DISABLE") == "1" or gate.exists():
        print("impact: disabled by kill switch")
        return None
    path = Path(workdir) / "ASPECTS.yaml"
    if not path.exists():
        print(f"impact: no ASPECTS.yaml in {workdir}, skipped")
        return None
    try:
        return load_manifest(path)
    except (ManifestError, OSError, ValueError, RecursionError) as exc:
        print("impact: manifest invalid: " + " ".join(str(exc).splitlines()) + ", skipped")
        return None


def rows_for_scope(result, manifest, digest, added_reason=None):
    rows = [record.new_row(item.concern, item.subject, digest, family=item.family,
                           reason=added_reason) for item in result.required]
    for name in CONCERNS:
        entry = manifest["concerns"][name]
        if entry["monitor_obligation"]:
            evidence = next(iter(entry["evidence"]), None)
            rows.append(record.new_row(name, "monitor", digest,
                                       outcome=entry["outcome_default"] if evidence else "unresolved",
                                       evidence=evidence, reason="monitor obligation, assessed per target"))
    for path, missing in result.companions:
        # One suggestion per concern avoids duplicate row ids for several companions.
        groups = {}
        for entry in missing:
            concern = "tests" if any(part in ("test", "tests", "__tests__") for part in entry.split("/")[:-1]) else "contracts"
            groups.setdefault(concern, []).append(entry)
        for concern, entries in groups.items():
            rows.append(record.new_row(concern, f"companion:{path}", digest,
                                       reason="moves with " + ", ".join(entries)))
    for name in CONCERNS:
        if not any(row["concern"] == name for row in rows):
            rows.append(record.new_row(name, "-", digest, required=False,
                                       outcome="not-applicable", reason=f"no trigger at {digest[:12]}"))
    unique = {}
    for row in rows:
        unique.setdefault(row["id"], row)
    return record.sort_rows(unique.values())


def render(data, families, result=None):
    evidence = {family.id: family.evidence for family in families}
    touched = data["touched"]
    lines = ["## Impact",
             f"manifest {data['repository']} digest {data['manifest_digest'][:12]} checker {data['checker_version']}",
             "touched: " + " ".join(f"{key}=[{', '.join(touched[key])}]" for key in
                                    ("components", "contracts", "targets", "companions")),
             "required rows (changed and unchanged-compatible need an evidence reference; a prose reason never satisfies a contract row):",
             "| row | concern | subject | outcome | evidence expectation |",
             "| --- | --- | --- | --- | --- |"]
    for row in record.sort_rows(data["rows"]):
        if row["required"]:
            expectation = evidence.get(row["family"], "evidence reference")
            lines.append(f"| {row['id']} | {row['concern']} | {row['subject']} | {row['outcome']} | {expectation} |")
    if result is not None:
        for path, missing in result.companions:
            if missing:
                lines.append(f"companions: {path} moves with {', '.join(missing)} (not in the owned set)")
    for item in data["carried"]:
        lines.append(f"carried forward (unresolved in earlier records): {item['row']} from {item['from_change_id']}")
    return "\n".join(lines)


def run(args, version):
    manifest = enabled_manifest(args.workdir)
    if manifest is None:
        return 0
    families = load_families()
    paths = sorted(set(args.owned))
    if len(paths) > 5000:
        raise ManifestError("owned paths exceed 5,000 limit")
    result = scope.impact_scope(paths, manifest, families)
    scope.add_targets(result, args.target or [], manifest, families)
    digest = manifest_digest(manifest)
    data = record.new_record(args.change_id, manifest, digest, version)
    data["touched"] = scope.touched(result, paths)
    data["rows"] = rows_for_scope(result, manifest, digest)
    data["carried"] = record.carried_rows(data["repository"], args.change_id)
    record.save_record(args.record, data)
    print(json.dumps(data, sort_keys=True, ensure_ascii=True) if args.json else render(data, families, result))
    return 0
