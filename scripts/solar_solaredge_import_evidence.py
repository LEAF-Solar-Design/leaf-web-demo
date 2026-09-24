#!/usr/bin/env python3
"""Studio's strings evidence for ImportSolarEdgePDF (capability import-solaredge-pdf).

Reads the SolarEdge PDF (server/solar_solaredge_pdf.py, S1, and server/solar_solaredge_parse.py,
S2) and the drawing intake (the panel centroids and the panel groups PanelGroupCreate made, in the
plugin's selection order), runs the command port (server/solar_solaredge_import.py, S3 and S4),
and writes the strings the plugin draws in the comparator's strings family
(scripts/solar_w1_compare.py), shaped like the plugin's own document
(Branch2025 tools/parity/plugin_evidence.py --source bthost-strings):

- after.strings: one record per drawn string, ordered_membership in the plugin's CableXData
  order, polarity "positive" (the declared negative-to-positive convention both sides use, not a
  measurement, so it is named in fallback_fields as the plugin names it); records sorted by the
  neutral id "string:<first panel handle>";
- after.unassigned_panels: grouped panels no string holds; duplicate_panels: panels in two or
  more strings; length_distribution: sorted string lengths;
- entity_mapping: every panel handle to itself (rule 8), every string's Studio id to its neutral
  id; fixture_sha256 the PDF bytes, revision the git commit that last touched the PDF, input_sha256
  over fixture and parameters.

Honest flags. The group row angle is reconstructed from the centroid lattice
(solar_solaredge_import.lattice_row_angle) because the intake carries block rotation only, not
the block definition's rectangle rotation the plugin adds; that is named in fallback_fields and
the angle and its source go to provenance. versions.catalog and versions.solver carry the values
the plugin document declares, and both documents name them synthetic.

Fails closed: an unreadable input, a parse or import refusal, a string longer than
max_string_length, or a document the comparator refuses is a named error and nothing is written.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The server modules import one another by bare name when loaded outside the package.
if str(ROOT / "server") not in sys.path:
    sys.path.insert(0, str(ROOT / "server"))
pdf = _load("solar_solaredge_pdf", ROOT / "server" / "solar_solaredge_pdf.py")
parse = _load("solar_solaredge_parse", ROOT / "server" / "solar_solaredge_parse.py")
engine = _load("solar_solaredge_import", ROOT / "server" / "solar_solaredge_import.py")
compare = _load("solar_w1_compare", HERE / "solar_w1_compare.py")

DEFAULT_PDF = ROOT / "data" / "solaredge_1to1_demo.pdf"
DEFAULT_INTAKE = ROOT / "docs" / "parity" / "evidence" / "solaredge" / "se-pg-intake.json"
DEFAULT_OUT = Path("C:/tmp/solar-parity/se-ev/studio-evidence.json")
INTAKE_FORMAT = "solaredge-import-intake-v1"
MAX_INTAKE_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT = 15
CAPABILITY = "import-solaredge-pdf"
CAPABILITY_VERSION = "0"
MAX_STRING_LENGTH = 40
# The plugin strings document's values for the compared version labels; both sides flag them.
PLUGIN_CATALOG = "none"
PLUGIN_SOLVER = "leaf-stringer-service"
ROW_ANGLE_FALLBACK = "provenance/row_angle/reconstructed-from-centroid-lattice"


class EvidenceError(ValueError):
    """A named refusal: nothing is written."""


def _serialize(doc):
    """Compact canonical JSON, under the comparator reader's 2 MB bound."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n"


def fixture_revision(path):
    """The commit that last touched the fixture; a fixture git does not track is refused."""
    path = Path(path).resolve()

    def git(*args):
        return subprocess.run(["git", *args], cwd=path.parent, capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, check=True).stdout.strip()
    try:
        git("ls-files", "--error-unmatch", "--", path.name)
        rev = git("log", "-1", "--format=%H", "--", path.name)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError(f"fixture revision unavailable: {exc}") from None
    if not re.fullmatch(r"[0-9a-f]{40}", rev):
        raise EvidenceError("fixture has no committed revision")
    return rev


