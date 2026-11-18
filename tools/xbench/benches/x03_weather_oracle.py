"""Linear open-circuit voltage correction at a supplied cell temperature."""
from math import isfinite, isclose


def weather_quality(weather, *, as_of, max_age, voc_at_25, coefficient_per_c, modules, reported):
    if not isinstance(weather, dict) or any(weather.get(k) is None for k in ("unit","cell_temp","timestamp")):
        return "INCOMPLETE"
    values = (weather["timestamp"], weather["cell_temp"], as_of, max_age, voc_at_25, coefficient_per_c, reported)
    try:
        values = tuple(float(x) if type(x) in (float, int) else float("nan") for x in values)
    except OverflowError:
        return "FAIL"
    if any(type(x) not in (float, int) or not isfinite(x) for x in values):
        return "FAIL"
    if weather["unit"] != "C" or not 0 <= as_of-weather["timestamp"] <= max_age:
        return "FAIL"
    if type(modules) is not int or modules <= 0 or voc_at_25 <= 0:
        return "FAIL"
    try:
        expected = float(modules) * voc_at_25 * (1 + coefficient_per_c * (weather["cell_temp"] - 25))
    except OverflowError:
        return "FAIL"
    if not isfinite(expected) or expected <= 0 or reported <= 0 or weather["cell_temp"] < -273.15:
        return "FAIL"
    return "PASS" if isclose(reported, expected, rel_tol=1e-9) else "FAIL"
