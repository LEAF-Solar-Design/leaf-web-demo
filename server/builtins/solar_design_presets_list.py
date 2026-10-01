"""LEAFPROFILE List as a registry graph read over the drawing-owned preset store.

Returns the store's state in the two row shapes the parity receipts record (the Create, Swap and
Delete state rows and the List rows), the active prefix and the drawing's current preset settings
(the three always-synced layer names read from graph["settings"] and InstallationDesign from
graph["project"], server/solar_preset_sync.py).
A drawing with no store lists no presets. Pure: linear in the preset count, no I/O; a malformed
store is DESIGN_PRESETS_STORE_INVALID. Graph layer names are returned verbatim, including names
the preset store cannot hold: List does not write them into the store.
"""
import solar_preset_store as preset_store
import solar_preset_sync as preset_sync
from solar_design_graph import GraphValidationError

INVALID = "INVALID_DESIGN_PRESET_LIST_REQUEST"


def run(graph, params):
    if type(params) is not dict or params:
        raise GraphValidationError(INVALID)
    current, manager = preset_store.load(graph)
    current = preset_sync.effective_current(graph, current, check_bounds=False)
    # These five keys are the fixed output contract.
    return {"schema": preset_store.STORE_SCHEMA, "active_prefix": manager.active_prefix,
            "current_settings": current, "rows": preset_store.state_rows(manager),
            "list_rows": preset_store.list_rows(manager)}
