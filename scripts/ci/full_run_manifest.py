"""Build the full-run binding from explicit inputs and the packed catalog."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import sys

# -I omits the script directory. Only this extracted, trusted directory is added.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import select_tests


BINDING_FIELDS = ("run_id", "source_sha", "source_tree", "capture_sha", "catalog_sha256")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def build_manifest(inputs, catalog, shards=()):
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
    manifest = {key: inputs[key] for key in ("repo", "run_id", "source_sha", "source_tree",
                "capture_sha", "execution_mode", "full_run_complete", "test_id_reporting_complete")}
    manifest.update(schema="leaf.ci.full-run.v1", catalog_sha256=fingerprint,
                    provider_bound=True, suite_ids=suite_ids,
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
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        inputs = json.load(sys.stdin)
        catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
        shards = []
        for path in sorted(Path(args.readsets).rglob("*.json")):
            if path.name.endswith(".misses.json"):
                continue
            shards.append(json.loads(path.read_text(encoding="utf-8")))
        manifest = build_manifest(inputs, catalog, shards)
        # Validate everything before touching the output.
        raw = canonical(manifest) + b"\n"
        Path(args.output).write_bytes(raw)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print("full-run manifest failed: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
