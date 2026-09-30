"""The route-aware electrical bridge: the equipment topology of solar_electrical_state_bridge.py (the
legacy bridge, unchanged) plus the graph's conductor routes, projected both ways between the shared
design graph (contract/solar-design-graph.v1.schema.json, route kinds typed since 0b77c211) and the
inverter kernels' G35 state (server/solar_inverter_state.py, solar_inverter_cabling.py).

Two pure functions, no I/O, no clock, no network:

  state_from_graph(graph) -> (state, binding)
      The legacy bridge's state plus one kernel cable row per homerun and feeder route. The binding is
      the legacy binding, unchanged in shape.
  graph_from_state(graph, state, binding, *, defaults, new_id, created_at) -> (graph, binding)
      The legacy bridge's write-back of the equipment topology, then the state's dc-homerun and feeder
      rows written onto the graph's routes. The binding's graph_sha256 names the returned graph.
  adopt_state is the legacy function, re-exported: routes need no binding of their own.

Frozen decisions (sf-solar-electrical-bridge-routes):
  - What is projected. A "start homerun" or "end homerun" route is a dc-homerun row (segment "start"
    or "end"); a "feeder" route is a feeder row. A trench route is never projected and a graph trench is
    never changed, moved or removed; a string's pathway_ref is never touched (the legacy bridge copies
    strings), and a route's pathway_ref survives whenever its points do. The kernels' private `_trenches`
    and `_trench` keys are ignored both ways.
  - Identity is the natural key, never the row's private `_pair` (the kernels mint a fresh pair on every
    row they redraw): a homerun is (its string, its route_kind) <-> (the string's handle, the segment);
    a feeder is its L1 inverter <-> the feeder row's L1 number (validate_graph already allows one feeder
    per L1: DUPLICATE_FEEDER). A route whose key the state no longer carries is removed; a row whose key
    no route has becomes a new route with a fresh id from `new_id`.
  - Units. Coordinates convert with the project's meters_per_unit exactly as the legacy bridge converts
    positions: x_du = x_m / meters_per_unit, x_m = x_du * meters_per_unit. A z value is not projected.
    length_ft is stored, never recomputed: graph length_ft is the row's length value in feet verbatim,
    and the row's length value in feet is the graph length_ft verbatim (the kernels compute feet from
    drawing units as inches; the bridge never re-derives a length from points).
  - No drift. A route whose projected vertices, length, gauge and endpoint all equal the row keeps every
    field exactly. Otherwise each point whose projection equals the row's vertex at the same index is
    kept verbatim (its type and any z) and every other point becomes [x_du * mpu, y_du * mpu]; a route
    whose points changed loses its pathway_ref (set to null when the key is present): a conductor the
    kernel redrew no longer rides the trench it was snapped to.
  - Gauge. The kernels never size conductors: every leg and feeder they draw carries gauge "". A row's
    non-empty `_detail.gauge` is the route's wire_gauge; an empty one keeps the existing route's
    wire_gauge; a new homerun without one takes its string's wire_gauge (the W1 homerun policy,
    solar_wiring_client.local_routes); a new feeder without one gets "".
  - Endpoints. A homerun's to_ref is the device the legacy bridge associates its string with (the device
    every one of the string's legs ends at). A feeder's from_ref and to_ref are the L1 and L2 its row
    numbers (cab._feeder_ends: from and to, else the F<l1>/<l2> circuit). In the input graph (both
    functions check it first, before anything is projected or minted) a homerun must end exactly at its
    string's current inverter and a feeder must run from an L1 to that L1's l2_ref; state to graph, a
    feeder must name the L2 its L1 is assigned to after the write-back: else
    BRIDGE_ROUTE_TOPOLOGY_MISMATCH. Feeder geometry is never checked against device positions (MOVEINV
    moves a combiner and leaves its feeder where it was; the bridge persists what the kernel holds).
  - Order. Kept routes stay in graph order, trenches included; new routes follow in the kernels' cable
    order (st.ORDER["cable"]); cable rows from the graph follow graph route order.
  - New routes: kind "route", rev and provenance.source_rev the graph rev, created_by and last_writer
    WRITER, created_at the caller's (required when any route is new: BRIDGE_INVALID_REQUEST), extra {},
    validity valid, point_units "m", length_units "ft".

Bounds: at most 100,000 routes (the contract's routes maxItems), 200,000 cable rows (st.MAX_ROWS_PER_KIND),
100,000 vertices in one route or row (the contract's points maxItems) and 1,000,000 vertices in all,
coordinates of magnitude at most 1e12 drawing units, lengths 0 to 1e9 feet, gauges at most 4,096
characters (the contract's wire_gauge maxLength). Linear in routes, rows and vertices: one dict per
lookup. Every refusal is the legacy ElectricalBridgeError (a ValueError whose message is its code alone);
a graph the result would break fails closed with the validator's GraphValidationError. Inputs are never
mutated.
"""
from __future__ import annotations

