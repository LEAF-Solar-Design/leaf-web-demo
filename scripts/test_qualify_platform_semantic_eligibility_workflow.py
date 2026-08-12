from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "qualify-platform-semantic-eligibility.yml"
SCRIPT = ROOT / "scripts" / "platform_semantic_qualification.py"


def workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def workflow_document() -> dict:
    # BaseLoader preserves the literal `on` key instead of applying YAML 1.1 booleans.
    return yaml.load(workflow_text(), Loader=yaml.BaseLoader)


def test_workflow_is_manual_dormant_and_has_no_live_mutation_surface():
    document = workflow_document()

    assert set(document["on"]) == {"workflow_dispatch"}
    inputs = document["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"shadow_enabled"}
    assert inputs["shadow_enabled"]["default"] == "false"
    assert inputs["shadow_enabled"]["type"] == "boolean"
    assert document["permissions"] == {"contents": "read"}

    lowered = workflow_text().casefold()
    forbidden = (
        "aws-actions/",
        "terraform apply",
        "ecs update-service",
        "workflow_run:",
        "schedule:",
        "pull_request:",
        "push:",
        "upload-artifact",
        "repository_dispatch",
        "gh workflow run",
    )
    assert all(token not in lowered for token in forbidden)
    assert "workflow-preflight" in lowered
    assert "status\" -eq 78" in lowered


def test_workflow_preflight_fails_closed_without_publishing_receipt(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "workflow-preflight", "--shadow-enabled", "false"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 78
    value = json.loads(result.stdout)
    assert value["state"] == "UNCONFIGURED"
    assert value["shadow_enabled"] is False
    assert value["producer_signing_configured"] is False
    assert value["terminal_p6_verifier_configured"] is False
    assert value["deployment_effect"] is False
    assert value["receipt_published"] is False
    assert list(tmp_path.iterdir()) == []


def test_workflow_preflight_remains_unconfigured_when_shadow_input_is_true(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "workflow-preflight", "--shadow-enabled", "true"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 78
    value = json.loads(result.stdout)
    assert value["state"] == "UNCONFIGURED"
    assert value["shadow_enabled"] is True
    assert value["deployment_effect"] is False
    assert value["receipt_published"] is False


def test_workflow_run_blocks_are_valid_bash():
    document = workflow_document()
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    blocks = []
    for job in document["jobs"].values():
        for step in job.get("steps", []):
            if "run" in step:
                blocks.append(step["run"])
    assert len(blocks) == 2
    for block in blocks:
        result = subprocess.run([str(bash), "-n"], input=block.encode("utf-8"), capture_output=True)
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
