"""Declaration-only unit sync, the Studio counterpart of the plugin's LEAFUNITSYNC.

The plugin reads its persisted distance preference (Meters or Feet) and writes
exactly one thing to the drawing, the INSUNITS sysvar (Meters -> 6, Feet -> 2);
no geometry moves. Studio changes the graph's unit DECLARATION the same way:
project.units names the new unit with its matching scale and leaves every
coordinate untouched. When the graph already declares that unit nothing is
written and the reply says so, the plugin's "already {code}" path.
"""
from solar_design_graph import GraphValidationError, _bounded_json
from solar_sizing_client import advance, checked_graph

# The plugin's Distance preference (0 Meters / 1 Feet) as the graph's unit name and scale.
DISTANCE_UNITS = {"Meters": ("m", 1.0), "Feet": ("ft", 0.3048)}


def sync_units(graph, params):
    """Set project.units to the requested declaration, or report it already holds."""
    _bounded_json(params)
    if (type(params) is not dict or set(params) != {"expected_rev", "distance_unit"}
            or type(params["distance_unit"]) is not str
            or params["distance_unit"] not in DISTANCE_UNITS):
        raise GraphValidationError("INVALID_UNIT_SYNC_REQUEST")
    result = checked_graph(graph, params["expected_rev"])
    name, scale = DISTANCE_UNITS[params["distance_unit"]]
    units = result["project"]["units"]
    if units["drawing_units"] == name:
        return {"graph": result, "changed": False, "drawing_units": name}
    units.update(drawing_units=name, meters_per_unit=scale,
                 drawing_unit_is_feet=name == "ft", source="explicit")
    result = advance(result, [result["project"]], "solar-unit-sync")
    return {"graph": result, "changed": True, "drawing_units": name}


def run(intake, params):
    return sync_units(intake, params)["graph"]
