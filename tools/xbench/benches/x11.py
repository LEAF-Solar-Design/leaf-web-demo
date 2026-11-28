"""Score X11's six frozen steps from in-process product observations."""

import re

from benches import x08_metrics
from benches.x11_evidence import (
    UnreadableEvidence,
    changed_paint_ms,
    cold_memberships_match,
    cold_progressive_paint,
    nearest_rank,
    partition,
    solve_frame_p95,
)
from benches.x11_runtime import preflight
from xbench_core import Step


LIMITS = "X11 requires live product observations. Saved or injected samples cannot qualify."
EVIDENCE = "x11-observations.json"
LENA = "lena"
BENSENVILLE = "bensenville-dwg"
MODULES = {LENA: 1394, BENSENVILLE: 2889}
SAVED_LENGTH_KEY = "panels_in_sequence"
SETTINGS_WRITE_METADATA = {"rev", "provenance"}


def _runs(rows, fixture_id):
    if not isinstance(rows, list) or len(rows) != 5:
        raise UnreadableEvidence(f"{fixture_id} requires five runs")
    jobs = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("fixture_id") != fixture_id:
            raise UnreadableEvidence(f"run is not bound to {fixture_id}")
        for key in ("solve_id", "cold_solve_id"):
            job = row.get(key)
            if not isinstance(job, str) or not job.strip() or job in jobs:
                raise UnreadableEvidence(f"missing or reused {key}")
            jobs.add(job)
    return rows


def _baseline(row):
    members = partition(row.get("strings"))
    count = sum(map(len, members))
    lengths = sorted({len(string) for string in members})
    observed = {"panel_count": count, "string_count": len(members),
                "string_lengths": lengths, "controls": row.get("controls"),
                "saved_version": row.get("saved_version")}
    if (not isinstance(observed["controls"], list)
            or not isinstance(observed["saved_version"], str)
            or not observed["saved_version"].strip()):
        raise UnreadableEvidence("missing normal controls or saved drawing version")
    expected = count == MODULES[LENA] and lengths == [17] and len(members) == 82
    return expected and observed["controls"] == ["panels", "groups", "strings", "save"], observed


def _settings_values(snapshot):
    return {key: value for key, value in snapshot.items() if key not in SETTINGS_WRITE_METADATA}


