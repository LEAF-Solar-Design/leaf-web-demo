"""The converse lane's slash-menu registry — commands, skills and tools in ONE
tenant-scoped catalog.

The composer's `/` picker currently lists tools only. Claude Code's picker
lists three kinds in one menu, and the client already ranks by `kind`
(web/src/composer.js `rankEntries`, whose group order is command -> skill ->
tool) — it just has nothing to supply that field. This module is that supply.

ONE RULE, and it is the reason this file is short: a command only exists here
if the client can actually run it. Every entry carries `client_action`, and the
UI dispatches on that; an entry with no action could not be executed, so it
would be a dead affordance in a menu the user is trusting. Commands are
therefore added as their behaviour lands, not ahead of it — the same discipline
the tool chips already follow (they expand only when the event carried a
payload).

Skills are declared in the shape here but come from an empty source until the
curated per-tier skill bundle ships (see the build plan's Wave 0 spike). The
plumbing is honest about that: `build_registry` takes whatever the caller has,
and today the caller has none.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# Group order, mirroring web/src/composer.js REGISTRY_GROUPS. Keep in step.
KINDS = ("command", "skill", "tool")

# Platform commands. `client_action` is the contract with the UI: the client
# dispatches on it, so a command with an unknown action is inert by
# construction and must not be listed.
#
# Deliberately small. `/stop` exists because interrupt shipped; `/help` because
# listing the menu is the menu's own job. Everything else in the parity matrix
# (/clear, /model, /usage, /resume, /compact, /mcp …) lands here as each one
# becomes real.
PLATFORM_COMMANDS: List[Dict[str, Any]] = [
    {
        "kind": "command",
        "name": "help",
        "description": "List everything you can run here — commands, skills and tools.",
        "client_action": "help",
    },
    {
        "kind": "command",
        "name": "stop",
        "description": "Interrupt the turn the assistant is running now.",
        "client_action": "cancel_turn",
    },
]


def command_entries() -> List[Dict[str, Any]]:
    """The platform commands, copied so a caller cannot mutate the module list."""
    return [dict(entry) for entry in PLATFORM_COMMANDS]


def tool_entries(tools: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Registry rows for the tenant's runnable tools.

    Only `name`/`description` are surfaced: the picker needs a label and a
    subtitle, and copying the whole catalog row would leak fields (pins,
    digests, repo paths) into a response whose only job is to populate a menu.
    A tool without a name cannot be completed into the input, so it is dropped
    rather than rendered blank.
    """
    entries: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        description = tool.get("description")
        entries.append({
            "kind": "tool",
            "name": name,
            "description": description if isinstance(description, str) else "",
            "client_action": "run_tool",
        })
    return entries


def skill_entries(skills: Optional[Iterable[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Registry rows for loaded skills.

    Empty until the curated per-tier skill bundle exists. Shaped now so the
    client and its ranking need no change when it does.
    """
    entries: List[Dict[str, Any]] = []
    for skill in skills or []:
        if not isinstance(skill, dict):
            continue
        name = skill.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        description = skill.get("description")
        entries.append({
            "kind": "skill",
            "name": name,
            "description": description if isinstance(description, str) else "",
            "client_action": "invoke_skill",
        })
    return entries


def build_registry(tools: Optional[Iterable[Dict[str, Any]]] = None,
                   skills: Optional[Iterable[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The whole catalog, grouped-order first and counted per kind.

    Entries arrive in KINDS order so a client that renders them verbatim is
    already grouped; the client still ranks by query, and `rankEntries` uses
    the same order to break ties.
    """
    entries = command_entries() + skill_entries(skills) + tool_entries(tools)
    counts = {kind: 0 for kind in KINDS}
    for entry in entries:
        kind = entry.get("kind")
        if kind in counts:
            counts[kind] += 1
    return {"entries": entries, "counts": counts, "kinds": list(KINDS)}
