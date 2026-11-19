"""Linear open-circuit voltage correction at a supplied cell temperature."""
from math import isfinite, isclose


def _sample_voltage(weather, *, site=None, station=None, clock_skew=0, as_of, max_age, voc_at_25, coefficient_per_c, modules):
    if not isinstance(weather, dict) or any(weather.get(k) is None for k in ("unit","cell_temp","timestamp")):
        return "INCOMPLETE"
    location = (weather.get("site"), weather.get("station"))
    if not all(isinstance(s, str) and s.strip() for s in (*location, site, station)):
        return "INCOMPLETE"
    if location != (site, station):
        return "FAIL"
    if weather.get("timezone") is None:
        return "INCOMPLETE"
    if weather["timezone"] != "UTC":
        return "FAIL"
    values = (clock_skew, weather["timestamp"], weather["cell_temp"], as_of, max_age, voc_at_25, coefficient_per_c)
    try:
        values = tuple(float(x) if type(x) in (float, int) else float("nan") for x in values)
    except OverflowError:
        return "FAIL"
    if any(type(x) not in (float, int) or not isfinite(x) for x in values):
        return "FAIL"
    if weather["unit"] != "C" or clock_skew < 0 or not -clock_skew <= as_of-weather["timestamp"] <= max_age:
        return "FAIL"
    if type(modules) is not int or modules <= 0 or voc_at_25 <= 0:
        return "FAIL"
    try:
        expected = float(modules) * voc_at_25 * (1 + coefficient_per_c * (weather["cell_temp"] - 25))
    except OverflowError:
        return "FAIL"
    if not isfinite(expected) or expected <= 0 or weather["cell_temp"] < -273.15:
        return "FAIL"
    return expected


def _voltage_verdict(expected, reported):
    if isinstance(expected, str):
        return expected
    try:
        matches = type(reported) in (int, float) and isfinite(reported) and isclose(reported, expected, rel_tol=1e-9)
    except OverflowError:
        matches = False
    return "PASS" if matches else "FAIL"


def weather_quality(weather, *, reported, **conditions):
    return _voltage_verdict(_sample_voltage(weather, **conditions), reported)


def series_voltage_quality(samples, required_times, *, reported, **conditions):
    if not isinstance(samples, list) or not isinstance(required_times, (list, tuple)) or not required_times:
        return "INCOMPLETE"
    if any(type(t) not in (int, float) for t in required_times) or len(set(required_times)) != len(required_times):
        return "INCOMPLETE"
    voltages = {}
    for sample in samples:
        voltage = _sample_voltage(sample, **conditions)
        if isinstance(voltage, str):
            return voltage
        stamp = sample["timestamp"]
        if stamp in voltages:
            return "FAIL"
        voltages[stamp] = voltage
    if set(required_times) - voltages.keys():
        return "INCOMPLETE"
    if voltages.keys() != set(required_times):
        return "FAIL"
    return _voltage_verdict(max(voltages.values()), reported)


def weather_cadence_quality(timestamps, start, end, step):
    """Check an inclusive UTC sample window in integer ticks, regardless of input order."""
    if not isinstance(timestamps, list) or any(v is None for v in (start, end, step)) or None in timestamps:
        return "INCOMPLETE"
    if any(type(v) is not int for v in (start, end, step, *timestamps)) or step <= 0 or end < start:
        return "FAIL"
    intervals, remainder = divmod(end - start, step)
    if remainder or len(timestamps) != intervals + 1 or len(set(timestamps)) != len(timestamps):
        return "FAIL"
    return "PASS" if all(start <= t <= end and (t-start) % step == 0 for t in timestamps) else "FAIL"


def uncertain_voltage_quality(weather, bounds_c, limit_volts, **conditions):
    """Both endpoints of a recorded cell-temperature interval must satisfy the voltage limit."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(bounds_c, (tuple, list)) or len(bounds_c) != 2 or None in bounds_c or limit_volts is None:
        return "INCOMPLETE"
    nominal = _sample_voltage(weather, **conditions)
    if isinstance(nominal, str):
        return nominal
    low, high = bounds_c
    if not all(map(finite_measure, (low, high, limit_volts))) or not low <= weather["cell_temp"] <= high or limit_volts <= 0:
        return "FAIL"
    for temperature in (low, high):
        volts = _sample_voltage(dict(weather, cell_temp=temperature), **conditions)
        if isinstance(volts, str):
            return volts
        reference = Fraction(str(conditions["voc_at_25"])) * conditions["modules"]
        factor = 1 + Fraction(str(conditions["coefficient_per_c"])) * (Fraction(str(temperature)) - 25)
        if reference * factor > Fraction(str(limit_volts)):
            return "FAIL"
    return "PASS"


def filled_voltage_quality(samples, timestamp, reported, *, interpolation, extrapolation, max_gap, **conditions):
    """Use observed samples, bounded linear interpolation, or explicitly bounded endpoint holds."""
    from bisect import bisect_left
    from benches.x03_quality import finite_measure
    if (not isinstance(samples, list) or not samples or interpolation not in ("none", "linear")
            or extrapolation not in ("none", "hold") or None in (timestamp, max_gap)):
        return "INCOMPLETE"
    if not finite_measure(timestamp) or not finite_measure(max_gap) or max_gap < 0:
        return "FAIL"
    by_time = {}
    for sample in samples:
        checked = _sample_voltage(sample, **conditions)
        if isinstance(checked, str):
            return checked
        t = sample["timestamp"]
        if t in by_time:
            return "FAIL"
        by_time[t] = sample
    times = sorted(by_time)
    if timestamp in by_time:
        return weather_quality(by_time[timestamp], reported=reported, **conditions)
    index = bisect_left(times, timestamp)
    if index == 0 or index == len(times):
        endpoint = times[0] if index == 0 else times[-1]
        if extrapolation != "hold" or abs(timestamp - endpoint) > max_gap:
            return "FAIL"
        temperature = by_time[endpoint]["cell_temp"]
    else:
        left, right = times[index-1:index+1]
        if interpolation != "linear" or right-left > max_gap:
            return "FAIL"
        first = by_time[left]["cell_temp"]
        temperature = first + (by_time[right]["cell_temp"]-first) * (timestamp-left) / (right-left)
    filled = dict(samples[0], timestamp=timestamp, cell_temp=temperature)
    return weather_quality(filled, reported=reported, **conditions)


def flagged_voltage_quality(sample, allowed_flags, *, reported, **conditions):
    """Only observations with an explicitly accepted quality flag can support voltage credit."""
    if not isinstance(allowed_flags, (list, tuple)) or not all(isinstance(v, str) and v for v in allowed_flags):
        return "INCOMPLETE"
    if not isinstance(sample, dict) or not isinstance(sample.get("quality_flag"), str) or not sample["quality_flag"]:
        return "INCOMPLETE"
    if sample["quality_flag"] not in allowed_flags:
        return "FAIL"
    return weather_quality(sample, reported=reported, **conditions)


def calibrated_voltage_quality(sensor, revision, at, raw_volts, records, reported):
    """Use the pinned additive correction in volts during [start, end)."""
    from benches.x03_quality import finite_measure
    if not sensor or not revision or not isinstance(records, list) or not all(map(finite_measure, (at, raw_volts))):
        return "INCOMPLETE"
    pinned = [r for r in records if isinstance(r, dict) and r.get("sensor") == sensor and r.get("revision") == revision]
    if not pinned:
        return "INCOMPLETE"
    active = []
    for record in pinned:
        start, end, offset = (record.get(k) for k in ("start", "end", "offset_volts"))
        if not all(map(finite_measure, (start, end, offset))) or end <= start:
            return "INCOMPLETE"
        if start <= at < end:
            active.append(offset)
    if not active:
        return "FAIL"
    if len(active) != 1 or reported is None:
        return "INCOMPLETE"
    return "PASS" if finite_measure(reported) and isclose(reported, raw_volts + active[0], rel_tol=0, abs_tol=1e-9) else "FAIL"


def failover_voltage_quality(priority, online, sample, *, reported, **conditions):
    """At the observation time the first online station in the approved list owns the sample."""
    if not isinstance(priority, list) or not priority or not isinstance(online, dict):
        return "INCOMPLETE"
    if not all(isinstance(s, str) and s for s in priority) or len(set(priority)) != len(priority):
        return "INCOMPLETE"
    for station in priority:
        if type(online.get(station)) is not bool:
            return "INCOMPLETE"
        if online[station]:
            return weather_quality(sample, station=station, reported=reported, **conditions)
    return "FAIL"


def spatial_voltage_quality(samples, weights, *, reported, **conditions):
    """Use synchronized observations and the declared normalized station weights."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(weights, dict) or not weights or not isinstance(samples, dict) or samples.keys() != weights.keys():
        return "INCOMPLETE"
    if not all(map(finite_measure, weights.values())):
        return "INCOMPLETE"
    if min(weights.values()) < 0 or sum(Fraction(str(w)) for w in weights.values()) != 1:
        return "FAIL"
    total = 0.0
    timestamps = set()
    for station, weight in weights.items():
        sample = samples[station]
        voltage = _sample_voltage(sample, station=station, **conditions)
        if isinstance(voltage, str):
            return voltage
        timestamps.add(sample["timestamp"])
        total += voltage * weight
    if len(timestamps) != 1:
        return "INCOMPLETE"
    return _voltage_verdict(total, reported)


