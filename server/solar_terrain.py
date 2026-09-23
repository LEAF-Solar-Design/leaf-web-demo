"""Studio's literal ports of six licensed terrain and irradiance engines, byte-exact.

Six production capabilities are pure calculations that a LEAF*DEMO command
exposes by running a fixed scenario list and writing a CSV, with no drawing
state. This module is the CALCULATION half; scripts/solar_terrain_probes.py
holds Studio's copies of the scenario lists and writes the files.

Ported 2026-09-23 from C:/tmp/solar-parity/wt-b25-s17 (Branch2025), read-only:

  shared grid
      Terrain/TerrainGridInterpolator.cs:50-236 (constructors, InterpolateZ,
      AllNodes). Every engine below that samples terrain reads it through this.
  cut-fill-heatmap
      Terrain/CutFillHeatmapCalculator.cs:61-137 (ComputeCells, ComputeHeatRgb,
      MaxAbsDeltaM), rendered as Terrain/HeatmapDemoCommand.cs:212-248.
  horizon-profile
      Terrain/HorizonProfileCalculator.cs:54-148 (Generate, IsSunBelowHorizon),
      rendered as Terrain/HorizonProfileCommand.cs:448-465 (WriteHorizonCsv).
  plane-of-array-irradiance
      Terrain/InPlaneIrradianceCalculator.cs:88-452 (ComputeIsotropic,
      ComputeHdkr, ComputePerez with the Perez 1990 all-sites-composite
      coefficient table and clearness bins), rendered as
      Terrain/InPlaneIrradianceDemoCommand.cs:193-207.
  grading-pad-design
      Terrain/GradingPadCalculator.cs:32-185 (PadRectangle,
      ComputeEmbankmentToes, ComputeEmbankmentToesPerEdge), rendered as
      Terrain/GradingPadDemoCommand.cs:136-164.
  terrain-cross-section
      Terrain/TerrainProfileCalculator.cs:54-154 (SampleLine, SamplePolyline),
      rendered as Terrain/CrossSectionDemoCommand.cs:136-169.
  capacity-iteration-sweep
      Terrain/CapacityIterationCalculator.cs:33-197 (CapacityRange, Enumerate,
      ShoelaceArea) over Terrain/TrackerRowGenerator.cs:10-165 and 341-660
      (TrackerModuleSpec, Generate, ClipColumnToPolygon), rendered as
      Terrain/CapacityIterationDemoCommand.cs:89-105. LEAFCAPACITYITERATEDEMO is
      the command Pvcase/PvcaseRibbon.cs:214 binds to its "Capacity iteration"
      button.

Byte-exactness is the contract. The rules that carry it:

  * Every number is rendered through the C# formatter the plugin names, reusing
    server/solar_probe_calcs.py (and through it server/solar_nec.py) so each
    formatter and its tie rule has ONE implementation on this side: `F<n>` is
    `format_fixed` (away-from-zero ties over the double's exact value, and a
    negative value that rounds to zero keeps its sign, "-0.000000", as .NET
    Core 3.0+ prints it), `R` is `format_roundtrip`, an int is `format_int`.
  * Arithmetic follows the C# expression order literally, operand for operand,
    because a reassociated sum is a different double. Where C# calls
    Math.Round(double) the port uses Python's round(), which is the same
    banker's rounding over the same binary value; Math.Max and Math.Min go
    through `_net_max` and `_net_min`, which keep .NET's NaN and signed-zero
    answers where Python's builtins would return their first argument.
  * Each file's encoding and preamble are the plugin's: HeatmapDemoCommand
    writes Encoding.ASCII, InPlaneIrradianceDemoCommand UTF-8 with NO byte
    order mark, and the other four `Encoding.UTF8`, whose preamble is a byte
    order mark. Every line ends in CRLF (StringBuilder.AppendLine on Windows),
    including the last one. No field is quoted.
  * Three commands format with the CURRENT culture rather than the invariant
    one (WriteHorizonCsv's interpolated strings). The licensed capture ran
    under a dot-decimal culture, so the invariant rendering here is what the
    plugin wrote; a comma-decimal host would make the plugin's own file
    unreadable, which is a plugin defect this port does not reproduce.

Deliberate scope: the demos that seed LEAFTOPO through PersistElevationGrid and
read it back through TrackerCommand.TryReadTerrainInterpolator
(TrackerCommand.cs:275-333) get the same doubles and the same axis-aligned
frame the in-memory constructor builds, so the port constructs the grid
directly. TrackerRow's TrackerPack is not built, because nothing the capacity
sweep reports reads it.

No AutoCAD, no network, no dependencies outside the standard library.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

CRLF = "\r\n"
# Encoding.UTF8's preamble. File.WriteAllText(path, text, Encoding.UTF8) writes
# it; `new UTF8Encoding(false)` and Encoding.ASCII do not.
UTF8_BOM = "\ufeff"


def _load_probe_calcs():
    """Load server/solar_probe_calcs.py by path so the import works from any cwd.

    It already carries the C# F, R and int formatters over solar_nec.py's tie
    rule; re-deriving them here is how two renderings of one double drift apart.
    """
    path = Path(__file__).resolve().with_name("solar_probe_calcs.py")
    spec = importlib.util.spec_from_file_location("solar_probe_calcs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


calcs = _load_probe_calcs()
format_fixed = calcs.format_fixed
format_roundtrip = calcs.format_roundtrip
format_int = calcs.format_int


def _net_max(a, b):
    """C# Math.Max(double, double) on .NET Core 3.0+: NaN wins, +0 beats -0."""
    if math.isnan(a) or math.isnan(b):
        return math.nan
    if a == b == 0.0:
        return a if math.copysign(1.0, a) > 0 else b
    return a if a > b else b


def _net_min(a, b):
    """C# Math.Min(double, double) on .NET Core 3.0+: NaN wins, -0 beats +0."""
    if math.isnan(a) or math.isnan(b):
        return math.nan
    if a == b == 0.0:
        return a if math.copysign(1.0, a) < 0 else b
    return a if a < b else b


def _lines(lines, preamble=""):
    """AppendLine per line: CRLF after every line, the last one included."""
    return preamble + "".join(line + CRLF for line in lines)


