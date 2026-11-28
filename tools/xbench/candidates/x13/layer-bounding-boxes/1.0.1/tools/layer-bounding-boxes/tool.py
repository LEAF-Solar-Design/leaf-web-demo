"""
layer-bounding-boxes

Deterministic, pure-stdlib tool. Computes the 2D bounding box (min/max X, Y)
of each layer's model-space geometry (from polylines and 3D faces) and returns the
layers sorted by bounding-box area, largest first.

No I/O, no network, no LLM involvement at run time.
"""


def run(intake, params):
    params = params or {}
    layer_filter = params.get("layers")
    layer_filter_set = set(layer_filter) if layer_filter else None

    stats = {}

    def consider(layer, pts):
        if not pts:
            return
        if layer_filter_set is not None and layer not in layer_filter_set:
            return
        entry = stats.get(layer)
        if entry is None:
            entry = {
                "min_x": None,
                "max_x": None,
                "min_y": None,
                "max_y": None,
                "count": 0,
            }
            stats[layer] = entry
        for pt in pts:
            if pt is None or len(pt) < 2:
                continue
            x = float(pt[0])
            y = float(pt[1])
            if entry["min_x"] is None or x < entry["min_x"]:
                entry["min_x"] = x
            if entry["max_x"] is None or x > entry["max_x"]:
                entry["max_x"] = x
            if entry["min_y"] is None or y < entry["min_y"]:
                entry["min_y"] = y
            if entry["max_y"] is None or y > entry["max_y"]:
                entry["max_y"] = y
        entry["count"] += 1

    for pl in intake.get("polylines") or []:
        if pl.get("space") == "paper":
            continue
        consider(pl.get("layer", ""), pl.get("pts") or [])

    for f in intake.get("faces3d") or []:
        if f.get("space") == "paper":
            continue
        consider(f.get("layer", ""), f.get("pts") or [])

    layers_out = []
    for layer, entry in stats.items():
        if entry["min_x"] is None:
            continue
        width = entry["max_x"] - entry["min_x"]
        height = entry["max_y"] - entry["min_y"]
        area = width * height
        layers_out.append(
            {
                "layer": layer,
                "min_x": entry["min_x"],
                "max_x": entry["max_x"],
                "min_y": entry["min_y"],
                "max_y": entry["max_y"],
                "width": width,
                "height": height,
                "area": area,
                "geometry_count": entry["count"],
            }
        )

    layers_out.sort(key=lambda r: r["area"], reverse=True)

    result = {
        "layers": layers_out,
        "layer_count": len(layers_out),
    }
    return (result, None)
