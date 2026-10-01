"""sf-graph-date-time-deterministic: the design graph validator decides RFC 3339 date-time itself.

jsonschema registers its own date-time checker only when the optional rfc3339_validator package
imports, so a host without that package used to accept a malformed provenance created_at that
another host refused. The graph validator now carries one module-local rule
(solar_design_graph.is_rfc3339_date_time) on a FormatChecker that knows only date-time, and the
Ground conversion kernel uses the same rule. Every expected value was measured with python -B.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_design_graph as sdg  # noqa: E402
import solar_design_profiles as profiles  # noqa: E402
import solar_ground_conversion as conv  # noqa: E402
import store  # noqa: E402  (da/ is on sys.path once solar_ground_conversion is imported)
from test_solar_ground_conversion import convert, ground_base, provenance  # noqa: E402
from test_w1_design_graph import graph  # noqa: E402,F401
from test_w1_graph_seed import empty_graph  # noqa: E402

GOOD = "2026-10-01T10:00:00Z"
FRACTION_4096 = "2026-10-01T10:00:00." + "1" * 4075 + "Z"   # 4096 characters, the schema bound
FRACTION_4097 = "2026-10-01T10:00:00." + "1" * 4076 + "Z"   # one past it

ACCEPTED = [
    "2026-09-17T00:00:00Z",
    GOOD,
    "2026-10-01t10:00:00z",
    "2026-10-01T10:00:00z",
    "2026-10-01t10:00:00Z",
    "2024-02-29T23:59:59.123+05:30",
    "2000-02-29T00:00:00Z",
    "2026-10-01T12:34:56.123456+00:00",
    "2026-10-01T12:34:56+00:00",
    "2026-10-01T12:34:56-00:00",
    "2026-10-01T12:34:56.1Z",
    "2026-10-01T12:34:56.123456Z",
    "2026-10-01T12:34:56.123456789012345678Z",
    "2026-10-01T23:59:59-23:59",
    "2026-10-01T00:00:00+23:59",
    "0001-01-01T00:00:00Z",
    "9999-12-31T23:59:59Z",
    FRACTION_4096,
]

REFUSED = [
    "",
    "oops",
    "2026-10-01",
    "2026-10-01T10:00:00",
    "2026-10-01 10:00:00Z",
    "2026-10-01T10:00Z",
    "20261001T100000Z",
    "0000-01-01T00:00:00Z",
    "2026-00-01T00:00:00Z",
    "2026-13-01T00:00:00Z",
    "2026-10-00T00:00:00Z",
    "2026-10-32T00:00:00Z",
    "2026-04-31T00:00:00Z",
    "2026-02-29T00:00:00Z",
    "2026-02-30T00:00:00Z",
    "1900-02-29T00:00:00Z",
    "2026-10-01T24:00:00Z",
    "2026-10-01T10:60:00Z",
    "2026-10-01T10:00:60Z",
    "2026-12-31T23:59:60Z",
    "2026-10-01T10:00:00+24:00",
    "2026-10-01T10:00:00+00:60",
    "2026-10-01T10:00:00+0000",
    "2026-10-01T10:00:00+00",
    "2026-10-01T10:00:00+00:00:00",
    "2026-10-01T10:00:00.123456+00:00:30",
    "2026-10-01T10:00:00.Z",
    "2026-10-01T10:00:00,5Z",
    "2026-10-01T10:00:00ZZ",
    "2026-10-01T10:00:00Z+00:00",
    "2026-10-01T1:00:00Z",
    "2026-1-01T10:00:00Z",
    "226-10-01T10:00:00Z",
    "12026-10-01T10:00:00Z",
    "-2026-10-01T10:00:00Z",
    "+2026-10-01T10:00:00Z",
    " 2026-10-01T10:00:00Z",
    "2026-10-01T10:00:00Z ",
    "\n2026-10-01T10:00:00Z",
    "2026-10-01T10:00:00Z\n",
    "2026-10-01T10:00:00+00:00\n",
    "2026-10-01T10:00:00Z\r\n",
    "2026-10-01T10:00:00\x00Z",
    "2026-10-01T10:00:00Z\x00",
    "٢٠٢٦-10-01T10:00:00Z",      # Arabic-Indic digits
    "２026-10-01T10:00:00Z",                     # a fullwidth digit
    "2026–10-01T10:00:00Z",                     # an en dash for the hyphen
    "2026-10-01T10:00:00ſ",                     # long s, whose upper case is S
    "2026-10-01ẗ" + "10:00:00Z",                # t with diaeresis, whose upper case starts with T
]

TABLE_SHA256 = "18d4cce5420ca26b9b3d50fbf8ef2a1a67d69e94744aff5d304deb29e0c02e8b"
MUTATION_COUNTS = {"compared": 2675, "accepted": 204}

# The rule the conversion kernel carried before this change, equal to jsonschema's own checker on a
# host that has rfc3339_validator: validate_rfc3339(value.upper()).
_OLD = re.compile(
    r"^(\d{4})-(0[1-9]|1[0-2])-(\d{2})T(?:[01]\d|2[0123]):(?:[0-5]\d):(?:[0-5]\d)"
    r"(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0123]):[0-5]\d)$", re.ASCII)


def old_rule(value):
    match = _OLD.match(value.upper())
    if match is None:
        return False
    year, month, day = map(int, match.groups())
    return bool(year) and 1 <= day <= calendar.monthrange(year, month)[1]


def w1():
    return graph.__wrapped__()


def graph_code(base, value, place=None):
    """'ok' or the GraphValidationError code for `base` with one created_at replaced."""
    g = deepcopy(base)
    (place(g) if place else g["project"])["provenance"]["created_at"] = value
    sdg._reset_validation_caches()
    try:
        assert sdg.validate_graph(g) == g
    except sdg.GraphValidationError as error:
        return error.code
    return "ok"


def conversion_code(base, value):
    """'ok' or the GroundConversionError code for a conversion whose provenance carries `value`."""
    prov = provenance()
    prov["created_at"] = value
    snapshot = deepcopy(prov)
    try:
        result = convert(base, prov=prov)
    except conv.GroundConversionError as error:
        assert prov == snapshot
        return error.code
    assert result["counts"] == {"trackers": 2, "slots": 5}
    return "ok"


def table_digest():
    """sha256 of the canonical JSON of {spelling: [rule, graph, conversion]} over every spelling."""
    base = w1()
    table = {value: [sdg.is_rfc3339_date_time(value), graph_code(base, value), conversion_code(base, value)]
             for value in ACCEPTED + REFUSED + [FRACTION_4097]}
    canonical = json.dumps(table, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ids(values):
    return [ascii(value) if len(value) < 48 else f"len-{len(value)}" for value in values]


@pytest.mark.parametrize("value", ACCEPTED, ids=ids(ACCEPTED))
def test_graph_date_time_accepted(graph, value):
    assert sdg.is_rfc3339_date_time(value) is True
    assert graph_code(graph, value) == "ok"
    assert conversion_code(graph, value) == "ok"


@pytest.mark.parametrize("value", REFUSED, ids=ids(REFUSED))
def test_graph_date_time_refused(graph, value):
    assert sdg.is_rfc3339_date_time(value) is False
    assert graph_code(graph, value) == "INVALID_GRAPH_SCHEMA"
    assert conversion_code(graph, value) == "GROUND_CONVERSION_INPUT_INVALID"


def test_graph_date_time_length_bound_is_the_schema(graph):
    assert len(FRACTION_4096) == 4096 and len(FRACTION_4097) == 4097
    # The rule itself has no length bound; the schema's maxLength (4096) and the kernel's MAX_TEXT do.
    assert sdg.is_rfc3339_date_time(FRACTION_4096) is True
    assert sdg.is_rfc3339_date_time(FRACTION_4097) is True
    assert graph_code(graph, FRACTION_4096) == "ok"
    assert graph_code(graph, FRACTION_4097) == "INVALID_GRAPH_SCHEMA"
    assert conversion_code(graph, FRACTION_4096) == "ok"
    assert conversion_code(graph, FRACTION_4097) == "GROUND_CONVERSION_INPUT_INVALID"


def test_graph_date_time_non_strings(graph):
    class Text(str):
        pass

    for value in (None, 0, 1, 1.5, True, False, GOOD.encode("ascii"), [GOOD], {"value": GOOD}, (GOOD,), Text(GOOD)):
        assert sdg.is_rfc3339_date_time(value) is False
    # A JSON Schema format constrains strings only: the checker passes everything else to `type`.
    for value in (None, 0, 1.5, True, [], {}):
        assert sdg._date_time_format(value) is True
        assert graph_code(graph, value) == "INVALID_GRAPH_SCHEMA"
    assert sdg._date_time_format(GOOD) is True
    assert sdg._date_time_format("oops") is False
    assert sdg._date_time_format(Text(GOOD)) is False
    assert sdg.is_rfc3339_date_time("\ud800") is False
    assert sdg.is_rfc3339_date_time("9" * 1_000_000) is False


def test_graph_date_time_every_entity(graph):
    kinds = [entity["kind"] for entity in sdg.entities(graph)]
    assert kinds == ["project", "settings", "zone-el", "frame", "panel", "panel", "panel", "string", "string",
                     "inverter", "route", "schedule"]
    for index in range(len(kinds)):
        assert graph_code(graph, GOOD, lambda g, i=index: sdg.entities(g)[i]) == "ok"
        assert graph_code(graph, "oops", lambda g, i=index: sdg.entities(g)[i]) == "INVALID_GRAPH_SCHEMA"
    compact = ground_base(graph)
    compact["frames"] = convert(graph)["frames"]
    assert graph_code(compact, GOOD, lambda g: g["frames"][0]) == "ok"
    assert graph_code(compact, "oops", lambda g: g["frames"][0]) == "INVALID_GRAPH_SCHEMA"
    # The slot template every compact panel shares sits under the frame's ground_slots.
    assert graph_code(compact, GOOD, lambda g: g["frames"][1]["ground_slots"]["panel"]) == "ok"
    assert graph_code(compact, "oops", lambda g: g["frames"][1]["ground_slots"]["panel"]) == "INVALID_GRAPH_SCHEMA"


def test_graph_date_time_checker_knows_one_format():
    first, second = sdg.graph_format_checker(), sdg.graph_format_checker()
    assert first is not second and first.checkers is not second.checkers
    assert sorted(first.checkers) == ["date-time"]
    assert first.checkers["date-time"] == (sdg._date_time_format, ())
    assert first.conforms(GOOD, "date-time") is True
    assert first.conforms("oops", "date-time") is False
    assert first.conforms(7, "date-time") is True
    # No other format is decided here, with or without an optional package.
    for name in ("date", "time", "email", "uri", "uuid", "ipv4", "regex", "duration"):
        assert first.conforms("oops", name) is True


def test_graph_date_time_validators_use_the_graph_checker(monkeypatch):
    sdg._reset_validation_caches()
    units, full = sdg._schema_validators()
    assert units.format_checker is None
    assert sorted(full.format_checker.checkers) == ["date-time"]
    assert full.format_checker.checkers["date-time"][0] is sdg._date_time_format
    monkeypatch.setattr(conv, "_PROVENANCE_VALIDATOR", None)
    validator = conv._provenance_validator()
    assert sorted(validator.format_checker.checkers) == ["date-time"]
    assert validator.format_checker.checkers["date-time"][0] is sdg._date_time_format
    assert validator.format_checker is not full.format_checker
    bad = provenance()
    bad["created_at"] = "oops"
    assert validator.is_valid(provenance()) is True
    assert validator.is_valid(bad) is False
    # One rule, one owner: the kernel keeps no copy of its own.
    assert not hasattr(conv, "_rfc3339") and not hasattr(conv, "_RFC3339") and not hasattr(conv, "calendar")
    sdg._reset_validation_caches()


def test_graph_date_time_schema_names_only_date_time():
    found = []
    stack = [("", sdg.load_schema())]
    while stack:
        path, value = stack.pop()
        if type(value) is dict:
            if type(value.get("format")) is str:
                found.append((path, value["format"]))
            stack.extend((f"{path}/{key}", child) for key, child in value.items())
        elif type(value) is list:
            stack.extend((f"{path}/{index}", child) for index, child in enumerate(value))
    # A second format keyword in the graph schema needs its own checker in graph_format_checker.
    assert found == [("/$defs/provenance/properties/created_at", "date-time")]


def test_graph_date_time_matches_the_kernel_rule():
    alphabet = "0159:+-.TtZz \n"
    values = set(ACCEPTED[:-1] + REFUSED)
    for base in (GOOD, "2024-02-29T23:59:59.123+05:30", "2026-10-01t12:34:56-00:00", "0001-01-01T00:00:00z"):
        for index in range(len(base) + 1):
            values.add(base[:index] + base[index + 1:])
            for char in alphabet:
                values.add(base[:index] + char + base[index:])
                values.add(base[:index] + char + base[index + 1:])
    accepted = 0
    for value in sorted(values):
        expected = old_rule(value) and not value.endswith("\n")
        assert sdg.is_rfc3339_date_time(value) is expected, ascii(value)
        accepted += expected
    assert {"compared": len(values), "accepted": accepted} == MUTATION_COUNTS
    # The one deliberate difference: the old rule's `$` let one trailing line feed through.
    assert old_rule(GOOD + "\n") is True and sdg.is_rfc3339_date_time(GOOD + "\n") is False


def test_graph_date_time_product_writers():
    rule = sdg.is_rfc3339_date_time
    assert rule(datetime.now(timezone.utc).isoformat()) is True          # the builtins and the importers
    assert rule(store._now_iso()) is True                                # the file store's version clock
    moment = datetime(2026, 10, 1, 12, 34, 56, tzinfo=timezone.utc)
    assert moment.isoformat() == "2026-10-01T12:34:56+00:00" and rule(moment.isoformat()) is True
    assert rule(moment.replace(microsecond=1).isoformat()) is True
    assert rule(datetime(1, 1, 1, tzinfo=timezone.utc).isoformat()) is True
    assert rule(datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc).isoformat()) is True
    assert profiles.format_created_utc(moment) == "2026-10-01T12:34:56Z"
    assert rule(profiles.format_created_utc(moment.replace(microsecond=123400))) is True
    # PostgreSQL TIMESTAMPTZ arrives as an aware datetime in the session zone: every whole-minute offset.
    written = 0
    for minutes in range(-1439, 1440):
        zone = timezone(timedelta(minutes=minutes))
        for stamp in (moment, moment.replace(microsecond=678901)):
            assert rule(stamp.astimezone(zone).isoformat()) is True, minutes
            written += 1
    assert written == 5758
    # What Python can write and the product never does: no offset, and an offset with seconds.
    assert rule(datetime(2026, 10, 1, 12, 0, 0).isoformat()) is False
    odd = timezone(timedelta(hours=5, minutes=30, seconds=15))
    assert datetime(2026, 10, 1, 12, 0, 0, tzinfo=odd).isoformat() == "2026-10-01T12:00:00+05:30:15"
    assert rule(datetime(2026, 10, 1, 12, 0, 0, tzinfo=odd).isoformat()) is False


def test_graph_date_time_seed_created_at():
    stamp = store._now_iso()
    seeded = empty_graph(created_at=stamp)
    assert seeded["project"]["provenance"]["created_at"] == stamp
    assert seeded["settings"]["provenance"]["created_at"] == stamp
    for value in ("oops", "2026-10-01", GOOD + "\n"):
        with pytest.raises(sdg.GraphValidationError) as error:
            empty_graph(created_at=value)
        assert error.value.code == "INVALID_GRAPH_SCHEMA"


def test_graph_date_time_refusal_is_never_memoized(graph):
    sdg._reset_validation_caches()
    assert sdg.validate_graph(graph) == graph
    assert sdg.validate_graph(graph) == graph           # the memo hit
    bad = deepcopy(graph)
    bad["project"]["provenance"]["created_at"] = "oops"
    for _ in range(2):
        with pytest.raises(sdg.GraphValidationError) as error:
            sdg.validate_graph(bad)
        assert error.value.code == "INVALID_GRAPH_SCHEMA"
    sdg._reset_validation_caches()


def test_graph_date_time_table_digest():
    assert table_digest() == TABLE_SHA256


HOSTS = {
    # rfc3339_validator cannot be imported: jsonschema registers no date-time checker at all.
    "package-absent": ("import sys; sys.modules['rfc3339_validator'] = None", False),
    # A package that accepts everything, and one that refuses everything: neither is ever consulted.
    "package-accepts-all": (
        "import sys, types; fake = types.ModuleType('rfc3339_validator'); "
        "fake.validate_rfc3339 = lambda value: True; sys.modules['rfc3339_validator'] = fake", True),
    "package-refuses-all": (
        "import sys, types; fake = types.ModuleType('rfc3339_validator'); "
        "fake.validate_rfc3339 = lambda value: False; sys.modules['rfc3339_validator'] = fake", True),
}


@pytest.mark.parametrize("host", sorted(HOSTS))
def test_graph_date_time_hosts_agree(host):
    setup, default_has_date_time = HOSTS[host]
    program = (
        f"{setup}\n"
        "import json\n"
        "from jsonschema import FormatChecker\n"
        "sys.path.insert(0, 'tests')\n"
        "import test_solar_graph_date_time as rows\n"
        "print(json.dumps({'default': 'date-time' in FormatChecker().checkers, 'digest': rows.table_digest()}))\n"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-B", "-c", program], cwd=str(SERVER), env=env,
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    # The simulated host really is the host it claims to be, and the table is the same on it.
    assert report == {"default": default_has_date_time, "digest": TABLE_SHA256}