# --------------------------------------------------------------------------- #
# TerrainGridInterpolator (TerrainGridInterpolator.cs:50-236)
# --------------------------------------------------------------------------- #
class TerrainGridInterpolator:
    """The LEAFTOPO elevation grid with bilinear Z, row-major, rows north.

    `elevations[row * cols + col]`; column 0 sits at x_min and row 0 at y_min.
    The default frame is the axis-aligned one the 8-argument constructor builds
    (cs:50-68): origin (x_min, y_min), X axis (x_max - x_min, 0), Y axis
    (0, y_max - y_min). `frame` supplies the explicit affine one (cs:74-104) as
    (origin_x, origin_y, x_axis_x, x_axis_y, y_axis_x, y_axis_y).
    """

    __slots__ = ("elevations", "rows", "cols", "x_min", "x_max", "y_min", "y_max",
                 "meters_per_unit", "origin_x", "origin_y", "x_axis_x", "x_axis_y",
                 "y_axis_x", "y_axis_y")

    def __init__(self, elevations, rows, cols, x_min, x_max, y_min, y_max,
                 meters_per_unit=1.0, frame=None):
        if elevations is None:
            raise TypeError("elevations is required")
        self.elevations = tuple(float(value) for value in elevations)
        self.rows = int(rows)
        self.cols = int(cols)
        self.x_min = float(x_min)
        self.x_max = float(x_max)
        self.y_min = float(y_min)
        self.y_max = float(y_max)
        # cs:94: a nonpositive scale falls back to 1.0 rather than failing.
        self.meters_per_unit = float(meters_per_unit) if meters_per_unit > 0 else 1.0
        if frame is None:
            frame = (self.x_min, self.y_min, self.x_max - self.x_min, 0.0,
                     0.0, self.y_max - self.y_min)
        (self.origin_x, self.origin_y, self.x_axis_x, self.x_axis_y,
         self.y_axis_x, self.y_axis_y) = (float(value) for value in frame)

    def _at(self, index):
        # C# indexes a double[]: a negative index throws, it never wraps.
        if index < 0 or index >= len(self.elevations):
            raise IndexError("elevation index out of range: %d" % index)
        return self.elevations[index]

    def interpolate_z(self, drawing_x, drawing_y):
        """Bilinear Z at a drawing point, or None outside the grid (cs:151-185)."""
        if self.rows < 1 or self.cols < 1:
            return None
        det = self.x_axis_x * self.y_axis_y - self.x_axis_y * self.y_axis_x
        if abs(det) <= 1e-12:
            return None
        dx = drawing_x - self.origin_x
        dy = drawing_y - self.origin_y
        u = (dx * self.y_axis_y - dy * self.y_axis_x) / det
        v = (self.x_axis_x * dy - self.x_axis_y * dx) / det
        frac_x = u * (self.cols - 1)
        frac_y = v * (self.rows - 1)
        if frac_x < 0 or frac_x > self.cols - 1 or frac_y < 0 or frac_y > self.rows - 1:
            return None
        # (int) truncates toward zero; both fractions are nonnegative here.
        col_left = min(int(frac_x), self.cols - 2)
        row_bot = min(int(frac_y), self.rows - 2)
        col_right = col_left + 1
        row_top = row_bot + 1
        tx = frac_x - col_left
        ty = frac_y - row_bot
        e_bl = self._at(row_bot * self.cols + col_left)
        e_br = self._at(row_bot * self.cols + col_right)
        e_tl = self._at(row_top * self.cols + col_left)
        e_tr = self._at(row_top * self.cols + col_right)
        return ((1 - ty) * ((1 - tx) * e_bl + tx * e_br)
                + ty * ((1 - tx) * e_tl + tx * e_tr))

    def all_nodes(self):
        """(x, y, elevation) per node, row by row (cs:224-235)."""
        for r in range(self.rows):
            v = r / max(1, self.rows - 1)
            for c in range(self.cols):
                u = c / max(1, self.cols - 1)
                x = self.origin_x + self.x_axis_x * u + self.y_axis_x * v
                y = self.origin_y + self.x_axis_y * u + self.y_axis_y * v
                yield x, y, self.elevations[r * self.cols + c]


# --------------------------------------------------------------------------- #
# cut-fill-heatmap (CutFillHeatmapCalculator.cs)
# --------------------------------------------------------------------------- #
NEUTRAL_BAND_M = 0.05   # cs:98, |delta| strictly below this is at-grade grey
MAX_DEPTH_M = 3.0       # cs:99, the ramp saturates here


class CutFillCell:
    __slots__ = ("center_x", "center_y", "half_width_du", "half_height_du", "delta_m")

    def __init__(self, center_x, center_y, half_width_du, half_height_du, delta_m):
        self.center_x = center_x
        self.center_y = center_y
        self.half_width_du = half_width_du
        self.half_height_du = half_height_du
        self.delta_m = delta_m


def compute_cells(interpolator, proposed_elev_m):
    """One cell per grid node, delta = existing - proposed (cs:69-93)."""
    if interpolator is None:
        raise TypeError("interpolator is required")
    x_span = interpolator.x_max - interpolator.x_min
    y_span = interpolator.y_max - interpolator.y_min
    half_w = (x_span / (interpolator.cols - 1) * 0.5 if interpolator.cols > 1
              else x_span * 0.5)
    half_h = (y_span / (interpolator.rows - 1) * 0.5 if interpolator.rows > 1
              else y_span * 0.5)
    return [CutFillCell(x, y, half_w, half_h, elev_m - proposed_elev_m)
            for x, y, elev_m in interpolator.all_nodes()]


def compute_heat_rgb(delta_m):
    """The colour ramp (cs:112-123): grey band, then a banker's-rounded fade."""
    if abs(delta_m) < NEUTRAL_BAND_M:
        return (210, 210, 210)
    t = _net_min(abs(delta_m) / MAX_DEPTH_M, 1.0)
    # (byte)Math.Round: MidpointRounding.ToEven, the rule Python's round() uses.
    fade = round(210 * (1.0 - t)) & 0xFF
    if delta_m > 0:
        return (255, fade, fade)
    return (fade, 255, fade)


def max_abs_delta_m(cells):
    """cs:129-137."""
    best = 0.0
    for cell in cells:
        value = abs(cell.delta_m)
        if value > best:
            best = value
    return best


def heatmap_bucket(delta_m):
    """HeatmapDemoCommand.BucketFor (cs:257-262), the same strict boundary."""
    if abs(delta_m) < NEUTRAL_BAND_M:
        return "NEUTRAL"
    return "CUT" if delta_m > 0 else "FILL"


