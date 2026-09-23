"""Studio's PV arrays and the PVsyst scene export, ported from the plugin (contract G20: a5, a6, a7, a13).

Literal ports, each function citing the plugin source it reproduces:
  LEAFDEFINEARRAY   define_array          Pvcase/LeafArrayCommand.cs:226-319
  LEAFLISTARRAYS    list_arrays           Pvcase/LeafArrayCommand.cs:324-363
  LEAFDELETEARRAY   delete_array          Pvcase/LeafArrayCommand.cs:368-425, :502-523
  outline           array_outline         Pvcase/LeafArrayCommand.cs:457-500
  LEAFEXPORTSCENE   export_scene          Pvcase/LeafExportSceneCommand.cs:48-273
    DAE writer      write_collada         LeafSolarDesign.Core/SiteFromMap/ColladaExporter.cs:68-492
    PVC writer      write_pvc             LeafSolarDesign.Core/SiteFromMap/PvcExporter.cs:62-668
    array spec      to_spec, contains_xy, world_aabb
                                          LeafExportSceneCommand.cs:219-251, SiteFromMap/ArraySpec.cs:14-88
  project defaults  export_settings       Pvcase/LeafExportSupportCommands.cs:31-43, :106-124

The array store is neutral data: a list of records, one per array, each a dict of the fields
the plugin persists per array (RECORD_FIELDS), plus the outline polylines on layer LEAF-ARRAY as
{"key", "vertices"}. Nothing here reads or writes drawing storage.

Output text is the plugin's byte for byte (XmlWriter with Indent and a BOM-less UTF-8
encoding: two-space indent, CRLF line ends, `<x />` for an empty element, no newline after the
root's end tag); only the DAE `<created>` and `<modified>` values are the caller's. XML is
written as text; nothing is parsed. Reals print as C# ToString("F4") does on the .NET 8 build:
the exact binary value rounded half to even, a negative that rounds to zero keeping its sign.

Every function is pure, bounded (MAX_* below) and fails closed: a malformed record, grid or
answer raises SceneInputError, never a partial file.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import math
import re

ARRAY_LAYER = "LEAF-ARRAY"                     # LeafArrayCommand.cs:82
KEY_PREFIX = "array_"                          # NextKey, LeafArrayCommand.cs:177-183

# LeafExportSettings field defaults, LeafExportSupportCommands.cs:33-42.
DEFAULT_SETTINGS = {
    "module_width_m": 0.992, "module_height_m": 1.640,
    "module_x_spacing_m": 0.02, "module_y_spacing_m": 0.02,
    "orientation": 0, "default_tilt_deg": 15.0, "default_azimuth_deg": 0.0,
    "maintenance_margin_m": 2.0,
    "module_manufacturer": "generic", "module_name": "generic",
}
SETTING_REALS = ("module_width_m", "module_height_m", "module_x_spacing_m", "module_y_spacing_m",
                 "default_tilt_deg", "default_azimuth_deg", "maintenance_margin_m")

# LEAFDEFINEARRAY prompt defaults that do not come from the settings (LeafArrayCommand.cs:244-251).
DEFAULT_CENTRE = 0.0
DEFAULT_MODULES_X = 100
DEFAULT_MODULES_Y = 85
# The prompts in the order the command asks them (:244-269): (answer key, negatives allowed).
DEFINE_PROMPTS = (
    ("centre_x", True), ("centre_y", True),
    ("modules_x", False), ("modules_y", False),
    ("orientation", False),
    ("module_width_m", False), ("module_height_m", False),
    ("module_x_spacing_m", False), ("module_y_spacing_m", False),
    ("tilt_deg", True), ("azimuth_deg", True),
)

# The fields persisted per array (LeafArrayRecord, LeafArrayCommand.cs:58-77), neutral names.
RECORD_REALS = ("centre_x", "centre_y", "half_x", "half_y", "tilt_deg", "azimuth_deg",
                "module_width_m", "module_height_m", "module_x_spacing_m", "module_y_spacing_m",
                "row_pitch_m")
RECORD_INTS = ("orientation", "modules_x", "modules_y")
RECORD_TEXTS = ("module_manufacturer", "module_name")
RECORD_FIELDS = ("key",) + RECORD_REALS + RECORD_INTS + RECORD_TEXTS

# LEAFEXPORTSCENE's writer arguments, LeafExportSceneCommand.cs:130-146, and ToSpec's margin (:247).
MIN_OBSTACLE_HEIGHT_M = 3.0
GROUND_STRIDE = 12
DSM_STRIDE = 8
PV_BASE_Z = 0.0
MAINTENANCE_MARGIN_M = 2.0

COLLADA_NS = "http://www.collada.org/2005/11/COLLADASchema"
XML_DECLARATION = '<?xml version="1.0" encoding="utf-8"?>'
NEWLINE = "\r\n"                               # XmlWriterSettings.NewLineChars on Windows

# PVC material slots PVsyst keys off by name, PvcExporter.cs:44-51, and their colours, :460-463.
MATERIALS = (("Material0", "Frames", "0.10 0.20 0.45 1.0"),
             ("Material1", "Tree_trunk", "0.30 0.20 0.10 1.0"),
             ("Material2", "Tree_crown", "0.20 0.45 0.20 1.0"),
             ("Material3", "Topography_mesh", "0.55 0.45 0.30 1.0"))
MAT_FRAMES, MAT_TREE_CROWN, MAT_TOPOGRAPHY = "Material0", "Material2", "Material3"

# Bounds: every loop below is linear in one of these.
MAX_ARRAYS = 1_000
MAX_FRAMES = 100_000
MAX_GRID_NODES = 4_000_000
MAX_ABS_VALUE = 1e15                           # keeps F4 text exact inside Decimal's precision
MAX_TEXT_CHARS = 1_000
INT32_MAX = 2**31 - 1

_O_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z")
_XML_CHAR = re.compile("[^\u0009\u000a\u000d -퟿-�\U00010000-\U0010ffff]")


class SceneInputError(ValueError):
    """A named refusal: no record, no outline, no file."""


# --------------------------------------------------------------- validation --

def _finite(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SceneInputError(f"{what} must be a number")
    value = float(value)
    if not math.isfinite(value) or abs(value) > MAX_ABS_VALUE:
        raise SceneInputError(f"{what} must be finite and within {MAX_ABS_VALUE:g}")
    return value


def _int32(value, what):
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > INT32_MAX:
        raise SceneInputError(f"{what} must be a 32-bit integer")
    return value


def _text(value, what):
    if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS:
        raise SceneInputError(f"{what} must be text of at most {MAX_TEXT_CHARS} characters")
    if _XML_CHAR.search(value):
        # XmlWriter's CheckCharacters throws here; the command reports the error and writes nothing.
        raise SceneInputError(f"{what} holds a character XML cannot carry")
    return value


def validate_record(record):
    """One array record, every persisted field present and well formed; returns a clean copy."""
    if not isinstance(record, dict) or set(record) != set(RECORD_FIELDS):
        raise SceneInputError(f"an array record carries exactly {list(RECORD_FIELDS)}")
    key = record["key"]
    if not isinstance(key, str) or not key or len(key) > 255 or key != key.strip():
        raise SceneInputError("an array key must be non-empty text without surrounding spaces")
    clean = {"key": key}
    for name in RECORD_REALS:
        clean[name] = _finite(record[name], name)
    for name in RECORD_INTS:
        clean[name] = _int32(record[name], name)
    for name in RECORD_TEXTS:
        clean[name] = _text(record[name], name)
    return clean


def validate_records(records):
    if not isinstance(records, (list, tuple)) or len(records) > MAX_ARRAYS:
        raise SceneInputError(f"the array store must be a list of at most {MAX_ARRAYS} records")
    clean = [validate_record(r) for r in records]
    folded = [r["key"].upper() for r in clean]
    if len(set(folded)) != len(folded):
        raise SceneInputError("array keys must be unique (drawing dictionary keys ignore case)")
    return clean


def validate_outlines(outlines):
    if not isinstance(outlines, (list, tuple)) or len(outlines) > MAX_ARRAYS:
        raise SceneInputError(f"the outlines must be a list of at most {MAX_ARRAYS} polylines")
    clean = []
    for o in outlines:
        if not isinstance(o, dict) or set(o) != {"key", "vertices"} or not isinstance(o["key"], str):
            raise SceneInputError("an outline is {key, vertices}")
        verts = o["vertices"]
        if not isinstance(verts, (list, tuple)) or len(verts) != 4:
            raise SceneInputError("an outline has four vertices")
        clean.append({"key": o["key"], "vertices": [
            (_finite(v[0], "outline x"), _finite(v[1], "outline y"))
            if isinstance(v, (list, tuple)) and len(v) == 2 else _bad_vertex() for v in verts]})
    return clean


def _bad_vertex():
    raise SceneInputError("an outline vertex is [x, y]")


# -------------------------------------------------------------- formatting --

def net_fixed(value, digits):
    """C# double.ToString("F<digits>", InvariantCulture) on .NET 8: the exact binary value
    rounded half to even at `digits` decimals (.NET Core 2.1+), and a negative value that
    rounds to zero keeps its sign (.NET Core 3.0+). Fails closed on non-finite input."""
    value = _finite(value, "formatted value")
    with localcontext() as ctx:
        ctx.prec = 64
        return format(Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN), "f")


def _f4(value):
    return net_fixed(value, 4)


def _mm(metres):
    """PvcExporter.Mm, :545-546: (int)Math.Round(m * 1000.0), banker's rounding."""
    v = round(_finite(metres, "module dimension") * 1000.0)
    if abs(v) > INT32_MAX:
        raise SceneInputError("module dimension overflows a 32-bit millimetre count")
    return str(int(v))


