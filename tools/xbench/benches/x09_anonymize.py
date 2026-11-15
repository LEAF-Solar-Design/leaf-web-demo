"""Offline, geometry-only DXF reconstruction. ezdxf is an optional dependency.

No source entity, table, dictionary or header (except INSUNITS) is copied.
Nonuniform INSERT scales are supported for straight geometry; curved geometry
under a nonsimilarity is refused because these output primitives cannot encode it.
"""

import argparse
import math
import os
from pathlib import Path
import secrets


DATA_ROOT = Path("D:/agent-data/leaf-research")
MAX_DEPTH = 32
MAX_ENTITIES = 100_000
MAX_VERTICES = 1_000_000
_TEXT = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}


def _number(value):
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("invalid numeric geometry") from None
    if not math.isfinite(result):
        raise ValueError("nonfinite numeric geometry")
    return result


def _point(value):
    values = tuple(_number(v) for v in value)
    if len(values) not in (2, 3) or (len(values) == 3 and values[2] != 0):
        raise ValueError("geometry must be in the XY plane")
    return values[:2]


def _compose(a, b):
    # a after b: x'=a*x+c*y+e, y'=b*x+d*y+f.
    aa, ab, ac, ad, ae, af = a
    ba, bb, bc, bd, be, bf = b
    return tuple(_number(v) for v in (
        aa*ba+ac*bb, ab*ba+ad*bb, aa*bc+ac*bd, ab*bc+ad*bd,
        aa*be+ac*bf+ae, ab*be+ad*bf+af))


def _map(matrix, point):
    x, y = _point(point)
    a, b, c, d, e, f = matrix
    return (_number(a*x+c*y+e), _number(b*x+d*y+f))


def _plane(entity):
    thickness = entity.dxf.get("thickness", 0) if entity.dxf.is_supported("thickness") else 0
    if _number(thickness) != 0:
        raise ValueError("nonplanar thickness")
    extrusion = tuple(_number(v) for v in entity.dxf.get("extrusion", (0, 0, 1)))
    if extrusion not in ((0., 0., 1.), (0., 0., -1.)):
        raise ValueError("nonplanar extrusion")
    return extrusion[2]


def _similarity(matrix):
    a, b, c, d, _, _ = matrix
    first, second = math.hypot(a, b), math.hypot(c, d)
    if (first == 0 or second == 0 or
            not math.isclose(first, second, rel_tol=1e-10, abs_tol=0) or
            abs((a/first)*(c/second)+(b/first)*(d/second)) > 1e-10):
        raise ValueError("curved geometry requires a uniform orthogonal INSERT transform")
    return first, 1 if a*d-b*c > 0 else -1


def _layers(document, module_layers, roof_layers):
    known = {layer.dxf.name.casefold() for layer in document.layers}

    def normalize(values):
        if isinstance(values, (str, bytes)) or values is None:
            raise ValueError("layer allowlists must be nonempty collections")
        try:
            values = list(values)
        except TypeError:
            raise ValueError("invalid layer allowlist") from None
        if not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError("layer allowlists must contain explicit names")
        names = [v.casefold() for v in values]
        if len(set(names)) != len(names) or not set(names) <= known:
            raise ValueError("duplicate or unknown layer in allowlist")
        return set(names)

    modules, roofs = normalize(module_layers), normalize(roof_layers)
    if modules & roofs:
        raise ValueError("ambiguous layer roles")
    return {**dict.fromkeys(modules, "MODULE"), **dict.fromkeys(roofs, "ROOF")}