def condensation_quality(air_c, humidity_pct, surface_c, rule, reported_risk):
    """Magnus dew point over water using caller-declared coefficients. Equality is at risk."""
    from math import log
    from benches.x03_quality import finite_measure
    if not isinstance(rule, dict) or rule.get("method") != "magnus-water" or type(reported_risk) is not bool:
        return "INCOMPLETE"
    a, b = rule.get("a"), rule.get("b_c")
    if not all(map(finite_measure, (air_c, humidity_pct, surface_c, a, b))):
        return "INCOMPLETE"
    if not 0 < humidity_pct <= 100 or min(a,b) <= 0 or air_c <= -b:
        return "INCOMPLETE"
    gamma = log(humidity_pct/100) + a * air_c / (b + air_c)
    if gamma >= a:
        return "INCOMPLETE"
    dew_c = air_c if humidity_pct == 100 else b * gamma / (a-gamma)
    return "PASS" if reported_risk == (surface_c <= dew_c) else "FAIL"


def wind_period_quality(speed_mps, observed_seconds, required_seconds, rules, reported_mps):
    """Apply only an explicitly approved directed conversion between averaging periods."""
    from benches.x03_quality import finite_measure
    if not all(map(finite_measure,(speed_mps,observed_seconds,required_seconds,reported_mps))):
        return "INCOMPLETE"
    if speed_mps < 0 or min(observed_seconds,required_seconds) <= 0:
        return "INCOMPLETE"
    factor = 1
    if observed_seconds != required_seconds:
        if not isinstance(rules,dict):
            return "INCOMPLETE"
        key = (observed_seconds,required_seconds)
        if key not in rules:
            return "FAIL"
        factor = rules[key]
        if not finite_measure(factor) or factor <= 0:
            return "INCOMPLETE"
    return "PASS" if isclose(reported_mps,speed_mps*factor,rel_tol=1e-9) else "FAIL"


def temperature_unit_voltage_quality(weather, **conditions):
    """Normalize a recorded temperature before applying the Celsius voltage model."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(weather, dict) or not finite_measure(weather.get("cell_temp")):
        return "INCOMPLETE"
    value = Fraction(str(weather["cell_temp"]))
    unit = weather.get("unit")
    if unit == "F":
        value = (value - 32) * 5 / 9
    elif unit == "K":
        value -= Fraction("273.15")
    elif unit != "C":
        return "INCOMPLETE"
    if value < Fraction("-273.15"):
        return "FAIL"
    return weather_quality(dict(weather, unit="C", cell_temp=float(value)), **conditions)


def irradiance_voltage_quality(weather, irradiance_w_m2, model, **conditions):
    """Use a declared linear ambient-to-cell rise for the recorded irradiance."""
    from benches.x03_quality import finite_measure
    if not isinstance(weather, dict) or not isinstance(model, dict) or model.get("method") != "linear-rise":
        return "INCOMPLETE"
    rise = model.get("rise_c_per_w_m2")
    ambient = weather.get("ambient_c")
    if not all(map(finite_measure, (rise, ambient, irradiance_w_m2))) or min(rise, irradiance_w_m2) < 0:
        return "INCOMPLETE"
    if ambient < -273.15:
        return "FAIL"
    sample = dict(weather, cell_temp=ambient + rise * irradiance_w_m2, unit="C")
    return weather_quality(sample, **conditions)


def interval_weather_quality(window, observations, rule, reported):
    """Mean of piecewise constant readings over an exactly covered observation window."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if rule != "duration-weighted-mean" or not isinstance(window, (list, tuple)) or len(window) != 2:
        return "INCOMPLETE"
    if not all(map(finite_measure, window)) or window[0] >= window[1] or not finite_measure(reported):
        return "INCOMPLETE"
    if not isinstance(observations, list) or not observations:
        return "INCOMPLETE"
    for row in observations:
        if not isinstance(row, (list, tuple)) or len(row) != 3 or not all(map(finite_measure, row)):
            return "INCOMPLETE"
    cursor = window[0]
    total = Fraction(0)
    for start, end, value in sorted(observations, key=lambda r: r[0]):
        if start != cursor or not start < end <= window[1]:
            return "INCOMPLETE"
        total += (Fraction(str(end)) - Fraction(str(start))) * Fraction(str(value))
        cursor = end
    if cursor != window[1]:
        return "INCOMPLETE"
    mean = total / (Fraction(str(window[1])) - Fraction(str(window[0])))
    return "PASS" if isclose(reported, mean, rel_tol=1e-9, abs_tol=1e-12) else "FAIL"


