"""CLI for the change-impact manifest half; main(argv=None) returns an exit code.

Library callers should import manifest, gitio, rules, and index directly. The CLI
keeps index failures nonfatal for DRIFT and converts operational errors to lines.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse
import json
import re

import yaml

if __package__:
    from . import gitio, index, manifest, rules, scope
else:
    import gitio
    import index
    import manifest
    import rules
    import scope

IMPACT_VERSION = "0.1.0"


def impact_scope(paths, loaded, families):
    return scope.impact_scope(paths, loaded, families)


def _plan(args):
    if __package__:
        from .plan import run
    else:
        from plan import run
    return run(args, IMPACT_VERSION)


def _check(args):
    if __package__:
        from .check import run
    else:
        from check import run
    return run(args, IMPACT_VERSION)


def _disposition(args):
    if __package__:
        from .record import dispose
    else:
        from record import dispose
    return dispose(args)


def _selftest(args):
    if __package__:
        from .selftest import run
    else:
        from selftest import run
    return run(args, IMPACT_VERSION)


def _proven(args):
    if __package__:
        from .proven import run
    else:
        from proven import run
    return run(args, IMPACT_VERSION)


def _json(data):
    print(json.dumps(data, sort_keys=True, ensure_ascii=True))


def _drift(loaded):
    # DRIFT is informational; an unreadable index or checkout becomes unknown.
    rows = []
    for contract in loaded["contracts"]:
        for endpoint in [contract["producer"], *contract["consumers"]]:
            repository, revision = endpoint["repo"], endpoint["revision"]
            if repository == "self" or re.fullmatch(manifest.SHA_PATTERN, revision) is None:
                continue
            head = None
            try:
                entry = index.lookup(repository)
                if entry and entry.get("path"):
                    head = gitio.default_head(entry["path"])
            except Exception:
                pass
            rows.append({"contract": contract["id"], "repository": repository, "pinned": revision, "head": head or "unknown", "status": "unknown" if head is None else "same" if head == revision else "moved"})
    return sorted(rows, key=lambda row: (row["contract"], row["repository"], row["pinned"], row["head"]))


def _validate(args):
    loaded = manifest.load_manifest(args.manifest)
    repo = args.repo or str(Path(args.manifest).resolve().parent)
    rev = gitio.rev_parse(repo, args.rev)
    coverage = manifest.coverage(loaded, gitio.ls_tree(repo, rev), args.manifest)
    result = {"ok": coverage.uncovered == 0, "rev": rev, "tracked": coverage.tracked, "mapped": coverage.mapped, "listed": coverage.listed, "uncovered": coverage.uncovered, "uncovered_paths": coverage.uncovered_paths[:200], "contracts": manifest.contract_counts(loaded), "drift": _drift(loaded), "digest": manifest.manifest_digest(loaded)}
    if args.json:
        _json(result)
    else:
        print(f"coverage: rev={rev[:12]} tracked={coverage.tracked} mapped={coverage.mapped} listed={coverage.listed} uncovered={coverage.uncovered}")
        for path in result["uncovered_paths"]:
            print(f"uncovered: {path}")
        if coverage.uncovered > 200:
            print(f"uncovered: ... {coverage.uncovered - 200} more")
        counts = result["contracts"]
        print(f"contracts: rows={counts['rows']} pinned={counts['pinned']} unresolved={counts['unresolved']} head={counts['head']}")
        for row in result["drift"]:
            print(f"DRIFT {row['contract']} {row['repository']} pinned={row['pinned'][:12]} head={row['head'][:12]} status={row['status']}")
        print(f"manifest_digest: {result['digest']}")
        print("validate: OK" if result["ok"] else "validate: FAIL")
    return 0 if result["ok"] else 1


def _identifier(value):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", value.lower())).strip("-")[:64].rstrip("-") or "project"


def _init(args):
    repo = Path(args.repo).resolve()
    destination = Path(args.out) if args.out else repo / "ASPECTS.yaml"
    if destination.exists() and not args.force:
        raise manifest.ManifestError(f"{destination}: already exists; use --force to overwrite")
    rev = gitio.rev_parse(repo, args.rev)
    paths = gitio.ls_tree(repo, rev)
    directories = sorted({path.split("/", 1)[0] for path in paths if "/" in path})
    repository = "local"
    remote = gitio.remote_url(repo) or ""
    match = re.search(r"(?:^|[/:@])github\.com[:/]([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", remote)
    if match:
        candidate = f"{match[1]}/{match[2]}"
        if re.fullmatch(manifest.REPOSITORY_PATTERN, candidate):
            repository = candidate
    components = []
    used = set()
    for directory in directories:
        identifier = base = _identifier(directory)
        suffix = 2
        while identifier in used:
            ending = f"-{suffix}"
            identifier = base[:64 - len(ending)] + ending
            suffix += 1
        used.add(identifier)
        components.append({"id": identifier, "paths": [f"{directory}/**"], "facets": []})
    if not components:
        # A files-only or empty tree still needs one component under schema v1.
        components.append({"id": "root", "paths": ["**"], "facets": []})
    data = {"schema_version": 1, "project_id": _identifier(repo.name), "repository": repository, "authored_at_revision": rev, "components": sorted(components, key=lambda item: item["id"]), "unmapped": sorted(path for path in paths if "/" not in path and path != destination.name), "targets": [], "contracts": [], "companions": [], "concerns": {name: {"evidence": []} for name in manifest.CONCERNS}}
    loaded = manifest.validate_manifest(data)
    if manifest.coverage(loaded, paths, destination).uncovered:
        raise manifest.ManifestError("init: generated manifest does not cover the tree")
    text = f"# Generated by impact {IMPACT_VERSION}\n# Revision: {rev}\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if len(text.encode("utf-8")) > manifest.MAX_BYTES:
        raise manifest.ManifestError("init: generated manifest exceeds 1 MiB limit")
    with destination.open("w" if args.force else "x", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    print(f"init: wrote {destination} components={len(components)} unmapped={len(data['unmapped'])}")
    return 0


def _sync(args):
    data = index.sync()
    result = {"repos": len(data["repos"]), "hosts": len(data["hosts"]), "registry": len(data["registry"]), "index": str(index.index_path())}
    if args.json:
        _json(result)
    else:
        print(f"sync: repos={result['repos']} hosts={result['hosts']} registry={result['registry']} index={result['index']}")
    return 0


def _doctor(args):
    result = index.doctor()
    if args.json:
        _json(result)
    else:
        if result["missing_index"]:
            print("doctor: index missing, run sync")
        for repository in result["unreachable"]:
            print(f"doctor: manifest unreachable for {repository}")
        for repository in result["no_manifest"]:
            print(f"doctor: no manifest for {repository}")
        if result["ok"]:
            print("doctor: OK")
    return 0 if result["ok"] else 1


def _where(args):
    entry = index.lookup(args.repo)
    if entry is None:
        print(f"where: absent {args.repo}")
        return 3
    print(f"path={entry['path']} manifest={entry['manifest']} digest={entry['digest']}")
    return 0


def parser():
    result = argparse.ArgumentParser(prog="impact")
    verbs = result.add_subparsers(dest="verb", required=True)
    command = verbs.add_parser("init")
    command.add_argument("--repo", required=True)
    command.add_argument("--rev", default="HEAD")
    command.add_argument("--out")
    command.add_argument("--force", action="store_true")
    command.set_defaults(handler=_init)
    command = verbs.add_parser("validate")
    command.add_argument("--manifest", required=True)
    command.add_argument("--repo")
    command.add_argument("--rev", default="HEAD")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=_validate)
    for verb, handler in (("sync", _sync), ("doctor", _doctor)):
        command = verbs.add_parser(verb)
        command.add_argument("--json", action="store_true")
        command.set_defaults(handler=handler)
    command = verbs.add_parser("where")
    command.add_argument("--repo", required=True)
    command.set_defaults(handler=_where)
    command = verbs.add_parser("version")
    command.set_defaults(handler=lambda args: print(f"impact {IMPACT_VERSION}") or 0)
    command = verbs.add_parser("selftest")
    command.add_argument("--fixtures", action="store_true", required=True)
    command.add_argument("--json", action="store_true")
    command.add_argument("--only")
    command.set_defaults(handler=_selftest)
    command = verbs.add_parser("proven")
    command.add_argument("--out", required=True)
    command.add_argument("--studio-workdir", default="C:/tmp/ci-s1-lwd")
    command.add_argument("--pair-runs")
    command.add_argument("--merge-group-build")
    command.add_argument("--since")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=_proven)
    command = verbs.add_parser("plan")
    command.add_argument("--workdir", required=True)
    command.add_argument("--change-id", required=True)
    command.add_argument("--owned", action="append", default=[])
    command.add_argument("--target", action="append", default=[])
    command.add_argument("--record", required=True)
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=_plan)
    command = verbs.add_parser("check")
    command.add_argument("--workdir", required=True)
    command.add_argument("--change-id", required=True)
    command.add_argument("--base", required=True)
    command.add_argument("--head", required=True)
    command.add_argument("--record", required=True)
    command.add_argument("--receipt", required=True)
    command.add_argument("--transaction", choices=("deploy", "config-write", "restart"))
    command.add_argument("--target")
    command.add_argument("--strict", action="store_true")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=_check)
    for verb in ("dismiss", "resolve", "defer"):
        command = verbs.add_parser(verb)
        command.add_argument("--record", required=True)
        command.add_argument("--row", required=True)
        if verb == "dismiss":
            command.add_argument("--reason", required=True)
        elif verb == "resolve":
            command.add_argument("--evidence", required=True)
            command.add_argument("--outcome", choices=("changed", "unchanged-compatible"), default="changed")
        else:
            command.add_argument("--task", required=True)
        command.set_defaults(handler=_disposition)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except Exception as exc:
        # Host verbs never expose a traceback; all errors become one stderr line.
        location = f"{args.manifest}: " if args.verb == "validate" else ""
        prefix = "impact: " if args.verb in ("plan", "check", "dismiss", "resolve", "defer") else "error: "
        print(prefix + location + " ".join(str(exc).splitlines()), file=sys.stderr)
        return 1 if args.verb in ("init", "validate", "dismiss", "resolve", "defer") else 4


if __name__ == "__main__":
    raise SystemExit(main())