HEATMAP_HEADER = ("Fixture,CellIndex,Rows,Cols,CenterX,CenterY,HalfWidthDu,"
                  "HalfHeightDu,DeltaM,R,G,B,Bucket")


def heatmap_demo_csv(fixtures):
    """leafheatmap_demo.csv: `fixtures` is (name, rows, cols, cells) per fixture.

    HeatmapDemoCommand.EmitCells (cs:212-248), ASCII, every double as "R".
    """
    lines = [HEATMAP_HEADER]
    for name, rows, cols, cells in fixtures:
        for index, cell in enumerate(cells):
            r, g, b = compute_heat_rgb(cell.delta_m)
            lines.append(",".join((
                name, format_int(index), format_int(rows), format_int(cols),
                format_roundtrip(cell.center_x), format_roundtrip(cell.center_y),
                format_roundtrip(cell.half_width_du), format_roundtrip(cell.half_height_du),
                format_roundtrip(cell.delta_m), format_int(r), format_int(g),
                format_int(b), heatmap_bucket(cell.delta_m))))
    return _lines(lines)


# --------------------------------------------------------------------------- #
# horizon-profile (HorizonProfileCalculator.cs)
# --------------------------------------------------------------------------- #
DEG_TO_RAD = math.pi / 180.0
RAD_TO_DEG = 180.0 / math.pi


class SiteOutsideTerrainError(ValueError):
    """The C# InvalidOperationException for a site centre off the grid (cs:70-72)."""


class HorizonSample:
    __slots__ = ("azimuth_deg", "elevation_deg")

    def __init__(self, azimuth_deg, elevation_deg):
        self.azimuth_deg = azimuth_deg
        self.elevation_deg = elevation_deg


def generate_horizon_profile(terrain, site_x, site_y, observer_height_m=1.5,
                             azimuth_step_deg=1.0, ray_step_du=1.0, max_ray_du=math.nan):
    """360 degree horizon, highest elevation angle per azimuth (cs:54-118).

    Work is bounded: round(360 / step) azimuths, each marching at most
    max_ray_du / ray_step_du samples and stopping when the ray leaves the grid.
    """
    if terrain is None:
        raise TypeError("terrain is required")
    if azimuth_step_deg <= 0.0:
        raise ValueError("azimuthStepDeg must be > 0.")
    if ray_step_du <= 0.0:
        raise ValueError("rayStepDu must be > 0.")
    site_z = terrain.interpolate_z(site_x, site_y)
    if site_z is None:
        raise SiteOutsideTerrainError(
            "Site centre (%s, %s) lies outside terrain grid." % (site_x, site_y))
    observer_z = site_z + observer_height_m
    if math.isnan(max_ray_du):
        dx = terrain.x_max - terrain.x_min
        dy = terrain.y_max - terrain.y_min
        max_ray_du = math.sqrt(dx * dx + dy * dy)
    mpu = terrain.meters_per_unit
    # (int)Math.Round: banker's rounding, which Python's round() also uses.
    n_az = int(round(360.0 / azimuth_step_deg))
    samples = []
    for i in range(n_az):
        az_deg = i * azimuth_step_deg
        az_rad = az_deg * DEG_TO_RAD
        dir_x = math.sin(az_rad)
        dir_y = math.cos(az_rad)
        max_elev_deg = 0.0
        s = ray_step_du
        while s <= max_ray_du:
            px = site_x + dir_x * s
            py = site_y + dir_y * s
            z = terrain.interpolate_z(px, py)
            if z is None:
                break
            delta_z = z - observer_z
            horiz_m = s * mpu
            if horiz_m > 0.0:
                elev = math.atan2(delta_z, horiz_m) * RAD_TO_DEG
                if elev > max_elev_deg:
                    max_elev_deg = elev
            s += ray_step_du
        samples.append(HorizonSample(az_deg, max_elev_deg))
    return samples


def is_sun_below_horizon(profile, sun_azimuth_deg, sun_elevation_deg):
    """cs:125-148; C#'s double % is fmod, the sign of the dividend."""
    if not profile:
        return False
    az = math.fmod(math.fmod(sun_azimuth_deg, 360.0) + 360.0, 360.0)
    n = len(profile)
    step_deg = 360.0 / n
    frac_index = az / step_deg
    i0 = int(math.floor(frac_index)) % n
    i1 = (i0 + 1) % n
    t = frac_index - math.floor(frac_index)
    horizon = (1.0 - t) * profile[i0].elevation_deg + t * profile[i1].elevation_deg
    return sun_elevation_deg < horizon


HORIZON_HEADER = "azimuth_deg_cw_from_N,horizon_elevation_deg"


def horizon_csv(profile, cx, cy):
    """WriteHorizonCsv (HorizonProfileCommand.cs:448-465), Encoding.UTF8 with BOM."""
    lines = ["# LEAFHORIZON far-shading horizon profile",
             "# Site centre: (%s, %s)" % (format_fixed(cx, 4), format_fixed(cy, 4)),
             HORIZON_HEADER]
    for sample in profile:
        lines.append("%s,%s" % (format_fixed(sample.azimuth_deg, 3),
                                format_fixed(sample.elevation_deg, 6)))
    return _lines(lines, UTF8_BOM)


# --------------------------------------------------------------------------- #
# plane-of-array-irradiance (InPlaneIrradianceCalculator.cs)
# --------------------------------------------------------------------------- #
SOLAR_CONSTANT_WM2 = 1367.0                          # cs:167
PEREZ_KAPPA = 1.041                                  # cs:303
PEREZ_B_MIN = math.cos(85.0 * math.pi / 180.0)       # cs:307-308
# Perez 1990 "all sites composite" (cs:316-326), rows are clearness bins 1..8,
# columns F11, F12, F13, F21, F22, F23.
PEREZ_COEFFICIENTS_1990 = (
    (-0.008, 0.588, -0.062, -0.060, 0.072, -0.022),
    (0.130, 0.683, -0.151, -0.019, 0.066, -0.029),
    (0.330, 0.487, -0.221, 0.055, -0.064, -0.026),
    (0.568, 0.187, -0.295, 0.109, -0.152, -0.014),
    (0.873, -0.392, -0.362, 0.226, -0.462, 0.001),
    (1.132, -1.237, -0.412, 0.288, -0.823, 0.056),
    (1.060, -1.600, -0.359, 0.264, -1.127, 0.131),
    (0.678, -0.327, -0.250, 0.156, -1.377, 0.251),
)


