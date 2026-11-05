"""Build a disabled candidate map; capture observations never grant eligibility."""

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
import tempfile

# The isolated CLI imports only the trusted helpers beside this script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_capture_shards as merger
import select_tests


Refusal = merger.Refusal
LINEAGE = ("source_sha", "source_tree", "runner", "catalog_sha256", "image_digest",
           "python_version", "external_root_digests", "capture_sha", "capture_epoch",
           "toolchain_fingerprint", "repo")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def object_at(documents, name):
    value = documents.get(name)
    if not isinstance(value, dict):
        raise Refusal("malformed")
    return value


def epoch_roots(epoch):
    roots = epoch.get("external_root_digests")
    if not isinstance(roots, dict) or epoch.get("external_root_conflicts"):
        raise Refusal("malformed")
    for name, identity in roots.items():
        if not merger.string(name) or not isinstance(identity, dict) or "sha256" not in identity:
            raise Refusal("malformed")
        fingerprint = identity["sha256"]
        # The actual inventory schema also names unhashed runtime roots and
        # incomplete inventories. Retain their status; never certify them here.
        unhashed = fingerprint is None and (identity.get("class") == "container-runtime" or
                                            identity.get("complete") is False)
        if not unhashed and (not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
            raise Refusal("malformed")
        if "complete" in identity and type(identity["complete"]) is not bool:
            raise Refusal("malformed")
    return roots


def compare_lineage(first, second):
    for field in LINEAGE:
        if first[field] != second[field]:
            merger.mismatch(field)


def read_merged(directory):
    """Recheck the retained producer manifests, not just the union's claims."""
    root, members = merger.inventory(Path(directory))
    documents = {name: merger.load(path) for name, path in sorted(members.items())
                 if name.endswith(".json")}
    for document in documents.values():
        merger.declared_paths(document)
    for name, path in sorted(members.items()):
        if name.endswith(".jsonl"):
            with path.open("rb") as stream:
                for line in stream:
                    if line.strip() and not isinstance(merger.decode(line), dict):
                        raise Refusal("malformed")
    manifest = object_at(documents, "merged-manifest.json")
    catalog = object_at(documents, "catalog.json")
    epoch = object_at(documents, "capture/capture-epoch.json")
    if (manifest.get("schema") != "leaf.ci.capture-merge.v1" or
            epoch.get("schema") != "leaf.ci.capture-epoch.v1" or
            any(not merger.string(manifest.get(field)) for field in merger.LINEAGE)):
        raise Refusal("malformed")
    lineage = {field: manifest[field] for field in merger.LINEAGE}
    lineage["external_root_digests"] = epoch_roots(epoch)
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", lineage["runner"]):
        raise Refusal("malformed")
    if select_tests.normalized_epoch(epoch) != lineage["capture_epoch"]:
        merger.mismatch("capture_epoch")
    entries, fingerprint = select_tests.catalog_info(catalog)
    if fingerprint != lineage["catalog_sha256"]:
        merger.mismatch("catalog_sha256")
    records = manifest.get("shards")
    if not isinstance(records, list) or not records:
        raise Refusal("malformed")
    if any(not isinstance(record, dict) or not merger.string(record.get("run_id")) for record in records):
        raise Refusal("malformed")
    record_ids = [record["run_id"] for record in records]
    if len(record_ids) != len(set(record_ids)):
        raise Refusal("duplicate_run")
    readsets = {}
    for name, document in documents.items():
        if name.startswith("readsets/") and not name.endswith(".misses.json"):
            if (not isinstance(document, dict) or document.get("schema") != "leaf.ci.readset.v1" or
                    not merger.string(document.get("suite_id")) or
                    not merger.string(document.get("run_id"))):
                raise Refusal("malformed")
            readsets[name] = document
    runs, prefixes, accepted, proven = [], set(), set(), set()
    for record in records:
        ref = merger.member_name(record.get("manifest"))
        if not re.fullmatch(r"shards/[0-9]+/full-run\.json", ref):
            raise Refusal("path_escape")
        prefix = ref.rsplit("/", 1)[0] + "/"
        if prefix in prefixes:
            raise Refusal("malformed")
        prefixes.add(prefix)
        original = object_at(documents, ref)
        if (original.get("schema") != "leaf.ci.full-run.v1" or
                original.get("run_id") != record["run_id"] or
                sha256(members[ref].read_bytes()) != record.get("manifest_sha256")):
            raise Refusal("malformed")
        original_catalog = object_at(documents, prefix + "catalog.json")
        if original_catalog != catalog:
            merger.mismatch("catalog_sha256")
        original_epoch = original.get("capture_epoch_manifest")
        if not isinstance(original_epoch, dict):
            raise Refusal("malformed")
        interpreter = original_epoch.get("interpreter_identity")
        if not isinstance(interpreter, dict):
            raise Refusal("malformed")
        original_lineage = {field: original.get(field) for field in merger.LINEAGE}
        original_lineage.update(runner=original_catalog.get("runner_blob_sha", original_catalog.get("runner_sha256")),
                                image_digest=original_epoch.get("image_manifest_digest"),
                                python_version=interpreter.get("version"))
        if any(not merger.string(original_lineage[field]) for field in merger.LINEAGE):
            raise Refusal("malformed")
        original_lineage["external_root_digests"] = epoch_roots(original_epoch)
        compare_lineage(lineage, original_lineage)
        own_readsets = {name: doc for name, doc in readsets.items() if doc["run_id"] == record["run_id"]}
        own_accepted = {doc["suite_id"] for doc in own_readsets.values()}
        declared = merger.suite_list(record.get("accepted_suite_ids"))
        if not own_accepted or declared - own_accepted:
            raise Refusal("missing_readsets")
        if own_accepted != declared or accepted & declared:
            raise Refusal("malformed")
        if "accepted_suite_ids" in original and merger.suite_list(original["accepted_suite_ids"]) != declared:
            raise Refusal("malformed")
        decision = documents.get(prefix + "decision.json")
        if isinstance(decision, dict) and "executed_suite_ids" in decision:
            if merger.suite_list(decision["executed_suite_ids"]) != declared:
                raise Refusal("malformed")
        # Present the merger's validator with the retained metadata and shared
        # readset/report namespace. No reconstruction or temporary input writes.
        own_documents = {name[len(prefix):]: doc for name, doc in documents.items() if name.startswith(prefix)}
        own_documents.update({name: doc for name, doc in documents.items() if not name.startswith("shards/")})
        shard = {"manifest": original, "catalog": original_catalog, "epoch": original_epoch,
                 "lineage": original_lineage, "readsets": own_readsets, "accepted": declared,
                 "members": members, "documents": own_documents}
        # S1 permits a shard with only the manifest's embedded epoch. When a
        # separate copy was retained, recheck that original copy as well.
        if prefix + "capture/capture-epoch.json" in documents:
            own_documents["capture/capture-epoch.json"] = object_at(documents, prefix + "capture/capture-epoch.json")
        merger.validate_shard(shard)
        accepted.update(declared)
        proven.update(shard["proven"])
        runs.append({"run_id": record["run_id"], "manifest_sha256": record["manifest_sha256"],
                     "manifest": original})
    run_ids = [run["run_id"] for run in runs]
    if any(doc["run_id"] not in run_ids for doc in readsets.values()):
        raise Refusal("malformed")
    if (merger.suite_list(manifest.get("run_ids")) != set(run_ids) or
            merger.suite_list(manifest.get("suite_ids")) != set(entries) or
            merger.suite_list(manifest.get("accepted_suite_ids")) != accepted or
            merger.suite_list(manifest.get("uncovered")) != set(entries) - accepted or
            merger.suite_list(manifest.get("unproven")) != accepted - proven or
            type(manifest.get("complete")) is not bool or
            manifest["complete"] != (not (set(entries) - proven))):
        raise Refusal("malformed")
    return {"root": root, "lineage": lineage, "catalog": catalog, "entries": entries,
            "accepted": accepted, "proven": proven, "readsets": readsets, "documents": documents,
            "runs": runs, "sha256": sha256(members["merged-manifest.json"].read_bytes())}


def observed_paths(capture, sid):
    paths, reasons = set(), set()
    if sid not in capture["accepted"]:
        return paths, {"not_accepted"}
    if sid not in capture["proven"]:
        return paths, {"unproven"}
    for doc in capture["readsets"].values():
        if doc["suite_id"] != sid:
            continue
        certificate = capture["documents"].get(doc.get("process_tree_ref"))
        # Include the process union as well: a narrowed or empty suite delta
        # cannot hide shared inputs or outside reads from an informational row.
        sources = [doc] + ([certificate] if isinstance(certificate, dict) else [])
        own_paths = set()
        for source in sources:
            if source.get("external_inputs") or source.get("external_dependency_classes"):
                reasons.add("outside_read")
            reads = source.get("reads", [])
            if not isinstance(reads, list):
                reasons.add("unknown_read")
                continue
            for row in reads:
                if not isinstance(row, dict) or not merger.string(row.get("path")):
                    reasons.add("unknown_read")
                    continue
                path = row["path"]
                if PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive or PureWindowsPath(path).is_absolute():
                    reasons.add("absolute_read")
                    continue
                if row.get("scope", "repo") != "repo" or row.get("kind") in ("outside", "external"):
                    reasons.add("outside_read")
                    continue
                if row.get("kind") not in ("open", "read", "stat", "import", "bytecode", "exec", "mmap", "readlink"):
                    reasons.add("unknown_read")
                    continue
                try:
                    own_paths.add(select_tests.repo_path(path))
                except select_tests.InvalidInput:
                    reasons.add("outside_read")
        if not own_paths:
            reasons.add("missing_read")
        paths.update(own_paths)
    return paths, reasons


def build(trusted_map, out, directories):
    trusted_map, out = Path(trusted_map), Path(out)
    destination = out.resolve()
    roots = [Path(directory).resolve() for directory in directories]
    reserved = "/".join(destination.parts[-3:]).lower() == select_tests.MAP_PATH.lower()
    if (destination == trusted_map.resolve() or reserved or
            any(destination.is_relative_to(root) for root in roots)):
        raise Refusal("path_escape")
    if out.exists() or out.is_symlink():
        raise Refusal("output_exists")
    trusted_raw = trusted_map.read_bytes()
    trusted = merger.decode(trusted_raw)
    if not isinstance(trusted, dict) or trusted.get("schema") != "leaf.ci.test-selection-map.v1":
        raise Refusal("malformed")
    if trusted.get("selection_enabled") is not False or trusted.get("phase") != "shadow":
        raise Refusal("trusted_map_enabled")
    minimum = trusted.get("minimum_full_runs")
    if (type(minimum) is not int or minimum < 1 or not isinstance(trusted.get("suites"), dict) or
            any(not isinstance(row, dict) for row in trusted["suites"].values())):
        raise Refusal("malformed")
    merger.suite_list(trusted.get("mandatory_suite_ids"))
    if len(directories) < minimum:
        raise Refusal("minimum_full_runs")
    captures = [read_merged(directory) for directory in directories]
    run_ids = [run["run_id"] for capture in captures for run in capture["runs"]]
    if len(run_ids) != len(set(run_ids)):
        raise Refusal("duplicate_run")
    baseline = captures[0]
    if (not merger.string(trusted.get("repo")) or
            baseline["lineage"]["repo"] != trusted["repo"].rsplit("/", 1)[-1]):
        merger.mismatch("repo")
    for capture in captures[1:]:
        compare_lineage(baseline["lineage"], capture["lineage"])
    proposals, exclusions = {}, {}
    for sid in sorted(baseline["entries"]):
        paths, reasons = set(), set()
        for capture in captures:
            observed, excluded = observed_paths(capture, sid)
            paths.update(observed)
            reasons.update(excluded)
        if reasons:
            exclusions[sid] = sorted(reasons)
        else:
            proposals[sid] = {"read_paths": sorted(paths), "runs": len(captures),
                              "status": "empirical", "closure_proven": False}
    provenance = []
    for capture in captures:
        for run in capture["runs"]:
            provenance.append(dict(run, merged_manifest_sha256=capture["sha256"]))
    candidate = copy.deepcopy(trusted)
    candidate.update(catalog_snapshot=baseline["catalog"], catalog_sha256=baseline["lineage"]["catalog_sha256"],
                     capture_epoch=baseline["lineage"]["capture_epoch"],
                     external_root_digests=baseline["lineage"]["external_root_digests"],
                     capture_proposals=proposals, proposal_exclusions=exclusions,
                     builder={"schema": "leaf.ci.selection-map-builder.v1", "run_ids": sorted(run_ids),
                              "runs": sorted(provenance, key=lambda run: run["run_id"]),
                              "merged_manifest_sha256": sorted(capture["sha256"] for capture in captures),
                              "trusted_map_sha256": sha256(trusted_raw)})
    raw = select_tests.canonical(candidate) + b"\n"
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix="." + out.name + ".", dir=out.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, out)
        except FileExistsError as exc:
            raise Refusal("output_exists") from exc
    finally:
        if temporary is not None:
            os.unlink(temporary)


def main(argv=None):
    parser = merger.Parser(description=__doc__)
    parser.add_argument("--trusted-map", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("merged_dirs", nargs="+")
    try:
        args = parser.parse_args(argv)
        build(args.trusted_map, args.out, args.merged_dirs)
    except Refusal as exc:
        print(json.dumps({"built": False, "reason": str(exc)}, sort_keys=True))
        return 1
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        print(json.dumps({"built": False, "reason": "malformed"}, sort_keys=True))
        return 1
    except OSError:
        print(json.dumps({"built": False, "reason": "io_error"}, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
