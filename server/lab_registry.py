"""Trusted execution metadata, never authority supplied by a document.

Total effect table, in precedence order: malformed arguments, live APS, or
drawing.write/solve/cloud-proposal (including descendants) -> refused;
worker -> compute; simulate.* or simulate adapter -> simulate;
read.* or handoff -> read; otherwise -> pure. None is the absent adapter.
Execution classes outside inline/worker/handoff are malformed.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import math
import re
from threading import Lock

import catalog

KASHBOT_CLASSES = ("engine", "book", "market", "feed", "run", "position",
                   "settlement", "tuning", "engine-lab", "tape")


def total_effect(capability, solar_adapter, execution_class, aps_live):
    if (not isinstance(capability, str) or not capability.strip()
            or (solar_adapter is not None and not isinstance(solar_adapter, str))
            or not isinstance(execution_class, str)
            or execution_class not in ("inline", "worker", "handoff")
            or type(aps_live) is not bool):
        return "refused"
    if aps_live or any(capability == prefix or capability.startswith(prefix + ".")
                       for prefix in ("drawing.write", "solve", "cloud-proposal")):
        return "refused"
    if execution_class == "worker":
        return "compute"
    if capability.startswith("simulate.") or (solar_adapter is not None and (
            solar_adapter == "simulate" or solar_adapter.startswith("simulate."))):
        return "simulate"
    if capability.startswith("read.") or execution_class == "handoff":
        return "read"
    return "pure"


def total_residency(effect, capability, execution_class, host_source):
    """Residency table, in precedence order:

    host.* capability, a host source name, or handoff -> host;
    pure total effect -> none; otherwise -> tenant.
    """
    if ((isinstance(capability, str) and capability.startswith("host."))
            or host_source or execution_class == "handoff"):
        return "host"
    if effect == "pure":
        return "none"
    return "tenant"


@dataclass(frozen=True)
class TrustedBlock:
    kind: str
    version: str
    effect: str
    residency: str
    capability: str
    solar_adapter: str | None
    execution_class: str
    aps_live: bool
    host: str
    returns_schema_digest: str | None = None
    host_source: str | None = None

    @property
    def runnable_in_production(self):
        return self.host == "production" and self.execution_class != "handoff"


def _seed_registry():
    result = {}
    for tool in catalog.seed_tools():
        capability = tool.get("capability", next(iter(tool.get("capabilities", [])), "pure"))
        execution = tool.get("execution_class", "inline")
        adapter = tool.get("solar_adapter")
        live = tool.get("aps_live", False)
        effect = total_effect(capability, adapter, execution, live)
        if effect == "refused" or capability == "money" or capability.startswith("money."):
            continue
        block = TrustedBlock(tool.get("id", tool.get("name")), tool.get("version", "1.0.0"),
                             effect, total_residency(effect, capability, execution, tool.get("host_source")),
                             capability, adapter, execution, live, tool.get("host", "production"),
                             host_source=tool.get("host_source"))
        key = (block.kind, block.version)
        if key in result:
            raise RuntimeError(f"duplicate registry seed: {key!r}")
        result[key] = block
    for source in KASHBOT_CLASSES:
        capability = "read." + source
        effect = total_effect(capability, None, "handoff", False)
        block = TrustedBlock("kashbot." + source, "1.0.0", effect,
                             total_residency(effect, capability, "handoff", source),
                             capability, None, "handoff", False, "production", host_source=source)
        key = (block.kind, block.version)
        if key in result:
            raise RuntimeError(f"duplicate registry seed: {key!r}")
        result[key] = block
    return result


REGISTRY = _seed_registry()
_REGISTRY_LOCK = Lock()


def lookup(kind, version):
    if not isinstance(kind, str) or not isinstance(version, str):
        return None
    return REGISTRY.get((kind, version))


def register_extension(manifest):
    from lab_validate import ManifestInvalid, validate_returns_schema, _SCHEMA

    def fail(field, code="lab_extension_incomplete"):
        raise ManifestInvalid(code, "/" + field, manifest.get("id"))

    if not isinstance(manifest, dict):
        raise ManifestInvalid("lab_extension_incomplete", "")
    block_id = manifest.get("id")
    if (not isinstance(block_id, str) or len(block_id) > 64
            or not re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", block_id)):
        fail("id", "lab_manifest_invalid")
    capability = manifest.get("capability")
    if isinstance(capability, str) and (capability == "money" or capability.startswith("money.")):
        fail("capability", "lab_manifest_unregistrable")
    schema = manifest.get("schema")
    if (not isinstance(schema, dict) or type(schema.get("version")) not in (str, int)
            or not schema["version"]):
        fail("schema")
    for field in ("fixtures", "samples"):
        if not isinstance(manifest.get(field), list) or not manifest[field]:
            fail(field)
    if not isinstance(manifest.get("ui_schema"), dict):
        fail("ui_schema")
    latency = manifest.get("latency")
    if not isinstance(latency, dict) or any(type(latency.get(key)) is not int or
            not 1 <= latency[key] <= 60000 for key in ("p50_ms", "p95_ms")):
        fail("latency")
    migration = manifest.get("migration")
    if (not isinstance(migration, dict) or migration.get("policy") not in ("none", "rewrite", "reject")
            or ("from_version" in migration and not isinstance(migration["from_version"], str))):
        fail("migration")
    kind, version = manifest.get("kind"), manifest.get("version")
    if isinstance(version, dict):
        version = version.get("semver")
    if not isinstance(kind, str) or not kind:
        fail("kind")
    if not isinstance(version, str) or not re.fullmatch(
            _SCHEMA["properties"]["version"]["properties"]["semver"]["pattern"], version):
        fail("version", "lab_manifest_invalid")
    execution = manifest.get("execution_class", "inline")
    adapter, live = manifest.get("solar_adapter"), manifest.get("aps_live", False)
    effect = total_effect(capability, adapter, execution, live)
    if effect == "refused":
        fail("capability", "lab_manifest_unregistrable")
    if "effect" in manifest and manifest["effect"] != effect:
        fail("effect", "lab_manifest_spoof")
    host_source = manifest.get("host_source")
    residency = total_residency(effect, capability, execution, host_source)
    if "residency" in manifest and manifest["residency"] != residency:
        fail("residency", "lab_manifest_spoof")
    host = manifest.get("host", "production")
    if host not in ("production", "pc"):
        fail("host")
    returns_schema_digest = None
    if "returns_schema" in manifest:
        try:
            returns_schema_digest = validate_returns_schema(manifest["returns_schema"])
        except ManifestInvalid as error:
            raise ManifestInvalid(error.code, "/returns_schema" + error.pointer,
                                  manifest.get("id")) from error
    block = TrustedBlock(kind, version, effect, residency, capability, adapter, execution, live, host,
                         returns_schema_digest, host_source=host_source)
    with _REGISTRY_LOCK:
        if (kind, version) in REGISTRY:
            fail("kind", "lab_manifest_duplicate")
        REGISTRY[(kind, version)] = block
    return block


class LatencyHistogram:
    """32 upper bounds in microseconds; overflow clamps to the final bucket."""
    buckets = tuple(64 * 2 ** index for index in range(32))

    def __init__(self):
        self.counts = [0] * 32
        self.dropped = 0

    def observe(self, us):
        if (isinstance(us, bool) or not isinstance(us, (int, float))
                or (isinstance(us, float) and not math.isfinite(us)) or us < 0):
            self.dropped += 1
            return
        self.counts[min(bisect_left(self.buckets, us), 31)] += 1

    def p95(self):
        target = math.ceil(sum(self.counts) * .95)
        if target == 0:
            return 0
        count = 0
        for upper, observations in zip(self.buckets, self.counts):
            count += observations
            if count >= target:
                return upper
