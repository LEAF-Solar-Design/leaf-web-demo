"""Author minimum-total-distance cable bundle routes on a capacitated tray network."""
import re


def matches(description):
    prompt = description.casefold()
    return (bool(re.search(r"\bbundles?\b", prompt))
            and bool(re.search(r"\btrays?\b", prompt))
            and bool(re.search(r"\b(?:rout\w*|rerout\w*|allocat\w*)\b", prompt)))


def build_body(description):
    return _BODY


_BODY = r"""
import math
import re


def network(intake):
    # Tray terminals are joined at coincident plan-view endpoints. Interior
    # vertices describe bends. Crossing trays do not imply an electrical joint.
    graph, limits, bundles = {}, {}, {}
    handles = set()
    for entity in intake.get("polylines", []):
        if entity.get("space") == "paper":
            continue
        layer = entity.get("layer", "").casefold()
        if re.search(r"\btrays?\b", layer):
            kind, quantity = "tray", r"(?:cap|capacity)"
        elif re.search(r"\bbundles?\b", layer):
            kind, quantity = "bundle", r"(?:load|demand)"
        else:
            continue
        match = re.search(r"\b" + quantity + r"[\s:=]+(\d+)(?=$|\s)", layer)
        handle = entity.get("handle")
        if not match or int(match[1]) < (1 if kind == "bundle" else 0):
            raise ValueError(f"Specify tray capacity or positive bundle load on layer {layer}.")
        if not handle or handle in handles:
            raise ValueError("Trays and bundles need unique drawing handles.")
        handles.add(handle)
        points = [tuple(p[:2]) for p in entity["pts"]]
        if len(points) < 2 or entity.get("closed") or points[0] == points[-1]:
            raise ValueError(f"{kind} {handle} must have distinct terminals.")
        units = int(match[1])
        if kind == "bundle":
            bundles[handle] = (points[0], points[-1], units)
            continue
        if any(entity.get("bulges", [])):
            raise ValueError(f"Tray {handle} must have straight segments.")
        length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
        limits[handle] = units
        graph.setdefault(points[0], []).append((handle, length, points))
        graph.setdefault(points[-1], []).append((handle, length, points[::-1]))
    if not bundles or not limits:
        raise ValueError("Draw tray centerlines on 'Tray cap N' and requests on 'Bundle load N'.")
    return graph, limits, bundles


def paths(graph, limits, start, end, demand):
    pending = [(start, {start}, (), 0.0, [start])]
    while pending:
        node, seen, trays, length, points = pending.pop()
        if node == end:
            yield (length, trays, points)
            continue
        for tray, distance, part in graph.get(node, []):
            other = part[-1]
            if other not in seen and demand <= limits[tray]:
                pending.append((other, seen | {other}, trays + (tray,),
                                length + distance, points + part[1:]))


def run(intake, params):
    graph, limits, bundles = network(intake)
    options = {h: sorted(paths(graph, limits, *bundle)) for h, bundle in bundles.items()}
    # Branch first on the most constrained bundle. A positive-length cycle
    # only consumes capacity, so simple paths suffice for the global optimum.
    order = sorted(bundles, key=lambda h: len(options[h]))
    remaining = limits.copy()
    selected = {}
    best, best_length = None, math.inf

    def assign(index, length):
        nonlocal best, best_length
        if length >= best_length:
            return
        if index == len(order):
            best, best_length = selected.copy(), length
            return
        h = order[index]
        demand = bundles[h][2]
        for route in options[h]:
            distance, trays, _ = route
            if length + distance >= best_length:
                break
            if all(remaining[t] >= demand for t in trays):
                for t in trays:
                    remaining[t] -= demand
                selected[h] = route
                assign(index + 1, length + distance)
                for t in trays:
                    remaining[t] += demand

    assign(0, 0.0)
    overlay = {"polylines": [], "highlight_handles": [], "markers": []}
    if best is None:
        return {"status": "infeasible", "routes": {}}, overlay
    loads = dict.fromkeys(limits, 0)
    result = {}
    for h, (length, trays, points) in best.items():
        for t in trays:
            loads[t] += bundles[h][2]
        result[h] = {"length": length, "trays": list(trays), "points": [list(p) for p in points]}
        overlay["polylines"].append({"pts": result[h]["points"]})
    return ({"status": "optimal", "routes": result, "total_length": best_length,
             "units": "drawing_units", "tray_loads": loads, "tray_capacities": limits}, overlay)
"""
