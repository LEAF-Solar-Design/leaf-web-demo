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


def test_allowlist_copies_match_and_drop_font_path_members():
    compile_dir = VENDOR / "LeafSolarDesign.ScriptHost/Compile"
    copies = [
        (compile_dir / "api-allowlist.v2.json").read_bytes(),
        (ROOT / "harness/src/vendor/mushy-author/registry/api-allowlist.json").read_bytes(),
        (ROOT / "server/branch_tools/vendor/cad_tool_package/api-allowlist.json").read_bytes(),
    ]
    assert copies[0] == copies[1] == copies[2]
    members = json.loads(copies[0])["allowed_members"]
    text_style = "Autodesk.AutoCAD.DatabaseServices.TextStyleTableRecord."
    assert text_style + "FileName" not in members
    assert text_style + "BigFontFileName" not in members
    assert not any(member.startswith(text_style) and member.endswith("FileName")
                   for member in members)
    digest = hashlib.sha256(copies[0].replace(b"\r", b"")).hexdigest()
    assert f'Digest = "{digest}"' in (compile_dir / "ApiAllowlist.cs").read_text(encoding="utf-8")


def test_only_locked_reference_assemblies_ship():
    years = read_json(VENDOR / "tools/ci/acad-refs.lock.json")["years"]
    refs = {
        f"{record['package']}/{record['version']}/{record['path']}": record["dllSha256"]
        for year in ("2025", "2023") for record in (years[year][key] for key in DLLS)
    }
    framework = "microsoft.netframework.referenceassemblies.net48/1.0.3/build/.NETFramework/v4.8/"
    refs.update({
        framework + "mscorlib.dll": "6b35530467b914b0b195146ca1d1485ddd219afaad6461103f16f54e952f75a9",
        framework + "System.dll": "2fe343569f794f2ca92ee14a41875571a9f21bf92637b8f8ee86306534209cca",
        framework + "System.Core.dll": "94a1fc97eb0d36f13acd209bb7207da504c8be10caf7e5096b2c46e671dc6fe2",
        "newtonsoft.json/13.0.3/lib/net45/Newtonsoft.Json.dll":
            "e1e27af7b07eeedf5ce71a9255f0422816a6fc5849a483c6714e1b472044fa9d",
    })
    assert len(refs) == 10
    docker = (ROOT / "deploy/Dockerfile.harness").read_text(encoding="utf-8")
    build, runtime = docker.split("FROM node:22-slim AS harness")
    hashes = re.findall(r"'([0-9a-f]{64})  ([^']+\.dll)'", build)
    assert {path: digest for digest, path in hashes} == refs
    assert len(hashes) == len(refs)
    install_refs = re.search(r"for ref in (.*?); do", build, re.S)[1]
    assert sorted(install_refs.replace("\\", "").split()) == sorted(refs)
    assert 'install -D -m 0444 "$ref" "/out/refs/$ref"' in build
    assert "sha256sum -c -" in build
    assert re.findall(r"^COPY --from=branch-compile (.*)$", runtime, re.M) == [
        "/out/bin /opt/branch-compile/bin", "/out/refs /opt/branch-compile/refs",
        "/out/net48 /opt/branch-compile/net48",
    ]
    assert ".nupkg" not in docker.lower()
    assert not re.search(r"^COPY .*?(\.nuget|harness/branch-compile)", runtime, re.M)
    for ref in refs:
        assert f"test -r /opt/branch-compile/refs/{ref}" in runtime.split("USER 10002:10002")[1]
    guard = runtime.split("USER 10002:10002")[1]
    assert "test -x /opt/branch-compile/net48/bin/tool-check-net48" in guard
    assert "test -r /opt/branch-compile/net48/bin/net48-globals/LeafSolarDesign.ScriptHost.dll" in guard


def test_net48_checker_is_configured_in_runtime_env():
    docker = (ROOT / "deploy/Dockerfile.harness").read_text(encoding="utf-8")
    env = re.search(r"^ENV HARNESS_PORT=.*?(?=\n\n)", docker, re.M | re.S)[0]
    assert "LEAF_BRANCH_COMPILE_NET48_BIN=/opt/branch-compile/net48/bin/tool-check-net48" in env


def test_net48_projects_use_locked_compile_references():
    projects = [COMPILE / "net48/globals/Net48Globals.csproj", COMPILE / "net48/check/ToolCheckNet48.csproj"]
    for path in projects:
        assert path.is_file()
        assert (path.parent / "packages.lock.json").is_file()
        project = ET.parse(path)
        assert project.findtext(".//RestorePackagesWithLockFile") == "true"
        for ref in project.iter("PackageReference"):
            if ref.attrib["Include"].lower().startswith("autocad."):
                assert "runtime" in ref.attrib["ExcludeAssets"].lower().split(";")
                assert ref.attrib["PrivateAssets"] == "all"
    globals_project = ET.parse(projects[0])
    assert globals_project.findtext(".//TargetFramework") == "net48"
    assert globals_project.findtext(".//AssemblyName") == "LeafSolarDesign.ScriptHost"


def test_net48_image_build_restores_locked_inputs_and_checks_parity():
    build = (ROOT / "deploy/Dockerfile.harness").read_text(encoding="utf-8").split("FROM node:22-slim AS harness")[0]
    restore_run = build.split("RUN --mount=type=tmpfs,target=/root", 1)[1].split("\nRUN ", 1)[0]
    for project in ("globals/Net48Globals.csproj", "check/ToolCheckNet48.csproj"):
        assert f"dotnet restore /src/net48/{project} --locked-mode" in restore_run
    assert "dotnet build /src/net48/globals/Net48Globals.csproj -c Release --no-restore" in restore_run
    assert "dotnet publish /src/net48/check/ToolCheckNet48.csproj -c Release --no-restore -o /out/net48/bin" in restore_run
    assert "/out/net48/bin/net48-globals/LeafSolarDesign.ScriptHost.dll" in restore_run
    parity = build.split("RUN set -eu; for name in good typo-bycli html-escaped-ops allowlist-violation; do", 1)[1]
    assert 'NUGET_PACKAGES=/out/refs /out/net48/bin/tool-check-net48 --package "/src/parity/$name.zip"' in parity
    assert 'test "$rc" = 0' in parity and 'test "$rc" = 1' in parity
    assert "'\"decision\":\"accept\"'" in parity and "'\"decision\":\"compile_failed\"'" in parity
    assert "grep -q 'CS1061' /tmp/result" in parity
    for binary in ("/out/net48/bin/tool-check-net48", "/out/bin/tool-check-portable"):
        assert f"NUGET_PACKAGES=/out/refs {binary} --package /src/parity-net48/net8-only.zip" in parity


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
