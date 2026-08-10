"""Real server-catalog fixture for the harness cross-language live-APS regression.

Computes the ACTUAL /api/capabilities response body (server/routers/capabilities.py
+ server/catalog.py, both UNMOCKED) for one scenario of the live-APS "trusted
operator-owned engine winner" authority chain.

The provenance seam it exercises (``deps.effective_tools_with_provenance`` /
``deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE``) has NO production implementation
anywhere in this repository (confirmed by a repo-wide search, 2026-08-09): it
is a documented ``getattr(deps, ..., None)``-guarded optional hook (see
server/routers/capabilities.py), so in the deployed server today
``operator_owned_engine_source`` always resolves to ``None`` and live APS can
never turn on. This fixture populates the seam the same way
server/tests/test_catalog_read_fallback.py already does (monkeypatch AT that
boundary), then lets the real router and real catalog module compute the
projection from there — nothing inside catalog.py's selection algebra or
capabilities.py's routing is mocked.

Printed as one JSON line so harness/test/serverCatalogLiveAps.test.ts can feed
it, byte-for-byte, into the REAL HttpAppRunClient + real ConverseLoop
run_capability selection logic (never the fake app-run client / fake catalog).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SERVER_DIR = ROOT / "server"
for p in (str(ROOT), str(SERVER_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import deps  # noqa: E402
import customization_service  # noqa: E402
from routers import capabilities as capabilities_router  # noqa: E402


# The real engine/registry.json definition for count-by-layer, plus the
# aps_live marker a trusted engine winner would carry.
ENGINE_TOOL = {
    "name": "count-by-layer",
    "version": "1.0.0",
    "description": "Counts every model-space entity per layer.",
    "kind": "script",
    "engine_op": "count_by_layer",
    "script": "engine/tools/count_by_layer.lsp",
    "params": {"type": "object", "properties": {}, "required": []},
    "returns": {
        "type": "object",
        "properties": {"counts": {"type": "object", "additionalProperties": {"type": "integer"}}},
    },
    "capabilities": ["drawing.read"],
    "provenance": {"author": "agent", "created": "2026-07-17T00:00:00Z"},
    "aps_live": True,
}


def _wire(effective_row, source, aps_live_enabled, engine_registry_tools):
    deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE = "operator_owned_engine"
    deps.effective_tools_with_provenance = lambda _tenant: [(effective_row, source)]
    deps.load_engine_registry_tools = lambda: engine_registry_tools
    deps.all_tools = lambda _tenant: [effective_row]
    deps.APS_LIVE = aps_live_enabled
    customization_service.effective_catalog_pin = lambda _tenant: None


def run(scenario: str) -> dict:
    if scenario == "engine_winner_live":
        # The real regression: a trusted operator-owned engine winner, digest
        # intact, with runtime APS enabled.
        _wire(ENGINE_TOOL, "operator_owned_engine", True, [ENGINE_TOOL])

    elif scenario == "runtime_off":
        # Fail-closed (1): identical winner, runtime APS disabled.
        _wire(ENGINE_TOOL, "operator_owned_engine", False, [ENGINE_TOOL])

    elif scenario == "malformed_registry":
        # Fail-closed (2): point ENGINE_REGISTRY at malformed JSON so the REAL
        # deps.load_engine_registry_tools() (left unpatched) takes its actual
        # except-and-fallback branch to tools_fallback.DEFAULT_TOOLS, whose
        # count-by-layer carries no aps_live marker at all -> no trusted
        # digest -> fails closed even though the effective row still claims
        # operator_owned_engine + aps_live True.
        bad = Path(tempfile.mkstemp(suffix=".json")[1])
        bad.write_text("{not valid json", encoding="utf-8")
        deps.ENGINE_REGISTRY = bad
        deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE = "operator_owned_engine"
        deps.effective_tools_with_provenance = lambda _tenant: [(ENGINE_TOOL, "operator_owned_engine")]
        deps.all_tools = lambda _tenant: [ENGINE_TOOL]
        deps.APS_LIVE = True
        customization_service.effective_catalog_pin = lambda _tenant: None

    elif scenario == "shadow":
        # Fail-closed (3): a tenant/authored tool shadows the engine tool's
        # NAME (and even claims aps_live True) but its provenance source is
        # tenant_repo, not operator_owned_engine -- engine ownership must not
        # be spoofable by a same-named row from another tier.
        shadow_row = {**ENGINE_TOOL, "description": "tenant-authored row shadowing the engine tool name"}
        _wire(shadow_row, "tenant_repo", True, [ENGINE_TOOL])

    elif scenario == "non_boolean_marker":
        # Fail-closed (4): the winning row's aps_live marker is a non-boolean
        # truthy value ("true", not True) -- must be rejected, not coerced.
        forged_row = {**ENGINE_TOOL, "aps_live": "true"}
        _wire(forged_row, "operator_owned_engine", True, [ENGINE_TOOL])

    else:
        raise SystemExit(f"unknown scenario {scenario!r}")

    return capabilities_router.capabilities(x_internal_role=None, x_ops_secret=None, tenant="tenant-a")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: live-aps-catalog-fixture.py SCENARIO")
    print(json.dumps(run(sys.argv[1])))
