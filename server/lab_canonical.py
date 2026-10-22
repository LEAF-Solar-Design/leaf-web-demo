"""Canonical JSON bytes shared with the browser Lab contract.

Binary64 numbers, ECMAScript decimal thresholds and UTF-16 key ordering.
No normalization, BOM or trailing newline is introduced.
"""
from __future__ import annotations

import hashlib
import json
import math


def canonical_json(value):
    active = set()

    def encode(item):
        if item is None or isinstance(item, bool):
            return json.dumps(item)
        if isinstance(item, (int, float)):
            try:
                number = float(item)
            except OverflowError as error:
                raise ValueError("finite number required") from error
            if not math.isfinite(number):
                raise ValueError("finite number required")
            if number == 0:
                return "0"
            text = next(format(number, f".{precision}g") for precision in range(1, 18)
                        if float(format(number, f".{precision}g")) == number)
            sign = "-" if text.startswith("-") else ""
            mantissa, _, exponent = text.lstrip("-").lower().partition("e")
            whole, _, fraction = mantissa.partition(".")
            digits = whole + fraction
            position = len(whole) + int(exponent or 0)
            leading = len(digits) - len(digits.lstrip("0"))
            digits = digits.lstrip("0").rstrip("0")
            position -= leading
            if 0 < position <= 21:
                result = (digits + "0" * (position - len(digits)) if position >= len(digits)
                          else digits[:position] + "." + digits[position:])
            elif -6 < position <= 0:
                result = "0." + "0" * -position + digits
            else:
                power = position - 1
                result = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
                result += "e" + ("+" if power >= 0 else "") + str(power)
            return sign + result
        if isinstance(item, str):
            item.encode("utf-8", errors="strict")
            return json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if not isinstance(item, (list, dict)) or id(item) in active:
            raise ValueError("JSON required")
        active.add(id(item))
        try:
            if isinstance(item, list):
                return "[" + ",".join(encode(child) for child in item) + "]"
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON keys required")
            keys = sorted(item, key=lambda key: key.encode("utf-16-be"))
            return "{" + ",".join(encode(key) + ":" + encode(item[key]) for key in keys) + "}"
        finally:
            active.remove(id(item))

    return encode(value)


def canonical_bytes(value):
    return canonical_json(value).encode("utf-8")


def graph_digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
