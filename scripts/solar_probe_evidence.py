#!/usr/bin/env python3
"""The shared probe-file normalizer: a DEMO probe file becomes `exports` evidence.

Joint identity contract v5 (C:/tmp/solar-parity/W1-IDENTITY-CONTRACT.md, rules
E1 to E5) makes a probe capability's committed state the FILE the LEAF*DEMO
command writes. This module turns one such file, from EITHER side, into an
evidence document the frozen comparator (scripts/solar_w1_compare.py) accepts
for family `exports`. Both sides run the same code over their own file, so a
difference in the verdict is a difference in the plugin's and Studio's outputs,
never in how the two were described.

What the rules mean here:

  E1  The fixture is the SCENARIO INPUT LIST, not a drawing. `fixture_sha256`
      is the semantic hash of the inputs-only projection: every probe's id plus
      its declared input fields, sorted by id. Equal fixture hashes prove both
      sides ran the same cases without either side reading the other's inputs,
      and they stay equal when the OUTPUTS differ, which is what makes a
      genuine parity failure visible instead of masked.
  E2  One probe is one `after.rows` record. `id` is the probe's name column, or
      its 1-based ordinal when the file has none. For a file with several probe
      SECTIONS the id is "<section>:<name>", because record ids must be unique
      across the whole document and the same label can repeat across sections.
      `quantity` is the primary numeric result, named per probe type in
      PROBE_SPECS below, never guessed. `unit` is the file's units string or
      "none".
  E3  `rows` keeps the file's own order (several DEMOs assert probe order), and
      for a sectioned file the sections concatenate in the file's section order.
      `format` names the file kind, `source_revision` is "scenario-list".
  E4  `entity_mapping` maps each row id to itself and to nothing else.
  E5  XLSX is read cell by cell per sheet. Not implemented here: this slice
      carries JSON only, and READERS below is the single extension point where
      the CSV and XLSX readers land.

Two contract details that the frozen comparator, not this module, decides:

  * Evidence-level `units` MUST be one of the comparator's LENGTH_UNITS
    (solar_w1_compare.py:151), so it cannot be "none". It is "in" here, the
    joint contract's rule 5 value, and it is identical on both sides. The
    "none" units of a unitless probe live on the ROW's `unit`, which is where
    E2 puts them.
  * Every probe field other than the ones E2 names rides on the record verbatim
    under `fields`, rather than at the record's top level, because a probe
    field can legitimately be called "unit" (the conductor-area battery has
    one) and would otherwise collide with the record key of the same name.
    Nothing is dropped and nothing is rounded, so formula and citation strings
    still compare byte-exact.

No network, no dependencies outside the standard library.

Usage:
    python scripts/solar_probe_evidence.py --probe-file <path> \\
        --capability nec-conduit-fill --probe-type nec-max-fill-fraction \\
        --side studio --revision <40-hex> --output <path>
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys

SCHEMA = "leaf.solar-w1-comparison.v1"
FAMILY = "exports"
CAPABILITY_VERSION = "0"
SOURCE_REVISION = "scenario-list"
# Rule 5 of the joint contract. The comparator refuses anything outside its
# LENGTH_UNITS here, so a unitless probe carries "none" on the ROW instead.
EVIDENCE_UNITS = "in"
# Rule 6 of the joint contract, byte for byte on both sides.
FRAME = {
    "coordinate_system": "world",
    "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
    "elevation_datum": "unrecorded",
    "crs": "none",
}
# Rule 12: neither side's capture can record them, so both are declared synthetic.
SYNTHETIC_FIELDS = ["before/recorded", "changes/unrecorded"]


def _sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare = _sibling("solar_w1_compare")


class Section:
    """One probe battery inside a file: where it lives and how it projects.

    `key` is the document key holding the list, or None when the document IS
    the list. `quantity_field` is the E2 choice, stated once here so no caller
    has to guess which of a multi-output probe's numbers is the result.
    """

    __slots__ = ("key", "row_type", "id_field", "inputs", "quantity_field",
                 "unit_field", "unit_literal")

    def __init__(self, key, row_type, id_field, inputs, quantity_field,
                 unit_field=None, unit_literal="none"):
        self.key = key
        self.row_type = row_type
        self.id_field = id_field
        self.inputs = tuple(inputs)
        self.quantity_field = quantity_field
        self.unit_field = unit_field
        self.unit_literal = unit_literal


class ProbeSpec:
    __slots__ = ("file_format", "sections")

    def __init__(self, file_format, sections):
        self.file_format = file_format
        self.sections = tuple(sections)


# The six licensed DEMO probe files. Each row's `type` is the CALCULATION, which
# is why the conduit-fill file contributes four different types.
PROBE_SPECS = {
    "nec-ac-voltage-drop": ProbeSpec("json-probes", [
        Section(None, "nec-ac-voltage-drop", "Name",
                ("I_in", "L_in", "R_in", "X_in", "PF_in", "Phase_in", "V_in"),
                "Result", unit_field="Units"),
    ]),
    "nec-ampacity-correction": ProbeSpec("json-probes", [
        Section(None, "nec-ampacity-correction", "Name",
                ("BaseA", "TempFactor", "ConduitFactor"),
                "Result", unit_field="Units"),
    ]),
    "nec-max-fill-fraction": ProbeSpec("json-probes", [
        Section(None, "nec-max-fill-fraction", "Name", ("N",), "Fill"),
    ]),
    "nec-feeder-ocpd-sizing": ProbeSpec("json-probes", [
        # SizeFeederOcpd returns four outputs; the OCPD RATING is the decision
        # the capability exists to make, so it is the quantity. MinOcpdA,
        # EgcGauge and Note ride verbatim under fields and compare exactly.
        Section(None, "nec-feeder-ocpd-sizing", "Name",
                ("ContinuousCurrentA", "IsContinuous", "EgcMaterial"),
                "OcpdRatingA", unit_literal="A"),
    ]),
    "nec-citation-one-line-render": ProbeSpec("json-probes", [
        # The rendered OneLiner is a string, so the numeric result the citation
        # carries is the quantity; the render itself rides verbatim.
        Section(None, "nec-citation-one-line-render", "Name",
                ("Article", "ShortDescription", "Formula", "Result", "Units",
                 "HasSyntheticInputs", "SyntheticInputNames"),
                "Result", unit_field="Units"),
    ]),
    "nec-conduit-fill-tables": ProbeSpec("json-probes", [
        Section("maxFill", "nec-max-fill-fraction", "label",
                ("conductorCount",), "csharpFraction"),
        Section("conductorArea", "nec-conductor-area", "label",
                ("gauge", "unit", "insulation"), "csharpArea", unit_literal="sq in"),
        Section("conduitArea", "nec-conduit-area", "label",
                ("tradeSize", "conduitType"), "csharpArea", unit_literal="sq in"),
        # SizeConduit returns a trade size plus five numbers; the FILL PERCENT
        # is the quantity because it is what Table 1 bounds and what the picked
        # trade size follows from. tradeSize, areas, counts and the failure
        # reason ride verbatim under fields.
        Section("sizeConduit", "nec-conduit-sizing", "label",
                ("conduitType", "conductors"), "fillPct", unit_literal="%"),
    ]),
}


def _read_json_probes(text):
    """Parse a Newtonsoft-written probe file, refusing duplicate keys."""
    return json.loads(text, object_pairs_hook=compare._unique_object)


# The single extension point for the rest of contract v5. A later slice adds
# "csv" and "xlsx:<sheet list>" here (rule E5 reads XLSX cell by cell per sheet)
# together with the ProbeSpec rows that name them; an unknown format fails
# closed rather than guessing a projection.
READERS = {"json-probes": _read_json_probes}


def _sections(document, spec):
    """Yield (section, probe list) in the file's own order (rule E3)."""
    for section in spec.sections:
        if section.key is None:
            if not isinstance(document, list):
                raise compare.InputError("probe file must be a JSON array")
            yield section, document
        else:
            if not isinstance(document, dict) or section.key not in document:
                raise compare.InputError("probe file is missing section " + section.key)
            probes = document[section.key]
            if not isinstance(probes, list):
                raise compare.InputError("probe section must be an array: " + section.key)
            yield section, probes


