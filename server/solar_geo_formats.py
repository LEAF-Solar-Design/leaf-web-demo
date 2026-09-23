"""Literal ports of the plugin's KML and LandXML writers and readers.

Four production capabilities, each exposed by a licensed LEAF*DEMO command that
runs a fixed fixture and writes probe files with no drawing state:

  * kml-export     KmlBoundaryExporter (Terrain/KmlBoundaryExporter.cs), driven by
                   LEAFKMLEXPORTDEMO (Terrain/KmlExportCommand.cs:207-298) and
                   LEAFKMLEXPORTFMTDEMO (Terrain/KmlExportFmtDemoCommand.cs).
  * kml-import     KmlBoundaryParser (Terrain/KmlBoundaryParser.cs), driven by
                   LEAFKMLIMPORTDEMO (Terrain/KmlImportDemoCommand.cs).
  * landxml-export TerrainExporter.ToLandXml with its Fixed and Delaunay
                   triangulations (Terrain/TerrainExporter.cs), driven by
                   LEAFLANDXMLDEMO (Terrain/LandXmlExportDemoCommand.cs).
  * landxml-import TerrainImporter.ParseLandXmlPoints (Terrain/TerrainImporter.cs:
                   105-143) and LandXmlImporter (Terrain/LandXmlImporter.cs), driven
                   by LEAFIMPORTLANDXMLDEMO (Terrain/LandXmlImportDemoCommand.cs).

Sources read 2026-09-23 at C:/tmp/solar-parity/wt-b25-s17. Citations below are
`<file>:<line>` into that tree.

Output contract: the TEXT each writer returns is the plugin's text, character for
character: element and attribute order, two-space indentation, CRLF line breaks
(XmlWriterSettings.NewLineChars and StringBuilder.AppendLine on Windows), the XML
declaration, and number rendering (G17 for KML coordinates, F3 for LandXML
points, F8 and R for the import reports). A byte order mark is ENCODING, not
text: the demo builders below prepend it exactly where the command passes
Encoding.UTF8 to File.WriteAllText, and the pure writers never do.

Two literals follow the LICENSED binary rather than today's source, because the
capture of 2026-09-23 is the ground truth and the source tree has since been
edited by text sweeps: the pole-guard message carries an em dash where the
source now has a hyphen (KmlBoundaryExporter.cs:128-131), and the LandXML
surface `desc` attribute keeps the company name the licensed build wrote
(TerrainExporter.cs:125-127). Both are named constants below.

Hardening. Every XML document this module reads goes through `parse_xml`, which
is expat with no DTD at all: a DOCTYPE, an entity or notation declaration, and
an external entity reference are REFUSED before any expansion can happen, so
neither an external fetch nor a billion-laughs expansion is reachable. Input is
bounded by character count, element count and nesting depth, and the tree walk
is iterative, so a deep document cannot exhaust the Python stack. The writers
refuse any character XmlWriter itself would refuse (CheckCharacters), and the
LandXML writer refuses a surface name that would break its unescaped attribute
rather than emitting malformed XML the way the plugin would.

No network, no dependencies outside the standard library.
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import math
from pathlib import Path
import re
import unicodedata
from xml.parsers import expat

CRLF = "\r\n"
BOM = "\ufeff"


def _load(name):
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The C# number formatters already live in the NEC and probe-calc ports, with
# their tie rules; re-deriving them here is how two renderings drift apart.
calcs = _load("solar_probe_calcs")
nec = calcs.nec
terrain = _load("solar_terrain")

format_fixed = calcs.format_fixed
format_roundtrip = calcs.format_roundtrip
TerrainGridInterpolator = terrain.TerrainGridInterpolator


def format_g17(value):
    """C# double.ToString("G17", InvariantCulture) (KmlBoundaryExporter.cs:229-236)."""
    return nec._format_general(float(value), 17)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class GeoFormatError(ValueError):
    """A document this module cannot read or a value it cannot write."""


class UnsafeXmlError(GeoFormatError):
    """A document refused for its SHAPE: a DTD, an entity, or a size bound."""


class KmlArgumentError(GeoFormatError):
    """C# ArgumentException; str() is its Message, parameter suffix included."""


# --------------------------------------------------------------------------- #
# Safe XML reading (the only XML entry point in this module)
# --------------------------------------------------------------------------- #
MAX_XML_CHARS = 16 * 1024 * 1024
MAX_XML_ELEMENTS = 1_000_000
MAX_XML_DEPTH = 256


