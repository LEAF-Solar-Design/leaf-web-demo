"""Bounded, tenant-local graph output resolution. No host adapters or I/O."""
from __future__ import annotations

import json
from collections import defaultdict

from jsonschema import Draft202012Validator
from referencing import Registry

import lab_artifacts as ledger
from lab_ops import (UNDEFINED, STREAM_ADAPTERS, OperatorRefused, OperatorUnavailable,
                     _field, has_operator, run_operator)
from lab_registry import lookup
from lab_widget_doc import InvalidWidget, validate_binding

MAX_BINDINGS = 32
MAX_ROWS = 200
MAX_VALUE_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8"))


class Unavailable(LookupError):
    pass


def validate_bindings(bindings):
    if not isinstance(bindings, list) or len(bindings) > MAX_BINDINGS:
        raise InvalidWidget("At most 32 bindings are supported.", "/bindings")
    for index, binding in enumerate(bindings):
        validate_binding(binding, f"/bindings/{index}")
        if binding["source"] != "graph.output":
            raise InvalidWidget("Unknown binding.", f"/bindings/{index}/source")
    return bindings


class _Graph:
    def __init__(self, graph):
        self.blocks = {block["id"]: block for block in graph["blocks"]}
        self.outputs = graph["outputs"]
        self.incoming = defaultdict(list)
        for pipe in graph["pipes"]:
            self.incoming[pipe["to"]["node"]].append(pipe)
        self.cache = {}
        self.visiting = set()

    def port(self, node, port):
        block = self.blocks.get(node)
        if block is None:
            raise Unavailable("missing-port")
        ref = block["registry"]
        trusted = lookup(ref["id"], ref["version"])
        manifest = block.get("manifest")
        embedded = lookup(manifest.get("kind"), ref["version"]) if manifest is not None else None
        # Both identities restrict execution independently. Neither may erase
        # the other's host residency or effect restriction.
        if (any(entry is not None and entry.residency == "host" for entry in (trusted, embedded))
                or (manifest is not None and manifest.get("residency") == "host")):
            raise Unavailable("host-resident")
        if embedded is not None and embedded.effect != "pure":
            raise Unavailable("execution-unavailable")
        # Without a manifest only the fixed operator's result port is known.
        ports = manifest.get("outputs", {}) if manifest is not None else (
            {"result": {}} if has_operator(ref["id"]) else {})
        if port not in ports:
            raise Unavailable("missing-port")
        return block, manifest, trusted, ports[port]

    def visit(self, node, port):
        key = (node, port)
        if key in self.cache:
            result = self.cache[key]
            if isinstance(result, Unavailable):
                raise result
            return result
        if node in self.visiting:
            raise Unavailable("graph-cycle")
        block, manifest, trusted, pin = self.port(node, port)
        op = block["registry"]["id"]
        if (not has_operator(op) or (trusted is not None and trusted.effect != "pure")
                or (manifest is not None and manifest.get("effect") != "pure")):
            raise Unavailable("execution-unavailable")
        self.visiting.add(node)
        try:
            inputs = {}
            stream = False
            for pipe in self.incoming[node]:
                source = pipe["from"]
                _, _, _, source_pin = self.port(source["node"], source["port"])
                stream |= source_pin.get("mode") == "stream"
                target = pipe["to"]["port"]
                if target in inputs:
                    raise Unavailable("ambiguous-input")
                inputs[target] = self.visit(source["node"], source["port"])
            if manifest is not None:
                for name, input_pin in manifest.get("inputs", {}).items():
                    if name not in inputs:
                        if input_pin.get("has_default"):
                            inputs[name] = input_pin["default"]
                        elif input_pin.get("required"):
                            raise Unavailable("missing-input")
            if stream and op not in STREAM_ADAPTERS and pin.get("mode") != "stream":
                raise Unavailable("stream-adapter-required")
            result = run_operator(op, inputs, block["settings"])
            if result is UNDEFINED:
                raise Unavailable("missing-value")
            if "schema" in pin:
                try:
                    validator = Draft202012Validator(pin["schema"], registry=Registry())
                    items = result if pin.get("mode") == "stream" else [result]
                    if not isinstance(items, list):
                        raise ValueError("invalid_stream")
                    for item in items:
                        if item is not None or not pin.get("nullable"):
                            validator.validate(item)
                except Exception:
                    raise Unavailable("output-refused") from None
            # Operator output is data only; malformed/nonfinite output never leaks.
            _bytes(result)
            self.cache[key] = result
            return result
        except (OperatorRefused, OperatorUnavailable, ValueError, TypeError, OverflowError):
            error = Unavailable("operator-refused")
            self.cache[key] = error
            raise error from None
        except Unavailable as error:
            self.cache[key] = error
            raise
        finally:
            self.visiting.remove(node)


def _bounded(value):
    truncated = False

    def rows(item):
        nonlocal truncated
        if isinstance(item, list):
            truncated |= len(item) > MAX_ROWS
            return [rows(child) for child in item[:MAX_ROWS]]
        if isinstance(item, dict):
            return {key: rows(child) for key, child in item.items()}
        return item

    bounded = rows(value)
    return bounded, truncated


def _fit(result):
    if _bytes(result) <= MAX_VALUE_BYTES:
        return result
    result["truncated"] = True
    value = result["value"]
    if isinstance(value, (list, str)):
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            result["value"] = value[:middle]
            if _bytes(result) <= MAX_VALUE_BYTES:
                low = middle
            else:
                high = middle - 1
        result["value"] = value[:low]
    else:
        result["value"] = None
    if _bytes(result) > MAX_VALUE_BYTES:
        return {"status": "unavailable", "reason": "value-limit", "truncated": True}
    return result


def resolve_values(tenant, bindings):
    validate_bindings(bindings)
    documents = {}
    # Resolve each head once, even if the document is missing.
    for binding in bindings:
        doc_id = binding["doc_id"]
        if doc_id not in documents:
            try:
                revision = ledger.get(tenant, doc_id)
                documents[doc_id] = (revision, _Graph(revision["graph"])) if revision["kind"] == "graph" else None
            except ledger.NotFound:
                documents[doc_id] = None
    response = {"values": [{"status": "unavailable", "reason": "response-limit", "truncated": True}
                           for _ in bindings], "truncated": False}
    for index, binding in enumerate(bindings):
        try:
            document = documents[binding["doc_id"]]
            if document is None:
                raise Unavailable("missing-document")
            revision, graph = document
            output = graph.outputs.get(binding["output"])
            if output is None:
                raise Unavailable("missing-output")
            _, _, _, port = graph.port(output["node"], output["port"])
            value = graph.visit(output["node"], output["port"])
            if "field" in binding:
                value = _field(value, binding["field"])
            value, cut = _bounded(value)
            result = _fit({"status": "resolved", "value": value, "truncated": cut, "pins": {
                "doc_id": binding["doc_id"], "graph_revision": revision["revision"],
                "graph_sha256": revision["sha256"],
                "output": {"name": binding["output"], **output}, "schema": port.get("schema"),
            }})
        except Unavailable as error:
            result = {"status": "unavailable", "reason": str(error), "truncated": False}
        except OperatorRefused:
            result = {"status": "unavailable", "reason": "missing-field", "truncated": False}
        response["values"][index] = result
        if _bytes(response) > MAX_RESPONSE_BYTES - 1:
            result = {"status": "unavailable", "reason": "response-limit", "truncated": True}
            response["values"][index] = result
        response["truncated"] |= result["truncated"]
    return response
