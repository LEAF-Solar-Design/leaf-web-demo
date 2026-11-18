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
