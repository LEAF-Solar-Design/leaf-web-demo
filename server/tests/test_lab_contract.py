"""Lab reference validation and shared browser conformance, without a running host."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import warnings

import pytest
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contract"
FIXTURES = CONTRACT / "fixtures/lab"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def canonical_json(value):
    """JCS: binary64, shortest decimal, UTF-16 key order, no Unicode normalization."""
    active = set()

    def encode(item):
        if item is None or isinstance(item, bool):
            return json.dumps(item)
        if isinstance(item, (int, float)):
            try:
                number = float(item)
            except OverflowError as error:
                raise ValueError("Test: finite number required") from error
            if not math.isfinite(number):
                raise ValueError("Test: finite number required")
            if number == 0:
                return "0"
            # Find the shortest round-tripping decimal. Decimal position below
            # uses ECMAScript's fixed/exponential thresholds, not Python's.
            text = next(format(number, f".{precision}g") for precision in range(1, 18)
                        if float(format(number, f".{precision}g")) == number)
            sign = "-" if text.startswith("-") else ""
            text = text.lstrip("-")
            mantissa, _, exponent = text.lower().partition("e")
            whole, _, fraction = mantissa.partition(".")
            digits = whole + fraction
            position = len(whole) + int(exponent or 0)
            leading = len(digits) - len(digits.lstrip("0"))
            digits = digits.lstrip("0").rstrip("0")
            position -= leading
            if 0 < position <= 21:
                result = (digits + "0" * (position - len(digits)) if position >= len(digits)
                          else digits[:position] + "." + digits[position:])
            elif -6 < position <= 0:
                result = "0." + "0" * -position + digits
            else:
                power = position - 1
                result = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
                result += "e" + ("+" if power >= 0 else "") + str(power)
            return sign + result
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if not isinstance(item, (list, dict)) or id(item) in active:
            raise ValueError("Test: JSON required")
        active.add(id(item))
        try:
            if isinstance(item, list):
                return "[" + ",".join(encode(child) for child in item) + "]"
            if any(not isinstance(key, str) for key in item):
                raise ValueError("Test: JSON keys required")
            keys = sorted(item, key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(encode(key) + ":" + encode(item[key]) for key in keys) + "}"
        finally:
            active.remove(id(item))

    return encode(value)


SCHEMAS = {path.name: json.loads(path.read_text(encoding="utf-8"))
           for path in CONTRACT.glob("lab-*.schema.json")}
SCHEMAS["fixtures/lab/leaf-block-manifest.v1.schema.json"] = load("leaf-block-manifest.v1.schema.json")
REGISTRY = Registry().with_resources(
    (uri, Resource.from_contents(schema)) for name, schema in SCHEMAS.items()
    for uri in (schema["$id"], "https://leaf.internal/contract/" + name))
BLOCKS = load("blocks.json")["manifests"]
TRUSTED = {f"{block['id']}@{block['version']['semver']}": block for block in BLOCKS}
GRAPH = load("graphs/first-cut.json")
GOLDEN = load("lab-embed.v1.golden.json")
CASES = load("ops-conformance.json")["cases"]
PORT_CASES = load("port-compat.v1.json")["cases"]
VECTORS = load("digest-vectors.json")["vectors"]


def validate_schema(value, name):
    canonical_json(value)
    Draft202012Validator(SCHEMAS[name], registry=REGISTRY).validate(value)


def validate_value(value, schema):
    canonical_json(value)
    Draft202012Validator(schema, registry=REGISTRY).validate(value)


EMBEDDED = {"type", "const", "enum", "required", "properties", "additionalProperties", "items",
            "minItems", "maxItems", "minimum", "maximum", "minLength", "maxLength", "title", "description"}


def check_embedded(schema):
    if not isinstance(schema, dict) or "type" not in schema or set(schema) - EMBEDDED:
        raise ValueError("Test: unsupported schema")
    Draft202012Validator.check_schema(schema)
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise ValueError("Test: closed settings required")
    if "properties" in schema:
        for child in schema["properties"].values():
            check_embedded(child)
    if "items" in schema:
        check_embedded(schema["items"])
    if "object" in types:
        if not isinstance(schema.get("properties"), dict) or schema.get("additionalProperties") is not False:
            raise ValueError("Test: closed settings required")
        if set(schema.get("required", [])) - set(schema["properties"]):
            raise ValueError("Test: required setting missing")
    if "array" in types:
        check_embedded(schema.get("items"))


def validate_block(manifest, trusted, previous=None):
    validate_schema(manifest, "lab-block.v1.schema.json")
    if canonical_json(manifest) != canonical_json(trusted):
        raise ValueError("Test: registry mismatch")
    check_embedded(manifest["settings_schema"])
    if set(manifest["ui_schema"]) - {"order", "labels"}:
        raise ValueError("Test: presentation refused")
    if manifest["latency_budget_us"]["p50"] > manifest["latency_budget_us"]["p99"]:
        raise ValueError("Test: latency bounds refused")
    for direction in ("inputs", "outputs"):
        pins = manifest[direction]
        for pin in pins.values():
            check_embedded(pin["schema"])
            types = pin["schema"]["type"] if isinstance(pin["schema"]["type"], list) else [pin["schema"]["type"]]
            if not pin["nullable"] and "null" in types:
                raise ValueError("Test: nullability mismatch")
            if pin["has_default"] and not (pin["default"] is None and pin["nullable"]):
                validate_value(pin["default"], pin["schema"])
        sample_schema = {
            "type": "object", "additionalProperties": False,
            "properties": {name: {"anyOf": [pin["schema"], {"type": "null"}]} if pin["nullable"]
                           else pin["schema"] for name, pin in pins.items()},
            "required": [name for name, pin in pins.items() if direction == "outputs"
                         or (pin["required"] and not pin["has_default"])],
        }
        validate_value(manifest["sample_input" if direction == "inputs" else "sample_output"], sample_schema)
    if previous and (previous["id"] != manifest["id"] or any(
            name not in manifest["outputs"] or canonical_json(pin) != canonical_json(manifest["outputs"][name])
            for name, pin in previous["outputs"].items())):
        raise ValueError("Test: output changed")


def compatible(output, input_pin):
    if (output["mode"] != input_pin["mode"] or output["unit"] != input_pin["unit"]
            or (output["nullable"] and not input_pin["nullable"])):
        return False
    a, b = output["schema"], input_pin["schema"]
    if canonical_json(a) == canonical_json(b):
        return True
    if (set(a) | set(b)) - {"type", "minimum", "maximum", "enum"}:
        return False
    if not (a["type"] == b["type"] or (a["type"] == "integer" and b["type"] == "number")):
        return False
    if "minimum" in b and ("minimum" not in a or a["minimum"] < b["minimum"]):
        return False
    if "maximum" in b and ("maximum" not in a or a["maximum"] > b["maximum"]):
        return False
    return "enum" not in b or ("enum" in a and all(
        any(canonical_json(value) == canonical_json(item) for item in b["enum"]) for value in a["enum"]))


def validate_graph(document, registry, previous=None, wire_bytes=None):
    validate_schema(document, "lab-graph.v1.schema.json")
    size = len(canonical_json(document).encode())
    if size > 262144 or (wire_bytes is not None and (
            not nonnegative_integer(wire_bytes) or wire_bytes > 262144)):
        raise ValueError("Test: graph byte limit")
    nodes = {}
    for node in document["blocks"]:
        if node["id"] in nodes:
            raise ValueError("Test: duplicate block")
        ref = node["registry"]
        manifest = registry.get(f"{ref['id']}@{ref['version']}")
        if not manifest or manifest["version"]["code_sha256"] != ref["code_sha256"]:
            raise ValueError("Test: registry mismatch")
        validate_block(manifest, manifest)
        validate_value(node["settings"], manifest["settings_schema"])
        nodes[node["id"]] = manifest
    adjacency = {node: [] for node in nodes}
    attached = set()

    def pin(endpoint, direction):
        result = nodes.get(endpoint["node"], {}).get(direction, {}).get(endpoint["port"])
        if result is None:
            raise ValueError("Test: pin missing")
        return result

    for pipe in document["pipes"]:
        if not compatible(pin(pipe["from"], "outputs"), pin(pipe["to"], "inputs")):
            raise ValueError("Test: incompatible pins")
        target = (pipe["to"]["node"], pipe["to"]["port"])
        if target in attached:
            raise ValueError("Test: duplicate input")
        attached.add(target)
        adjacency[pipe["from"]["node"]].append(pipe["to"]["node"])
    for node, manifest in nodes.items():
        for name, input_pin in manifest["inputs"].items():
            if input_pin["required"] and not input_pin["has_default"] and (node, name) not in attached:
                raise ValueError("Test: input required")
    done, stack = set(), []

    def visit(node):
        if node in stack:
            raise ValueError("Test: cycle " + " -> ".join(stack[stack.index(node):] + [node]))
        if node in done:
            return
        stack.append(node)
        for child in adjacency[node]:
            visit(child)
        stack.pop()
        done.add(node)

    for node in nodes:
        visit(node)
    for endpoint in document["outputs"].values():
        pin(endpoint, "outputs")
    if previous and any(slug not in document["outputs"] or document["outputs"][slug] != endpoint
                        for slug, endpoint in previous["outputs"].items()):
        raise ValueError("Test: output changed")


def nonnegative_integer(value):
    # Parsed JSON numbers such as 0.0 are integers in both schema implementations.
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 9007199254740991 and value == int(value))


def validate_embed(message, context, state):
    validate_schema(message, "lab-embed.v1.schema.json")
    if (context["authenticated"] is not True or not context["origin"] or context["origin"] == "*"
            or context["eventOrigin"] != context["origin"] or not context["source"]
            or context["eventSource"] != context["source"]):
        raise ValueError("Test: host mismatch")
    for key in ("channel", "principal_proof", "source_sha", "proposal_revision", "proposal_hash"):
        if message[key] != context[key]:
            raise ValueError("Test: host binding mismatch")
    now = context["now_ms"]
    if (not nonnegative_integer(now) or not nonnegative_integer(state["sequence"])
            or not isinstance(state["accepted_at_ms"], list)
            or any(not nonnegative_integer(time) or time > now for time in state["accepted_at_ms"])
            or message["sent_at_ms"] > now or message["expires_at_ms"] <= now
            or message["expires_at_ms"] <= message["sent_at_ms"]
            or message["expires_at_ms"] - message["sent_at_ms"] > 30000
            or message["sequence"] <= state["sequence"]):
        raise ValueError("Test: replay refused")
    limit = 262144 if message["kind"] == "source.value" else 4096
    wire_bytes = context.get("wire_bytes", 0)
    if (len(canonical_json(message).encode()) > limit
            or not nonnegative_integer(wire_bytes) or wire_bytes > limit):
        raise ValueError("Test: message byte limit")
    recent = [time for time in state["accepted_at_ms"] if time > now - 1000]
    if len(recent) >= 20:
        raise ValueError("Test: message rate limit")
    state["sequence"] = message["sequence"]
    state["accepted_at_ms"] = recent + [now]


def scenario(case):
    """Materialize the same fixture operations for both validators."""
    if case["target"] == "graph":
        value = copy.deepcopy(GRAPH)
        if "generate" in case:
            count = case["generate"]["blocks"]
            value["blocks"] = [dict(copy.deepcopy(GRAPH["blocks"][0]), id=f"block-{i}") for i in range(count)]
            pipes = [{"from": {"node": f"block-{i - distance}", "port": "result"},
                      "to": {"node": f"block-{i}", "port": port}}
                     for i in range(1, count) for distance, port in enumerate(("value", "auxiliary", "extra"), 1)
                     if i >= distance]
            value["pipes"] = pipes[:case["generate"]["pipes"]]
            value["outputs"] = {}
    elif case["target"] == "block":
        value = copy.deepcopy(BLOCKS[0])
    else:
        value = {"message": copy.deepcopy(GOLDEN["messages"][0]),
                 "context": copy.deepcopy(GOLDEN["context"]), "state": copy.deepcopy(GOLDEN["state"])}
    for change in case["changes"]:
        parts = change["path"].strip("/").split("/")
        parent = value
        for part in parts[:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
        if change["op"] == "delete":
            del parent[key]
        elif change["op"] == "append":
            parent[key].append(copy.deepcopy(change["value"]))
        else:
            parent[key] = copy.deepcopy(change["value"])
    return value


def run_case(case, value):
    if case["target"] == "graph":
        registry = copy.deepcopy(TRUSTED)
        if "registry_input" in case:
            registry["level-fast-index@1.0.0"]["inputs"]["value"] = case["registry_input"]
        validate_graph(value, registry, GRAPH if case.get("previous") else None, case.get("wire_bytes"))
    elif case["target"] == "block":
        trusted = BLOCKS[0] if case["name"].startswith("spoof-") else value
        validate_block(value, trusted, BLOCKS[0] if case.get("previous") else None)
    else:
        validate_embed(value["message"], value["context"], value["state"])


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_shared_conformance(case):
    value = scenario(case)
    if case["expected"]:
        run_case(case, value)
    else:
        original = copy.deepcopy(value)
        with pytest.raises((ValueError, ValidationError, SchemaError)) as caught:
            run_case(case, value)
        if case.get("contains"):
            assert case["contains"] in str(caught.value)
        if case["target"] == "embed":
            assert value["state"] == original["state"], "Refusals must not consume replay state"


@pytest.mark.parametrize("case", PORT_CASES, ids=lambda case: case["name"])
def test_pin_compatibility(case):
    for pin in (case["output"], case["input"]):
        validate_schema(pin, "lab-pin.v1.schema.json")
        check_embedded(pin["schema"])
    assert compatible(case["output"], case["input"]) is case["expected"]


def test_frozen_sources_and_schemas():
    for source in load("SOURCES.json")["sources"]:
        data = (ROOT / source["frozen_path"]).read_bytes()
        assert len(data) == source["bytes"]
        assert hashlib.sha256(data).hexdigest() == source["sha256"]
    for schema in SCHEMAS.values():
        Draft202012Validator.check_schema(schema)
    lanes = {lane["id"] for lane in load("lanes.json")["lanes"]}
    words = {term["plain"] for term in load("glossary.json")["terms"]}
    assert len(lanes) == 9
    assert len(TRUSTED) == len(BLOCKS)
    assert {block["kind"] for block in BLOCKS} == {"builtin", "settings", "rust"}
    for block in BLOCKS:
        assert block["lane"] in lanes
        assert block["title"] in words
        validate_block(block, block)


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_digest_bytes(vector):
    text = canonical_json(vector["input"])
    assert text == vector["canonical"]
    assert text.encode().hex() == vector["utf8_hex"]
    assert hashlib.sha256(text.encode()).hexdigest() == vector["sha256"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "\ud800", "\udfff"])
def test_canonical_refusals(value):
    with pytest.raises((ValueError, UnicodeError)):
        canonical_json(value)


def test_golden_protocol_and_replay():
    state = copy.deepcopy(GOLDEN["state"])
    for sequence, original in enumerate(GOLDEN["messages"], 1):
        message = dict(original, sequence=sequence)
        validate_embed(message, GOLDEN["context"], state)
        with pytest.raises(ValueError, match="replay"):
            validate_embed(message, GOLDEN["context"], state)


def test_actual_bytes_not_only_reported_length():
    document = copy.deepcopy(GRAPH)
    document["blocks"][0]["settings"] = {"padding": "é" * 131073}
    with pytest.raises(ValueError, match="byte limit"):
        validate_graph(document, TRUSTED)
    message = dict(GOLDEN["messages"][3], payload={"rows": [{"padding": "é" * 131073}]})
    with pytest.raises(ValueError, match="byte limit"):
        validate_embed(message, GOLDEN["context"], copy.deepcopy(GOLDEN["state"]))


def test_javascript_twin():
    node = shutil.which("node")
    if node is None:
        warnings.warn("DEFERRED: Lab JavaScript conformance requires node on PATH", RuntimeWarning)
        return
    script = r"""