class XmlElement:
    """One element: its LOCAL name and its content in document order.

    `content` holds text strings and child elements interleaved, so `value`
    reproduces XElement.Value (every descendant text node, concatenated).
    """

    __slots__ = ("local_name", "content")

    def __init__(self, local_name):
        self.local_name = local_name
        self.content = []

    def children(self):
        return [item for item in self.content if isinstance(item, XmlElement)]

    def descendants(self):
        """XContainer.Descendants(): every element below this one, pre-order.

        Iterative on purpose, so nesting depth never reaches the Python stack.
        """
        result = []
        stack = [iter(self.content)]
        while stack:
            item = next(stack[-1], None)
            if item is None:
                stack.pop()
                continue
            if isinstance(item, XmlElement):
                result.append(item)
                stack.append(iter(item.content))
        return result

    def value(self):
        """XElement.Value: all descendant text, concatenated in document order."""
        parts = []
        stack = [iter(self.content)]
        while stack:
            item = next(stack[-1], None)
            if item is None:
                stack.pop()
                continue
            if isinstance(item, XmlElement):
                stack.append(iter(item.content))
            else:
                parts.append(item)
        return "".join(parts)


def _refuse(what):
    def handler(*_args):
        raise UnsafeXmlError("XML " + what + " refused")
    return handler


def parse_xml(text):
    """Parse an XML document with no DTD, no entities and bounded size.

    Returns the root XmlElement. Fails closed with UnsafeXmlError on a DOCTYPE,
    an entity or notation declaration, an external entity reference, or a size,
    element-count or depth bound, and with GeoFormatError on anything that is
    not well-formed XML (C# XDocument.Parse throws XmlException there). A single
    leading byte order mark is encoding and is dropped, as File.ReadAllText does.
    """
    if not isinstance(text, str):
        raise TypeError("XML content must be text")
    if len(text) > MAX_XML_CHARS:
        raise UnsafeXmlError("XML document exceeds %d characters" % MAX_XML_CHARS)
    if text.startswith(BOM):
        text = text[1:]
    # namespace_separator makes expat resolve prefixes, so an element name is
    # "<uri> <local>" or "<local>" and the local name is its last part, the same
    # thing XName.LocalName reports. An unbound prefix is a well-formedness error.
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _refuse("DOCTYPE")
    parser.EntityDeclHandler = _refuse("entity declaration")
    parser.UnparsedEntityDeclHandler = _refuse("unparsed entity")
    parser.NotationDeclHandler = _refuse("notation declaration")
    parser.ExternalEntityRefHandler = _refuse("external entity reference")
    parser.buffer_text = True

    stack = []
    holder = []
    count = [0]

    def start(name, _attributes):
        count[0] += 1
        if count[0] > MAX_XML_ELEMENTS:
            raise UnsafeXmlError("XML document exceeds %d elements" % MAX_XML_ELEMENTS)
        if len(stack) >= MAX_XML_DEPTH:
            raise UnsafeXmlError("XML document exceeds depth %d" % MAX_XML_DEPTH)
        element = XmlElement(name.rsplit(" ", 1)[-1])
        if stack:
            stack[-1].content.append(element)
        else:
            holder.append(element)
        stack.append(element)

    def end(_name):
        stack.pop()

    def characters(data):
        # Text outside the root is whitespace (anything else is an expat error).
        if stack:
            stack[-1].content.append(data)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    try:
        parser.Parse(text, True)
    except expat.ExpatError as error:
        raise GeoFormatError("XML is not well-formed: %s" % error) from None
    if not holder:
        raise GeoFormatError("XML document has no root element")
    return holder[0]


def _descendants_by_local_name(element, local_name):
    """KmlBoundaryParser.DescendantsByLocalName (KmlBoundaryParser.cs:210-215)."""
    return [item for item in element.descendants() if item.local_name == local_name]


# .NET char.IsWhiteSpace: the Unicode separators plus U+0009-000D and U+0085.
_NET_CONTROL_SPACE = frozenset("\t\n\v\f\r\x85")


def _net_is_white(ch):
    return ch in _NET_CONTROL_SPACE or unicodedata.category(ch) in ("Zs", "Zl", "Zp")


def _net_trim(text):
    """C# string.Trim(): strips char.IsWhiteSpace from both ends."""
    start, end = 0, len(text)
    while start < end and _net_is_white(text[start]):
        start += 1
    while end > start and _net_is_white(text[end - 1]):
        end -= 1
    return text[start:end]


def _net_split_white(text):
    """C# Split((char[])null, RemoveEmptyEntries): split on char.IsWhiteSpace."""
    tokens = []
    current = []
    for ch in text:
        if _net_is_white(ch):
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


# NumberStyles.Float in InvariantCulture: optional sign, digits with an optional
# point, optional exponent. Deliberately narrower than Python's float(), which
# would also take "1_0", "inf" and "nan" spellings C# does not.
_NET_FLOAT = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_NET_WHITE_EDGES = " \t\n\v\f\r"
_NET_SPECIALS = {"nan": math.nan, "-nan": math.nan, "+nan": math.nan,
                 "infinity": math.inf, "+infinity": math.inf, "-infinity": -math.inf,
                 "\u221e": math.inf, "+\u221e": math.inf, "-\u221e": -math.inf}


