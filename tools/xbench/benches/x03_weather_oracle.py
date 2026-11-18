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
