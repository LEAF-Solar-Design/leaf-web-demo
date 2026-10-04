"""Receipt parity and supported guided-flow source configuration contracts."""

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
spec = importlib.util.spec_from_file_location("parity_test_builders", SCRIPT_DIR / "test_solar_parity_status.py")
builders = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builders)
status = builders.status


def docker(flags=(1, 1, 1), rail="forward"):
    lines = ["FROM node:22-slim AS build"]
    for name, value in zip(status.UI_FLAGS, flags):
        if name != "VITE_SOLAR_FLOW_RAIL" or rail not in ("arg-missing", "literal-env"):
            lines.append(f"ARG {name}={value}")
    for name in status.UI_FLAGS:
        if name == "VITE_SOLAR_FLOW_RAIL":
            if rail in ("env-missing", "other-stage", "comment"):
                continue
            value = "1" if rail == "literal-env" else "true" if rail == "not-one" else "${" + name + "}"
        else:
            value = "${" + name + "}"
        lines.append(f"ENV {name}={value}")
    if rail == "comment":
        lines.append("# ENV VITE_SOLAR_FLOW_RAIL=1")
    lines.append("RUN npm run build")
    if rail == "other-stage":
        lines.extend(["FROM nginx AS serve", "ENV VITE_SOLAR_FLOW_RAIL=1"])
    return "\n".join(lines) + "\n"


def flow(stage="flowStage('tools', 'Tools', ['solar-example'])", rooftop=False):
    entry = "flowEntry('rooftop', 'Rooftop', 'production', null)" if rooftop else (
        "flowEntry('example', 'Example', 'production', [" + stage + "])")
    text = "export const MAX_FLOW_STEPS = 64\nexport const SOLAR_FLOWS = Object.freeze([" + entry + "])\n"
    if rooftop:
        shipped = (ROOT / "web/src/solar/solarFlowModel.js").read_text(encoding="utf-8")
        text += shipped[shipped.index("export function solarFlowSelect("):]
    return text


def declaration(name="solar-example", ledger=None, wave=3):
    return {"schema": "leaf.solar-tool.v1", "name": name,
            "ledger": ["example"] if ledger is None else ledger, "wave": wave,
            "order": 20, "family": "stringing", "entitlement": "run_write",
            "interaction": {"mode": "form"}}


def write_sources(root, docker_text=None, flow_text=None, declarations=None):
    files = {"deploy/Dockerfile.web": docker() if docker_text is None else docker_text,
             "web/src/solar/solarFlowModel.js": flow() if flow_text is None else flow_text}
    for index, doc in enumerate([declaration()] if declarations is None else declarations):
        files[f"server/solar_tools/solar_fixture_{index}.json"] = json.dumps(doc)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return status.read_ui_sources(root)


def evaluate(root, sources, rows=None, require="all-production"):
    rows = [builders.row("EXAMPLE", capability="example", wave=1)] if rows is None else rows
    for capability in {row["capability"] for row in rows}:
        builders.write_receipt(root, capability, builders.receipt(capability))
    return status.evaluate(len(rows), rows, root / "receipts", require, root, ui_sources=sources)


@pytest.mark.parametrize("flags,rail,blocked", [
    ((1, 1, 1), "forward", []),
    ((0, 1, 1), "forward", ["VITE_CAD_EDIT"]),
    ((1, 0, 1), "forward", ["VITE_SOLAR_FLOW_RAIL"]),
    ((1, 1, 0), "forward", ["VITE_SOLAR_SETTINGS_FORM"]),
    ((1, 1, 1), "arg-missing", ["VITE_SOLAR_FLOW_RAIL"]),
    ((1, 1, 1), "env-missing", ["VITE_SOLAR_FLOW_RAIL"]),
    ((1, 1, 1), "not-one", ["VITE_SOLAR_FLOW_RAIL"]),
    ((1, 1, 1), "other-stage", ["VITE_SOLAR_FLOW_RAIL"]),
    ((1, 1, 1), "comment", ["VITE_SOLAR_FLOW_RAIL"]),
    ((1, 1, 1), "literal-env", []),
], ids=["W22C1-all-on", "W22C1-cad-off", "W22C1-rail-off", "W22C1-settings-off",
        "W22C1-arg-missing", "W22C1-env-missing", "W22C1-not-one", "W22C1-other-stage",
        "W22C1-comment", "W22C1-literal-env"])
