"""Validated declarations and projections for the server's solar capabilities."""
import copy
import hashlib
import json
import math
import re
from pathlib import Path

SCHEMA = "leaf.solar-tool.v1"
ADAPTER_KINDS = ("local-graph-commit", "cloud-proposal")
TRUSTED_INPUTS = ()
MAX_DECLARATIONS = 256
_DIRECTORY = Path(__file__).resolve().parent
_KEYS = frozenset((
    "schema", "name", "builtin", "family", "adapter", "entitlement",
    "requires_persisted_graph", "seedable", "invalid_request_code", "readiness",
    "engine", "interaction", "record_store", "record", "ledger", "trusted_inputs",
    "maturity", "wave", "order", "scenario",
))
_PROJECTION_KEYS = frozenset((
    "catalog_digest", "tool_manifest_sha256", "catalog_commit",
    "effective_catalog_digest", "_aps_live_runtime_authorized",
))


class SolarRegistryError(ValueError):
    """A packaged declaration is invalid; registry discovery must fail closed."""


def _require(condition, message):
    if not condition:
        raise SolarRegistryError(message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def _constant(value):
    raise SolarRegistryError("non-finite JSON number: " + value)


def _float(value):
    number = float(value)
    _require(math.isfinite(number), "non-finite JSON number")
    return number


def _json(data):
    return json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant,
                      parse_float=_float)


def _text(value):
    return isinstance(value, str) and bool(value)


def _strings(value):
    return isinstance(value, list) and all(_text(item) for item in value)


def _enum(value, choices, field):
    _require(isinstance(value, str) and value in choices, "invalid " + field)


def _shape(value, keys, field):
    _require(isinstance(value, dict) and set(value) == set(keys), "invalid " + field)


def _interaction(value, record):
    _require(isinstance(value, dict), "invalid interaction")
    mode = value.get("mode")
    _enum(mode, ("form", "none", "pick"), "interaction mode")
    if mode != "pick":
        _shape(value, ("mode",), "interaction")
        return
    _shape(value, ("mode", "pick", "preview", "commit"), "interaction")
    _enum(value["preview"], ("none", "highlight", "dry-run"), "preview")
    _require(value["commit"] == "explicit", "invalid commit")
    _require(isinstance(value["pick"], list) and bool(value["pick"]), "invalid pick steps")
    properties = None
    if record is not None:
        params = record.get("params")
        _require(isinstance(params, dict) and isinstance(params.get("properties"), dict),
                 "pick record requires params.properties")
        properties = params["properties"]
    for step in value["pick"]:
        _require(isinstance(step, dict), "invalid pick step")
        _enum(step.get("kind"), ("entity", "point"), "pick kind")
        if step["kind"] == "entity":
            _shape(step, ("kind", "key", "role", "repeat", "ordered", "min", "max"), "entity step")
            _require(_text(step["key"]), "invalid pick key")
            _enum(step["role"], ("panel", "string", "frame", "inverter", "zone", "route", "device"), "pick role")
            _require(type(step["repeat"]) is bool and type(step["ordered"]) is bool,
                     "invalid pick flags")
            _require(type(step["min"]) is int and type(step["max"]) is int
                     and 0 <= step["min"] <= step["max"], "invalid pick bounds")
            keys = [step["key"]]
        else:
            _shape(step, ("kind", "keys", "snap"), "point step")
            keys = step["keys"]
            _require(_strings(keys) and len(keys) == 2 and len(set(keys)) == 2,
                     "invalid point keys")
            _require(step["snap"] is True, "invalid point snap")
        if properties is not None:
            _require(all(key in properties for key in keys), "pick key absent from params.properties")


def _validate(row, stem, server_dir, families):
    _shape(row, _KEYS, stem)
    _require(row["schema"] == SCHEMA, "invalid schema")
    _require(row["name"] == stem.replace("_", "-"), "name disagrees with filename")
    _require(row["builtin"] == "builtins/" + stem + ".py", "invalid builtin path")
    _require((server_dir / row["builtin"]).is_file(), "missing builtin")
    _enum(row["family"], families, "family")
    if row["adapter"] is not None:
        _enum(row["adapter"], ADAPTER_KINDS, "adapter")
    _enum(row["entitlement"], ("run_read", "run_write", "solve"), "entitlement")
    _enum(row["engine"], ("server-builtin", "browser", "cloud-service", "autocad-lane", "dotnet-worker"), "engine")
    _enum(row["maturity"], ("production", "preview", "tutorial"), "maturity")
    _enum(row["record_store"], ("registry", "write_seed", "catalog_seed"), "record_store")
    for field in ("requires_persisted_graph", "seedable"):
        _require(type(row[field]) is bool, "invalid " + field)
    for field, lower, upper in (("wave", 1, 5), ("order", 0, 9999)):
        _require(type(row[field]) is int and lower <= row[field] <= upper, "invalid " + field)
    _require(_text(row["scenario"]), "invalid scenario")
    _require(_strings(row["ledger"]), "invalid ledger")
    _require(_strings(row["trusted_inputs"])
             and all(name in TRUSTED_INPUTS for name in row["trusted_inputs"]), "invalid trusted_inputs")
    _require(row["invalid_request_code"] is None or _text(row["invalid_request_code"]),
             "invalid invalid_request_code")
    if row["adapter"] == "local-graph-commit":
        _require(_text(row["invalid_request_code"]), "local adapter requires invalid_request_code")
    if row["seedable"]:
        _require(row["adapter"] == "local-graph-commit", "seed must use local adapter")
    readiness = row["readiness"]
    _require(isinstance(readiness, dict), "invalid readiness")
    _enum(readiness.get("kind"), ("facets", "w1-chain", "hook"), "readiness kind")
    if readiness["kind"] == "facets":
        _shape(readiness, ("kind", "facets"), "readiness")
        _require(_strings(readiness["facets"]), "invalid facets")
    else:
        _shape(readiness, ("kind",), "readiness")
    record = row["record"]
    if row["record_store"] == "registry":
        _require(isinstance(record, dict), "registry store requires record")
        _require(row["adapter"] != "cloud-proposal", "registry cloud proposal is unsupported")
        for key, expected in (("name", row["name"]), ("entry", row["builtin"]), ("family_id", row["family"])):
            _require(record.get(key) == expected, "record disagrees on " + key)
    else:
        _require(record is None, "legacy store must not carry record")
    _interaction(row["interaction"], record)