import {readFileSync} from 'node:fs';
import {createHash, webcrypto} from 'node:crypto';
import {canonicalJSON, compatible, createContract, digest} from './web/src/lab/contract.js';
globalThis.crypto ??= webcrypto;
const data = JSON.parse(readFileSync(0, 'utf8'));
const contract = createContract(data.schemas);
const clone = value => structuredClone(value);
const results = data.cases.map(({case: test, value}) => {
  const state = test.target === 'embed' ? canonicalJSON(value.state) : null;
  try {
    if (test.target === 'graph') {
      const registry = clone(data.registry);
      if (test.registry_input) registry['level-fast-index@1.0.0'].inputs.value = test.registry_input;
      contract.graph(value, registry, test.previous ? data.graph : null, test.wire_bytes);
    }
    else if (test.target === 'block') contract.block(value, test.name.startsWith('spoof-') ? data.blocks[0] : value, test.previous ? data.blocks[0] : null);
    else contract.embed(value.message, value.context, value.state);
    return {name:test.name, accepted:true};
  } catch (error) {
    return {name:test.name, accepted:false, message:error.message, unchanged:state === null || state === canonicalJSON(value.state)};
  }
});
for (const block of data.blocks) contract.block(block, block);
for (const message of data.golden.messages) contract.embed(message, data.golden.context, clone(data.golden.state));
const vectors = data.vectors.map(vector => {
  const text = canonicalJSON(vector.input);
  return {canonical:text, utf8_hex:Buffer.from(text).toString('hex'), sha256:createHash('sha256').update(text).digest('hex')};
});
for (const vector of data.vectors) {
  if (await digest(vector.input) !== vector.sha256) throw Error('Web Crypto digest mismatch');
}
const ports = data.ports.map(test => {
  contract.schema(test.input, 'lab-pin.v1.schema.json');
  contract.schema(test.output, 'lab-pin.v1.schema.json');
  return compatible(test.output, test.input);
});
for (const value of [NaN, Infinity, -Infinity, '\ud800', '\udfff', undefined, [,]]) {
  let refused = false;
  try { canonicalJSON(value); } catch { refused = true; }
  if (!refused) throw Error('Canonical refusal missing');
}
process.stdout.write(JSON.stringify({results,vectors,ports}));
"""
    payload = {"schemas": SCHEMAS, "registry": TRUSTED, "blocks": BLOCKS, "graph": GRAPH,
               "golden": GOLDEN, "vectors": VECTORS, "ports": PORT_CASES,
               "cases": [{"case": case, "value": scenario(case)} for case in CASES]}
    result = subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT,
                            input=json.dumps(payload), text=True, encoding="utf-8",
                            capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    for case, outcome in zip(CASES, actual["results"], strict=True):
        assert outcome["name"] == case["name"]
        assert outcome["accepted"] is case["expected"], (case["name"], outcome)
        if not case["expected"]:
            assert outcome["unchanged"]
            if case.get("contains"):
                assert case["contains"] in outcome["message"]
    for expected, vector in zip(VECTORS, actual["vectors"], strict=True):
        for key in ("canonical", "utf8_hex", "sha256"):
            assert vector[key] == expected[key], (expected["name"], key)
    assert actual["ports"] == [case["expected"] for case in PORT_CASES]
