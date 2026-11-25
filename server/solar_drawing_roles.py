"""Read-only drawing role proposals, independent of customer identity or state."""

from collections import defaultdict
import math

SCHEMA = 'leaf.drawing-role-discovery.v1'


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _word(text, words):
    text = text.casefold().replace('_', ' ').replace('-', ' ')
    return any(word in text for word in words)


def _rectangle(block):
    if not isinstance(block, dict):
        return False
    children = block.get('children', [])
    if not isinstance(children, list):
        return False
    for child in children:
        if not isinstance(child, dict) or not child.get('closed'):
            continue
        points = child.get('pts')
        if not isinstance(points, list) or len(points) not in (4, 5):
            continue
        if not all(isinstance(p, (list, tuple)) and len(p) >= 2
                   and _number(p[0]) and _number(p[1]) for p in points):
            continue
        width = max(p[0] for p in points) - min(p[0] for p in points)
        height = max(p[1] for p in points) - min(p[1] for p in points)
        if min(width, height) > 0 and 1.1 <= max(width, height) / min(width, height) <= 3.5:
            return True
    return False


def discover_roles(intake):
    """Count INSERT sets and their insertion-coordinate extents; mutate nothing.

    Naming evidence outranks repeated rectangular geometry. Geometry-only sets
    remain visible as competing options. Bounds locate inserts in drawing units;
    they do not claim to be transformed block geometry or a selected design copy.
    """
    groups = defaultdict(list)
    if not isinstance(intake, dict):
        intake = {}
    inserts = intake.get('inserts', [])
    if not isinstance(inserts, list):
        inserts = []
    for row in inserts:
        if (isinstance(row, dict) and isinstance(row.get('name'), str)
                and row['name'] and isinstance(row.get('layer'), str)
                and _number(row.get('x')) and _number(row.get('y'))):
            groups[row['name'], row['layer']].append((row['x'], row['y']))
    blocks = intake.get('blocks', {})
    if not isinstance(blocks, dict):
        blocks = {}
    roles = {'module': [], 'roof': [], 'obstruction': []}
    for (name, layer), points in groups.items():
        module_layer = _word(layer, ('module', 'panel', 'photovoltaic'))
        module_name = _word(name, ('module', 'panel', 'photovoltaic'))
        obstruction = _word(name + ' ' + layer, ('obst', 'keepout', 'chimney', 'skylight'))
        roof = _word(name + ' ' + layer, ('roof', 'building outline'))
        rectangle = _rectangle(blocks.get(name))
        score = 6 * module_layer + 3 * module_name + 2 * rectangle
        row = {'block': name, 'layer': layer, 'count': len(points),
               'insertion_bounds': [min(p[0] for p in points), min(p[1] for p in points),
                                    max(p[0] for p in points), max(p[1] for p in points)]}
        if module_layer or module_name or (rectangle and len(points) >= 4 and not (roof or obstruction)):
            roles['module'].append({**row, 'score': score,
                                    'basis': 'names' if module_layer or module_name else 'repeated rectangular block'})
        if obstruction:
            roles['obstruction'].append(row)
        elif roof:
            roles['roof'].append(row)
    for role, rows in roles.items():
        rows.sort(key=lambda row: (-row.get('score', 0), -row['count'], row['layer'], row['block']))
    return {'schema': SCHEMA, 'module': {'first_choice': next(iter(roles['module']), None),
             'candidates': roles['module'], 'competing': len(roles['module']) > 1},
            'roof': roles['roof'], 'obstruction': roles['obstruction']}