def test_W22C1_flags(tmp_path, flags, rail, blocked):
    sources = write_sources(tmp_path, docker_text=docker(flags, rail))
    result = evaluate(tmp_path, sources)
    assert result["reachability"] == [{"capability": "example", "state": "unreachable" if blocked else "reachable",
                                       "tools": ["solar-example"], "blocked_flags": blocked}]
    assert builders.finding_codes(result) == (["UI_UNREACHABLE"] if blocked else [])
    assert result["ok"] is (not blocked)
    assert result["counts"]["capabilities_passing"] == 1


@pytest.mark.parametrize("case", ["explicit", "rooftop", "conductors", "panel-only", "no-ledger-map", "shared", "empty-map"],
                         ids=["W22C1-" + case for case in
                              ("explicit", "rooftop", "conductors", "panel-only", "no-ledger-map", "shared", "empty-map")])
def test_W22C1_binding(tmp_path, case):
    docs = [declaration()]
    text = flow()
    expected_tools = ["solar-example"]
    if case in ("rooftop", "conductors"):
        docs = [declaration(wave=1)] if case == "rooftop" else [declaration(name="solar-string-conductors", wave=2)]
        text = flow(rooftop=True)
        expected_tools = [docs[0]["name"]]
        # Any semantic change to the supported Rooftop rule must fail closed.
        with pytest.raises(status.InputError):
            status.read_flow_bindings(text.replace("result.view.wave !== 1", "result.view.wave !== 2"))
    elif case == "panel-only":
        text = flow("panelCatalogStage('tools', 'Tools', ['physical-read'], ['solar-example'])")
        expected_tools = []
    elif case == "no-ledger-map":
        docs = [declaration(ledger=["other"])]
        expected_tools = []
    elif case == "shared":
        docs.append(declaration(name="solar-another"))
        text = flow("flowStage('tools', 'Tools', ['solar-example', 'solar-another'])")
        expected_tools = ["solar-another", "solar-example"]
    elif case == "empty-map":
        docs = [declaration(ledger=[])]
        expected_tools = []
    sources = write_sources(tmp_path, flow_text=text, declarations=docs)
    result = evaluate(tmp_path, sources)
    assert result["reachability"] == [{"capability": "example", "state": "reachable" if expected_tools else "unverified",
                                       "tools": expected_tools, "blocked_flags": []}]
    assert result["findings"] == []
    if case == "empty-map":
        assert status.capability_reachability({}, sources) == []


@pytest.mark.parametrize("case", ["missing-source", "oversized-boundary", "invalid-json", "expression"],
                         ids=["W22C1-" + case for case in ("missing-source", "oversized-boundary", "invalid-json", "expression")])