def _edits(rows):
    for index, row in enumerate(rows):
        before, after = row.get("settings_before"), row.get("settings_after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise UnreadableEvidence("missing settings at UI commit")
        before, after = _settings_values(before), _settings_values(after)
        if (type(before.get(SAVED_LENGTH_KEY)) is not int
                or type(after.get(SAVED_LENGTH_KEY)) is not int):
            raise UnreadableEvidence("missing integer maximum string length")
        changes = {key for key in before.keys() | after.keys()
                   if key not in before or key not in after or before[key] != after[key]}
        source, target = (17, 16) if index % 2 == 0 else (16, 17)
        if (changes != {SAVED_LENGTH_KEY}
                or (before[SAVED_LENGTH_KEY], after[SAVED_LENGTH_KEY]) != (source, target)):
            return False
    return True


def _latency(rows, p95_lt, p50_lt=None):
    values = [changed_paint_ms(row.get("before"), row.get("incremental"),
                               row.get("commit_ms"), row.get("frames"), row.get("drawing_key"))
              for row in rows]
    if None in values:
        return False, {"samples_ms": values, "p50_ms": None, "p95_ms": None}
    p50, p95 = nearest_rank(values, 50), nearest_rank(values, 95)
    return (p95 < p95_lt and (p50_lt is None or p50 < p50_lt),
            {"samples_ms": values, "p50_ms": p50, "p95_ms": p95})


def _continuous_membership(rows, initial):
    previous = partition(initial)
    for row in rows:
        if partition(row.get("before")) != previous:
            return False
        previous = partition(row.get("incremental"))
    return True


def _oracle(rows, fixture_id):
    matches = []
    for row in rows:
        settings, cold_settings = row.get("settings_after"), row.get("cold_settings")
        if not isinstance(settings, dict) or not isinstance(cold_settings, dict):
            raise UnreadableEvidence("missing settings for the cold comparison")
        same_members = cold_memberships_match(row.get("incremental"), row.get("cold"),
                                              row["solve_id"], row["cold_solve_id"])
        count = sum(map(len, partition(row["incremental"])))
        matches.append(_settings_values(settings) == _settings_values(cold_settings)
                       and same_members and count == MODULES[fixture_id])
    return matches


def _step(n, expected, evaluate):
    try:
        passed, observed = evaluate()
    except UnreadableEvidence as exc:
        return Step(n, "UNREADABLE", EVIDENCE, {"reason": str(exc)}, expected)
    if passed is None:
        return Step(n, "UNREADABLE", EVIDENCE, observed, expected)
    return Step(n, "PASS" if passed else "FAIL", EVIDENCE, observed, expected)


def _responsiveness(row):
    if type(row.get("visible_progress")) is not bool:
        raise UnreadableEvidence("missing foreground progress observation")
    limit = solve_frame_p95(row.get("raf_ticks_ms"), row.get("start_ms"), row.get("end_ms"))
    return {"frame_p95_ms": limit, "visible_progress": row["visible_progress"]}


def _cancel(row):
    outcome = row.get("terminal_state")
    solve_id = row.get("cancelled_solve_id")
    version_before, version_after = row.get("version_before"), row.get("version_after")
    if not all(isinstance(value, str) and value.strip()
               for value in (outcome, solve_id, version_before, version_after)):
        raise UnreadableEvidence("missing cancellation identity or terminal state")
    previous, current = partition(row.get("before")), partition(row.get("after"))
    if not previous:
        raise UnreadableEvidence("missing last valid solve")
    return outcome == "cancelled" and previous == current and version_before == version_after


def _object(observations, key):
    result = observations.get(key)
    if not isinstance(result, dict):
        raise UnreadableEvidence(f"missing {key} observation")
    return result


def score_live_observations(observations, source_sha):
    """Score the runtime's raw observations, without accepting a receipt as input."""
    if (not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
            or not isinstance(observations, dict) or observations.get("source_sha") != source_sha):
        return [Step(n, "UNREADABLE", EVIDENCE, {"reason": "missing or mismatched build SHA"})
                for n in range(1, 7)]

    def lena():
        return _runs(observations.get(LENA), LENA)

    def timing():
        reference = x08_metrics.score({"reference": observations.get("reference")}, 0)[5]
        if reference.verdict == "UNREADABLE":
            raise UnreadableEvidence("missing X08 reference desktop metadata")
        rows = lena()
        continuous = _continuous_membership(rows, _object(observations, "baseline").get("strings"))
        within_budget, measured = _latency(rows, 5000, 2000)
        measured["reference_verdict"] = reference.verdict
        measured["continuous_membership"] = continuous
        return within_budget and continuous and reference.verdict == "PASS", measured

    def memberships():
        matches = _oracle(lena(), LENA)
        return all(matches), {"cold_matches": matches}

    def large():
        rows = _runs(observations.get(BENSENVILLE), BENSENVILLE)
        within_budget, measured = _latency(rows, 15000)
        matches = _oracle(rows, BENSENVILLE)
        measured["cold_matches"] = matches
        measured["alternating_edit"] = _edits(rows)
        return within_budget and all(matches) and measured["alternating_edit"], measured

    def responsiveness():
        frame_rows = [_responsiveness(row) for row in lena()]
        cancelled = _cancel(_object(observations, "cancel"))
        cold = _object(observations, "cold_lena")
        painted = cold_progressive_paint(cold.get("before"), cold.get("after"),
                                        cold.get("request_ms"), cold.get("groups"),
                                        cold.get("frames"), cold.get("drawing_key"))
        observed = {"resolves": frame_rows, "cancel_preserved": cancelled, "cold": painted}
        if painted["progressive"] is None:
            return None, observed
        ready = (all(row["frame_p95_ms"] < 33 and row["visible_progress"] for row in frame_rows)
                 and cancelled and painted["progressive"] and painted["first_paint_ms"] is not None
                 and painted["first_paint_ms"] <= 10000
                 and sum(map(len, partition(cold.get("after")))) == MODULES[LENA])
        return ready, observed

    return [
        _step(1, {"panel_count": 1394, "strings": 82, "length": 17, "saved": True},
              lambda: _baseline(_object(observations, "baseline"))),
        _step(2, {"maximum_string_length": [16, 17, 16, 17, 16], "other_changes": 0},
              lambda: (_edits(lena()), {"runs": 5})),
        _step(3, {"p50_ms_lt": 2000, "p95_ms_lt": 5000, "reference_desktop": "X08"}, timing),
        _step(4, {"cold_matches": 5}, memberships),
        _step(5, {"panels": 2889, "p95_ms_lt": 15000, "cold_matches": 5}, large),
        _step(6, {"frame_p95_ms_lt": 33, "progress": True, "cancel_preserves_result": True,
                  "cold_first_paint_ms_lte": 10000, "cold_progressive": True}, responsiveness),
    ]


def run(ctx):
    """Fail closed until the live product collector is wired to the grader."""
    try:
        preflight(ctx)
    except UnreadableEvidence as exc:
        reason = str(exc)
    else:
        reason = "Live browser capture of painted membership is unavailable"
    return [Step(n, "UNREADABLE", LIMITS, {"reason": reason}) for n in range(1, 7)]