def _xml_text(value):
    """XmlWriter.WriteString in element content: &, <, > escaped; every line break written as
    NewLineChars (NewLineHandling.Replace)."""
    value = _text(value, "element text")
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", NEWLINE)


def _element(indent, name, text):
    """XmlWriter.WriteElementString: an empty value writes `<name />` (the PVC's `<author />`)."""
    return f"{indent}<{name} />" if text == "" else f"{indent}<{name}>{_xml_text(text)}</{name}>"


def utc_stamp(moment=None):
    """DateTime.UtcNow.ToString("o"): seven fractional digits and Z (ColladaExporter.cs:334-335)."""
    moment = datetime.now(timezone.utc) if moment is None else moment.astimezone(timezone.utc)
    return f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond:06d}0Z"


def scene_file_names(local_moment):
    """The two file names LEAFEXPORTSCENE writes (:126-139): DateTime.Now as yyyyMMdd-HHmmss."""
    stamp = f"{local_moment:%Y%m%d-%H%M%S}"
    return f"{stamp}-pvsyst-scene.dae", f"{stamp}-pvsyst-scene.pvc"


# ---------------------------------------------------------- the array store --

def export_settings(values=None):
    """LeafExportSettingsStore.Read, LeafExportSupportCommands.cs:50-63, :106-124: the defaults
    unless the project wrote its own. `values` holds neutral setting names only."""
    s = dict(DEFAULT_SETTINGS)
    if values is None:
        return s
    if not isinstance(values, dict) or not set(values) <= set(DEFAULT_SETTINGS):
        raise SceneInputError(f"export settings take only {sorted(DEFAULT_SETTINGS)}")
    for name, value in values.items():
        if name in SETTING_REALS:
            s[name] = _finite(value, name)
        elif name == "orientation":
            s[name] = _int32(value, name)
        else:
            # `?? "generic"` where the value is read back as a missing string, :120-121, :272-273.
            s[name] = "generic" if value is None else _text(value, name)
    return s


