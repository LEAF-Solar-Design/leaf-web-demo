"""Real server-catalog fixture for the harness cross-language live-APS regression.

Computes the ACTUAL /api/capabilities response body (server/routers/capabilities.py
+ server/catalog.py + server/deps.py, ALL UNMOCKED) for one scenario of the
live-APS "trusted operator-owned engine winner" authority chain.

THE SEAM IS REAL HERE. Earlier drafts of this fixture replaced
``deps.effective_tools_with_provenance`` / ``deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE``
with per-scenario lambdas because the seam had no production implementation yet.
It does now (server/deps.py, PR #545): every scenario below runs the REAL strict
provenance fold over REAL stores on disk. The only overrides this fixture makes
are STORE WIRING, the same sanctioned pattern
server/tests/test_catalog_tool_source_provenance.py uses:

  * environment knobs read by the real code at their real read points
    (``APS_LIVE`` before import, ``LEAF_TENANTS_DIR`` for the tenant fold,
    ``LEAF_CUSTOMIZATION_DB`` pointed at a nonexistent file so the
    customization authority is naturally absent), and
  * store LOCATIONS (``deps.ENGINE_REGISTRY`` / ``deps.AUTHORED_STORE`` paths)
    for scenarios that need a variant registry file or hermetic isolation from
    a developer's local authored store.

No function or constant of the provenance seam, the catalog algebra, or the
router is ever replaced.

Printed as one JSON line so harness/test/serverCatalogLiveAps.test.ts can feed
it, byte-for-byte, into the REAL HttpAppRunClient + real ConverseLoop
run_capability selection logic (never the fake app-run client / fake catalog).
"""
from __future__ import annotations

import importlib.util
import json
import os
import platform as _stdlib_platform
import sys
import sysconfig
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SERVER_DIR = ROOT / "server"
# A repository-root current directory can resolve Leaf's top-level ``platform``
# package before Python's standard-library module. Load the stdlib file by
# path and bind its canonical name before FastAPI or Pydantic imports it.
if not callable(getattr(_stdlib_platform, "system", None)):
    platform_path = Path(sysconfig.get_path("stdlib")) / "platform.py"
    platform_spec = importlib.util.spec_from_file_location("_leaf_stdlib_platform", platform_path)
    if platform_spec is None or platform_spec.loader is None:
        raise RuntimeError(f"cannot load standard-library platform module from {platform_path}")
    _stdlib_platform = importlib.util.module_from_spec(platform_spec)
    platform_spec.loader.exec_module(_stdlib_platform)
    sys.modules["platform"] = _stdlib_platform


def _scenario_environment(scenario: str, scratch: Path) -> None:
    """Set the real environment knobs BEFORE the server modules import.

    ``deps.APS_LIVE`` is read from the environment at import time in
    production; setting it here is the production path, not a patch.
    """
    os.environ["APS_LIVE"] = "0" if scenario == "runtime_off" else "1"
    # The customization authority is naturally absent: a nonexistent sqlite
    # path with rollout OFF makes effective_catalog_pin() return None through
    # its real branches (see customization_service.effective_catalog_pin).
    os.environ["LEAF_CUSTOMIZATION_DB"] = str(scratch / "absent-customization.sqlite3")
    for rollout_knob in (
        "LEAF_CUSTOMIZATION_STORE",
        "LEAF_CUSTOMIZATION_R5_MODE",
        "LEAF_CUSTOMIZATION_R6_MODE",
    ):
        os.environ.pop(rollout_knob, None)
    # Tenant fold: OFF unless the scenario needs a tenant-repo shadow.
    os.environ.pop("LEAF_TENANT_REPO", None)
    if scenario == "shadow":
        os.environ["LEAF_TENANTS_DIR"] = str(scratch / "tenants")
    else:
        os.environ.pop("LEAF_TENANTS_DIR", None)


def _real_registry_tool(registry: dict, name: str) -> dict:
    for tool in registry["tools"]:
        if tool.get("name") == name:
            return tool
    raise SystemExit(f"engine/registry.json no longer defines {name!r}")


def run(scenario: str) -> dict:
    scratch = Path(tempfile.mkdtemp(prefix="live-aps-fixture-"))
    _scenario_environment(scenario, scratch)

    for p in (str(ROOT), str(SERVER_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)

    import deps  # noqa: E402
    from routers import capabilities as capabilities_router  # noqa: E402

    # Hermetic isolation from a developer's local gitignored authored store:
    # point the store PATH at an empty file (never replace a loader).
    empty_authored = scratch / "authored.json"
    empty_authored.write_text(json.dumps({"tools": []}), encoding="utf-8")
    deps.AUTHORED_STORE = empty_authored
    deps._AUTHORED[:] = []

    real_registry = json.loads(deps.ENGINE_REGISTRY.read_text(encoding="utf-8"))

    if scenario in ("engine_winner_live", "runtime_off"):
        # The real regression (and its runtime-off fail-closed twin): the
        # SHIPPED engine/registry.json, whose count-by-layer row carries the
        # aps_live marker PR #541 authorized. Nothing else is wired.
        pass

    elif scenario == "malformed_registry":
        # Fail-closed (2): point ENGINE_REGISTRY at malformed JSON. The REAL
        # strict provenance fold raises ToolCatalogProvenanceError, the router
        # degrades loudly to the forgiving read, and the forgiving loader's
        # real fallback (tools_fallback.DEFAULT_TOOLS) carries no aps_live
        # marker -> no trusted digest -> live APS fails closed while the
        # catalog itself stays available.
        bad = scratch / "malformed-registry.json"
        bad.write_text("{not valid json", encoding="utf-8")
        deps.ENGINE_REGISTRY = bad

    elif scenario == "shadow":
        # Fail-closed (3): a tenant-repo row shadows the engine tool's NAME
        # (and even claims aps_live True), written into a REAL tenant repo the
        # REAL fold resolves via LEAF_TENANTS_DIR. The strict fold classifies
        # the winner as tenant_repo, never operator_owned_engine -- engine
        # ownership is decided by which tier the winning row came from, not by
        # anything the row claims about itself.
        shadow_row = {
            **_real_registry_tool(real_registry, "count-by-layer"),
            "description": "tenant-authored row shadowing the engine tool name",
            "aps_live": True,
        }
        tenant_repo = scratch / "tenants" / "tenant-a"
        tenant_repo.mkdir(parents=True)
        (tenant_repo / "registry.json").write_text(
            json.dumps({"tools": [shadow_row]}), encoding="utf-8"
        )

    elif scenario == "non_boolean_marker":
        # Fail-closed (4): the engine registry itself carries a non-boolean
        # truthy marker ("true", not True). The row still WINS as
        # operator_owned_engine through the real fold, but the runtime
        # authority requires ``is True`` and the trusted-digest set only
        # admits ``aps_live is True`` rows -- must be rejected, not coerced.
        forged = json.loads(json.dumps(real_registry))
        _real_registry_tool(forged, "count-by-layer")["aps_live"] = "true"
        forged_path = scratch / "forged-registry.json"
        forged_path.write_text(json.dumps(forged), encoding="utf-8")
        deps.ENGINE_REGISTRY = forged_path

    else:
        raise SystemExit(f"unknown scenario {scenario!r}")

    return capabilities_router.capabilities(
        x_internal_role=None, x_ops_secret=None, tenant="tenant-a"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: live-aps-catalog-fixture.py SCENARIO")
    print(json.dumps(run(sys.argv[1])))
