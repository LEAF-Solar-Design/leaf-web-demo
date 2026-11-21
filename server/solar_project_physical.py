"""Terrain preview from the selected, verified canonical drawing version."""
import project_solar_materialization as native
import solar_ground_terrain_adapter as terrain

VIEW_SCHEMA = "leaf.solar-project-physical-view.v1"
TERRAIN_RESPONSE_SCHEMA = "leaf.solar-terrain-view-response.v1"


def terrain_view(verified):
    binding = verified.binding
    head, document = None, None
    if native.KEY in verified.intake:
        head, document, _ = native._canonical_state(verified)
    result = {
        "schema": TERRAIN_RESPONSE_SCHEMA,
        "stored": head is not None,
        "head": head,
        "terrain": None if head is None else dict(
            terrain.terrain_view(document), drawing_id=str(binding.drawing_id),
            project_id=str(binding.project_id)),
    }
    return {
        "schema": VIEW_SCHEMA,
        "view": "terrain",
        "context": {
            "organization_id": str(binding.organization_id),
            "project_id": str(binding.project_id),
            "drawing_id": str(binding.drawing_id),
            "version_id": str(binding.input_version_id),
            "head_version_id": str(binding.head_version_id),
            "is_head": binding.is_head,
            "intake_sha256": verified.intake_sha256,
        },
        "result": result,
    }