import math

import solar_electrical_state_bridge as legacy
from solar_design_graph import new_id as graph_new_id, validate_graph

st = legacy.st
cab = legacy.cab
ElectricalBridgeError = legacy.ElectricalBridgeError
canonical_sha256 = legacy.canonical_sha256
adopt_state = legacy.adopt_state

WRITER = "solar-electrical-route-bridge"
SEGMENT_OF = {"start homerun": "start", "end homerun": "end"}
KIND_OF = {"start": "start homerun", "end": "end homerun"}
MAX_ROUTES = 100_000                       # contract: routes maxItems
MAX_CABLE_ROWS = st.MAX_ROWS_PER_KIND      # 200,000
MAX_ROUTE_VERTICES = 100_000               # contract: points maxItems
MAX_VERTICES = 1_000_000                   # all projected vertices, either way
MAX_COORDINATE = 1e12                      # drawing units
MAX_LENGTH_FT = 1e9
MAX_GAUGE = 4096                           # contract: wire_gauge maxLength
MAX_NUMBER = legacy.MAX_NUMBER
# Bound feeder circuits because the legacy parser calls int() on the digits.
MAX_CIRCUIT_CHARS = 64
MAX_CIRCUIT_DIGITS = 9
CODES = ("BRIDGE_ROUTE_UNSUPPORTED", "BRIDGE_ROUTE_DUPLICATE", "BRIDGE_ROUTE_TOPOLOGY_MISMATCH",
         "BRIDGE_ROUTE_UNBOUND", "BRIDGE_ROUTE_INVALID", "BRIDGE_BOUNDS_EXCEEDED", "BRIDGE_INVALID_REQUEST",
         "BRIDGE_ID_COLLISION", "BRIDGE_DEVICE_UNNUMBERED")


def _fail(code):
    raise ElectricalBridgeError(code)


def _finite(value):
    return legacy._finite(value)


def _du(point, mpu):
    """Drawing units of a graph point (metres; any z dropped)."""
    x, y = float(point[0]) / mpu, float(point[1]) / mpu
    if not (math.isfinite(x) and math.isfinite(y)) or abs(x) > MAX_COORDINATE or abs(y) > MAX_COORDINATE:
        _fail("BRIDGE_ROUTE_UNSUPPORTED")
    return (x, y)


def _metres(vertex, mpu):
    return [vertex[0] * mpu, vertex[1] * mpu]


def _number(value):
    return legacy._integer(value, "BRIDGE_DEVICE_UNNUMBERED")


def _row_vertices(row, budget):
    vertices = row.get("vertices")
    if not isinstance(vertices, list) or not 2 <= len(vertices) <= MAX_ROUTE_VERTICES:
        _fail("BRIDGE_ROUTE_INVALID")
    budget[0] += len(vertices)
    if budget[0] > MAX_VERTICES:
        _fail("BRIDGE_BOUNDS_EXCEEDED")
    out = []
    for vertex in vertices:
        try:
            x, y = st.point_of(vertex, "cable vertex")
        except (st.InverterStateError, OverflowError):
            _fail("BRIDGE_ROUTE_INVALID")
        if abs(x) > MAX_COORDINATE or abs(y) > MAX_COORDINATE:
            _fail("BRIDGE_ROUTE_INVALID")
        out.append((x, y))
    return out


def _row_length(row):
    length = row.get("length")
    if not isinstance(length, dict) or set(length) != {"kind", "value", "unit"} \
            or length.get("kind") != "length" or length.get("unit") != "ft" \
            or not _finite(length.get("value")) or not 0 <= length["value"] <= MAX_LENGTH_FT:
        _fail("BRIDGE_ROUTE_INVALID")
    return float(length["value"])


def _row_gauge(row):
    if "_detail" not in row:
        return ""
    detail = row["_detail"]
    if not isinstance(detail, dict):
        _fail("BRIDGE_ROUTE_INVALID")
    gauge = detail.get("gauge", "")
    if type(gauge) is not str or len(gauge) > MAX_GAUGE:
        _fail("BRIDGE_ROUTE_INVALID")
    return gauge


