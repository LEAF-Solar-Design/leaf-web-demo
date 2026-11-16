"""Pure, model-space DXF measurements for the X01 deliverable grader."""

from collections import defaultdict
import io
import math


NEEDS = "X01 needs ezdxf and shapely: pip install ezdxf shapely"
MAX_DXF_BYTES = 64 * 1024 * 1024


def dependencies():
    """Keep optional CAD libraries out of bench discovery and CLI startup."""
    try:
        import ezdxf
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        from shapely.strtree import STRtree
    except ImportError as exc:
        raise RuntimeError(NEEDS) from exc
    return ezdxf, Polygon, unary_union, STRtree


def load(dxf_bytes):
    if len(dxf_bytes) > MAX_DXF_BYTES:
        raise ValueError("DXF exceeds 64 MiB")
    ezdxf, _, _, _ = dependencies()
    # Normalize DXF CRLF endings so ezdxf parses header and entity values correctly.
    return ezdxf.read(io.StringIO(dxf_bytes.decode("utf-8", errors="replace"), newline=None))


def insunits(doc):
    return int(doc.header.get("$INSUNITS", 0))


def _modules(doc, layers, blocks):
    layers = {name.casefold() for name in layers}
    blocks = {name.casefold() for name in blocks}
    for entity in doc.modelspace():
        if entity.dxf.layer.casefold() not in layers:
            continue
        if blocks:
            if entity.dxftype() == "INSERT" and entity.dxf.name.casefold() in blocks:
                yield entity
        elif (entity.dxftype() == "LWPOLYLINE" and entity.closed and len(entity) == 4):
            yield entity


def _polyline_polygon(entity):
    _, Polygon, _, _ = dependencies()
    from ezdxf.path import make_path

    vertices = list(make_path(entity).flattening(distance=0.0001))
    polygon = Polygon([(point.x, point.y) for point in vertices])
    if polygon.is_empty or polygon.area <= 0 or not polygon.is_valid:
        raise ValueError("invalid module/fill polygon")
    return polygon


def module_points(doc, layers, blocks):
    points = []
    for entity in _modules(doc, layers, blocks):
        if entity.dxftype() == "INSERT":
            point = entity.dxf.insert
            x, y = point.x, point.y
        else:
            point = _polyline_polygon(entity).centroid
            x, y = point.x, point.y
        points.append((entity.dxf.handle, float(x), float(y)))
    return points


def match_positions(original, exported, tol):
    """Reserve equal handles first; search only adjacent tolerance grid cells.

    A moved same-handle entity is consumed, but is not a successful match.
    Renumbered entities outside tolerance are reported as missing and extra.
    """
    if not math.isfinite(tol) or tol <= 0:
        raise ValueError("tolerance must be finite and positive")
    original, exported = list(original), list(exported)
    if any(not math.isfinite(value) for row in (*original, *exported) for value in row[1:]):
        raise ValueError("non-finite module position")
    by_handle = {point[0]: i for i, point in enumerate(exported)}
    used, pending, moved, missing = set(), [], [], []
    matched = 0
    for handle, x, y in original:
        index = by_handle.get(handle)
        if index is None or index in used:
            pending.append((handle, x, y))
            continue
        used.add(index)
        distance = math.hypot(x - exported[index][1], y - exported[index][2])
        if distance <= tol:
            matched += 1
        else:
            moved.append((handle, distance))
    buckets = defaultdict(set)
    for index, (_, x, y) in enumerate(exported):
        if index not in used:
            buckets[(math.floor(x / tol), math.floor(y / tol))].add(index)
    for handle, x, y in pending:
        cx, cy = math.floor(x / tol), math.floor(y / tol)
        nearest, distance = None, float("inf")
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for index in buckets.get((cx + dx, cy + dy), ()):
                    candidate = exported[index]
                    delta = math.hypot(x - candidate[1], y - candidate[2])
                    if delta <= tol and (delta < distance or
                                        (delta == distance and index < nearest)):
                        nearest, distance = index, delta
        if nearest is None:
            missing.append(handle)
        else:
            used.add(nearest)
            _, px, py = exported[nearest]
            buckets[(math.floor(px / tol), math.floor(py / tol))].remove(nearest)
            matched += 1
    return {"matched": matched, "missing": missing, "moved": moved,
            "extra": len(exported) - len(used)}