def voltage_temperature_alignment_quality(voltages, temperatures, max_skew, reported_pairs):
    """Each required voltage timestamp pairs with its unique nearest temperature timestamp."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not finite_measure(max_skew) or max_skew < 0 or not isinstance(reported_pairs, dict):
        return "INCOMPLETE"
    for trace in (voltages, temperatures):
        if not isinstance(trace, dict) or not trace or not all(map(finite_measure, (*trace.keys(), *trace.values()))):
            return "INCOMPLETE"
    expected = {}
    for stamp in voltages:
        distances = {t: abs(Fraction(str(t)) - Fraction(str(stamp))) for t in temperatures}
        nearest = min(distances.values())
        matches = [t for t, delta in distances.items() if delta == nearest]
        if len(matches) != 1 or nearest > Fraction(str(max_skew)):
            return "INCOMPLETE"
        expected[stamp] = matches[0]
    if expected.keys() - reported_pairs.keys():
        return "INCOMPLETE"
    return "PASS" if expected == reported_pairs and all(map(finite_measure, reported_pairs.values())) else "FAIL"


def wind_height_quality(revision, terrain, sensor_m, target_m, speed_mps, reported_mps):
    """Apply the pinned terrain power-law exponent between recorded positive heights."""
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(terrain, dict) or terrain.get("revision") != revision or terrain.get("model") != "power-law":
        return "INCOMPLETE"
    exponent = terrain.get("exponent")
    if not all(map(finite_measure, (sensor_m, target_m, speed_mps, reported_mps, exponent))):
        return "INCOMPLETE"
    if min(sensor_m, target_m) <= 0 or min(exponent, speed_mps) < 0:
        return "INCOMPLETE"
    try:
        adjusted = speed_mps * (target_m / sensor_m) ** exponent
    except OverflowError:
        return "INCOMPLETE"
    if not finite_measure(adjusted):
        return "INCOMPLETE"
    return "PASS" if isclose(adjusted, reported_mps, rel_tol=1e-9, abs_tol=1e-12) else "FAIL"


def circular_wind_quality(rows, policy, reported_deg, tolerance_deg):
    """Use recorded weights and compare the shortest angular distance to the vector mean."""
    from math import atan2, cos, degrees, fsum, hypot, radians, sin
    from benches.x03_quality import finite_measure
    if policy != "weighted-vector-mean" or not isinstance(rows, list) or not rows:
        return "INCOMPLETE"
    if not finite_measure(reported_deg) or not finite_measure(tolerance_deg) or not 0 <= tolerance_deg <= 180:
        return "INCOMPLETE"
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2 or not all(map(finite_measure, row)) or row[1] < 0:
            return "INCOMPLETE"
    scale = max(weight for _, weight in rows)
    if not scale:
        return "INCOMPLETE"
    weights = [weight / scale for _, weight in rows]
    angles = [radians(direction % 360) for direction, _ in rows]
    x = fsum(w*cos(a) for w, a in zip(weights, angles))
    y = fsum(w*sin(a) for w, a in zip(weights, angles))
    if hypot(x, y) <= 1e-14 * fsum(weights):
        return "INCOMPLETE"
    error = abs((degrees(atan2(y, x)) - reported_deg + 180) % 360 - 180)
    return "PASS" if error <= tolerance_deg or isclose(error, tolerance_deg, abs_tol=1e-12) else "FAIL"


def wind_dynamic_pressure_quality(pressure_pa, temperature_k, speed_mps, model, reported_pa):
    """Compute dynamic pressure using absolute dry-air pressure and a declared specific gas constant."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(model, dict) or model.get("method") != "dry-ideal-gas":
        return "INCOMPLETE"
    gas_constant = model.get("specific_r_j_kg_k")
    values = (pressure_pa, temperature_k, speed_mps, gas_constant, reported_pa)
    if not all(map(finite_measure, values)) or min(pressure_pa, temperature_k, gas_constant) <= 0 or speed_mps < 0:
        return "INCOMPLETE"
    pressure, kelvin, speed, specific_r, reported = (Fraction(str(v)) for v in values)
    expected = pressure * speed**2 / (2 * specific_r * kelvin)
    if reported < 0:
        return "FAIL"
    return "PASS" if abs(reported - expected) <= max(Fraction("1e-12"), expected / 10**9) else "FAIL"


def plane_irradiance_quality(revision, model, dni, dhi, zenith_deg, sun_azimuth_deg, tilt_deg, face_azimuth_deg, reported):
    """Isotropic sky with ground reflection from horizontal direct-plus-diffuse irradiance."""
    from math import cos, sin, radians
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(model, dict) or model.get("revision") != revision or model.get("kind") != "isotropic":
        return "INCOMPLETE"
    albedo = model.get("ground_albedo")
    if not all(map(finite_measure, (dni,dhi,zenith_deg,sun_azimuth_deg,tilt_deg,face_azimuth_deg,albedo,reported))):
        return "INCOMPLETE"
    if min(dni,dhi) < 0 or not 0 <= zenith_deg <= 90 or not 0 <= tilt_deg <= 180 or not 0 <= albedo <= 1:
        return "INCOMPLETE"
    z, tilt = radians(zenith_deg), radians(tilt_deg)
    azimuth_diff = radians(sun_azimuth_deg % 360 - face_azimuth_deg % 360)
    projection = cos(z)*cos(tilt) + sin(z)*sin(tilt)*cos(azimuth_diff)
    sky_view = (1 + cos(tilt)) / 2
    ground = (dni*cos(z) + dhi) * albedo * (1 - sky_view)
    expected = dni * max(0, projection) + dhi * sky_view + ground
    return "PASS" if reported >= 0 and isclose(reported, expected, rel_tol=1e-9, abs_tol=1e-9) else "FAIL"




