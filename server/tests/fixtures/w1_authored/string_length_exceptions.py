"""String-length exception report: strings whose length falls outside the bounds.

Bounds come from the params when given, else from the graph's settings
(extra.string_length_min / extra.string_length_max, then panels_in_sequence as the
committed maximum). A string above the maximum reports above_max; otherwise one
below the minimum reports below_min. Authored tool source as the authoring harness
returns it. Declares graph_input 'solar-w1-graph'. Standard library only.
"""


def _bound(params, key, extra, extra_key):
    value = params.get(key)
    if value is None:
        value = extra.get(extra_key)
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(key + " must be a non-negative integer")
    return value


def run(intake, params):
    graph = intake["graph"]
    settings = graph["settings"]
    extra = settings.get("extra") or {}
    minimum = _bound(params, "min_length", extra, "string_length_min")
    maximum = _bound(params, "max_length", extra, "string_length_max")
    sized = settings.get("panels_in_sequence")
    if maximum is None and type(sized) is int and sized >= 1:
        maximum = sized
    exceptions = []
    for string in graph["strings"]:
        count = len(string["ordered_panel_refs"])
        name = string.get("circuit_tag") or string["id"]
        if maximum is not None and count > maximum:
            exceptions.append({"string": name, "panels": count, "rule": "above_max", "max": maximum})
        elif minimum is not None and count < minimum:
            exceptions.append({"string": name, "panels": count, "rule": "below_min", "min": minimum})
    return {
        "report": "string-length-exceptions",
        "drawing_id": intake["drawing_id"],
        "source_version": intake["source_version"],
        "graph_sha256": intake["graph_sha256"],
        "min": minimum,
        "max": maximum,
        "exceptions": exceptions,
    }, None