def layer_flags(doc):
    return {layer.dxf.name: {"off": layer.is_off(), "frozen": layer.is_frozen(),
                             "plot": bool(layer.dxf.get("plot", 1))}
            for layer in doc.layers}


def _tag_entities(doc, layers):
    layers = {layer.casefold() for layer in layers}
    for entity in doc.modelspace():
        if entity.dxf.layer.casefold() in layers:
            if entity.dxftype() in ("TEXT", "MTEXT"):
                yield entity


def entities_on_bad_layers(doc, handles_or_layers):
    selected = {str(value).casefold() for value in handles_or_layers}
    flags = {name.casefold(): value for name, value in layer_flags(doc).items()}
    failures = []
    for parent in doc.modelspace():
        entities = [parent, *parent.attribs] if parent.dxftype() == "INSERT" else [parent]
        for entity in entities:
            handle, layer = entity.dxf.handle, entity.dxf.layer
            if handle.casefold() not in selected and layer.casefold() not in selected:
                continue
            state = flags.get(layer.casefold())
            if state is None:
                failures.append((handle, layer, "missing layer"))
                continue
            reasons = [name for name in ("off", "frozen") if state[name]]
            if not state["plot"]:
                reasons.append("non-plotting")
            if reasons:
                failures.append((handle, layer, ", ".join(reasons)))
    return failures


def module_footprints(doc, layers, blocks):
    _, _, unary_union, _ = dependencies()
    footprints = []
    for entity in _modules(doc, layers, blocks):
        if entity.dxftype() == "INSERT":
            polygons = []

            def collect(children):
                for child in children:
                    kind = child.dxftype()
                    if kind == "INSERT":
                        collect(child.virtual_entities())
                    elif ((kind == "LWPOLYLINE" and child.closed) or
                          (kind == "POLYLINE" and child.is_closed)):
                        polygons.append(_polyline_polygon(child))

            collect(entity.virtual_entities())
            if not polygons:
                raise ValueError(f"module {entity.dxf.handle} has no closed footprint")
            polygon = unary_union(polygons)
        else:
            polygon = _polyline_polygon(entity)
        footprints.append((entity.dxf.handle, polygon))
    return footprints


def _fill_polygon(entity):
    _, Polygon, _, _ = dependencies()
    from ezdxf.path import from_hatch

    if entity.dxftype() != "HATCH":
        # make_path handles DXF's swapped third/fourth SOLID vertices and OCS.
        return _polyline_polygon(entity)
    result = Polygon()
    for path in from_hatch(entity):
        vertices = list(path.flattening(distance=0.0001))
        if len(vertices) < 3:
            raise ValueError("invalid hatch boundary")
        polygon = Polygon([(point.x, point.y) for point in vertices])
        if not polygon.is_valid:
            raise ValueError("invalid hatch boundary")
        # Nested hatch boundaries alternate filled regions and islands.
        result = result.symmetric_difference(polygon)
    return result


def fill_cover(doc, footprints):
    _, _, _, STRtree = dependencies()
    if not footprints:
        return []
    polygons = [polygon for _, polygon in footprints]
    tree = STRtree(polygons)
    msp = doc.modelspace()
    order = (msp.entities_in_redraw_order() if list(msp.get_redraw_order()) else iter(msp))
    entities = list(order)
    rank = {entity.dxf.handle: i for i, entity in enumerate(entities)}
    failures = []
    for entity in entities:
        if entity.dxftype() not in ("HATCH", "SOLID", "TRACE"):
            continue
        fill = _fill_polygon(entity)
        if fill.is_empty:
            continue
        for index in tree.query(fill):
            handle, polygon = footprints[int(index)]
            if rank[entity.dxf.handle] <= rank[handle] or polygon.area <= 0:
                continue
            fraction = polygon.intersection(fill).area / polygon.area
            if fraction > 0.05 + 1e-12:
                failures.append((handle, entity.dxf.handle, fraction))
    return failures