class PoaComponents:
    """cs:11-24. `global_irradiance` is Direct + SkyDiffuse + GroundDiffuse."""

    __slots__ = ("direct", "sky_diffuse", "ground_diffuse")

    def __init__(self, direct, sky_diffuse, ground_diffuse):
        self.direct = direct
        self.sky_diffuse = sky_diffuse
        self.ground_diffuse = ground_diffuse

    @property
    def global_irradiance(self):
        return self.direct + self.sky_diffuse + self.ground_diffuse


def _check_day_of_year(day_of_year):
    if isinstance(day_of_year, bool) or not isinstance(day_of_year, int):
        raise TypeError("dayOfYear must be an int")
    if day_of_year < 1 or day_of_year > 366:
        raise ValueError("dayOfYear must be in [1, 366].")


def compute_isotropic(dni, dhi, ghi, solar_zenith_deg, solar_azimuth_deg,
                      tilt_deg, surface_azimuth_deg, albedo=0.2):
    """Liu-Jordan isotropic transposition (cs:88-141)."""
    if dni < 0.0:
        raise ValueError("dni must be >= 0.")
    if dhi < 0.0:
        raise ValueError("dhi must be >= 0.")
    if ghi < 0.0:
        raise ValueError("ghi must be >= 0.")
    if tilt_deg < 0.0 or tilt_deg > 180.0:
        raise ValueError("tiltDeg must be in [0, 180].")
    if albedo < 0.0 or albedo > 1.0:
        raise ValueError("albedo must be in [0, 1].")
    beta = tilt_deg * math.pi / 180.0
    theta_z = solar_zenith_deg * math.pi / 180.0
    gamma = surface_azimuth_deg * math.pi / 180.0
    gamma_s = solar_azimuth_deg * math.pi / 180.0
    cos_beta = math.cos(beta)
    sin_beta = math.sin(beta)
    cos_z = math.cos(theta_z)
    sin_z = math.sin(theta_z)
    cos_aoi = cos_z * cos_beta + sin_z * sin_beta * math.cos(gamma_s - gamma)
    poa_direct = dni * _net_max(0.0, cos_aoi)
    sky_view = 0.5 * (1.0 + cos_beta)
    gnd_view = 0.5 * (1.0 - cos_beta)
    if sky_view < 0.0 and sky_view > -1e-15:
        sky_view = 0.0
    if gnd_view < 0.0 and gnd_view > -1e-15:
        gnd_view = 0.0
    return PoaComponents(poa_direct, dhi * sky_view, albedo * ghi * gnd_view)


def compute_hdkr(dni, dhi, ghi, solar_zenith_deg, solar_azimuth_deg,
                 tilt_deg, surface_azimuth_deg, day_of_year, albedo=0.2):
    """Hay-Davies-Klucher-Reindl sky diffuse (cs:177-258)."""
    _check_day_of_year(day_of_year)
    iso = compute_isotropic(dni, dhi, ghi, solar_zenith_deg, solar_azimuth_deg,
                            tilt_deg, surface_azimuth_deg, albedo)
    beta = tilt_deg * math.pi / 180.0
    theta_z = solar_zenith_deg * math.pi / 180.0
    cos_z = math.cos(theta_z)
    if cos_z <= 1e-9 or ghi <= 1e-12 or dhi <= 0.0:
        return iso
    eccentricity = 1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0)
    i_on = SOLAR_CONSTANT_WM2 * eccentricity
    anisotropy = dni / i_on
    if anisotropy < 0.0:
        anisotropy = 0.0
    if anisotropy > 1.0:
        anisotropy = 1.0
    beam_horizontal = dni * cos_z
    f = 0.0
    if ghi > 0.0:
        ratio = beam_horizontal / ghi
        if ratio < 0.0:
            ratio = 0.0
        if ratio > 1.0:
            ratio = 1.0
        f = math.sqrt(ratio)
    cos_aoi = iso.direct / dni if dni > 0.0 else 0.0
    r_b = _net_max(0.0, cos_aoi / cos_z)
    half_sin_half_beta_cubed = math.pow(math.sin(beta / 2.0), 3.0)
    isotropic_share = ((1.0 - anisotropy) * 0.5 * (1.0 + math.cos(beta))
                       * (1.0 + f * half_sin_half_beta_cubed))
    circumsolar_share = anisotropy * r_b
    sky = dhi * (isotropic_share + circumsolar_share)
    if sky < 0.0 and sky > -1e-12:
        sky = 0.0
    return PoaComponents(iso.direct, sky, iso.ground_diffuse)


def perez_bin(epsilon):
    """Perez 1990 Table 1 clearness bins 1..8 (cs:330-340)."""
    for bin_number, upper in enumerate((1.065, 1.230, 1.500, 1.950, 2.800, 4.500, 6.200),
                                       start=1):
        if epsilon < upper:
            return bin_number
    return 8


def compute_perez(dni, dhi, ghi, solar_zenith_deg, solar_azimuth_deg,
                  tilt_deg, surface_azimuth_deg, day_of_year, albedo=0.2):
    """Perez 1990 anisotropic sky diffuse (cs:350-452)."""
    _check_day_of_year(day_of_year)
    iso = compute_isotropic(dni, dhi, ghi, solar_zenith_deg, solar_azimuth_deg,
                            tilt_deg, surface_azimuth_deg, albedo)
    if dhi <= 0.0:
        return PoaComponents(iso.direct, 0.0, iso.ground_diffuse)
    beta = tilt_deg * math.pi / 180.0
    theta_z = solar_zenith_deg * math.pi / 180.0
    cos_z = math.cos(theta_z)
    sin_beta = math.sin(beta)
    cos_beta = math.cos(beta)
    if cos_z <= 1e-9:
        return iso
    sin_z = math.sin(theta_z)
    gamma_rad = surface_azimuth_deg * math.pi / 180.0
    gamma_s_rad = solar_azimuth_deg * math.pi / 180.0
    cos_aoi = cos_z * cos_beta + sin_z * sin_beta * math.cos(gamma_s_rad - gamma_rad)
    z_deg_clamped = _net_max(0.0, _net_min(90.0, solar_zenith_deg))
    air_mass = 1.0 / (cos_z + 0.50572 * math.pow(96.07995 - z_deg_clamped, -1.6364))
    i_on = SOLAR_CONSTANT_WM2 * (1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0))
    delta = dhi * air_mass / i_on
    z3 = theta_z * theta_z * theta_z
    epsilon = ((dhi + dni) / dhi + PEREZ_KAPPA * z3) / (1.0 + PEREZ_KAPPA * z3)
    c = PEREZ_COEFFICIENTS_1990[perez_bin(epsilon) - 1]
    f1 = _net_max(0.0, c[0] + c[1] * delta + c[2] * theta_z)
    f2 = c[3] + c[4] * delta + c[5] * theta_z
    a = _net_max(0.0, cos_aoi)
    b = _net_max(PEREZ_B_MIN, cos_z)
    isotropic_share = (1.0 - f1) * 0.5 * (1.0 + cos_beta)
    circumsolar_share = f1 * (a / b)
    horizon_share = f2 * sin_beta
    sky = dhi * (isotropic_share + circumsolar_share + horizon_share)
    if sky < 0.0 and sky > -1e-12:
        sky = 0.0
    return PoaComponents(iso.direct, sky, iso.ground_diffuse)