def _net_try_parse_double(token):
    """C# double.TryParse(s, NumberStyles.Float, InvariantCulture): value or None.

    An out-of-range exponent is +/-Infinity on .NET Core 3.0+, exactly as float().
    """
    text = token.strip(_NET_WHITE_EDGES)
    if _NET_FLOAT.match(text):
        return float(text)
    return _NET_SPECIALS.get(text.lower())


def _net_parse_double(token):
    """C# double.Parse(s, InvariantCulture): FormatException becomes GeoFormatError."""
    value = _net_try_parse_double(token)
    if value is None:
        raise GeoFormatError("The input string '%s' was not in a correct format." % token)
    return value


# --------------------------------------------------------------------------- #
# KML value types (KmlBoundaryParser.cs:37-69)
# --------------------------------------------------------------------------- #
class KmlCoordinate:
    """lon, lat in degrees and altitude in metres (KmlBoundaryParser.cs:37-49)."""

    __slots__ = ("lon_deg", "lat_deg", "altitude_m")

    def __init__(self, lon_deg, lat_deg, altitude_m=0.0):
        self.lon_deg = float(lon_deg)
        self.lat_deg = float(lat_deg)
        self.altitude_m = float(altitude_m)

    def __eq__(self, other):
        return (isinstance(other, KmlCoordinate)
                and (self.lon_deg, self.lat_deg, self.altitude_m)
                == (other.lon_deg, other.lat_deg, other.altitude_m))

    def __hash__(self):
        return hash((self.lon_deg, self.lat_deg, self.altitude_m))

    def __repr__(self):
        return "KmlCoordinate(%r, %r, %r)" % (self.lon_deg, self.lat_deg, self.altitude_m)


class KmlPolygon:
    """A named ring (KmlBoundaryParser.cs:51-61). `vertices` may be None, as in C#."""

    __slots__ = ("name", "vertices")

    def __init__(self, name, vertices):
        self.name = name
        self.vertices = None if vertices is None else tuple(vertices)


class KmlProjectedPoint:
    """Drawing-frame metres (KmlBoundaryParser.cs:63-68)."""

    __slots__ = ("x", "y")

    def __init__(self, x, y):
        self.x = float(x)
        self.y = float(y)


EARTH_METRES_PER_DEGREE = 111_000.0   # KmlBoundaryExporter.cs:53, KmlBoundaryParser.cs:72
KML_NS = "http://www.opengis.net/kml/2.2"   # KmlBoundaryExporter.cs:52
# The licensed binary's ArgumentException.Message, parameter suffix included; see
# the module docstring for why this is not today's source text.
POLE_GUARD_MESSAGE = ("centerLatDeg too close to \u00b190\u00b0 (pole) \u2014 longitude "
                      "degenerate. (Parameter 'centerLatDeg')")


# --------------------------------------------------------------------------- #
# KML writing (KmlBoundaryExporter.cs)
# --------------------------------------------------------------------------- #
def _check_xml_chars(text):
    """XmlWriter CheckCharacters: refuse what XML 1.0 cannot carry."""
    for ch in text:
        code = ord(ch)
        if code < 0x20 and ch not in "\t\n\r":
            raise GeoFormatError("'\\x%02x', hexadecimal value 0x%02X, is an invalid character."
                                 % (code, code))
        if 0xD800 <= code <= 0xDFFF or code in (0xFFFE, 0xFFFF):
            raise GeoFormatError("U+%04X is an invalid character." % code)


_NEWLINES = re.compile(r"\r\n|\r|\n")


def _xml_text(text):
    """XmlWriter element text: & < > escaped, each newline written as CRLF.

    NewLineHandling.Replace (the default) rewrites every CR, LF and CRLF inside
    element content to NewLineChars, which is CRLF on the Windows host.
    """
    _check_xml_chars(text)
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _NEWLINES.sub(CRLF, escaped)


def _is_closed(vertices):
    """IsClosed (KmlBoundaryExporter.cs:215-223): strict 1e-12 on lon, lat and alt."""
    if len(vertices) < 2:
        return False
    a = vertices[0]
    b = vertices[-1]
    return (abs(a.lon_deg - b.lon_deg) < 1e-12
            and abs(a.lat_deg - b.lat_deg) < 1e-12
            and abs(a.altitude_m - b.altitude_m) < 1e-12)


def _tuple(coordinate):
    """AppendTuple (KmlBoundaryExporter.cs:225-237): lon,lat and ,alt only when nonzero."""
    text = format_g17(coordinate.lon_deg) + "," + format_g17(coordinate.lat_deg)
    if abs(coordinate.altitude_m) > 1e-12:
        text += "," + format_g17(coordinate.altitude_m)
    return text