def _row_id(section, probe, ordinal):
    """E2: the probe's name column, or its 1-based ordinal when there is none.

    A sectioned file prefixes the section key so ids stay unique document-wide.
    """
    if section.id_field is not None and section.id_field in probe:
        name = probe[section.id_field]
        if not isinstance(name, str) or not name:
            raise compare.InputError("probe name column must be a nonempty string")
    else:
        name = str(ordinal)
    return name if section.key is None else section.key + ":" + name


def _unit(section, probe):
    if section.unit_field is not None:
        value = probe.get(section.unit_field)
        if isinstance(value, str) and value:
            return value
        return "none"
    return section.unit_literal


def _quantity_value(section, probe):
    """The primary numeric result; a COUNT when the result is list-valued (E2)."""
    if section.quantity_field not in probe:
        raise compare.InputError("probe is missing its quantity field " + section.quantity_field)
    value = probe[section.quantity_field]
    if isinstance(value, list):
        return len(value)
    if type(value) is bool or type(value) not in (int, float):
        raise compare.InputError("probe quantity must be numeric or list-valued")
    return value


def project(document, probe_type):
    """Split a probe document into its (row_id, row, inputs) triples.

    Pure, and the ONLY place the file shape is interpreted; both the fixture
    projection and the rows are derived from this single pass, so the two can
    never disagree about which probes a file holds.
    """
    if probe_type not in PROBE_SPECS:
        raise compare.InputError("unknown probe type: " + str(probe_type))
    spec = PROBE_SPECS[probe_type]
    rows = []
    fixture = []
    seen = set()
    for section, probes in _sections(document, spec):
        for ordinal, probe in enumerate(probes, start=1):
            if not isinstance(probe, dict):
                raise compare.InputError("probe must be an object")
            row_id = _row_id(section, probe, ordinal)
            if row_id in seen:
                raise compare.InputError("duplicate probe id: " + row_id)
            seen.add(row_id)
            missing = [name for name in section.inputs if name not in probe]
            if missing:
                raise compare.InputError("probe is missing input fields: " + ", ".join(missing))
            rows.append({
                "id": {"entity_id": row_id},
                "type": section.row_type,
                "quantity": {"kind": "float",
                             "value": _quantity_value(section, probe),
                             "unit": _unit(section, probe)},
                "unit": _unit(section, probe),
                "fields": deepcopy(probe),
            })
            fixture.append({"id": row_id,
                            "inputs": {name: deepcopy(probe[name]) for name in section.inputs}})
    if not rows:
        raise compare.InputError("probe file holds no probes")
    fixture.sort(key=lambda entry: entry["id"])
    return rows, fixture


