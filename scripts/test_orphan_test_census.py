"""Census of server/browser tests with no textual gate registration.

This deliberately inspects source rather than importing the gate runner or
executing Playwright configs. Config coverage means testDir/testMatch coverage,
not a claim that every matching test runs in a gate's selected CLI invocation.
"""

import fnmatch
import json
from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[1]


def _string_property(source, name, default=None):
    match = re.search(r"\b" + name + r"\s*:\s*(['\"])(.*?)\1\s*[,}]", source)
    if match:
        return match.group(2)
    assert not re.search(r"\b" + name + r"\s*:", source), (
        f"Census needs support for non-literal {name}"
    )
    return default


def _ignored_patterns(source):
    match = re.search(r"\btestIgnore\s*:\s*\[([^\]]*)\]", source)
    if not match:
        assert not re.search(r"\btestIgnore\s*:", source), (
            "Census needs support for non-array testIgnore"
        )
        return []
    return re.findall(r"['\"]([^'\"]+)['\"]", match.group(1))


def _matches(path, pattern):
    # Playwright treats a bare filename/glob as matching in any subdirectory.
    if "/" not in pattern:
        return fnmatch.fnmatchcase(Path(path).name, pattern)
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
    )


def test_orphan_test_census():
    runner = (REPO / "scripts/run-all-gates.py").read_text(encoding="utf-8")
    server_files = sorted((REPO / "server/tests").glob("test_*.py"))
    browser_files = sorted((REPO / "web/e2e").rglob("*.spec.mjs"))
    files = server_files + browser_files
    registered = set()
    for path in files:
        # Server targets are relative to server/, browser targets to web/.
        rooted = path.relative_to(REPO).as_posix()
        prefix, target = rooted.split("/", 1)
        reference = r"(?<![\w/.-])(?:" + re.escape(prefix) + r"/)?" + re.escape(target)
        if re.search(reference + r"(?![\w.-])", runner):
            registered.add(path)

    # Only configs actually passed to --config in the gate catalog count.
    # In particular, the generated reporting wrapper is not a source config.
    configs = set(re.findall(
        r"['\"]--config['\"]\s*,\s*['\"]([^'\"]+)['\"]", runner
    ))
    assert configs, "No gate Playwright configs found; check catalog syntax"
    for name in sorted(configs):
        config = REPO / "web" / name
        source = config.read_text(encoding="utf-8")
        directory = (config.parent / _string_property(source, "testDir", ".")).resolve()
        pattern = _string_property(source, "testMatch", "**/*.spec.mjs")
        ignored = _ignored_patterns(source)
        for path in browser_files:
            if not path.is_relative_to(directory):
                continue
            relative = path.relative_to(directory).as_posix()
            if _matches(relative, pattern) and not any(
                _matches(relative, exclusion) for exclusion in ignored
            ):
                registered.add(path)

    orphans = {path.relative_to(REPO).as_posix() for path in files if path not in registered}
    entries = json.loads((REPO / "scripts/orphan-tests-allowlist.json").read_text(encoding="utf-8"))
    assert isinstance(entries, list), "Allow-list must be a list of path/reason entries"
    for entry in entries:
        assert isinstance(entry, dict) and set(entry) == {"path", "reason"}, entry
        assert isinstance(entry["path"], str) and entry["path"], entry
        assert isinstance(entry["reason"], str) and entry["reason"].strip(), entry
    allowed = {entry["path"] for entry in entries}
    assert len(allowed) == len(entries), "Duplicate orphan allow-list paths"
    assert not orphans - allowed, f"Unlisted orphan tests: {sorted(orphans - allowed)}"
    assert not allowed - orphans, f"Stale orphan entries: {sorted(allowed - orphans)}"
    assert len(orphans) == len(entries), "Orphan count differs from allow-list length"
