from __future__ import annotations

from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest
import yaml

from platform_release_admission import (
    evaluate_release_admission,
    workflow_preflight,
)
from platform_semantic_eligibility import ContractError, sha256_digest
from platform_source_impact import (
    FIXTURE_NOW,
    _fixture_token_payload,
    _fixture_trusted_roots,
    _seal_token,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contract" / "platform-release-admission.v1.schema.json"
WORKFLOW = ROOT / ".github" / "workflows" / "qualify-platform-release-admission.yml"
SCRIPT = ROOT / "scripts" / "platform_release_admission.py"
BASE_TREE = "698efba6b35b2a08eece8c548ba77f71d8859c21"
SOURCE_TREE = "0a2eaab98582526b8f9579f443b6965a945270ec"


def jsonschema_module():
    loaded = sys.modules.get("platform")
    if loaded is None or not hasattr(loaded, "python_implementation"):
        path = Path(sysconfig.get_path("stdlib")) / "platform.py"
        spec = importlib.util.spec_from_file_location("platform", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["platform"] = module
        spec.loader.exec_module(module)
    return importlib.import_module("jsonschema")


def evidence() -> dict:
    return {
        "schema": "leaf.platform-release-admission-input.v3",
        "selector": "UNCONFIGURED",
        "producer_token": _fixture_token_payload(),
        "candidate": {
            "relay_base_tree": BASE_TREE,
            "deferred": False,
            "queue_age_seconds": 30,
            "queue_count": 1,
            "urgent": False,
        },
        "settlement": {
            "active": False,
            "census_started": False,
            "terminal_receipt_published": True,
            "release_ready": False,
            "identity_restamp_active": False,
            "active_writers": 0,
            "open_markers": 0,
            "census_head": SOURCE_TREE,
            "source_head": SOURCE_TREE,
            "prior_train_digest": sha256_digest("prior-train"),
        },
        "limits": {
            "max_queue_age_seconds": 3600,
            "max_queue_count": 8,
        },
        "urgent_authority": None,
    }


def evaluate(value: dict) -> dict:
    return evaluate_release_admission(
        value,
        trusted_roots=_fixture_trusted_roots(),
        now_epoch=FIXTURE_NOW,
        fixture_enabled=True,
    )


def test_open_window_admits_after_verifying_full_token():
    result = evaluate(evidence())
    token = _fixture_token_payload()

    assert result["decision"] == "admit"
    assert result["reason_code"] == "admission_window_open"
    assert result["producer_token_digest"] == token["content_digest"]
    assert result["release_scope_digest"] == token["release_scope_digest"]
    assert result["writer_acquisition_authorized"] is False


def test_pending_terminal_receipt_coalesces_exact_nil_impact():
    value = evidence()
    value["settlement"].update(
        active=True,
        census_started=True,
        terminal_receipt_published=False,
    )
    result = evaluate(value)
    assert result["decision"] == "coalesce"
    assert result["reason_code"] == "nil_impact_held_during_settlement"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda value: value["settlement"].update(
                identity_restamp_active=True
            ),
            "settlement_occupied",
        ),
        (
            lambda value: value["settlement"].update(active_writers=1),
            "settlement_occupied",
        ),
        (
            lambda value: value["settlement"].update(open_markers=1),
            "settlement_occupied",
        ),
        (
            lambda value: value["settlement"].update(census_head="c" * 40),
            "classification_or_census_stale",
        ),
        (
            lambda value: value["candidate"].update(queue_age_seconds=3601),
            "queue_expired_reclassify",
        ),
        (
            lambda value: value["candidate"].update(queue_count=9),
            "queue_expired_reclassify",
        ),
    ],
)
def test_settlement_and_queue_drift_fail_closed(mutation, reason: str):
    value = evidence()
    mutation(value)
    result = evaluate(value)
    assert result["decision"] == "hold"
    assert result["reason_code"] == reason


def test_urgent_path_recomputes_exact_token_relations():
    value = evidence()
    token = value["producer_token"]
    value["candidate"]["urgent"] = True
    value["settlement"].update(
        active=True,
        census_started=True,
        terminal_receipt_published=False,
    )
    value["urgent_authority"] = {
        "approval_scope_digest": token["terminal"]["approval_scope_digest"],
        "displaced_train_digest": value["settlement"][
            "prior_train_digest"
        ],
        "rollback_digest": token["terminal"]["rollback_digest"],
        "release_lineage_digest": token["terminal"][
            "release_lineage_digest"
        ],
    }
    assert evaluate(value)["reason_code"] == "urgent_authority_exact"

    value["urgent_authority"]["rollback_digest"] = sha256_digest("wrong")
    assert evaluate(value)["decision"] == "coalesce"


def test_fabricated_rebound_or_digest_only_evidence_never_admits():
    value = evidence()
    forged = deepcopy(value["producer_token"])
    forged["producer"]["run_id"] += 1
    value["producer_token"] = _seal_token(forged)
    with pytest.raises(ContractError, match="PRODUCER_TOKEN_UNTRUSTED"):
        evaluate(value)

    digest_only = evidence()
    digest_only["producer_token"] = _fixture_token_payload()["content_digest"]
    with pytest.raises(ContractError, match="PRODUCER_TOKEN_INVALID"):
        evaluate(digest_only)


def test_default_unconfigured_and_output_schema():
    with pytest.raises(ContractError, match="UNCONFIGURED"):
        evaluate_release_admission(
            evidence(),
            trusted_roots=_fixture_trusted_roots(),
            now_epoch=FIXTURE_NOW,
        )
    result = evaluate(evidence())
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema = jsonschema_module()
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(result)


def test_manual_workflow_has_no_live_or_publication_surface(tmp_path: Path):
    text = WORKFLOW.read_text(encoding="utf-8")
    document = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(document["on"]) == {"workflow_dispatch"}
    assert document["permissions"] == {"contents": "read"}
    assert (
        document["on"]["workflow_dispatch"]["inputs"]["shadow_enabled"][
            "default"
        ]
        == "false"
    )
    lowered = text.casefold()
    for token in (
        "aws-actions/",
        "terraform apply",
        "workflow_run:",
        "schedule:",
        "push:",
        "pull_request:",
        "repository_dispatch",
        "upload-artifact",
        "gh workflow run",
    ):
        assert token not in lowered
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "workflow-preflight",
            "--shadow-enabled",
            "false",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 78
    assert json.loads(result.stdout) == workflow_preflight(shadow_enabled=False)
    assert list(tmp_path.iterdir()) == []


def test_workflow_run_blocks_are_valid_bash():
    document = yaml.load(
        WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    blocks = [
        step["run"]
        for job in document["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step
    ]
    assert len(blocks) == 2
    for block in blocks:
        result = subprocess.run(
            [str(bash), "-n"], input=block.encode(), capture_output=True
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
