"""Optional presentation fields carried BY the tool record: `icon`, `placement`,
`mcp_source`.

Before this module the ribbon hardcoded one icon key ("toolbox") for every
catalog tool and one static tab map, so a tool could not say how it wants to be
shown. These optional fields let the record say it; nothing else about the
record changes, and a record that declares none of them renders exactly as it
did.

`mcp_source` (standardization slice 8c) is a different kind of optional field:
it is never author-set (server/routers/author.py never forwards it to
`validate_optional_fields`), only stamped by the tenant MCP tool projection
(`server/mcp_tool_projection.py`) on a record folded from a connected server.
Its `server_id` is validated by CALLING `tenant_mcp_store.is_valid_server_id`
— the registry's own id shape — never a second regex here.

ONE definition of the ribbon tab set lives here, server-side. The web declares
the same ids in ``web/src/site/CockpitTopBand.jsx`` (``RIBBON_TABS``). That list
also declares ``model``, which carries a ``reason`` there (3D modelling is not in
this engine yet) and is therefore never selectable, so it is deliberately NOT a
valid placement target here.

Two entry points, two failure modes, both fail closed:

* ``sanitize_optional_fields`` — for tools READ from a fold tier (engine
  registry, catalog seed, tenant repo, write seed, authored store). The catalog
  must never break on one bad tool, so an invalid value is DROPPED and a warning
  naming the tool is logged once per tool per process.
* ``validate_optional_fields`` — for the authoring/publish API, where a caller
  is present to be told. An invalid value RAISES ``ToolRecordFieldError`` so the
  router can answer 422 with the specific message.

Hardening contract: every string is length-checked BEFORE it is matched, so a
hostile record cannot make the regex do unbounded work; the warn-once ledger is
a bounded set that stops growing rather than leaking one entry per bad row.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Mapping, Optional

import tenant_mcp_store

_LOG = logging.getLogger(__name__)

# Source of truth: web/src/site/CockpitTopBand.jsx RIBBON_TABS (minus `model`,
# which is declared there with a `reason` and is not a selectable tab).
RIBBON_TAB_IDS = frozenset({"draw", "insert", "annotate", "view", "manage"})
# Source of truth: the ribbon tool shape in web/src/lib/ribbonClusters.js.
PLACEMENT_SIZES = frozenset({"large", "small", "row"})
PLACEMENT_KEYS = frozenset({"tab", "size"})

# A sprite key, not a path: `web/src/site/CockpitIcon.jsx` looks it up in the
# icons8 built manifest and falls back to a monogram on a miss.
MAX_ICON_LEN = 40
_ICON_KEY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")

# `mcp_source.tool` names an upstream tool that was projected onto the record
# (never enumerated back out — see server/mcp_tool_projection.py); bounded the
# same as every other bare tool-name field in this codebase (e.g.
# routers/author.py's `target_tool_name`).
MAX_MCP_TOOL_LEN = 64
MCP_SOURCE_KEYS = frozenset({"server_id", "tool"})

# The warn-once ledger is bounded so a catalog full of bad rows cannot grow it
# without limit. Past the cap the module stops remembering and stops warning:
# the first WARN_LEDGER_MAX distinct subjects are the useful signal.
WARN_LEDGER_MAX = 512
_WARNED: set = set()


class ToolRecordFieldError(ValueError):
    """An optional tool-record field is present and malformed.

    Carries the offending field name so the authoring router can answer 422 with
    a message that names it.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def _warn_once(subject: str, message: str, *args: Any) -> None:
    """Log ``message`` at most once per process for ``subject`` (bounded)."""
    if subject in _WARNED:
        return
    if len(_WARNED) >= WARN_LEDGER_MAX:
        return
    _WARNED.add(subject)
    _LOG.warning(message, *args)


def reset_warn_ledger() -> None:
    """Test seam: forget which subjects have already warned."""
    _WARNED.clear()


def validate_icon(value: Any) -> str:
    """Return a valid sprite key, else raise. Bounded before it is matched."""
    if not isinstance(value, str):
        raise ToolRecordFieldError(
            "icon", f"icon must be a string sprite key, got {type(value).__name__}")
    if len(value) > MAX_ICON_LEN:
        raise ToolRecordFieldError(
            "icon", f"icon must be at most {MAX_ICON_LEN} characters")
    if _ICON_KEY_RE.fullmatch(value) is None:
        raise ToolRecordFieldError(
            "icon",
            "icon must be a lowercase sprite key matching [a-z0-9][a-z0-9-]{0,39}")
    return value