class _Registry:
    def __init__(self, rows, hashes, server_dir):
        self._rows = tuple(sorted(rows, key=lambda row: (row["wave"], row["order"], row["name"])))
        self._by_name = {row["name"]: row for row in self._rows}
        self._manifest = tuple(sorted(hashes))
        self._server_dir = server_dir

    def entries(self):
        return copy.deepcopy(self._rows)

    def get(self, name):
        return copy.deepcopy(self._by_name.get(name)) if isinstance(name, str) else None

    def capability_table(self):
        keys = ("requires_persisted_graph", "adapter", "entitlement", "scenario", "seedable")
        return {row["name"]: {key: row[key] for key in keys} for row in self._rows}

    def local_graph_tools(self):
        return tuple(row["name"] for row in self._rows if row["adapter"] == "local-graph-commit")

    def seed_tool(self):
        return next(row["name"] for row in self._rows if row["seedable"])

    def registry_records(self):
        return [copy.deepcopy(row["record"]) for row in self._rows if row["record_store"] == "registry"]

    def _store(self, store):
        path = self._server_dir / {"write_seed": "write_tools.json", "catalog_seed": "catalog_tools.json"}[store]
        try:
            with open(path, "rb") as stream:
                data = _json(stream.read())
        except FileNotFoundError:
            return {}
        return {row["name"]: row for row in data["tools"]}

    def trusted_record(self, name):
        row = self.get(name)
        if row is None:
            return None
        if row["record_store"] == "registry":
            return row["record"]
        return copy.deepcopy(self._store(row["record_store"]).get(name))

    def trusted_snapshot(self):
        stores = {store: self._store(store) for store in ("write_seed", "catalog_seed")}
        return {row["name"]: copy.deepcopy(row["record"] if row["record_store"] == "registry"
                else stores[row["record_store"]].get(row["name"])) for row in self._rows}

    def catalog_view(self, tool, snapshot=None):
        if not isinstance(tool, dict):
            return None
        row = self.get(tool.get("name"))
        if row is None:
            return None
        trusted = self.trusted_record(row["name"]) if snapshot is None else snapshot.get(row["name"])
        if trusted is None or {k: v for k, v in tool.items() if k not in _PROJECTION_KEYS} != trusted:
            return None
        return {"schema": "leaf.solar-tool-view.v1", **{key: copy.deepcopy(row[key]) for key in (
            "name", "family", "wave", "order", "maturity", "engine", "adapter",
            "entitlement", "interaction", "ledger")}}

    def manifest(self):
        return self._manifest


def load(directory=None, server_dir=None):
    """Load a complete registry without changing the process's packaged registry."""
    directory = _DIRECTORY if directory is None else Path(directory)
    server_dir = _DIRECTORY.parent if server_dir is None else Path(server_dir)
    try:
        with open(server_dir / "capability_families.json", "rb") as stream:
            families = {row["family_id"] for row in _json(stream.read())["families"]}
        paths = []
        for path in directory.iterdir():
            if path.name in ("__init__.py", "__pycache__"):
                continue
            _require(path.is_file() and path.suffix == ".json"
                     and re.fullmatch(r"solar_[a-z0-9]+(_[a-z0-9]+)*", path.stem),
                     "unexpected registry entry: " + path.name)
            paths.append(path)
        _require(len(paths) <= MAX_DECLARATIONS, "too many solar declarations")
        rows, hashes = [], []
        for path in sorted(paths):
            with open(path, "rb") as stream:
                data = stream.read(65537)
            _require(len(data) <= 65536, "declaration exceeds 65536 bytes")
            row = _json(data)
            _validate(row, path.stem, server_dir, families)
            rows.append(row)
            hashes.append((row["name"], hashlib.sha256(data).hexdigest()))
        _require(sum(row["seedable"] for row in rows) == 1, "exactly one seed tool is required")
        return _Registry(rows, hashes, server_dir)
    except SolarRegistryError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise SolarRegistryError("could not load solar registry: " + str(exc)) from exc


_REGISTRY = load()


def entries():
    return _REGISTRY.entries()


def get(name):
    return _REGISTRY.get(name)


def capability_table():
    return _REGISTRY.capability_table()


def local_graph_tools():
    return _REGISTRY.local_graph_tools()


def seed_tool():
    return _REGISTRY.seed_tool()


def registry_records():
    return _REGISTRY.registry_records()


def trusted_record(name):
    return _REGISTRY.trusted_record(name)


def trusted_snapshot():
    return _REGISTRY.trusted_snapshot()


def catalog_view(tool, snapshot=None):
    return _REGISTRY.catalog_view(tool, snapshot=snapshot)


def manifest():
    return _REGISTRY.manifest()