def anonymize_document(source_doc, module_layers, roof_layers):
    """Return a fresh Drawing containing only explicitly selected XY geometry.

    Fail closed on incompatible block children, cycles, excessive expansion,
    unknown layers and unsupported selected primitives. Text is always dropped.
    The random rigid transform exists only in this call's local numeric state.
    """
    import ezdxf

    roles = _layers(source_doc, module_layers, roof_layers)
    known = {layer.dxf.name.casefold() for layer in source_doc.layers}
    units = source_doc.header.get("$INSUNITS", 0)
    if type(units) is not int or not 0 <= units <= 24:
        raise ValueError("invalid INSUNITS")
    output = ezdxf.new("R2010")
    output.header["$INSUNITS"] = units
    for name in ("MODULE", "ROOF"):
        output.layers.new(name)
    space = output.modelspace()
    rng = secrets.SystemRandom()
    angle = rng.uniform(0, math.tau)
    cosine, sine = math.cos(angle), math.sin(angle)
    rigid = (cosine, sine, -sine, cosine,
             rng.uniform(-1_000_000, 1_000_000), rng.uniform(-1_000_000, 1_000_000))
    emitted = 0
    visited = 0
    vertices = 0
    expansions = 0

    def emit(entity, role, matrix):
        nonlocal emitted, vertices
        emitted += 1
        if emitted > MAX_ENTITIES:
            raise ValueError("emitted entity limit exceeded")
        kind = entity.dxftype()
        if kind not in {"LINE", "LWPOLYLINE", "CIRCLE", "ARC"}:
            raise ValueError("unsupported selected geometry")
        normal = _plane(entity)
        attrs = {"layer": role}
        if kind == "LINE":
            space.add_line(_map(matrix, entity.dxf.start), _map(matrix, entity.dxf.end),
                           dxfattribs=attrs)
            return
        # These entity types use OCS; negative Z reverses its X axis.
        matrix = _compose(matrix, (normal, 0, 0, 1, 0, 0))
        if kind == "LWPOLYLINE":
            if _number(entity.dxf.get("elevation", 0)) != 0:
                raise ValueError("nonplanar polyline elevation")
            points = []
            constant = _number(entity.dxf.get("const_width", 0))
            if constant < 0:
                raise ValueError("negative width")
            scale, orientation = None, None
            for x, y, start, end, bulge in entity.get_points("xyseb"):
                vertices += 1
                if vertices > MAX_VERTICES:
                    raise ValueError("vertex limit exceeded")
                start, end, bulge = _number(start), _number(end), _number(bulge)
                if start < 0 or end < 0:
                    raise ValueError("negative width")
                if constant or start or end or bulge:
                    if scale is None:
                        scale, orientation = _similarity(matrix)
                px, py = _map(matrix, (x, y))
                points.append((px, py, _number((constant or start)*(scale or 1)),
                               _number((constant or end)*(scale or 1)),
                               bulge*(orientation or 1)))
            if len(points) < 2:
                raise ValueError("polyline needs at least two vertices")
            space.add_lwpolyline(points, format="xyseb", close=entity.closed, dxfattribs=attrs)
            return
        if kind in ("CIRCLE", "ARC"):
            scale, orientation = _similarity(matrix)
            center = _map(matrix, entity.dxf.center)
            radius = _number(_number(entity.dxf.radius)*scale)
            if radius <= 0:
                raise ValueError("radius must be positive")
            if kind == "CIRCLE":
                space.add_circle(center, radius, dxfattribs=attrs)
            else:
                def direction(degrees):
                    theta = math.radians(_number(degrees))
                    a, b, c, d, _, _ = matrix
                    x, y = math.cos(theta), math.sin(theta)
                    return math.degrees(math.atan2(b*x+d*y, a*x+c*y)) % 360
                start, end = direction(entity.dxf.start_angle), direction(entity.dxf.end_angle)
                if orientation < 0:
                    start, end = end, start
                space.add_arc(center, radius, start, end, dxfattribs=attrs)
            return
        raise ValueError("unsupported selected geometry")

    def walk(entities, matrix, parent_role=None, stack=()):
        nonlocal visited, expansions
        expansions += 1
        if expansions > MAX_ENTITIES:
            raise ValueError("INSERT expansion limit exceeded")
        if len(stack) > MAX_DEPTH:
            raise ValueError("INSERT recursion limit exceeded")
        for entity in entities:
            visited += 1
            if visited > MAX_ENTITIES * 4:
                raise ValueError("entity traversal limit exceeded")
            kind = entity.dxftype()
            if kind in _TEXT:
                continue
            layer = entity.dxf.get("layer", None)
            if not isinstance(layer, str) or layer.casefold() not in known:
                raise ValueError("entity has missing or unknown layer")
            layer = layer.casefold()
            role = roles.get(layer)
            if parent_role is not None:
                if layer == "0":
                    role = parent_role
                elif role != parent_role:
                    raise ValueError("incompatible child layer role")
            if role is None:
                continue
            if kind != "INSERT":
                emit(entity, role, matrix)
                continue
            normal = _plane(entity)
            name = entity.dxf.name
            key = name.casefold()
            if key in stack:
                raise ValueError("cyclic INSERT")
            block = source_doc.blocks.get(name)
            if block is None or block.block.is_xref or block.block.is_xref_overlay:
                raise ValueError("missing or external block")
            bx, by = _point(block.block.dxf.base_point)
            ix, iy = _point(entity.dxf.insert)
            sx = _number(entity.dxf.get("xscale", 1))
            sy = _number(entity.dxf.get("yscale", 1))
            sz = _number(entity.dxf.get("zscale", 1))
            if sx == 0 or sy == 0 or sz == 0:
                raise ValueError("degenerate INSERT scale")
            theta = math.radians(_number(entity.dxf.get("rotation", 0)))
            co, si = math.cos(theta), math.sin(theta)
            rows, columns = entity.dxf.get("row_count", 1), entity.dxf.get("column_count", 1)
            if (type(rows) is not int or type(columns) is not int or
                    rows < 1 or columns < 1 or rows*columns > MAX_ENTITIES):
                raise ValueError("invalid INSERT array size")
            rs = _number(entity.dxf.get("row_spacing", 0))
            cs = _number(entity.dxf.get("column_spacing", 0))
            for row in range(rows):
                for column in range(columns):
                    # Array spacing rotates with the INSERT but does not scale.
                    x = ix+co*column*cs-si*row*rs
                    y = iy+si*column*cs+co*row*rs
                    local = (normal*co*sx, si*sx, -normal*si*sy, co*sy,
                             normal*(x-co*sx*bx+si*sy*by), y-si*sx*bx-co*sy*by)
                    walk(block, _compose(matrix, local), role, stack+(key,))

    walk(source_doc.modelspace(), rigid)
    return output


