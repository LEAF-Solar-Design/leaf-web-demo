"""Membership and browser timing rules for the frozen X11 benchmark."""

class UnreadableEvidence(ValueError):
    """A sample is absent, malformed, or bound to the wrong observation."""


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _time(value):
    if type(value) not in (int, float) or not 0 <= value < float("inf"):
        raise UnreadableEvidence("invalid browser timestamp")
    return value


def partition(rows):
    """Canonicalize a partition by panel identity, ignoring string IDs and order."""
    if not isinstance(rows, list):
        raise UnreadableEvidence("missing string membership")
    panels = set()
    strings = set()
    for row in rows:
        if not isinstance(row, list) or not row or not all(map(_text, row)):
            raise UnreadableEvidence("empty or invalid panel membership")
        members = frozenset(row)
        if len(members) != len(row) or not panels.isdisjoint(members):
            raise UnreadableEvidence("panel appears more than once")
        panels.update(members)
        strings.add(members)
    return frozenset(strings)


def nearest_rank(samples, percentile):
    """Use X08's nearest-rank percentile convention."""
    if not samples or any(
        type(sample) not in (int, float) or not 0 < sample < float("inf")
        for sample in samples
    ):
        raise UnreadableEvidence("missing or invalid timing samples")
    return sorted(samples)[(percentile * len(samples) + 99) // 100 - 1]


def _frames(rows, drawing_key):
    if not isinstance(rows, list) or not rows or not _text(drawing_key):
        raise UnreadableEvidence("missing renderer frames or drawing key")
    previous = -1
    for row in rows:
        if not isinstance(row, dict) or row.get("drawing_key") != drawing_key:
            raise UnreadableEvidence("renderer frame has the wrong drawing identity")
        now = _time(row.get("at_ms"))
        if now <= previous or row.get("visible") is not True:
            raise UnreadableEvidence("renderer frames must be ordered and visible")
        previous = now
        yield now, partition(row.get("strings"))


def changed_paint_ms(before, result, commit_ms, frames, drawing_key):
    """Find one post-commit frame containing all changed result strings."""
    previous, final = partition(before), partition(result)
    changed = final - previous
    if not previous or not final or not changed:
        raise UnreadableEvidence("no changed result strings to time")
    commit = _time(commit_ms)
    first = None
    # Validate the complete capture even after finding a candidate frame.
    for now, painted in _frames(frames, drawing_key):
        if first is None and now > commit and changed <= painted <= final:
            first = now - commit
    return first


def cold_memberships_match(incremental, cold, incremental_run, cold_run):
    """Compare memberships from distinct solve invocations."""
    if not all(map(_text, (incremental_run, cold_run))) or incremental_run == cold_run:
        raise UnreadableEvidence("missing independent cold solve identity")
    result, oracle = partition(incremental), partition(cold)
    if not result or not oracle:
        raise UnreadableEvidence("empty solved string membership")
    return result == oracle


def solve_frame_p95(ticks, start_ms, end_ms):
    """Measure intervals overlapping a solve, including gaps at its boundaries."""
    start, end = _time(start_ms), _time(end_ms)
    if end <= start or not isinstance(ticks, list) or len(ticks) < 3:
        raise UnreadableEvidence("missing browser frame coverage")
    ordered = [_time(tick) for tick in ticks]
    if ordered[0] > start or ordered[-1] < end:
        raise UnreadableEvidence("browser frames do not bracket the solve")
    intervals = []
    for left, right in zip(ordered, ordered[1:]):
        if right <= left:
            raise UnreadableEvidence("browser timestamps are out of order")
        if left < end and right > start:
            intervals.append(right - left)
    return nearest_rank(intervals, 95)


def _completion_batches(groups, request, result):
    if not isinstance(groups, list) or not groups:
        raise UnreadableEvidence("missing timed group completions")
    batches = []
    completed = set()
    previous = request
    for group in groups:
        if not isinstance(group, dict):
            raise UnreadableEvidence("invalid group completion")
        at = _time(group.get("at_ms"))
        members = partition(group.get("strings"))
        if at < previous or not members or not completed.isdisjoint(members):
            raise UnreadableEvidence("group completions are disordered or overlap")
        batches.append((at, members))
        completed.update(members)
        previous = at
    if completed != result:
        raise UnreadableEvidence("group completions do not account for the cold result")
    return batches


def cold_progressive_paint(initial, final, request_ms, groups, frames, drawing_key):
    """Measure cold paint from frames and assess progression with completion evidence."""
    starting, result = partition(initial), partition(final)
    request = _time(request_ms)
    if not result:
        raise UnreadableEvidence("missing cold solve result")

    first, full = None, None
    first_paint = {}
    for now, painted in _frames(frames, drawing_key):
        if now < request:
            continue
        for string in painted & result:
            first_paint.setdefault(string, now)
        if first is None and painted and painted <= result:
            first = now - request
        if full is None and painted == result:
            full = now - request

    observed = {"first_paint_ms": first, "full_paint_ms": full, "progressive": None}
    # The latest preview response may skip completed groups. Its arrival
    # cannot timestamp an individual producer completion.
    try:
        batches = _completion_batches(groups, request, result)
    except UnreadableEvidence as exc:
        observed["progression_reason"] = str(exc)
        return observed
    observed["progressive"] = (
        not starting and len(batches) > 1 and full is not None
        and all(
            all(at <= first_paint.get(string, float("inf")) < next_at for string in members)
            for (at, members), (next_at, _) in zip(batches, batches[1:])
        )
        and all(first_paint.get(string, -1) >= batches[-1][0] for string in batches[-1][1])
    )
    return observed