def list_arrays(records):
    """LeafArrayStore.ReadAll, LeafArrayCommand.cs:88-104: every record in dictionary order.
    A drawing dictionary iterates its entries sorted by key, ignoring case (inferred from the
    AutoCAD dictionary; the fixture holds one array)."""
    return sorted(validate_records(records), key=lambda r: (r["key"].upper(), r["key"]))


def next_key(records):
    """LeafArrayStore.NextKey, :177-183: the first unused array_N, N from 0 (exact match)."""
    keys = {r["key"] for r in records}
    n = 0
    while KEY_PREFIX + str(n) in keys:
        n += 1
    return KEY_PREFIX + str(n)


def _prompt(answers, name, default, allow_negative):
    """PromptDouble, LeafArrayCommand.cs:529-542: the answer, or the default on Enter. The
    prompt refuses a negative where AllowNegative is false; here that refusal is an error."""
    if name not in answers:
        return float(default)
    value = _finite(answers[name], name)
    if value < 0 and not allow_negative:
        raise SceneInputError(f"{name} does not accept a negative value")
    return value


def _to_int(value, name):
    """C# (int) on a double: truncation toward zero; out of the 32-bit range is refused."""
    if abs(value) >= INT32_MAX:
        raise SceneInputError(f"{name} overflows a 32-bit integer")
    return int(value)


def define_array(records, answers=None, settings=None):
    """LEAFDEFINEARRAY, LeafArrayCommand.cs:226-319.

    answers: {prompt key: value} for the prompts answered with something other than Enter
    (DEFINE_PROMPTS). Returns the new record, or None where the command returns without
    storing anything (a module count under 1, :250, :252). The caller adds the record to the
    store and array_outline(record) to layer LEAF-ARRAY (:294-295)."""
    records = validate_records(records)
    if len(records) >= MAX_ARRAYS:
        raise SceneInputError(f"the store already holds {MAX_ARRAYS} arrays")
    answers = {} if answers is None else answers
    known = {name for name, _ in DEFINE_PROMPTS}
    if not isinstance(answers, dict) or not set(answers) <= known:
        raise SceneInputError(f"define answers take only {[n for n, _ in DEFINE_PROMPTS]}")
    s = export_settings(settings)
    neg = dict(DEFINE_PROMPTS)
    a = {"key": next_key(records)}                                               # :239
    a["centre_x"] = _prompt(answers, "centre_x", DEFAULT_CENTRE, neg["centre_x"])  # :244
    a["centre_y"] = _prompt(answers, "centre_y", DEFAULT_CENTRE, neg["centre_y"])  # :246
    a["modules_x"] = _to_int(_prompt(answers, "modules_x", DEFAULT_MODULES_X, False), "modules_x")  # :249
    if a["modules_x"] < 1:
        return None
    a["modules_y"] = _to_int(_prompt(answers, "modules_y", DEFAULT_MODULES_Y, False), "modules_y")  # :251
    if a["modules_y"] < 1:
        return None
    orientation = _to_int(_prompt(answers, "orientation", s["orientation"], False), "orientation")  # :254
    a["orientation"] = 1 if orientation == 1 else 0                               # :255
    a["module_width_m"] = _prompt(answers, "module_width_m", s["module_width_m"], False)          # :257
    a["module_height_m"] = _prompt(answers, "module_height_m", s["module_height_m"], False)       # :259
    a["module_x_spacing_m"] = _prompt(answers, "module_x_spacing_m", s["module_x_spacing_m"], False)  # :261
    a["module_y_spacing_m"] = _prompt(answers, "module_y_spacing_m", s["module_y_spacing_m"], False)  # :263
    a["tilt_deg"] = _prompt(answers, "tilt_deg", s["default_tilt_deg"], True)     # :266
    a["azimuth_deg"] = _prompt(answers, "azimuth_deg", s["default_azimuth_deg"], True)  # :268
    a["row_pitch_m"] = 0.0                                                        # :271
    a["module_manufacturer"] = s["module_manufacturer"]                           # :272
    a["module_name"] = s["module_name"]                                           # :273
    # Half extents from module count, size and spacing, :278-292. Landscape puts the module's
    # long side (height) along X; Portrait its short side (width).
    if a["orientation"] == 0:
        module_x, module_y = a["module_height_m"], a["module_width_m"]
    else:
        module_x, module_y = a["module_width_m"], a["module_height_m"]
    field_w = a["modules_x"] * module_x + (a["modules_x"] - 1) * a["module_x_spacing_m"]
    field_d = a["modules_y"] * module_y + (a["modules_y"] - 1) * a["module_y_spacing_m"]
    a["half_x"] = field_w * 0.5
    a["half_y"] = field_d * 0.5
    return validate_record(a)


def array_outline(record):
    """DrawArrayOutline, LeafArrayCommand.cs:457-500: the closed footprint polyline, corners
    (-hx,-hy), (hx,-hy), (hx,hy), (-hx,hy) rotated by the azimuth about the centre."""
    a = validate_record(record)
    a_rad = a["azimuth_deg"] * math.pi / 180.0
    cos_a, sin_a = math.cos(a_rad), math.sin(a_rad)
    corners = ((-a["half_x"], -a["half_y"]), (a["half_x"], -a["half_y"]),
               (a["half_x"], a["half_y"]), (-a["half_x"], a["half_y"]))
    return [(x * cos_a - y * sin_a + a["centre_x"], x * sin_a + y * cos_a + a["centre_y"])
            for x, y in corners]


