"""Compare geometry-only panel group membership across exact saved revisions."""
import math
import re
from collections import Counter

import solar_panel_group_kernel as kernel
from solar_design_graph import GraphValidationError
from solar_project_graph import resolve_project_graph_context

MAX_CHANGES = 256
_SOURCE_HANDLE = re.compile(r'[0-9A-Fa-f]{1,32}')


def _handle(value):
    if not isinstance(value, str) or not _SOURCE_HANDLE.fullmatch(value):
        raise GraphValidationError('PANEL_GROUP_GEOMETRY_UNAVAILABLE')
    return value.upper().lstrip('0') or '0'


def _snapshot(context):
    intake = context.intake
    graph = intake.get('solar_design_graph')
    if graph is None:
        raise GraphValidationError('PANEL_GROUP_GRAPH_UNAVAILABLE')
    project = graph['project']
    layer = graph['settings']['panel_layer_contains']
    design = project['installation_design']
    frames = {frame['id']: frame for frame in graph['frames']}
    if not frames or any(frame['extra'].get('grouping_basis') != 'geometry-only'
                         or frame['provenance']['created_by'] != 'solar-panel-groups'
                         for frame in frames.values()):
        raise GraphValidationError('PANEL_GROUP_GRAPH_UNAVAILABLE')
    try:
        measured = kernel.panels_from_intake(intake, layer_contains=layer, installation_design=design)
    except kernel.PanelGroupKernelError as exc:
        raise GraphValidationError('PANEL_GROUP_GEOMETRY_UNAVAILABLE') from exc
    source = {_handle(panel['handle']): panel for panel in measured}
    if len(source) != len(measured):
        raise GraphValidationError('PANEL_GROUP_GEOMETRY_UNAVAILABLE')
    panels = {}
    for panel in graph['panels']:
        provenance = panel['provenance']
        handle = _handle(provenance.get('source_handle'))
        geometry = source.get(handle)
        if (provenance['created_by'] != 'solar-panels-from-drawing'
                or provenance['source_hash'] != graph['source_hash']
                or handle in panels or geometry is None
                or any(not math.isclose(a, b, rel_tol=1e-9, abs_tol=kernel.INTAKE_LENGTH_TOL)
                       for a, b in zip(geometry['centre'], panel['centre'], strict=True))):
            raise GraphValidationError('PANEL_GROUP_GEOMETRY_UNAVAILABLE')
        panels[handle] = (panel['frame_ref'], (geometry['column_dim'], geometry['row_dim']))
    if panels.keys() != source.keys():
        raise GraphValidationError('PANEL_GROUP_GEOMETRY_UNAVAILABLE')
    return (project['id'], project['units']['drawing_units'], design, layer), frames, panels


def _panel_details(panel, frames):
    if panel is None:
        return None
    frame_ref, dimensions = panel
    frame = frames.get(frame_ref)
    return {'group': None if frame is None else {
        'name': frame['name'], 'panel_count': len(frame['panel_refs']),
    }, 'dimensions': list(dimensions)}


def compare_panel_groups(previous, current):
    previous, current = map(resolve_project_graph_context, (previous, current))
    if ((previous.organization_id, previous.project_id, previous.drawing_id)
            != (current.organization_id, current.project_id, current.drawing_id)
            or previous.parent_version_id == current.parent_version_id):
        raise GraphValidationError('PANEL_GROUP_REVISIONS_INCOMPARABLE')
    before_basis, before_frames, before = _snapshot(previous)
    after_basis, after_frames, after = _snapshot(current)
    if before_basis != after_basis:
        raise GraphValidationError('PANEL_GROUP_REVISIONS_INCOMPARABLE')

    shared = before.keys() & after.keys()
    # Graph IDs are revision-local. Compare the overlap of each pair of groups by DXF handle.
    overlap = Counter((before[handle][0], after[handle][0]) for handle in shared)
    changed = []
    for handle in sorted(before.keys() | after.keys(), key=kernel.handle_sort_key):
        left, right = before.get(handle), after.get(handle)
        if left is None or right is None:
            membership_changed, size_changed = None, None
        else:
            old_frame, new_frame = left[0], right[0]
            if old_frame is None or new_frame is None:
                membership_changed = old_frame != new_frame
            else:
                membership_changed = not (len(before_frames[old_frame]['panel_refs'])
                    == overlap[old_frame, new_frame] == len(after_frames[new_frame]['panel_refs']))
            size_changed = any(not kernel.v_close(a, b, kernel.INTAKE_LENGTH_TOL)
                               for a, b in zip(left[1], right[1], strict=True))
            if not membership_changed and not size_changed:
                continue
        if len(changed) == MAX_CHANGES:
            raise GraphValidationError('PANEL_GROUP_CHANGE_LIMIT_EXCEEDED')
        changed.append({'handle': handle, 'previous': _panel_details(left, before_frames),
                        'current': _panel_details(right, after_frames),
                        'membership_changed': membership_changed, 'size_changed': size_changed})
    return {'previous_input_version_id': str(previous.parent_version_id),
            'input_version_id': str(current.parent_version_id), 'drawing_units': before_basis[1],
            'previous_panel_count': len(before), 'current_panel_count': len(after), 'changes': changed}