def format_coordinates(vertices):
    """FormatCoordinates (KmlBoundaryExporter.cs:193-213): the ring, closed per §10.6."""
    if not vertices:
        return ""
    parts = [_tuple(vertex) for vertex in vertices]
    if not _is_closed(vertices):
        parts.append(_tuple(vertices[0]))
    return " ".join(parts)


def write_kml(polygons, document_name):
    """WriteKml (KmlBoundaryExporter.cs:63-86) as the text XmlWriter produces.

    BuildDocument (cs:152-169) skips a None polygon and omits an empty or None
    name; BuildPlacemark (cs:171-186) nests Polygon/outerBoundaryIs/LinearRing.
    XmlWriter with Indent=true writes two-space indentation, CRLF between nodes,
    no newline after the last one, and the declaration its StringWriterUtf8
    reports (cs:243-247). An element with no content at all is written `<x />`.
    """
    if polygons is None:
        raise TypeError("polygons is required")
    body = []
    if document_name:
        body.append("    <name>" + _xml_text(document_name) + "</name>")
    for polygon in polygons:
        if polygon is None:
            continue
        body.append("    <Placemark>")
        if polygon.name:
            body.append("      <name>" + _xml_text(polygon.name) + "</name>")
        body.append("      <Polygon>")
        body.append("        <outerBoundaryIs>")
        body.append("          <LinearRing>")
        body.append("            <coordinates>" + format_coordinates(polygon.vertices or ())
                    + "</coordinates>")
        body.append("          </LinearRing>")
        body.append("        </outerBoundaryIs>")
        body.append("      </Polygon>")
        body.append("    </Placemark>")
    lines = ['<?xml version="1.0" encoding="utf-8"?>',
             '<kml xmlns="' + KML_NS + '">']
    if body:
        lines.append("  <Document>")
        lines.extend(body)
        lines.append("  </Document>")
    else:
        lines.append("  <Document />")
    lines.append("</kml>")
    return CRLF.join(lines)


def write_kml_from_projected(polygons, center_lat_deg, center_lon_deg, document_name):
    """WriteKmlFromProjected (KmlBoundaryExporter.cs:118-146), the equirectangular inverse.

    `polygons` is a sequence of (name, [KmlProjectedPoint]). The pole guard
    raises KmlArgumentError with the licensed binary's message.
    """
    if polygons is None:
        raise TypeError("polygons is required")
    lat_rad = center_lat_deg * math.pi / 180.0
    metres_per_deg_lon = EARTH_METRES_PER_DEGREE * math.cos(lat_rad)
    if abs(metres_per_deg_lon) < 1e-9:
        raise KmlArgumentError(POLE_GUARD_MESSAGE)
    converted = []
    for name, vertices in polygons:
        coordinates = []
        for point in vertices:
            lon = center_lon_deg + point.x / metres_per_deg_lon
            lat = center_lat_deg + point.y / EARTH_METRES_PER_DEGREE
            coordinates.append(KmlCoordinate(lon, lat, 0.0))
        converted.append(KmlPolygon(name, coordinates))
    return write_kml(converted, document_name)


# --------------------------------------------------------------------------- #
# KML reading (KmlBoundaryParser.cs)
# --------------------------------------------------------------------------- #
_KML_TOKEN_SEPARATORS = re.compile(r"[ \t\n\r]+")


def _parse_coordinate_list(text):
    """ParseCoordinateList (KmlBoundaryParser.cs:217-241): split on space, tab, CR, LF."""
    coordinates = []
    for token in _KML_TOKEN_SEPARATORS.split(text):
        if not token:
            continue
        parts = token.split(",")
        if len(parts) < 2:
            raise GeoFormatError(
                "KML coordinate '%s' has fewer than 2 comma-separated components." % token)
        lon = _net_parse_double(parts[0])
        lat = _net_parse_double(parts[1])
        alt = _net_parse_double(parts[2]) if len(parts) >= 3 else 0.0
        coordinates.append(KmlCoordinate(lon, lat, alt))
    return coordinates


def _drop_closing_duplicate(ring):
    """DropClosingDuplicate (KmlBoundaryParser.cs:243-258): lon and lat within 1e-9."""
    if len(ring) < 2:
        return ring
    first, last = ring[0], ring[-1]
    if abs(first.lon_deg - last.lon_deg) < 1e-9 and abs(first.lat_deg - last.lat_deg) < 1e-9:
        return ring[:-1]
    return ring


def parse_kml(kml_xml):
    """KmlBoundaryParser.Parse (KmlBoundaryParser.cs:78-121), through `parse_xml`.

    Every Placemark in document order whose first Polygon/outerBoundaryIs/
    LinearRing/coordinates chain exists and holds at least three distinct
    vertices; the name is the first descendant <name>'s trimmed text.
    """
    if kml_xml is None:
        raise TypeError("kml_xml is required")
    root = parse_xml(kml_xml)
    result = []
    for placemark in _descendants_by_local_name(root, "Placemark"):
        chain = placemark
        for local_name in ("Polygon", "outerBoundaryIs", "LinearRing", "coordinates"):
            found = _descendants_by_local_name(chain, local_name)
            if not found:
                chain = None
                break
            chain = found[0]
        if chain is None:
            continue
        distinct = _drop_closing_duplicate(_parse_coordinate_list(chain.value()))
        if len(distinct) < 3:
            continue
        names = _descendants_by_local_name(placemark, "name")
        name = _net_trim(names[0].value()) if names else ""
        result.append(KmlPolygon(name, distinct))
    return result


