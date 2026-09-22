import importlib.util
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SOURCE = "0123456789abcdef0123456789abcdef01234567"
ENTRY = "index-abc.js"
IMPACT = {
    "change_id": "deploy-web-" + SOURCE[:12],
    "receipt": "deploy-web.impact.json",
    "verdict": "pass",
    "unresolved": 0,
}


@pytest.fixture
def deploy_web():
    spec = importlib.util.spec_from_file_location(
        "deploy_web", REPO / "scripts" / "deploy-web.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(deploy_web, monkeypatch, tmp_path):
    monkeypatch.setattr(deploy_web, "build", lambda: None)
    monkeypatch.setattr(deploy_web, "preflight", lambda: ENTRY)
    monkeypatch.setattr(deploy_web, "deploy", lambda preview: None)
    monkeypatch.setattr(
        deploy_web, "fetch",
        lambda url: (200, ENTRY if url == deploy_web.DOMAIN + "/" else ""),
    )
    monkeypatch.setattr(deploy_web, "git_head", lambda: SOURCE)
    monkeypatch.setattr(deploy_web, "impact_assessment", lambda directory, source: IMPACT)
    monkeypatch.setattr(
        deploy_web.sys, "argv",
        ["deploy-web.py", "--receipt-dir", str(tmp_path)],
    )
    return deploy_web


def read_receipt(tmp_path):
    paths = list(tmp_path.glob("deploy-web-*.json"))
    assert len(paths) == 1
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


def test_production_receipt(prepared, tmp_path, capsys):
    prepared.main()

    path, receipt = read_receipt(tmp_path)
    assert receipt["schema"] == "leaf.deploy-web.v1"
    assert receipt["target"] == "production"
    assert receipt["outcome"] == "ready"
    assert receipt["domain"] == prepared.DOMAIN
    assert receipt["entry"] == ENTRY
    assert receipt["routes"] == [
        {"route": route, "status": 200}
        for route in ["/", "/app", "/try", "/sheets", "/sheets/01"]
    ]
    assert receipt["source"] == SOURCE
    assert receipt["impact"] == IMPACT
    output = capsys.readouterr().out
    assert f"RECEIPT {path}" in output
    assert "READY: deploy is live and every route verified" in output


def test_not_ready_receipt(prepared, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        prepared, "fetch",
        lambda url: (500, "") if url == prepared.DOMAIN + "/sheets" else (200, ENTRY),
    )

    with pytest.raises(SystemExit) as error:
        prepared.main()

    assert error.value.code == 1
    _, receipt = read_receipt(tmp_path)
    assert receipt["outcome"] == "not-ready"
    assert {"route": "/sheets", "status": 500} in receipt["routes"]
    assert "NOT-READY" in capsys.readouterr().out


def test_preview_receipt(prepared, monkeypatch, tmp_path):
    monkeypatch.setattr(prepared.sys, "argv", prepared.sys.argv + ["--preview"])
    prepared.main()

    _, receipt = read_receipt(tmp_path)
    assert receipt["target"] == "preview"
    assert receipt["outcome"] == "ready"
    assert receipt["routes"] == []


def test_dry_run_writes_no_receipt(prepared, monkeypatch, tmp_path):
    monkeypatch.setattr(prepared.sys, "argv", prepared.sys.argv + ["--dry-run"])
    prepared.main()

    assert list(tmp_path.iterdir()) == []


def test_missing_impact_checker(deploy_web, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(deploy_web, "REPO", tmp_path)

    assert deploy_web.impact_assessment(tmp_path, SOURCE) == {"skipped": "checker-missing"}
    assert capsys.readouterr().out == "impact: skipped (checker-missing)\n"