def list_report(records):
    """LEAFLISTARRAYS's command-line text, LeafArrayCommand.cs:334-356 (read only)."""
    arrays = list_arrays(records)
    if not arrays:
        return ["\nLEAFLISTARRAYS: no arrays defined. Use LEAFDEFINEARRAY.\n"]
    lines = [f"\nLEAFLISTARRAYS: {len(arrays)} array(s):\n"]
    for a in arrays:
        modules = a["modules_x"] * a["modules_y"]
        lines.append(
            f"  {a['key']}: {a['modules_x']}×{a['modules_y']}={modules} modules "
            f"({'Landscape' if a['orientation'] == 0 else 'Portrait'}), "
            f"{net_fixed(2 * a['half_x'], 1)}×{net_fixed(2 * a['half_y'], 1)} m, "
            f"tilt={net_fixed(a['tilt_deg'], 1)}°, azim={net_fixed(a['azimuth_deg'], 1)}°, "
            f"centre=({net_fixed(a['centre_x'], 1)}, {net_fixed(a['centre_y'], 1)})\n")
    return lines


def delete_array(records, outlines, answer):
    """LEAFDELETEARRAY, LeafArrayCommand.cs:368-425.

    answer: the text typed at "Array key to delete (or . for cancel)". Returns {"status":
    "no_arrays" | "cancelled" | "not_found" | "removed", "key", "records", "outlines",
    "erased_outlines"}. The record is matched as the drawing dictionary matches (ignoring case,
    :161-172); its outlines are erased by an ordinal key match (EraseArrayOutline, :502-523)."""
    records = validate_records(records)
    outlines = validate_outlines(outlines)
    result = {"status": None, "key": None, "records": records, "outlines": outlines, "erased_outlines": 0}
    if not records:
        return dict(result, status="no_arrays")                                   # :381-389
    if not isinstance(answer, str) or len(answer) > MAX_TEXT_CHARS:
        raise SceneInputError("the delete answer must be text")
    if answer == ".":
        return dict(result, status="cancelled")                                   # :399-406
    key = answer.strip()                                                          # :407
    folded = key.upper()
    kept = [r for r in records if r["key"].upper() != folded]
    if len(kept) == len(records):
        return dict(result, status="not_found", key=key)                          # :409-417
    kept_outlines = [o for o in outlines if o["key"] != key]
    return dict(result, status="removed", key=key, records=kept, outlines=kept_outlines,
                erased_outlines=len(outlines) - len(kept_outlines))


# ------------------------------------------------------------- array specs --

def to_spec(record):
    """LeafExportSceneCommand.ToSpec, :219-251: one frame per row when a row pitch is set
    (round(2 * halfY / pitch), banker's, at least 1), a 2 m maintenance margin."""
    r = validate_record(record)
    frames_in_y = 1
    if r["row_pitch_m"] > 0.0 and r["half_y"] > 0.0:
        est = round((2.0 * r["half_y"]) / r["row_pitch_m"])
        if est > MAX_FRAMES:
            raise SceneInputError(f"array {r['key']} would emit more than {MAX_FRAMES} frames")
        frames_in_y = max(1, int(est))
    return dict(r, frames_in_x=1, frames_in_y=frames_in_y, maintenance_margin_m=MAINTENANCE_MARGIN_M)


def contains_xy(spec, x, y):
    """ArraySpec.ContainsXY, ArraySpec.cs:66-76: inside the rotated footprint plus the margin."""
    dx = x - spec["centre_x"]
    dy = y - spec["centre_y"]
    a_rad = spec["azimuth_deg"] * math.pi / 180.0
    cos_a, sin_a = math.cos(a_rad), math.sin(a_rad)
    x_local = dx * cos_a + dy * sin_a
    y_local = -dx * sin_a + dy * cos_a
    m = spec["maintenance_margin_m"]
    return abs(x_local) <= spec["half_x"] + m and abs(y_local) <= spec["half_y"] + m


def world_aabb(spec):
    """ArraySpec.WorldAabb, ArraySpec.cs:80-88: (xMin, xMax, yMin, yMax) with the margin."""
    a_rad = spec["azimuth_deg"] * math.pi / 180.0
    cos_a = abs(math.cos(a_rad))
    sin_a = abs(math.sin(a_rad))
    m = spec["maintenance_margin_m"]
    ext_x = (spec["half_x"] + m) * cos_a + (spec["half_y"] + m) * sin_a
    ext_y = (spec["half_x"] + m) * sin_a + (spec["half_y"] + m) * cos_a
    return (spec["centre_x"] - ext_x, spec["centre_x"] + ext_x,
            spec["centre_y"] - ext_y, spec["centre_y"] + ext_y)


# ------------------------------------------------------------- scene model --

def read_grid(interpolate_z, rows, cols, x_min, x_max, y_min, y_max):
    """LeafExportSceneCommand.ReadGrid, :256-273: the grid re-sampled at its own nodes through
    the interpolator, 0.0 where it answers null (outside the grid)."""
    rows, cols = _grid_shape(rows, cols)
    dx = (x_max - x_min) / (cols - 1)
    dy = (y_max - y_min) / (rows - 1)
    out = [0.0] * (rows * cols)
    for r in range(rows):
        y = y_min + r * dy
        base = r * cols
        for c in range(cols):
            z = interpolate_z(x_min + c * dx, y)
            out[base + c] = 0.0 if z is None else z
    return out


def _grid_shape(rows, cols):
    rows = _int32(rows, "rows")
    cols = _int32(cols, "cols")
    if rows < 2 or cols < 2:
        raise SceneInputError("rows/cols must be >= 2")                         # ColladaExporter.cs:82
    if rows * cols > MAX_GRID_NODES:
        raise SceneInputError(f"grid {rows}x{cols} exceeds {MAX_GRID_NODES} nodes")
    return rows, cols


