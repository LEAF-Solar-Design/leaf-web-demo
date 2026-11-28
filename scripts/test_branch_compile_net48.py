"""Compile real packages against the locked AutoCAD 2023 and 2025 references."""

import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPILE = ROOT / "harness" / "branch-compile"
DOTNET = shutil.which("dotnet")
pytestmark = pytest.mark.skipif(DOTNET is None, reason="dotnet is not on PATH")


def run(args, env=None):
    return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)


@pytest.fixture(scope="module")
def checkers(tmp_path_factory):
    output = tmp_path_factory.mktemp("branch-net48")
    if sys.platform == "win32":
        rid = "win-x64"
    elif sys.platform == "darwin":
        rid = "osx-arm64" if platform.machine().lower() == "arm64" else "osx-x64"
    else:
        rid = "linux-x64"
    projects = {
        "globals": COMPILE / "net48/globals/Net48Globals.csproj",
        "check": COMPILE / "net48/check/ToolCheckNet48.csproj",
        "net8": COMPILE / "ToolCheckPortable.csproj",
    }
    for name, project in projects.items():
        # RID overrides must never rewrite the committed Linux lock files.
        lock = output / f"{name}.packages.lock.json"
        shutil.copyfile(project.parent / "packages.lock.json", lock)
        args = [DOTNET, "build", str(project), "-c", "Release", "-o", str(output / name),
                f"-p:NuGetLockFilePath={lock}",
                f"-p:BaseIntermediateOutputPath={(output / (name + '-obj')).as_posix()}/"]
        if name != "globals":
            args += ["-r", rid, "--self-contained", "false"]
            if rid != "linux-x64":
                args += ["-p:RestoreLockedMode=false"]
        built = run(args)
        assert built.returncode == 0, built.stdout + built.stderr
    env = os.environ.copy()
    env["LEAF_BRANCH_COMPILE_NET48_GLOBALS"] = str(output / "globals/LeafSolarDesign.ScriptHost.dll")
    return output, env


def check(checkers, package, net8=False):
    output, env = checkers
    dll = output / ("net8/tool-check-portable.dll" if net8 else "check/tool-check-net48.dll")
    result = run([DOTNET, str(dll), "--package", str(package)], env=env)
    assert result.stdout.strip(), result.stderr
    report = json.loads(result.stdout)
    assert report["schema"] == "leaf.tool-check.v1"
    if not net8:
        assert report["framework"].startswith(".NETFramework,Version=v4.8 reference check on ")
    return result.returncode, report


def test_net48_good_package(checkers):
    code, report = check(checkers, COMPILE / "parity/good.zip")
    assert (code, report["ok"], report["decision"]) == (0, True, "accept")
    assert report["diagnostics"] == []


def test_net48_typo_uses_vendored_diagnostic_format(checkers):
    code, report = check(checkers, COMPILE / "parity/typo-bycli.zip")
    assert (code, report["ok"], report["decision"]) == (1, False, "compile_failed")
    assert any(re.match(r"^main\.csx\(\d+,\d+\): CS0117: ", d) for d in report["diagnostics"])


def test_net48_scanner_refuses_denied_apis(checkers):
    code, report = check(checkers, COMPILE / "parity/allowlist-violation.zip")
    assert (code, report["ok"], report["decision"]) == (1, False, "compile_failed")
    assert any(re.match(r"^main\.csx\(\d+,\d+\): scan: ", d) for d in report["diagnostics"])


def test_net8_only_member_is_refused_by_net48(checkers):
    package = COMPILE / "parity-net48/net8-only.zip"
    code, report = check(checkers, package)
    assert (code, report["ok"], report["decision"]) == (1, False, "compile_failed")
    assert any(re.match(r"^main\.csx\(\d+,\d+\): CS1061: ", d) for d in report["diagnostics"])
    code, report = check(checkers, package, net8=True)
    assert (code, report["ok"], report["decision"]) == (0, True, "accept")


def test_net48_html_escaped_source_is_refused(checkers):
    code, report = check(checkers, COMPILE / "parity/html-escaped-ops.zip")
    assert (code, report["decision"]) == (1, "compile_failed")