def centroid(polygon):
    """KmlBoundaryParser.Centroid (cs:127-143): the arithmetic mean, in vertex order."""
    if polygon is None:
        raise TypeError("polygon is required")
    if not polygon.vertices:
        raise ValueError("Polygon has no vertices.")
    sum_lon = sum_lat = sum_alt = 0.0
    for vertex in polygon.vertices:
        sum_lon += vertex.lon_deg
        sum_lat += vertex.lat_deg
        sum_alt += vertex.altitude_m
    n = len(polygon.vertices)
    return KmlCoordinate(sum_lon / n, sum_lat / n, sum_alt / n)


def project_equirectangular(polygon, anchor=None):
    """ProjectEquirectangular (KmlBoundaryParser.cs:155-177); anchor defaults to the centroid."""
    if polygon is None:
        raise TypeError("polygon is required")
    if anchor is None:
        anchor = centroid(polygon)
    m_per_deg_lon = EARTH_METRES_PER_DEGREE * math.cos(anchor.lat_deg * math.pi / 180.0)
    m_per_deg_lat = EARTH_METRES_PER_DEGREE
    return [KmlProjectedPoint((v.lon_deg - anchor.lon_deg) * m_per_deg_lon,
                              (v.lat_deg - anchor.lat_deg) * m_per_deg_lat)
            for v in polygon.vertices]


# --------------------------------------------------------------------------- #
# The KML DEMOs
# --------------------------------------------------------------------------- #
KML_EXPORT_DEMO_FILE = "leafkmlexport_demo.kml"
KML_IMPORT_DEMO_FILE = "leafkmlimport_demo.csv"
KML_FMT_MANIFEST_FILE = "leafkmlexportfmt_demo.csv"
KML_FMT_FILE_PREFIX = "leafkmlexportfmt_demo_"     # KmlExportFmtDemoCommand.cs:85
KML_IMPORT_HEADER = ("polygon_index,polygon_name,vertex_index,vertex_count,"
                     "lon_deg,lat_deg,centroid_lon,centroid_lat")


def kml_export_demo(polygons, center_lat, center_lon, document_name):
    """LEAFKMLEXPORTDEMO (KmlExportCommand.cs:207-298): WriteAllText, no BOM."""
    return {KML_EXPORT_DEMO_FILE: write_kml_from_projected(
        polygons, center_lat, center_lon, document_name)}


def _csv_escape(text):
    """CsvEscape (KmlExportFmtDemoCommand.cs:383-389), RFC 4180."""
    if not text:
        return ""
    if not any(ch in text for ch in ',"\r\n'):
        return text
    return '"' + text.replace('"', '""') + '"'


def kml_export_fmt_demo(fixtures):
    """LEAFKMLEXPORTFMTDEMO (KmlExportFmtDemoCommand.cs:76-338).

    `fixtures` is the command's ordered list. Each entry is one of
      ("kml", fixture_id, document_name, [KmlPolygon or None])        EmitKml
      ("projected", fixture_id, lat, lon, document_name, [(name, pts)]) EmitKmlProjected
      ("pole", fixture_id, lat, lon, document_name, [(name, pts)])   the F13 guard
    Returns {file name: text} in write order: every fixture file, then the
    manifest (written last, cs:315-316). No file carries a byte order mark.
    """
    files = {}
    manifest = ["Fixture,KmlFile,CenterLat,CenterLon,Error"]
    for fixture in fixtures:
        kind, fixture_id = fixture[0], fixture[1]
        file_name = KML_FMT_FILE_PREFIX + fixture_id + ".xml"
        if kind == "kml":
            _, _, document_name, polygons = fixture
            files[file_name] = write_kml(polygons, document_name)
            manifest.append("%s,%s,,," % (fixture_id, file_name))
        elif kind == "projected":
            _, _, lat, lon, document_name, projected = fixture
            files[file_name] = write_kml_from_projected(projected, lat, lon, document_name)
            manifest.append("%s,%s,%s,%s," % (fixture_id, file_name,
                                               format_roundtrip(lat), format_roundtrip(lon)))
        elif kind == "pole":
            _, _, lat, lon, document_name, projected = fixture
            try:
                write_kml_from_projected(projected, lat, lon, document_name)
                manifest.append(fixture_id + ",,,,NO_EXCEPTION_THROWN")
            except KmlArgumentError as error:
                manifest.append(fixture_id + ",,,," + _csv_escape(str(error)))
        else:
            raise ValueError("unknown fixture kind: " + str(kind))
    files[KML_FMT_MANIFEST_FILE] = "".join(line + CRLF for line in manifest)
    return files


