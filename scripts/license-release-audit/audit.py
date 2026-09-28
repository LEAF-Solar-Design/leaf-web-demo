#!/usr/bin/env python3
"""Read-only license release audit for P-061 and P-062 (R10 lane 10, LA1).

P-061: acadrust crate source once lived OUTSIDE the license fence's
SCAN_ROOTS (at engine/acadrust-worker/), so the fence's green for that period
said nothing about it. This finds, from the current branch's git history,
every commit where acadrust crate source or a manifest naming acadrust existed
outside the fence's scan roots, and re-runs the fence
(scripts/check_license_fence.py, imported from its file path, never copied)
over that commit's tree with the missing root(s) added.

P-062: reports whether vendor/acadrust-worker/Cargo.toml still carries the
bare `rev =` git pin the review accepted, and whether the MPL-2.0 NOTICE line
is present in a shipped (non-doc, non-test) file.

Contract: git reads only (log, cat-file, archive, grep), no network, no
working-tree writes. Every git call is time-bounded; trees are materialized
into a temp dir that is removed afterwards, once per distinct tree object
(cached), so the cost scales with distinct trees, not with window length.
Fails closed: a git read or extraction error for one commit marks that commit
"unresolved" instead of guessing "clean". Exit 0 when the audit ran (findings
included), 2 when it could not run at all.

Usage: python scripts/license-release-audit/audit.py [--out FILE]
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older interpreters use the regex path
    tomllib = None

REPO_ROOT = Path(__file__).resolve().parents[2]
FENCE_RELPATH = "scripts/check_license_fence.py"
CARGO_RELPATH = "vendor/acadrust-worker/Cargo.toml"
REVIEW_RELPATH = "docs/CAD-ENGINE-LICENSE-REVIEW.md"
OWN_DIR_RELPATH = "scripts/license-release-audit"

GIT_TIMEOUT_S = 300
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024   # bound on one materialized root tree
MAX_BATCH_SPECS = 2_000_000             # bound on one cat-file batch
MAX_LISTED_VIOLATIONS = 50              # per verdict; the count is always exact

# A file named for acadrust counts as crate source or manifest only with one
# of these suffixes. engine/acadrust_adapter.py (a subprocess adapter holding
# no engine code) and test files are reported as not counted, never hidden.
NAMED_SOURCE_SUFFIXES = frozenset({".rs", ".wasm", ".toml", ".lock"})
MANIFEST_NAMES = frozenset({"Cargo.toml", "Cargo.lock"})
HEX40_RE = re.compile(r"[0-9a-f]{40}")


class AuditError(RuntimeError):
    pass


# --- git plumbing (bounded, bytes in and out) ------------------------------

def _git(args: list[str], *, input_bytes: bytes | None = None,
         ok_codes: tuple[int, ...] = (0,)) -> tuple[int, bytes]:
    cmd = ["git", "-C", str(REPO_ROOT), "-c", "core.quotepath=off", *args]
    try:
        proc = subprocess.run(cmd, input=input_bytes, capture_output=True,
                              timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError(f"git {' '.join(args[:3])}: {exc}") from exc
    if proc.returncode not in ok_codes:
        err = proc.stderr.decode("utf-8", "replace").strip()[:300]
        raise AuditError(f"git {' '.join(args[:3])} exit {proc.returncode}: {err}")
    return proc.returncode, proc.stdout


def _text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _batch_check(specs: list[str]) -> list[tuple[str, str] | None]:
    """One cat-file process for every `<rev>:<path>`; None means missing."""
    if not specs:
        return []
    if len(specs) > MAX_BATCH_SPECS:
        raise AuditError(f"cat-file batch of {len(specs)} specs exceeds {MAX_BATCH_SPECS}")
    payload = ("\n".join(specs) + "\n").encode("utf-8", "surrogateescape")
    _, out = _git(["cat-file", "--batch-check=%(objectname) %(objecttype)"],
                  input_bytes=payload)
    lines = _text(out).splitlines()
    if len(lines) != len(specs):
        raise AuditError(f"cat-file answered {len(lines)} lines for {len(specs)} specs")
    result: list[tuple[str, str] | None] = []
    for line in lines:
        parts = line.split(" ")
        if len(parts) == 2 and HEX40_RE.fullmatch(parts[0]):
            result.append((parts[0], parts[1]))
        else:  # "<spec> missing" / "<spec> ambiguous"
            result.append(None)
    return result


def _read_blobs(oids: list[str]) -> dict[str, bytes]:
    if not oids:
        return {}
    _, out = _git(["cat-file", "--batch"], input_bytes=("\n".join(oids) + "\n").encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        nl = out.index(b"\n", pos)
        header = out[pos:nl].decode("ascii", "replace").split(" ")
        pos = nl + 1
        if len(header) != 3:
            raise AuditError(f"cat-file --batch: unexpected header for {oid}")
        size = int(header[2])
        blobs[oid] = out[pos:pos + size]
        pos += size + 1
    return blobs


def _show(spec: str) -> str | None:
    code, out = _git(["cat-file", "-p", spec], ok_codes=(0, 128))
    return _text(out) if code == 0 else None


def _materialize_tree(tree_oid: str, dest: Path) -> int:
    """Extract one tree object into dest; returns the count of skipped links."""
    _, data = _git(["archive", "--format=tar", tree_oid])
    if len(data) > MAX_ARCHIVE_BYTES:
        raise AuditError(f"tree {tree_oid[:12]} archive {len(data)} bytes exceeds bound")
    dest.mkdir(parents=True, exist_ok=True)
    skipped = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if member.name.startswith("/") or ".." in parts:
                raise AuditError(f"unsafe archive member {member.name!r}")
            target = dest.joinpath(*parts) if parts else dest
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                if src is None:
                    raise AuditError(f"unreadable archive member {member.name!r}")
                target.write_bytes(src.read())
            else:
                skipped += 1
    return skipped


# --- the fence, imported from its own file ---------------------------------

def load_fence():
    path = REPO_ROOT / FENCE_RELPATH
    spec = importlib.util.spec_from_file_location("check_license_fence", path)
    if spec is None or spec.loader is None:
        raise AuditError(f"cannot load {FENCE_RELPATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- P-061: out-of-root acadrust history -----------------------------------

def classify_path(path: str, scan_roots: tuple[str, ...]) -> str | None:
    """Return 'crate_dir' | 'named_file' | 'manifest' | None for an out-of-root path."""
    parts = path.split("/")
    if parts[0] in scan_roots:
        return None
    if any("acadrust" in p.lower() for p in parts[:-1]):
        return "crate_dir"
    name = parts[-1]
    if "acadrust" in name.lower() and PurePosixPath(name).suffix.lower() in NAMED_SOURCE_SUFFIXES:
        return "named_file"
    if name in MANIFEST_NAMES:
        return "manifest"
    return None


def crate_unit(path: str) -> str | None:
    """The directory prefix ending at the first acadrust-named component."""
    parts = path.split("/")
    for i, part in enumerate(parts[:-1]):
        if "acadrust" in part.lower():
            return "/".join(parts[:i + 1])
    return None


def _log_paths(extra: list[str], pathspecs: list[str]) -> set[str]:
    _, out = _git(["log", "--format=@@%H", "--name-only", "--no-renames", *extra,
                   "HEAD", "--", *pathspecs])
    return {line for line in _text(out).splitlines() if line and not line.startswith("@@")}


def history_candidates(scan_roots: tuple[str, ...]) -> tuple[dict[str, str], list[str]]:
    named = _log_paths([], [":(icase)*acadrust*"])
    manifests = _log_paths(["-G", "[Aa][Cc][Aa][Dd][Rr][Uu][Ss][Tt]"],
                           [":(glob)**/Cargo.toml", ":(glob)**/Cargo.lock"])
    candidates: dict[str, str] = {}
    not_counted: list[str] = []
    for path in sorted(named | manifests):
        kind = classify_path(path, scan_roots)
        if kind is not None:
            candidates[path] = kind
        elif path.split("/")[0] not in scan_roots:
            not_counted.append(path)
    return candidates, not_counted


def branch_commits() -> list[tuple[str, str]]:
    _, out = _git(["log", "--format=%H%x09%cI", "HEAD"])
    rows = []
    for line in _text(out).splitlines():
        sha, _, date = line.partition("\t")
        if HEX40_RE.fullmatch(sha):
            rows.append((sha, date))
    if not rows:
        raise AuditError("HEAD has no history")
    return rows


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


class FenceRunner:
    """Runs the imported fence over materialized roots; cached per tree set."""

    def __init__(self, fence):
        self.fence = fence
        self.cache: dict[tuple, dict] = {}

    def verdict(self, roots: dict[str, tuple[str, str]], units: list[str]) -> dict:
        key = (tuple(sorted((r, o) for r, (o, _) in roots.items())), tuple(units))
        if key not in self.cache:
            self.cache[key] = self._run(roots, units)
        return self.cache[key]

    def _run(self, roots: dict[str, tuple[str, str]], units: list[str]) -> dict:
        fence = self.fence
        allowed = fence.ALLOWED_ACADRUST_PREFIX.rstrip("/")
        skipped = 0
        with tempfile.TemporaryDirectory(prefix="la1-audit-") as tmp:
            base = Path(tmp) / "tree"
            dir_roots, file_roots = [], []
            for root, (oid, typ) in sorted(roots.items()):
                if typ == "tree":
                    skipped += _materialize_tree(oid, base / root)
                    dir_roots.append(root)
                else:
                    target = base / root
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(_read_blobs([oid])[oid])
                    file_roots.append(root)
            violations = list(fence.scan_tree(base, scan_roots=tuple(dir_roots)))
            for root in file_roots:
                violations.extend(fence._scan_file(base / root, root))

            relocated = []
            for i, unit in enumerate(units):
                reloc_root = Path(tmp) / f"reloc{i}"
                shutil.copytree(base / unit, reloc_root / allowed)
                for v in fence.scan_tree(reloc_root, scan_roots=(allowed.split("/")[0],)):
                    relocated.append(f"[{unit} as {allowed}] {v.format()}")

        expected, real = [], []
        inside = 0
        for v in violations:
            in_crate = any(_under(v.path, u) for u in units)
            if v.kind == "acadrust_outside_prefix" and in_crate:
                expected.append(v.format())
            else:
                real.append(v.format())
                inside += in_crate
        return {
            "fence_source": f"{FENCE_RELPATH} (current branch, imported)",
            "scan_roots_added": sorted(roots),
            "root_object_ids": {r: o for r, (o, _) in sorted(roots.items())},
            "verdict": "clean" if not real and not relocated else "violations",
            "violation_count": len(real),
            "violations_inside_crate_dirs": inside,
            "violations_elsewhere_in_added_roots": len(real) - inside,
            "violations": real[:MAX_LISTED_VIOLATIONS],
            "crate_dirs_relocated_to_allowed_prefix": units,
            "relocated_violation_count": len(relocated),
            "relocated_violations": relocated[:MAX_LISTED_VIOLATIONS],
            "expected_prefix_hits_inside_crate_dirs": len(expected),
            "skipped_non_regular_members": skipped,
        }


def audit_window(fence) -> tuple[dict, list[dict], list[str]]:
    scan_roots = tuple(fence.SCAN_ROOTS)
    candidates, not_counted = history_candidates(scan_roots)
    commits = branch_commits()
    paths = sorted(candidates)
    specs = [f"{sha}:{p}" for sha, _ in commits for p in paths]
    found = _batch_check(specs)

    manifest_oids = sorted({hit[0] for p_idx, hit in enumerate(found)
                            if hit and candidates[paths[p_idx % len(paths)]] == "manifest"}) if paths else []
    manifest_blobs = _read_blobs(manifest_oids)
    mentions = {oid for oid, data in manifest_blobs.items() if b"acadrust" in data.lower()}

    present: list[tuple[str, str, list[str]]] = []
    for c_idx, (sha, date) in enumerate(commits):
        hits = []
        for p_idx, path in enumerate(paths):
            hit = found[c_idx * len(paths) + p_idx]
            if hit is None:
                continue
            if candidates[path] == "manifest" and hit[0] not in mentions:
                continue
            hits.append(path)
        if hits:
            present.append((sha, date, hits))

    root_specs = []
    for sha, _, hits in present:
        for root in sorted({h.split("/")[0] for h in hits}):
            root_specs.append((sha, root))
    root_found = dict(zip(root_specs, _batch_check([f"{s}:{r}" for s, r in root_specs])))

    runner = FenceRunner(fence)
    rows = []
    for sha, date, hits in reversed(present):  # oldest first
        units = sorted({u for u in (crate_unit(h) for h in hits) if u})
        roots_needed = sorted({h.split("/")[0] for h in hits})
        row = {"sha": sha, "date": date, "paths_outside_scan_roots": hits}
        try:
            roots = {}
            for root in roots_needed:
                obj = root_found.get((sha, root))
                if obj is None:
                    raise AuditError(f"root {root!r} unreadable at {sha[:12]}")
                roots[root] = obj
            verdict = runner.verdict(roots, units)
            row["fence_verdict_with_root_added"] = verdict
            if verdict["verdict"] == "clean":
                row["disposition"] = "clean"
                row["note"] = (
                    f"fence re-run over added root(s) {roots_needed}: no OpenCADStudio, "
                    f"no GPL-3.0, no acadrust reference outside the crate dir(s) {units}; "
                    f"{verdict['expected_prefix_hits_inside_crate_dirs']} acadrust hit(s) inside "
                    "the crate dir are the fence's prefix rule firing on the pre-move path, "
                    "and the same files relocated to the allowed prefix scan clean")
            else:
                row["disposition"] = "finding"
                row["note"] = (
                    f"{verdict['violations_inside_crate_dirs']} violation(s) inside the crate "
                    f"dir(s) {units}, {verdict['violations_elsewhere_in_added_roots']} elsewhere "
                    f"in the added root(s) {roots_needed}, and "
                    f"{verdict['relocated_violation_count']} with the crate relocated to the "
                    "allowed prefix; see fence_verdict_with_root_added")
        except (AuditError, OSError, ValueError) as exc:
            row["fence_verdict_with_root_added"] = {"verdict": "error", "error": str(exc)[:300]}
            row["disposition"] = "unresolved"
            row["note"] = f"could not materialize or scan this commit's tree: {str(exc)[:200]}"
        rows.append(row)

    window: dict = {"first_commit": None, "last_commit": None, "dates": {}}
    if rows:
        newest_idx = next(i for i, (sha, _) in enumerate(commits) if sha == rows[-1]["sha"])
        window = {
            "first_commit": rows[0]["sha"],
            "last_commit": rows[-1]["sha"],
            "dates": {"first": rows[0]["date"], "last": rows[-1]["date"]},
            "commit_count": len(rows),
            "next_newer_commit_in_log_order": commits[newest_idx - 1][0] if newest_idx > 0 else None,
            "distinct_fence_runs": len(runner.cache),
            "scan_roots_of_fence": list(scan_roots),
            "candidate_paths_in_history": paths,
        }
    return window, rows, not_counted


# --- P-062: Cargo pin shape and NOTICE presence ----------------------------

def _find_key(node, key: str, prefix: str = ""):
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{prefix}.{k}" if prefix else k
            if k == key:
                yield here, v
            yield from _find_key(v, key, here)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _find_key(v, key, f"{prefix}[{i}]")


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    if any(p in ("tests", "test", "__tests__") for p in parts[:-1]):
        return True
    return re.search(r"(^test_|_test\.|\.test\.|\.spec\.)", parts[-1]) is not None


def cargo_pin() -> dict:
    review = _show(f"HEAD:{REVIEW_RELPATH}") or ""
    m = re.search(r"rev-pinned at `([0-9a-f]{40})`", review)
    reviewed_rev = m.group(1) if m else None
    text = _show(f"HEAD:{CARGO_RELPATH}")
    if text is None:
        return {"cargo_pin_is_bare_rev": False, "reviewed_rev": reviewed_rev,
                "cargo_pin_detail": f"{CARGO_RELPATH} is missing at HEAD"}

    if tomllib is not None:
        occurrences = list(_find_key(tomllib.loads(text), "acadrust"))
    else:
        occurrences = [(f"line {n}", line.strip()) for n, line in enumerate(text.splitlines(), 1)
                       if re.match(r"\s*acadrust\s*=", line)]

    _, tree = _git(["ls-tree", "-r", "--name-only", "HEAD"])
    vendored = []
    for path in _text(tree).splitlines():
        parts = path.split("/")
        if "acadrust" in parts[:-1]:
            vendored.append(path)
        elif len(parts) >= 2 and parts[-2] == ".cargo" and parts[-1] in ("config", "config.toml"):
            cfg = _show(f"HEAD:{path}") or ""
            if "vendored-sources" in cfg or "replace-with" in cfg:
                vendored.append(path)

    dep = occurrences[0][1] if len(occurrences) == 1 else None
    bare = (
        len(occurrences) == 1
        and occurrences[0][0] == "dependencies.acadrust"
        and isinstance(dep, dict)
        and set(dep) == {"git", "rev"}
        and isinstance(dep.get("rev"), str)
        and HEX40_RE.fullmatch(dep["rev"]) is not None
        and not vendored
    )
    pinned = dep.get("rev") if isinstance(dep, dict) else None
    detail = (
        f"{CARGO_RELPATH}: acadrust occurrences {[(loc, val) for loc, val in occurrences]}; "
        f"{'bare git+rev pin' if bare else 'NOT a bare git+rev pin'}; "
        f"pinned rev {pinned} {'matches' if pinned and pinned == reviewed_rev else 'does not match'} "
        f"the reviewed rev {reviewed_rev} ({REVIEW_RELPATH}); "
        f"vendored-source indicators at HEAD: {vendored[:10] or 'none'}"
    )
    return {"cargo_pin_is_bare_rev": bool(bare), "pinned_rev": pinned,
            "reviewed_rev": reviewed_rev, "cargo_pin_detail": detail}


# Adjacent string literals split across lines ("...acadrust ... "\n  "licensed
# under ...") are joined before matching, so a NOTICE held in a source constant
# is found the same as one written on a single line.
_LITERAL_JOIN_RE = re.compile(r"""["'`]\s*\+?\s*\r?\n\s*\+?\s*["'`]""")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", _LITERAL_JOIN_RE.sub("", text)).strip().lower()


def _grep_lines(pattern: str, pathspecs: list[str]) -> list[tuple[str, str, str]]:
    code, out = _git(["grep", "-n", "-I", "-i", "-F", "-e", pattern,
                      "HEAD", "--", *pathspecs], ok_codes=(0, 1))
    rows = []
    if code == 1:
        return rows
    for line in _text(out).splitlines():
        body = line[len("HEAD:"):] if line.startswith("HEAD:") else line
        path, _, rest = body.partition(":")
        lineno, _, content = rest.partition(":")
        rows.append((path, lineno, content))
    return rows


def _notice_hits(pathspecs: list[str], reference: str | None) -> list[str]:
    """path:line of each non-test file whose text carries the acadrust MPL NOTICE."""
    hits = []
    for path in sorted({p for p, _, _ in _grep_lines("Mozilla Public License", pathspecs)}):
        if _is_test_path(path):
            continue
        normalized = _normalize(_show(f"HEAD:{path}") or "")
        if reference and reference in normalized:
            kind = "exact NOTICE text"
        elif re.search(r"acadrust.{0,300}mozilla public license", normalized):
            kind = "acadrust MPL notice, wording differs from the review's NOTICE"
        else:
            continue
        lines = [n for p, n, _ in _grep_lines("Mozilla Public License", [path]) if p == path]
        hits.append(f"{path}:{lines[0] if lines else '?'} ({kind})")
    return hits


def notice() -> dict:
    review = _show(f"HEAD:{REVIEW_RELPATH}") or ""
    m = re.search(r"^>\s*(This product includes acadrust.+)$", review, re.MULTILINE)
    reference = _normalize(m.group(1)) if m else None
    shipped = _notice_hits([".", ":(exclude)docs", ":(exclude)plans",
                            f":(exclude){OWN_DIR_RELPATH}"], reference)
    render_sites = [f"{p}:{n}" for p, n, _ in _grep_lines("cad_engine?.notice", ["web/src"])
                    if not _is_test_path(p)]
    if shipped:
        return {
            "notice_present": True,
            "notice_location_or_missing": "; ".join(shipped) + (
                f"; rendered by {', '.join(render_sites)}" if render_sites
                else "; no web render site found for cad_engine.notice"),
            "notice_reference": f"{REVIEW_RELPATH} 'NOTICE line' section",
        }
    docs_only = _notice_hits(["docs", "plans"], reference)
    return {
        "notice_present": False,
        "notice_location_or_missing": (
            "missing: no tracked non-doc, non-test file at HEAD carries the acadrust "
            "Mozilla Public License NOTICE; it appears only in "
            f"{docs_only or 'no file'} (review documents, not a shipped attributions surface)"),
        "notice_reference": f"{REVIEW_RELPATH} 'NOTICE line' section",
    }


def run_audit() -> dict:
    fence = load_fence()
    window, rows, not_counted = audit_window(fence)
    head = _text(_git(["rev-parse", "HEAD"])[1]).strip()
    p062 = {"cargo_manifest": CARGO_RELPATH, **cargo_pin(), **notice()}
    return {
        "schema": "leaf.license-release-audit.v1",
        "head": head,
        "window": window,
        "commits": rows,
        "out_of_root_acadrust_named_paths_not_counted": not_counted,
        "p062": p062,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="also write the JSON report to this file")
    args = parser.parse_args(argv)
    try:
        report = run_audit()
    except AuditError as exc:
        print(f"license-release-audit: cannot run: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
