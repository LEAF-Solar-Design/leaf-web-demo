"""Build the full-run binding from explicit inputs and the packed catalog."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import sys

# -I omits the script directory. Only this extracted, trusted directory is added.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_tests


BINDING_FIELDS = ("run_id", "source_sha", "source_tree", "capture_sha", "catalog_sha256")
COLLECTION_UNSET = object()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def partition_readsets(readsets, rejected, catalog, run_id):
    """Quarantine foreign shards and their outcomes before manifest validation."""
    entries, _ = select_tests.catalog_info(catalog)
    readsets, rejected = Path(readsets), Path(rejected)
    files = sorted(path for path in readsets.rglob("*") if path.is_file())
    members = {path.relative_to(readsets.parent).as_posix(): path for path in files}
    accepted = set()
    rejected_files = set()
    accepted_dirs = set()
    accepted_outcomes = set()
    count = 0
    for path in files:
        if path.suffix != ".json" or path.name.endswith(".misses.json"):
            continue
        try:
            shard = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            shard = {}
        if not isinstance(shard, dict):
            shard = {}
        ref = shard.get("outcomes_ref")
        outcomes = members.get(ref) if isinstance(ref, str) else None
        # Older plugin shards used a filename relative to the shard directory.
        if outcomes is None and isinstance(ref, str) and Path(ref).name == ref:
            candidate = path.parent / ref
            if candidate in files:
                outcomes = candidate
        sid = shard.get("suite_id")
        if isinstance(sid, str) and sid in entries and run_id and shard.get("run_id") == run_id:
            accepted.add(path)
            accepted.add(path.with_suffix(".misses.json"))
            accepted_dirs.add(path.parent)
            if outcomes is not None:
                accepted_outcomes.add(outcomes)
        else:
            count += 1
            rejected_files.update((path, path.with_suffix(".misses.json"),
                                   path.parent / ("attempts-" + path.stem + ".jsonl")))
            if outcomes is not None:
                rejected_files.add(outcomes)
    for path in files:
        if path in accepted or path in accepted_outcomes:
            continue
        if (path.parent in accepted_dirs and path not in rejected_files and
                path.name.startswith("attempts") and path.suffix == ".jsonl"):
            continue
        destination = rejected / path.relative_to(readsets)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
    return count


def build_manifest(inputs, catalog, shards=(), collection_ids_by_suite=COLLECTION_UNSET):
    if not isinstance(inputs, dict):
        raise ValueError("invalid_manifest_inputs")
    entries, fingerprint = select_tests.catalog_info(catalog)
    if catalog.get("catalog_sha256") != fingerprint:
        raise ValueError("catalog_digest_mismatch")
    for key in ("source_sha", "source_tree", "capture_sha"):
        if not isinstance(inputs.get(key), str) or not re.fullmatch(r"[0-9a-fA-F]{40}", inputs[key]):
            raise ValueError("invalid_" + key)
    run_id = inputs.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("invalid_run_id")
    if inputs.get("repo") not in ("leaf-web-demo", "leaf-automation-aws-terraform"):
        raise ValueError("invalid_repo")
    if inputs.get("execution_mode") not in ("full", "selected"):
        raise ValueError("invalid_execution_mode")
    for key in ("full_run_complete", "test_id_reporting_complete"):
        if type(inputs.get(key)) is not bool:
            raise ValueError("invalid_" + key)
    if inputs["full_run_complete"] and (inputs["execution_mode"] != "full" or
                                       not inputs["test_id_reporting_complete"]):
        raise ValueError("invalid_completion")
    image = inputs.get("image", "")
    if not isinstance(image, str):
        raise ValueError("invalid_image")
    suite_ids = sorted(entries)
    if not suite_ids or any(not isinstance(sid, str) or not sid.strip() for sid in suite_ids):
        raise ValueError("invalid_suite_ids")
    if collection_ids_by_suite is COLLECTION_UNSET:
        collection_ids_by_suite = {}
    if not isinstance(collection_ids_by_suite, dict):
        raise ValueError("invalid_collection_ids_by_suite")
    for sid, ids in collection_ids_by_suite.items():
        if (sid not in entries or not isinstance(ids, list) or not ids or
                any(not isinstance(tid, str) or not tid.startswith(sid + "::") or
                    not tid[len(sid) + 2:] for tid in ids) or ids != sorted(set(ids))):
            raise ValueError("invalid_collection_ids_by_suite")
    manifest = {key: inputs[key] for key in ("repo", "run_id", "source_sha", "source_tree",
                "capture_sha", "execution_mode", "full_run_complete", "test_id_reporting_complete")}
    manifest.update(schema="leaf.ci.full-run.v1", catalog_sha256=fingerprint,
                    provider_bound=True, suite_ids=suite_ids,
                    collection_ids_by_suite=collection_ids_by_suite,
                    toolchain_fingerprint=hashlib.sha256(canonical({
                        "schema": "leaf.ci.toolchain.v1", "python": platform.python_version(),
                        "image": image, "capture_sha": inputs["capture_sha"]})).hexdigest())
    manifest["provider_binding"] = {key: manifest[key] for key in BINDING_FIELDS}
    workers = {}
    for shard in shards:
        if not isinstance(shard, dict) or shard.get("schema") != "leaf.ci.readset.v1":
            raise ValueError("invalid_shard")
        sid, worker = shard.get("suite_id"), shard.get("worker")
        if sid not in entries or not isinstance(worker, str) or not worker.strip():
            raise ValueError("invalid_shard_worker")
        if any(shard.get(key) != manifest[key] for key in BINDING_FIELDS):
            raise ValueError("shard_binding_mismatch")
        workers.setdefault(sid, set()).add(worker)
    if workers:
        manifest["workers_by_suite"] = {sid: sorted(names) for sid, names in sorted(workers.items())}
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--readsets", required=True)
    parser.add_argument("--collection", help="Path to the collected IDs by suite JSON object")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        inputs = json.load(sys.stdin)
        catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
        collection = {}
        if args.collection:
            collection = json.loads(Path(args.collection).read_text(encoding="utf-8"))
            if not isinstance(collection, dict):
                raise ValueError("invalid_collection_ids_by_suite")
        shards = []
        for path in sorted(Path(args.readsets).rglob("*.json")):
            if path.name.endswith(".misses.json"):
                continue
            shards.append(json.loads(path.read_text(encoding="utf-8")))
        manifest = build_manifest(inputs, catalog, shards, collection)
        # Validate everything before touching the output.
        raw = canonical(manifest) + b"\n"
        Path(args.output).write_bytes(raw)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print("full-run manifest failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
