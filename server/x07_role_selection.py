"""Pure selection of confirmed module INSERT origins in drawing XY units.

Bounds are inclusive on all four edges, with no implicit tolerance. A zero-area
window is valid. Block/layer names match case-insensitively, like CAD symbols.
Each matching INSERT record counts once; block extents, rotation, scale and Z
do not affect origin membership. Invalid matching coordinates refuse the count.
Repeated nonempty handles among matching records refuse the operation (CAD
handles compare case-insensitively). Coincident distinct or unidentified records
remain separate INSERTs; coordinates alone never establish duplicate identity.
"""
import math


class SelectionError(ValueError):
    pass


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def select_modules(intake, *, block, layer, window):
    if not all(isinstance(value, str) and value and len(value) <= 1024
               for value in (block, layer)):
        raise SelectionError('confirmed block and layer are required')
    if (not isinstance(window, (list, tuple)) or len(window) != 4
            or not all(_finite(value) for value in window)):
        raise SelectionError('window must have four finite coordinates')
    xmin, ymin, xmax, ymax = window
    if xmin > xmax or ymin > ymax:
        raise SelectionError('window bounds are reversed')
    if not isinstance(intake, dict) or not isinstance(intake.get('inserts'), list):
        raise SelectionError('intake must contain an INSERT list')
    indices, handles, seen = [], [], set()
    for index, row in enumerate(intake['inserts']):
        if not isinstance(row, dict):
            raise SelectionError('INSERT record must be an object')
        if not all(isinstance(row.get(key), str) for key in ('name', 'layer')):
            raise SelectionError('INSERT names must be strings')
        if row['name'].casefold() != block.casefold() or row['layer'].casefold() != layer.casefold():
            continue
        handle = row.get('handle')
        if isinstance(handle, str) and handle:
            identity = handle.casefold()
            if identity in seen:
                raise SelectionError('duplicate matching INSERT handle')
            seen.add(identity)
        x, y = row.get('x'), row.get('y')
        if not _finite(x) or not _finite(y):
            raise SelectionError('matching INSERT origin is not finite')
        if xmin <= x <= xmax and ymin <= y <= ymax:
            indices.append(index)
            if isinstance(row.get('handle'), str):
                handles.append(row['handle'])
    return {'block': block, 'layer': layer, 'design_window': list(window),
            'boundary': 'inclusive_insert_origin', 'count': len(indices),
            'insert_indices': indices, 'handles': handles}
