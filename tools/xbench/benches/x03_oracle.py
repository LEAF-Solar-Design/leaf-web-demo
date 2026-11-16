"""Independent, offline DXF oracles for the frozen X03 authoring evaluation.

No product parsers or product results are used. Customer values belong only in
the external prompt file. All queries refer to top-level model-space entities.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

import ezdxf
from ezdxf import bbox


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def selected(doc, query):
    return [e for e in doc.modelspace()
            if (not query.get("layer") or e.dxf.layer == query["layer"])
            and (not query.get("type") or e.dxftype() == query["type"])]


def oracle(doc, query):
    entities = selected(doc, query)
    operation = query["operation"]
    if operation == "count":
        return len(entities)
    if operation == "handles":
        return sorted(e.dxf.handle for e in entities)
    if operation == "layers":
        return dict(sorted(Counter(e.dxf.layer for e in entities).items()))
    if operation == "blocks":
        return dict(sorted(Counter(e.dxf.name for e in entities
                                   if e.dxftype() == "INSERT").items()))
    if operation == "bounds":
        bounds = bbox.extents(entities, fast=False)
        if not bounds.has_data:
            raise ValueError("selected entities have no computable bounds")
        return [bounds.extmin.x, bounds.extmin.y, bounds.extmax.x, bounds.extmax.y]
    if operation == "line_length":
        return sum(math.hypot(e.dxf.end.x - e.dxf.start.x,
                              e.dxf.end.y - e.dxf.start.y)
                   for e in entities if e.dxftype() == "LINE")
    if operation == "closed":
        return sum(bool(e.closed) for e in entities if e.dxftype() == "LWPOLYLINE")
    if operation == "vertices":
        return sum(len(e) for e in entities if e.dxftype() == "LWPOLYLINE")
    if operation == "polyline_length":
        total = 0.0
        for entity in entities:
            if entity.dxftype() != "LWPOLYLINE":
                continue
            points = list(entity.get_points("xyb"))
            pairs = list(zip(points, points[1:]))
            if entity.closed and points:
                pairs.append((points[-1], points[0]))
            for start, end in pairs:
                chord = math.hypot(end[0] - start[0], end[1] - start[1])
                bulge = start[2]
                total += (chord if not bulge else
                          chord * (1 + bulge * bulge) / abs(4 * bulge) * abs(4 * math.atan(bulge)))
        return total
    raise ValueError("unknown oracle operation")


def build_rows(drawings):
    """Bind 15 queries per solar drawing, 5 per structural, 10 mechanical."""
    rows = []
    expected = Counter(solar=30, structural=10, mechanical=10)
    for drawing in drawings:
        doc = ezdxf.readfile(drawing["dxf_path"])
        trade = drawing["trade"]
        count = 15 if trade == "solar" else 5 if trade == "structural" else 10
        layers = [name for name, _ in Counter(e.dxf.layer for e in doc.modelspace()).most_common()]
        if not layers:
            raise ValueError("drawing has no model-space entities")
        queries = [
            ({"operation": "count"}, "Count all entities"),
            ({"operation": "layers"}, "Count entities per layer, keyed by exact layer name"),
            ({"operation": "count", "type": "LINE"}, "Count LINE entities"),
            ({"operation": "line_length", "type": "LINE"}, "Sum the 2D lengths of LINE entities in drawing units"),
            ({"operation": "handles", "type": "INSERT"}, "List the handles of INSERT entities as a sorted string array"),
            ({"operation": "count", "type": "LWPOLYLINE"}, "Count LWPOLYLINE entities"),
            ({"operation": "closed", "type": "LWPOLYLINE"}, "Count closed LWPOLYLINE entities"),
            ({"operation": "count", "type": "CIRCLE"}, "Count CIRCLE entities"),
            ({"operation": "blocks", "type": "INSERT"}, "Group INSERT entities by exact block name and return the count for each name"),
            ({"operation": "count", "type": "TEXT"}, "Count TEXT entities"),
        ]
        for layer in layers[:3]:
            queries.append(({"operation": "count", "layer": layer},
                            f"Count entities on the exact layer {json.dumps(layer)}"))
        for layer in layers[:2]:
            queries.append(({"operation": "bounds", "layer": layer},
                            f"Return the XY geometric bounding box [minX,minY,maxX,maxY] of entities on the exact layer {json.dumps(layer)}"))
        queries.extend([
            ({"operation": "polyline_length", "type": "LWPOLYLINE"},
             "Sum the 2D path lengths of LWPOLYLINE entities, including bulge arcs and the closing segment of each closed polyline"),
            ({"operation": "vertices", "type": "LWPOLYLINE"},
             "Count all stored vertices in LWPOLYLINE entities without counting an implicit closing vertex twice"),
            ({"operation": "handles", "type": "LWPOLYLINE"},
             "List the handles of LWPOLYLINE entities as a sorted string array"),
        ])
        # Structural queries cover both drawings without repeating a generic count-only set.
        if trade == "structural":
            queries = [queries[i] for i in (1, 2, 3, 4, 8)]
        for query, instruction in queries[:count]:
            value = oracle(doc, query)
            compare = {"mode": "absolute", "tolerance": 0.001} if query["operation"] in ("bounds", "line_length", "polyline_length") else {"mode": "exact"}
            rows.append({"id": f"X03-{len(rows)+1:02d}", "trade": trade,
                         "drawing": drawing["id"],
                         "prompt": "Create a read-only CAD tool. " + instruction +
                         ' in top-level model space only, without expanding block references. Return only a JSON object with the key "value" holding the result. Do not hardcode drawing values.',
                         "oracle": {"value": value}, "compare": compare,
                         "oracle_method": {"script": "tools/xbench/benches/x03_oracle.py",
                                           "query": query, "dxf_sha256": digest(drawing["dxf_path"])}})
    if len(rows) != 50 or Counter(r["trade"] for r in rows) != expected:
        raise ValueError("requires two solar, two structural and one mechanical drawing")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bindings", required=True,
                        help="private JSON list of id, trade and dxf_path")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    target = Path(args.out)
    if target.exists():
        raise SystemExit("refusing to overwrite a frozen prompt set")
    drawings = json.loads(Path(args.bindings).read_text(encoding="utf-8-sig"))
    payload = {"schema": "leaf.xbench.x03-prompts.v1", "rows": build_rows(drawings),
               "oracle_script_sha256": digest(__file__)}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(digest(target))


if __name__ == "__main__":
    main()
