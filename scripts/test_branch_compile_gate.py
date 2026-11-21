"""Hermetic checks on the inputs and distribution scope of the Branch compiler."""

import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
COMPILE = ROOT / "harness" / "branch-compile"
VENDOR = COMPILE / "vendor"
DLLS = ("accoremgd", "acdbmgd", "acmgd")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_vendored_sources_match_pin_exactly():
    pin = read_json(COMPILE / "BRANCH-PIN.json")
    assert pin["contract"] == "leaf.branch-compile-pin.v1"
    assert {
        path.relative_to(VENDOR).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in VENDOR.rglob("*") if path.is_file()
    } == pin["files"]


def test_only_three_locked_reference_assemblies_ship():
    records = read_json(VENDOR / "tools/ci/acad-refs.lock.json")["years"]["2025"]
    refs = {
        f"{record['package']}/{record['version']}/{record['path']}": record["dllSha256"]
        for record in (records[key] for key in DLLS)
    }
    docker = (ROOT / "deploy/Dockerfile.harness").read_text(encoding="utf-8")
    build, runtime = docker.split("FROM node:22-slim AS harness")
    hashes = re.findall(r"'([0-9a-f]{64})  ([^']+\.dll)'", build)
    assert {path: digest for digest, path in hashes} == refs
    install_refs = re.search(r"for ref in (.*?); do", build, re.S)[1]
    assert sorted(install_refs.replace("\\", "").split()) == sorted(refs)
    assert 'install -D -m 0444 "$ref" "/out/refs/$ref"' in build
    assert "sha256sum -c -" in build
    assert re.findall(r"^COPY --from=branch-compile (.*)$", runtime, re.M) == [
        "/out/bin /opt/branch-compile/bin", "/out/refs /opt/branch-compile/refs",
    ]
    assert ".nupkg" not in docker.lower()
    assert not re.search(r"^COPY .*?(\.nuget|harness/branch-compile)", runtime, re.M)
    for ref in refs:
        assert f"test -r /opt/branch-compile/refs/{ref}" in runtime.split("USER 10002:10002")[1]


def test_autocad_packages_are_private_compile_references():
    project = ET.parse(COMPILE / "ToolCheckPortable.csproj")
    refs = {ref.attrib["Include"]: ref.attrib for ref in project.iter("PackageReference")
            if ref.attrib["Include"].startswith("autocad.")}
    assert set(refs) == {"autocad.net", "autocad.net.core", "autocad.net.model"}
    for ref in refs.values():
        assert "runtime" in ref["ExcludeAssets"].lower().split(";")
        assert ref["PrivateAssets"] == "all"


def test_parity_capture_set_and_schema():
    parity = COMPILE / "parity"
    names = {"good", "typo-bycli", "html-escaped-ops", "allowlist-violation"}
    assert {p.stem for p in parity.glob("*.zip")} == names
    assert {p.stem for p in (parity / "expected").glob("*.json")} == names
    for name in names:
        expected = read_json(parity / "expected" / f"{name}.json")
        assert set(expected) == {"exit_code", "stdout", "source"}
        assert expected["exit_code"] == (0 if name == "good" else 1)
        assert expected["stdout"]["framework"] == "<framework>"


def test_readme_documents_distribution_scope():
    readme = (COMPILE / "README.md").read_text(encoding="utf-8")
    for text in ("AcCoreMgd.dll", "AcDbMgd.dll", "AcMgd.dll", "staging harness image only"):
        assert text in readme