POA_HEADER = "Fixture,Model,Direct,SkyDiffuse,GroundDiffuse,Global"


def poa_demo_csv(rows):
    """leafpoa_demo.csv: `rows` is (fixture, model, PoaComponents) in file order.

    InPlaneIrradianceDemoCommand.EmitRow (cs:193-207), UTF-8 with no BOM,
    every component as "R".
    """
    lines = [POA_HEADER]
    for fixture, model, poa in rows:
        lines.append(",".join((fixture, model, format_roundtrip(poa.direct),
                               format_roundtrip(poa.sky_diffuse),
                               format_roundtrip(poa.ground_diffuse),
                               format_roundtrip(poa.global_irradiance))))
    return _lines(lines)


# --------------------------------------------------------------------------- #
# grading-pad-design (GradingPadCalculator.cs)
# --------------------------------------------------------------------------- #
class PadRectangle:
    """cs:32-55. Fails closed on an empty or inverted rectangle."""

    __slots__ = ("min_x", "min_y", "max_x", "max_y")

    def __init__(self, min_x, min_y, max_x, max_y):
        if max_x <= min_x:
            raise ValueError("MaxX %s must be > MinX %s." % (max_x, min_x))
        if max_y <= min_y:
            raise ValueError("MaxY %s must be > MinY %s." % (max_y, min_y))
        self.min_x = float(min_x)
        self.min_y = float(min_y)
        self.max_x = float(max_x)
        self.max_y = float(max_y)

    def corners(self):
        """CCW from SW: SW, SE, NE, NW."""
        return ((self.min_x, self.min_y), (self.max_x, self.min_y),
                (self.max_x, self.max_y), (self.min_x, self.max_y))


class EmbankmentToes:
    __slots__ = ("horizontal_run_m", "height_delta_m", "slope_ratio_h", "corners_du")

    def __init__(self, horizontal_run_m, height_delta_m, slope_ratio_h, corners_du):
        self.horizontal_run_m = horizontal_run_m
        self.height_delta_m = height_delta_m
        self.slope_ratio_h = slope_ratio_h
        self.corners_du = tuple(corners_du)


def compute_embankment_toes(pad, pad_elev_m, reference_terrain_m, slope_ratio_h,
                            meters_per_unit=1.0):
    """Uniform reference terrain: the pad inflated by one run (cs:96-127)."""
    if slope_ratio_h < 0.0:
        raise ValueError("slopeRatioH must be >= 0, got %s." % slope_ratio_h)
    if meters_per_unit <= 0.0:
        raise ValueError("metersPerUnit must be > 0, got %s." % meters_per_unit)
    height_m = pad_elev_m - reference_terrain_m
    run_m = abs(height_m) * slope_ratio_h
    run_du = run_m / meters_per_unit
    return EmbankmentToes(run_m, height_m, slope_ratio_h, (
        (pad.min_x - run_du, pad.min_y - run_du),
        (pad.max_x + run_du, pad.min_y - run_du),
        (pad.max_x + run_du, pad.max_y + run_du),
        (pad.min_x - run_du, pad.max_y + run_du),
    ))


def compute_embankment_toes_per_edge(pad, pad_elev_m, terrain, slope_ratio_h,
                                     meters_per_unit=1.0):
    """Each edge offset by its own mid-edge run (cs:141-185)."""
    if terrain is None:
        raise TypeError("terrain is required")
    if slope_ratio_h < 0.0:
        raise ValueError("slopeRatioH must be >= 0, got %s." % slope_ratio_h)
    if meters_per_unit <= 0.0:
        raise ValueError("metersPerUnit must be > 0, got %s." % meters_per_unit)
    mid_s = (pad.min_x + pad.max_x) * 0.5
    mid_n = mid_s
    mid_w = (pad.min_y + pad.max_y) * 0.5
    mid_e = mid_w
    z_s = _or_zero(terrain.interpolate_z(mid_s, pad.min_y))
    z_n = _or_zero(terrain.interpolate_z(mid_n, pad.max_y))
    z_w = _or_zero(terrain.interpolate_z(pad.min_x, mid_w))
    z_e = _or_zero(terrain.interpolate_z(pad.max_x, mid_e))
    run_s = abs(pad_elev_m - z_s) * slope_ratio_h / meters_per_unit
    run_n = abs(pad_elev_m - z_n) * slope_ratio_h / meters_per_unit
    run_w = abs(pad_elev_m - z_w) * slope_ratio_h / meters_per_unit
    run_e = abs(pad_elev_m - z_e) * slope_ratio_h / meters_per_unit
    horizontal = _net_max(_net_max(run_s * meters_per_unit, run_n * meters_per_unit),
                          _net_max(run_w * meters_per_unit, run_e * meters_per_unit))
    return EmbankmentToes(horizontal, pad_elev_m - 0.25 * (z_s + z_n + z_w + z_e),
                          slope_ratio_h, (
                              (pad.min_x - run_w, pad.min_y - run_s),
                              (pad.max_x + run_e, pad.min_y - run_s),
                              (pad.max_x + run_e, pad.max_y + run_n),
                              (pad.min_x - run_w, pad.max_y + run_n),
                          ))


def _or_zero(value):
    """C#'s `?? 0.0` on a double?."""
    return 0.0 if value is None else value


GRADING_PAD_HEADER = ("mode,corner,toe_x,toe_y,horizontal_run_m,height_delta_m,"
                      "slope_ratio_h")
CORNER_NAMES = ("SW", "SE", "NE", "NW")