def _length_row(value):
    return {"kind": "length", "value": float(value), "unit": "ft"}


# ----------------------------------------------------------- graph -> state --

def _projected(g, mpu):
    """[(route, key, vertices in drawing units)] for every homerun and feeder route of a validated graph,
    in graph order, each checked (see "Endpoints"); trenches are skipped. Linear in routes and points."""
    if len(g["routes"]) > MAX_ROUTES:
        _fail("BRIDGE_BOUNDS_EXCEEDED")
    strings = {string["id"]: string for string in g["strings"]}
    inverters = {inverter["id"]: inverter for inverter in g["inverters"]}
    out, seen, total = [], set(), 0
    for route in g["routes"]:
        kind = route["route_kind"]
        if kind == "trench":
            continue
        points = route["points"]
        if len(points) < 2:
            _fail("BRIDGE_ROUTE_UNSUPPORTED")
        total += len(points)
        if total > MAX_VERTICES:
            _fail("BRIDGE_BOUNDS_EXCEEDED")
        vertices = [_du(point, mpu) for point in points]
        length = float(route["length_ft"])
        if not math.isfinite(length) or length > MAX_LENGTH_FT:
            _fail("BRIDGE_ROUTE_UNSUPPORTED")
        if kind in SEGMENT_OF:
            string, target = strings.get(route["from_ref"]), inverters.get(route["to_ref"])
            if string is None or target is None:
                _fail("BRIDGE_ROUTE_UNSUPPORTED")
            if string["inverter_ref"] != target["id"] or vertices[-1] != _du(target["position"], mpu):
                _fail("BRIDGE_ROUTE_TOPOLOGY_MISMATCH")
        else:                                   # feeder: validate_graph proved the kinds and one per L1
            if inverters[route["from_ref"]].get("l2_ref") != route["to_ref"]:
                _fail("BRIDGE_ROUTE_TOPOLOGY_MISMATCH")
        key = (route["from_ref"], kind)
        if key in seen:
            _fail("BRIDGE_ROUTE_DUPLICATE")
        seen.add(key)
        out.append((route, key, vertices))
    return out


def state_from_graph(graph):
    """The legacy bridge's state of the graph plus its homerun and feeder routes as cable rows."""
    g = validate_graph(graph)
    mpu = legacy._meters_per_unit(g)
    projected = _projected(g, mpu)
    state, binding = legacy.state_from_graph(g)
    handles = {item["id"]: handle for handle, item in binding["strings"].items()}
    circuits = {row["string"]: row["_detail"]["circuit"] for row in state["rows"]["string-assignment"]}
    inverters = {inverter["id"]: inverter for inverter in g["inverters"]}
    cables = []
    for k, (route, key, vertices) in enumerate(projected, 1):
        target = inverters[route["to_ref"]]
        if key[1] in SEGMENT_OF:
            handle = handles[route["from_ref"]]
            row = {"cable_kind": "dc-homerun", "segment": SEGMENT_OF[key[1]], "from": handle,
                   "to": _number(target["number"])}
            circuit = circuits[handle]
        else:
            l1, l2 = _number(inverters[route["from_ref"]]["number"]), _number(target["number"])
            row = {"cable_kind": "feeder", "from": l1, "to": l2}
            circuit = f"F{l1}/{l2}"
        row.update(vertices=[st.coordinate(x, y) for x, y in vertices], length=_length_row(route["length_ft"]),
                   _pair=f"cable:bridge-{k}", _detail={"circuit": circuit, "gauge": route["wire_gauge"],
                                                       "closed": False})
        cables.append(row)
    state["rows"]["cable"] = cables
    return legacy._state(state), binding


# ----------------------------------------------------------- state -> graph --

