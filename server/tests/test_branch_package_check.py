"""Pinned runner parity, reachable validation rules, and adapter boundaries."""

from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import re
import socket
import sys
from types import SimpleNamespace

import pytest

from branch_tools import BranchCheckResult, check_branch_package
from branch_tools import package_check as checker


FIXTURES = Path(__file__).parent / "fixtures" / "branch_tools"
REFERENCE = "reference-package/panel-gap-check"


def package(relative=REFERENCE):
    root = FIXTURES / relative
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize(("relative", "digest"), [
    (REFERENCE, "079bc8c277c9cba64b38d881a58fdaede524d19636d7400887fc99273bcd1f72"),
    ("proof/panel-gap-check", "a76241bb6eb69fce4db10f843aeaad2765e8c4747ce2ee321856a6c728c31a95"),
])
def test_known_packages(relative, digest):
    files = package(relative)
    result = check_branch_package(files, "panel-gap-check")
    assert isinstance(result, BranchCheckResult)
    assert result.ok and result.failures == []
    assert result.package_digest == digest
    assert result.manifest == json.loads(files["tool.json"])
    assert result.allowlist_sha256 == "03830bbe9eb579e291a0e588556b2597497892de92a0485d15d269013b8c11dd"
    with pytest.raises(FrozenInstanceError):
        result.ok = False


@pytest.mark.parametrize("name", [
    "needs-host-api-2", "slow-loop", "throws-error", "unreviewed-tool",
    "write-without-capability",
])
def test_ctp_execution_negatives_are_valid_packages(name):
    files = package(f"negative/{name}")
    runner = checker._load_runner()
    failures = []
    manifest = runner.validate(files, name, failures)
    result = check_branch_package(files, name)
    assert failures == []
    assert result.ok and result.failures == []
    assert result.manifest == manifest
    assert result.package_digest == runner.package_digest(files)


# All V1..V17 are reachable through validate(files, name, failures).
# Archive-only branches of V3/V16 are outside this mapping-based API; their
# rule ids remain reachable via name mismatch and a disallowed file respectively.
@pytest.mark.parametrize(("rule", "field", "value"), [
    ("V1", None, None),
    ("V2", "unknown", True),
    ("V3", "name", "other-tool"),
    ("V4", "version", "invalid"),
    ("V5", "description", ""),
    ("V6", "entry", "tool.py"),
    ("V7", "params", {"type": "object", "properties": {"x": {"type": "bogus"}}}),
    ("V8", "capabilities", ["arbitrary"]),
    ("V9", "family", "INVALID"),
    ("V10", "hosts", {"min_release": 2000, "min_host_api": 1}),
    ("V11", "timeout_ms", 0),
    ("V12", "idempotent", "yes"),
    ("V13", "provenance", {}),
    ("V14", "review", {"status": "invalid"}),
    ("V15", "params", {"type": "object", "default": {"x": 1.5}}),
    ("V16", None, None),
    ("V17", None, None),
])
def test_every_reachable_rule(rule, field, value):
    files = package()
    if field is not None:
        manifest = json.loads(files["tool.json"])
        manifest[field] = value
        files["tool.json"] = json.dumps(manifest).encode()
    elif rule == "V1":
        del files["tool.json"]
    elif rule == "V16":
        files["tool.py"] = b"raise RuntimeError('must never execute')"
    else:
        files["main.csx"] = b'#r "untrusted.dll"\n'
    result = check_branch_package(files, "panel-gap-check")
    assert not result.ok
    assert rule in {failure["rule"] for failure in result.failures}
    for failure in result.failures:
        assert set(failure) == {"rule", "path", "message"}
        assert all(isinstance(value, str) for value in failure.values())
    if rule in ("V1", "V15", "V16", "V17"):
        expected = {"V1": "tool.json", "V15": "tool.json",
                    "V16": "tool.py", "V17": "main.csx"}[rule]
        assert any(f["rule"] == rule and f["path"] == expected for f in result.failures)


