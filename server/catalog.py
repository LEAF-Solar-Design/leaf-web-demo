"""
Capability catalog (CONTRACT-ADDENDUM section 9): tools grouped into families,
with SERVER-SIDE filtering of internal/QA tools.

A tool is INTERNAL iff it carries `"internal": true` in its package OR its name
matches a QA prefix (`qa-`, `_`) — rules declared in capability_families.json.
Internal tools are visible only with the X-Internal-Role: qa header.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

SERVER_DIR = Path(__file__).resolve().parent
FAMILIES_FILE = SERVER_DIR / "capability_families.json"
DEFAULT_FAMILY = "custom"
_APS_LIVE_AUTHORIZED = object()


def _load_config() -> Dict[str, Any]:
    return json.loads(FAMILIES_FILE.read_text(encoding="utf-8"))


def seed_tools() -> List[Dict[str, Any]]:
    """Catalog-seeded tools (QA/internal); NOT part of the /api/tools flat list."""
    return list(_load_config().get("seed_tools", []))


def is_internal(tool: Dict[str, Any], rules: Dict[str, Any]) -> bool:
    if tool.get(rules.get("internal_flag", "internal")):
        return True
    name = str(tool.get("name", ""))
    return any(name.startswith(p) for p in rules.get("qa_prefixes", []))


def filter_internal(tools: List[Dict[str, Any]],
                    include_internal: bool = False) -> List[Dict[str, Any]]:
    """Drop internal/QA tools using the SAME rules `build_catalog` applies.

    `build_catalog` filters as a side effect of grouping into families, so a
    caller that wants a flat tool list (the converse slash-menu registry) had
    no way to inherit that filtering and would otherwise ship internal/QA
    tools straight into a tenant's picker. One predicate, one config, both
    callers.
    """
    if include_internal:
        return list(tools)
    rules = _load_config().get("filter_rules", {})
    return [t for t in tools if isinstance(t, dict) and not is_internal(t, rules)]


def _family_for(tool: Dict[str, Any], cfg: Dict[str, Any], rules: Dict[str, Any]) -> str:
    if tool.get("family_id"):
        return str(tool["family_id"])
    if is_internal(tool, rules):
        return "internal_qa"
    mapping = cfg.get("tool_families", {})
    name = str(tool.get("name", ""))
    if name in mapping:
        return mapping[name]
    op = str(tool.get("engine_op", ""))
    if op in mapping:
        return mapping[op]
    return DEFAULT_FAMILY


def apply_live_aps_runtime_authority(
    tools: List[Dict[str, Any]], *, aps_live_enabled: bool
) -> List[Dict[str, Any]]:
    """Combine static tool eligibility with this process's live APS gate.

    The private projection marker is applied after catalog digests are issued,
    so runtime configuration cannot alter source authority or its digest.
    """
    return [
        {
            **tool,
            "_aps_live_runtime_authorized": _APS_LIVE_AUTHORIZED
            if aps_live_enabled is True and tool.get("aps_live") is True
            else None,
        }
        for tool in tools
    ]


def _capability_entry(tool: Dict[str, Any]) -> Dict[str, Any]:
    effective_digest = tool.get("effective_catalog_digest")
    if (tool.get("execution_class") == "instant" and isinstance(effective_digest, str)
            and len(effective_digest) == 64 and not effective_digest.startswith("sha256:")):
        effective_digest = "sha256:" + effective_digest
    return {
        "name": tool.get("name"),
        "version": tool.get("version", "1.0.0"),
        "description": tool.get("description", ""),
        "params_schema": tool.get("params", {"type": "object", "properties": {}}),
        "capabilities": tool.get("capabilities", []),
        "provenance": tool.get("provenance", {}),
        "kind": tool.get("kind"),
        "engine_op": tool.get("engine_op"),
        "catalog_digest": tool.get("catalog_digest"),
        "tool_manifest_sha256": tool.get("tool_manifest_sha256"),
        "catalog_commit": tool.get("catalog_commit"),
        "effective_catalog_digest": effective_digest,
        # Execution routing is catalog authority. The harness must never infer
        # instant eligibility from drawing.read or silently switch lanes.
        "execution_class": tool.get("execution_class", "batch"),
        "runtime": tool.get("runtime"),
        "limits": tool.get("limits"),
        "artifact_digest": tool.get("artifact_digest"),
        "batch_fallback": bool(tool.get("batch_fallback", False)),
        # Only the route's combined static and runtime authority marker may
        # advertise live APS. Raw registry metadata alone always fails closed.
        "aps_live": tool.get("_aps_live_runtime_authorized") is _APS_LIVE_AUTHORIZED,
    }


def build_catalog(tools: List[Dict[str, Any]], include_internal: bool = False) -> List[Dict[str, Any]]:
    """Group tools (+ catalog seed tools) into families; drop empty families.

    include_internal=False (default) filters internal/QA tools SERVER-SIDE.
    """
    cfg = _load_config()
    rules = cfg.get("filter_rules", {})
    source = list(tools) + seed_tools()

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for tool in source:
        if not include_internal and is_internal(tool, rules):
            continue
        grouped.setdefault(_family_for(tool, cfg, rules), []).append(_capability_entry(tool))

    families: List[Dict[str, Any]] = []
    for fam in cfg.get("families", []):
        caps = grouped.pop(fam["family_id"], [])
        if not caps:
            continue
        families.append({
            "family_id": fam["family_id"],
            "label": fam.get("label", fam["family_id"]),
            "description": fam.get("description", ""),
            "capabilities": caps,
        })
    # families not declared in the config file (defensive)
    for fam_id, caps in grouped.items():
        families.append({"family_id": fam_id, "label": fam_id, "description": "",
                         "capabilities": caps})
    return families
