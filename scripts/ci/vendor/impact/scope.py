"""The shared path-to-obligation predicate for plan and check."""

from dataclasses import dataclass, field

try:
    from .manifest import CONCERNS, ManifestError, match_path
except ImportError:
    from manifest import CONCERNS, ManifestError, match_path


@dataclass(frozen=True)
class RequiredRow:
    concern: str
    subject: str
    family: str | None


@dataclass
class Scope:
    components: list[str] = field(default_factory=list)
    contracts_producer: list[str] = field(default_factory=list)
    contracts_consumer: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    companions: list[tuple[str, list[str]]] = field(default_factory=list)
    required: list[RequiredRow] = field(default_factory=list)


def ordered(rows):
    # The first family in stable family order owns a duplicate concern/subject.
    unique = {}
    for row in rows:
        unique.setdefault((row.concern, row.subject), row)
    return sorted(unique.values(), key=lambda row: (CONCERNS.index(row.concern), row.subject))


def _required(scope, paths, manifest, families):
    rows = []
    for family in sorted(families, key=lambda item: item.id):
        trigger = family.trigger
        entity = trigger["entity"]
        subjects = set()
        if entity == "contract":
            side = trigger.get("side", "any")
            for contract in manifest["contracts"]:
                for consumer in contract["consumers"]:
                    producer_hit = side in ("producer", "any") and contract["id"] in scope.contracts_producer
                    consumer_hit = side in ("consumer", "any") and contract["id"] in scope.contracts_consumer
                    if producer_hit or consumer_hit:
                        repo = manifest["repository"] if consumer["repo"] == "self" else consumer["repo"]
                        subjects.add(f"{contract['id']}/{repo}")
        elif entity == "component":
            subjects.update(component["id"] for component in manifest["components"]
                            if component["id"] in scope.components
                            and ("facet" not in trigger or trigger["facet"] in component["facets"]))
        elif entity == "target":
            subjects.update(scope.targets)
        elif entity == "companion":
            subjects.update(path for path, _ in scope.companions)
        rows.extend(RequiredRow(concern, subject, family.id)
                    for subject in sorted(subjects) for concern in family.concerns)
    return ordered(rows)


def impact_scope(paths, manifest, families):
    # All path matching for obligations lives here, for both planned and actual paths.
    paths = sorted(set(paths))
    hit = lambda patterns: any(match_path(patterns, path) for path in paths)
    result = Scope(
        components=sorted(item["id"] for item in manifest["components"] if hit(item["paths"])),
        contracts_producer=sorted(item["id"] for item in manifest["contracts"]
                                  if item["producer"]["repo"] == "self" and hit(item["producer"]["paths"])),
        contracts_consumer=sorted(item["id"] for item in manifest["contracts"]
                                  if any(consumer["repo"] == "self" and hit(consumer["paths"])
                                         for consumer in item["consumers"])),
        targets=sorted(item["id"] for item in manifest["targets"] if hit(item.get("paths", []))),
        companions=sorted((item["path"], sorted(set(value for value in item["moves_with"]
                               if not value.startswith(("EXTERNAL: ", "repo:")) and value not in paths)))
                          for item in manifest["companions"] if hit([item["path"]])),
    )
    result.required = _required(result, paths, manifest, families)
    return result


def add_targets(result, targets, manifest, families):
    # Explicit transaction targets use the same family expansion as path targets.
    known = {item["id"] for item in manifest["targets"]}
    for target in targets:
        if target not in known:
            raise ManifestError(f"unknown target: {target}")
    result.targets = sorted(set(result.targets) | set(targets))
    target_rows = _required(Scope(targets=result.targets), [], manifest, families)
    result.required = ordered([*result.required, *target_rows])
    return result


def touched(result, paths):
    return {"paths": sorted(set(paths)), "components": result.components,
            "contracts": sorted(set(result.contracts_producer + result.contracts_consumer)),
            "targets": result.targets, "companions": sorted({path for path, _ in result.companions})}
