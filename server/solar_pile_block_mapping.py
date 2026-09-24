"""Studio's pile block mapping: a literal port of the plugin's LEAFPILEBLOCKMAP save path (contract G36).

Sources (Branch2025, cited file:line): the mapper selects a source and loads its template
(PileBlockMapperForm.SelectCurrentSource, :308-324: the saved mapping's template, else the PVcase template, else
DefaultTemplateForSource, :326-337), shows it in the station grid (LoadTemplate, :339-366, offsets printed "0.###"),
reads it back on Save mapping (ReadTemplateFromUi, :368-392; ReadStations, :394-411), upserts the drawing's mapping
by signature (PileBlockMappingDocument.Upsert, PileBlockMappingStore.cs:31-39) and saves the template to the host
store (PileTemplateStore.Save, PileTemplateStore.cs:63-74, with Normalize, :240-270). Even stations come from
PileBlockMappingStore.BuildEvenStationTemplate (:380-417) and template defaults from PileTemplate.cs:62-93.

One declared divergence: the plugin's store read deserialises every template into a PileTemplate whose
RevealBucketBoundariesM is already initialised to the five defaults, and Newtonsoft's default list handling appends
the saved values to them, so every save grows every loaded template's boundary list by one copy (Branch2025 #281).
Studio's store keeps each template's boundaries as saved.

Everything works on the neutral intake (sources and templates named by ordinal; the template the mapper saves for
source N is named `source-N`, as the adapter's neutraliser names SafeTemplateName of that source's block). Pure and
bounded; a malformed intake or a path this intake does not carry raises PileMappingError.
"""
from __future__ import annotations

import copy
from decimal import ROUND_HALF_UP, Decimal
import json
import math

FT_TO_M = 0.3048
DEFAULT_BOUNDARIES_M = [3.0 * FT_TO_M, 4.0 * FT_TO_M, 5.0 * FT_TO_M, 6.0 * FT_TO_M, 7.0 * FT_TO_M]  # PileTemplate.cs:107-111
DEFAULT_PILE_COUNT = 4                                       # DefaultTemplateForSource, :329-336
MAX_SOURCES = 10_000
MAX_TEMPLATES = 1_000
FORM_KEYS = {"source_row", "save_mapping", "confirm_ok", "close"}


class PileMappingError(ValueError):
    """A malformed pile mapping intake or form; the port refuses rather than guessing."""


def _require(condition, message):
    if not condition:
        raise PileMappingError(message)


def _finite(value, label):
    _require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
             f"{label} is not a finite number")
    return float(value)


def format_3(value):
    """double.ToString("0.###", InvariantCulture) read back by ReadDouble (:658-676): 15 significant digits, then
    three decimals rounded half away from zero."""
    text = Decimal(format(value, ".15g")).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    return float(text)


def piles_per_frame(template):
    """PileTemplate.PilesPerFrame (PileTemplate.cs:95-105), serialised with the template."""
    if template["PlacementMode"] == "AxisStations" and template["Stations"]:
        return len(template["Stations"])
    return template["HorizontalPoleCount"] * template["VerticalPoleCount"]


def new_template(name):
    """A PileTemplate with its declared defaults (PileTemplate.cs:62-93)."""
    return {"Name": name, "AreEqualMargins": False, "ShouldPlacePilesAtJoints": False, "IsMirrorFromMiddle": False,
            "DistributionType": 2, "HorizontalDistancesM": [], "VerticalDistancesM": [], "MiddleDistribution": 0.0,
            "SelectedMiddlePole": False, "HorizontalPoleCount": 2, "VerticalPoleCount": 1, "PlacementMode": "Grid",
            "StationAxis": "LocalX", "ReverseStationStart": False, "Stations": [], "PileDiameterM": 0.0,
            "PileRevealM": 0.0, "PileEmbedmentM": 0.0, "MinPileLengthM": 0.0, "MaxPileLengthM": 0.0,
            "RevealBucketBoundariesM": list(DEFAULT_BOUNDARIES_M)}