def ice_history_quality(start_h, end_h, initial_mm, intervals, melt_mm_per_degree_hour, reported_mm, model):
    """Water-equivalent retained ice, freezing rain at or below zero, degree-hour melt."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    values = (start_h, end_h, initial_mm, melt_mm_per_degree_hour, reported_mm)
    if model != "freezing-rain-degree-hour-v1" or not all(map(finite_measure, values)):
        return "INCOMPLETE"
    if start_h >= end_h or min(initial_mm, melt_mm_per_degree_hour) < 0 or not isinstance(intervals, list):
        return "INCOMPLETE"
    cursor, end, retained, melt, reported = (Fraction(str(x)) for x in values)
    for row in intervals:
        if not isinstance(row, (tuple, list)) or len(row) != 5:
            return "INCOMPLETE"
        left, right, phase, temp_c, water_mm = row
        if phase not in ("rain", "freezing-rain", "dry") or not all(map(finite_measure, (left, right, temp_c, water_mm))):
            return "INCOMPLETE"
        left, right, temp, water = (Fraction(str(x)) for x in (left, right, temp_c, water_mm))
        if left != cursor or not left < right <= end or water < 0 or (phase == "dry" and water):
            return "INCOMPLETE"
        if temp <= 0 and phase == "freezing-rain":
            retained += water
        retained = max(0, retained - melt * max(0, temp) * (right-left))
        cursor = right
    if cursor != end:
        return "INCOMPLETE"
    return "PASS" if reported == retained else "FAIL"


def psychrometric_quality(revision, model, dry_c, wet_c, pressure_kpa, reported_rh, tolerance_pct):
    """Liquid-water Magnus saturation pressure with a pinned ventilated psychrometer coefficient."""
    from math import exp
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(model, dict) or model.get("revision") != revision or model.get("kind") != "magnus-psychrometer":
        return "INCOMPLETE"
    a, b, base, coeff = (model.get(key) for key in ("a", "b_c", "base_kpa", "coefficient_per_c"))
    if not all(map(finite_measure, (a,b,base,coeff,dry_c,wet_c,pressure_kpa,reported_rh,tolerance_pct))):
        return "INCOMPLETE"
    if min(a,b,base,coeff) <= 0 or not 0 <= tolerance_pct <= 100:
        return "INCOMPLETE"
    if pressure_kpa <= 0 or wet_c > dry_c or not 0 <= reported_rh <= 100:
        return "FAIL"
    if wet_c < 0:
        return "INCOMPLETE"
    try:
        saturation = base * exp(a * dry_c / (b + dry_c))
        vapor = base * exp(a * wet_c / (b + wet_c)) - coeff * pressure_kpa * (dry_c - wet_c)
    except OverflowError:
        return "INCOMPLETE"
    if not all(map(finite_measure, (saturation, vapor))):
        return "INCOMPLETE"
    if not 0 <= vapor < pressure_kpa:
        return "FAIL"
    return "PASS" if abs(100*vapor/saturation - reported_rh) <= tolerance_pct else "FAIL"


def precipitation_quality(window, intervals, policy, reported_mm):
    """Sum interval increments over an exactly tiled window. Resets require resolved increments."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if policy != "complete-interval-increments" or not isinstance(intervals, list) or not finite_measure(reported_mm):
        return "INCOMPLETE"
    if not isinstance(window, (list, tuple)) or len(window) != 2 or not all(map(finite_measure, window)) or window[0] >= window[1]:
        return "INCOMPLETE"
    factors = {"mm":1, "cm":10, "in":Fraction("25.4")}
    rows = []
    for row in intervals:
        if not isinstance(row, (list, tuple)) or len(row) != 5:
            return "INCOMPLETE"
        left, right, amount, unit, provenance = row
        if not isinstance(unit, str) or unit not in factors or provenance not in ("increment", "resolved-reset"):
            return "INCOMPLETE"
        if not all(map(finite_measure, (left,right,amount))) or amount < 0 or not window[0] <= left < right <= window[1]:
            return "INCOMPLETE"
        rows.append((left,right,Fraction(str(amount))*factors[unit]))
    cursor = window[0]
    total = Fraction(0)
    for left,right,increment in sorted(rows):
        if left < cursor:
            return "FAIL"
        if left > cursor:
            return "INCOMPLETE"
        total += increment
        cursor = right
    if cursor != window[1]:
        return "INCOMPLETE"
    return "PASS" if total == Fraction(str(reported_mm)) else "FAIL"


def horizon_beam_quality(revision, profile, azimuth_deg, elevation_deg, clear_beam, reported_beam):
    """Constant horizon per half-open azimuth sector; a ray grazing the horizon is masked."""
    from benches.x03_quality import finite_measure
    if not revision or not isinstance(profile, dict) or profile.get("revision") != revision:
        return "INCOMPLETE"
    values = (azimuth_deg, elevation_deg, clear_beam, reported_beam)
    if not all(map(finite_measure, values)) or not -90 <= elevation_deg <= 90 or clear_beam < 0:
        return "INCOMPLETE"
    sectors = profile.get("sectors_deg")
    if not isinstance(sectors, list):
        return "INCOMPLETE"
    matches = []
    for sector in sectors:
        if not isinstance(sector, (list, tuple)) or len(sector) != 3 or not all(map(finite_measure, sector)):
            return "INCOMPLETE"
        start, end, height = sector
        if not 0 <= start < end <= 360 or not -90 <= height <= 90:
            return "INCOMPLETE"
        if start <= azimuth_deg % 360 < end:
            matches.append(height)
    if len(matches) != 1:
        return "INCOMPLETE"
    expected = clear_beam if elevation_deg > max(0, matches[0]) else 0
    return "PASS" if reported_beam == expected else "FAIL"