def mm_per_unit(doc):
    units = insunits(doc)
    if units not in (0, 1, 6):
        raise ValueError(f"unsupported INSUNITS {units}")
    return 1000.0 if units == 6 else 25.4


def tag_checks(doc, tag_layers, sheet_scale=None, *, model_units_per_paper_mm=None):
    """Measure tags using recorded model-units/mm or viewport paper-mm/model-unit."""
    _, Polygon, _, STRtree = dependencies()
    from ezdxf import bbox

    if model_units_per_paper_mm is not None:
        scale = float(model_units_per_paper_mm)
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("sheet scale must be finite and positive")
        paper_factor = 1.0 / scale
    else:
        if sheet_scale is None or not math.isfinite(sheet_scale) or sheet_scale <= 0:
            raise ValueError("sheet scale must be finite and positive")
        paper_factor = sheet_scale
    handles, boxes, too_small = [], [], []
    for entity in _tag_entities(doc, tag_layers):
        is_mtext = entity.dxftype() == "MTEXT"
        height = float(entity.dxf.char_height if is_mtext else entity.dxf.height)
        if not math.isfinite(height) or height <= 0:
            raise ValueError(f"invalid tag height: {entity.dxf.handle}")
        paper_mm = height * paper_factor
        if paper_mm < 1.8 - 1e-12:
            too_small.append((entity.dxf.handle, paper_mm))
        bounds = bbox.extents([entity], fast=False)
        if not bounds.has_data:
            raise ValueError(f"tag has no text bounds: {entity.dxf.handle}")
        left, bottom = bounds.extmin.x, bounds.extmin.y
        right, top = bounds.extmax.x, bounds.extmax.y
        if (not all(math.isfinite(value) for value in (left, bottom, right, top))
                or right <= left or top <= bottom):
            raise ValueError(f"invalid tag bounds: {entity.dxf.handle}")
        # ezdxf accounts for font metrics, alignment, MTEXT wrapping and rotation.
        boxes.append(Polygon([(left, bottom), (right, bottom), (right, top), (left, top)]))
        handles.append(entity.dxf.handle)
    overlaps = []
    if boxes:
        tree = STRtree(boxes)
        for i, box in enumerate(boxes):
            for index in tree.query(box):
                j = int(index)
                smaller = min(box.area, boxes[j].area)
                if j <= i or smaller <= 0:
                    continue
                fraction = box.intersection(boxes[j]).area / smaller
                if fraction > 0.1 + 1e-12:
                    overlaps.append((handles[i], handles[j], fraction))
    return {"too_small": too_small, "overlaps": overlaps, "count": len(handles)}


def sheet_scale(doc):
    """Return paper mm per model unit from the first model viewport."""
    for layout in doc.layouts:
        if layout.name == "Model":
            continue
        for viewport in layout.query("VIEWPORT"):
            if viewport.dxf.id <= 1:
                continue  # ID 1 is the paper-space overview, not a model viewport.
            height, view_height = viewport.dxf.height, viewport.dxf.view_height
            if (math.isfinite(height) and math.isfinite(view_height) and
                    height > 0 and view_height > 0):
                paper_mm = 25.4 if layout.dxf.get("plot_paper_units", 1) == 0 else 1.0
                return float(height / view_height) * paper_mm
            return None
    return None


def render_pdf(doc, path):
    dependencies()
    try:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from ezdxf.addons.drawing import Frontend, RenderContext
        from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    except ImportError as exc:
        raise RuntimeError("X01 evidence plot needs matplotlib: pip install matplotlib") from exc
    figure = Figure()
    FigureCanvasAgg(figure)
    axes = figure.add_axes([0, 0, 1, 1])
    try:
        Frontend(RenderContext(doc), MatplotlibBackend(axes)).draw_layout(doc.modelspace(), finalize=True)
        figure.savefig(str(path), format="pdf")
    finally:
        figure.clear()