def even_station_template(name, length_m, count, start_m, end_m, axis, reverse):
    """PileBlockMappingStore.BuildEvenStationTemplate (:380-417)."""
    count = max(1, count)
    length = max(0.0, length_m)
    first = max(0.0, start_m)
    last = max(first, length - max(0.0, end_m))
    stations = []
    for i in range(count):
        t = 0.5 if count == 1 else i / (count - 1)
        offset = (first + last) * 0.5 if count == 1 else first + (last - first) * t
        stations.append({"OffsetM": offset, "Kind": "End" if i in (0, count - 1) else "Bearing",
                         "Label": str(i + 1), "CrossAxisOffsetM": 0.0})
    template = new_template(name if name and name.strip() else "Mapped")
    template.update(PlacementMode="AxisStations", StationAxis=axis, ReverseStationStart=reverse,
                    HorizontalPoleCount=max(1, count), VerticalPoleCount=1, Stations=stations)
    return template


def source_length(source, axis="LocalX"):
    """PileBlockMapperForm.SourceLength (:638-649) with one metre per drawing unit (the intake is in metres)."""
    width, height = source["local_width_m"], source["local_height_m"]
    dimension = height if axis == "LocalY" else width
    if dimension <= 1e-9:
        dimension = max(width, height)
    return max(0.001, dimension)


def read_back(template, source):
    """LoadTemplate then ReadTemplateFromUi (:339-392): what the station grid and fields hand back on Save mapping."""
    if template["PlacementMode"] != "AxisStations":
        raise PileMappingError("a grid template's station conversion is not carried by this intake version")
    shown = sorted(template["Stations"], key=lambda s: s["OffsetM"])
    stations = []
    for station in shown:                                   # ReadStations (:394-411): the grid's printed values
        cross = station.get("CrossAxisOffsetM", 0.0)
        stations.append({"OffsetM": max(0.0, format_3(station["OffsetM"])), "Kind": station["Kind"],
                         "Label": station.get("Label") or "",
                         "CrossAxisOffsetM": format_3(cross) if cross > 0.0 else 0.0})
    stations.sort(key=lambda s: s["OffsetM"])

    def maybe(value):                                       # FormatMaybe: a non-positive value shows empty, reads 0
        return format_3(value) if value > 0.0 else 0.0

    result = new_template(source["template_name"])
    result.update(PlacementMode="AxisStations", StationAxis=template["StationAxis"],
                  ReverseStationStart=template["ReverseStationStart"],
                  PileDiameterM=maybe(template["PileDiameterM"]), PileRevealM=maybe(template["PileRevealM"]),
                  PileEmbedmentM=maybe(template["PileEmbedmentM"]), MinPileLengthM=maybe(template["MinPileLengthM"]),
                  MaxPileLengthM=maybe(template["MaxPileLengthM"]), VerticalPoleCount=1, Stations=stations)
    if not stations:
        result = default_template(source)
    result["HorizontalPoleCount"] = max(1, len(result["Stations"]))
    return result


def default_template(source):
    """DefaultTemplateForSource (:326-337)."""
    return even_station_template(source["template_name"], source_length(source, "LocalX"), DEFAULT_PILE_COUNT,
                                 0.0, 0.0, "LocalX", False)


def normalise(template):
    """PileTemplateStore.Normalize (PileTemplateStore.cs:240-270)."""
    template["Name"] = template["Name"].strip()
    template["HorizontalPoleCount"] = max(1, template["HorizontalPoleCount"])
    template["VerticalPoleCount"] = max(1, template["VerticalPoleCount"])
    template["Stations"] = sorted((s for s in template["Stations"] if math.isfinite(s["OffsetM"])),
                                  key=lambda s: s["OffsetM"])
    if template["PlacementMode"] == "AxisStations" and not template["Stations"]:
        template["PlacementMode"] = "Grid"
    for key in ("PileDiameterM", "PileRevealM", "PileEmbedmentM", "MinPileLengthM", "MaxPileLengthM"):
        if not math.isfinite(template[key]) or template[key] < 0.0:
            template[key] = 0.0
    if not template["RevealBucketBoundariesM"]:
        template["RevealBucketBoundariesM"] = list(DEFAULT_BOUNDARIES_M)
    return template