def load_intake(path):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_INTAKE_BYTES:
        raise EvidenceError("intake exceeds its byte bound")
    intake = json.loads(raw.decode("utf-8"))
    if not isinstance(intake, dict) or intake.get("format") != INTAKE_FORMAT:
        raise EvidenceError(f"intake is not {INTAKE_FORMAT}")
    for key, kind in (("panels", list), ("groups", list), ("settings", dict), ("units", str)):
        if not isinstance(intake.get(key), kind):
            raise EvidenceError(f"intake.{key} is missing or malformed")
    tolerance = intake["settings"].get("AlignmentTolerance")
    if type(tolerance) not in (int, float) or not tolerance > 0:
        raise EvidenceError("intake.settings.AlignmentTolerance must be a positive number")
    if intake["units"] not in compare.LENGTH_UNITS:
        raise EvidenceError("intake.units is not a known length unit")
    return intake, hashlib.sha256(raw).hexdigest()


def row_angle_for(intake):
    """The panel angle the plugin's PanelGroup takes, and a record of how it was obtained."""
    centres = [(p["x"], p["y"]) for p in intake["panels"]]
    lattice = engine.lattice_row_angle(centres)
    half = intake.get("half_extents")
    if not (isinstance(half, list) and len(half) == 2 and all(type(v) in (int, float) and v > 0 for v in half)):
        raise EvidenceError("intake.half_extents must be two positive numbers")
    block_rotation = intake.get("row_angle", 0.0)
    if type(block_rotation) not in (int, float) or block_rotation != 0:
        # A nonzero block rotation would need its own term; the fixture's panels are all at 0.
        raise EvidenceError("intake.row_angle (block rotation) other than 0 is not supported")
    # Roof: the row runs along the rectangle's long side (PanelBlockDefinition, Roof branch). The
    # definition's extents say which lattice axis that is: wider than tall means the lattice
    # direction near 0, otherwise the one near 90 degrees.
    angle = lattice if half[0] >= half[1] else lattice + math.pi / 2
    if angle < 0:
        angle += math.pi  # PanelGroup folds into [0, pi)
    return angle, {"value": angle, "lattice_direction": lattice, "block_rotation": block_rotation,
                   "source": "centroid-lattice"}


def run(pdf_path=DEFAULT_PDF, intake_path=DEFAULT_INTAKE, revision=None,
        max_string_length=MAX_STRING_LENGTH):
    """Build the Studio strings evidence document (validated, not yet written)."""
    pdf_path = Path(pdf_path)
    try:
        pdf_bytes = pdf_path.read_bytes()
        intake, intake_sha256 = load_intake(intake_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"input unreadable: {exc}") from None
    if revision is None:
        revision = fixture_revision(pdf_path)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvidenceError("revision must be a 40-character lower-case git commit id")
    if type(max_string_length) is not int or not 1 <= max_string_length <= 900:
        raise EvidenceError("max_string_length must be an int in 1..900")
    try:
        prims = pdf.extract_primitives(pdf_bytes)
        parsed, matrices = parse.parse_primitives(prims, pdf_path.stem)
        angle, angle_record = row_angle_for(intake)
        result = engine.run_import(
            matrices, intake["groups"], intake["panels"], row_angle=angle,
            alignment_tolerance=intake["settings"]["AlignmentTolerance"],
            selection_order="recorded")
    except (pdf.SolarEdgePdfError, parse.SolarEdgeParseError, engine.SolarEdgeImportError) as exc:
        raise EvidenceError(f"import refused: {exc}") from None
    return build_document(pdf_bytes, intake, intake_sha256, result, angle_record, revision,
                          max_string_length, len(matrices))