def _check_raw_state(state):
    """Check containers and cheap bounds before the legacy copy and row serialization."""
    if not isinstance(state, dict) or not isinstance(state.get("rows"), dict):
        _fail("BRIDGE_ROUTE_INVALID")
    rows = state["rows"]
    for items in rows.values():
        if not isinstance(items, list):
            _fail("BRIDGE_ROUTE_INVALID")
    cables = rows.get("cable")
    if not isinstance(cables, list):
        _fail("BRIDGE_ROUTE_INVALID")
    if len(cables) > MAX_CABLE_ROWS:
        _fail("BRIDGE_BOUNDS_EXCEEDED")
    total = 0
    # Read the validator's live bound without adding an import or freezing a second copy.
    node_limit = validate_graph.__globals__["MAX_NODES"]
    for row in cables:
        if not isinstance(row, dict):
            _fail("BRIDGE_ROUTE_INVALID")
        vertices = row.get("vertices")
        if not isinstance(vertices, list):
            _fail("BRIDGE_ROUTE_INVALID")
        if len(vertices) > 100_000:           # contract: points maxItems
            _fail("BRIDGE_BOUNDS_EXCEEDED")
        total += len(vertices)
        if total > node_limit:
            _fail("BRIDGE_BOUNDS_EXCEEDED")
        for key in ("from", "to", "segment"):
            value = row.get(key)
            if isinstance(value, str) and len(value) > legacy.MAX_TEXT:
                _fail("BRIDGE_ROUTE_INVALID")
        detail = row.get("_detail")
        if detail is not None and not isinstance(detail, dict):
            _fail("BRIDGE_ROUTE_INVALID")
        if isinstance(detail, dict):
            circuit = detail.get("circuit")
            if circuit is not None and (type(circuit) is not str or
                    len(circuit) > legacy.MAX_TEXT or len(circuit) > MAX_CIRCUIT_CHARS):
                _fail("BRIDGE_ROUTE_INVALID")


def _check_row_types(state):
    """Bound the pass and reject malformed inputs before the legacy bridge reads cable rows."""
    rows = state["rows"]["cable"]
    if len(rows) > MAX_CABLE_ROWS:
        _fail("BRIDGE_BOUNDS_EXCEEDED")
    for row in rows:
        vertices = row.get("vertices")
        if not isinstance(vertices, list) or len(vertices) < 2:
            _fail("BRIDGE_ROUTE_INVALID")
        for vertex in vertices:
            try:
                st.point_of(vertex, "cable vertex")
            except (st.InverterStateError, OverflowError):
                _fail("BRIDGE_ROUTE_INVALID")
        kind = row.get("cable_kind")
        if type(kind) is not str:
            _fail("BRIDGE_ROUTE_INVALID")
        if kind == "dc-homerun":
            if type(row.get("from")) is not str or type(row.get("segment")) is not str:
                _fail("BRIDGE_ROUTE_INVALID")
        elif kind == "feeder":
            if any(isinstance(row.get(key), (list, dict)) for key in ("from", "to")):
                _fail("BRIDGE_ROUTE_INVALID")
            if "_detail" not in row:
                continue
            detail = row["_detail"]
            if not isinstance(detail, dict):
                _fail("BRIDGE_ROUTE_INVALID")
            circuit = detail.get("circuit")
            if circuit is None:
                continue
            if type(circuit) is not str or len(circuit) > MAX_CIRCUIT_CHARS:
                _fail("BRIDGE_ROUTE_INVALID")
            digits = 0
            for char in circuit:
                if not char.isdecimal():
                    digits = 0
                elif digits or char != "0":
                    digits += 1
                if digits > MAX_CIRCUIT_DIGITS:
                    _fail("BRIDGE_ROUTE_INVALID")


def _read_rows(state, strings_by_handle, strings_after, by_level):
    """(homeruns, feeders): natural key -> (row, vertices, length, gauge, to_ref). Linear in rows."""
    rows = state["rows"]["cable"]
    if len(rows) > MAX_CABLE_ROWS:
        _fail("BRIDGE_BOUNDS_EXCEEDED")
    homeruns, feeders, budget = {}, {}, [0]
    for row in rows:
        kind = row.get("cable_kind")
        if kind == "dc-homerun":
            handle, segment = row.get("from"), row.get("segment")
            if type(handle) is not str or handle not in strings_by_handle:
                _fail("BRIDGE_ROUTE_UNBOUND")
            if segment not in KIND_OF:
                _fail("BRIDGE_ROUTE_INVALID")
            ref = strings_by_handle[handle]
            key = (ref, KIND_OF[segment])
            if key in homeruns:
                _fail("BRIDGE_ROUTE_DUPLICATE")
            vertices, length, gauge = _row_vertices(row, budget), _row_length(row), _row_gauge(row)
            homeruns[key] = (row, vertices, length, gauge, strings_after[ref]["inverter_ref"])
        elif kind == "feeder":
            source, sink = cab._feeder_ends(row)
            if not all(type(n) is int and 1 <= n <= MAX_NUMBER for n in (source, sink)):
                _fail("BRIDGE_ROUTE_INVALID")
            l1, l2 = by_level.get((False, source)), by_level.get((True, sink))
            if l1 is None or l2 is None:
                _fail("BRIDGE_ROUTE_UNBOUND")
            key = (l1["id"], "feeder")
            if key in feeders:
                _fail("BRIDGE_ROUTE_DUPLICATE")
            if l1.get("l2_ref") != l2["id"]:
                _fail("BRIDGE_ROUTE_TOPOLOGY_MISMATCH")
            vertices, length, gauge = _row_vertices(row, budget), _row_length(row), _row_gauge(row)
            feeders[key] = (row, vertices, length, gauge, l2["id"])
        else:
            _fail("BRIDGE_ROUTE_UNSUPPORTED")
    return homeruns, feeders