def validate_placement(value: Any) -> Dict[str, str]:
    """Return a valid ``{tab?, size?}`` placement, else raise.

    Unknown keys are rejected rather than ignored: a typo'd key would otherwise
    be silently dropped and the tool would sit somewhere the author did not ask
    for, with nothing said about it.
    """
    if not isinstance(value, Mapping):
        raise ToolRecordFieldError(
            "placement",
            f"placement must be an object with tab and/or size, got {type(value).__name__}")
    extra = sorted(str(key) for key in value.keys() if key not in PLACEMENT_KEYS)
    if extra:
        raise ToolRecordFieldError(
            "placement",
            f"placement accepts only tab and size; unknown key(s): {', '.join(extra)}")
    placement: Dict[str, str] = {}
    if "tab" in value:
        tab = value["tab"]
        if not isinstance(tab, str) or tab not in RIBBON_TAB_IDS:
            raise ToolRecordFieldError(
                "placement",
                "placement.tab must be one of: " + ", ".join(sorted(RIBBON_TAB_IDS)))
        placement["tab"] = tab
    if "size" in value:
        size = value["size"]
        if not isinstance(size, str) or size not in PLACEMENT_SIZES:
            raise ToolRecordFieldError(
                "placement",
                "placement.size must be one of: " + ", ".join(sorted(PLACEMENT_SIZES)))
        placement["size"] = size
    if not placement:
        raise ToolRecordFieldError(
            "placement", "placement must declare at least one of tab or size")
    return placement


def validate_mcp_source(value: Any) -> Dict[str, str]:
    """Return a valid ``{server_id, tool}`` mcp_source, else raise.

    ``server_id`` is checked through ``tenant_mcp_store.is_valid_server_id`` —
    the registry's own id shape — never re-typed here. Unknown keys are
    rejected rather than ignored, the same posture ``validate_placement`` takes.
    """
    if not isinstance(value, Mapping):
        raise ToolRecordFieldError(
            "mcp_source",
            f"mcp_source must be an object with server_id and tool, got {type(value).__name__}")
    extra = sorted(str(key) for key in value.keys() if key not in MCP_SOURCE_KEYS)
    if extra:
        raise ToolRecordFieldError(
            "mcp_source",
            f"mcp_source accepts only server_id and tool; unknown key(s): {', '.join(extra)}")
    server_id = value.get("server_id")
    if not tenant_mcp_store.is_valid_server_id(server_id):
        raise ToolRecordFieldError(
            "mcp_source", "mcp_source.server_id must be a valid registry server id")
    tool = value.get("tool")
    if not isinstance(tool, str) or not tool or len(tool) > MAX_MCP_TOOL_LEN:
        raise ToolRecordFieldError(
            "mcp_source",
            f"mcp_source.tool must be a non-empty string of at most {MAX_MCP_TOOL_LEN} characters")
    return {"server_id": server_id, "tool": tool}


def validate_optional_fields(source: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the optional fields PRESENT on ``source``; raise on the first bad one.

    Used by the authoring/publish path, where the caller can be told 422.
    ``mcp_source`` is never a key of ``source`` here — routers/author.py's
    ``_validated_record_fields`` builds ``source`` from only ``icon`` and
    ``placement``, because an author never sets their own projection origin.
    """
    fields: Dict[str, Any] = {}
    if source.get("icon") is not None:
        fields["icon"] = validate_icon(source["icon"])
    if source.get("placement") is not None:
        fields["placement"] = validate_placement(source["placement"])
    if source.get("mcp_source") is not None:
        fields["mcp_source"] = validate_mcp_source(source["mcp_source"])
    return fields


def sanitize_optional_fields(tool: Mapping[str, Any]) -> Dict[str, Any]:
    """Return only the VALID optional fields of a folded tool record.

    Fold-tier read path: one malformed tool must never break the whole catalog,
    so an invalid field is dropped and warned about (once per tool per field per
    process), naming the tool so the drop is never silent.
    """
    fields: Dict[str, Any] = {}
    name = str(tool.get("name", "")) or "<unnamed>"
    for field, check in (
        ("icon", validate_icon),
        ("placement", validate_placement),
        ("mcp_source", validate_mcp_source),
    ):
        raw = tool.get(field)
        if raw is None:
            continue
        try:
            fields[field] = check(raw)
        except ToolRecordFieldError as exc:
            _warn_once(
                f"{field}:{name}",
                "tool_record_field_dropped: tool=%s field=%s reason=%s",
                name, field, exc.message,
            )
    return fields


def warn_family_fallback(name: str, default_family: str) -> None:
    """Warn once that ``name`` fell through to the default family by name lookup.

    A rename that outruns ``capability_families.json``'s ``tool_families`` map
    used to land the tool in the default family with nothing said. Authored
    tools are stamped with a persisted ``family_id`` at publish time and never
    reach this path.
    """
    _warn_once(
        f"family_fallback:{name}",
        "tool_family_fallback: tool=%s fell through to family=%s "
        "(no explicit family_id and no tool_families entry)",
        name or "<unnamed>", default_family,
    )


def family_id_for_persist(tool: Mapping[str, Any], resolved: Optional[str]) -> Optional[str]:
    """The ``family_id`` to STAMP on a record being written, or None to leave it.

    An explicit family_id on the record always wins (it is the author's answer);
    otherwise the resolver's answer is persisted so a later rename keeps the
    family instead of silently falling back to the default.
    """
    explicit = tool.get("family_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if isinstance(resolved, str) and resolved.strip():
        return resolved.strip()
    return None
