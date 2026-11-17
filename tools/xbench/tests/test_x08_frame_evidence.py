"""Synthetic collector snapshots only; no live evidence is asserted."""

from copy import deepcopy

import pytest

from benches.x08_frame_evidence import validate_frame_evidence


@pytest.fixture
def cleanup():
    return dict(recorder_removed=True, element_disposed=True, mouse_released=True)


@pytest.fixture
def snapshot():
    kinds = ['pan'] * 4 + ['zoom_in'] * 10 + ['zoom_out'] * 10 + ['window_select']
    starts = [0, 800, 1600, 2400] + list(range(3200, 8200, 250)) + [8500]
    return {
        'complete': True, 'visible': True, 'visibility': ['visible', 'visible'],
        'error': None, 'start_ms': 123, 'finish_ms': 10123, 'duration_ms': 10000,
        'frame_deltas_ms': [16] * 600,
        'sequence': dict(pan_drags=4, zoom_in_steps=10, zoom_out_steps=10,
                         duration_ms=10000),
        'actions': [dict(kind=kind, start_ms=start, finish_ms=start + 10,
                         success=True, within_recording=True)
                    for kind, start in zip(kinds, starts)],
        'raw_events': {kind: dict(trusted=count, untrusted=0) for kind, count in
                       [('pointerdown', 5), ('pointerup', 5),
                        ('pointermove', 5), ('wheel', 20)]},
    }


def test_600_and_copied_scorer_fields_without_mutation(snapshot, cleanup):
    before, cleanup_before = deepcopy(snapshot), deepcopy(cleanup)
    result = validate_frame_evidence(snapshot, cleanup=cleanup)
    assert result == {key: snapshot[key] for key in ('frame_deltas_ms', 'sequence')}
    assert snapshot == before and cleanup == cleanup_before
    result['frame_deltas_ms'].append(1)
    result['sequence']['pan_drags'] = 0
    assert snapshot == before


def test_599_refused(snapshot, cleanup):
    snapshot['frame_deltas_ms'].pop()
    with pytest.raises(ValueError, match='600'):
        validate_frame_evidence(snapshot, cleanup=cleanup)


@pytest.mark.parametrize('key,value', [
    ('complete', False), ('complete', 1), ('visible', False),
    ('visibility', []), ('visibility', ['visible', 'hidden', 'visible']),
    ('error', 'interrupted'), ('duration_ms', 9999), ('finish_ms', 10125),
    ('start_ms', float('nan')), ('duration_ms', float('inf')),
])
def test_recording_fail_closed(snapshot, cleanup, key, value):
    snapshot[key] = value
    before = deepcopy(snapshot)
    with pytest.raises(ValueError):
        validate_frame_evidence(snapshot, cleanup=cleanup)
    assert snapshot.keys() == before.keys()
    assert snapshot['actions'] == before['actions']


@pytest.mark.parametrize('kind', ['pointerdown', 'pointerup', 'pointermove', 'wheel'])
def test_untrusted_never_substitutes_for_trusted(snapshot, cleanup, kind):
    snapshot['raw_events'][kind] = dict(trusted=0, untrusted=1000)
    with pytest.raises(ValueError, match='trusted'):
        validate_frame_evidence(snapshot, cleanup=cleanup)


@pytest.mark.parametrize('counter', ['trusted', 'untrusted'])
@pytest.mark.parametrize('value', [-1, True, 5.0, None])
def test_invalid_counters(snapshot, cleanup, counter, value):
    snapshot['raw_events']['pointermove'][counter] = value
    with pytest.raises(ValueError):
        validate_frame_evidence(snapshot, cleanup=cleanup)


@pytest.mark.parametrize('key,value', [
    ('kind', 'zoom_out'), ('start_ms', -1), ('start_ms', 9000),
    ('finish_ms', 10001), ('finish_ms', None), ('finish_ms', float('inf')),
    ('success', False), ('within_recording', False),
])
def test_malformed_late_interrupted_actions(snapshot, cleanup, key, value):
    snapshot['actions'][0][key] = value
    with pytest.raises(ValueError):
        validate_frame_evidence(snapshot, cleanup=cleanup)


def test_missing_window_despite_complete_claim(snapshot, cleanup):
    snapshot['actions'].pop()
    assert snapshot['complete'] is True
    with pytest.raises(ValueError, match='window'):
        validate_frame_evidence(snapshot, cleanup=cleanup)


def test_overlapping_actions(snapshot, cleanup):
    snapshot['actions'][1]['start_ms'] = 5
    with pytest.raises(ValueError):
        validate_frame_evidence(snapshot, cleanup=cleanup)


@pytest.mark.parametrize('key,value', [('pan_drags', 3), ('pan_drags', True),
                                       ('duration_ms', 10001)])
def test_sequence_must_match(snapshot, cleanup, key, value):
    snapshot['sequence'][key] = value
    with pytest.raises(ValueError, match='sequence'):
        validate_frame_evidence(snapshot, cleanup=cleanup)


@pytest.mark.parametrize('value', [0, -1, True, float('nan'), float('inf'), 1000])
def test_invalid_or_impossible_deltas(snapshot, cleanup, value):
    snapshot['frame_deltas_ms'][0] = value
    with pytest.raises(ValueError):
        validate_frame_evidence(snapshot, cleanup=cleanup)


def test_actual_interval_tolerance(snapshot, cleanup):
    snapshot['frame_deltas_ms'] = [10] * 599 + [4011]
    validate_frame_evidence(snapshot, cleanup=cleanup)
    snapshot['frame_deltas_ms'][-1] += .01
    with pytest.raises(ValueError, match='interval'):
        validate_frame_evidence(snapshot, cleanup=cleanup)


@pytest.mark.parametrize('key', ['recorder_removed', 'element_disposed', 'mouse_released'])
def test_cleanup_explicit_true_required(snapshot, cleanup, key):
    with pytest.raises(ValueError, match='cleanup'):
        validate_frame_evidence(snapshot)
    cleanup[key] = False
    with pytest.raises(ValueError, match='cleanup'):
        validate_frame_evidence(snapshot, cleanup=cleanup)
    del cleanup[key]
    with pytest.raises(ValueError, match='cleanup'):
        validate_frame_evidence(snapshot, cleanup=cleanup)


@pytest.mark.parametrize('key', ['actions', 'sequence', 'raw_events', 'visibility',
                                'error', 'start_ms', 'finish_ms', 'frame_deltas_ms'])
def test_missing_metadata(snapshot, cleanup, key):
    del snapshot[key]
    with pytest.raises(ValueError):
        validate_frame_evidence(snapshot, cleanup=cleanup)
