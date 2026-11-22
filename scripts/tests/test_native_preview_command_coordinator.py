"""Actual validator/journal composition with inert authority and app callbacks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import native_preview_command_coordinator as coordinator
import native_preview_command_journal as journal
from test_native_preview_session_contract import command, context, wire, IDS


class Harness:
    def __init__(self, tmp_path):
        self.owner = journal.CommandJournal(tmp_path / 'owned', context(), create=True)
        self.now = 100
        self.authorities = []
        self.effects = []
        self.denied = set()
        self.result_change = {}
        self.effect_error = False
        self.after_effect = None
        self.handle_change = {}

    def authorize(self, _context, mode):
        self.authorities.append(mode)
        return dict(context_sha256=self.owner.binding, mode=mode,
                    allowed=len(self.authorities) not in self.denied)

    def effect(self, value, _context, decision):
        self.effects.append(value['operation'])
        if self.effect_error:
            raise TimeoutError('private synthetic upstream details')
        if self.after_effect:
            self.after_effect()
        result = dict(context_sha256=self.owner.binding, request_id=value['request_id'],
                    command_sha256=decision['command_sha256'], sequence=value['sequence'],
                    outcome='complete', evidence_sha256='d' * 64)
        result.update(self.result_change)
        return result

    def resolve(self, _context, snapshot_id, element_id):
        value = dict(context_sha256=self.owner.binding, source_commit='a' * 40,
                     snapshot_id=snapshot_id, element_id=element_id, resolved=True)
        value.update(self.handle_change)
        return value

    def controller(self, **kwargs):
        return coordinator.CommandCoordinator(self.owner, authorize=self.authorize,
            effect=self.effect, clock=lambda: self.now, resolve_handles=self.resolve, **kwargs)


def fail(control, raw):
    with pytest.raises(coordinator.CoordinatorError) as caught:
        control.execute(raw)
    assert str(caught.value) == 'Native session command unavailable or uncertain.'
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_real_journal_and_validator_run_one_inert_effect_across_restart(tmp_path):
    h = Harness(tmp_path)
    raw = wire(command())
    result = h.controller().execute(raw)
    assert result['record']['status'] == 'complete'
    assert not result['replayed']
    h.owner = journal.CommandJournal(tmp_path / 'owned', context())
    assert h.controller().execute(raw)['replayed'] is True
    assert h.effects == ['inspect']
    assert h.owner.snapshot()['next_sequence'] == 4


def test_early_denial_has_no_intent_or_effect(tmp_path):
    h = Harness(tmp_path)
    h.denied = {1}
    fail(h.controller(), wire(command()))
    assert not h.effects
    assert not h.owner.snapshot()['commands']


@pytest.mark.parametrize('field,value', [('context_sha256', 'e' * 64), ('mode', 'teardown'),
    ('allowed', False), ('allowed', 1), ('extra', 'private'), ('mode', None)])
def test_unbound_authority_reply_is_not_permission(tmp_path, field, value):
    h = Harness(tmp_path)
    def authority(_context, mode):
        reply = dict(context_sha256=h.owner.binding, mode=mode, allowed=True)
        reply[field] = value
        return reply
    h.authorize = authority
    fail(h.controller(), wire(command()))
    assert not h.effects and not h.owner.snapshot()['commands']


@pytest.mark.parametrize('denied', [2, 3])
def test_authority_loss_around_effect_retains_unknown_without_redispatch(tmp_path, denied):
    h = Harness(tmp_path)
    h.denied = {denied}
    raw = wire(command())
    fail(h.controller(), raw)
    assert len(h.effects) == (1 if denied == 3 else 0)
    state = h.owner.snapshot()
    assert state['commands'][0]['status'] == 'unknown'
    assert state['next_sequence'] == 3
    before = len(h.effects)
    h.denied.clear()
    assert h.controller().execute(raw)['replayed'] is True
    assert len(h.effects) == before


def test_callback_timeout_never_repeats_effect(tmp_path):
    h = Harness(tmp_path)
    h.effect_error = True
    raw = wire(command())
    fail(h.controller(), raw)
    h.effect_error = False
    assert h.controller().execute(raw)['record']['status'] == 'unknown'
    assert h.effects == ['inspect']
    for file in (tmp_path / 'owned').iterdir():
        if file.is_file():
            assert b'private synthetic upstream' not in file.read_bytes()


def test_expiry_during_callback_cannot_settle_success(tmp_path):
    h = Harness(tmp_path)
    h.after_effect = lambda: setattr(h, 'now', 121)
    fail(h.controller(), wire(command()))
    assert h.effects == ['inspect']
    assert h.owner.snapshot()['commands'][0]['status'] == 'unknown'


@pytest.mark.parametrize('step', [1, 2])
def test_expiry_during_authority_read_never_reaches_effect(tmp_path, step):
    h = Harness(tmp_path)
    original = h.authorize
    def authority(context_value, mode):
        reply = original(context_value, mode)
        if len(h.authorities) == step:
            h.now = 121
        return reply
    h.authorize = authority
    fail(h.controller(), wire(command()))
    assert not h.effects
    records = h.owner.snapshot()['commands']
    assert len(records) == (1 if step == 2 else 0)
    if records:
        assert records[0]['status'] == 'unknown'


@pytest.mark.parametrize('outcome', ['failed', 'unknown'])
def test_bound_failed_or_unknown_result_is_not_reexecuted(tmp_path, outcome):
    h = Harness(tmp_path)
    h.result_change['outcome'] = outcome
    raw = wire(command())
    assert h.controller().execute(raw)['record']['status'] == outcome
    assert h.controller().execute(raw)['replayed'] is True
    assert h.effects == ['inspect']
    assert h.owner.snapshot()['next_sequence'] == (4 if outcome == 'failed' else 3)


@pytest.mark.parametrize('field,value', [('context_sha256', 'e' * 64), ('request_id', IDS[7]),
    ('command_sha256', 'f' * 64), ('sequence', True), ('sequence', 4),
    ('outcome', 'ok'), ('evidence_sha256', 'private error'), ('extra', 'private')])
def test_unbound_callback_result_cannot_clear_pending(tmp_path, field, value):
    h = Harness(tmp_path)
    def malformed(command_value, _context, decision):
        h.effects.append(command_value['operation'])
        result = dict(context_sha256=h.owner.binding, request_id=command_value['request_id'],
                      command_sha256=decision['command_sha256'], sequence=command_value['sequence'],
                      outcome='complete', evidence_sha256='d' * 64)
        result[field] = value
        return result
    h.effect = malformed
    fail(h.controller(), wire(command()))
    assert h.owner.snapshot()['commands'][0]['status'] == 'unknown'
    assert h.owner.snapshot()['next_sequence'] == 3


@pytest.mark.parametrize('field,value', [('context_sha256', 'e' * 64), ('source_commit', 'f' * 40),
    ('snapshot_id', IDS[8]), ('element_id', IDS[8]), ('resolved', False), ('resolved', 1), ('extra', True)])
def test_stale_or_foreign_inspector_witness_prevents_effect(tmp_path, field, value):
    h = Harness(tmp_path)
    h.handle_change[field] = value
    fail(h.controller(), wire(command('tap')))
    assert not h.effects
    assert h.owner.snapshot()['commands'][0]['status'] == 'unknown'


@pytest.mark.parametrize('operation', ['tap', 'set_text'])
def test_bound_inspector_witness_reaches_only_inert_effect(operation, tmp_path):
    h = Harness(tmp_path)
    assert h.controller().execute(wire(command(operation)))['record']['status'] == 'complete'
    assert h.effects == [operation]


def test_expiry_during_handle_resolution_prevents_effect(tmp_path):
    h = Harness(tmp_path)
    original = h.resolve
    def resolver(context_value, snapshot_id, element_id):
        reply = original(context_value, snapshot_id, element_id)
        h.now = 121
        return reply
    h.resolve = resolver
    fail(h.controller(), wire(command('tap')))
    assert not h.effects
    assert h.owner.snapshot()['commands'][0]['status'] == 'unknown'


def test_missing_inspector_producer_does_not_fabricate_handle_authority(tmp_path):
    h = Harness(tmp_path)
    control = coordinator.CommandCoordinator(h.owner, authorize=h.authorize,
        effect=h.effect, clock=lambda: h.now)
    fail(control, wire(command('tap')))
    assert not h.effects
    assert h.owner.snapshot()['commands'][0]['status'] == 'unknown'


def test_result_commit_failure_remains_pending(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    def break_storage():
        def fail_commit(_connection):
            raise OSError('synthetic disk unavailable')
        monkeypatch.setattr(h.owner, '_commit', fail_commit)
    h.after_effect = break_storage
    raw = wire(command())
    fail(h.controller(), raw)
    h.owner = journal.CommandJournal(tmp_path / 'owned', context())
    assert h.owner.snapshot()['commands'][0]['status'] == 'pending'
    assert h.controller().execute(raw)['replayed'] is True
    assert h.effects == ['inspect']


def test_close_requests_owned_teardown_without_app_effect_or_closed_claim(tmp_path):
    h = Harness(tmp_path)
    h.now = 121
    result = h.controller().execute(wire(command('close')))
    assert result['status'] == 'teardown-required'
    assert not h.effects
    assert h.authorities == ['teardown']
    assert h.owner.snapshot()['phase'] == 'closing'
    assert h.owner.snapshot()['teardown_sha256'] is None


def test_concurrent_same_command_dispatches_at_most_once(tmp_path):
    h = Harness(tmp_path)
    raw = wire(command())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: h.controller().execute(raw), range(8)))
    assert h.effects == ['inspect']
    assert sum(not item['replayed'] for item in results) == 1


def test_malformed_command_does_not_invoke_authority_or_effect(tmp_path):
    h = Harness(tmp_path)
    value = command()
    value['project_id'] = IDS[8]
    fail(h.controller(), wire(value))
    assert not h.authorities and not h.effects
    assert not h.owner.snapshot()['commands']