def snow_water_quality(depth, depth_unit, layers, density_unit, reported_mm):
    """Layer boundaries use the declared depth unit, from surface to full recorded depth."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    metres = {"m":1, "cm":Fraction(1,100), "mm":Fraction(1,1000)}
    densities = {"kg/m3":1, "g/cm3":1000}
    if not isinstance(depth_unit, str) or not isinstance(density_unit, str) or depth_unit not in metres or density_unit not in densities:
        return "INCOMPLETE"
    if not all(map(finite_measure, (depth, reported_mm))) or depth < 0 or not isinstance(layers, list):
        return "INCOMPLETE"
    cursor = total = Fraction(0)
    for layer in layers:
        if not isinstance(layer, (tuple, list)) or len(layer) != 3 or not all(map(finite_measure, layer)):
            return "INCOMPLETE"
        top, bottom, density = (Fraction(str(x)) for x in layer)
        if top != cursor or bottom <= top or density <= 0:
            return "INCOMPLETE"
        total += (bottom-top) * metres[depth_unit] * density * densities[density_unit]
        cursor = bottom
    if cursor != Fraction(str(depth)):
        return "INCOMPLETE"
    return "PASS" if total == Fraction(str(reported_mm)) else "FAIL"


def rolling_rain_quality(window_min, max_sample_min, max_mm_h, trace, rule):
    """Maximum whole-window mean under a uniform rate in each recorded gauge interval."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    values = (window_min, max_sample_min, max_mm_h)
    if rule != "continuous-uniform-interval-v1" or not all(map(finite_measure, values)):
        return "INCOMPLETE"
    width, resolution, limit = (Fraction(str(x)) for x in values)
    if min(width, resolution) <= 0 or limit < 0 or not isinstance(trace, list) or not trace:
        return "INCOMPLETE"
    times, depths, rates = [], [], []
    depth = Fraction(0)
    for row in trace:
        if not isinstance(row, (tuple, list)) or len(row) != 3 or not all(map(finite_measure, row)):
            return "INCOMPLETE"
        start, end, mm = (Fraction(str(x)) for x in row)
        if (times and start != last_end) or not 0 < end-start <= resolution or mm < 0:
            return "INCOMPLETE"
        times.append(start)
        depths.append(depth)
        rates.append(mm/(end-start))
        depth += mm
        last_end = end
    if last_end-times[0] < width:
        return "INCOMPLETE"
    from bisect import bisect_right
    def accumulated(t):
        i = bisect_right(times, t)-1
        return depths[i] + rates[i]*(t-times[i])
    # The rolling integral changes slope only when either edge passes a gauge boundary.
    bounds = times + [last_end]
    starts = {x for b in bounds for x in (b,b-width) if times[0] <= x <= last_end-width}
    peak = max(accumulated(x+width)-accumulated(x) for x in starts)
    return "PASS" if peak * 60 <= limit * width else "FAIL"


def pyranometer_horizontal_quality(model, tilt_deg, facing_deg, sun_elevation_deg, sun_azimuth_deg, direct_poa, reported, tolerance):
    """Project a beam-only plane reading to horizontal using the supplied sensor normal."""
    from math import sin, cos, radians
    from benches.x03_quality import finite_measure
    values = (tilt_deg, facing_deg, sun_elevation_deg, sun_azimuth_deg, direct_poa, reported, tolerance)
    if model != "direct-only-cosine-v1" or not all(map(finite_measure, values)):
        return "INCOMPLETE"
    if not 0 <= tilt_deg <= 90 or not 0 < sun_elevation_deg <= 90 or min(direct_poa,tolerance) < 0:
        return "INCOMPLETE"
    elevation, tilt = radians(sun_elevation_deg), radians(tilt_deg)
    delta = radians((sun_azimuth_deg % 360) - (facing_deg % 360))
    incidence = sin(elevation)*cos(tilt) + cos(elevation)*sin(tilt)*cos(delta)
    if incidence <= 1e-12:
        return "INCOMPLETE"
    expected = direct_poa * sin(elevation) / incidence
    return "PASS" if reported >= 0 and abs(reported-expected) <= tolerance else "FAIL"


