"""Merge extracted captures without turning several runs into a full-run proof.

The union retains archive-relative readset/report/outcome paths and original
bytes. merged-manifest.json describes coverage; shards/<index>/ retains the
original metadata, including full-run.json. There is no synthetic full-run.json
at the output root. A selecting build can use capture/capture-epoch.json with
select_tests.verify_capture_epoch, but merging never admits a suite to a map.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sys
import tempfile

# Match the trusted helpers' isolated (-I -B) entry point.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_tests


BINDINGS = ("run_id", "source_sha", "source_tree", "capture_sha", "catalog_sha256")
REFERENCES = ("outcomes_ref", "process_tree_ref", "union_ref")
LINEAGE = ("source_sha", "source_tree", "runner", "catalog_sha256", "image_digest",
           "python_version", "capture_sha", "capture_epoch", "toolchain_fingerprint", "repo")


class Refusal(ValueError):
    """A stable, machine-readable refusal reason."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal("malformed")


def canonical(value):
    return select_tests.canonical(value)


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise Refusal("malformed")
        result[key] = value
    return result


def invalid_constant(_):
    raise Refusal("malformed")


def decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise Refusal("malformed") from exc


def load(path):
    return decode(path.read_bytes())


def member_name(name):
    """Archive members use POSIX paths, even when inspected on Windows."""
    if (not isinstance(name, str) or not name or "\x00" in name or "\\" in name or
            PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive or
            any(part in ("", ".", "..") for part in name.split("/"))):
        raise Refusal("path_escape")
    return name