def _safe_path(value, *, output=False):
    """Resolve only beneath the fixed Windows data root, rejecting symlinks."""
    if os.name != "nt":
        raise ValueError("file I/O requires the fixed Windows data root")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("an absolute path inside the data root is required")
    root = DATA_ROOT
    # Check every component before resolve, including components above the root.
    for part in (path, *path.parents, root, *root.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("symlinks and junctions are forbidden")
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=not output)
    if not resolved.is_relative_to(resolved_root) or resolved == resolved_root:
        raise ValueError("path is outside the data root")
    if output:
        if path.exists() or not resolved.parent.is_dir():
            raise ValueError("output must be new in an existing directory")
    elif not resolved.is_file():
        raise ValueError("input must be a regular file")
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--module-layer", action="append", required=True)
    parser.add_argument("--roof-layer", action="append", required=True)
    args = parser.parse_args(argv)
    try:
        source = _safe_path(args.input)
        target = _safe_path(args.output, output=True)
        if source == target:
            raise ValueError("input and output must differ")
        import ezdxf

        result = anonymize_document(ezdxf.readfile(source), args.module_layer, args.roof_layer)
        # Exclusive creation prevents overwriting even if another writer races us.
        with target.open("x", encoding="utf-8") as stream:
            result.write(stream)
    except (ValueError, OSError):
        # Do not disclose paths, entity identifiers or geometry in diagnostics.
        parser.exit(2, "anonymization refused\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
