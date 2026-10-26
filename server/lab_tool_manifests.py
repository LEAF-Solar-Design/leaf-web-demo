"""One authority for Lab tool nodes: manifest, trusted block and submitted row come from the same projection, pinned by code_sha256."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import re
from threading import Lock

from jsonschema import Draft202012Validator

import deps
import lab_evaluate
import lab_registry


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    version: str
    source: str
    code_sha256: str
    manifest: dict | None
    trusted: lab_registry.TrustedBlock | None
    params_by_port: dict
    reason: str | None
    result_encoding: str | None


_CACHE = OrderedDict()
_CACHE_LOCK = Lock()
_MANIFEST_SCHEMA = "fixtures/lab/leaf-block-manifest.v1.schema.json"


def _valid_id(value):
    return (isinstance(value, str) and 1 <= len(value) <= 64
            and re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", value) is not None)


def _sample(schema):
    if "default" in schema:
        candidate = schema["default"]
    elif "enum" in schema:
        candidate = schema["enum"][0]
    else:
        kind = schema["type"]
        if isinstance(kind, list):
            kind = next((item for item in kind if item != "null"), "null")
        candidate = {"string": "", "integer": schema.get("minimum", 0),
                     "number": schema.get("minimum", 0), "boolean": False,
                     "array": [], "object": {}, "null": None}[kind]
    Draft202012Validator(schema).validate(candidate)
    return candidate


def _lab_schema(schema):
    """Copy a schema, removing annotations unsupported by Lab admission."""
    if not isinstance(schema, dict):
        raise ValueError("schema required")
    result = {key: value for key, value in schema.items() if key != "default"}
    if isinstance(result.get("properties"), dict):
        result["properties"] = {key: _lab_schema(value)
                                for key, value in result["properties"].items()}
    if "items" in result:
        result["items"] = _lab_schema(result["items"])
    lab_evaluate.check_embedded(result)
    return result


def _pin(schema, *, required=False, output=False):
    pin = {"mode": "scalar", "schema": _lab_schema(schema), "unit": "none",
           "nullable": not output and isinstance(schema["type"], list) and "null" in schema["type"],
           "required": required, "has_default": not output and "default" in schema}
    if pin["has_default"]:
        pin["default"] = schema["default"]
    return pin


def project_tool(row, source):
    """Project one row; a malformed row is refused alone, never a tenant-wide failure."""
    try:
        return _project_tool(row, source)
    except Exception:
        text = lambda key: row.get(key) if isinstance(row, dict) and isinstance(row.get(key), str) else ""
        return ToolDescriptor(text("name"), text("version"), source, "", None, None,
                              lab_evaluate._copy({}, frozen=True), "malformed tool row", None)


def _project_tool(row, source):
    # Snapshot caller-owned data before constructing cached manifests.
    row = json.loads(json.dumps(row, allow_nan=False))
    name, version = row.get("name"), row.get("version")
    digest = deps.catalog_tool_digest(row).removeprefix("sha256:")

    def refused(reason):
        return ToolDescriptor(name, version, source, digest, None, None,
                              lab_evaluate._copy({}, frozen=True), reason, None)

    capabilities, capability = row.get("capabilities", []), row.get("capability")
    if (not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities)
            or (capability is not None and not isinstance(capability, str))):
        return refused("malformed tool row")
    if row.get("engine") == "server-builtin":
        return refused("runs on the project graph, not as a Lab node")
    capabilities = row.get("capabilities", [])
    all_capabilities = [row.get("capability"), *capabilities]
    if source == "write_seed" or any(
            isinstance(capability, str) and (capability == prefix or capability.startswith(prefix + "."))
            for capability in all_capabilities for prefix in ("drawing.write", "solve", "cloud-proposal")):
        return refused("writes are not Lab nodes")
    if row.get("aps_live") is True:
        return refused("live APS tools are not Lab nodes")
    if row.get("kind") != "script":
        return refused("only script tools become Lab nodes")
    if row.get("execution_class", "inline") not in ("inline", "worker"):
        return refused("execution class is not a Lab node")
    capability = row.get("capability", next(iter(capabilities), "pure"))
    effect = lab_registry.total_effect(capability, row.get("solar_adapter"), "worker", False)
    if effect not in ("compute", "read") or capability == "money" or capability.startswith("money."):
        return refused("not a read-only drawing tool")
    validator = lab_evaluate._contract_validators()[_MANIFEST_SCHEMA]
    semver = validator.schema["properties"]["version"]["properties"]["semver"]["pattern"]
    if not _valid_id(name) or not isinstance(version, str) or not re.fullmatch(semver, version):
        return refused("tool name or version is not a Lab id")
    if any(manifest["id"] == name for manifest in lab_evaluate.trusted_registry().values()):
        return refused("name collides with a built-in block")
    params = row.get("params")
    if (not isinstance(params, dict) or params.get("type") != "object"
            or not isinstance(params.get("properties", {}), dict)):
        return refused("parameters lack Lab types")
    try:
        for schema in params.get("properties", {}).values():
            _lab_schema(schema)
    except Exception:
        return refused("parameters lack Lab types")
    inputs, params_by_port = {}, {}
    for param, schema in params.get("properties", {}).items():
        if param == "drawing_id":
            continue
        port = param.replace("_", "-")
        if "-" in param or not _valid_id(port) or port in params_by_port:
            return refused("parameter name is not a Lab port id")
        params_by_port[port] = param
        inputs[port] = _pin(schema, required=param in params.get("required", []))
    output_schema = {"type": "string", "maxLength": 65536,
                     "description": "JSON text of the tool result"}
    result_encoding = "json-text"
    for key in ("returns_schema", "returns"):
        try:
            _lab_schema(row.get(key))
        except Exception:
            continue
        output_schema = row[key]
        result_encoding = "value"
        break
    outputs = {"result": _pin(output_schema, output=True)}
    try:
        sample_input = {port: _sample(params["properties"][param])
                        for port, param in params_by_port.items()}
        sample_output = {"result": _sample(output_schema)}
    except Exception:
        return refused("no valid sample")
    description = row.get("description", "")
    first = description.splitlines()[0].strip()[:120] if isinstance(description, str) and description.strip() else ""
    # Titles carry plain words only, never a path, URL or drive letter.
    title = first if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ,.'()&+-]*", first) and ".." not in first else ""
    permissions = list(dict.fromkeys(item for item in capabilities if isinstance(item, str)
                                    and re.fullmatch(r"[a-z][a-z0-9]*([.-][a-z0-9]+)*", item)))
    manifest = {
        "schema_version": 1, "id": name, "title": title or name, "profile": "lab-registry",
        "version": {"semver": version, "code_sha256": digest}, "kind": "builtin",
        "inputs": inputs, "outputs": outputs,
        "settings_schema": {"type": "object", "additionalProperties": False, "required": ["drawing"],
                            "properties": {"drawing": {
                                "type": "object", "additionalProperties": False,
                                "required": ["project_id", "drawing_id", "dwg_version"],
                                "properties": {
                                    "project_id": {"type": "string", "minLength": 1, "maxLength": 200},
                                    "drawing_id": {"type": "string", "minLength": 1, "maxLength": 200},
                                    "dwg_version": {"type": "integer", "minimum": 1}}}}},
        "ui_schema": {}, "sample_input": sample_input, "sample_output": sample_output,
        # The run wall bounds these budgets; these are not measured latencies.
        "latency_budget_us": {"profile": "legacy-us-to-std4-ns-ms-v1", "p50": 10000000, "p99": 600000000},
        "lane": "processing", "effect": "read", "execution_target": "runner", "residency": "tenant",
        "permissions": permissions, "state": "stateless", "tests": ["lab-tool-node-admission"],
    }
    try:
        validator.validate(manifest)
        lab_evaluate.canonical_json(manifest)
        lab_evaluate._admit_manifest(manifest)
    except Exception:
        return refused("manifest invalid")
    trusted = lab_registry.TrustedBlock(
        name, version, effect, lab_registry.total_residency(effect, capability, "worker", None),
        capability, row.get("solar_adapter"), "worker", False, row.get("host", "production"),
        host_source=None)
    return ToolDescriptor(name, version, source, digest,
                          lab_evaluate._copy(manifest, frozen=True), trusted,
                          lab_evaluate._copy(params_by_port, frozen=True), None, result_encoding)


def _tenant_projection(tenant_id):
    rows = deps.effective_tools_with_provenance(tenant_id)
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    key = (tenant_id, hashlib.sha256(encoded.encode("utf-8")).hexdigest())
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
        snapshot = json.loads(encoded)
        pairs = sorted(((project_tool(row, source), row) for row, source in snapshot),
                       key=lambda pair: pair[0].name)
        # Retain exact JSON numbers in submitted rows; runtime _copy coerces
        # large integers to binary64 for graph values.
        result = tuple((descriptor, json.dumps(row, allow_nan=False)) for descriptor, row in pairs)
        _CACHE[key] = result
        if len(_CACHE) > 64:
            _CACHE.popitem(last=False)
        return result


def tenant_tool_projection(tenant_id):
    return tuple(descriptor for descriptor, _ in _tenant_projection(tenant_id))


def registry_for(tenant_id):
    registry = dict(lab_evaluate.trusted_registry())
    registry.update({f"{tool.name}@{tool.version}": tool.manifest
                     for tool in tenant_tool_projection(tenant_id) if tool.manifest is not None})
    return registry


def trusted_for(tenant_id, block_id, version):
    for tool in tenant_tool_projection(tenant_id):
        if tool.name == block_id and tool.version == version and tool.trusted is not None:
            return tool.trusted
    return lab_registry.lookup(block_id, version)


def row_for(tenant_id, block_id, version, code_sha256):
    for tool, row in _tenant_projection(tenant_id):
        if (tool.manifest is not None and tool.name == block_id and tool.version == version
                and tool.code_sha256 == code_sha256):
            return json.loads(row), dict(tool.params_by_port)
    return None