def daylight_energy_quality(daylight_s, intervals, reported_wh_m2):
    """Integrate recorded interval means over one fully covered daylight window."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure
    if not isinstance(daylight_s, (list, tuple)) or len(daylight_s) != 2 or not all(map(finite_measure, daylight_s)):
        return "INCOMPLETE"
    start, end = daylight_s
    if start >= end or not isinstance(intervals, list) or not finite_measure(reported_wh_m2):
        return "INCOMPLETE"
    energy = Fraction(0)
    for row in intervals:
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            return "INCOMPLETE"
        left, right, mean, unit = row
        if not all(map(finite_measure, (left, right, mean))) or left != start or not left < right <= end or mean < 0:
            return "INCOMPLETE"
        if unit not in ("W/m2", "kW/m2"):
            return "INCOMPLETE"
        seconds = Fraction(str(right)) - Fraction(str(left))
        energy += Fraction(str(mean)) * (1000 if unit == "kW/m2" else 1) * seconds / 3600
        start = right
    if start != end:
        return "INCOMPLETE"
    return "PASS" if energy == Fraction(str(reported_wh_m2)) else "FAIL"


def rear_reflected_irradiance_quality(model, ground, rear_view, reported_w_m2):
    """Lambertian ground patches, with rear-facing view factors and incident W/m2.

    Each ground patch contributes once. Any remaining view is nonreflecting.
    The report covers the ground-reflected component on the rear surface.
    """
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if model != "lambertian-ground-patches-v1" or not finite_measure(reported_w_m2):
        return "INCOMPLETE"
    if not isinstance(ground, dict) or not ground or not isinstance(rear_view, dict):
        return "INCOMPLETE"
    if ground.keys() != rear_view.keys():
        return "INCOMPLETE"
    expected = Fraction(0)
    total_view = Fraction(0)
    for patch, reading in ground.items():
        if not isinstance(reading, (list, tuple)) or len(reading) != 2:
            return "INCOMPLETE"
        irradiance, albedo = reading
        view = rear_view[patch]
        if not all(map(finite_measure, (irradiance, albedo, view))):
            return "INCOMPLETE"
        if irradiance < 0 or not 0 <= albedo <= 1 or not 0 <= view <= 1:
            return "INCOMPLETE"
        factor = Fraction(str(view))
        total_view += factor
        expected += Fraction(str(irradiance)) * Fraction(str(albedo)) * factor
    if total_view > 1:
        return "INCOMPLETE"
    return "PASS" if expected == Fraction(str(reported_w_m2)) else "FAIL"


def tipping_bucket_depth_quality(model, tips_s, reported_mm):
    """Correct only complete inter-tip intervals, assuming steady rain in each.

    Each tip blinds the gauge for dead_s, then the next bucket fills. Observed
    interval depth is bucket depth times elapsed time divided by active time.
    """
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if not isinstance(model, dict) or model.get("rule") != "nonparalyzable-intertip-v1":
        return "INCOMPLETE"
    values = [model.get(k) for k in ("bucket_mm", "dead_s", "min_gap_s", "max_gap_s")]
    if not all(map(finite_measure, values)) or not finite_measure(reported_mm):
        return "INCOMPLETE"
    bucket, dead, shortest, longest = (Fraction(str(x)) for x in values)
    if bucket <= 0 or not 0 <= dead < shortest <= longest:
        return "INCOMPLETE"
    if not isinstance(tips_s, (tuple, list)) or len(tips_s) < 2 or not all(map(finite_measure, tips_s)):
        return "INCOMPLETE"
    times = [Fraction(str(t)) for t in tips_s]
    depth = Fraction(0)
    for start, end in zip(times, times[1:]):
        gap = end - start
        if not shortest <= gap <= longest:
            return "INCOMPLETE"
        depth += bucket * gap / (gap - dead)
    return "PASS" if Fraction(str(reported_mm)) == depth else "FAIL"


def wind_direction_frame_quality(frame, sensor_deg, reported_from_deg, tolerance_deg):
    """Grade a horizontal meteorological bearing clockwise from true north.

    The sensor bearing advances clockwise from its recorded zero orientation.
    The output is the direction wind comes from.
    """
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if not isinstance(frame, dict) or frame.get("model") != "clockwise-true-north-v1":
        return "INCOMPLETE"
    convention = frame.get("sensor_direction")
    orientation = frame.get("zero_true_deg")
    if convention not in ("from", "to"):
        return "INCOMPLETE"
    if not all(map(finite_measure, (orientation, sensor_deg, reported_from_deg, tolerance_deg))):
        return "INCOMPLETE"
    if not 0 <= tolerance_deg < 180:
        return "INCOMPLETE"
    orientation, reading, reported, tolerance = (
        Fraction(str(value)) for value in (orientation, sensor_deg, reported_from_deg, tolerance_deg)
    )
    expected = orientation + reading + (180 if convention == "to" else 0)
    distance = abs((reported - expected + 180) % 360 - 180)
    return "PASS" if distance <= tolerance else "FAIL"


def sea_level_pressure_quality(model, station, report, tolerance_hpa):
    """Reduce dry station pressure using a constant mean column temperature."""
    from math import exp, isfinite
    from benches.x03_quality import finite_measure

    if model != "dry-isothermal-hypsometric-v1" or not all(isinstance(x, dict) for x in (station, report)):
        return "INCOMPLETE"
    pressure = station.get("pressure_hpa")
    elevation = station.get("elevation_m_asl")
    temperature = station.get("mean_column_k")
    reported = report.get("pressure_hpa")
    if not all(map(finite_measure, (pressure, elevation, temperature, reported, tolerance_hpa))):
        return "INCOMPLETE"
    if pressure <= 0 or temperature <= 0 or tolerance_hpa < 0:
        return "INCOMPLETE"
    if station.get("datum") not in ("station", "sea-level") or report.get("datum") not in ("station", "sea-level"):
        return "INCOMPLETE"
    if station["datum"] != "station" or report["datum"] != "sea-level":
        return "FAIL"
    # Version 1 pins gravity at 9.80665 m/s2 and dry-air R at 287.05 J/(kg K).
    try:
        reduced = pressure * exp(9.80665 * elevation / temperature / 287.05)
    except OverflowError:
        return "INCOMPLETE"
    if not isfinite(reduced) or reduced <= 0:
        return "INCOMPLETE"
    return "PASS" if reported > 0 and abs(reported - reduced) <= tolerance_hpa else "FAIL"


def anemometer_overspeed_quality(model, trace, reported_mean):
    """Invert a calibrated asymmetric first-order response at fixed sample intervals.

    The first reading initializes the sensor. Each later reading follows one
    interval of constant wind, with a separate gain for acceleration and decay.
    """
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if not isinstance(model, dict) or model.get("rule") != "asymmetric-discrete-response-v1":
        return "INCOMPLETE"
    values = [model.get(key) for key in ("step_s", "rise_gain", "fall_gain", "min_speed", "max_speed")]
    if not all(map(finite_measure, values)) or not finite_measure(reported_mean):
        return "INCOMPLETE"
    step, rise, fall, low, high = (Fraction(str(x)) for x in values)
    if step <= 0 or not 0 < fall <= rise <= 1 or not 0 <= low < high:
        return "INCOMPLETE"
    if not isinstance(trace, list) or len(trace) < 2:
        return "INCOMPLETE"
    readings = []
    for row in trace:
        if not isinstance(row, (tuple, list)) or len(row) != 2 or not all(map(finite_measure, row)):
            return "INCOMPLETE"
        time, speed = (Fraction(str(x)) for x in row)
        if not low <= speed <= high:
            return "INCOMPLETE"
        readings.append((time, speed))
    total = Fraction(0)
    for (start, prior), (end, observed) in zip(readings, readings[1:]):
        if end - start != step:
            return "INCOMPLETE"
        gain = rise if observed >= prior else fall
        wind = prior + (observed - prior) / gain
        if not low <= wind <= high:
            return "INCOMPLETE"
        total += wind
    return "PASS" if total == Fraction(str(reported_mean)) * (len(readings) - 1) else "FAIL"


def wind_undercatch_quality(revision, calibration, intervals, reported_mm):
    """Correct each gauge depth with its phase-specific efficiency at gauge height.

    The calibration interpolates linearly between wind/efficiency knots. It
    supports only the wind range measured for that phase.
    """
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if not isinstance(revision, str) or not revision or not isinstance(calibration, dict):
        return "INCOMPLETE"
    curves = calibration.get("phase_curves")
    if calibration.get("revision") != revision or not isinstance(curves, dict):
        return "INCOMPLETE"
    if not isinstance(intervals, list) or not intervals or not finite_measure(reported_mm):
        return "INCOMPLETE"
    corrected = Fraction(0)
    for interval in intervals:
        if not isinstance(interval, dict):
            return "INCOMPLETE"
        phase = interval.get("phase")
        wind, depth = interval.get("gauge_height_wind_m_s"), interval.get("caught_mm")
        if not isinstance(phase, str) or not phase or not all(map(finite_measure, (wind, depth))):
            return "INCOMPLETE"
        curve = curves.get(phase)
        if not isinstance(curve, list) or len(curve) < 2 or min(wind, depth) < 0:
            return "INCOMPLETE"
        wind = Fraction(str(wind))
        previous = None
        efficiency = None
        for knot in curve:
            if not isinstance(knot, (tuple, list)) or len(knot) != 2 or not all(map(finite_measure, knot)):
                return "INCOMPLETE"
            speed, ratio = (Fraction(str(v)) for v in knot)
            if speed < 0 or not 0 < ratio <= 1:
                return "INCOMPLETE"
            if previous is not None:
                left, left_ratio = previous
                if speed <= left:
                    return "INCOMPLETE"
                if left <= wind <= speed:
                    efficiency = left_ratio + (ratio - left_ratio) * (wind - left) / (speed - left)
            previous = speed, ratio
        if efficiency is None:
            return "INCOMPLETE"
        corrected += Fraction(str(depth)) / efficiency
    return "PASS" if corrected == Fraction(str(reported_mm)) else "FAIL"


def cosine_response_quality(revision, calibration, incidence_deg, measured_w_m2, corrected_w_m2):
    """Correct beam irradiance using measured/ideal plane-response ratio at incidence."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if not isinstance(revision, str) or not revision or not isinstance(calibration, dict):
        return "INCOMPLETE"
    if calibration.get("revision") != revision or calibration.get("model") != "linear-measured-to-ideal-v1":
        return "INCOMPLETE"
    knots = calibration.get("angle_ratio")
    if not isinstance(knots, list) or len(knots) < 2:
        return "INCOMPLETE"
    if not all(map(finite_measure, (incidence_deg, measured_w_m2, corrected_w_m2))) or measured_w_m2 < 0:
        return "INCOMPLETE"
    angle = Fraction(str(incidence_deg))
    previous = None
    ratio = None
    for knot in knots:
        if not isinstance(knot, (tuple, list)) or len(knot) != 2 or not all(map(finite_measure, knot)):
            return "INCOMPLETE"
        position, response = (Fraction(str(x)) for x in knot)
        if not 0 <= position < 90 or response <= 0:
            return "INCOMPLETE"
        if previous is not None:
            left, left_response = previous
            if left >= position:
                return "INCOMPLETE"
            if left <= angle <= position:
                ratio = left_response + (angle - left) * (response - left_response) / (position - left)
        previous = position, response
    if ratio is None:
        return "INCOMPLETE"
    return "PASS" if Fraction(str(corrected_w_m2)) * ratio == Fraction(str(measured_w_m2)) else "FAIL"


