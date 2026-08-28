#!/usr/bin/env python3
"""Sync the vendored mushy-code library into this repo, or verify the vendor.

leaf-web-demo consumes LEAF-Solar-Design/mushy-code as a VENDORED GIT-PINNED
COPY rather than a submodule or git dependency: every CI job here runs on a
GitHub-hosted runner whose checkout token cannot read a private sibling repo,
and this repo's CI is deliberately hermetic (see the cross-repo contract
suite's posture). The pin manifests record the upstream commit and a sha256
per vendored file, so `--verify` is tamper-evident without network access.

Vendored surfaces (upstream -> here):

  packages/author-ts/src/**        -> harness/src/vendor/mushy-author/**
  packages/author-ts/scripts/git-worker.cjs
                                   -> harness/scripts/git-worker.cjs
  packages/fold-py/src/mushy_fold/** -> server/_vendor/mushy_fold/**

usage:
  python scripts/sync-mushy-code.py --from <mushy-code checkout>   # re-vendor
  python scripts/sync-mushy-code.py --verify                       # CI check

Exit 0 on success / verified; 1 on drift or damage (with a per-file report).

PIN CONVENTION -- read this before re-vendoring:

  The vendored tree is NOT "whatever `upstream_commit` points at". It is
  upstream_commit PLUS the downstream patches listed in DOWNSTREAM_PATCHES
  below, applied directly to the vendored files here (never to the upstream
  repo) with only their per-file sha256 in the pin manifest hand-updated to
  match. `upstream_commit` itself is NOT bumped for these patches, because
  the patch never landed upstream.

  The per-file hash in VENDOR-PIN.json is therefore the real integrity
  check, not `upstream_commit`. `--verify` trusts it correctly. But
  `--from <checkout of upstream_commit>` does not know a patch exists: it
  rmtree's the vendored dir and recopies straight from upstream, which
  SILENTLY REVERTS every entry in DOWNSTREAM_PATCHES and then re-pins the
  reverted (unpatched) content as if it were correct. do_sync() below
  detects this by diffing the pre-sync pinned hash of each listed file
  against the post-sync one and refuses to write a manifest that reverts a
  known patch -- see check_for_reverted_patches().

  To retire an entry (i.e. the patch really did land upstream and
  `--from` now reproduces it byte-for-byte), delete it from
  DOWNSTREAM_PATCHES in the same commit that re-vendors it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SURFACES = [
    # (upstream_rel, vendored_abs, pin_abs)
    ("packages/author-ts/src", REPO / "harness" / "src" / "vendor" / "mushy-author",
     REPO / "harness" / "src" / "vendor" / "VENDOR-PIN.json"),
    ("packages/fold-py/src/mushy_fold", REPO / "server" / "_vendor" / "mushy_fold",
     REPO / "server" / "_vendor" / "VENDOR-PIN.json"),
]
# Single files ride the first surface's pin manifest.
SINGLE_FILES = [
    ("packages/author-ts/scripts/git-worker.cjs", REPO / "harness" / "scripts" / "git-worker.cjs"),
]

# Files patched directly in the vendored tree, downstream of upstream_commit.
# Keyed by the "files" key used in the pin manifest (surface-relative path).
# Re-vendoring from upstream_commit alone reverts these; see PIN CONVENTION
# above. Each entry names the PR that introduced the patch and what it does.
DOWNSTREAM_PATCHES = {
    "ports/impl/converseSdkRunner.ts":
        "PR #761: classify a clean SDK EOF that already streamed visible "
        "text as end_turn instead of an internal error.",
    "ports/impl/tenantChangeRepo.ts":
        "p8-unit4: real vendored Git adapter for staged_tree resolution "
        "and the closed, digest-only Unit 3 coordination client.",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def tree_manifest(root: Path) -> dict[str, str]:
    # Interpreter bytecode is a LOCAL, gitignored artifact: any process that
    # imports the vendored package writes __pycache__ here. It is not vendored
    # content, so it is neither pinned nor counted as drift.
    return {
        p.relative_to(root).as_posix(): sha256(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    }


def check_for_reverted_patches(
    vendored: Path, old_files: dict[str, str], new_files: dict[str, str]
) -> list[str]:
    """Diff pre- and post-sync pinned hashes for every DOWNSTREAM_PATCHES
    entry under this surface. A changed hash means --from just overwrote a
    hand-applied patch with plain upstream content."""
    hits = []
    for rel, why in DOWNSTREAM_PATCHES.items():
        old_hash, new_hash = old_files.get(rel), new_files.get(rel)
        if old_hash is not None and new_hash is not None and old_hash != new_hash:
            hits.append(f"  {(vendored / rel).relative_to(REPO)}: {why}")
    return hits


def do_sync(upstream: Path) -> int:
    head = subprocess.run(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(upstream), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        print("refusing to vendor from a DIRTY upstream checkout (pin would lie):")
        print(dirty)
        return 1
    for upstream_rel, vendored, pin in SURFACES:
        src = upstream / upstream_rel
        if not src.is_dir():
            print(f"upstream surface missing: {src}")
            return 1
        old_files = (
            json.loads(pin.read_text(encoding="utf-8")).get("files", {})
            if pin.exists() else {}
        )
        if vendored.exists():
            shutil.rmtree(vendored)
        # pyc-free discipline (spec non-negotiable #3): never vendor bytecode.
        shutil.copytree(src, vendored,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        manifest = {
            "contract": "leaf.vendor-pin.v1",
            "upstream": "LEAF-Solar-Design/mushy-code",
            "upstream_commit": head,
            "surface": upstream_rel,
            "files": tree_manifest(vendored),
        }
        reverted = check_for_reverted_patches(vendored, old_files, manifest["files"])
        if reverted:
            print("REFUSING to write the pin: this sync would silently revert "
                  "known downstream patches (see DOWNSTREAM_PATCHES / PIN "
                  "CONVENTION at the top of this file):")
            for line in reverted:
                print(line)
            print("Re-apply the patch on top of the freshly vendored file, "
                  "then re-run --from; or, if it truly landed upstream, "
                  "remove its DOWNSTREAM_PATCHES entry in the same commit.")
            return 1
        extras = {}
        if pin == SURFACES[0][2]:
            for single_rel, single_dst in SINGLE_FILES:
                shutil.copy2(upstream / single_rel, single_dst)
                extras[str(single_dst.relative_to(REPO)).replace("\\", "/")] = sha256(single_dst)
            manifest["single_files"] = extras
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"vendored {upstream_rel} @ {head[:12]} -> {vendored.relative_to(REPO)}"
              f" ({len(manifest['files'])} files)")
    return 0


def do_verify() -> int:
    bad = 0
    for _upstream_rel, vendored, pin in SURFACES:
        if not pin.exists():
            print(f"missing pin manifest: {pin}")
            bad += 1
            continue
        manifest = json.loads(pin.read_text(encoding="utf-8"))
        actual = tree_manifest(vendored) if vendored.is_dir() else {}
        for rel, want in manifest["files"].items():
            got = actual.pop(rel, None)
            if got != want:
                print(f"DRIFT {vendored.relative_to(REPO)}/{rel}: {want[:12]} != {str(got)[:12]}")
                bad += 1
        for rel in actual:
            print(f"UNPINNED file in vendor tree: {vendored.relative_to(REPO)}/{rel}")
            bad += 1
        for rel, want in manifest.get("single_files", {}).items():
            p = REPO / rel
            got = sha256(p) if p.is_file() else None
            if got != want:
                print(f"DRIFT {rel}: {want[:12]} != {str(got)[:12]}")
                bad += 1
    if bad:
        print(f"vendor verify: NOT-READY ({bad} problems)")
        return 1
    print("vendor verify: READY (all vendored files match their pin)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--from", dest="upstream", help="path to a clean mushy-code checkout")
    group.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if args.verify:
        return do_verify()
    return do_sync(Path(args.upstream).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
