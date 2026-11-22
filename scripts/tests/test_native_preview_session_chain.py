"""Inert session-chain rehearsal. No SDK, lease acquisition or runtime entrypoint."""
import pytest

import native_preview_command_coordinator as coordinator
import native_preview_command_journal as journal
from test_native_preview_command_coordinator import Harness
from test_native_preview_session_contract import command, context, wire


@pytest.mark.parametrize('fault', [None, 'validate', 'journal', 'coordinator', 'effect', 'commit'])
def test_chain_faults_preserve_durable_dispatch_boundary(tmp_path, monkeypatch, fault):
    h = Harness(tmp_path)
    value = command()
    original_prepare = h.owner.prepare
    original_effect = h.effect
    original_commit = h.owner._commit
    observed = []

    def prepare(raw, **kwargs):
        if fault == 'journal':
            raise OSError('inert intent storage failure')
        return original_prepare(raw, **kwargs)

    def effect(value, trusted, decision):
        # An independent connection must see the intent BEFORE any effect.
        reopened = journal.CommandJournal(tmp_path / 'owned', context())
        pending = reopened.snapshot()['commands']
        assert len(pending) == 1 and pending[0]['status'] == 'pending'
        observed.append('durable-intent')
        result = original_effect(value, trusted, decision)
        if fault == 'commit':
            monkeypatch.setattr(h.owner, '_commit', lambda connection: (_ for _ in ()).throw(OSError('inert result storage failure')))
        return result

    monkeypatch.setattr(h.owner, 'prepare', prepare)
    h.effect = effect
    if fault == 'validate':
        value['source_commit'] = 'f' * 40
    if fault == 'coordinator':
        h.denied = {2}  # authority loss after durable intent, before callback
    h.effect_error = fault == 'effect'
    raw = wire(value)
    if fault:
        with pytest.raises(coordinator.CoordinatorError):
            h.controller().execute(raw)
    else:
        assert h.controller().execute(raw)['record']['status'] == 'complete'

    monkeypatch.setattr(h.owner, '_commit', original_commit)
    h.owner = journal.CommandJournal(tmp_path / 'owned', context())
    state = h.owner.snapshot()
    if fault in ('validate', 'journal'):
        assert state['commands'] == [] and h.effects == [] and observed == []
        assert state['next_sequence'] == 3
        return
    expected = {None: 'complete', 'coordinator': 'unknown', 'effect': 'unknown', 'commit': 'pending'}[fault]
    assert state['commands'][0]['status'] == expected
    assert state['next_sequence'] == (4 if fault is None else 3)
    assert len(h.effects) == (0 if fault == 'coordinator' else 1)
    assert observed == ([] if fault == 'coordinator' else ['durable-intent'])
    h.denied.clear()
    h.effect_error = False
    before = list(h.effects)
    assert h.controller().execute(raw)['replayed'] is True
    assert h.effects == before  # restart never repeats complete or uncertain effects