def kml_import_demo_csv(kml_text, kml_path):
    """LEAFKMLIMPORTDEMO (KmlImportDemoCommand.cs:61-110): the per-vertex report.

    `kml_path` is the path the command read; it appears only in the `#` line.
    Encoding.UTF8, so the text starts with the byte order mark (cs:110).
    """
    polygons = parse_kml(kml_text)
    lines = ["# LEAFKMLIMPORTDEMO round-trip re-parse of " + kml_path, KML_IMPORT_HEADER]
    for p, polygon in enumerate(polygons):
        center = centroid(polygon)
        n = len(polygon.vertices)
        name = (polygon.name or "").replace(",", "_")
        for v, vertex in enumerate(polygon.vertices):
            lines.append(",".join((
                str(p), name, str(v), str(n),
                format_fixed(vertex.lon_deg, 8), format_fixed(vertex.lat_deg, 8),
                format_fixed(center.lon_deg, 8), format_fixed(center.lat_deg, 8))))
    return BOM + "".join(line + CRLF for line in lines)


# --------------------------------------------------------------------------- #
# LandXML writing (TerrainExporter.cs:90-264)
# --------------------------------------------------------------------------- #
TRIANGULATION_FIXED = "Fixed"
TRIANGULATION_DELAUNAY = "Delaunay"
TRIANGULATIONS = (TRIANGULATION_FIXED, TRIANGULATION_DELAUNAY)
# The licensed binary's <Surface desc="..."> literal; see the module docstring.
LANDXML_SURFACE_DESC = "Leaf Solar Design terrain import"
_UNSAFE_ATTRIBUTE_CHARS = frozenset('&<>"')


def pick_bl_tr_diagonal(ax, ay, bx, by, cx, cy, dx, dy):
    """PickBlTrDiagonal (TerrainExporter.cs:237-262): the in-circle test, cocircular keeps BL-TR."""
    adx, ady = ax - dx, ay - dy
    bdx, bdy = bx - dx, by - dy
    cdx, cdy = cx - dx, cy - dy
    a2 = adx * adx + ady * ady
    b2 = bdx * bdx + bdy * bdy
    c2 = cdx * cdx + cdy * cdy
    det = (adx * (bdy * c2 - cdy * b2)
           - ady * (bdx * c2 - cdx * b2)
           + a2 * (bdx * cdy - bdy * cdx))
    return det <= 1e-12


def _utc_stamp(utc_now):
    if not isinstance(utc_now, datetime):
        raise TypeError("utc_now must be a datetime (the plugin reads DateTime.UtcNow)")
    if utc_now.tzinfo is not None:
        utc_now = utc_now.astimezone(timezone.utc)
    return utc_now.strftime("%Y-%m-%d"), utc_now.strftime("%H:%M:%S")


