"""STD-9 sentences and metadata from the frozen Lab fixture."""
import json
from pathlib import Path


def _read_codes():
    try:
        path = Path(__file__).resolve().parents[1] / "contract/fixtures/lab/leaf-lab-honesty.v1.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        rows = fixture["codes"]
        if (fixture["schema"] != "leaf.lab-honesty.v1" or not isinstance(rows, list)
                or len(rows) != 13 or any(
                    not isinstance(row, dict) or not isinstance(row.get("code"), str)
                    or not row["code"] or not isinstance(row.get("sentence"), str)
                    or not isinstance(row.get("scope"), str)
                    or type(row.get("withholds_result")) is not bool for row in rows)):
            raise ValueError("lab_honesty_invalid")
        codes = {row["code"]: (row["sentence"], row["withholds_result"], row["scope"]) for row in rows}
        if len(codes) != 13:
            raise ValueError("lab_honesty_invalid")
        return codes
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError("lab_honesty_invalid") from error


_CODES = _read_codes()
HONESTY_CODES: tuple[str, ...] = tuple(_CODES)


def _lookup(code: str):
    if not isinstance(code, str) or code not in _CODES:
        raise ValueError("unknown_code")
    return _CODES[code]


def sentence(code: str) -> str:
    return _lookup(code)[0]


def withholds(code: str) -> bool:
    return _lookup(code)[1]


def scope_of(code: str) -> str:
    return _lookup(code)[2]