def sonic_dry_bulb_quality(model, sonic_k, specific_humidity, reported_dry_k):
    """Check the pinned acoustic approximation Ts = T * (1 + 0.51 q), in kelvin.

    q is specific humidity in kg water per kg moist air. The calibration's
    q interval limits where the approximation can be used.
    """
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if not isinstance(model, dict) or model.get("rule") != "sonic-specific-humidity-0.51-v1":
        return "INCOMPLETE"
    values = (model.get("min_q"), model.get("max_q"), sonic_k, specific_humidity, reported_dry_k)
    if not all(map(finite_measure, values)):
        return "INCOMPLETE"
    low, high, acoustic, humidity, reported = (Fraction(str(x)) for x in values)
    if not 0 <= low <= humidity <= high < 1 or acoustic <= 0:
        return "INCOMPLETE"
    return "PASS" if acoustic == reported * (1 + Fraction(51, 100) * humidity) else "FAIL"


def irradiance_plausibility_quality(model, elevation_deg, distance_au, ghi_w_m2):
    """Check horizontal irradiance against a pinned direct extraterrestrial envelope.

    The declared multiplier scales the daylit bound. Night has zero solar flux.
    """
    from math import sin, radians, isfinite, isclose
    from benches.x03_quality import finite_measure

    if not isinstance(model, dict) or model.get("rule") != "inverse-square-horizontal-v1":
        return "INCOMPLETE"
    constant = model.get("solar_constant_w_m2")
    multiplier = model.get("daylight_multiplier")
    if not all(map(finite_measure, (constant, multiplier, elevation_deg, distance_au, ghi_w_m2))):
        return "INCOMPLETE"
    if min(constant, multiplier, distance_au) <= 0 or not -90 <= elevation_deg <= 90:
        return "INCOMPLETE"
    if elevation_deg <= 0:
        return "PASS" if ghi_w_m2 == 0 else "FAIL"
    bound = constant / distance_au / distance_au * sin(radians(elevation_deg)) * multiplier
    if not isfinite(bound) or bound <= 0:
        return "INCOMPLETE"
    return "PASS" if 0 <= ghi_w_m2 and (ghi_w_m2 <= bound or isclose(ghi_w_m2, bound, rel_tol=1e-14)) else "FAIL"


def irradiance_closure_quality(readings, tolerance_w_m2):
    """Check a simultaneous daylight observation with absolute GHI residual in W/m2."""
    from math import cos, radians, isfinite, ulp
    from benches.x03_quality import finite_measure

    if not isinstance(readings, dict) or not finite_measure(tolerance_w_m2) or tolerance_w_m2 < 0:
        return "INCOMPLETE"
    ticks = set()
    values = []
    for channel in ("ghi", "dni", "dhi", "zenith_deg"):
        row = readings.get(channel)
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            return "INCOMPLETE"
        tick, value = row
        if type(tick) is not int or not finite_measure(value):
            return "INCOMPLETE"
        ticks.add(tick)
        values.append(value)
    ghi, dni, dhi, zenith = values
    if len(ticks) != 1 or not 0 <= zenith <= 90:
        return "INCOMPLETE"
    if min(ghi, dni, dhi) < 0:
        return "FAIL"
    projected = 0.0 if zenith == 90 else dni * cos(radians(zenith))
    expected = dhi + projected
    if not isfinite(expected):
        return "INCOMPLETE"
    residual = abs(ghi - expected)
    # Allow only floating roundoff in the cosine projection at the boundary.
    bound = tolerance_w_m2 + 2 * ulp(expected)
    return "PASS" if residual <= bound else "FAIL"