def _sanitized(dtm, dsm, n):
    """NaN/Infinity replacement, ColladaExporter.cs:95-111 and PvcExporter.cs:84-92: DTM holes
    take the mean of the finite DTM values, DSM holes the DTM value. Returns copies."""
    if not isinstance(dtm, (list, tuple)) or len(dtm) != n:
        raise SceneInputError(f"the DTM must hold rows * cols = {n} values")
    if dsm is not None and (not isinstance(dsm, (list, tuple)) or len(dsm) != n):
        raise SceneInputError(f"the DSM must hold rows * cols = {n} values")

    def num(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SceneInputError("grid values must be numbers")
        return float(v)
    dtm = [num(v) for v in dtm]
    finite = [v for v in dtm if math.isfinite(v)]
    fallback = 0.0
    if finite:
        total = 0.0
        for v in finite:
            total += v
        fallback = total / len(finite)
    dtm = [v if math.isfinite(v) else fallback for v in dtm]
    if dsm is not None:
        dsm = [num(v) for v in dsm]
        dsm = [v if math.isfinite(v) else dtm[i] for i, v in enumerate(dsm)]
    for v in dtm + (dsm or []):
        if abs(v) > MAX_ABS_VALUE:
            raise SceneInputError(f"grid values must be within {MAX_ABS_VALUE:g}")
    return dtm, dsm


def _z_offset(dtm):
    """Max DTM + 2 m, ColladaExporter.cs:128-131 (named meanDtmZ there), PvcExporter.cs:98-101."""
    max_z = -math.inf
    for v in dtm:
        if v > max_z:
            max_z = v
    return max_z + 2.0


def _ground_mesh(dtm, rows, cols, x_min, y_min, dx, dy, z_offset, stride):
    """The DTM subsampled by the stride, two triangles per quad (ColladaExporter.cs:139-167,
    PvcExporter.cs:104-132)."""
    gs = max(1, stride)
    g_rows = ((rows - 1) // gs) + 1
    g_cols = ((cols - 1) // gs) + 1
    verts = []
    for gr in range(g_rows):
        r = min(rows - 1, gr * gs)
        y = y_min + r * dy
        for gc in range(g_cols):
            c = min(cols - 1, gc * gs)
            verts.append((x_min + c * dx, y, dtm[r * cols + c] - z_offset))
    tris = []
    for gr in range(g_rows - 1):
        for gc in range(g_cols - 1):
            v00 = gr * g_cols + gc
            v10 = gr * g_cols + gc + 1
            v01 = (gr + 1) * g_cols + gc
            v11 = (gr + 1) * g_cols + gc + 1
            tris.append((v00, v10, v11))
            tris.append((v00, v11, v01))
    if not tris:
        # The plugin would write an empty <p>; the empty-text element form is not pinned by
        # any capture, so this refuses rather than guess.
        raise SceneInputError(f"a {rows}x{cols} grid has no ground triangles at stride {gs}")
    return verts, tris


def _obstacle_boxes(dtm, dsm, rows, cols, x_min, y_min, dx, dy, specs, min_height, stride,
                    z_offset, overlap):
    """Greedy-merged DSM obstacle boxes over the max-pooled grid (ColladaExporter.cs:182-307,
    PvcExporter.cs:138-257). Array footprints are excluded by the mega-cell centre (DAE,
    :230-255) or by any of its four corners or centre (PVC, `overlap`, :185-217)."""
    if dsm is None:
        return []
    ds = max(1, stride)
    m_rows = (rows + ds - 1) // ds
    m_cols = (cols + ds - 1) // ds
    m_dsm = [0.0] * (m_rows * m_cols)
    m_dtm = [0.0] * (m_rows * m_cols)
    mask = [False] * (m_rows * m_cols)
    for mr in range(m_rows):
        r0 = mr * ds
        r1 = min(rows, r0 + ds)
        for mc in range(m_cols):
            c0 = mc * ds
            c1 = min(cols, c0 + ds)
            max_dsm = -math.inf
            min_dtm = math.inf
            any_above = False
            for rr in range(r0, r1):
                for cc in range(c0, c1):
                    idx = rr * cols + cc
                    if dsm[idx] > max_dsm:
                        max_dsm = dsm[idx]
                    if dtm[idx] < min_dtm:
                        min_dtm = dtm[idx]
                    if (dsm[idx] - dtm[idx]) >= min_height:
                        any_above = True
            m_idx = mr * m_cols + mc
            m_dsm[m_idx] = max_dsm
            m_dtm[m_idx] = min_dtm
            mask[m_idx] = any_above
    visited = [False] * (m_rows * m_cols)
    if specs:
        aabbs = [(s, world_aabb(s)) for s in specs]
        for mr in range(m_rows):
            cy_min_cell = y_min + (mr * ds - 0.5) * dy
            cy_max_cell = y_min + ((mr + 1) * ds - 0.5) * dy
            cy_centre = 0.5 * (cy_min_cell + cy_max_cell)
            for mc in range(m_cols):
                cx_min_cell = x_min + (mc * ds - 0.5) * dx
                cx_max_cell = x_min + ((mc + 1) * ds - 0.5) * dx
                cx_centre = 0.5 * (cx_min_cell + cx_max_cell)
                for s, bb in aabbs:
                    if cx_max_cell < bb[0] or cx_min_cell > bb[1]:
                        continue
                    if cy_max_cell < bb[2] or cy_min_cell > bb[3]:
                        continue
                    if overlap:
                        hit = (contains_xy(s, cx_min_cell, cy_min_cell)
                               or contains_xy(s, cx_max_cell, cy_min_cell)
                               or contains_xy(s, cx_min_cell, cy_max_cell)
                               or contains_xy(s, cx_max_cell, cy_max_cell)
                               or contains_xy(s, 0.5 * (cx_min_cell + cx_max_cell),
                                              0.5 * (cy_min_cell + cy_max_cell)))
                    else:
                        hit = contains_xy(s, cx_centre, cy_centre)
                    if hit:
                        visited[mr * m_cols + mc] = True
                        break
    boxes = []
    for r in range(m_rows):
        for c in range(m_cols):
            i = r * m_cols + c
            if visited[i] or not mask[i]:
                continue
            c1 = c
            while c1 + 1 < m_cols and mask[r * m_cols + c1 + 1] and not visited[r * m_cols + c1 + 1]:
                c1 += 1
            r1 = r
            extend = True
            while extend and r1 + 1 < m_rows:
                for cc in range(c, c1 + 1):
                    idx = (r1 + 1) * m_cols + cc
                    if not mask[idx] or visited[idx]:
                        extend = False
                        break
                if extend:
                    r1 += 1
            max_z = -math.inf
            min_ground_z = math.inf
            for rr in range(r, r1 + 1):
                for cc in range(c, c1 + 1):
                    idx = rr * m_cols + cc
                    visited[idx] = True
                    if m_dsm[idx] > max_z:
                        max_z = m_dsm[idx]
                    if m_dtm[idx] < min_ground_z:
                        min_ground_z = m_dtm[idx]
            boxes.append((x_min + (c * ds - 0.5) * dx, x_min + ((c1 + 1) * ds - 0.5) * dx,
                          y_min + (r * ds - 0.5) * dy, y_min + ((r1 + 1) * ds - 0.5) * dy,
                          min_ground_z - z_offset, max_z - z_offset))
    return boxes


def _append_box(verts, tris, x0, x1, y0, y1, z0, z1):
    """AppendBox, ColladaExporter.cs:384-419 and PvcExporter.cs:642-668: 8 corners, 12 triangles."""
    b = len(verts)
    verts.extend(((x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                  (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)))
    tris.extend(((b + 4, b + 5, b + 6), (b + 4, b + 6, b + 7),
                 (b + 0, b + 2, b + 1), (b + 0, b + 3, b + 2),
                 (b + 0, b + 1, b + 5), (b + 0, b + 5, b + 4),
                 (b + 1, b + 2, b + 6), (b + 1, b + 6, b + 5),
                 (b + 2, b + 3, b + 7), (b + 2, b + 7, b + 6),
                 (b + 3, b + 0, b + 4), (b + 3, b + 4, b + 7)))


def _sample_dtm(dtm, rows, cols, x_min, y_min, dx, dy, x, y):
    """PvcExporter.SampleDtm, :422-434: the nearest node (banker's Math.Round), clamped."""
    if dx == 0 or dy == 0:
        raise SceneInputError("a grid with an empty extent cannot seat a frame on the ground")
    c = int(round((x - x_min) / dx))
    r = int(round((y - y_min) / dy))
    c = min(max(c, 0), cols - 1)
    r = min(max(r, 0), rows - 1)
    return dtm[r * cols + c]


def _frames(specs, dtm, rows, cols, x_min, y_min, dx, dy, z_offset, pv_base_z):
    """PV racks per array, PvcExporter.cs:262-326: the top tilted about X (the -Y edge high),
    rotated by the azimuth and moved to the centre; the bottom dropped to the nearest node."""
    frames = []
    for a in specs:
        if a["half_x"] <= 0 or a["half_y"] <= 0:
            continue
        nx = max(1, a["frames_in_x"])
        ny = max(1, a["frames_in_y"])
        if len(frames) + nx * ny > MAX_FRAMES:
            raise SceneInputError(f"the scene would hold more than {MAX_FRAMES} frames")
        frame_w = 2 * a["half_x"] / nx
        frame_d = 2 * a["half_y"] / ny
        t_rad = a["tilt_deg"] * math.pi / 180.0
        a_rad = a["azimuth_deg"] * math.pi / 180.0
        sin_t, cos_t = math.sin(t_rad), math.cos(t_rad)
        sin_a, cos_a = math.sin(a_rad), math.cos(a_rad)
        for fy in range(ny):
            y_lo = -a["half_y"] + fy * frame_d
            y_hi = y_lo + frame_d
            for fx in range(nx):
                x_lo = -a["half_x"] + fx * frame_w
                x_hi = x_lo + frame_w
                top = []
                for rx, ry in ((x_lo, y_lo), (x_hi, y_lo), (x_hi, y_hi), (x_lo, y_hi)):
                    z = -ry * sin_t + pv_base_z
                    y_t = ry * cos_t
                    top.append((rx * cos_a - y_t * sin_a + a["centre_x"],
                                rx * sin_a + y_t * cos_a + a["centre_y"], z))
                bottom = [(x, y, _sample_dtm(dtm, rows, cols, x_min, y_min, dx, dy, x, y) - z_offset)
                          for x, y, _ in top]
                frames.append({"verts": top + bottom,
                               "module_width_m": a["module_width_m"], "module_height_m": a["module_height_m"],
                               "module_x_spacing_m": a["module_x_spacing_m"],
                               "module_y_spacing_m": a["module_y_spacing_m"],
                               # ToSpec's `?? "generic"` (:248-249) replaces only a missing name.
                               "module_manufacturer": a["module_manufacturer"],
                               "module_name": a["module_name"]})
    return frames


# -------------------------------------------------------------- XML writers --

def _geometry(level, gid, verts, tris, material=None, extra=()):
    """ColladaExporter.WriteGeometry, :421-492, and PvcExporter's WriteGeometry /
    WritePositionsSource / WriteVerticesInput / WriteTriangles, :487-615: positions as F4
    reals each followed by one space, indices each followed by one space."""
    i = "  " * level
    floats = "".join(f"{_f4(x)} {_f4(y)} {_f4(z)} " for x, y, z in verts)
    indices = "".join(f"{a} {b} {c} " for a, b, c in tris)
    mat = "" if material is None else f' material="{material}"'
    return [
        f'{i}<geometry id="{gid}">',
        f"{i}  <mesh>",
        f'{i}    <source id="{gid}-positions">',
        f'{i}      <float_array id="{gid}-positions-array" count="{len(verts) * 3}">{floats}</float_array>',
        f"{i}      <technique_common>",
        f'{i}        <accessor source="#{gid}-positions-array" count="{len(verts)}" stride="3">',
        f'{i}          <param name="X" type="float" />',
        f'{i}          <param name="Y" type="float" />',
        f'{i}          <param name="Z" type="float" />',
        f"{i}        </accessor>",
        f"{i}      </technique_common>",
        f"{i}    </source>",
        f'{i}    <vertices id="{gid}-vertices">',
        f'{i}      <input semantic="POSITION" source="#{gid}-positions" />',
        f"{i}    </vertices>",
        f'{i}    <triangles count="{len(tris)}"{mat}>',
        f'{i}      <input semantic="VERTEX" source="#{gid}-vertices" offset="0" />',
        f"{i}      <p>{indices}</p>",
        f"{i}    </triangles>",
        *extra,
        f"{i}  </mesh>",
        f"{i}</geometry>",
    ]


def write_collada(dtm, dsm, rows, cols, x_min, x_max, y_min, y_max, specs=None,
                  min_obstacle_height_m=1.0, ground_stride=4, dsm_stride=4, pv_base_z=0.0,
                  created="", modified=""):
    """ColladaExporter.Write, ColladaExporter.cs:68-382: the DTM ground and, with a DSM, one
    obstacles geometry of merged boxes. No PV geometry (the DAE path never writes one, :317-321;
    pv_base_z is accepted for the signature and unused, as in the plugin)."""
    rows, cols = _grid_shape(rows, cols)
    bounds = [_finite(v, "grid bound") for v in (x_min, x_max, y_min, y_max)]
    x_min, x_max, y_min, y_max = bounds
    dx = (x_max - x_min) / (cols - 1)
    dy = (y_max - y_min) / (rows - 1)
    dtm, dsm = _sanitized(dtm, dsm, rows * cols)
    z_offset = _z_offset(dtm)
    ground_verts, ground_tris = _ground_mesh(dtm, rows, cols, x_min, y_min, dx, dy, z_offset, ground_stride)
    obstacle_verts, obstacle_tris = [], []
    boxes = _obstacle_boxes(dtm, dsm, rows, cols, x_min, y_min, dx, dy, list(specs or ()),
                            min_obstacle_height_m, dsm_stride, z_offset, overlap=False)
    for box in boxes:
        _append_box(obstacle_verts, obstacle_tris, *box)
    lines = [XML_DECLARATION,
             f'<COLLADA version="1.4.1" xmlns="{COLLADA_NS}">',
             "  <asset>",
             _element("    ", "created", created),
             _element("    ", "modified", modified),
             '    <unit name="metre" meter="1.0" />',
             "    <up_axis>Z_UP</up_axis>",
             "  </asset>",
             "  <library_geometries>",
             *_geometry(2, "ground", ground_verts, ground_tris)]
    if boxes:
        lines += _geometry(2, "obstacles", obstacle_verts, obstacle_tris)
    lines += ["  </library_geometries>",
              "  <library_visual_scenes>",
              '    <visual_scene id="scene">',
              '      <node id="ground_node" name="Ground">',
              '        <instance_geometry url="#ground" />',
              "      </node>"]
    if boxes:
        lines += ['      <node id="obstacles_node" name="Obstacles">',
                  '        <instance_geometry url="#obstacles" />',
                  "      </node>"]
    lines += ["    </visual_scene>",
              "  </library_visual_scenes>",
              "  <scene>",
              '    <instance_visual_scene url="#scene" />',
              "  </scene>",
              "</COLLADA>"]
    return NEWLINE.join(lines)


def _instance_geometry(url):
    """PvcExporter.WriteInstanceGeometry, :617-640: every material bound on every instance."""
    return ([f'          <instance_geometry url="{url}">',
             "            <bind_material>",
             "              <technique_common>"]
            + [f'                <instance_material symbol="{m}" target="#{m}" />' for m, _, _ in MATERIALS]
            + ["              </technique_common>",
               "            </bind_material>",
               "          </instance_geometry>"])


def write_pvc(dtm, dsm, rows, cols, x_min, x_max, y_min, y_max, specs,
              min_obstacle_height_m=3.0, ground_stride=12, dsm_stride=8, pv_base_z=0.0):
    """PvcExporter.Write, PvcExporter.cs:62-408: the ground (Material3), one tree_crown box per
    merged obstacle (Material2) and one Frame per rack (Material0) with its frame_parameters."""
    rows, cols = _grid_shape(rows, cols)
    x_min, x_max, y_min, y_max = [_finite(v, "grid bound") for v in (x_min, x_max, y_min, y_max)]
    pv_base_z = _finite(pv_base_z, "pv_base_z")
    dx = (x_max - x_min) / (cols - 1)
    dy = (y_max - y_min) / (rows - 1)
    dtm, dsm = _sanitized(dtm, dsm, rows * cols)
    z_offset = _z_offset(dtm)
    specs = list(specs or ())
    ground_verts, ground_tris = _ground_mesh(dtm, rows, cols, x_min, y_min, dx, dy, z_offset, ground_stride)
    boxes = _obstacle_boxes(dtm, dsm, rows, cols, x_min, y_min, dx, dy, specs,
                            min_obstacle_height_m, dsm_stride, z_offset, overlap=True)
    frames = _frames(specs, dtm, rows, cols, x_min, y_min, dx, dy, z_offset, pv_base_z)

    lines = [XML_DECLARATION,
             f'<COLLADA version="1.4.1" xmlns="{COLLADA_NS}">',
             "  <asset>",                                                        # :340-355
             "    <contributor>",
             "      <author />",
             "      <authoring_tool>Leaf Solar Design</authoring_tool>",
             "      <comments />",
             "    </contributor>",
             "    <keywords />",
             "    <revision />",
             "    <subject />",
             "    <title />",
             '    <unit meter="1.0" name="metre" />',
             "    <up_axis>Z_UP</up_axis>",
             "  </asset>",
             "  <library_materials>"]                                            # :436-455
    for mid, name, _ in MATERIALS:
        lines += [f'    <material id="{mid}" name="{name}">',
                  f'      <instance_effect url="#{mid}-fx" />',
                  "    </material>"]
    lines += ["  </library_materials>", "  <library_effects>"]                   # :457-485
    for mid, _, rgba in MATERIALS:
        lines += [f'    <effect id="{mid}-fx" name="{mid}">',
                  "      <profile_COMMON>",
                  '        <technique sid="standard">',
                  "          <phong>",
                  "            <diffuse>",
                  f"              <color>{rgba}</color>",
                  "            </diffuse>",
                  "          </phong>",
                  "        </technique>",
                  "      </profile_COMMON>",
                  "    </effect>"]
    lines += ["  </library_effects>", "  <library_geometries>"]
    lines += _geometry(2, "topography_mesh", ground_verts, ground_tris, MAT_TOPOGRAPHY)
    for i, box in enumerate(boxes):
        verts, tris = [], []
        _append_box(verts, tris, *box)
        lines += _geometry(2, f"tree_crown_{i}", verts, tris, MAT_TREE_CROWN)
    for i, f in enumerate(frames):                                               # :505-543
        params = ["        <frame_parameters>",
                  f"          <module_width>{_mm(f['module_width_m'])}</module_width>",
                  f"          <module_height>{_mm(f['module_height_m'])}</module_height>",
                  f"          <module_x_spacing>{_mm(f['module_x_spacing_m'])}</module_x_spacing>",
                  f"          <module_y_spacing>{_mm(f['module_y_spacing_m'])}</module_y_spacing>",
                  _element("          ", "module_manufacturer", f["module_manufacturer"]),
                  _element("          ", "module_name", f["module_name"]),
                  "        </frame_parameters>"]
        lines += _geometry(2, f"Frame{i}", f["verts"], [(0, 1, 2), (0, 2, 3)], MAT_FRAMES, params)
    lines += ["  </library_geometries>",
              "  <library_visual_scenes>",                                       # :378-398
              '    <visual_scene id="Scene0" name="Scene0">',
              '      <node id="Fbx_Root" name="Fbx_Root">',
              '        <node id="LeafScene0" name="LeafScene0">']
    lines += _instance_geometry("#topography_mesh")
    for i in range(len(boxes)):
        lines += _instance_geometry(f"#tree_crown_{i}")
    for i in range(len(frames)):
        lines += _instance_geometry(f"#Frame{i}")
    lines += ["        </node>",
              "      </node>",
              "    </visual_scene>",
              "  </library_visual_scenes>",
              "  <scene>",
              '    <instance_visual_scene url="#Scene0" />',
              "  </scene>",
              "</COLLADA>"]
    return NEWLINE.join(lines)


# ------------------------------------------------------------ the command --

def export_scene(dtm_grid, records, dsm_grid=None, created=None, modified=None):
    """LEAFEXPORTSCENE, LeafExportSceneCommand.cs:48-201, without the file system.

    dtm_grid / dsm_grid: terrain interpolators (rows, cols, x_min, x_max, y_min, y_max and
    interpolate_z(x, y), as solar_ground_terrain.TerrainGridInterpolator), or None when the
    drawing has no such grid. records: the array store. created / modified: the DAE asset
    stamps (DateTime.UtcNow "o"; utc_stamp() when omitted).

    Returns {"succeeded", "message", "dae", "pvc", "rows", "cols", "has_dsm", "array_count"}; no
    terrain grid is the command's failure (:71-88) and writes nothing."""
    if dtm_grid is None:
        return {"succeeded": False, "message": "LEAFEXPORTSCENE did not run: no terrain grid.",
                "dae": None, "pvc": None, "rows": 0, "cols": 0, "has_dsm": False, "array_count": 0}
    specs = [to_spec(r) for r in list_arrays(records)]                           # :95-99
    rows, cols = _grid_shape(dtm_grid.rows, dtm_grid.cols)                       # :115-117
    bounds = [_finite(v, "grid bound") for v in (dtm_grid.x_min, dtm_grid.x_max, dtm_grid.y_min, dtm_grid.y_max)]
    dtm = read_grid(dtm_grid.interpolate_z, rows, cols, *bounds)                 # :119
    dsm = None if dsm_grid is None else read_grid(dsm_grid.interpolate_z, rows, cols, *bounds)  # :120-122
    now = utc_stamp()
    created = now if created is None else created
    modified = now if modified is None else modified
    for stamp in (created, modified):
        if not isinstance(stamp, str) or not _O_STAMP.fullmatch(stamp):
            raise SceneInputError("a DAE stamp is a UTC round-trip time, yyyy-MM-ddTHH:mm:ss.fffffffZ")
    dae = write_collada(dtm, dsm, rows, cols, *bounds, specs=specs,
                        min_obstacle_height_m=MIN_OBSTACLE_HEIGHT_M, ground_stride=GROUND_STRIDE,
                        dsm_stride=DSM_STRIDE, pv_base_z=PV_BASE_Z, created=created, modified=modified)
    pvc = write_pvc(dtm, dsm, rows, cols, *bounds, specs,
                    min_obstacle_height_m=MIN_OBSTACLE_HEIGHT_M, ground_stride=GROUND_STRIDE,
                    dsm_stride=DSM_STRIDE, pv_base_z=PV_BASE_Z)
    message = ("LEAFEXPORTSCENE wrote PVsyst shade scene." if specs
               else "LEAFEXPORTSCENE wrote terrain-only PVsyst shade scene.")   # :173-176
    return {"succeeded": True, "message": message, "dae": dae, "pvc": pvc, "rows": rows, "cols": cols,
            "has_dsm": dsm is not None, "array_count": len(specs)}