def inventory(root):
    if root.is_symlink():
        raise Refusal("path_escape")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise Refusal("malformed")
    members = {}

    def walk_error(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = member_name(path.relative_to(root).as_posix())
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise Refusal("path_escape")
            if name in files:
                if not path.is_file():
                    raise Refusal("malformed")
                members[relative] = path
    return root, members


def declared_paths(value):
    """Check member declarations, never traced absolute external input paths."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in REFERENCES:
                member_name(item)
            elif key in ("members", "archive_members", "readsets_archive_members"):
                if not isinstance(item, (list, dict)):
                    raise Refusal("malformed")
                for name in item:
                    member_name(name)
            declared_paths(item)
    elif isinstance(value, list):
        for item in value:
            declared_paths(item)


def string(value):
    return isinstance(value, str) and bool(value.strip())


def suite_list(value):
    if (not isinstance(value, list) or not all(string(sid) for sid in value) or
            len(value) != len(set(value))):
        raise Refusal("malformed")
    return set(value)


def read_shard(directory):
    root, members = inventory(Path(directory))
    if "full-run.json" not in members or "catalog.json" not in members:
        raise Refusal("malformed")
    manifest, catalog = load(members["full-run.json"]), load(members["catalog.json"])
    if (not isinstance(manifest, dict) or manifest.get("schema") != "leaf.ci.full-run.v1" or
            not isinstance(catalog, dict)):
        raise Refusal("malformed")
    documents = {name: load(path) for name, path in sorted(members.items()) if name.endswith(".json")}
    for document in documents.values():
        declared_paths(document)
    for name, path in sorted(members.items()):
        if name.endswith(".jsonl"):
            with path.open("rb") as stream:
                for line in stream:
                    if line.strip() and not isinstance(decode(line), dict):
                        raise Refusal("malformed")
    epoch = manifest.get("capture_epoch_manifest")
    if not isinstance(epoch, dict):
        raise Refusal("missing_lineage:capture_epoch")
    interpreter = epoch.get("interpreter_identity")
    lineage = {key: manifest.get(key) for key in LINEAGE}
    lineage.update(runner=catalog.get("runner_blob_sha", catalog.get("runner_sha256")),
                   image_digest=epoch.get("image_manifest_digest"),
                   python_version=interpreter.get("version") if isinstance(interpreter, dict) else None)
    for key in LINEAGE:
        if not string(lineage[key]):
            raise Refusal("missing_lineage:" + key)
    if not string(manifest.get("run_id")):
        raise Refusal("malformed")
    readsets = {name: doc for name, doc in documents.items()
                if name.startswith("readsets/") and not name.endswith(".misses.json")}
    for doc in readsets.values():
        if not isinstance(doc, dict) or doc.get("schema") != "leaf.ci.readset.v1" or not string(doc.get("suite_id")):
            raise Refusal("malformed")
    # full-run.suite_ids is the *whole catalog*, not the subset executed by --only.
    accepted = {doc["suite_id"] for doc in readsets.values()}
    claims = []
    if "accepted_suite_ids" in manifest:
        claims.append(suite_list(manifest["accepted_suite_ids"]))
    if "workers_by_suite" in manifest:
        workers = manifest["workers_by_suite"]
        if not isinstance(workers, dict):
            raise Refusal("malformed")
        claims.append(suite_list(list(workers)))
    decision = documents.get("decision.json")
    if isinstance(decision, dict) and "executed_suite_ids" in decision:
        claims.append(suite_list(decision["executed_suite_ids"]))
    if any(claim - accepted for claim in claims) or not accepted:
        raise Refusal("missing_readsets")
    if any(accepted - claim for claim in claims):
        raise Refusal("malformed")
    return {"root": root, "members": members, "documents": documents, "manifest": manifest,
            "catalog": catalog, "lineage": lineage, "epoch": epoch,
            "readsets": readsets, "accepted": accepted}


def mismatch(field):
    raise Refusal("lineage_mismatch:" + field)


def validate_shard(shard):
    manifest, catalog, epoch = shard["manifest"], shard["catalog"], shard["epoch"]
    for key in ("source_sha", "source_tree", "capture_sha"):
        if not re.fullmatch(r"[0-9a-fA-F]{40}", manifest[key]):
            raise Refusal("malformed")
    if not re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{64}", shard["lineage"]["image_digest"]):
        raise Refusal("malformed")
    try:
        entries, fingerprint = select_tests.catalog_info(catalog)
    except ValueError as exc:
        raise Refusal("lineage_mismatch:catalog_sha256") from exc
    if fingerprint != manifest["catalog_sha256"] or catalog.get("catalog_sha256") != fingerprint:
        mismatch("catalog_sha256")
    if (epoch.get("schema") != "leaf.ci.capture-epoch.v1" or
            select_tests.normalized_epoch(epoch) != manifest["capture_epoch"]):
        mismatch("capture_epoch")
    if epoch.get("toolchain_fingerprint") != manifest["toolchain_fingerprint"]:
        mismatch("toolchain_fingerprint")
    packed_epoch = shard["documents"].get("capture/capture-epoch.json")
    if packed_epoch is not None and select_tests.normalized_epoch(packed_epoch) != manifest["capture_epoch"]:
        mismatch("capture_epoch")
    if suite_list(manifest.get("suite_ids")) != set(entries) or not shard["accepted"] <= set(entries):
        raise Refusal("malformed")
    binding = manifest.get("provider_binding")
    if manifest.get("provider_bound") is not True or not isinstance(binding, dict):
        raise Refusal("malformed")
    for key in BINDINGS + ("capture_epoch", "toolchain_fingerprint"):
        if binding.get(key) != manifest[key]:
            mismatch(key)
    proven = set()
    by_suite = {}
    workers = {}
    references = set()
    certificates = set()
    for doc in shard["readsets"].values():
        for key in BINDINGS:
            if doc.get(key) != manifest[key]:
                mismatch(key)
        if doc.get("capture_epoch") != manifest["capture_epoch"]:
            mismatch("capture_epoch")
        if "capture_epoch_manifest" in doc and select_tests.normalized_epoch(
                doc["capture_epoch_manifest"]) != manifest["capture_epoch"]:
            mismatch("capture_epoch")
        for key in REFERENCES:
            ref = doc.get(key)
            if key != "outcomes_ref" and doc.get("trace_kind") != "linux-process-tree":
                continue
            member_name(ref)
            if ref not in shard["members"] or ref.startswith("readsets-rejected/"):
                raise Refusal("missing_readsets")
            references.add(ref)
        if not string(doc.get("worker")):
            raise Refusal("malformed")
        workers.setdefault(doc["suite_id"], set()).add(doc["worker"])
        complete = (doc.get("trace_kind") == "linux-process-tree" and doc.get("python_only") is False and
                    shard["members"][doc["outcomes_ref"]].stat().st_size > 0 and
                    all(doc.get(key) is True for key in
                        ("capture_complete", "children_complete", "generated_inputs_resolved",
                         "shared_state_closure_resolved", "external_inputs_resolved")) and
                    not doc.get("incomplete_reasons") and not doc.get("capture_errors") and
                    doc.get("trace_loss") is False)
        if doc.get("trace_kind") == "linux-process-tree":
            certificate = shard["documents"].get(doc["process_tree_ref"])
            if not isinstance(certificate, dict) or certificate.get("schema") != "leaf.ci.process-tree.v1":
                raise Refusal("malformed")
            if digest(certificate) != doc.get("process_tree_sha256") or doc["union_ref"] != doc["process_tree_ref"]:
                mismatch("process_tree_sha256")
            for key in BINDINGS + ("capture_epoch",):
                if certificate.get(key) != manifest[key]:
                    mismatch(key)
            if select_tests.normalized_epoch(certificate.get("capture_epoch_manifest")) != manifest["capture_epoch"]:
                mismatch("capture_epoch")
            certificates.add(digest(certificate))
            complete = (complete and doc.get("decoder_complete") is True and
                        certificate.get("complete") is True and certificate.get("decoder_complete") is True and
                        certificate.get("trace_loss") is False and not certificate.get("reasons") and
                        not certificate.get("capture_errors"))
        by_suite.setdefault(doc["suite_id"], []).append(complete)
    if "process_tree_sha256s" in manifest and manifest["process_tree_sha256s"] != sorted(certificates):
        mismatch("process_tree_sha256s")
    if "workers_by_suite" in manifest:
        for sid, names in manifest["workers_by_suite"].items():
            expected = suite_list(names)
            if expected - workers.get(sid, set()):
                raise Refusal("missing_readsets")
            if expected != workers.get(sid, set()):
                raise Refusal("malformed")
    for sid, complete in by_suite.items():
        if all(complete):
            proven.add(sid)
    shard.update(entries=entries, proven=proven, references=references)


def merge(out, directories):
    out = Path(out)
    # No output may live in an input (or contain one), even through a symlink.
    roots = [Path(directory).resolve() for directory in directories]
    destination = out.resolve()
    if any(destination.is_relative_to(root) or root.is_relative_to(destination) for root in roots):
        raise Refusal("path_escape")
    if out.exists() or out.is_symlink():
        raise Refusal("output_exists")
    shards = [read_shard(directory) for directory in directories]
    for field in LINEAGE:
        if any(shard["lineage"][field] != shards[0]["lineage"][field] for shard in shards[1:]):
            mismatch(field)
    accepted = set()
    for shard in shards:
        if accepted & shard["accepted"]:
            raise Refusal("overlap")
        accepted.update(shard["accepted"])
        validate_shard(shard)
    shards.sort(key=lambda shard: (shard["manifest"]["run_id"], tuple(sorted(shard["accepted"]))))
    copies = {}

    def copy_member(name, path):
        member_name(name)
        if name in copies and copies[name].read_bytes() != path.read_bytes():
            raise Refusal("overlap")
        copies[name] = path

    records = []
    for index, shard in enumerate(shards):
        for name, path in sorted(shard["members"].items()):
            if name.startswith("readsets-rejected/"):
                continue
            if name.startswith(("readsets/", "reports/")) or name in shard["references"]:
                # Reserve generated metadata and per-shard provenance namespaces.
                if name in ("catalog.json", "merged-manifest.json", "full-run.json") or name.startswith("shards/"):
                    raise Refusal("overlap")
                copy_member(name, path)
            else:
                copy_member("shards/" + str(index) + "/" + name, path)
        records.append({"run_id": shard["manifest"]["run_id"],
                        "manifest": "shards/" + str(index) + "/full-run.json",
                        "manifest_sha256": hashlib.sha256(shard["members"]["full-run.json"].read_bytes()).hexdigest(),
                        "accepted_suite_ids": sorted(shard["accepted"])})
    catalog_path = min((shard["members"]["catalog.json"] for shard in shards), key=lambda path: path.read_bytes())
    copy_member("catalog.json", catalog_path)
    suites = set(shards[0]["entries"])
    proven = set().union(*(shard["proven"] for shard in shards))
    uncovered = suites - accepted
    merged = dict(shards[0]["lineage"], schema="leaf.ci.capture-merge.v1", shards=records,
                  run_ids=[record["run_id"] for record in records], suite_ids=sorted(suites),
                  accepted_suite_ids=sorted(accepted), uncovered=sorted(uncovered),
                  # Capture coverage is not map admission (which needs multiple
                  # complete producer runs and collection/closure evidence).
                  unproven=sorted(accepted - proven), mandatory_suite_ids=sorted(suites),
                  complete_scope="capture_coverage",
                  complete=not (suites - proven), full_run_complete=False,
                  test_id_reporting_complete=False)
    generated = {"merged-manifest.json": canonical(merged) + b"\n",
                 "capture/capture-epoch.json": canonical(min(
                     (shard["epoch"] for shard in shards), key=canonical)) + b"\n"}
    for name in generated:
        if name in copies:
            raise Refusal("overlap")
    # Detect file/directory collisions before staging, too.
    names = set(copies) | set(generated)
    for name in names:
        if any(parent.as_posix() in names for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")):
            raise Refusal("overlap")
    stage = None
    try:
        stage = Path(tempfile.mkdtemp(prefix="." + out.name + ".", dir=out.parent))
        for name, path in sorted(copies.items()):
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        for name, raw in sorted(generated.items()):
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        if out.exists() or out.is_symlink():
            raise Refusal("output_exists")
        stage.rename(out)
        stage = None
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("shards", nargs="+")
    try:
        args = parser.parse_args(argv)
        merge(args.out, args.shards)
    except Refusal as exc:
        print(json.dumps({"merged": False, "reason": str(exc)}, sort_keys=True))
        return 1
    except (ValueError, TypeError, KeyError, RecursionError):
        print(json.dumps({"merged": False, "reason": "malformed"}, sort_keys=True))
        return 1
    except OSError:
        print(json.dumps({"merged": False, "reason": "io_error"}, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
