"""BSP6: audit actual native-engine or browser-exported rooftop DXF bytes.

Run with Python's -P option from outside the repository to avoid its platform
package shadowing the standard library. Requires installed ezdxf and, unless
--saved is supplied, a real wasm-pack --target nodejs build in pkg-node.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

import ezdxf

ORIGINAL_SHA = "27a1a309ca8c55580770bba1925a6624ef9e963106412a9b3db9db9e12eeda62"
HERE = Path(__file__).resolve().parent
NODE_EXPORT = r"""
import {readFileSync, writeFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const [glue, fixture, saved, reopened] = process.argv.slice(1);
const engine = await import(pathToFileURL(glue).href);
const original = new Uint8Array(readFileSync(fixture));
const doc = engine.parseDxf(original);
const target = doc.editableEntities().find(e => String(e.handle) === String(0xA100));
if (!target || !Number.isInteger(target.index)) throw new Error('fixture target A100 missing');
doc.translateEntity(target.index, 12, 6);
const bytes = engine.writeDxf(doc);
writeFileSync(saved, bytes);
const back = engine.parseDxf(bytes);
writeFileSync(reopened, engine.writeDxf(back));
console.log(JSON.stringify({producer:'compiled-acadrust', target:'A100', dx:12, dy:6,
  entities:back.editableEntities().length, bytes:bytes.length}));
"""


def rounded(value):
    if isinstance(value, (tuple, list)) or hasattr(value, "__iter__") and not isinstance(value, str):
        return [rounded(item) for item in value]
    return round(float(value), 8) if isinstance(value, (float, int)) else value


def snapshot(entity):
    kind = entity.dxftype()
    common = {key: entity.dxf.get(key, default) for key, default in (
        ("layer", "0"), ("color", 256), ("linetype", "BYLAYER"), ("lineweight", -1),
        ("ltscale", 1), ("invisible", 0), ("true_color", None), ("transparency", None))}
    if kind == "LWPOLYLINE":
        geometry = {"points": list(entity.get_points("xyseb")), "closed": entity.closed,
                    "elevation": entity.dxf.elevation, "extrusion": entity.dxf.extrusion}
    elif kind == "LINE":
        geometry = {"start": entity.dxf.start, "end": entity.dxf.end}
    elif kind in ("CIRCLE", "ARC"):
        geometry = {"center": entity.dxf.center, "radius": entity.dxf.radius}
        if kind == "ARC":
            geometry.update(start_angle=entity.dxf.start_angle, end_angle=entity.dxf.end_angle)
    elif kind == "TEXT":
        geometry = {key: getattr(entity.dxf, key) for key in ("insert", "text", "height", "rotation", "style")}
    elif kind == "POINT":
        geometry = {"location": entity.dxf.location}
    else:
        raise AssertionError(f"unexpected original fixture type: {kind}")
    return {"kind": kind, "common": common, "geometry": {k: rounded(v) for k, v in geometry.items()}}


def audit_output(path, expected):
    lines = path.read_text().splitlines()
    assert len(lines) % 2 == 0, "odd DXF pair count"
    handles = []
    section = record = ""
    for code, value in zip(lines[::2], lines[1::2]):
        code, value = int(code), value.strip()
        if code == 0:
            record = value
            if value == "ENDSEC":
                section = ""
        elif code == 2 and record == "SECTION":
            section = value
        if code in (5, 105) and section != "HEADER":
            handles.append(value.upper())
    duplicates = {handle: count for handle, count in Counter(handles).items() if count > 1}
    assert not duplicates, f"duplicate record handles: {duplicates}"
    document = ezdxf.readfile(path)
    actual = {entity.dxf.handle: snapshot(entity) for entity in document.modelspace()}
    assert document.units == 1, f"units changed: {document.units}"
    assert len(actual) == 80, f"entity count changed: {len(actual)}"
    assert actual == expected, f"semantic mismatch: {[h for h in actual.keys() | expected.keys() if actual.get(h) != expected.get(h)]}"
    audit = document.audit()
    assert not audit.errors, [(item.code, item.message) for item in audit.errors]
    assert not audit.fixes, [(item.code, item.message) for item in audit.fixes]
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "entities": len(actual), "units": "inches", "audit_errors": 0, "audit_fixes": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--saved", type=Path, help="audit an actual browser Download without producing new bytes")
    args = parser.parse_args()
    fixture = HERE / "fixtures" / "bsp6-rooftop.dxf"
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == ORIGINAL_SHA
    baseline = ezdxf.readfile(fixture)
    audit = baseline.audit()
    assert not audit.errors and not audit.fixes
    baseline.entitydb["A100"].translate(12, 6, 0)
    expected = {entity.dxf.handle: snapshot(entity) for entity in baseline.modelspace()}
    if args.saved:
        outputs = [args.saved.resolve()]
    else:
        assert args.out_dir, "supply --out-dir for native export or --saved for browser export"
        args.out_dir.mkdir(parents=True, exist_ok=True)
        glue = next((HERE / "pkg-node" / name for name in ("acadrust_worker.js", "engine.js")
                     if (HERE / "pkg-node" / name).is_file()), None)
        assert glue, "missing compiled native engine, this is not a passing or skipped result"
        outputs = [args.out_dir / "bsp6-moved.dxf", args.out_dir / "bsp6-reopened.dxf"]
        result = subprocess.run(["node", "--input-type=module", "-e", NODE_EXPORT, str(glue), str(fixture),
                                 *(str(path.resolve()) for path in outputs)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr or result.stdout
        print(result.stdout.strip())
    print(json.dumps({"original_sha256": ORIGINAL_SHA, "checks": [audit_output(path, expected) for path in outputs]}, indent=2))


if __name__ == "__main__":
    main()