def pyranometer_thermal_offset_quality(revision, calibration, dome_c, body_c, raw_w_m2, reported_w_m2):
    """Apply the pinned additive offset a + b*(dome-body), over qualified temperatures."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if not isinstance(revision, str) or not revision or not isinstance(calibration, dict):
        return "INCOMPLETE"
    if calibration.get("revision") != revision or calibration.get("rule") != "dome-body-linear-offset-v1":
        return "INCOMPLETE"
    values = (dome_c, body_c, raw_w_m2, reported_w_m2, calibration.get("offset_w_m2"),
              calibration.get("slope_w_m2_per_k"))
    if not all(map(finite_measure, values)):
        return "INCOMPLETE"
    for name, value in (("dome_c_range", dome_c), ("body_c_range", body_c)):
        bounds = calibration.get(name)
        if not isinstance(bounds, (tuple, list)) or len(bounds) != 2 or not all(map(finite_measure, bounds)):
            return "INCOMPLETE"
        if not bounds[0] <= value <= bounds[1]:
            return "INCOMPLETE"
    dome, body, raw, reported, intercept, slope = (Fraction(str(v)) for v in values)
    corrected = raw - intercept - slope * (dome - body)
    return "PASS" if corrected == reported else "FAIL"


def shield_radiation_bias_quality(revision, shield, irradiance, ventilation, measured_c, reported_c):
    """Interpolate shield heating per W/m2 at the recorded ventilation speed."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if not isinstance(revision, str) or not revision or not isinstance(shield, dict):
        return "INCOMPLETE"
    if shield.get("revision") != revision or shield.get("rule") != "linear-heating-by-ventilation-v1":
        return "INCOMPLETE"
    knots, bounds = shield.get("wind_gain"), shield.get("irradiance_range")
    if not isinstance(knots, list) or len(knots) < 2 or not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
        return "INCOMPLETE"
    if not all(map(finite_measure, (irradiance, ventilation, measured_c, reported_c, *bounds))):
        return "INCOMPLETE"
    if not 0 <= bounds[0] <= irradiance <= bounds[1] or ventilation < 0:
        return "INCOMPLETE"
    wind = Fraction(str(ventilation))
    prior = gain = None
    for knot in knots:
        if not isinstance(knot, (tuple, list)) or len(knot) != 2 or not all(map(finite_measure, knot)):
            return "INCOMPLETE"
        speed, factor = (Fraction(str(x)) for x in knot)
        if min(speed, factor) < 0:
            return "INCOMPLETE"
        if prior is not None:
            left, left_gain = prior
            if speed <= left:
                return "INCOMPLETE"
            if left <= wind <= speed:
                gain = left_gain + (wind - left) * (factor - left_gain) / (speed - left)
        prior = speed, factor
    if gain is None:
        return "INCOMPLETE"
    expected = Fraction(str(measured_c)) - Fraction(str(irradiance)) * gain
    return "PASS" if expected == Fraction(str(reported_c)) else "FAIL"


def precipitation_phase_quality(thresholds, trace, reported):
    """Classify each wet-bulb sample. Snow and rain include their outer boundaries."""
    from benches.x03_quality import finite_measure

    if not isinstance(thresholds, dict) or thresholds.get("boundary_rule") != "snow-le-rain-ge-v1":
        return "INCOMPLETE"
    snow = thresholds.get("snow_max_wet_bulb_c")
    rain = thresholds.get("rain_min_wet_bulb_c")
    if not all(map(finite_measure, (snow, rain))) or snow >= rain:
        return "INCOMPLETE"
    if not isinstance(trace, list) or not trace or not isinstance(reported, list):
        return "INCOMPLETE"
    expected = []
    prior = None
    for sample in trace:
        if not isinstance(sample, (tuple, list)) or len(sample) != 2:
            return "INCOMPLETE"
        tick, temperature = sample
        if type(tick) is not int or (prior is not None and tick <= prior) or not finite_measure(temperature):
            return "INCOMPLETE"
        if temperature <= snow:
            phase = "snow"
        elif temperature >= rain:
            phase = "rain"
        else:
            phase = "mixed"
        expected.append((tick, phase))
        prior = tick
    observed = []
    for row in reported:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            return "INCOMPLETE"
        if type(row[0]) is not int or not isinstance(row[1], str):
            return "INCOMPLETE"
        observed.append(tuple(row))
    return "PASS" if observed == expected else "FAIL"


def pressure_port_quality(revision, calibration, wind_en_m_s, density_kg_m3, raw_pa, reported_static_pa):
    """Remove signed port bias from dynamic pressure along a calibrated horizontal axis."""
    from fractions import Fraction
    from benches.x03_quality import finite_measure

    if not isinstance(revision, str) or not revision or not isinstance(calibration, dict):
        return "INCOMPLETE"
    if calibration.get("revision") != revision or calibration.get("model") != "axial-dynamic-pressure-v1":
        return "INCOMPLETE"
    axis = calibration.get("axis_en")
    if not all(isinstance(pair, (tuple, list)) and len(pair) == 2 for pair in (axis, wind_en_m_s)):
        return "INCOMPLETE"
    values = (*axis, *wind_en_m_s, density_kg_m3, raw_pa, reported_static_pa,
              calibration.get("forward_cp"), calibration.get("reverse_cp"), calibration.get("max_wind_m_s"))
    if not all(map(finite_measure, values)):
        return "INCOMPLETE"
    ax, ay, east, north, density, raw, static, forward, reverse, limit = (Fraction(str(x)) for x in values)
    if ax * ax + ay * ay != 1 or density <= 0 or raw <= 0 or limit < 0 or east * east + north * north > limit * limit:
        return "INCOMPLETE"
    speed = east * ax + north * ay
    coefficient = forward if speed >= 0 else reverse
    pressure_bias = coefficient * density * speed * speed / 2
    return "PASS" if static > 0 and static == raw - pressure_bias else "FAIL"


def leaf_wetness_duration_quality(rule, window, intervals, reported_ticks):
    """Integrate the hysteretic wet state over the union of sensor intervals.

    Each half-open interval holds one sensor value. Identical overlaps are one
    observation. Unobserved time or conflicting simultaneous values lack coverage.
    """
    from collections import Counter, defaultdict
    from benches.x03_quality import finite_measure

    if not isinstance(rule, dict) or rule.get("policy") != "inclusive-wet-dry-hysteresis-v1":
        return "INCOMPLETE"
    wet, low, high = rule.get("initially_wet"), rule.get("dry_at"), rule.get("wet_at")
    if type(wet) is not bool or not all(map(finite_measure, (low, high))) or low >= high:
        return "INCOMPLETE"
    if not isinstance(window, (tuple, list)) or len(window) != 2 or not all(type(t) is int for t in window):
        return "INCOMPLETE"
    begin, end = window
    if end <= begin or not isinstance(intervals, list) or type(reported_ticks) is not int:
        return "INCOMPLETE"
    changes = defaultdict(Counter)
    for row in intervals:
        if not isinstance(row, (tuple, list)) or len(row) != 3:
            return "INCOMPLETE"
        left, right, signal = row
        if type(left) is not int or type(right) is not int or not begin <= left < right <= end:
            return "INCOMPLETE"
        if not finite_measure(signal):
            return "INCOMPLETE"
        changes[left][signal] += 1
        changes[right][signal] -= 1
    boundaries = sorted({begin, end, *changes})
    active = Counter()
    duration = 0
    for left, right in zip(boundaries, boundaries[1:]):
        active.update(changes[left])
        active = +active
        if len(active) != 1:
            return "INCOMPLETE"
        signal = next(iter(active))
        if signal >= high:
            wet = True
        elif signal <= low:
            wet = False
        if wet:
            duration += right - left
    return "PASS" if duration == reported_ticks else "FAIL"
