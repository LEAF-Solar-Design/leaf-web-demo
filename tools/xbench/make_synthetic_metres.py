"""Create an anonymous metre-unit module-only DXF from an inch-unit DXF."""

import argparse
import hashlib
import json
import math
from pathlib import Path

import x01_geometry as geometry


def generate(source, layers, blocks, output, *, rotate_deg=17, offset=(1000, 2000)):
    ezdxf, _, _, _ = geometry.dependencies()
    doc = geometry.load(Path(source).read_bytes())
    if geometry.insunits(doc) not in (0, 1):
        raise ValueError("source DXF must use inch units (INSUNITS 0 or 1)")
    if not all(math.isfinite(value) for value in (rotate_deg, *offset)):
        raise ValueError("rotation and offset must be finite")
    target = ezdxf.new("R2010")
    target.header["$INSUNITS"] = 6
    target.layers.new("MODULES")
    cosine, sine = math.cos(math.radians(rotate_deg)), math.sin(math.radians(rotate_deg))
    count = 0
    for _, footprint in geometry.module_footprints(doc, layers, blocks):
        polygons = [footprint] if footprint.geom_type == "Polygon" else list(footprint.geoms)
        for polygon in polygons:
            if polygon.interiors:
                raise ValueError("module footprint with holes cannot be a closed LWPOLYLINE")
            points = []
            for x, y in list(polygon.exterior.coords)[:-1]:
                x, y = x * 0.0254, y * 0.0254
                points.append((x * cosine - y * sine + offset[0],
                               x * sine + y * cosine + offset[1]))
            target.modelspace().add_lwpolyline(points, close=True, dxfattribs={"layer": "MODULES"})
            count += 1
    target.saveas(str(output))
    content = Path(output).read_bytes()
    return {"modules": count, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--layers", nargs="+", required=True)
    parser.add_argument("--blocks", nargs="*", default=[])
    parser.add_argument("--out", required=True)
    parser.add_argument("--rotate-deg", type=float, default=17)
    parser.add_argument("--offset", type=float, nargs=2, default=(1000, 2000))
    args = parser.parse_args(argv)
    print(json.dumps(generate(args.source, args.layers, args.blocks, args.out,
                              rotate_deg=args.rotate_deg, offset=args.offset)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
