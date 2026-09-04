"""
Authored-tool provenance on a version row (standardization slice 6a).

`_version_row()` grew ONE nullable key, `source_ref`: the sha256 of the writing
tool's PUBLISHED body as the server itself measures it at stamp time
(server/tool_loader.py `published_tool_source_sha256`, the same text the
harness's `leaf.tool-source.v1` receipt hashed on submit). It is bound into the
mutation binding as `tool_source_sha256` and stamped by server/write_loop.py
(`_server_held_source_ref`: never a value the sandbox returned; the seam rows
live in tests/test_live_mutation_plan.py), is stored verbatim by the store
(da/store.py `put_drawing` / `_pg_put`), and is bounded + charset-validated on
the way out so a drifted or hostile stored value can never be rendered as
provenance.

What this file pins, all of it falsifiable:

  1. the row shape gained EXACTLY that one key, and it is null for a version
     written without a receipt (never fabricated);
  2. a version written WITH a receipt reports it verbatim;
  3. `_source_ref` fails closed on every non-conforming shape (wrong charset,
     wrong length, uppercase, whitespace, non-string, oversized);
  4. the bound is checked BEFORE the regex, so a pathological input is
     rejected on length;
  5. restore carries the SOURCE version's provenance forward, because the new
     head's bytes are that version's bytes verbatim, and reports it in the
     restore envelope;
  6. restoring a version with no provenance yields null, not the restoring
     actor dressed as an author.

Hermetic: the same in-process TestClient + isolated LEAF_STORE_DIR pattern as
tests/test_version_restore.py, APS_LIVE=0.

Run:  cd server && python -m pytest tests/test_version_source_ref.py -q
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import write_loop  # noqa: E402

TENANT = "s6a-source-ref"
DRAWING = "provenance-demo"

# A real-shaped receipt digest: 64 lowercase hex characters.
RECEIPT_A = "a" * 64
RECEIPT_B = "0123456789abcdef" * 4

# The frozen row key set AFTER this slice. Written as a literal so an
# accidental addition (or a silent drop of an existing key) fails here rather
# than reaching a client that has no idea what the new key means.
ROW_KEYS = {
    "v", "parent", "created", "bytes", "sha256", "tool", "workitem_id",
    "note", "source_ref",
}


def _poly(handle: str, layer: str = "Panels") -> Dict:
    return {"layer": layer, "closed": True,
            "pts": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
            "xdata": None, "handle": handle}


def _intake(polylines: List[Dict]) -> Dict:
    return {"dwg": {}, "layers": ["Panels", "Roof"], "polylines": polylines,
            "inserts": [], "faces3d": [], "blockdefs": [], "geodata": None,
            "images": [], "imageNames": []}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi import FastAPI  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    monkeypatch.setenv("LEAF_STORE_DIR", str(tmp_path / "drawings"))
    monkeypatch.delenv("APS_LIVE", raising=False)
    monkeypatch.delenv("LEAF_AUTH_LIVE", raising=False)

    from routers import drawings as drawings_router  # noqa: PLC0415
    from envelopes import install_error_handlers  # noqa: PLC0415

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(drawings_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _backend(tmp_path):
    import store  # noqa: PLC0415

    return store.FilesystemBackend(str(tmp_path / "drawings"))


def _seed(tmp_path, refs: List[Optional[str]]):
    """v1 (ingest, no meta at all) then one version per entry in `refs`.

    `refs[i]` is the `source_ref` stamped on version i+2, so a chain of
    authored and unauthored versions is built with the SAME primitive
    (`write_loop._put_bytes_version`) the live write path uses.
    """
    import store  # noqa: PLC0415

    backend = _backend(tmp_path)
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    Path(tmp).write_bytes(
        json.dumps(_intake([_poly("A")]), separators=(",", ":")).encode("utf-8"))
    try:
        store.ingest_drawing(backend, TENANT, tmp, drawing_id=DRAWING)
    finally:
        Path(tmp).unlink(missing_ok=True)

    for index, ref in enumerate(refs):
        parent = index + 1
        payload = _intake([_poly("A"), _poly(f"H{index}")])
        meta = {"tool": "authored.tool" if ref else "drawing.write",
                "note": f"seed v{parent + 1}"}
        if ref is not None:
            meta["source_ref"] = ref
        got = write_loop._put_bytes_version(
            backend, TENANT, DRAWING,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            parent_version=parent, meta=meta,
        )
        assert got == parent + 1
    return backend


def _rows(client) -> Dict[int, Dict]:
    r = client.get(f"/api/drawings/{DRAWING}/versions", headers=_h(TENANT))
    assert r.status_code == 200, r.text
    return {row["v"]: row for row in r.json()["versions"]}


# --------------------------------------------------------------------------- #
# 1 + 2: the row shape, and what it says
# --------------------------------------------------------------------------- #
def test_row_shape_gained_exactly_source_ref(client, tmp_path):
    _seed(tmp_path, [RECEIPT_A])
    rows = _rows(client)
    assert set(rows) == {1, 2}
    for row in rows.values():
        assert set(row) == ROW_KEYS, "version row key set drifted"


def test_source_ref_is_null_without_a_receipt_and_verbatim_with_one(client, tmp_path):
    _seed(tmp_path, [None, RECEIPT_B])
    rows = _rows(client)
    # v1 was ingested with no meta at all; v2 was written by a tool that
    # carried no receipt. Both mean "not established", and neither invents one.
    assert rows[1]["source_ref"] is None
    assert rows[2]["source_ref"] is None
    assert rows[3]["source_ref"] == RECEIPT_B
    # The addition is backwards compatible: every pre-existing key survives.
    assert rows[3]["tool"] == "authored.tool"
    assert rows[3]["note"] == "seed v3"


def test_include_deltas_rows_carry_source_ref_too(client, tmp_path):
    # Both response shapes go through _version_row, and the delta branch is a
    # separate code path — pin it, or one of the two shells silently loses the
    # chip.
    _seed(tmp_path, [RECEIPT_A])
    r = client.get(f"/api/drawings/{DRAWING}/versions",
                   params={"include_deltas": 1}, headers=_h(TENANT))
    assert r.status_code == 200, r.text
    rows = {row["v"]: row for row in r.json()["versions"]}
    assert set(rows[2]) == ROW_KEYS | {"delta"}
    assert rows[2]["source_ref"] == RECEIPT_A


# --------------------------------------------------------------------------- #
# 3 + 4: the validator fails closed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    None,
    123,
    b"a" * 64,
    ["a" * 64],
    "",
    "A" * 64,                    # uppercase hex is not the receipt's shape
    "a" * 63,                    # too short
    "a" * 65,                    # too long
    " " + "a" * 63,              # leading whitespace
    "a" * 63 + "\n",             # trailing newline
    "g" * 64,                    # out of charset
    "sha256:" + "a" * 64,        # prefixed
    "<script>alert(1)</script>",
    "a" * 4096,                  # oversized: rejected on the bound
])
def test_source_ref_validator_fails_closed(bad):
    from routers import drawings as drawings_router  # noqa: PLC0415

    assert drawings_router._source_ref(bad) is None


def test_source_ref_validator_accepts_the_receipt_shape():
    from routers import drawings as drawings_router  # noqa: PLC0415

    # Not vacuous: the validator must actually pass a real digest, or every
    # "fails closed" row above would be satisfied by a function returning None.
    assert drawings_router._source_ref(RECEIPT_A) == RECEIPT_A
    assert drawings_router._source_ref(RECEIPT_B) == RECEIPT_B


def test_oversized_input_is_rejected_on_the_bound_not_scanned():
    from routers import drawings as drawings_router  # noqa: PLC0415

    # The bound exists so an unbounded stored value never reaches the regex.
    # A value one character over the cap that WOULD otherwise be scanned is
    # rejected, and the cap is smaller than any pathological payload.
    assert drawings_router._SOURCE_REF_MAX_LEN == 128
    assert drawings_router._source_ref("a" * (drawings_router._SOURCE_REF_MAX_LEN + 1)) is None


def test_a_stored_hostile_value_never_reaches_the_wire(client, tmp_path):
    # The store column is free text. Prove the READ path, not the write path,
    # is what protects the client.
    _seed(tmp_path, ["../../etc/passwd"])
    rows = _rows(client)
    assert rows[2]["source_ref"] is None


# --------------------------------------------------------------------------- #
# 5 + 6: restore carries provenance forward, honestly
# --------------------------------------------------------------------------- #
def test_restore_carries_the_source_versions_provenance_forward(client, tmp_path):
    # v2 authored (receipt A), v3 unauthored. Restoring v2 copies v2's BYTES
    # verbatim into a new head, so attributing those bytes to the tool whose
    # receipt produced them is a fact about the payload.
    _seed(tmp_path, [RECEIPT_A, None])

    r = client.post(f"/api/drawings/{DRAWING}/versions/2/restore", headers=_h(TENANT))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored_from"] == 2
    assert body["new_version"]["version"] == 4
    assert body["new_version"]["source_ref"] == RECEIPT_A

    rows = _rows(client)
    assert rows[4]["source_ref"] == RECEIPT_A
    assert rows[4]["tool"] == "restore"
    # History is never rewritten: the older rows keep exactly what they had.
    assert rows[2]["source_ref"] == RECEIPT_A
    assert rows[3]["source_ref"] is None


def test_restoring_an_unauthored_version_reports_no_provenance(client, tmp_path):
    _seed(tmp_path, [None, RECEIPT_A])

    r = client.post(f"/api/drawings/{DRAWING}/versions/2/restore", headers=_h(TENANT))
    assert r.status_code == 200, r.text
    body = r.json()
    # The restoring actor is NOT an author. Null, never "restore".
    assert body["new_version"]["source_ref"] is None
    assert _rows(client)[4]["source_ref"] is None