def build_evidence(document, *, capability, probe_type, side, revision,
                   survived_reopen, file_name, file_sha256, elapsed_ms=0):
    """Build one 22-key `exports` evidence object from a parsed probe document.

    `survived_reopen` is the CALLER's observation: build_evidence_from_file sets
    it only after reading the file back off disk and finding it unchanged. This
    function never claims a reopen it did not see.
    """
    if not isinstance(capability, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", capability):
        raise compare.InputError("invalid capability")
    if not isinstance(side, str) or not side:
        raise compare.InputError("side label is required")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise compare.InputError("revision must be a 40-character lowercase git commit")
    if type(survived_reopen) is not bool:
        raise compare.InputError("survived_reopen must be observed, not assumed")
    rows, fixture = project(document, probe_type)
    spec = PROBE_SPECS[probe_type]
    if spec.file_format not in READERS:
        raise compare.InputError("unsupported probe file format: " + spec.file_format)
    parameters = {"family": FAMILY, "capability": capability}
    fixture_sha256 = compare.semantic_hash(fixture)
    after = {"rows": rows, "source_revision": SOURCE_REVISION, "format": spec.file_format}
    evidence = {
        "fixture_sha256": fixture_sha256,
        "input_sha256": compare.semantic_hash({"fixture_sha256": fixture_sha256,
                                               "parameters": parameters}),
        "output_sha256": compare.semantic_hash(after),
        "revision": revision,
        # versions.capability is the ledger's capability_version and versions.engine one of the
        # ledger's engine values: Studio computes probes in the server, the plugin side names none.
        "versions": {"schema": SCHEMA, "producer": side, "capability": CAPABILITY_VERSION,
                     "engine": "server-builtin" if side == "studio" else "none",
                     "catalog": "none", "solver": "none"},
        "parameters": parameters,
        "units": EVIDENCE_UNITS,
        "frame": deepcopy(FRAME),
        # Rule E4: exactly the entities `after` references, mapped to themselves.
        "entity_mapping": {row["id"]["entity_id"]: row["id"]["entity_id"] for row in rows},
        "before": {"recorded": False},
        "after": after,
        "changes": {"created": [], "modified": [], "deleted": []},
        "warnings": [],
        "rejected_inputs": [],
        "provenance": {"side": side, "probe_file": file_name,
                       "probe_file_sha256": file_sha256, "probe_type": probe_type},
        "elapsed_ms": elapsed_ms,
        "execution_mode": "recorded",
        "state": "committed",
        "survived_reopen": survived_reopen,
        "synthetic_fields": list(SYNTHETIC_FIELDS),
        "fallback_fields": [],
        "synthetic_flagged": True,
    }
    compare.validate_evidence(evidence, FAMILY)
    compare._normalize(evidence["before"], evidence["entity_mapping"])
    compare._normalize(evidence["after"], evidence["entity_mapping"])
    return evidence


def build_evidence_from_file(path, *, capability, probe_type, side, revision, elapsed_ms=0):
    """Read a probe file, build its evidence, and PROVE the reopen.

    The file is read twice: once to build, once again afterwards. Only a second
    read that returns the identical bytes sets survived_reopen, which is what
    rule E4 means by "true only when the adapter parsed the file back from disk
    after it was written".
    """
    path = Path(path)
    spec = PROBE_SPECS.get(probe_type)
    if spec is None:
        raise compare.InputError("unknown probe type: " + str(probe_type))
    reader = READERS.get(spec.file_format)
    if reader is None:
        raise compare.InputError("unsupported probe file format: " + spec.file_format)
    raw = path.read_bytes()
    if len(raw) > compare.MAX_INPUT_BYTES:
        raise compare.InputError("probe file exceeds byte limit")
    document = reader(raw.decode("utf-8"))
    compare.scan_input(document)
    reopened = path.read_bytes()
    return build_evidence(document, capability=capability, probe_type=probe_type, side=side,
                          revision=revision, survived_reopen=reopened == raw,
                          file_name=path.name,
                          file_sha256=hashlib.sha256(raw).hexdigest(),
                          elapsed_ms=elapsed_ms)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-file", type=Path, required=True)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--probe-type", choices=sorted(PROBE_SPECS), required=True)
    parser.add_argument("--side", required=True, help="side label, e.g. plugin or studio")
    parser.add_argument("--revision", required=True, help="40-hex commit of the scenario list")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        evidence = build_evidence_from_file(
            args.probe_file, capability=args.capability, probe_type=args.probe_type,
            side=args.side, revision=args.revision)
        payload = json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if len(payload.encode("utf-8")) > compare.MAX_BYTES:
            raise compare.InputError("evidence exceeds byte limit")
        args.output.write_text(payload, encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print("solar-probe-evidence: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
