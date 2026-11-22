"""Pure scoring of X08 interaction evidence against the frozen budgets."""

from math import isfinite

from xbench_core import Step


def _positive(value):
    return ((type(value) is int and value > 0)
            or (type(value) is float and isfinite(value) and value > 0))


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _handles(value):
    return (isinstance(value, list) and all(_text(item) for item in value)
            and len(set(value)) == len(value))


def _timing(values):
    """Keep the supplied count, but never compute a percentile of invalid data."""
    observed = {"count": len(values) if isinstance(values, list) else None,
                "p95_ms": None}
    valid = isinstance(values, list) and bool(values) and all(
        _positive(value) for value in values)
    if valid:
        observed["p95_ms"] = sorted(values)[(95 * len(values) + 99) // 100 - 1]
    return observed, valid


def _budget(observed, valid, minimum, budget, cold):
    if not valid or observed["count"] < minimum or cold is not True:
        return "UNREADABLE"
    return "PASS" if observed["p95_ms"] < budget else "FAIL"


def score(samples, expected_modules, evidence="samples.json") -> list[Step]:
    """Return six steps without collecting, repairing, or inventing evidence."""
    samples = samples if isinstance(samples, dict) else {}
    steps = []
    geometry = samples.get("geometry")
    keys = ("rendered_module_count", "marker_module_count", "omitted_module_count")
    expected = dict(zip(keys, (expected_modules, 0, 0)))
    if (not isinstance(geometry, dict)
            or not all(type(geometry.get(key)) is int for key in keys)
            or type(expected_modules) is not int or expected_modules < 0):
        verdict = "UNREADABLE"
    else:
        verdict = "PASS" if all(geometry[key] == expected[key] for key in keys) else "FAIL"
    steps.append(Step(1, verdict, evidence, geometry, expected))

    cold = samples.get("cold_run_discarded")
    observed, valid = _timing(samples.get("single_select_ms"))
    observed["cold_run_discarded"] = cold
    steps.append(Step(2, _budget(observed, valid, 20, 100, cold), evidence, observed,
                      {"minimum_count": 20, "p95_ms_lt": 100,
                       "finite_positive_ms": True, "cold_run_discarded": True}))

    rows = samples.get("window_select")
    row_list = isinstance(rows, list)
    rows = rows if row_list else []
    observed, valid = _timing([
        row.get("latency_ms") if isinstance(row, dict) else None for row in rows
    ] if row_list else None)
    oracle_valid = bool(rows) and all(
        isinstance(row, dict) and row.get("oracle_independent") is True
        and _handles(row.get("expected_handles"))
        and 490 <= len(row["expected_handles"]) <= 510
        and _handles(row.get("selected_handles")) for row in rows)
    sets_match = oracle_valid and all(
        set(row["expected_handles"]) == set(row["selected_handles"]) for row in rows)
    observed.update(cold_run_discarded=cold, oracle_valid=oracle_valid,
                    selected_sets_match=sets_match if oracle_valid and rows else None)
    verdict = _budget(observed, valid and oracle_valid, 5, 100, cold)
    if verdict != "UNREADABLE" and not sets_match:
        verdict = "FAIL"
    steps.append(Step(3, verdict, evidence, observed,
                      {"minimum_count": 5, "p95_ms_lt": 100,
                       "finite_positive_ms": True, "cold_run_discarded": True,
                       "oracle_independent": True, "expected_handle_count": [490, 510],
                       "unique_nonempty_handles": True, "selected_set_equals_oracle": True}))

    rows = samples.get("job_ack")
    row_list = isinstance(rows, list)
    rows = rows if row_list else []
    observed, valid = _timing([
        row.get("latency_ms") if isinstance(row, dict) else None for row in rows
    ] if row_list else None)
    ack_valid = bool(rows) and all(isinstance(row, dict) and type(row.get("http_status")) is int
                    and type(row.get("rendered")) is bool for row in rows)
    ack_match = ack_valid and all(row["http_status"] == 202 and row["rendered"] is True
                                 for row in rows)
    observed.update(cold_run_discarded=cold,
                    acknowledgements_match=ack_match if ack_valid and rows else None)
    verdict = _budget(observed, valid and ack_valid, 10, 200, cold)
    if verdict != "UNREADABLE" and not ack_match:
        verdict = "FAIL"
    steps.append(Step(4, verdict, evidence, observed,
                      {"minimum_count": 10, "p95_ms_lt": 200,
                       "finite_positive_ms": True, "cold_run_discarded": True,
                       "http_status": 202, "rendered": True}))

    sequence = samples.get("sequence")
    counts = {"pan_drags": 4, "zoom_in_steps": 10, "zoom_out_steps": 10}
    sequence_valid = (isinstance(sequence, dict)
                      and all(type(sequence.get(key)) is int for key in counts)
                      and _positive(sequence.get("duration_ms")))
    sequence_match = sequence_valid and all(sequence[key] == value for key, value in counts.items())
    if sequence_valid:
        sequence_match = sequence_match and sequence["duration_ms"] >= 10000
    observed, valid = _timing(samples.get("frame_deltas_ms"))
    observed.update(cold_run_discarded=cold, sequence=sequence)
    verdict = _budget(observed, valid and sequence_valid, 600, 33, cold)
    if verdict != "UNREADABLE" and not sequence_match:
        verdict = "FAIL"
    steps.append(Step(5, verdict, evidence, observed,
                      {"minimum_count": 600, "p95_ms_lt": 33,
                       "finite_positive_ms": True, "cold_run_discarded": True,
                       "sequence": dict(counts, duration_ms_gte=10000)}))

    reference = samples.get("reference")
    expected = {"hostname": "EVANS-DEKSTOP", "hostname_case_insensitive": True,
                "os_contains_after_space_normalization": "Windows11",
                "cpu": "nonempty string", "gpu": "nonempty list of nonempty strings",
                "free_ram_bytes": "positive integer", "chromium_version": "nonempty string",
                "headed": True, "viewport": {"width": 1920, "height": 1080},
                "device_scale_factor": 1}
    reference_valid = isinstance(reference, dict) and all(
        _text(reference.get(key)) for key in ("hostname", "os", "cpu", "chromium_version"))
    if reference_valid:
        viewport = reference.get("viewport")
        reference_valid = (
            isinstance(reference.get("gpu"), list) and bool(reference["gpu"])
            and all(_text(gpu) for gpu in reference["gpu"])
            and type(reference.get("free_ram_bytes")) is int
            and _positive(reference["free_ram_bytes"])
            and type(reference.get("headed")) is bool
            and isinstance(viewport, dict)
            and all(type(viewport.get(key)) is int and _positive(viewport[key])
                    for key in ("width", "height"))
            and _positive(reference.get("device_scale_factor")))
    if not reference_valid:
        verdict = "UNREADABLE"
    else:
        matches = (reference["hostname"].casefold() == "evans-dekstop"
                   and "Windows11" in "".join(reference["os"].split())
                   and reference["headed"] is True
                   and reference["viewport"]["width"] == 1920
                   and reference["viewport"]["height"] == 1080
                   and reference["device_scale_factor"] == 1)
        verdict = "PASS" if matches else "FAIL"
    steps.append(Step(6, verdict, evidence, reference, expected))
    return steps