def serialised(template):
    """The template as the plugin serialises it: every property, PilesPerFrame included."""
    value = copy.deepcopy(template)
    value["PilesPerFrame"] = piles_per_frame(template)
    return value


def canonical_text(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ---------------------------------------------------------------------------------------------
# The intake.

def _sources(intake):
    raw = intake.get("sources")
    _require(isinstance(raw, list) and 0 < len(raw) <= MAX_SOURCES, "the intake sources are invalid")
    sources = []
    for number, item in enumerate(raw, 1):
        _require(isinstance(item, dict) and item.get("source") == f"source-{number}",
                 f"intake source {number} is not named source-{number}")
        _require(item.get("source_kind") in ("Block", "PVcase", "Native"), f"source {number} has an unknown kind")
        _require(item.get("has_pvcase_piling") is False, "a source with PVcase piling is not carried")
        _require(item.get("status") in ("mapped", "unmapped"), f"source {number} has an unknown status")
        _require(isinstance(item.get("count"), int) and item["count"] >= 0, f"source {number} count is invalid")
        sizes = []
        for key in ("local_width", "local_height"):
            size = item.get(key)
            if size is None:
                sizes.append(None)
                continue
            _require(isinstance(size, dict) and size.get("kind") == "length" and size.get("unit") == "m",
                     f"source {number} {key} is not a length in metres")
            sizes.append(_finite(size.get("value"), f"source {number} {key}"))
        sources.append({"source": item["source"], "source_kind": item["source_kind"], "count": item["count"],
                        "status": item["status"], "local_width_m": sizes[0], "local_height_m": sizes[1],
                        "template_name": item["source"]})
    return sources


def _template_fields(fields, label):
    _require(isinstance(fields, dict), f"{label} is not an object")
    value = {key: copy.deepcopy(item) for key, item in fields.items() if key != "PilesPerFrame"}
    for key in new_template("x"):
        if key != "Name":
            _require(key in value, f"{label} lacks {key}")
    stations = value["Stations"]
    _require(isinstance(stations, list), f"{label} stations are not a list")
    for station in stations:
        _require(isinstance(station, dict) and {"OffsetM", "Kind", "Label", "CrossAxisOffsetM"} <= set(station),
                 f"{label} holds a malformed station")
        _finite(station["OffsetM"], f"{label} station offset")
    return value


def _store(intake):
    store = intake.get("pile_store")
    _require(isinstance(store, dict) and isinstance(store.get("templates"), list), "the intake pile store is invalid")
    _require(len(store["templates"]) <= MAX_TEMPLATES, "the intake pile store is too large")
    templates = {}
    for entry in store["templates"]:
        _require(isinstance(entry, dict) and isinstance(entry.get("template"), str), "a store template is invalid")
        name = entry["template"]
        _require(name.upper() not in {n.upper() for n in templates}, f"the store repeats template {name}")
        fields = _template_fields(entry.get("fields"), f"store template {name}")
        fields["Name"] = name
        templates[name] = fields
    active = store.get("active_template")
    _require(active in templates, "the store's active template is not one of its templates")
    return templates, active


def _mappings(intake, sources):
    raw = intake.get("mapping_record")
    if raw is None:
        return None
    _require(isinstance(raw, list), "the intake mapping record is invalid")
    by_source = {s["source"]: s for s in sources}
    mappings = []
    for item in raw:
        _require(isinstance(item, dict) and item.get("source") in by_source, "a recorded mapping names no source")
        _require(isinstance(item.get("template"), str), "a recorded mapping has no template text")
        try:
            template = json.loads(item["template"])
        except ValueError:
            raise PileMappingError("a recorded mapping's template is not JSON") from None
        fields = _template_fields(template, "a recorded mapping template")
        fields["Name"] = template.get("Name", "")
        mappings.append({"source": item["source"], "source_kind": item.get("source_kind"),
                         "user_override": item.get("user_override"), "template": fields})
    return mappings


# ---------------------------------------------------------------------------------------------
# The command.

def map_pile_block(intake, form_values):
    """The mapper run the form values describe: select `source_row`, Save mapping `save_mapping` times, confirm, close.
    Returns {sources, mappings (or None), store (templates, active), store_before}."""
    _require(isinstance(intake, dict) and intake.get("format") == "pile-block-map-intake-v1",
             "the intake is not a pile block mapping intake")
    _require(intake.get("units") == "m", "the intake is not in metres")
    _require(isinstance(form_values, dict) and set(form_values) == FORM_KEYS, "the form values are invalid")
    for key in FORM_KEYS:
        _require(isinstance(form_values[key], int) and not isinstance(form_values[key], bool) and form_values[key] >= 0,
                 f"form value {key} is not a count")
    sources = _sources(intake)
    templates, active = _store(intake)
    mappings = _mappings(intake, sources)
    store_before = copy.deepcopy(templates)
    row = form_values["source_row"]
    _require(row < len(sources), "the selected source row does not exist")
    selected = sources[row]
    statuses = [s["status"] for s in sources]
    if form_values["save_mapping"] > 0:
        _require(selected["local_width_m"] is not None and selected["local_height_m"] is not None,
                 "the selected source's local size is not carried by this intake")
        recorded = next((m for m in mappings or [] if m["source"] == selected["source"]), None)
        loaded = copy.deepcopy(recorded["template"]) if recorded else default_template(selected)
        for _ in range(form_values["save_mapping"]):
            template = read_back(loaded, selected)
            mapping = {"source": selected["source"], "source_kind": selected["source_kind"], "user_override": True,
                       "template": template}
            mappings = [m for m in (mappings or []) if m["source"] != selected["source"]] + [mapping]
            saved = normalise(copy.deepcopy(template))
            existing = next((name for name in templates if name.upper() == saved["Name"].upper()), None)
            if existing is None:
                _require(not templates, "a new template's place in the store's name order is not carried by the "
                                        "neutral intake")
                existing = saved["Name"]
            templates[existing] = saved
            loaded = template
        statuses[row] = "mapped"
    return {"sources": sources, "statuses": statuses, "mappings": mappings, "templates": templates,
            "active": active, "store_before": store_before}


def evidence_rows(intake, form_values):
    """{kind: [(id, fields)]} in the plugin adapter's s6 shape."""
    run = map_pile_block(intake, form_values)
    rows = {"pile-source": [(f"pile-source-{n}", {"source": s["source"], "source_kind": s["source_kind"],
                                                  "count": s["count"], "status_before": s["status"],
                                                  "status_after": after})
                            for n, (s, after) in enumerate(zip(run["sources"], run["statuses"]), 1)]}
    before_mappings = _mappings(intake, run["sources"])
    if run["mappings"] is not None and run["mappings"] != before_mappings:
        by_source = {s["source"]: s for s in run["sources"]}
        records = []
        for number, mapping in enumerate(run["mappings"], 1):
            source = by_source[mapping["source"]]
            width, height = source["local_width_m"], source["local_height_m"]
            records.append((f"pile-block-mapping-{number}", {
                "record": "pile-block-mapping", "source": mapping["source"], "source_kind": mapping["source_kind"],
                "user_override": mapping["user_override"],
                "local_width": None if width is None else {"kind": "length", "value": width, "unit": "m"},
                "local_height": None if height is None else {"kind": "length", "value": height, "unit": "m"},
                "pvcase_template_digest": None,
                "template": canonical_text(serialised(mapping["template"]))}))
        rows["pile-block-mapping"] = records
    template_records, number = [], 0
    for name, template in run["templates"].items():
        was = run["store_before"].get(name)
        after = serialised(template)
        before = serialised(was) if was is not None else None
        for field in sorted(after):
            if field == "Name":
                continue
            if before is not None and field in before and canonical_text(before[field]) == canonical_text(after[field]):
                continue
            number += 1
            template_records.append((f"pile-template-{number}", {
                "template": name, "field": field,
                "change": "added" if before is None or field not in before else "changed",
                "value": canonical_text(after[field])}))
    if template_records:
        rows["pile-template"] = template_records
    reports = {"templates": len(run["templates"]), "active-template": run["active"]}
    rows["report"] = [(f"report-{name}", {"name": name, "value": reports[name]}) for name in sorted(reports)]
    return rows
