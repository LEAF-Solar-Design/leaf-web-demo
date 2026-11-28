"""Count captured drawing entities under two distinct literal layer names."""
from envelopes import ErrorCode, err_envelope


_ENTITY_COLLECTIONS = (
    "polylines", "inserts", "faces3d", "circles", "arcs", "splines", "texts", "dimensions", "mleaders",
)


def run(intake, params):
    names = params["layers"]
    declared = {row.get("name") if isinstance(row, dict) else row
                for row in intake.get("layers", ())}
    if len(set(names)) != 2 or not declared.issuperset(names):
        result = err_envelope(
            ErrorCode.BAD_PARAMS,
            "Both exact layer names must be present in the saved drawing to verify a separate count.",
            False, tool="compare-layer-counts", version="1.0.0",
        )
        result["error"]["reason_code"] = "LAYER_COMPARISON_UNVERIFIED"
        return result
    if any(intake.get(key) for key in ("parseErrors", "blocksCapped", "dimensions_unsupported",
                                     "mleaders_unsupported")):
        result = err_envelope(
            ErrorCode.BAD_PARAMS, "The saved drawing reports incomplete entity capture.",
            False, tool="compare-layer-counts", version="1.0.0",
        )
        result["error"]["reason_code"] = "LAYER_COUNT_COVERAGE_UNVERIFIED"
        return result

    counts = dict.fromkeys(names, 0)
    for collection in _ENTITY_COLLECTIONS:
        for entity in intake.get(collection) or ():
            name = entity.get("layer", "0")
            if name in counts and entity.get("space") != "paper":
                counts[name] += 1
    return ({"counts": counts, "total": sum(counts.values()), "merged": False,
             "coverage": "captured_model_space_entities"}, None)