def grading_pad_demo_csv(mid_edge_z, uniform_ref, uniform, per_edge):
    """leafgradingpad_demo.csv (GradingPadDemoCommand.cs:136-164), UTF-8 BOM.

    `mid_edge_z` is (S, N, W, E) as the command samples them.
    """
    z_s, z_n, z_w, z_e = mid_edge_z
    lines = [
        "# LEAFGRADINGPADDEMO Q19 embankment-toe geometry audit",
        "# terrain z(x,y) = 0.05*x m; pad [10,10]-[20,20]; padElev=1.5m; slope 2H:1V",
        "# mid-edge z: S=%s N=%s W=%s E=%s; uniform_ref=%s" % (
            format_fixed(z_s, 6), format_fixed(z_n, 6), format_fixed(z_w, 6),
            format_fixed(z_e, 6), format_fixed(uniform_ref, 6)),
        GRADING_PAD_HEADER,
    ]
    for mode, toes in (("uniform", uniform), ("per_edge", per_edge)):
        for name, (x, y) in zip(CORNER_NAMES, toes.corners_du):
            lines.append(",".join((
                mode, name, format_fixed(x, 6), format_fixed(y, 6),
                format_fixed(toes.horizontal_run_m, 6), format_fixed(toes.height_delta_m, 6),
                format_fixed(toes.slope_ratio_h, 6))))
    return _lines(lines, UTF8_BOM)


# --------------------------------------------------------------------------- #
# terrain-cross-section (TerrainProfileCalculator.cs)
# --------------------------------------------------------------------------- #
class ProfilePoint:
    __slots__ = ("distance_m", "elevation_m", "drawing_x", "drawing_y")

    def __init__(self, distance_m, elevation_m, drawing_x, drawing_y):
        self.distance_m = distance_m
        self.elevation_m = elevation_m
        self.drawing_x = drawing_x
        self.drawing_y = drawing_y


def sample_line(terrain, start_du, end_du, sample_count):
    """`sample_count` evenly spaced points, NaN elevation off-grid (cs:54-84)."""
    if terrain is None:
        raise TypeError("terrain is required")
    if sample_count < 1:
        raise ValueError("sampleCount must be >= 1.")
    dx = end_du[0] - start_du[0]
    dy = end_du[1] - start_du[1]
    len_du = math.sqrt(dx * dx + dy * dy)
    len_m = len_du * terrain.meters_per_unit
    result = []
    for i in range(sample_count):
        t = 0.0 if sample_count == 1 else i / (sample_count - 1)
        x = start_du[0] + t * dx
        y = start_du[1] + t * dy
        z = terrain.interpolate_z(x, y)
        result.append(ProfilePoint(t * len_m, math.nan if z is None else z, x, y))
    return result


def sample_polyline(terrain, vertices_du, samples_per_segment):
    """(N-1) x samples + 1 points with cumulative distance (cs:107-154)."""
    if terrain is None:
        raise TypeError("terrain is required")
    if vertices_du is None:
        raise TypeError("verticesDu is required")
    if len(vertices_du) < 2:
        raise ValueError("verticesDu must have at least 2 vertices.")
    if samples_per_segment < 1:
        raise ValueError("samplesPerSegment must be >= 1.")
    result = []
    cumulative_m = 0.0
    for seg in range(len(vertices_du) - 1):
        (fx, fy), (tx, ty) = vertices_du[seg], vertices_du[seg + 1]
        dx = tx - fx
        dy = ty - fy
        len_m = math.sqrt(dx * dx + dy * dy) * terrain.meters_per_unit
        for i in range(0 if seg == 0 else 1, samples_per_segment + 1):
            t = i / samples_per_segment
            x = fx + t * dx
            y = fy + t * dy
            z = terrain.interpolate_z(x, y)
            result.append(ProfilePoint(cumulative_m + t * len_m,
                                       math.nan if z is None else z, x, y))
        cumulative_m += len_m
    return result


CROSS_SECTION_HEADER = "distance_m,x,y,z_sampled,z_closed_form,abs_error_m"


def cross_section_demo_csv(profile, amplitude_m, wave_k):
    """leafcrosssection_demo.csv (CrossSectionDemoCommand.cs:136-169), UTF-8 BOM.

    The closed form is the command's own: amplitude * sin(k x) * cos(k y).
    """
    lines = [
        "# LEAFCROSSSECTIONDEMO bilinear-interp 5cm accuracy audit (Q20)",
        "# z(x,y) = 1.0 * sin(2pi*x/200) * cos(2pi*y/200) m; grid 41x41 at 10m",
        "# cut line: (-190,-150) -> (190,170); 100 samples",
        CROSS_SECTION_HEADER,
    ]
    for p in profile:
        z_closed = amplitude_m * math.sin(wave_k * p.drawing_x) * math.cos(wave_k * p.drawing_y)
        abs_err = math.nan if math.isnan(p.elevation_m) else abs(p.elevation_m - z_closed)
        lines.append(",".join(format_fixed(value, 6) for value in (
            p.distance_m, p.drawing_x, p.drawing_y, p.elevation_m, z_closed, abs_err)))
    return _lines(lines, UTF8_BOM)


# --------------------------------------------------------------------------- #
# capacity-iteration-sweep (CapacityIterationCalculator.cs over
# TrackerRowGenerator.cs)
# --------------------------------------------------------------------------- #
SQM_PER_ACRE = 4046.8564224   # CapacityIterationCalculator.cs:85, NIST exact