def test_W22C1_input(tmp_path, monkeypatch, capsys, case):
    write_sources(tmp_path)
    if case == "missing-source":
        (tmp_path / "deploy/Dockerfile.web").unlink()
    elif case == "oversized-boundary":
        valid = docker().encode("utf-8")
        padded = valid + b"#" + b"x" * (65537 - len(valid) - 2) + b"\n"
        assert len(padded) == 65537
        (tmp_path / "deploy/Dockerfile.web").write_bytes(padded)
    elif case == "invalid-json":
        (tmp_path / "server/solar_tools/solar_fixture_0.json").write_text("{", encoding="utf-8")
    else:
        (tmp_path / "web/src/solar/solarFlowModel.js").write_text(
            flow("flowStage('tools', 'Tools', ['solar-example', extra])"), encoding="utf-8")
    ledger = builders.write_ledger(tmp_path, [builders.row("EXAMPLE", capability="example")])
    monkeypatch.setattr(status, "repo_root", lambda: tmp_path)
    assert status.main(["--ledger", str(ledger), "--receipts", str(tmp_path / "receipts"), "--json"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "solar-parity-status:" in output.err
    if case == "oversized-boundary":
        assert "exceeds 65536 bytes" in output.err
        boundary = padded[:-2] + b"\n"
        assert len(boundary) == 65536
        (tmp_path / "deploy/Dockerfile.web").write_bytes(boundary)
        builders.write_receipt(tmp_path, "example", builders.receipt("example"))
        assert status.main(["--ledger", str(ledger), "--receipts", str(tmp_path / "receipts"), "--json"]) == 0
        output = capsys.readouterr()
        assert output.err == ""
        assert json.loads(output.out)["counts"]["capabilities_reachable"] == 1


@pytest.mark.parametrize("case", ["escaped", "global-arg", "global-no-import", "inherit"],
                         ids=["W22C1-" + case for case in ("escaped", "global-arg", "global-no-import", "inherit")])
def test_W22C1_docker_idioms(tmp_path, monkeypatch, capsys, case):
    write_sources(tmp_path)
    if case == "escaped":
        text = ("FROM node:22-slim AS build\nARG A=1\n"
                "ENV VITE_CAD_EDIT=1 VITE_SOLAR_SETTINGS_FORM=1\n"
                "ENV VITE_SOLAR_FLOW_RAIL=\\${A}\nRUN npm run build\n")
        message = "unsupported escape in flag assignment"
    elif case == "inherit":
        text = "FROM node:22-slim AS flags\nENV VITE_SOLAR_FLOW_RAIL=1\nFROM flags AS build\nRUN npm run build\n"
        message = "stage inheritance not supported"
    else:
        imported = "ARG VITE_SOLAR_FLOW_RAIL\n" if case == "global-arg" else ""
        text = ("ARG VITE_SOLAR_FLOW_RAIL=1\nFROM node:22-slim AS build\n" + imported +
                "ENV VITE_CAD_EDIT=1 VITE_SOLAR_SETTINGS_FORM=1\n"
                "ENV VITE_SOLAR_FLOW_RAIL=${VITE_SOLAR_FLOW_RAIL}\nRUN npm run build\n")
        expected = case == "global-arg"
        assert status.read_baked_flags(text)["VITE_SOLAR_FLOW_RAIL"] is expected
        if expected:
            assert status.read_baked_flags(text.replace("ARG VITE_SOLAR_FLOW_RAIL=1", "ARG VITE_SOLAR_FLOW_RAIL"))[
                "VITE_SOLAR_FLOW_RAIL"] is False
    (tmp_path / "deploy/Dockerfile.web").write_text(text, encoding="utf-8")
    ledger = builders.write_ledger(tmp_path, [builders.row("EXAMPLE", capability="example")])
    builders.write_receipt(tmp_path, "example", builders.receipt("example"))
    monkeypatch.setattr(status, "repo_root", lambda: tmp_path)
    code = status.main(["--ledger", str(ledger), "--receipts", str(tmp_path / "receipts"), "--json"])
    output = capsys.readouterr()
    if case in ("escaped", "inherit"):
        assert code == 2
        assert output.out == ""
        assert message in output.err
    else:
        assert code == (0 if expected else 1)
        assert output.err == ""
        result = json.loads(output.out)
        assert result["counts"]["capabilities_reachable"] == int(expected)
        assert result["counts"]["capabilities_unreachable"] == int(not expected)


@pytest.mark.parametrize("limit,count", [(64, 65), (1, 2)],
                         ids=["W22C1-limit-65", "W22C1-limit-one"])
def test_W22C1_rooftop_limit(tmp_path, monkeypatch, capsys, limit, count):
    text = flow(rooftop=True).replace("MAX_FLOW_STEPS = 64", f"MAX_FLOW_STEPS = {limit}")
    assert status.read_flow_bindings(text)["max_flow_steps"] == limit
    for invalid in (text.replace(f"export const MAX_FLOW_STEPS = {limit}\n", ""),
                    text + f"\nexport const MAX_FLOW_STEPS = {limit}\n",
                    text.replace(f"MAX_FLOW_STEPS = {limit}", "MAX_FLOW_STEPS = 1 + 1")):
        with pytest.raises(status.InputError):
            status.read_flow_bindings(invalid)
    write_sources(tmp_path, flow_text=text,
                  declarations=[declaration(name=f"solar-limit-{i}", wave=1) for i in range(limit)])
    extra = tmp_path / f"server/solar_tools/solar_fixture_{count - 1}.json"
    extra.write_text(json.dumps(declaration(name=f"solar-limit-{count - 1}", wave=1)), encoding="utf-8")
    ledger = builders.write_ledger(tmp_path, [builders.row("EXAMPLE", capability="example")])
    monkeypatch.setattr(status, "repo_root", lambda: tmp_path)
    assert status.main(["--ledger", str(ledger), "--json"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "rooftop steps exceed the browser flow limit" in output.err


def test_W22C1_shipped_counts():
    assert status.read_ui_sources(ROOT)["bindings"]["max_flow_steps"] == 64
    expected, rows = status.parse_ledger(ROOT / "docs/parity/solar-ledger.json")
    result = status.evaluate(expected, rows, ROOT / "docs/parity/receipts", "all-production", ROOT)
    assert [result["counts"]["capabilities_" + state] for state in
            ("reachable", "unreachable", "unverified")] == [22, 0, 106]
    assert [item["capability"] for item in result["reachability"] if item["state"] == "reachable"] == [
        "cable-export", "homeruns", "import-solaredge-pdf", "insert-schedules", "inverter-add",
        "nec-ac-voltage-drop", "nec-ampacity-correction", "nec-conduit-fill", "nec-feeder-ocpd-sizing",
        "panel-group-create", "route-l2-feeders", "solve", "string-data", "string-delete", "string-flip",
        "string-midpoint-connection", "string-multi-add", "string-rebuild", "string-single-add",
        "string-sizer", "string-swap", "trackers-to-panelgroups",
    ]
    assert result["ok"] is True
    assert result["counts"]["capabilities_passing"] == 128
    assert result["counts"]["duty_rows_passing"] == 151


def test_W22C1_receipt_independent(tmp_path, monkeypatch, capsys):
    write_sources(tmp_path, docker_text=docker((1, 0, 1)))
    monkeypatch.setenv("VITE_SOLAR_FLOW_RAIL", "1")
    monkeypatch.setattr(status, "repo_root", lambda: tmp_path)
    ledger = builders.write_ledger(tmp_path, [builders.row("EXAMPLE", capability="example")])
    builders.write_receipt(tmp_path, "example", builders.receipt("example"))
    assert status.main(["--ledger", str(ledger), "--receipts", str(tmp_path / "receipts"),
                        "--repo-root", str(tmp_path / "divergence-root"), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["counts"]["capabilities_passing"] == 1
    assert result["counts"]["capabilities_unreachable"] == 1
    assert builders.finding_codes(result) == ["UI_UNREACHABLE"]


def test_W22C1_report_bounded(tmp_path):
    result = evaluate(tmp_path, write_sources(tmp_path))
    result["reachability"] = [{"capability": f"example-{i}", "state": "unverified", "tools": [], "blocked_flags": []}
                              for i in range(300)]
    result["findings"] = [status.finding("RECEIPT_MISSING", "missing", capability=f"example-{i}") for i in range(300)]
    result["ok"] = False
    report = status.human_report(result, "ledger.json", "receipts")
    assert len(report.splitlines()) <= 200
    assert "220 capability rows omitted" in report
    assert " more" in report
    assert report.endswith("verdict: FAIL")


def test_W22C1_scope(tmp_path):
    sources = write_sources(tmp_path, declarations=[declaration(ledger=["first", "second"])])
    rows = [builders.row("FIRST", capability="first", wave=1), builders.row("SECOND", capability="second", wave=2)]
    result = evaluate(tmp_path, sources, rows, "w1")
    assert [item["capability"] for item in result["reachability"]] == ["first"]
    assert result["counts"]["capabilities_reachable"] == 1


def test_W22C1_json_human(tmp_path, monkeypatch, capsys):
    sources = write_sources(tmp_path, declarations=[declaration(ledger=["first", "second"])])
    rows = [builders.row(name.upper(), capability=name) for name in ("third", "second", "first")]
    result = evaluate(tmp_path, sources, rows)
    # Reporting accepts all three states; a single build configuration has shared flags.
    result["reachability"][1].update(state="unreachable", blocked_flags=["VITE_SOLAR_FLOW_RAIL"])
    result["counts"].update(capabilities_reachable=1, capabilities_unreachable=1, capabilities_unverified=1)
    result["findings"] = [status.finding("UI_UNREACHABLE", "disabled guided-flow flags", capability="second")]
    result["ok"] = False
    ledger = builders.write_ledger(tmp_path, rows)
    monkeypatch.setattr(status, "evaluate", lambda *args, **kwargs: result)
    assert status.main(["--ledger", str(ledger), "--json"]) == 1
    decoded = json.loads(capsys.readouterr().out)
    assert decoded == result
    assert [item["capability"] for item in decoded["reachability"]] == ["first", "second", "third"]
    assert builders.finding_codes(decoded) == ["UI_UNREACHABLE"]
    assert status.main(["--ledger", str(ledger)]) == 1
    report = capsys.readouterr().out
    assert "reachable: 1  unreachable: 1  unverified: 1" in report
    assert "first  pass  reachable" in report
    assert "second  pass  unreachable" in report
    assert "third  pass  unverified" in report
    assert "Guided-flow reachability (source configuration)" in report
    assert "Source configuration does not prove a deployed image or runtime readiness." in report
    assert report.rstrip().endswith("verdict: FAIL")


STRICT_PREFIX = "FROM node:22-slim AS build\nENV VITE_CAD_EDIT=1 VITE_SOLAR_SETTINGS_FORM=1\n"
STRICT_CASES = {
    "quoted": ("ARG A=1\nENV VITE_SOLAR_FLOW_RAIL='${A}'\n", "unsupported quoting in flag assignment"),
    "helper-escape": ("ENV A=\\${UNSET}1\nENV VITE_SOLAR_FLOW_RAIL=${A}\n", "unsupported flag reference"),
    "legacy-helper": ("ENV A 0\nARG A=1\nENV VITE_SOLAR_FLOW_RAIL=${A}\n", "unsupported flag reference"),
    "arg-reference": ("ARG A=1\nARG VITE_SOLAR_FLOW_RAIL=${A}\nENV VITE_SOLAR_FLOW_RAIL=${VITE_SOLAR_FLOW_RAIL}\n",
                      "unsupported flag reference"),
    "env-then-arg": ("ENV VITE_SOLAR_FLOW_RAIL=0\nARG VITE_SOLAR_FLOW_RAIL=1\n"
                     "ENV VITE_SOLAR_FLOW_RAIL=${VITE_SOLAR_FLOW_RAIL}\n", "ambiguous flag assignment"),
    "twice-env": ("ENV VITE_SOLAR_FLOW_RAIL=0\nENV VITE_SOLAR_FLOW_RAIL=1\n", "ambiguous flag assignment"),
}


@pytest.mark.parametrize("case", sorted(STRICT_CASES), ids=["W22C1-strict-" + case for case in sorted(STRICT_CASES)])
def test_W22C1_strict_refusals(case):
    body, message = STRICT_CASES[case]
    with pytest.raises(status.InputError, match=message):
        status.read_baked_flags(STRICT_PREFIX + body + "RUN npm run build\n")


def test_W22C1_strict_arg_stage():
    text = ("ARG BASE=flags\nFROM node:22-slim AS flags\nENV A=0\nFROM ${BASE} AS build\nARG A=1\n"
            "ENV VITE_CAD_EDIT=1 VITE_SOLAR_SETTINGS_FORM=1\nENV VITE_SOLAR_FLOW_RAIL=1\nRUN npm run build\n")
    with pytest.raises(status.InputError, match="stage inheritance not supported"):
        status.read_baked_flags(text)


@pytest.mark.parametrize("rail, expected", [("ARG VITE_SOLAR_FLOW_RAIL=1\nENV VITE_SOLAR_FLOW_RAIL=${VITE_SOLAR_FLOW_RAIL}\n", True),
                                            ("ARG VITE_SOLAR_FLOW_RAIL=0\nENV VITE_SOLAR_FLOW_RAIL=${VITE_SOLAR_FLOW_RAIL}\n", False),
                                            ("ENV VITE_SOLAR_FLOW_RAIL=1\n", True)],
                         ids=["W22C1-strict-own-arg", "W22C1-strict-own-arg-off", "W22C1-strict-literal"])
def test_W22C1_strict_admitted(rail, expected):
    assert status.read_baked_flags(STRICT_PREFIX + rail + "RUN npm run build\n")["VITE_SOLAR_FLOW_RAIL"] is expected


def test_W22C1_strict_repo_dockerfile():
    text = (Path(__file__).resolve().parents[1] / "deploy" / "Dockerfile.web").read_text(encoding="utf-8")
    assert status.read_baked_flags(text) == {flag: True for flag in status.UI_FLAGS}