def _update(route, item, mpu):
    """The kept route with the row written onto it, field by field (see "No drift")."""
    _, vertices, length, gauge, target = item
    old = [_du(point, mpu) for point in route["points"]]
    if vertices != old:
        route["points"] = [route["points"][i] if i < len(old) and old[i] == vertex else _metres(vertex, mpu)
                           for i, vertex in enumerate(vertices)]
        if route.get("pathway_ref") is not None:
            route["pathway_ref"] = None
    if length != float(route["length_ft"]):
        route["length_ft"] = length
    if gauge and gauge != route["wire_gauge"]:
        route["wire_gauge"] = gauge
    if target != route["to_ref"]:
        route["to_ref"] = target


def graph_from_state(graph, state, binding, *, defaults=None, new_id=None, created_at=None):
    """The graph with the state's equipment topology (the legacy bridge) and its homerun and feeder rows
    written onto a copy, validated, and the binding of that state to the result."""
    _check_raw_state(state)
    g = validate_graph(graph)
    _projected(g, legacy._meters_per_unit(g))
    # Malformed coordinates must not reach the legacy ordering-key serialization.
    _check_row_types(state)
    s = legacy._state(state)
    _check_row_types(s)
    mint = graph_new_id if new_id is None else new_id
    result, rebound = legacy.graph_from_state(g, s, binding, defaults=defaults, new_id=mint, created_at=created_at)
    mpu = legacy._meters_per_unit(result)
    strings_by_handle = {handle: item["id"] for handle, item in rebound["strings"].items()}
    strings_after = {string["id"]: string for string in result["strings"]}
    by_level = {(inverter["is_l2"], _number(inverter["number"])): inverter for inverter in result["inverters"]}
    homeruns, feeders = _read_rows(s, strings_by_handle, strings_after, by_level)
    routes, used = [], set()
    for route in result["routes"]:
        kind = route["route_kind"]
        if kind == "trench":
            routes.append(route)
            continue
        key = (route["from_ref"], kind)
        item = (feeders if kind == "feeder" else homeruns).get(key)
        if item is None:
            continue                        # the state no longer carries it
        used.add(key)
        _update(route, item, mpu)
        routes.append(route)
    fresh = [(key, item) for table in (homeruns, feeders) for key, item in table.items() if key not in used]
    fresh.sort(key=lambda pair: st.ORDER["cable"](pair[1][0]))
    if fresh:
        if type(created_at) is not str or len(created_at) > 32 or not legacy.CREATED_AT.fullmatch(created_at):
            _fail("BRIDGE_INVALID_REQUEST")
        taken = {entity["id"] for key in ("strings", "inverters", "panels", "frames", "routes", "schedules",
                                          "electrical_zones") for entity in result[key]}
        taken.update({result["project"]["id"], result["settings"]["id"]})
        for (ref, kind), (_, vertices, length, gauge, target) in fresh:
            ident = mint("route")
            if type(ident) is not str or len(ident) > 100:
                _fail("BRIDGE_INVALID_REQUEST")
            if ident in taken:
                _fail("BRIDGE_ID_COLLISION")
            taken.add(ident)
            if not gauge and kind != "feeder":
                gauge = strings_after[ref]["wire_gauge"]
            routes.append({"id": ident, "kind": "route", "rev": result["rev"],
                           "provenance": {"created_by": WRITER, "created_at": created_at, "last_writer": WRITER,
                                          "source_rev": result["rev"]},
                           "extra": {}, "validity": {"state": "valid", "reasons": []},
                           "route_kind": kind, "points": [_metres(vertex, mpu) for vertex in vertices],
                           "from_ref": ref, "to_ref": target, "wire_gauge": gauge, "length_ft": length,
                           "point_units": "m", "length_units": "ft"})
    if len(routes) > MAX_ROUTES:
        _fail("BRIDGE_BOUNDS_EXCEEDED")
    result["routes"] = routes
    result = validate_graph(result)
    return result, dict(rebound, graph_sha256=canonical_sha256(result))