def test_studio_package_fails():
    result = check_branch_package({
        "tool.py": b"raise RuntimeError('must never execute')",
        "tool.json": json.dumps({"name": "studio-tool", "kind": "engine_op",
                                 "engine_op": "panel-gap-check"}).encode(),
    }, "studio-tool")
    assert not result.ok
    assert {"V2", "V6", "V16"} <= {f["rule"] for f in result.failures}


def test_source_hashes():
    source = (checker._VENDOR / "SOURCE.md").read_text(encoding="utf-8")
    assert "8791d2e519fd6a7a9f438073e689391765d9bd17" in source
    rows = re.findall(r"^\| ([^|]+) \| ([0-9a-f]{64}) \|$", source, re.MULTILINE)
    assert {path for path, _ in rows} == {
        "runner-linux/runner.py", "contract/tool.schema.json", "contract/api-allowlist.json",
    }
    assert len(rows) == 3
    for path, digest in rows:
        assert hashlib.sha256((checker._VENDOR / Path(path).name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(("files", "rule"), [
    ({f"lib/a{i}.csx": b"" for i in range(65)}, "cap_files"),
    ({"main.csx": b"x" * (4 * 1024 * 1024 + 1)}, "cap_bytes"),
])
def test_caps_precede_runner(files, rule, monkeypatch):
    def forbidden():
        pytest.fail("runner must not load for an oversized package")
    monkeypatch.setattr(checker, "_load_runner", forbidden)
    result = check_branch_package(files, "panel-gap-check")
    assert not result.ok
    assert [f["rule"] for f in result.failures] == [rule]
    assert result.package_digest is None and result.manifest is None


@pytest.mark.parametrize("limit", ["files", "bytes"])
def test_caps_are_inclusive(limit):
    files = package()
    if limit == "files":
        files.update({f"lib/a{i}.csx": b"" for i in range(64 - len(files))})
        assert len(files) == 64
    else:
        files["main.csx"] += b" " * (4 * 1024 * 1024 - sum(map(len, files.values())))
        assert sum(map(len, files.values())) == 4 * 1024 * 1024
    assert check_branch_package(files, "panel-gap-check").ok


@pytest.mark.parametrize("stage", ["load", "validate", "digest"])
def test_exceptions_fail_closed(stage, monkeypatch):
    files = package()
    runner = checker._load_runner()
    def explode(*args):
        raise RuntimeError("V7: synthetic exception")
    if stage == "load":
        monkeypatch.setattr(checker, "_load_runner", explode)
    else:
        monkeypatch.setattr(runner, "validate" if stage == "validate" else "package_digest", explode)
        monkeypatch.setattr(checker, "_load_runner", lambda: runner)
    result = check_branch_package(files, "panel-gap-check")
    assert not result.ok
    assert result.failures[-1]["rule"] == "internal"


def test_unprefixed_runner_failure_is_internal(monkeypatch):
    def validate(files, name, failures):
        failures.append("schema unavailable")
        return {}
    monkeypatch.setattr(checker, "_load_runner", lambda: SimpleNamespace(
        validate=validate, package_digest=lambda files: "digest"))
    result = check_branch_package(package(), "panel-gap-check")
    assert not result.ok
    assert result.failures == [{"rule": "internal", "path": "", "message": "schema unavailable"}]


def test_check_is_read_only_and_deterministic(monkeypatch):
    files = package()
    original = dict(files)
    original_path = list(sys.path)
    original_modules = set(sys.modules)
    def forbidden(*args, **kwargs):
        pytest.fail("validation attempted a write or network connection")
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    first = check_branch_package(files, "panel-gap-check")
    assert first.ok
    assert check_branch_package(files, "panel-gap-check") == first
    assert files == original and sys.path == original_path
    assert not any(name.startswith("_branch_tools_cad_tool_package_runner")
                   for name in set(sys.modules) - original_modules)
