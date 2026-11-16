"""Fail-closed offline adapter for the current X08 frame collector."""

from collections.abc import Mapping
from math import fsum, isfinite


def _number(value):
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_frame_evidence(snapshot, *, cleanup=None):
    """Return copied scorer fields; cleanup must be observed separately."""
    _require(isinstance(snapshot, Mapping), "snapshot must be a mapping")
    _require(isinstance(cleanup, Mapping) and all(
        cleanup.get(key) is True for key in
        ('recorder_removed', 'element_disposed', 'mouse_released')),
        "separate successful cleanup evidence required")
    _require(snapshot.get('complete') is True and snapshot.get('visible') is True
             and 'error' in snapshot and snapshot['error'] is None,
             "recording incomplete, hidden, or errored")
    visibility = snapshot.get('visibility')
    _require(isinstance(visibility, list) and bool(visibility)
             and all(state == 'visible' for state in visibility),
             "continuous visible history required")
    start, finish, duration = (snapshot.get(key) for key in
                               ('start_ms', 'finish_ms', 'duration_ms'))
    _require(all(_number(value) for value in (start, finish, duration)),
             "finite recording times required")
    _require(start >= 0 and finish >= start and duration >= 10000
             and abs((finish - start) - duration) <= 1,
             "incoherent recording interval")

    kinds = ['pan'] * 4 + ['zoom_in'] * 10 + ['zoom_out'] * 10 + ['window_select']
    actions = snapshot.get('actions')
    _require(isinstance(actions, list) and len(actions) == len(kinds),
             "all 25 actions including window selection required")
    previous = 0
    for action, kind in zip(actions, kinds):
        _require(isinstance(action, Mapping) and action.get('kind') == kind
                 and action.get('success') is True
                 and action.get('within_recording') is True,
                 "ordered successful actions required")
        began, ended = action.get('start_ms'), action.get('finish_ms')
        _require(_number(began) and _number(ended)
                 and previous <= began <= ended <= 10000,
                 "action outside ordered recording interval")
        previous = ended

    sequence = snapshot.get('sequence')
    counts = {'pan_drags': kinds.count('pan'), 'zoom_in_steps': kinds.count('zoom_in'),
              'zoom_out_steps': kinds.count('zoom_out')}
    _require(isinstance(sequence, Mapping) and all(
        type(sequence.get(key)) is int and sequence[key] == count
        for key, count in counts.items())
        and _number(sequence.get('duration_ms')) and sequence['duration_ms'] == duration,
        "sequence must match actions and snapshot duration")
    raw = snapshot.get('raw_events')
    _require(isinstance(raw, Mapping), "raw trusted events required")
    for kind, minimum in [('pointerdown', 5), ('pointerup', 5),
                          ('pointermove', 5), ('wheel', 20)]:
        counters = raw.get(kind)
        _require(isinstance(counters, Mapping) and all(
            type(counters.get(key)) is int and counters[key] >= 0
            for key in ('trusted', 'untrusted')),
            "nonnegative integer event counters required")
        _require(counters['trusted'] >= minimum, "insufficient trusted " + kind)
    deltas = snapshot.get('frame_deltas_ms')
    _require(isinstance(deltas, list) and len(deltas) >= 600 and all(
        _number(value) and value > 0 for value in deltas),
        "at least 600 finite positive frame deltas required")
    try:
        fits = fsum(deltas) <= finish - start + 1
    except OverflowError:
        fits = False
    _require(fits, "frame deltas exceed actual recording interval")
    return {'frame_deltas_ms': list(deltas), 'sequence': dict(counts, duration_ms=duration)}
