"""load_families(path=None) returns validated Family rows for plan/check callers."""

from dataclasses import dataclass
from pathlib import Path

import yaml

try:
    from .manifest import CONCERNS, FACETS, ID_PATTERN, ManifestError, choice, mapping, read_text, sequence, string
except ImportError:
    from manifest import CONCERNS, FACETS, ID_PATTERN, ManifestError, choice, mapping, read_text, sequence, string


@dataclass(frozen=True)
class Family:
    id: str
    trigger: dict
    relationship: str
    concerns: list[str]
    evidence: str
    stage: str
    note: str = ""


def load_families(path=None):
    # Fails closed on unknown keys and bounds the YAML input to 1 MiB.
    path = Path(path) if path is not None else Path(__file__).parent / "rules" / "families.yaml"
    try:
        data = yaml.safe_load(read_text(path))
        mapping(data, "rules", ("schema_version", "families"))
        if type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ManifestError("rules.schema_version: expected integer 1")
        families = []
        seen = set()
        for i, value in enumerate(sequence(data["families"], "families", 1, 200)):
            location = f"families[{i}]"
            mapping(value, location, ("id", "trigger", "relationship", "concerns", "evidence", "stage"), ("note",))
            identifier = string(value["id"], f"{location}.id", 64, r"[a-z0-9][a-z0-9_-]{0,63}")
            if identifier in seen:
                raise ManifestError(f"{location}.id: duplicate id {identifier}")
            seen.add(identifier)
            trigger = mapping(value["trigger"], f"{location}.trigger", ("entity",), ("facet", "side"))
            choice(trigger["entity"], f"{location}.trigger.entity", ("contract", "component", "target", "companion"))
            if "facet" in trigger:
                choice(trigger["facet"], f"{location}.trigger.facet", FACETS)
            if "side" in trigger:
                choice(trigger["side"], f"{location}.trigger.side", ("producer", "consumer", "any"))
            for i, concern in enumerate(sequence(value["concerns"], f"{location}.concerns", 1, len(CONCERNS))):
                choice(concern, f"{location}.concerns[{i}]", CONCERNS)
            for key in ("relationship", "evidence"):
                string(value[key], f"{location}.{key}", 64, ID_PATTERN)
            choice(value["stage"], f"{location}.stage", ("plan", "check", "both"))
            if "note" in value:
                string(value["note"], f"{location}.note", 1000)
            families.append(Family(**value))
        return sorted(families, key=lambda item: item.id)
    except (OSError, yaml.YAMLError, RecursionError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc
