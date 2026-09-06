"""Ledger-line schema freeze gate (CONTRACT-ADDENDUM §8, census #4).

server/broker_ledger.schema.json is the FROZEN wire contract for one line of
the broker attribution ledger. This suite pins the three-way agreement that
makes the freeze real, with no server boot:

    schema file  <->  broker.py's entry construction  <->  da/usage.py reader
                 <->  the §8 contract text (freeze markers + field names)

Dependency-light: ast + jsonschema (already a server requirement via
tool_validate) + file reads.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import jsonschema
import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
SCHEMA_PATH = SERVER_DIR / "broker_ledger.schema.json"

FROZEN_KEYS = ["ts", "tenant_id", "tool", "engine_op", "aps_endpoint",
               "aps_live", "engine_seconds", "usd_est", "status"]


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _ok_line() -> dict:
    return {"ts": 1753222000.0, "tenant_id": "demo-tenant", "tool": "panel-count",
            "engine_op": "count_panels", "aps_endpoint": "https://developer.api.autodesk.com",
            "aps_live": True, "engine_seconds": 5.9, "usd_est": 0.0163, "status": "ok"}


def test_schema_parses_and_freezes_exactly_the_nine_keys():
    s = _schema()
    assert s["$id"] == "leaf.broker-ledger-line.v1"
    assert sorted(s["required"]) == sorted(FROZEN_KEYS)
    assert sorted(s["properties"].keys()) == sorted(FROZEN_KEYS)


def test_broker_entry_literal_initializes_every_frozen_key():
    """The run path's `entry = {...}` literal (the ONE ledger append per run)
    must initialize exactly the frozen key set — a key added there without a
    schema/contract update fails here, and vice versa.

    `broker_run` holds the storage-cutover commit guard and delegates through
    `_broker_run` to `_broker_run_request`, the body shared with the plan route.
    All three names are accepted, but exactly one literal must still be found: this
    must not degrade into a test that passes because it matched nothing."""
    tree = ast.parse((SERVER_DIR / "broker.py").read_text(encoding="utf-8"))
    run_entry_points = {"broker_run", "_broker_run", "_broker_run_request"}
    literals: list[list[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in run_entry_points:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    keys = [k.value for k in sub.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)]
                    if "ts" in keys and "tenant_id" in keys:
                        literals.append(keys)
    assert literals, "the broker run path's ledger entry literal not found"
    assert sorted(literals[0]) == sorted(FROZEN_KEYS)


def test_ok_and_denial_lines_validate():
    schema = _schema()
    jsonschema.validate(_ok_line(), schema)
    denial = dict(_ok_line(), status="TENANT_DISABLED", aps_live=False,
                  engine_seconds=None, usd_est=None, tool=None)
    jsonschema.validate(denial, schema)
    init_shape = dict(_ok_line(), status="unknown", engine_seconds=None, usd_est=None)
    jsonschema.validate(init_shape, schema)


@pytest.mark.parametrize("mutation", [
    lambda l: l.pop("tenant_id"),                       # missing frozen key
    lambda l: l.__setitem__("aps_live", "yes"),         # retyped boolean
    lambda l: l.__setitem__("ts", "2026-07-22"),        # retyped timestamp
])
def test_tampered_lines_fail_validation(mutation):
    line = _ok_line()
    mutation(line)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(line, _schema())


def test_usage_reader_touches_only_frozen_fields():
    """da/usage.py is the authoritative metering consumer; every ledger field
    it reads must be in the frozen set (reader drift == silent metering bugs)."""
    src = (REPO_ROOT / "da" / "usage.py").read_text(encoding="utf-8")
    # ledger-line reads use e.get("...") / line.get("...") on parsed JSONL rows
    reads = set(re.findall(r"\b(?:e|line|rec|row)\.get\(\"([a-z_]+)\"", src))
    unknown = reads - set(FROZEN_KEYS)
    assert not unknown, f"usage.py reads non-frozen ledger fields: {sorted(unknown)}"
    assert {"tenant_id", "status", "usd_est", "ts", "aps_live"} <= reads


def test_contract_addendum_section8_is_frozen_and_names_every_key():
    text = (SERVER_DIR / "CONTRACT-ADDENDUM.md").read_text(encoding="utf-8")
    m = re.search(r"## §8 .*?(?=\n## )", text, flags=re.S)
    assert m, "CONTRACT-ADDENDUM.md §8 not found"
    sec = m.group(0)
    assert "FROZEN" in sec, "§8 must carry the freeze marker"
    assert "broker_ledger.schema.json" in sec
    for key in FROZEN_KEYS:
        assert key in sec, f"§8 ledger schema text missing frozen key {key!r}"


def test_top_level_contract_section8_is_frozen_and_names_every_key():
    text = (REPO_ROOT / "contract" / "CONTRACT.md").read_text(encoding="utf-8")
    m = re.search(r"## §8 .*?(?=\n## )", text, flags=re.S)
    assert m, "contract/CONTRACT.md §8 not found"
    sec = m.group(0)
    assert "FROZEN" in sec, "§8 must carry the freeze marker"
    assert "broker_ledger.schema.json" in sec
    for key in FROZEN_KEYS:
        assert key in sec, f"§8 ledger schema text missing frozen key {key!r}"