def build_document(pdf_bytes, intake, intake_sha256, result, angle_record, revision,
                   max_string_length, pdf_grid_count):
    drawn = result["strings"] + result["bridge_strings"]
    mapping, records, owners, sources = {}, [], {}, {}
    for index, string in enumerate(drawn):
        members = string["panels"]
        if not members:
            raise EvidenceError(f"string {index} is empty")
        if len(members) > max_string_length:
            raise EvidenceError(f"string {index} has {len(members)} panels, over {max_string_length}")
        neutral = "string:" + members[0]
        studio_id = f"se-string-{index + 1}"
        if neutral in sources:
            raise EvidenceError(f"duplicate neutral string id {neutral}")
        sources[neutral] = {"studio_id": studio_id, "inverter_id": string["inverter_id"],
                            "string_input_number": string["string_input_number"],
                            "partial": string["partial"], **string["source"]}
        mapping[studio_id] = neutral
        for handle in members:
            mapping[handle] = handle
            owners.setdefault(handle, set()).add(studio_id)
        records.append({"id": {"entity_id": studio_id},
                        "ordered_membership": [{"entity_id": h} for h in members],
                        "polarity": "positive"})
    records.sort(key=lambda record: mapping[record["id"]["entity_id"]])
    for handle in result["unassigned"]:
        mapping[handle] = handle
    after = {
        "strings": records,
        "unassigned_panels": [{"entity_id": h} for h in sorted(result["unassigned"])],
        "duplicate_panels": [{"entity_id": h} for h in sorted(owners) if len(owners[h]) > 1],
        "length_distribution": sorted(len(r["ordered_membership"]) for r in records),
    }
    fixture = hashlib.sha256(pdf_bytes).hexdigest()
    parameters = {"family": "strings", "max_string_length": max_string_length}
    fallback = ["elapsed_ms", "frame", ROW_ANGLE_FALLBACK]
    fallback += [f"after/strings/{i}/polarity" for i in range(len(records))]
    doc = {
        "fixture_sha256": fixture,
        "input_sha256": compare.semantic_hash({"fixture_sha256": fixture, "parameters": parameters}),
        "output_sha256": compare.semantic_hash(after),
        "revision": revision,
        "versions": {"schema": compare.SCHEMA, "producer": "studio", "capability": CAPABILITY_VERSION,
                     "engine": "server-builtin", "catalog": PLUGIN_CATALOG, "solver": PLUGIN_SOLVER},
        "parameters": parameters,
        "units": intake["units"],
        "frame": {"coordinate_system": "world",
                  "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
                  "elevation_datum": "unrecorded", "crs": "none"},
        "entity_mapping": mapping,
        "before": {"recorded": False},
        "after": after,
        "changes": {"created": [], "modified": [], "deleted": []},
        "warnings": [],
        "rejected_inputs": [],
        "provenance": {
            "side": "studio", "capability": CAPABILITY, "command": "ImportSolarEdgePDF",
            "revision": revision, "source": "studio-solaredge-import",
            "intake_sha256": intake_sha256,
            "row_angle": angle_record,
            "pdf_grids": pdf_grid_count,
            "matchable_grids": result["matchable_count"],
            "groups": len(intake["groups"]),
            "matched_groups": len(result["matches"]),
            "group_strings": len(result["strings"]),
            "bridge_strings": len(result["bridge_strings"]),
            "matches": [{"block": m["block"], "grid": m["grid_index"],
                         "pdf_grid": m["original_index"], "sub_grid": m["sub_grid_id"]}
                        for m in result["matches"]],
            "strings": sources,
        },
        "elapsed_ms": 0,
        "execution_mode": "live",
        "state": "committed",
        "survived_reopen": True,
        "synthetic_fields": ["before", "changes", "versions.catalog", "versions.solver",
                             "rejected_inputs"],
        "fallback_fields": fallback,
        "synthetic_flagged": True,
    }
    try:
        compare.validate_evidence(doc, "strings")
        compare._normalize(doc["after"], doc["entity_mapping"])
    except compare.InputError as exc:
        raise EvidenceError(f"evidence refused by the comparator: {exc}") from None
    if len(_serialize(doc).encode("utf-8")) > compare.MAX_BYTES:
        raise EvidenceError("evidence exceeds the comparator's byte bound")
    return doc


def write(doc, target):
    """Write, then reopen through the comparator's own reader; a mismatch deletes the file."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _serialize(doc)
    target.write_text(payload, encoding="utf-8")
    try:
        reread = compare.load_evidence(target)
        compare.validate_evidence(reread, "strings")
    except compare.InputError as exc:
        target.unlink(missing_ok=True)
        raise EvidenceError(f"{target} did not reopen: {exc}") from None
    if reread != json.loads(payload):
        target.unlink(missing_ok=True)
        raise EvidenceError(f"{target} did not read back as written")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Studio strings evidence for ImportSolarEdgePDF.")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--intake", type=Path, default=DEFAULT_INTAKE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--revision", default=None,
                        help="override the fixture's git revision (default: the commit that last touched the PDF)")
    parser.add_argument("--max-string-length", type=int, default=MAX_STRING_LENGTH)
    args = parser.parse_args(argv)
    try:
        doc = run(args.pdf, args.intake, revision=args.revision, max_string_length=args.max_string_length)
        write(doc, args.out)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print(f"solar-solaredge-import-evidence: {exc}", file=sys.stderr)
        return 2
    prov = doc["provenance"]
    print(f"{len(doc['after']['strings'])} strings ({prov['group_strings']} group, "
          f"{prov['bridge_strings']} bridge), {prov['matched_groups']} of {prov['groups']} groups "
          f"matched, {len(doc['after']['unassigned_panels'])} unassigned -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
