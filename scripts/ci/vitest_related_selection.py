"""Conservative Vitest graph inputs; no environment or git reads in select()."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

SOURCE_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
# Vitest adds setupFiles as rerun triggers itself. They execute for every test
# already, so including them as artificial reader inputs selects the full suite.
SETUP_FILES = {"web/vitest.setup.js"}
SHA = re.compile(r"[0-9a-fA-F]{40}")
# Deliberately lexical, including comments/strings: false positives cost work,
# false negatives lose source-reading contracts. Module markers cover aliases,
# destructured require(), and fs/promises without needing a JavaScript parser.
IO_MARKERS = re.compile(
    r"\b(?:fs|child_process|worker_threads|subprocess)\b|"
    r"\b(?:readFile\w*|readdir\w*|exists\w*|statSync|lstatSync|"
    r"createReadStream|readlink\w*|realpath\w*|opendir\w*|"
    r"execFile\w*|execSync|spawn\w*|Worker|fileURLToPath)\b|"
    r"import\.meta\.url|file:")


def safe_path(path):
    return (isinstance(path, str) and bool(path) and not path.startswith(("/", "-"))
            and not any(ord(c) < 32 or ord(c) == 127 for c in path)
            and "\\" not in path and ":" not in path
            # npx.cmd uses the runner's existing Windows shim dispatch. These
            # characters cannot safely cross that command-interpreter boundary.
            and not any(c in path for c in "&|<>^%!\"'`$();")
            and all(part not in ("", ".", "..") and not part.startswith("-")
                    for part in path.split("/")))


def force_full(path):
    p = PurePosixPath(path)
    parts = {part.lower() for part in p.parts}
    name = p.name.lower()
    return (bool(parts & {"fixtures", "__fixtures__", "mocks", "__mocks__",
                          "generated", "__generated__", "public", "vendor",
                          "wasm", "dist", ".next", "test", "tests", "test-utils",
                          "testutils", "testing", "e2e"})
            or any(marker in name for marker in
                   ("config", "setup", "package", "fixture", "mock", "generated",
                    "testharness", "testhelper", "testutil", "testtimescale"))
            or name.endswith((".lock", ".lockb")) or "-lock." in name
            or p.suffix.lower() not in SOURCE_SUFFIXES)


def select(base, changed, sources, *, error=None):
    """sources maps EVERY tracked web source path to readable text.

    Missing inventory or deleted/unsafe inputs must never become a partial
    graph. Outside-web changes have no proven workspace graph boundary.
    """
    decision = {"base": base if isinstance(base, str) and SHA.fullmatch(base) else None,
                "mode": "full", "reason": error or "missing_base",
                "changed_count": len(changed) if changed is not None else 0,
                "reader_count": 0, "inputs": []}
    if error or decision["base"] is None:
        return decision
    if changed is None or not sources:
        decision["reason"] = "inventory_unavailable"
        return decision
    if any(not safe_path(p) or not p.startswith("web/")
           or PurePosixPath(p).suffix.lower() not in SOURCE_SUFFIXES
           or not isinstance(text, str) for p, text in sources.items()):
        decision["reason"] = "inventory_unavailable"
        return decision
    readers = {p[4:] for p, text in sources.items()
               if p not in SETUP_FILES and IO_MARKERS.search(text)}
    decision["reader_count"] = len(readers)
    inputs = set(readers)
    for path in changed:
        if not safe_path(path):
            decision["reason"] = "unsafe_path"
            return decision
        if force_full(path):
            decision["reason"] = "global_input"
            return decision
        if path.startswith("web/"):
            if path not in sources:
                decision["reason"] = "missing_source"
                return decision
            inputs.add(path[4:])
        else:
            decision["reason"] = "outside_web"
            return decision
    decision.update(mode="related", reason="source_graph_and_readers", inputs=sorted(inputs))
    return decision


def collect(repo, env, *, run=None):
    """Resolve immutable local commits and inventory tracked working-tree sources.

    LEAF_CHANGED_PATHS is intentionally ignored: it proves neither the base nor
    the completeness of a diff. No network, shell, ref guessing or HEAD~1.
    """
    if env.get("LEAF_CI_FULL") == "1":
        return select(None, None, None, error="full_override")
    run = subprocess.run if run is None else run
    event = env.get("CODEBUILD_WEBHOOK_EVENT", "")
    ref = env.get("CODEBUILD_WEBHOOK_HEAD_REF", "")
    is_main = ref in {"refs/heads/main", "main"} or event == "PUSH"
    is_pr = (event.startswith("PULL_REQUEST") or ref.startswith("refs/pull/")
             or (not is_main and bool(env.get("TRUSTED_SHA") or env.get("LEAF_VITEST_PR_BASE"))))
    base = (env.get("TRUSTED_SHA") or env.get("LEAF_VITEST_PR_BASE")) if is_pr else env.get("LEAF_VITEST_LAST_GREEN_MAIN")
    if not base or not SHA.fullmatch(base):
        return select(None, None, None)
    changed = None

    def git(*args):
        result = run(["git", "--no-replace-objects", *args], cwd=repo,
                     capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=30)
        if result.returncode:
            raise ValueError("git input unavailable")
        return result.stdout

    try:
        if git("rev-parse", "--verify", base + "^{commit}").strip().lower() != base.lower():
            raise ValueError("base is not a commit")
        if is_pr:
            merges = git("merge-base", "--all", base, "HEAD").splitlines()
            if len(merges) != 1 or not SHA.fullmatch(merges[0]):
                raise ValueError("ambiguous merge base")
        else:
            git("merge-base", "--is-ancestor", base, "HEAD")
        raw = git("diff", "--name-only", "-z", "--no-renames", base + ("...HEAD" if is_pr else "..HEAD"), "--")
        if raw and not raw.endswith("\0"):
            raise ValueError("incomplete diff")
        changed = raw.split("\0")[:-1] if raw else []
        if any(not safe_path(p) for p in changed):
            return select(base, changed, {}, error="unsafe_path")
        root = Path(repo).resolve()
        for path in changed:
            target = root / path
            if not target.resolve().is_relative_to(root) or not target.is_file():
                return select(base, changed, {}, error="deleted_or_missing_path")
        raw = git("ls-files", "-z", "--", "web/")
        if not raw or not raw.endswith("\0"):
            raise ValueError("missing inventory")
        sources = {}
        for path in raw.split("\0")[:-1]:
            if not safe_path(path):
                raise ValueError("unsafe inventory")
            if PurePosixPath(path).suffix.lower() in SOURCE_SUFFIXES:
                target = root / path
                if not target.resolve().is_relative_to(root / "web") or target.is_symlink():
                    raise ValueError("unsafe inventory")
                sources[path] = target.read_text(encoding="utf-8")
        if not sources:
            raise ValueError("empty source inventory")
        return select(base, changed, sources)
    except (OSError, ValueError, UnicodeError, RuntimeError, subprocess.SubprocessError):
        return select(base, changed, None, error="comparison_or_inventory_unavailable")


def related_argv(argv, inputs):
    """Convert the known npm test:unit launcher; preserve every option/path."""
    if len(argv) < 4 or argv[1:4] != ["run", "test:unit", "--"]:
        raise ValueError("unsupported Vitest launcher")
    # Existing explicit fail30 paths remain inputs (union, never intersection).
    tail = list(argv[4:])
    return ["npx.cmd" if str(argv[0]).lower().endswith(".cmd") else "npx",
            "vitest", "related", *inputs, "--run", "--passWithNoTests", *tail]


def valid_related_result(decision, executed):
    return (isinstance(decision, dict) and decision.get("mode") == "related"
            and isinstance(decision.get("base"), str) and bool(SHA.fullmatch(decision["base"]))
            and decision.get("reason") == "source_graph_and_readers"
            and all(type(decision.get(k)) is int and decision[k] >= 0
                    for k in ("changed_count", "reader_count"))
            and type(executed) is int and executed >= 0
            and (executed > 0 or decision.get("empty") is True))