def to_landxml(interpolator, surface_name, meters_per_unit, triangulation, utc_now):
    """TerrainExporter.ToLandXml (TerrainExporter.cs:97-199), one AppendLine per line.

    `utc_now` stands in for DateTime.UtcNow (cs:121-123): the clock is an INPUT
    here, so a caller that pins it gets the plugin's bytes. The surface name is
    formatted into the attribute unescaped (cs:125-127), so a name carrying
    & < > or a double quote is refused rather than written as malformed XML.
    """
    if interpolator is None:
        raise TypeError("interpolator is required")
    if not meters_per_unit > 0:
        raise ValueError("metersPerUnit must be > 0.")
    if triangulation not in TRIANGULATIONS:
        raise ValueError("unknown triangulation mode: " + str(triangulation))
    rows, cols = interpolator.rows, interpolator.cols
    if rows < 2 or cols < 2:
        raise ValueError("Terrain grid must have at least 2 rows and 2 columns to triangulate.")
    name = "" if surface_name is None else str(surface_name)
    _check_xml_chars(name)
    if any(ch in _UNSAFE_ATTRIBUTE_CHARS for ch in name):
        raise GeoFormatError("surface name would break the unescaped Surface name attribute")
    date, time = _utc_stamp(utc_now)

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2"',
        '         version="1.2"',
        '         date="%s" time="%s">' % (date, time),
        "  <Units>",
        '    <Metric linearUnit="meter" areaUnit="squareMeter" volumeUnit="cubicMeter"/>',
        "  </Units>",
        "  <Surfaces>",
        '    <Surface name="%s" desc="%s">' % (name, LANDXML_SURFACE_DESC),
        '      <Definition surfType="TIN">',
        "        <Pnts>",
    ]
    # Points: 1-based, row-major, LandXML's N E Z order (cs:132-145).
    point_id = 1
    for x, y, z in interpolator.all_nodes():
        north_m = y * meters_per_unit
        east_m = x * meters_per_unit
        lines.append('          <P id="%d">%s %s %s</P>' % (
            point_id, format_fixed(north_m, 3), format_fixed(east_m, 3), format_fixed(z, 3)))
        point_id += 1
    lines.append("        </Pnts>")

    # Faces: two per cell (cs:147-190). The steps divide AFTER scaling, in C#'s
    # left-to-right order, so the in-circle inputs are bit-identical.
    x_step_m = (interpolator.x_max - interpolator.x_min) * meters_per_unit / max(1, cols - 1)
    y_step_m = (interpolator.y_max - interpolator.y_min) * meters_per_unit / max(1, rows - 1)
    lines.append("        <Faces>")
    for r in range(rows - 1):
        for c in range(cols - 1):
            bl = r * cols + c + 1
            br = r * cols + (c + 1) + 1
            tl = (r + 1) * cols + c + 1
            tr = (r + 1) * cols + (c + 1) + 1
            use_bl_tr = True
            if triangulation == TRIANGULATION_DELAUNAY:
                x_l = c * x_step_m
                x_r = (c + 1) * x_step_m
                y_b = r * y_step_m
                y_t = (r + 1) * y_step_m
                use_bl_tr = pick_bl_tr_diagonal(x_l, y_b, x_r, y_b, x_r, y_t, x_l, y_t)
            if use_bl_tr:
                lines.append("          <F>%d %d %d</F>" % (bl, br, tr))
                lines.append("          <F>%d %d %d</F>" % (bl, tr, tl))
            else:
                lines.append("          <F>%d %d %d</F>" % (bl, br, tl))
                lines.append("          <F>%d %d %d</F>" % (br, tr, tl))
    lines.append("        </Faces>")
    lines.extend(["      </Definition>", "    </Surface>", "  </Surfaces>", "</LandXML>"])
    return "".join(line + CRLF for line in lines)


LANDXML_FIXED_FILE = "leaflandxml_fixed.xml"
LANDXML_DELAUNAY_FILE = "leaflandxml_delaunay.xml"


def sinusoid_grid(rows, cols, x_min, x_max, y_min, y_max, amplitude_m, wavelength_m):
    """LEAFLANDXMLDEMO's grid (LandXmlExportDemoCommand.cs:66-89), row-major."""
    dx = (x_max - x_min) / (cols - 1)
    dy = (y_max - y_min) / (rows - 1)
    k = 2.0 * math.pi / wavelength_m
    elevations = []
    for r in range(rows):
        y = y_min + r * dy
        for c in range(cols):
            x = x_min + c * dx
            elevations.append(amplitude_m * math.sin(k * x) * math.cos(k * y))
    return elevations


def landxml_demo(interpolator, surface_name, utc_now):
    """LEAFLANDXMLDEMO (cs:108-120): Fixed then Delaunay, each with Encoding.UTF8's BOM."""
    return {
        LANDXML_FIXED_FILE: BOM + to_landxml(interpolator, surface_name, 1.0,
                                             TRIANGULATION_FIXED, utc_now),
        LANDXML_DELAUNAY_FILE: BOM + to_landxml(interpolator, surface_name, 1.0,
                                                TRIANGULATION_DELAUNAY, utc_now),
    }


# --------------------------------------------------------------------------- #
# LandXML reading (TerrainImporter.cs:105-143, LandXmlImporter.cs)
# --------------------------------------------------------------------------- #
class SurveyPoint:
    """TerrainImporter.SurveyPoint (cs:30-42): X easting, Y northing, Z elevation."""

    __slots__ = ("x", "y", "z")

    def __init__(self, x, y, z):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def __eq__(self, other):
        return (isinstance(other, SurveyPoint)
                and (self.x, self.y, self.z) == (other.x, other.y, other.z))

    def __hash__(self):
        return hash((self.x, self.y, self.z))

    def __repr__(self):
        return "SurveyPoint(%r, %r, %r)" % (self.x, self.y, self.z)


def parse_landxml_points(xml_content):
    """TerrainImporter.ParseLandXmlPoints (TerrainImporter.cs:105-138).

    Every element whose LOCAL name is "P", in document order, namespace ignored.
    Its text splits on whitespace into N E Z; an empty, short, non-numeric or
    non-finite entry is skipped silently, never fatal.
    """
    if xml_content is None:
        raise TypeError("xmlContent is required")
    root = parse_xml(xml_content)
    points = []
    candidates = [root] + root.descendants()
    # XDocument.Descendants() includes the root element itself.
    for element in candidates:
        if element.local_name != "P":
            continue
        text = element.value()
        if not _net_trim(text):
            continue
        parts = _net_split_white(_net_trim(text))
        if len(parts) < 3:
            continue
        north_m = _net_try_parse_double(parts[0])
        if north_m is None:
            continue
        east_m = _net_try_parse_double(parts[1])
        if east_m is None:
            continue
        elev_m = _net_try_parse_double(parts[2])
        if elev_m is None:
            continue
        if not (math.isfinite(north_m) and math.isfinite(east_m) and math.isfinite(elev_m)):
            continue
        points.append(SurveyPoint(east_m, north_m, elev_m))
    return points


