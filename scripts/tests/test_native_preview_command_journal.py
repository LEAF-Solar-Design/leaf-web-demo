"""Dormant local journal tests. Effects are inert synthetic callbacks only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sys

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import native_preview_command_journal as journal
from test_native_preview_session_contract import command, context, wire, IDS


def make(tmp_path):
    root = tmp_path / 'owned'
    return journal.CommandJournal(root, context(), create=not root.exists())


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def finish(owner, raw, outcome='complete', evidence='d' * 64):
    return owner.finish(command()['request_id'], digest(raw), outcome, evidence)


def test_pending_survives_restart_without_second_inert_effect(tmp_path):
    effects = []
    raw = wire(command())
    first = make(tmp_path).prepare(raw, now=100)
    if first['dispatch']:
        effects.append('synthetic effect')
    restarted = make(tmp_path)
    assert restarted.prepare(raw, now=100)['dispatch'] is False
    assert restarted.prepare(raw, now=100)['status'] == 'pending'
    assert effects == ['synthetic effect']
    assert restarted.snapshot()['next_sequence'] == 3
    finish(restarted, raw)
    assert restarted.snapshot()['next_sequence'] == 4
    assert restarted.prepare(raw, now=130)['status'] == 'complete'
    assert effects == ['synthetic effect']


@pytest.mark.parametrize('outcome', ['complete', 'failed'])
def test_terminal_replay_advances_once_and_next_command_uses_next_sequence(tmp_path, outcome):
    owner = make(tmp_path)
    raw = wire(command())
    owner.prepare(raw, now=100)
    expected = finish(owner, raw, outcome)
    assert finish(owner, raw, outcome) == expected
    assert owner.snapshot()['next_sequence'] == 4
    next_command = command('screenshot')
    next_command.update(request_id=IDS[7], sequence=4)
    assert owner.prepare(wire(next_command), now=100)['dispatch'] is True


def test_unknown_effect_blocks_new_requests_until_exact_reconciliation(tmp_path):
    owner = make(tmp_path)
    raw = wire(command())
    owner.prepare(raw, now=100)
    finish(owner, raw, 'unknown')
    newer = command('screenshot')
    newer.update(request_id=IDS[7], sequence=4)
    with pytest.raises(journal.JournalError):
        owner.prepare(wire(newer), now=100)
    assert owner.prepare(raw, now=100)['status'] == 'unknown'
    assert owner.snapshot()['next_sequence'] == 3
    finish(owner, raw)
    assert owner.prepare(wire(newer), now=100)['dispatch'] is True


def test_same_request_with_changed_payload_is_not_a_retry(tmp_path):
    owner = make(tmp_path)
    owner.prepare(wire(command()), now=100)
    with pytest.raises(journal.JournalError):
        owner.prepare(wire(command('screenshot')), now=100)
    assert len(owner.snapshot()['commands']) == 1


@pytest.mark.parametrize('field', ['session_id', 'org_id', 'project_id', 'actor_binding_id',
                                  'job_id', 'attempt', 'lease_owner', 'allocator_ticket', 'deadline'])
def test_changed_context_cannot_adopt_existing_journal(tmp_path, field):
    owner = make(tmp_path)
    owner.prepare(wire(command()), now=100)
    before = owner.snapshot()
    changed = context()
    changed[field] = 2 if field == 'attempt' else 200 if field == 'deadline' else 'other-ticket' if field == 'allocator_ticket' else IDS[8]
    with pytest.raises(journal.JournalError):
        journal.CommandJournal(tmp_path / 'owned', changed)
    assert owner.snapshot() == before


@pytest.mark.parametrize('field', ['repo_key', 'source_request_id', 'source_version',
                                  'source_commit', 'source_tree', 'archive_sha256'])
def test_changed_source_cannot_adopt_existing_journal(tmp_path, field):
    make(tmp_path)
    changed = context()
    changed['source'][field] = 3 if field == 'source_version' else 'd' * 64 if field == 'archive_sha256' else 'd' * 40 if field in ('source_commit', 'source_tree') else IDS[8]
    with pytest.raises(journal.JournalError):
        journal.CommandJournal(tmp_path / 'owned', changed)


def test_failed_pending_commit_returns_no_dispatch(tmp_path, monkeypatch):
    owner = make(tmp_path)
    def fail(_connection):
        raise OSError('synthetic private storage failure')
    monkeypatch.setattr(owner, '_commit', fail)
    with pytest.raises(journal.JournalError) as caught:
        owner.prepare(wire(command()), now=100)
    assert str(caught.value) == 'Native session journal unavailable or conflicting.'
    assert caught.value.__context__ is None
    assert not make(tmp_path).snapshot()['commands']


def test_failed_result_commit_preserves_pending_and_sequence(tmp_path, monkeypatch):
    owner = make(tmp_path)
    raw = wire(command())
    owner.prepare(raw, now=100)
    def fail(_connection):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(owner, '_commit', fail)
    with pytest.raises(journal.JournalError):
        finish(owner, raw)
    restarted = make(tmp_path)
    assert restarted.snapshot()['commands'][0]['status'] == 'pending'
    assert restarted.snapshot()['next_sequence'] == 3
    assert restarted.prepare(raw, now=100)['dispatch'] is False
    finish(restarted, raw)
    assert restarted.snapshot()['next_sequence'] == 4


def test_concurrent_same_request_has_one_dispatch_decision(tmp_path):
    owner = make(tmp_path)
    raw = wire(command())
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: owner.prepare(raw, now=100), range(16)))
    assert sum(item['dispatch'] for item in results) == 1
    assert all(item['status'] == 'pending' for item in results)
    assert len(owner.snapshot()['commands']) == 1


def test_concurrent_distinct_requests_cannot_share_one_sequence(tmp_path):
    owner = make(tmp_path)
    values = [command(), command('screenshot')]
    values[1]['request_id'] = IDS[7]
    def prepare(value):
        try:
            return owner.prepare(wire(value), now=100)['dispatch']
        except journal.JournalError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(prepare, values)) == 1
    assert len(owner.snapshot()['commands']) == 1


def test_caller_context_mutation_cannot_extend_lease(tmp_path):
    trusted = context()
    owner = journal.CommandJournal(tmp_path / 'owned', trusted, create=True)
    trusted['deadline'] = 200
    with pytest.raises(journal.JournalError):
        owner.prepare(wire(command()), now=130)
    assert not owner.snapshot()['commands']


def test_changed_in_memory_context_cannot_authorize_against_old_binding(tmp_path):
    owner = make(tmp_path)
    owner.context['deadline'] = 200
    with pytest.raises(journal.JournalError):
        owner.prepare(wire(command()), now=130)
    assert not make(tmp_path).snapshot()['commands']


def test_app_text_is_never_written_to_journal(tmp_path):
    owner = make(tmp_path)
    value = command('set_text')
    value['arguments']['value'] = 'PRIVATE SYNTHETIC TEXT NEVER STORE'
    raw = wire(value)
    owner.prepare(raw, now=100)
    for path in (tmp_path / 'owned').iterdir():
        if path.is_file():
            assert value['arguments']['value'].encode() not in path.read_bytes()
    assert owner.snapshot()['commands'][0]['command_sha256'] == digest(raw)


def proof(owner):
    return dict(session_id=IDS[0], context_sha256=owner.snapshot()['context_sha256'],
                allocator_ticket='preview-owned.1', process_extinct=True,
                cleanup_complete=True, allocator_release_confirmed=True)


def test_closing_keeps_unknown_evidence_and_requires_exact_teardown(tmp_path):
    owner = make(tmp_path)
    raw = wire(command())
    owner.prepare(raw, now=100)
    finish(owner, raw, 'unknown')
    owner.start_closing()
    with pytest.raises(journal.JournalError):
        newer = command('screenshot')
        newer['request_id'] = IDS[7]
        owner.prepare(wire(newer), now=100)
    owner.close(proof(owner))
    state = make(tmp_path).snapshot()
    assert state['phase'] == 'closed'
    assert state['commands'][0]['status'] == 'unknown'
    assert state['next_sequence'] == 3
    assert make(tmp_path).prepare(raw, now=130)['dispatch'] is False


@pytest.mark.parametrize('field,value', [('session_id', IDS[8]), ('context_sha256', 'f' * 64),
    ('allocator_ticket', 'foreign'), ('process_extinct', False), ('cleanup_complete', False),
    ('allocator_release_confirmed', False), ('process_extinct', 1), ('extra', True)])
def test_bad_teardown_cannot_close_checkpoint(tmp_path, field, value):
    owner = make(tmp_path)
    owner.start_closing()
    invalid = proof(owner)
    invalid[field] = value
    with pytest.raises(journal.JournalError):
        owner.close(invalid)
    assert owner.snapshot()['phase'] == 'closing'


def test_expired_close_is_not_a_new_app_capability(tmp_path):
    owner = make(tmp_path)
    result = owner.prepare(wire(command('close')), now=130)
    assert result['dispatch'] is True
    assert owner.snapshot()['phase'] == 'closing'
    assert owner.snapshot()['next_sequence'] == 3


def test_teardown_is_idempotent_but_cannot_start_from_active_state(tmp_path):
    owner = make(tmp_path)
    observed = proof(owner)
    with pytest.raises(journal.JournalError):
        owner.close(observed)
    owner.start_closing()
    owner.close(observed)
    before = owner.snapshot()
    owner.close(observed)
    owner.start_closing()
    assert owner.snapshot() == before
    with pytest.raises(journal.JournalError):
        owner.prepare(wire(command()), now=100)


@pytest.mark.parametrize('change', ['wrong-id', 'wrong-digest', 'wrong-outcome', 'bad-evidence'])
def test_foreign_or_malformed_result_cannot_settle_pending(tmp_path, change):
    owner = make(tmp_path)
    raw = wire(command())
    owner.prepare(raw, now=100)
    args = [IDS[8], digest(raw), 'complete', 'e' * 64]
    if change == 'wrong-id': args[0] = IDS[7]
    elif change == 'wrong-digest': args[1] = 'f' * 64
    elif change == 'wrong-outcome': args[2] = 'ok'
    else: args[3] = 'private error'
    with pytest.raises(journal.JournalError):
        owner.finish(*args)
    assert owner.snapshot()['commands'][0]['status'] == 'pending'
    assert owner.snapshot()['next_sequence'] == 3


def test_corrupt_database_is_not_reinitialized(tmp_path):
    root = tmp_path / 'owned'
    root.mkdir()
    path = root / 'commands.sqlite'
    path.write_bytes(b'corrupt existing data')
    with pytest.raises(journal.JournalError):
        make(tmp_path)
    assert path.read_bytes() == b'corrupt existing data'


def test_missing_database_in_existing_session_cannot_authorize_duplicate_effect(tmp_path):
    owner = make(tmp_path)
    owner.prepare(wire(command()), now=100)
    (tmp_path / 'owned' / 'commands.sqlite').unlink()
    with pytest.raises(journal.JournalError):
        make(tmp_path)


def test_existing_empty_directory_is_not_assumed_a_new_session(tmp_path):
    (tmp_path / 'owned').mkdir()
    with pytest.raises(journal.JournalError):
        make(tmp_path)


def test_reopening_lost_session_directory_is_not_implicit_creation(tmp_path):
    owner = make(tmp_path)
    owner.prepare(wire(command()), now=100)
    root = tmp_path / 'owned'
    (root / 'commands.sqlite').unlink()
    root.rmdir()
    with pytest.raises(journal.JournalError):
        journal.CommandJournal(root, context())
