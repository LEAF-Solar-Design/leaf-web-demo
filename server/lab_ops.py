"""mirror of web/src/lab/runtime/ops.js; change both together and extend server/tests/fixtures/lab-operator-cases.v1.json"""

import math
from functools import cmp_to_key

from lab_canonical import canonical_json

MAX_ROWS = 5000
OPERATOR_IDS = (
    "constant", "identity", "field", "select", "filter", "sort", "take", "count",
    "sum", "mean", "min", "max", "add", "subtract", "multiply", "divide",
    "latest", "window", "collect",
)
STREAM_ADAPTERS = ("latest", "window", "collect")
_MISSING = object()


class OperatorRefused(ValueError):
    pass


class OperatorUnavailable(LookupError):
    pass


def _refuse():
    raise OperatorRefused("Test: operator input refused")


def _rows(value):
    if not isinstance(value, list):
        _refuse()
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse()
    try:
        result = float(value)
    except OverflowError:
        _refuse()
    if not math.isfinite(result):
        _refuse()
    return result


def _count(value=_MISSING):
    if value is _MISSING:
        return MAX_ROWS
    result = _number(value)
    if not 0 <= result <= MAX_ROWS or not result.is_integer():
        _refuse()
    return int(result)


def _truthy(value):
    # Empty arrays and objects are truthy in JavaScript.
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not math.isnan(value)
    return value != "" if isinstance(value, str) else True


def _field(value, path):
    if not isinstance(path, str) or not path:
        _refuse()
    for key in path.split("."):
        if key in ("__proto__", "prototype", "constructor"):
            _refuse()
        if isinstance(value, dict):
            if key not in value:
                return None
            value = value[key]
        elif isinstance(value, list):
            if key == "length":
                value = len(value)
            elif (key.isascii() and key.isdecimal() and (key == "0" or not key.startswith("0"))
                  and len(key) <= 10 and int(key) < len(value)):
                value = value[int(key)]
            else:
                return None
        else:
            return None
    return value


def _compare(a, b):
    a_number = isinstance(a, (int, float)) and not isinstance(a, bool)
    b_number = isinstance(b, (int, float)) and not isinstance(b, bool)
    if a is b or (a_number and b_number and float(a) == float(b)):
        return 0
    if type(a) is type(b) and isinstance(a, (str, bool)) and a == b:
        return 0
    if a is None:
        return -1
    if b is None:
        return 1
    if a_number and b_number:
        a, b = float(a), float(b)
    elif type(a) is type(b) and isinstance(a, (str, bool)):
        if isinstance(a, str):
            a, b = a.encode("utf-16-be", "surrogatepass"), b.encode("utf-16-be", "surrogatepass")
    else:
        _refuse()
    return -1 if a < b else 1


def _predicate(value, settings):
    selected = _field(value, settings.get("field"))
    comparison = settings.get("comparison")
    comparison = comparison if _truthy(comparison) else "eq"
    target = settings.get("value")
    if comparison in ("eq", "ne"):
        equal = canonical_json(selected) == canonical_json(target)
        return equal if comparison == "eq" else not equal
    if comparison not in ("gt", "gte", "lt", "lte"):
        _refuse()
    result = _compare(selected, target)
    return {"gt": result > 0, "gte": result >= 0, "lt": result < 0, "lte": result <= 0}[comparison]


def has_operator(op_id):
    return isinstance(op_id, str) and op_id in OPERATOR_IDS


def run_operator(op_id, input=None, settings=None):
    if not has_operator(op_id):
        raise OperatorUnavailable("Test: operator unavailable in browser")
    input = {} if input is None else input
    settings = {} if settings is None else settings
    value = input.get("value")
    if op_id == "constant":
        return settings.get("value")
    if op_id == "identity":
        return value
    if op_id == "field":
        return _field(value, settings.get("field"))
    if op_id == "select":
        return [_field(row, settings.get("field")) for row in _rows(value)]
    if op_id == "filter":
        return [row for row in _rows(value) if _predicate(row, settings)]
    if op_id == "sort":
        direction = settings.get("direction", _MISSING)
        if direction is not _MISSING and direction not in ("asc", "desc"):
            _refuse()
        sign = -1 if direction == "desc" else 1
        return sorted(_rows(value), key=cmp_to_key(lambda a, b: sign * _compare(
            _field(a, settings.get("field")), _field(b, settings.get("field")))))
    if op_id in ("take", "collect"):
        values = _rows(value)
        return values[:_count(settings.get("count", _MISSING))]
    if op_id == "window":
        size = _count(settings.get("count", _MISSING))
        return [] if size == 0 else _rows(value)[-size:]
    if op_id == "count":
        return len(_rows(value))
    if op_id == "latest":
        values = _rows(value)
        return values[-1] if values else None
    if op_id in ("sum", "mean", "min", "max"):
        values = _rows(value)
        if op_id != "sum" and not values:
            return None
        path = settings.get("field")
        numbers = (_number(_field(row, path) if _truthy(path) else row) for row in values)
        if op_id in ("min", "max"):
            return (min if op_id == "min" else max)(numbers)
        total = 0.0
        for number in numbers:
            total = _number(total + number)
        return total / len(values) if op_id == "mean" else total
    left, right = _number(value), _number(input.get("auxiliary"))
    try:
        if op_id == "add":
            result = left + right
        elif op_id == "subtract":
            result = left - right
        elif op_id == "multiply":
            result = left * right
        else:
            result = left / right
    except (OverflowError, ZeroDivisionError):
        _refuse()
    return _number(result)
