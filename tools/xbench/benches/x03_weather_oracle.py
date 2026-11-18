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
