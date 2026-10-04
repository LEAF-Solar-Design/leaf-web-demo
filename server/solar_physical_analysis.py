"""Pure hydration of recorded Ground physical inputs for CPU analysis reports."""
from copy import deepcopy

import solar_ground_shade as shade
import solar_ground_terrain as terrain
import solar_ground_terrain_adapter as terrain_adapter
import solar_physical_state as ps
from solar_design_graph import GraphValidationError


def hydrate_physical_document(document):
    """Return private native entities and terrain; never resolve or publish a head."""
    if (type(document) is not dict or type(document.get("state")) is not dict
            or type(document.get("units")) is not dict):
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_INVALID")
    units = document["units"]
    unit, mpu = units.get("drawing_units"), units.get("meters_per_unit")
    if (type(unit) is not str or unit not in ps.UNITS or type(mpu) is not float
            or mpu != ps.UNITS[unit]):
        raise GraphValidationError("PHYSICAL_STATE_UNITS_UNSUPPORTED")
    try:
        terrain_adapter.document_frame(document)
    except terrain_adapter.TerrainAdapterError:
        raise GraphValidationError("PHYSICAL_SHADE_FRAME_UNSUPPORTED") from None
    except (LookupError, TypeError, AttributeError, ValueError, OverflowError):
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_INVALID") from None
    try:
        grid = terrain_adapter.document_grid(document)
        dtm = terrain.terrain_interpolator(deepcopy(grid), mpu)
    except terrain_adapter.TerrainAdapterError as exc:
        codes = {"TERRAIN_FRAME_UNSUPPORTED": "PHYSICAL_SHADE_FRAME_UNSUPPORTED",
                 "TERRAIN_GRID_TOO_LARGE": "PHYSICAL_SHADE_INPUT_LIMIT_EXCEEDED",
                 "TERRAIN_GRID_MISSING": "PHYSICAL_SHADE_GRID_MISSING"}
        raise GraphValidationError(codes.get(exc.code, "PHYSICAL_SHADE_GRID_INVALID")) from None
    except (LookupError, TypeError, AttributeError, ValueError, OverflowError):
        raise GraphValidationError("PHYSICAL_SHADE_GRID_INVALID") from None
    if dtm is None:
        raise GraphValidationError("PHYSICAL_SHADE_GRID_INVALID")
    state = document["state"]
    frames = state.get("frames", [])
    if type(frames) is not list:
        raise GraphValidationError("PHYSICAL_SHADE_FRAMES_INVALID")
    if len(frames) > 20_000:
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_LIMIT_EXCEEDED")
    if any(type(entity) is not dict for entity in frames):
        raise GraphValidationError("PHYSICAL_SHADE_FRAMES_INVALID")
    if type(state.get("tracker_rows", [])) is not list:
        raise GraphValidationError("PHYSICAL_SHADE_INPUT_INVALID")
    frames = deepcopy(frames)
    try:
        samples = shade.read_panel_centres(frames, dtm, mpu)
    except (shade.ShadeInputError, LookupError, TypeError, AttributeError, ValueError, OverflowError):
        raise GraphValidationError("PHYSICAL_SHADE_FRAMES_INVALID") from None
    if not samples:
        code = ("PHYSICAL_SHADE_MANUAL_ROWS_UNSUPPORTED" if state.get("tracker_rows")
                else "PHYSICAL_SHADE_NATIVE_FRAMES_REQUIRED")
        raise GraphValidationError(code)
    return {"frames": frames, "dtm": dtm, "meters_per_unit": mpu, "settings": {}}