class TrackerModuleSpec:
    """TrackerRowGenerator.cs:10-165, the fields Generate and the sweep read."""

    __slots__ = ("along_axis_m", "cross_axis_m", "gap_m", "rail_overhang_m",
                 "corridor_gap_m", "secondary_corridor_gap_m", "pmax_w",
                 "structural_gap_every_n_modules", "structural_gap_m")

    def __init__(self, along_axis_m=1.0, cross_axis_m=2.1, gap_m=0.02, rail_overhang_m=0.0,
                 corridor_gap_m=0.0, secondary_corridor_gap_m=0.0, pmax_w=0.0,
                 structural_gap_every_n_modules=0, structural_gap_m=0.0):
        self.along_axis_m = along_axis_m
        self.cross_axis_m = cross_axis_m
        self.gap_m = gap_m
        self.rail_overhang_m = rail_overhang_m
        self.corridor_gap_m = corridor_gap_m
        self.secondary_corridor_gap_m = secondary_corridor_gap_m
        self.pmax_w = pmax_w
        self.structural_gap_every_n_modules = structural_gap_every_n_modules
        self.structural_gap_m = structural_gap_m

    @property
    def slot_m(self):
        return self.along_axis_m + self.gap_m

    def row_length_with_structural_gaps_meters(self, module_slots):
        gaps = count_interior_structural_gaps(module_slots, self.structural_gap_every_n_modules)
        return module_slots * self.slot_m + gaps * self.structural_gap_m

    def validate(self):
        """cs:140-165, the torque-tube gaps aside (the sweep never sets them)."""
        for name, value, floor_ok in (
                ("AlongAxisM", self.along_axis_m, False),
                ("CrossAxisM", self.cross_axis_m, False),
                ("GapM", self.gap_m, True),
                ("CorridorGapM", self.corridor_gap_m, True),
                ("SecondaryCorridorGapM", self.secondary_corridor_gap_m, True),
                ("StructuralGapEveryNModules", self.structural_gap_every_n_modules, True),
                ("StructuralGapM", self.structural_gap_m, True)):
            if (value < 0) if floor_ok else (value <= 0):
                raise ValueError("%s must be %s 0, got %s." % (
                    name, ">=" if floor_ok else ">", value))


def count_interior_structural_gaps(module_slots, every_n_modules):
    """cs:115-123: the trailing boundary at the row end is trimmed."""
    if module_slots <= 0 or every_n_modules <= 0:
        return 0
    candidates = module_slots // every_n_modules
    if module_slots % every_n_modules == 0:
        return max(0, candidates - 1)
    return candidates


class TrackerRowLayout:
    """The TrackerRow fields Generate sets (cs:453-553)."""

    __slots__ = ("axis_start", "axis_end", "module_slots", "length_meters",
                 "rail_overhang_m", "row_index")

    def __init__(self, axis_start, axis_end, module_slots, length_meters, rail_overhang_m,
                 row_index):
        self.axis_start = axis_start
        self.axis_end = axis_end
        self.module_slots = module_slots
        self.length_meters = length_meters
        self.rail_overhang_m = rail_overhang_m
        self.row_index = row_index


def _rotate(point, cos_a, sin_a):
    x, y = point
    return (x * cos_a - y * sin_a, x * sin_a + y * cos_a)


def _clip_column_to_polygon(poly, xi):
    """Half-open scanline crossings, sorted and paired even-odd (cs:622-660)."""
    n = len(poly)
    y_values = []
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        if not ((ax <= xi < bx) or (bx <= xi < ax)):
            continue
        t = (xi - ax) / (bx - ax)
        y_values.append(ay + t * (by - ay))
    segments = []
    if len(y_values) < 2:
        return segments
    y_values.sort()
    for i in range(0, len(y_values) - 1, 2):
        lo, hi = y_values[i], y_values[i + 1]
        if hi > lo:
            segments.append((lo, hi))
    return segments


def generate_tracker_rows(boundary_pts, pitch_m, module, meters_per_unit=1.0,
                          azimuth_deg=0.0, ew_slope_rad=None):
    """TrackerRowGenerator.Generate (cs:341-579).

    `ew_slope_rad` stands in for TerrainSlopeQuery.GetEWSlopeRad: a callable
    from drawing-frame X to a slope in radians, or None for flat ground. The
    sweep advances at least a tenth of the nominal pitch per step (cs:574), so
    the loop is bounded by 10 x (x extent / pitch) columns.
    """
    if boundary_pts is None:
        raise TypeError("boundaryPts is required")
    if len(boundary_pts) < 3:
        raise ValueError("Boundary must have at least 3 vertices.")
    if pitch_m <= 0:
        raise ValueError("pitchM must be > 0.")
    if module is None:
        raise TypeError("module is required")
    module.validate()
    if meters_per_unit <= 0:
        raise ValueError("metersPerUnit must be > 0.")
    pitch_du = pitch_m / meters_per_unit
    rot_in_rad = azimuth_deg * math.pi / 180.0
    cos_in = math.cos(rot_in_rad)
    sin_in = math.sin(rot_in_rad)
    cos_out = math.cos(-rot_in_rad)
    sin_out = math.sin(-rot_in_rad)
    rot_poly = [_rotate(pt, cos_in, sin_in) for pt in boundary_pts]
    x_min = 1.7976931348623157e308    # double.MaxValue
    x_max = -1.7976931348623157e308   # double.MinValue
    for rx, _ in rot_poly:
        if rx < x_min:
            x_min = rx
        if rx > x_max:
            x_max = rx
    rows = []
    row_index = 0
    xi = x_min + pitch_du * 0.5
    slot_m = module.slot_m
    overhang_du = module.rail_overhang_m / meters_per_unit
    while xi < x_max:
        for y_lo, y_hi in _clip_column_to_polygon(rot_poly, xi):
            length_du = y_hi - y_lo
            length_m = length_du * meters_per_unit
            slots = int(length_m / slot_m)
            if slots < 1:
                continue
            mid_y = (y_lo + y_hi) * 0.5
            split = False
            if module.corridor_gap_m > 0.0 and module.secondary_corridor_gap_m > 0.0:
                body_m = length_m - module.corridor_gap_m - module.secondary_corridor_gap_m
                third_m = body_m / 3.0
                third_slots = int(third_m / slot_m)
                if third_slots >= 1:
                    split = True
                    used_m = module.row_length_with_structural_gaps_meters(third_slots)
                    used_du = used_m / meters_per_unit
                    primary_du = module.corridor_gap_m / meters_per_unit
                    secondary_du = module.secondary_corridor_gap_m / meters_per_unit
                    total_du = 3 * used_du + primary_du + secondary_du
                    s1_start = mid_y - total_du * 0.5
                    s1_end = s1_start + used_du
                    s2_start = s1_end + primary_du
                    s2_end = s2_start + used_du
                    s3_start = s2_end + secondary_du
                    s3_end = s3_start + used_du
                    for start, end in (((xi, s1_start - overhang_du), (xi, s1_end)),
                                       ((xi, s2_start), (xi, s2_end)),
                                       ((xi, s3_start), (xi, s3_end + overhang_du))):
                        rows.append(TrackerRowLayout(
                            _rotate(start, cos_out, sin_out), _rotate(end, cos_out, sin_out),
                            third_slots, used_m, module.rail_overhang_m, row_index))
                        row_index += 1
            if not split and module.corridor_gap_m > 0.0:
                half_slots = int((length_m - module.corridor_gap_m) / 2.0 / slot_m)
                if half_slots >= 1:
                    split = True
                    corridor_du = module.corridor_gap_m / meters_per_unit
                    used_m = module.row_length_with_structural_gaps_meters(half_slots)
                    used_du = used_m / meters_per_unit
                    sec1_start = mid_y - corridor_du * 0.5 - used_du
                    sec1_end = mid_y - corridor_du * 0.5
                    sec2_start = mid_y + corridor_du * 0.5
                    sec2_end = mid_y + corridor_du * 0.5 + used_du
                    for start, end in (((xi, sec1_start - overhang_du), (xi, sec1_end)),
                                       ((xi, sec2_start), (xi, sec2_end + overhang_du))):
                        rows.append(TrackerRowLayout(
                            _rotate(start, cos_out, sin_out), _rotate(end, cos_out, sin_out),
                            half_slots, used_m, module.rail_overhang_m, row_index))
                        row_index += 1
            if not split:
                used_m = module.row_length_with_structural_gaps_meters(slots)
                used_du = used_m / meters_per_unit
                start_y = (mid_y - used_du * 0.5) - overhang_du
                end_y = (mid_y + used_du * 0.5) + overhang_du
                rows.append(TrackerRowLayout(
                    _rotate((xi, start_y), cos_out, sin_out),
                    _rotate((xi, end_y), cos_out, sin_out),
                    slots, used_m, module.rail_overhang_m, row_index))
                row_index += 1
        drawing_xi = xi * cos_out
        slope_rad = 0.0 if ew_slope_rad is None else ew_slope_rad(drawing_xi)
        if slope_rad is None or not math.isfinite(slope_rad):
            slope_rad = 0.0
        pitch_eff_m = pitch_m * math.cos(slope_rad)
        xi += _net_max(pitch_eff_m / meters_per_unit, pitch_du * 0.1)
    return rows