def parse_points(xml_content, northing_is_y=True):
    """LandXmlImporter.ParsePoints (LandXmlImporter.cs:108-128): swap for E N Z files."""
    survey = parse_landxml_points(xml_content)
    if northing_is_y:
        return [SurveyPoint(p.x, p.y, p.z) for p in survey]
    return [SurveyPoint(p.y, p.x, p.z) for p in survey]


class ResampledGrid:
    """LandXmlImporter.ResampledGrid (LandXmlImporter.cs:44-88)."""

    __slots__ = ("elevations", "rows", "cols", "x_min", "x_max", "y_min", "y_max",
                 "source_points")

    def __init__(self, elevations, rows, cols, x_min, x_max, y_min, y_max, source_points):
        self.elevations = tuple(elevations)
        self.rows = rows
        self.cols = cols
        self.x_min = x_min
        self.x_max = x_max
        self.y_min = y_min
        self.y_max = y_max
        self.source_points = source_points


# The IDW pass is O(rows * cols * points); bound it so a hostile file cannot
# turn one import into an unbounded computation.
MAX_RESAMPLE_WORK = 50_000_000


def _net_round(value):
    """C# Math.Round(double): banker's rounding, which Python's round() also is."""
    return int(round(value))


def resample_to_grid(points, target_cells):
    """LandXmlImporter.ResampleToGrid (LandXmlImporter.cs:146-243): IDW, p=2."""
    if points is None:
        raise TypeError("points is required")
    if len(points) < 3:
        raise ValueError("At least 3 points are required to resample a grid.")
    if target_cells < 2:
        raise ValueError("targetCells must be \u2265 2.")
    x_min = y_min = 1.7976931348623157e308
    x_max = y_max = -1.7976931348623157e308
    for p in points:
        if p.x < x_min:
            x_min = p.x
        if p.x > x_max:
            x_max = p.x
        if p.y < y_min:
            y_min = p.y
        if p.y > y_max:
            y_max = p.y
    if x_min >= x_max or y_min >= y_max:
        x_max = x_min + 1.0
        y_max = y_min + 1.0
    x_span = x_max - x_min
    y_span = y_max - y_min
    aspect = x_span / y_span
    if aspect >= 1.0:
        cols = max(2, target_cells)
        rows = max(2, _net_round(target_cells / aspect))
    else:
        rows = max(2, target_cells)
        cols = max(2, _net_round(target_cells * aspect))
    if rows * cols * len(points) > MAX_RESAMPLE_WORK:
        raise ValueError("resample work exceeds %d point-node pairs" % MAX_RESAMPLE_WORK)
    col_step = x_span / (cols - 1) if cols > 1 else x_span
    row_step = y_span / (rows - 1) if rows > 1 else y_span
    elevations = []
    for r in range(rows):
        gy = y_min + r * row_step
        for c in range(cols):
            gx = x_min + c * col_step
            numerator = 0.0
            denominator = 0.0
            exact_z = None
            for p in points:
                dx = p.x - gx
                dy = p.y - gy
                d2 = dx * dx + dy * dy
                if d2 < 1e-12:
                    exact_z = p.z
                    break
                w = 1.0 / d2
                numerator += w * p.z
                denominator += w
            elevations.append(exact_z if exact_z is not None else numerator / denominator)
    return ResampledGrid(elevations, rows, cols, x_min, x_max, y_min, y_max, len(points))


LANDXML_IMPORT_DEMO_FILE = "leafimportlandxml_demo.csv"


def landxml_import_demo_csv(fixtures):
    """LEAFIMPORTLANDXMLDEMO (LandXmlImportDemoCommand.cs:163-196): R-format rows, no BOM.

    `fixtures` is [(fixture_id, xml_text)] in the command's order.
    """
    lines = ["Fixture,PointIndex,X,Y,Z"]
    for fixture_id, xml_text in fixtures:
        for index, point in enumerate(parse_landxml_points(xml_text)):
            lines.append(",".join((fixture_id, str(index), format_roundtrip(point.x),
                                   format_roundtrip(point.y), format_roundtrip(point.z))))
    return "".join(line + CRLF for line in lines)


# --------------------------------------------------------------------------- #
# Structural counts, shared with scripts/solar_probe_evidence.py
# --------------------------------------------------------------------------- #
def count_local_names(root, local_name):
    """How many elements at or below `root` carry `local_name`."""
    return sum(1 for element in [root] + root.descendants() if element.local_name == local_name)
