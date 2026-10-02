"""The exact footprint of a converted Ground tracker, kept on its frame.

A converted Ground frame stores the four drawing-unit corners the tracker layout emitted at
frame["extra"]["ground_outline"] = {"point_units": "drawing", "points": [[x, y], [x, y], [x, y], [x, y]]},
in the layout's own order: axis start minus the half cross-axis vector, axis start plus it, axis end plus it,
axis end minus it. The graph validator admits frame.extra as free JSON and checks no geometry, so every
reader goes through frame_outline, which fails closed on anything it cannot trust. A frame written before
this record carries no outline and is refused: nothing here reconstructs a polygon.

Pure: no I/O, no clock, no input mutated. O(1) per frame, no allocation beyond the returned tuple."""
from __future__ import annotations

import math

OUTLINE_KEY = "ground_outline"
POINT_UNITS = "drawing"
ENVELOPE_KEYS = frozenset({"point_units", "points"})
# An axis point is within the plan coordinate bound (1e9) and a half cross-axis vector within 1e6 drawing units.
MAX_OUTLINE_COORDINATE = 2_000_000_000.0
# Positional tolerance, in ulps of the largest coordinate involved (a length in drawing units).
OUTLINE_ULPS = 16.0
# An edge must exceed this many tolerances, else the footprint is not resolvable at its coordinates. With the
# squareness allowance below, every accepted footprint is square to within 1/64 in cosine (under one degree).
OUTLINE_MIN_EDGE_TOLERANCES = 64.0
# Relative bound on the cosine between the cross edge and the axis edge: the footprint is a rectangle.
OUTLINE_SQUARE = 1e-9
# Corner rounding the squareness test forgives: this many tolerances times the sum of the two edge lengths. The
# layout's own arithmetic stays under 0.08 (measured over 3,300 converted frames); 2.0 admitted a 63 degree
# parallelogram with edges five tolerances long.
OUTLINE_SQUARE_TOLERANCES = 0.5

MISSING = "GROUND_OUTLINE_MISSING"
ENVELOPE_INVALID = "GROUND_OUTLINE_ENVELOPE_INVALID"
POINTS_INVALID = "GROUND_OUTLINE_POINTS_INVALID"
CODES = frozenset({MISSING, ENVELOPE_INVALID, POINTS_INVALID})
ENVELOPE_PATH = "extra.ground_outline"
POINTS_PATH = "extra.ground_outline.points"


class GroundOutlineError(ValueError):
    """A frame's stored outline is absent or cannot be trusted. `code` is one of CODES and `path` names the
    offending member relative to the frame."""

    def __init__(self, code, path):
        super().__init__(code)
        self.code = code
        self.path = path


def outline_record(outline):
    """The envelope stored on a converted frame for a layout tracker's `outline`: a fresh copy of its four
    [x, y] corners, numbers unchanged."""
    return {"point_units": POINT_UNITS, "points": [[point[0], point[1]] for point in outline]}


def _coordinate(value):
    """One stored coordinate, fails closed: a real number inside the bound, never a boolean."""
    if type(value) is int:
        if -MAX_OUTLINE_COORDINATE <= value <= MAX_OUTLINE_COORDINATE:
            return value
    elif type(value) is float and math.isfinite(value) and abs(value) <= MAX_OUTLINE_COORDINATE:
        return value
    raise GroundOutlineError(POINTS_INVALID, POINTS_PATH)


def _pair(value):
    if type(value) is not list or len(value) != 2:
        raise GroundOutlineError(POINTS_INVALID, POINTS_PATH)
    return (_coordinate(value[0]), _coordinate(value[1]))


def frame_outline(frame):
    """The four (x, y) corners of a converted Ground frame, in drawing units and in the stored order.

    Refusals (GroundOutlineError): MISSING when the frame carries no outline; ENVELOPE_INVALID when the
    envelope is not exactly {"point_units": "drawing", "points": ...}; POINTS_INVALID when the points are
    not four in-bound [x, y] pairs, when an edge is too short for its coordinates to resolve, when the
    footprint is not a rectangle (the cross edge is not square to the axis edge, or the two cross edges
    differ), or when the cross edges are not centred on the frame's own tracker axis. Every positional
    comparison is a length in drawing units: OUTLINE_ULPS ulps of the largest coordinate involved."""
    extra = frame.get("extra") if type(frame) is dict else None
    if type(extra) is not dict or OUTLINE_KEY not in extra:
        raise GroundOutlineError(MISSING, ENVELOPE_PATH)
    envelope = extra[OUTLINE_KEY]
    if (type(envelope) is not dict or envelope.keys() != ENVELOPE_KEYS
            or type(envelope["point_units"]) is not str or envelope["point_units"] != POINT_UNITS):
        raise GroundOutlineError(ENVELOPE_INVALID, ENVELOPE_PATH)
    points = envelope["points"]
    if type(points) is not list or len(points) != 4:
        raise GroundOutlineError(POINTS_INVALID, POINTS_PATH)
    p0, p1, p2, p3 = _pair(points[0]), _pair(points[1]), _pair(points[2]), _pair(points[3])
    tracker = frame.get("tracker")
    if type(tracker) is not dict:
        raise GroundOutlineError(POINTS_INVALID, POINTS_PATH)
    try:
        start, end = _pair(tracker.get("axis_start")), _pair(tracker.get("axis_end"))
    except GroundOutlineError:
        raise GroundOutlineError(POINTS_INVALID, POINTS_PATH) from None
    scale = max(1.0, *(abs(v) for point in (p0, p1, p2, p3, start, end) for v in point))
    # A length in drawing units: the layout adds a half vector to an axis point, so a corner is exact to a few
    # ulps of the largest coordinate. Never a fraction of the coordinate itself (that outgrows the footprint).
    tolerance = OUTLINE_ULPS * math.ulp(scale)
    cross = (p1[0] - p0[0], p1[1] - p0[1])          # the cross-axis edge at the axis start
    far = (p2[0] - p3[0], p2[1] - p3[1])            # the same edge at the axis end
    along = (p3[0] - p0[0], p3[1] - p0[1])          # the edge along the axis
    cross_length, along_length = math.hypot(*cross), math.hypot(*along)
    floor = OUTLINE_MIN_EDGE_TOLERANCES * tolerance  # an edge the coordinates cannot resolve is no footprint
    if (cross_length <= floor or along_length <= floor
            or abs(cross[0] * along[0] + cross[1] * along[1])
            > OUTLINE_SQUARE * cross_length * along_length
            + OUTLINE_SQUARE_TOLERANCES * tolerance * (cross_length + along_length)
            or abs(cross[0] - far[0]) > tolerance or abs(cross[1] - far[1]) > tolerance
            or abs((p0[0] + p1[0]) * 0.5 - start[0]) > tolerance
            or abs((p0[1] + p1[1]) * 0.5 - start[1]) > tolerance
            or abs((p3[0] + p2[0]) * 0.5 - end[0]) > tolerance
            or abs((p3[1] + p2[1]) * 0.5 - end[1]) > tolerance):
        raise GroundOutlineError(POINTS_INVALID, POINTS_PATH)
    return (p0, p1, p2, p3)
