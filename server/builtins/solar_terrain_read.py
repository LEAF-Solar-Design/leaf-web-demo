"""Read the terrain stored for a drawing, as a registry graph read over its physical head.

The terrain is not in the design graph: a LandXML import (server/solar_landxml_import.py) stores it
as the drawing's Ground physical state, and the terrain operations (server/solar_ground_terrain_adapter.py)
publish their previews as children of that state. This builtin declares READS_PHYSICAL_HEAD, so the
read adapter (server/solar_local_read.py) hands it the drawing's physical head: None when no state
was ever published, else {"head": the head view, "document": the decoded state document}.

The answer is the adapter's own frozen reading (solar_ground_terrain_adapter.terrain_view): the
frame (units, metres per drawing unit, CRS, elevation datum, identity transform), the grid summary
(rows, columns, bounds, cell size, elevation range, digest; None when the state holds no grid) and
each preview's record with its standing (absent, current or stale). It adds the head it read (so the
terminal proof can re-read that exact state) and the state's own capability, parent and source
digest. Ground Physical PREVIEW: maturity "preview", never a production claim. Pure and bounded: no
I/O, no clock, O(grid nodes) with at most 90,000 nodes; fails closed with a named code.
"""
import solar_ground_terrain_adapter as terrain_adapter
import solar_physical_state as ps
from solar_design_graph import GraphValidationError

TOOL = "solar-terrain-read"
OUTPUT_SCHEMA = "leaf.solar-terrain-read.v1"
INVALID = "INVALID_TERRAIN_READ_REQUEST"
MATURITY = terrain_adapter.MATURITY
READS_PHYSICAL_HEAD = True


def run(graph, params, physical_head=None):
    """The stored terrain reading; `stored` False (and head, state, terrain None) when the
    drawing has no physical state. The request carries no parameters."""
    if type(params) is not dict or params:
        raise GraphValidationError(INVALID)
    if physical_head is None:
        return {"schema": OUTPUT_SCHEMA, "maturity": MATURITY, "stored": False,
                "head": None, "state": None, "terrain": None}
    try:
        document = physical_head["document"]
        view = terrain_adapter.terrain_view(document)
        state = {"capability": document["capability"], "parent": document["parent"],
                 "source": dict(document["source"])}
        head = physical_head["head"]
    except ps.PhysicalStateError as exc:
        raise GraphValidationError(exc.code) from None
    except (LookupError, TypeError, AttributeError, ValueError):
        raise GraphValidationError("PHYSICAL_HEAD_CORRUPT") from None
    return {"schema": OUTPUT_SCHEMA, "maturity": MATURITY, "stored": True, "head": head,
            "state": state, "terrain": view}
