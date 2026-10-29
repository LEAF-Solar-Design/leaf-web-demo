"""Guard the UI import's missing-source-kind 422 without DATABASE_URL or a DB connection."""
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

import leaf_platform.api as platform_api


def test_import_body_without_source_kind_is_refused():
    with pytest.raises(ValidationError) as exc_info:
        platform_api.ImportDrawingVersionBody.model_validate({
            "source": {"drawing_id": str(uuid.uuid4()), "version": 1},
            "name": "site.dxf",
        })
    assert any(
        error["loc"] == ("source", "kind") and error["type"] == "missing"
        for error in exc_info.value.errors()
    )


def test_import_body_with_account_upload_kind_validates():
    body = {
        "source": {
            "kind": "account_upload",
            "drawing_id": str(uuid.uuid4()),
            "version": 1,
        },
        "name": "site.dxf",
    }
    validated = platform_api.ImportDrawingVersionBody.model_validate(body)
    assert validated.source.kind == "account_upload"

    body["source"]["kind"] = "guest_upload"
    with pytest.raises(ValidationError):
        platform_api.ImportDrawingVersionBody.model_validate(body)


def test_web_client_sends_the_account_upload_kind():
    source = (Path(__file__).resolve().parents[2] / "web/src/api.js").read_text(
        encoding="utf-8")
    function = source.split(
        "export async function importUploadedDrawingVersion", 1)[1].split(
        "\nexport ", 1)[0]
    assert "source: { kind: 'account_upload', drawing_id: drawingId, version }" in function