class CapacityRange:
    """Inclusive linspace, endpoints included (CapacityIterationCalculator.cs:33-66)."""

    __slots__ = ("min_value", "max_value", "count")

    def __init__(self, min_value, max_value, count):
        if count < 1:
            raise ValueError("Count must be >= 1, got %s." % count)
        if max_value < min_value:
            raise ValueError("maxValue (%s) must be >= minValue (%s)." % (max_value, min_value))
        self.min_value = min_value
        self.max_value = max_value
        self.count = count

    def values(self):
        if self.count == 1:
            return [self.min_value]
        step = (self.max_value - self.min_value) / (self.count - 1)
        return [self.min_value + step * i for i in range(self.count)]


class ScenarioResult:
    __slots__ = ("gcr_target", "tilt_deg", "pitch_m", "row_count", "module_count",
                 "dc_kwp", "acres_used")

    def __init__(self, gcr_target, tilt_deg, pitch_m, row_count, module_count, dc_kwp,
                 acres_used):
        self.gcr_target = gcr_target
        self.tilt_deg = tilt_deg
        self.pitch_m = pitch_m
        self.row_count = row_count
        self.module_count = module_count
        self.dc_kwp = dc_kwp
        self.acres_used = acres_used


def shoelace_area(poly):
    """cs:186-197, orientation-agnostic."""
    total = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        total += (x1 * y2) - (x2 * y1)
    return abs(total) * 0.5


def enumerate_capacity(boundary, module, gcr_range, tilt_range, max_scenarios):
    """GCR x tilt sweep, most DC first, ties by GCR ascending (cs:98-180).

    Rows are laid out once per GCR, because layout is planform only. The sort
    is Python's stable sort under the plugin's comparator. .NET's List.Sort is
    an introsort whose small-array path (16 elements or fewer, which covers the
    licensed demo's 10) is an insertion sort and therefore also stable; above
    16 its order among exact ties (same DC and same GCR, differing only in tilt)
    is unspecified, and the port keeps the input order there.
    """
    if boundary is None:
        raise TypeError("boundary is required")
    if module is None:
        raise TypeError("module is required")
    if gcr_range is None:
        raise TypeError("gcrRange is required")
    if tilt_range is None:
        raise TypeError("tiltRange is required")
    if (gcr_range.min_value <= 0.0 or gcr_range.max_value <= 0.0
            or gcr_range.min_value > 1.0 or gcr_range.max_value > 1.0):
        raise ValueError("GCR values must be in (0, 1]; got range [%s, %s]."
                         % (gcr_range.min_value, gcr_range.max_value))
    scenario_count = gcr_range.count * tilt_range.count
    if scenario_count > max_scenarios:
        raise ValueError("Scenario count %d exceeds maxScenarios=%d."
                         % (scenario_count, max_scenarios))
    acres_used = shoelace_area(boundary) / SQM_PER_ACRE
    tilts = tilt_range.values()
    results = []
    for gcr in gcr_range.values():
        pitch_m = module.cross_axis_m / gcr
        rows = generate_tracker_rows(boundary, pitch_m, module, meters_per_unit=1.0,
                                     azimuth_deg=0.0, ew_slope_rad=None)
        row_count = len(rows)
        module_count = sum(row.module_slots for row in rows)
        dc_kwp = module_count * module.pmax_w / 1000.0
        for tilt in tilts:
            results.append(ScenarioResult(gcr, tilt, pitch_m, row_count, module_count,
                                          dc_kwp, acres_used))
    results.sort(key=lambda r: (-r.dc_kwp, r.gcr_target))
    return results


CAPACITY_HEADER = "gcr,tilt_deg,pitch_m,row_count,module_count,dc_kwp,acres_used"


def capacity_demo_csv(results):
    """leafcapacity_demo.csv (CapacityIterationDemoCommand.cs:89-105), UTF-8 BOM."""
    lines = [
        "# LEAFCAPACITYITERATEDEMO synthetic capacity iteration",
        "# Boundary: 100.0 x 50.0 m rectangle; PmaxW=600.0; CrossAxisM=2.1",
        CAPACITY_HEADER,
    ]
    for r in results:
        lines.append(",".join((
            format_fixed(r.gcr_target, 4), format_fixed(r.tilt_deg, 4),
            format_fixed(r.pitch_m, 6), format_int(r.row_count), format_int(r.module_count),
            format_fixed(r.dc_kwp, 6), format_fixed(r.acres_used, 6))))
    return _lines(lines, UTF8_BOM)
