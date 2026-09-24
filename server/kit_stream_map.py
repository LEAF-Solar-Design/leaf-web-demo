# Studio SSE vocabulary projected onto Kit ThreadEventV1 (magpie VI.4 row 4).
# The check lives in tests/test_kit_stream_superset.py. An extension travels as
# an unnamed frame with its type in the envelope (K-D7), not a MAJOR change.
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping

KIT_SCHEMA_RELPATH = "contract/assistant.schema.json"


@dataclass(frozen=True)
class KitProjection:
    event: str
    role: str | None = None
    block: str | None = None
    states: tuple[str, ...] = ()


NATIVE = {
    "turn_started": KitProjection("turn", states=("running",)),
    "turn_queued": KitProjection("turn", states=("queued",)),
    "turn_queue_dropped": KitProjection("turn", states=("stopped",)),
    "turn_complete": KitProjection("turn", states=("done", "failed", "stopped")),
    "error": KitProjection("turn", states=("failed",)),
    "text_delta": KitProjection("item", "assistant", "text"),
    "tool_call": KitProjection("item", "assistant", "tools", ("running",)),
    "tool_result": KitProjection("item", "assistant", "tools", ("done", "failed")),
    "proposed_run": KitProjection("item", "server", "card", ("pending",)),
    "confirmation_required": KitProjection("item", "server", "card", ("pending",)),
    "confirmation_resolved": KitProjection("item", "server", "card", ("approved", "declined", "expired")),
}

EXTENSIONS = {
    "job_linked": "Job handoff: a Kit artifact block needs a content digest that a job id does not have.",
    "question_required": "Display-only question card: the Kit has no question block.",
    "turn_usage": "Per-turn token usage: the Kit carries quota on ThreadV1, not as an event.",
    "session_state": "Session lifecycle: the Kit has no session event.",
    "overlay_proposed": "T1 overlay proposal lifecycle is outside the assistant contract.",
    "overlay_decided": "T1 overlay decision lifecycle is outside the assistant contract.",
    "overlay_revoked": "T1 overlay revocation lifecycle is outside the assistant contract.",
}


def load_kit_vocabulary(path: Path) -> dict[str, frozenset[str]]:
    """Read a complete vocabulary or fail closed with the schema path."""
    path = Path(path)

    def strings(value):
        if (not isinstance(value, list) or not value
                or any(not isinstance(item, str) or not item for item in value)
                or len(set(value)) != len(value)):
            raise ValueError("expected a nonempty list of unique strings")
        return frozenset(value)

    def variants(value):
        if not isinstance(value, list) or not value:
            raise ValueError("expected nonempty anyOf")
        names = strings([item["properties"]["type"]["const"] for item in value])
        return {item["properties"]["type"]["const"]: item for item in value}, names

    try:
        with path.open("rb") as stream:
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("schema exceeds 1 MiB")
            blob = stream.read(1024 * 1024)
        defs = json.loads(blob)["$defs"]
        events, event_names = variants(defs["ThreadEventV1"]["anyOf"])
        _, blocks = variants(defs["BlockV1"]["anyOf"])
        return {
            "events": event_names,
            "turn_states": strings(events["turn"]["properties"]["state"]["enum"]),
            "roles": strings(defs["ItemV1"]["properties"]["role"]["enum"]),
            "blocks": blocks,
            "tool_states": strings(defs["ToolCallV1"]["properties"]["state"]["enum"]),
            "card_states": strings(defs["ConfirmCardV1"]["properties"]["state"]["enum"]),
        }
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise ValueError(f"{path}: invalid Kit vocabulary: {exc}") from exc


def unresolved(
    projections: Mapping[str, KitProjection],
    vocab: Mapping[str, frozenset[str]],
) -> list[str]:
    """Report each invalid projection field without hiding other defects."""
    problems = []
    for name, projection in projections.items():
        if projection.event not in vocab["events"]:
            problems.append(f"{name}: unknown event {projection.event}")
            continue
        if projection.event == "turn":
            if projection.role is not None:
                problems.append(f"{name}: turn has a role")
            if projection.block is not None:
                problems.append(f"{name}: turn has a block")
            if not projection.states:
                problems.append(f"{name}: turn has no states")
            for state in projection.states:
                if state not in vocab["turn_states"]:
                    problems.append(f"{name}: unknown turn state {state}")
        elif projection.event == "item":
            if projection.role not in vocab["roles"]:
                problems.append(f"{name}: unknown item role {projection.role}")
            if projection.block not in vocab["blocks"]:
                problems.append(f"{name}: unknown item block {projection.block}")
            state_key = {"tools": "tool_states", "card": "card_states"}.get(projection.block)
            if state_key:
                for state in projection.states:
                    if state not in vocab[state_key]:
                        problems.append(f"{name}: unknown {projection.block} state {state}")
            elif projection.states:
                problems.append(f"{name}: states on block {projection.block}")
    return problems
