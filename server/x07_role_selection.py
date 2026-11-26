"""Pure selection of confirmed module INSERT origins in drawing XY units.

Rectangles and closed drawing outlines include their edges without an implicit
tolerance. Explicit exclusion outlines remove their edges too. A zero-area
rectangular window is valid. Block/layer names match case-insensitively, like CAD symbols.
Each matching model-space occurrence counts once at its world XY origin.
For a single root INSERT, its own rotation, scale and Z do not affect membership.
Matching placements are keyed by their complete handle and array path.
Repeated paths refuse the operation. Root records retain case-insensitive
handle checks and the existing handling of unidentified coincident records.
"""
from collections import Counter
import math
import re
import unicodedata

from authored_block_transform import identity_frame, insert_frame, transform_point
from solar_drawing_roles import discover_roles


MAX_OCCURRENCES = 100_000
MAX_DEPTH = 64
MAX_OUTLINE_VERTICES = 128
MAX_EXCLUSION_OUTLINES = 4
MAX_OUTLINE_CHOICES = 64
MAX_OVERRIDE_PLACEMENTS = 256
MAX_PLACEMENT_PATH_LENGTH = MAX_DEPTH * 47
MAX_OUTLINE_EXTENT = math.sqrt(float.fromhex('0x1.fffffffffffffp1023')) / 4
HANDLE = re.compile('[0-9A-Fa-f]{1,32}')
PATH_COMPONENT = re.compile(r'[0-9A-Fa-f]{1,32}\[(?:0|[1-9][0-9]{0,5}),(?:0|[1-9][0-9]{0,5})\]')
ORIGIN = (0, 0, 0)


class SelectionError(ValueError):
    pass


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False  # unsafe conversion of an unbounded integer to CAD double


def _handle(row):
    value = row.get('handle')
    if isinstance(value, str) and HANDLE.fullmatch(value):
        return value.upper()
    return None


def _matching_occurrences(intake, blocks, layer, *, normalize=str.casefold):
    definitions = intake.get('blocks', {})
    if not isinstance(definitions, dict) or any(not isinstance(name, str) for name in definitions):
        raise SelectionError('block definitions must be keyed by name')
    definitions = {name.casefold(): value for name, value in definitions.items()}
    root = identity_frame()
    remaining = MAX_OCCURRENCES

    def visit(row, index, parent, prefix, ancestors, inherited_layer):
        nonlocal remaining
        if not isinstance(row, dict):
            raise SelectionError('INSERT record must be an object')
        if not all(isinstance(row.get(key), str) for key in ('name', 'layer')):
            raise SelectionError('INSERT names must be strings')
        if row.get('space') == 'paper':
            return
        name = row['name'].casefold()
        effective_layer = inherited_layer if row['layer'] == '0' else row['layer']
        labels = row['name'], effective_layer
        matches = normalize(labels[0]) in blocks and normalize(labels[1]) == layer
        definition = definitions.get(name, {})
        if not isinstance(definition, dict):
            raise SelectionError('block definition must be an object')
        children = definition.get('nested_inserts', [])
        if not isinstance(children, list):
            raise SelectionError('nested INSERT records must be a list')
        if name in ancestors or definition.get('nested_inserts_incomplete'):
            raise SelectionError('incomplete or cyclic nested INSERT references')
        if not matches and not children:
            return
        rows, columns = row.get('rows', 1), row.get('columns', 1)
        if type(rows) is not int or type(columns) is not int or min(rows, columns) < 1:
            raise SelectionError('invalid INSERT array size')
        simple = not ancestors and rows == columns == 1
        if not simple or children:
            remaining -= rows * columns
            if remaining < 0 or len(ancestors) >= MAX_DEPTH:
                raise SelectionError('INSERT placement limit exceeded')
        x, y = row.get('x'), row.get('y')
        if not _finite(x) or not _finite(y):
            raise SelectionError('matching INSERT origin is not finite')
        handle = _handle(row)
        if simple and not children:
            path = f'{handle}[0,0]' if handle else None
            yield index, row.get('handle'), path, x, y, True, labels
            return
        base = definition.get('base', ORIGIN)
        for r in range(rows):
            for c in range(columns):
                path = f'{prefix}{handle}[{r},{c}]' if prefix is not None and handle else None
                try:
                    frame = insert_frame(parent, row, base, r, c)
                    if simple:
                        world_x, world_y = x, y
                    else:
                        world_x, world_y, _ = transform_point(frame, base)
                except (TypeError, ValueError, OverflowError, IndexError, KeyError):
                    raise SelectionError('invalid INSERT placement transform') from None
                if not _finite(world_x) or not _finite(world_y):
                    raise SelectionError('matching INSERT origin is not finite')
                if matches:
                    yield index, row.get('handle'), path, world_x, world_y, simple, labels
                child_prefix = path + '/' if path is not None else None
                for child in children:
                    yield from visit(child, index, frame, child_prefix,
                                     ancestors + (name,), effective_layer)

    for index, row in enumerate(intake['inserts']):
        yield from visit(row, index, root, '', (), '0')



