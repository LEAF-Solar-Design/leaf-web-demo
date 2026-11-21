"""Version plan metadata is additive and describes the stored plan bytes."""
from __future__ import annotations

import hashlib

import pytest

import test_save_plan_version as saves
from test_version_plan_route import _get, client, save_client  # noqa: F401


def _store_plan(tmp_path, plan):
    import store

    backend = saves._seed_dwg_backed(tmp_path)
    version = store.put_drawing(
        backend, saves.TENANT, saves.DWG_DRAWING, str(tmp_path / "base.dwg"),
        parent_version=1, mutation_plan_bytes=plan)
    assert version == 2
    return hashlib.sha256((tmp_path / "base.dwg").read_bytes()).hexdigest()


@pytest.mark.parametrize("grammar,operations,kind", [
    (1, "RELAYER|A|Moved\n", "ops"),
    (2, "", "ops"),
    (3, "SETCOLOR|A|1\n", "ops"),
    (3, "CLEARMODEL\n", "checkpoint"),
    (3, "SETCOLOR|A|1\nCLEARMODEL\n", "ops"),
    (3, "CLEARMODEL|A\n", "ops"),
])
def test_plan_metadata_preserves_existing_body(client, tmp_path, grammar, operations, kind):
    plan_text = f"LEAF_MUTATION_PLAN|{grammar}\nBASE_SHA256|{'a' * 64}\n{operations}"
    plan = plan_text.encode("utf-8")
    result_digest = _store_plan(tmp_path, plan)
    response = _get(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.pop("plan_grammar") == grammar
    assert body.pop("plan_kind") == kind
    assert body == {
        "drawing_id": saves.DWG_DRAWING, "version": 2, "parent_version": 1,
        "plan_sha256": hashlib.sha256(plan).hexdigest(), "plan_text": plan_text,
        "result_sha256": result_digest, "error": None, "degraded_mode": False,
    }


@pytest.mark.parametrize("header", [
    "LEAF_MUTATION_PLAN|0", "LEAF_MUTATION_PLAN|4", "LEAF_MUTATION_PLAN|01",
    "LEAF_MUTATION_PLAN|", "LEAF_MUTATION_PLAN|3 ", "LEAF_MUTATION_PLAN|v3",
])
def test_unknown_plan_header_is_integrity_error(client, tmp_path, header):
    from envelopes import ErrorCode, error_obj

    _store_plan(tmp_path, f"{header}\nBASE_SHA256|{'a' * 64}\n".encode("utf-8"))
    response = _get(client)
    assert response.status_code == 500, response.text
    error = error_obj(ErrorCode.INTERNAL,
                      "stored mutation plan failed integrity validation", False)
    error["error_code"] = "plan_integrity"
    assert response.json() == {
        "error": error,
        "degraded_mode": False,
    }