def nested_module_spellings(intake, blocks, layer, *, normalize):
    """Return matched nested model-space block and effective layer spellings."""
    definitions = intake.get('blocks', {})
    if not isinstance(definitions, dict) or not any(
        isinstance(definition, dict) and definition.get('nested_inserts')
        for definition in definitions.values()
    ):
        return set()
    return {labels for _, _, _, _, _, simple, labels in
            _matching_occurrences(intake, blocks, layer, normalize=normalize) if not simple}


def _orientation(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _between(a, b, point):
    return all(min(start, end) <= pos <= max(start, end)
               for start, end, pos in zip(a, b, point))


def _edges_intersect(a, b, c, d):
    ac, ad = _orientation(a, b, c), _orientation(a, b, d)
    ca, cb = _orientation(c, d, a), _orientation(c, d, b)
    if (ac < 0 < ad or ad < 0 < ac) and (ca < 0 < cb or cb < 0 < ca):
        return True
    return ((ac == 0 and _between(a, b, c)) or (ad == 0 and _between(a, b, d))
            or (ca == 0 and _between(c, d, a)) or (cb == 0 and _between(c, d, b)))


def _outline_contains(row):
    points = row.get('pts')
    bulges = row.get('bulges', [])
    if (row.get('space') == 'paper' or row.get('layout', 'Model') not in ('Model', '')
            or row.get('closed') is not True or row.get('thickness', 0) != 0
            or not isinstance(bulges, (list, tuple)) or any(bulges)
            or not isinstance(points, (list, tuple))
            or not 3 <= len(points) <= MAX_OUTLINE_VERTICES + 1
            or any(not isinstance(point, (list, tuple)) or len(point) not in (2, 3)
                   or not all(_finite(value) for value in point) for point in points)
            or len({point[2] if len(point) == 3 else 0 for point in points}) != 1):
        raise SelectionError('outline must be a closed straight-edge model-space polyline parallel to XY')

    vertices = [tuple(point[:2]) for point in points]
    if vertices[0] == vertices[-1]:
        vertices.pop()
    count = len(vertices)
    if not 3 <= count <= MAX_OUTLINE_VERTICES or len(set(vertices)) != count:
        raise SelectionError('outline must have distinct vertices')
    xs, ys = zip(*vertices)
    xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
    if max(xmax - xmin, ymax - ymin) > MAX_OUTLINE_EXTENT:
        raise SelectionError('outline extent is too large')
    edges = list(zip(vertices, vertices[1:] + vertices[:1]))
    # Check adjacent backtracking and all non-adjacent contacts before using
    # the even-odd rule. Collinear vertices along a straight edge are valid.
    for i, (a, b) in enumerate(edges):
        previous = vertices[i - 1]
        if _orientation(previous, a, b) == 0 and (
                _between(previous, a, b) or _between(a, b, previous)):
            raise SelectionError('outline doubles back')
        stop = count - 1 if i == 0 else count
        if any(_edges_intersect(a, b, c, d) for c, d in edges[i + 2:stop]):
            raise SelectionError('outline intersects itself')

    def contains(x, y):
        if not xmin <= x <= xmax or not ymin <= y <= ymax:
            return False
        inside = False
        for a, b in edges:
            side = _orientation(a, b, (x, y))
            if side == 0 and _between(a, b, (x, y)):
                return True
            if (a[1] > y) != (b[1] > y) and (side > 0) == (b[1] > a[1]):
                inside = not inside
        return inside

    return contains, vertices


def _polygon_edges(vertices):
    """Compare straight-edge boundaries independently of winding and vertex spacing."""
    corners = [point for i, point in enumerate(vertices)
               if _orientation(vertices[i - 1], point, vertices[(i + 1) % len(vertices)]) != 0]
    return frozenset(frozenset((a, b)) for a, b in zip(corners, corners[1:] + corners[:1]))


def _selection_window(intake, window):
    if not isinstance(window, dict):
        if (not isinstance(window, (list, tuple)) or len(window) != 4
                or not all(_finite(value) for value in window)):
            raise SelectionError('window must have four finite coordinates')
        xmin, ymin, xmax, ymax = window
        if xmin > xmax or ymin > ymax:
            raise SelectionError('window bounds are reversed')
        return list(window), lambda x, y: xmin <= x <= xmax and ymin <= y <= ymax, None

    if window.keys() - {'outline_handle', 'exclude_handles'}:
        raise SelectionError('unknown outline selection fields')
    excluded = window.get('exclude_handles', [])
    if not isinstance(excluded, list) or len(excluded) > MAX_EXCLUSION_OUTLINES:
        raise SelectionError('invalid exclusion outline list')
    handles = [window.get('outline_handle'), *excluded]
    if any(not isinstance(handle, str) or not HANDLE.fullmatch(handle) for handle in handles):
        raise SelectionError('valid outline handles required')
    handles = [handle.upper() for handle in handles]
    if len(set(handles)) != len(handles):
        raise SelectionError('outline handles must be distinct')

    polylines = intake.get('polylines', [])
    if not isinstance(polylines, list):
        raise SelectionError('intake must contain a polyline list')
    outlines = {}
    for row in polylines:
        if not isinstance(row, dict):
            raise SelectionError('polyline record must be an object')
        handle = _handle(row)
        if handle in handles:
            if handle in outlines:
                raise SelectionError('outline handle is ambiguous')
            outlines[handle] = _outline_contains(row)
    if len(handles) != len(outlines):
        raise SelectionError('outline handle is absent on the selected revision')

    (included, vertices), *exclusions = [outlines[handle] for handle in handles]
    normalized = {'outline_handle': handles[0], 'exclude_handles': handles[1:]}

    def contains(x, y):
        return included(x, y) and not any(exclude(x, y) for exclude, _ in exclusions)

    return normalized, contains, (vertices, [points for _, points in exclusions])


def _outline_geometry(outline):
    if outline is None:
        return None  # A rectangle uses the same explicit bounds for both revisions.
    vertices, exclusions = outline
    return _polygon_edges(vertices), frozenset(_polygon_edges(points) for points in exclusions)


def outline_choices(intake):
    """Return a bounded set of outline suggestions for this exact drawing."""
    polylines = intake.get('polylines')
    if not isinstance(polylines, list):
        return []
    candidates, seen, ambiguous = {}, set(), set()
    for row in polylines:
        if not isinstance(row, dict):
            continue
        handle = _handle(row)
        if handle is None:
            continue
        if handle in seen:
            ambiguous.add(handle)
        seen.add(handle)
        if row.get('closed') is True and len(candidates) < MAX_OUTLINE_CHOICES:
            candidates.setdefault(handle, row)
    choices = []
    for handle, row in candidates.items():
        if handle in ambiguous:
            continue
        try:
            _outline_contains(row)
        except SelectionError:
            continue
        choices.append({'handle': handle, 'layer': row.get('layer') or ''})
    return choices


def roof_inheritance_candidates(intake, roof, outline_suggestions):
    """Preview current alternatives when the saved roof role is absent."""
    if not roof:
        return []

    def label(value):
        return unicodedata.normalize('NFC', value).casefold()

    polylines = [row for row in (intake.get('polylines') or [])
                 if isinstance(row, dict) and isinstance(row.get('layer'), str)]
    inserts = [row for row in (intake.get('inserts') or [])
               if isinstance(row, dict) and isinstance(row.get('layer'), str)
               and isinstance(row.get('name'), str)]
    entities = polylines + inserts
    saved_layer = label(roof['layer'])
    saved_block = roof.get('block')
    if saved_block:
        if any(label(row['layer']) == saved_layer and label(row['name']) == label(saved_block)
               for row in inserts):
            return []
    elif any(label(row['layer']) == saved_layer for row in entities):
        return []

    handles = Counter(_handle(row) for row in entities)
    layers = Counter(label(row['layer']) for row in entities)
    outlines = {row['handle'] for row in outline_suggestions}
    candidates = []
    for row in polylines:
        handle, layer = _handle(row), row['layer']
        if handle in outlines and handles[handle] == 1 and layer.strip() and len(layer) <= 512 and layers[label(layer)] == 1:
            vertices = row['pts']
            xs, ys = [p[0] for p in vertices], [p[1] for p in vertices]
            candidates.append({'handle': handle, 'role': {'layer': layer}, 'kind': 'outline',
                               'bounds': [min(xs), min(ys), max(xs), max(ys)]})

    roof_pairs = {(row['block'], row['layer']) for row in discover_roles(intake)['roof']}
    insert_pairs = Counter((label(row['name']), label(row['layer'])) for row in inserts)
    for row in inserts:
        handle, block, layer = _handle(row), row['name'], row['layer']
        if ((block, layer) in roof_pairs and handle and handles[handle] == 1
                and insert_pairs[label(block), label(layer)] == 1 and layer.strip() and len(layer) <= 512
                and block.strip() and len(block) <= 512 and not block.startswith('*')
                and row.get('space') != 'paper' and row.get('layout', 'Model') in ('Model', '')
                and _finite(row.get('x')) and _finite(row.get('y'))):
            candidates.append({'handle': handle, 'role': {'block': block, 'layer': layer}, 'kind': 'insert',
                               'bounds': [row['x'], row['y'], row['x'], row['y']]})
            if len(candidates) >= MAX_OUTLINE_CHOICES:
                break
    return candidates[:MAX_OUTLINE_CHOICES]


def _placement_window(window):
    if not isinstance(window, dict) or 'placement_paths' not in window:
        return window, None
    if window.keys() != {'within', 'placement_paths'}:
        raise SelectionError('placement override requires a containing design window')
    paths = window['placement_paths']
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_OVERRIDE_PLACEMENTS:
        raise SelectionError('invalid placement override count')
    if any(not isinstance(path, str) or len(path) > MAX_PLACEMENT_PATH_LENGTH
           or len(path.split('/')) > MAX_DEPTH
           or not all(PATH_COMPONENT.fullmatch(part) for part in path.split('/'))
           for path in paths):
        raise SelectionError('invalid placement override path')
    wanted = {path.upper() for path in paths}
    if len(wanted) != len(paths):
        raise SelectionError('placement override paths must be distinct')
    return window['within'], wanted


def _select_modules(intake, *, block, layer, window, allow_missing):
    if not all(isinstance(value, str) and value and len(value) <= 1024
               for value in (block, layer)):
        raise SelectionError('confirmed block and layer are required')
    if not isinstance(intake, dict) or not isinstance(intake.get('inserts'), list):
        raise SelectionError('intake must contain an INSERT list')
    within, wanted = _placement_window(window)
    window, contains, outline = _selection_window(intake, within)
    if wanted is not None:
        window = {'within': window, 'placement_paths': sorted(wanted)}
    indices, handles, placements, seen = [], [], [], set()
    drawing_origins = {}
    expanded = False
    occurrences = _matching_occurrences(intake, {block.casefold()}, layer.casefold())
    for index, handle, path, x, y, simple, _ in occurrences:
        identity = path
        if identity is None and simple and isinstance(handle, str):
            identity = handle.casefold()
        if identity:
            if identity in seen:
                raise SelectionError('duplicate matching INSERT handle')
            seen.add(identity)
        if allow_missing and wanted is not None and path in wanted:
            drawing_origins[path] = [x, y]
        if (wanted is None or path in wanted) and contains(x, y):
            indices.append(index)
            if not simple and path is None:
                raise SelectionError('missing handle in selected INSERT placement path')
            if isinstance(handle, str):
                handles.append(handle)
            placements.append({'path': path, 'origin': [x, y]})
            expanded |= not simple
    if wanted is not None and not allow_missing and len(placements) != len(wanted):
        raise SelectionError('placement override no longer matches the selected window and role')
    result = {'block': block, 'layer': layer, 'design_window': window,
              'boundary': 'inclusive_insert_origin', 'count': len(indices),
              'insert_indices': indices, 'handles': handles}
    if expanded or wanted is not None:
        result['placements'] = placements
    return result, outline, drawing_origins


def select_modules(intake, *, block, layer, window):
    selection, _, _ = _select_modules(intake, block=block, layer=layer, window=window, allow_missing=False)
    return selection


def compare_placement_override(previous, current, *, block, layer, window):
    original, previous_outline, _ = _select_modules(
        previous, block=block, layer=layer, window=window, allow_missing=False)
    if not isinstance(original['design_window'], dict) or 'placement_paths' not in original['design_window']:
        raise SelectionError('placement override paths are required')
    selection, current_outline, drawing_origins = _select_modules(
        current, block=block, layer=layer, window=window, allow_missing=True)
    origins = {item['path']: item['origin'] for item in selection['placements']}
    changes = []
    for item in original['placements']:
        path, previous_origin = item['path'], item['origin']
        origin = origins.get(path)
        status = ('missing' if origin is None else
                  'unchanged' if origin == previous_origin else 'moved')
        changes.append({'path': path, 'status': status,
                        'previous_origin': previous_origin, 'current_origin': origin,
                        'current_drawing_origin': drawing_origins.get(path)})
    return {'selection': selection, 'placements': changes,
            'boundary_changed': _outline_geometry(previous_outline) != _outline_geometry(current_outline),
            'all_paths_match': len(origins) == original['count']}
