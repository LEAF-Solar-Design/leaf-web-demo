"""Synthetic X05 bundle checks, no archive, network or customer fixtures."""

from copy import deepcopy
from hashlib import sha256
import json

import pytest

from benches import x05_bundle as bundle

FIXTURES = ["alpha", "beta"]
STAGES = ["import", "grids", "strings", "grouping", "table", "deltas", "reopened"]
VERSIONS = {"alpha": "design-a", "beta": "design-b"}


def synthetic_bundle():
    members, payloads, references = [], {}, []
    for fixture in FIXTURES:
        for stage in STAGES:
            name = f"{fixture}/{stage}.json"
            payload = b'{"panels":1}'  # Equal totals and bytes never establish fixture ownership.
            payloads[name] = payload
            members.append(dict(name=name, fixture=fixture, stage=stage,
                                byte_length=len(payload), sha256=sha256(payload).hexdigest(),
                                design_version=VERSIONS[fixture]))
            references.append(dict(name=name, fixture=fixture, stage=stage, design_version=VERSIONS[fixture]))
    return members, payloads, references


def inventory(members):
    return bundle.validate_inventory(members, expected_fixture_ids=FIXTURES, required_stages=STAGES)


def audit(members, payloads):
    return bundle.audit_bundle_bytes(members, payloads, expected_fixture_ids=FIXTURES, required_stages=STAGES)


def refs(members, references):
    return bundle.check_stage_references(members, references, expected_fixture_versions=VERSIONS, required_stages=STAGES)


def codes(result):
    return result["diagnostic_counts"]


def small_diff_inventory():
    fixtures = ['a', 'b', 'c']
    return dict(expected_fixture_ids=fixtures, required_stages=['saved'],
        members=[dict(name=fixture+'/saved.bin',fixture=fixture,stage='saved',design_version='v1',
            byte_length=0,sha256='0'*64) for fixture in fixtures])


def test_exhaustive_overlapping_diff_facts_match_independent_reference():
    from itertools import product
    for states in product(range(4), repeat=3):
        before = small_diff_inventory()
        after = deepcopy(before)
        expected = []
        for index, state in enumerate(states):
            row = after['members'][index]
            if state & 1:
                row['sha256'] = '1'*64
                expected.append(dict(name=row['name'],kind='repinned'))
            if state & 2:
                row['design_version'] = 'v2'
                expected.append(dict(name=row['name'],kind='reassigned'))
        preserved = deepcopy((before,after))
        result = bundle.diff_inventories(before,after)
        expected.sort(key=lambda row:(row['name'],row['kind']))
        assert result['ok'] and result['differences'] == expected
        assert result['difference_total'] == sum(state.bit_count() for state in states)
        assert result['changed_member_total'] == sum(state != 0 for state in states)
        assert result['difference_counts'] == dict(added=0,removed=0,
            repinned=sum(state in (1,3) for state in states), reassigned=sum(state in (2,3) for state in states))
        assert (before,after) == preserved


@pytest.mark.parametrize('total',[31,32,33])
def test_diff_cap_boundaries_mix_categories_and_unchanged_members(total):
    from collections import Counter
    fixtures = ['f%d' % i for i in range(4)]
    stages = ['s%02d' % i for i in range(16)]
    before = dict(expected_fixture_ids=fixtures,required_stages=stages,members=[
        dict(name='members/%03d.bin' % i,fixture=fixtures[i//16],stage=stages[i%16],
             design_version='v1',byte_length=0,sha256='0'*64) for i in range(64)])
    after = deepcopy(before)
    expected = [dict(name=before['members'][0]['name'],kind='removed'),dict(name='new/member.bin',kind='added')]
    after['members'][0]['name'] = 'new/member.bin'
    remaining = total - 2
    for index in range(remaining//2):
        row = after['members'][2*(index+1)]
        row.update(sha256='1'*64,design_version='v2')
        expected.extend([dict(name=row['name'],kind='repinned'),dict(name=row['name'],kind='reassigned')])
    if remaining % 2:
        row = after['members'][32]
        row['byte_length'] = 1
        expected.append(dict(name=row['name'],kind='repinned'))
    expected.sort(key=lambda row:(row['name'],row['kind']))
    result = bundle.diff_inventories(before,after)
    assert result['ok'] and result['difference_total'] == total
    assert result['differences'] == expected[:bundle.MAX_DIAGNOSTICS]
    assert result['difference_counts'] == Counter(row['kind'] for row in expected)
    assert result['changed_member_total'] == len({row['name'] for row in expected})
    unchanged = [i for i in range(64) if before['members'][i] == after['members'][i]]
    assert len(unchanged) > 30 and all(i in unchanged for i in range(1,32,2))
    before['members'].reverse(); after['members'].reverse()
    assert bundle.diff_inventories(before,after) == result


def test_diff_all_metadata_coordinates_and_overlap_count_members_once():
    from itertools import product
    coordinates = [('byte_length',1,'repinned'),('sha256','1'*64,'repinned'),
        ('fixture','new','reassigned'),('stage','new','reassigned'),('design_version','v2','reassigned')]
    for selected in product((False,True),repeat=5):
        before = dict(expected_fixture_ids=['unit'],required_stages=['saved'],members=[dict(
            name='unit/saved.bin',fixture='unit',stage='saved',design_version='v1',byte_length=0,sha256='0'*64)])
        after = deepcopy(before)
        categories = set()
        for enabled,(field,value,category) in zip(selected,coordinates):
            if enabled:
                after['members'][0][field] = value
                categories.add(category)
        after['expected_fixture_ids'] = [after['members'][0]['fixture']]
        after['required_stages'] = [after['members'][0]['stage']]
        result = bundle.diff_inventories(before,after)
        assert result['ok'] and result['difference_total'] == len(categories)
        assert result['changed_member_total'] == int(any(selected))
        assert result['differences'] == [dict(name='unit/saved.bin',kind=category) for category in sorted(categories)]


@pytest.mark.parametrize('framing',['duplicate_before','duplicate_after','concatenated','trailing'])
def test_diff_cli_framing_rejects_before_comparison(monkeypatch,framing):
    import io
    import sys
    from types import SimpleNamespace
    pair = json.dumps(dict(before=small_diff_inventory(),after=small_diff_inventory()))
    if framing.startswith('duplicate_'):
        key = framing.removeprefix('duplicate_')
        escaped = ''.join('\\u%04x' % ord(char) for char in key)
        text = pair.replace('"'+key+'":','"'+key+'":null,"'+escaped+'":',1)
        expected = 'JSON_DUPLICATE_KEY'
    else:
        text = pair + (pair if framing == 'concatenated' else 'SYNTHETIC_PRIVATE_TRAILING')
        expected = 'JSON_INVALID'
    calls = []
    def forbidden(*args,**kwargs): calls.append(1); raise AssertionError('framing reached comparison')
    monkeypatch.setattr(bundle,'diff_inventories',forbidden)
    output = io.BytesIO()
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=ShortInput(text.encode(),width=8192)))
    monkeypatch.setattr(sys,'stdout',SimpleNamespace(buffer=output))
    assert bundle.main(['--inventory-diff']) == 4 and calls == []
    raw = output.getvalue()
    assert len(raw.splitlines()) == 1 and b'SYNTHETIC_PRIVATE_TRAILING' not in raw
    assert json.loads(raw)['diagnostic_counts'] == {expected:1}


@pytest.mark.parametrize('kind',['PASS','FAIL','UNREADABLE'])
@pytest.mark.parametrize('failure',['partial_write','flush'])
def test_diff_delivery_failures_override_verdict_without_retry(monkeypatch,kind,failure):
    import io
    import sys
    from types import SimpleNamespace
    before = small_diff_inventory()
    after = deepcopy(before)
    if kind == 'FAIL': after['members'][0]['sha256'] = '1'*64
    if kind == 'UNREADABLE': after = None
    text = json.dumps(dict(before=before,after=after))
    original = bundle.diff_inventory_json
    expected = (bundle.reconciliation.serialize_result(original(text))+'\n').encode('ascii')
    calls = []
    def observed(value): calls.append(value); return original(value)
    class Sink:
        def __init__(self): self.parts=[]; self.writes=0; self.flushes=0
        def write(self,data):
            self.writes += 1
            if failure == 'partial_write':
                if self.writes == 1:
                    self.parts.append(data[:7])
                    return 7
                raise OSError('SYNTHETIC_PRIVATE_DELIVERY')
            self.parts.append(data)
            return len(data)
        def flush(self):
            self.flushes += 1
            raise OSError('SYNTHETIC_PRIVATE_FLUSH')
    sink = Sink()
    errors = io.StringIO()
    monkeypatch.setattr(bundle,'diff_inventory_json',observed)
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=io.BytesIO(text.encode())))
    monkeypatch.setattr(sys,'stdout',SimpleNamespace(buffer=sink))
    monkeypatch.setattr(sys,'stderr',errors)
    assert bundle.main(['--inventory-diff']) == 5 and calls == [text]
    assert b''.join(sink.parts) == (expected[:7] if failure == 'partial_write' else expected)
    assert sink.writes == (2 if failure == 'partial_write' else 1)
    assert sink.flushes == (0 if failure == 'partial_write' else 1)
    assert errors.getvalue() == ''


@pytest.mark.parametrize('kind,exit_code', [('PASS',0), ('FAIL',1), ('UNREADABLE',4), ('malformed',4), ('duplicate',4)])
def test_inventory_diff_cli_strict_verdicts_and_sanitized_single_result(tmp_path, kind, exit_code):
    import subprocess
    before = json.loads(json_inventory())
    after = deepcopy(before)
    if kind == 'FAIL': after['members'][0]['sha256'] = '0' * 64
    if kind == 'UNREADABLE': after['members'][0]['sha256'] = 'SYNTHETIC_PRIVATE_DIFF'
    text = json.dumps(dict(before=before, after=after))
    if kind == 'malformed': text = '{"private":"SYNTHETIC_PRIVATE_DIFF'
    if kind == 'duplicate': text = '{"before":{},"\\u0062efore":{},"after":{}}'
    completed = subprocess.run(cli_command('--inventory-diff'), input=text.encode(), capture_output=True, cwd=tmp_path, timeout=20)
    assert completed.returncode == exit_code and completed.stderr == b''
    assert len(completed.stdout.splitlines()) == 1 and completed.stdout.isascii()
    result = json.loads(completed.stdout)
    assert result == bundle.diff_inventory_json(text)
    assert result['verdict'] == ('UNREADABLE' if kind in ('malformed','duplicate') else kind)
    assert 'SYNTHETIC_PRIVATE_DIFF' not in completed.stdout.decode()
    assert not result['origin_authenticated']


def test_inventory_diff_cli_modes_are_exclusive_and_delivery_failure_wins(monkeypatch, tmp_path):
    import io
    import subprocess
    import sys
    from types import SimpleNamespace
    completed = subprocess.run(cli_command('--inventory-diff','--replay'), input=b'private', capture_output=True, cwd=tmp_path, timeout=20)
    assert completed.returncode == 2 and completed.stdout == b'' and b'private' not in completed.stderr
    source = json.loads(json_inventory())
    for kind in ('PASS','FAIL','UNREADABLE'):
        after = deepcopy(source)
        if kind == 'FAIL': after['members'][0]['sha256'] = '0' * 64
        if kind == 'UNREADABLE': after = None
        raw = json.dumps(dict(before=source,after=after)).encode()
        class FailedOutput:
            def write(self, value): raise OSError('SYNTHETIC_PRIVATE_OUTPUT')
            def fileno(self): raise OSError('memory stream')
        with monkeypatch.context() as patch:
            patch.setattr(sys,'stdin',SimpleNamespace(buffer=io.BytesIO(raw)))
            patch.setattr(sys,'stdout',FailedOutput())
            assert bundle.main(['--inventory-diff']) == 5
    with monkeypatch.context() as patch:
        output = io.BytesIO()
        patch.setattr(sys,'stdin',SimpleNamespace(buffer=ShortInput(b'', failure=ValueError)))
        patch.setattr(sys,'stdout',SimpleNamespace(buffer=output))
        assert bundle.main(['--inventory-diff']) == 4
        assert json.loads(output.getvalue())['diagnostic_counts'] == {'CLI_INPUT_UNREADABLE':1}


def test_inventory_diff_reversal_identity_and_input_permutations():
    before = json.loads(json_inventory())
    after = deepcopy(before)
    after['members'][0]['sha256'] = '0' * 64
    after['members'][1]['design_version'] = 'v2'
    after['members'][2]['name'] = 'renamed/member.bin'
    preserved = deepcopy((before,after))
    forward = bundle.diff_inventories(before,after)
    reverse = bundle.diff_inventories(after,before)
    assert forward['difference_total'] == reverse['difference_total'] == 4
    exchanged = [dict(row, kind={'added':'removed','removed':'added'}.get(row['kind'],row['kind'])) for row in forward['differences']]
    assert reverse['differences'] == sorted(exchanged,key=lambda row:(row['name'],row['kind']))
    for side in (before,after):
        identity = bundle.diff_inventories(side,deepcopy(side))
        assert identity['difference_total'] == identity['changed_member_total'] == 0
        assert identity['differences'] == [] and identity['inventory_equal']
    for left_reverse in (False,True):
        for right_reverse in (False,True):
            left,right = deepcopy(before),deepcopy(after)
            if left_reverse: left['members'].reverse(); left['expected_fixture_ids'].reverse(); left['required_stages'].reverse()
            if right_reverse: right['members'].reverse(); right['expected_fixture_ids'].reverse(); right['required_stages'].reverse()
            assert bundle.diff_inventories(left,right) == forward
    assert (before,after) == preserved


def test_inventory_diff_linear_index_work_and_bounded_storage_without_io(monkeypatch):
    import builtins
    counts = []
    original = bundle._name
    accesses = []
    def counted(value): accesses.append(value); return original(value)
    def forbidden(*args,**kwargs): raise AssertionError('diff attempted payload work')
    for size in (4,8,16):
        source = expanded_fixture('unit',['stage%02d' % index for index in range(size)])
        before = encoder_args(source)['inventory']
        after = deepcopy(before)
        for row in after['members']: row['sha256'] = '0' * 64; row['design_version'] = 'v2'
        accesses.clear()
        with monkeypatch.context() as patch:
            patch.setattr(bundle,'_name',counted)
            patch.setattr(bundle,'sha256',forbidden)
            patch.setattr(builtins,'open',forbidden)
            result = bundle.diff_inventories(before,after)
        counts.append(len(accesses))
        assert result['difference_total'] == 2 * size
        assert len(result['differences']) == min(2 * size,bundle.MAX_DIAGNOSTICS)
    assert counts[0] > 0 and counts[1] == 2 * counts[0] and counts[2] == 2 * counts[1]


@pytest.mark.parametrize('side',['before','after'])
@pytest.mark.parametrize('mutation',['fields','member','count','coverage'])
def test_inventory_diff_malformed_side_is_bounded_sanitized_and_recovers(side,mutation):
    source = json.loads(json_inventory())
    pair = dict(before=deepcopy(source),after=deepcopy(source))
    if mutation == 'fields': pair[side] = {'SYNTHETIC_PRIVATE_SIDE':True}
    elif mutation == 'member': pair[side]['members'][0]['sha256'] = 'SYNTHETIC_PRIVATE_SIDE'
    elif mutation == 'count': pair[side]['members'][0]['byte_length'] = True
    else: pair[side]['members'].pop()
    preserved = deepcopy(pair)
    result = bundle.diff_inventories(**pair)
    assert not result['ok'] and result['difference_total'] is None and result['differences'] == []
    assert 1 <= result['diagnostic_total'] and len(result['diagnostics']) <= bundle.MAX_DIAGNOSTICS
    assert {row['side'] for row in result['diagnostics']} == {side}
    assert 'SYNTHETIC_PRIVATE_SIDE' not in json.dumps(result) and pair == preserved
    assert bundle.diff_inventory_json(json.dumps(pair))['verdict'] == 'UNREADABLE'
    assert bundle.diff_inventories(source,deepcopy(source))['inventory_equal']


def test_concurrent_inventory_diffs_keep_returned_diagnostics_and_inputs_isolated(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier,Event
    pairs = []
    for fixture in ('alpha','beta'):
        before = encoder_args(expanded_fixture(fixture,['saved']))['inventory']
        after = deepcopy(before)
        after['members'][0]['sha256'] = 'invalid'
        pairs.append(dict(before=before,after=after))
    preserved = deepcopy(pairs)
    expected = [bundle.diff_inventories(**pair) for pair in pairs]
    barrier,release = Barrier(2),Event()
    original = bundle._inventory_envelope
    def coordinated(value):
        result = original(value)
        barrier.wait(timeout=10)
        assert release.wait(timeout=10)
        return result
    monkeypatch.setattr(bundle,'_inventory_envelope',coordinated)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(bundle.diff_inventories,**pair) for pair in pairs]
        release.set()
        results = [future.result(timeout=15) for future in futures]
    assert results == expected
    results[0]['diagnostics'][0]['code'] = 'MUTATED'
    results[0]['diagnostic_counts'].clear()
    assert results[1] == expected[1] and pairs == preserved


def test_inventory_diff_reports_exact_change_facts_with_shared_sorted_cap():
    before = json.loads(json_inventory())
    after = deepcopy(before)
    # Every fixture/stage remains covered, while identities and pins change.
    after['members'][0]['sha256'] = '0' * 64
    for member in after['members']: member['design_version'] = 'v2'
    after['members'][0]['fixture'], after['members'][7]['fixture'] = after['members'][7]['fixture'], after['members'][0]['fixture']
    preserved = deepcopy((before, after))
    result = bundle.diff_inventories(before, after)
    assert result['ok'] and not result['inventory_equal']
    assert result['difference_counts'] == dict(added=0, removed=0, repinned=1, reassigned=14)
    assert result['difference_total'] == 15 and result['changed_member_total'] == 14
    assert result['differences'] == sorted(result['differences'], key=lambda row:(row['name'],row['kind']))
    assert not result['origin_authenticated'] and not result['integrity_verified']
    assert (before, after) == preserved
    assert bundle.diff_inventories(before, before)['inventory_equal']
    after = deepcopy(before)
    for row in after['members']: row['name'] = 'new/' + row['name']
    result = bundle.diff_inventories(before, after)
    assert result['difference_counts'] == dict(added=14, removed=14, repinned=0, reassigned=0)
    assert result['difference_total'] == 28
    # Sixteen stages per fixture yield64 facts, capped at32 examples.
    source = expanded_fixture('unit', ['stage%02d' % i for i in range(16)])
    large = encoder_args(source)['inventory']
    large['members'] += [dict(row, name='other/'+row['name'], fixture='other') for row in large['members']]
    large['expected_fixture_ids'].append('other')
    changed = deepcopy(large)
    for row in changed['members']: row['name'] = 'renamed/' + row['name']
    result = bundle.diff_inventories(large, changed)
    assert result['difference_total'] == 64 and len(result['differences']) == bundle.MAX_DIAGNOSTICS
    assert result['difference_counts'] == dict(added=32, removed=32, repinned=0, reassigned=0)
    changed['members'].reverse(); large['members'].reverse()
    assert bundle.diff_inventories(large, changed) == result


def test_inventory_diff_rejects_ambiguous_inputs_and_normalizes_safe_slashes():
    source = json.loads(json_inventory())
    changed = deepcopy(source)
    for row in changed['members']: row['name'] = row['name'].replace('/', '\\')
    assert bundle.diff_inventories(source, changed)['inventory_equal']
    changed['members'].append(deepcopy(changed['members'][0]))
    result = bundle.diff_inventories(source, changed)
    assert not result['ok'] and result['difference_total'] is None and not result['differences']
    assert codes(result) == {'MEMBER_NAME_DUPLICATE':1, 'FIXTURE_STAGE_DUPLICATE':1}
    assert all(row['side'] == 'after' for row in result['diagnostics'])


@pytest.mark.parametrize('kind', ['PASS', 'FAIL', 'UNREADABLE'])
def test_canonical_encoder_decode_reencode_is_idempotent_and_keeps_supplied_pins(kind):
    source = reconciliation_bundle(('alpha', 'beta'), failed=('alpha',) if kind == 'FAIL' else ())
    if kind == 'UNREADABLE': source['payloads']['alpha/seven.json'] += b' '
    expected = bundle.reconcile_bundle(**source)
    encoded = bundle.encode_replay_json(**encoder_args(source))
    original = encoded['text']
    pins = json.loads(original)['inventory']['members']
    for _ in range(4):
        decoded = bundle.decode_replay_json(encoded['text'])
        assert decoded['ok']
        encoded = bundle.encode_replay_json(**encoder_args(decoded['envelope']))
        assert encoded['ok'] and encoded['text'] == original
        assert json.loads(encoded['text'])['inventory']['members'] == pins
        assert bundle.replay_bundle_json(encoded['text']) == expected
        assert not encoded['origin_authenticated']


@pytest.mark.parametrize('path,key', [('root','inventory'), ('inventory','members'),
    ('member','fixture'), ('reference','stage'), ('payload','alpha/seven.json')])
def test_unicode_escaped_key_collisions_are_rejected_before_work(monkeypatch, path, key):
    envelope = json.loads(cli_payload())
    escaped_key = ''.join('\\u%04x' % ord(char) for char in key)
    encoded_key = json.dumps(key)
    if path == 'root': target = envelope
    elif path == 'inventory': target = envelope['inventory']
    elif path == 'member': target = envelope['inventory']['members'][0]
    elif path == 'reference': target = envelope['references'][0]
    else: target = envelope['member_bytes_hex']
    text = json.dumps(target)
    text = text.replace(encoded_key + ':', encoded_key + ':null,"' + escaped_key + '":', 1)
    whole = json.dumps(envelope).replace(json.dumps(target), text, 1)
    calls = []
    def forbidden(*args, **kwargs): calls.append(1); raise AssertionError('collision reached work')
    monkeypatch.setattr(bundle,'sha256',forbidden)
    monkeypatch.setattr(bundle,'_member_bytes_from_hex',forbidden)
    monkeypatch.setattr(bundle.reconciliation,'reconcile_bound_seven_steps',forbidden)
    result = bundle.decode_replay_json(whole)
    assert codes(result) == {'JSON_DUPLICATE_KEY':1}
    if path in ('inventory','member'):
        inventory_text = text if path == 'inventory' else json.dumps(envelope['inventory']).replace(json.dumps(target), text, 1)
        assert codes(bundle.decode_inventory_json(inventory_text)) == {'JSON_DUPLICATE_KEY':1}
    code, raw, errors = in_process_cli(monkeypatch, ShortInput(whole.encode(), width=8192))
    assert code == 4 and not errors and calls == []
    assert json.loads(raw)['integrity']['diagnostic_counts'] == {'JSON_DUPLICATE_KEY':1}
    assert escaped_key not in raw.decode()


def test_fresh_process_import_has_no_evidence_transport_children_or_output(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    script = r'''
import sys, pathlib, socket, subprocess, io, builtins
root = sys.argv[1]
sys.path.insert(0, root)
attempts = []
def guarded(event, args):
    if event == 'open':
        name = args[0]
        # Python may load trusted module source/bytecode, never evidence.
        if isinstance(name, (str, bytes)) and str(name).endswith(('.py', '.pyc')):
            return
        attempts.append(event)
        raise AssertionError('unexpected evidence read')
    if event.startswith(('socket.', 'subprocess.', 'os.system', 'os.spawn')):
        attempts.append(event)
        raise AssertionError('unexpected execution or transport')
sys.addaudithook(guarded)
from benches import x05_bundle
assert not attempts
'''
    completed = subprocess.run([sys.executable, '-P', '-B', '-c', script,
        str(Path(bundle.__file__).resolve().parents[1])], cwd=tmp_path, capture_output=True, timeout=20)
    assert completed.returncode == 0 and completed.stdout == completed.stderr == b''


def test_equal_bytes_cannot_substitute_for_explicit_cross_fixture_identity(monkeypatch):
    source = reconciliation_bundle(('alpha', 'beta'))
    source['payloads']['beta/seven.json'] = source['payloads']['alpha/seven.json']
    raw = source['payloads']['alpha/seven.json']
    source['members'][1].update(byte_length=len(raw), sha256=sha256(raw).hexdigest())
    before = deepcopy(source)
    calls = []
    original = bundle.reconciliation.reconcile_bound_seven_steps
    def observed(*args, **kwargs): calls.append(args); return original(*args, **kwargs)
    monkeypatch.setattr(bundle.reconciliation,'reconcile_bound_seven_steps',observed)
    result = bundle.reconcile_bundle(**source)
    assert result['integrity']['integrity_verified'] and len(calls) == 1
    assert result['semantic']['fixtures']['alpha']['verdict'] == 'PASS'
    assert result['semantic']['fixtures']['beta']['result']['reason'] == 'bundle_evidence_fixture_conflict'
    assert source == before and not result['origin_authenticated']
    repaired = reconciliation_bundle(('alpha', 'beta'))
    assert bundle.reconcile_bundle(**repaired)['semantic']['verdict'] == 'PASS'


def renamed_bundle(source):
    renamed = deepcopy(source)
    mapping = {name: 'renamed/safe_%03d.bin' % index for index, name in enumerate(sorted(source['payloads']))}
    for member in renamed['members']: member['name'] = mapping[member['name']]
    for reference in renamed['references']: reference['name'] = mapping[reference['name']]
    renamed['payloads'] = {mapping[name]: raw for name, raw in source['payloads'].items()}
    return renamed


@pytest.mark.parametrize('kind', ['PASS', 'FAIL', 'UNREADABLE'])
def test_bijective_member_renaming_preserves_replay_and_stale_reference_is_unreadable(monkeypatch, kind):
    source = reconciliation_bundle(('alpha', 'beta'), failed=('alpha',) if kind == 'FAIL' else ())
    if kind == 'UNREADABLE': source['payloads']['alpha/seven.json'] += b' '
    before = deepcopy(source)
    expected = bundle.reconciliation.serialize_result(bundle.reconcile_bundle(**source))
    renamed = renamed_bundle(source)
    assert bundle.reconciliation.serialize_result(bundle.reconcile_bundle(**renamed)) == expected
    encoded = bundle.encode_replay_json(**encoder_args(renamed))
    assert encoded['ok']
    assert bundle.reconciliation.serialize_result(bundle.replay_bundle_json(encoded['text'])) == expected
    renamed['references'][0]['name'] = source['references'][0]['name']
    stale = bundle.reconcile_bundle(**renamed)
    assert stale['semantic']['verdict'] == 'UNREADABLE'
    assert codes(stale['bindings']) == {'REFERENCE_DANGLING': 1}
    assert source == before


def expanded_fixture(fixture, stages, *, failed=False, corrupt=False):
    source = reconciliation_bundle((fixture,), failed=(fixture,) if failed else ())
    raw = source['payloads'][fixture + '/seven.json']
    source['members'], source['references'], source['payloads'] = [], [], {}
    source['required_stages'], source['evidence_stage'] = list(stages), stages[0]
    for stage in stages:
        name = fixture + '/' + stage + '.json'
        source['members'].append(dict(name=name, fixture=fixture, stage=stage, design_version='design-' + fixture,
            byte_length=len(raw), sha256=sha256(raw).hexdigest()))
        source['references'].append(dict(name=name, fixture=fixture, stage=stage, design_version='design-' + fixture))
        source['payloads'][name] = raw + (b' ' if corrupt else b'')
    return source


@pytest.mark.parametrize('beyond_cap', [False, True])
def test_disjoint_bundle_union_has_additive_conflicts_and_independent_semantics(beyond_cap):
    from collections import Counter
    stages = ['stage%02d' % i for i in range(16 if beyond_cap else 1)]
    components = [expanded_fixture('alpha', stages, corrupt=True),
                  expanded_fixture('beta', stages, failed=True, corrupt=beyond_cap),
                  expanded_fixture('gamma', stages)]
    individual = [bundle.reconcile_bundle(**component) for component in components]
    union = dict(members=sum((component['members'] for component in components), []),
                 references=sum((component['references'] for component in components), []),
                 payloads={k:v for component in components for k,v in component['payloads'].items()},
                 expected_fixture_versions={k:v for component in components for k,v in component['expected_fixture_versions'].items()},
                 required_stages=stages, evidence_stage=stages[0])
    before = deepcopy(union)
    result = bundle.reconcile_bundle(**union)
    expected_counts = Counter()
    for item in individual: expected_counts.update(codes(item['integrity']))
    assert codes(result['integrity']) == expected_counts
    assert result['integrity']['diagnostic_total'] == sum(item['integrity']['diagnostic_total'] for item in individual)
    assert len(result['integrity']['diagnostics']) == min(sum(expected_counts.values()), bundle.MAX_DIAGNOSTICS)
    if beyond_cap: assert result['integrity']['diagnostic_total'] > bundle.MAX_DIAGNOSTICS
    for fixture, item in zip(('alpha', 'beta', 'gamma'), individual):
        assert result['semantic']['fixtures'][fixture] == item['semantic']['fixtures'][fixture]
    assert result['semantic']['fixtures']['gamma']['verdict'] == 'PASS'
    assert result['semantic']['verdict'] == 'UNREADABLE' and union == before


@pytest.mark.parametrize('raw,reason', [
    (b'\xe2\x82', 'bundle_evidence_utf8_unreadable'),
    (b'\xc0\xaf', 'bundle_evidence_utf8_unreadable'),
    ('{"private":"SYNTHETIC_PRIVATE_TEXT"}'.encode('utf-16'), 'bundle_evidence_utf8_unreadable'),
    (b'{"private":"SYNTHETIC_PRIVATE_TEXT\x00"}', 'json_malformed')])
def test_hash_correct_text_decode_failures_are_sanitized_and_repairable(monkeypatch, raw, reason):
    source = reconciliation_bundle()
    repaired = deepcopy(source)
    source['payloads']['alpha/seven.json'] = raw
    source['members'][0].update(byte_length=len(raw), sha256=sha256(raw).hexdigest())
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', lambda *args: calls.append(1))
        result = bundle.reconcile_bundle(**source)
        assert result['integrity']['integrity_verified'] and calls == []
        semantic = result['semantic']['fixtures']['alpha']['result']
        assert semantic['verdict'] == 'UNREADABLE' and semantic['reason'] == reason
        assert 'SYNTHETIC_PRIVATE_TEXT' not in bundle.reconciliation.serialize_result(result)
    assert bundle.reconcile_bundle(**repaired)['semantic']['verdict'] == 'PASS'


@pytest.mark.parametrize('size', [0, 1, 8191, 8192, 8193, 65537])
def test_byte_audit_hashes_every_valid_byte_once_in_bounded_chunks(monkeypatch, size):
    raw = bytes(index % 251 for index in range(size))
    member = dict(name='unit/evidence.bin', fixture='unit', stage='evidence', design_version='v1',
                  byte_length=size, sha256=sha256(raw).hexdigest())
    updates, finalized = [], []
    class Digest:
        def __init__(self, initial):
            assert initial == b''
            self.inner = sha256(initial)
        def update(self, chunk): updates.append(chunk); self.inner.update(chunk)
        def hexdigest(self): finalized.append(1); return self.inner.hexdigest()
    monkeypatch.setattr(bundle, 'sha256', Digest)
    result = bundle.audit_bundle_bytes([member], {member['name']:raw}, expected_fixture_ids=['unit'], required_stages=['evidence'])
    assert result['integrity_verified'] and b''.join(updates) == raw and finalized == [1]
    assert all(0 < len(chunk) <= bundle.DIGEST_CHUNK_BYTES for chunk in updates)
    assert len(updates) == (size + bundle.DIGEST_CHUNK_BYTES - 1) // bundle.DIGEST_CHUNK_BYTES
    calls = []
    source = dict(members=[dict(member, sha256='0' * 64)], payloads={member['name']:raw},
        references=[{key:member[key] for key in ('name', 'fixture', 'stage', 'design_version')}],
        expected_fixture_versions={'unit':'v1'}, required_stages=['evidence'], evidence_stage='evidence')
    monkeypatch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', lambda *args: calls.append(1))
    failed = bundle.reconcile_bundle(**source)
    assert failed['semantic']['verdict'] == 'UNREADABLE' and calls == []
    assert codes(failed['integrity']) == {'PAYLOAD_SHA256_MISMATCH': 1}


def test_stage_completion_lattice_requires_all_references_and_retains_byte_errors():
    from itertools import combinations, permutations
    source = expanded_fixture('alpha', ['import', 'grid', 'assurance'])
    references = deepcopy(source['references'])
    for count in range(4):
        for selected in combinations(range(3), count):
            for order in permutations([index for index in range(3) if index not in selected]):
                current = dict(source, references=[deepcopy(references[index]) for index in selected])
                for restored in (None,) + order:
                    if restored is not None: current['references'].append(deepcopy(references[restored]))
                    before = deepcopy(current)
                    result = bundle.reconcile_bundle(**current)
                    missing = 3 - len(current['references'])
                    assert result['semantic']['verdict'] == ('UNREADABLE' if missing else 'PASS')
                    assert codes(result['bindings']) == ({'REFERENCE_STAGE_MISSING': missing} if missing else {})
                    assert current == before
                    corrupt = deepcopy(current)
                    corrupt['payloads']['alpha/import.json'] += b' '
                    damaged = bundle.reconcile_bundle(**corrupt)
                    assert damaged['semantic']['verdict'] == 'UNREADABLE'
                    assert codes(damaged['integrity']) == {'PAYLOAD_LENGTH_MISMATCH':1, 'PAYLOAD_SHA256_MISMATCH':1}
                    assert damaged['bindings'] == result['bindings']


@pytest.mark.parametrize('mutation', ['hash', 'length', 'fixture', 'stage', 'reference_name', 'reference_version', 'payload', 'missing_payload'])
def test_one_field_mutations_agree_across_direct_wire_and_cli(monkeypatch, mutation):
    source = reconciliation_bundle()
    member, reference = source['members'][0], source['references'][0]
    if mutation == 'hash': member['sha256'] = '0' * 64
    elif mutation == 'length': member['byte_length'] += 1
    elif mutation == 'fixture': member['fixture'] = 'foreign'
    elif mutation == 'stage': member['stage'] = 'foreign'
    elif mutation == 'reference_name': reference['name'] = 'alpha/missing.json'
    elif mutation == 'reference_version': reference['design_version'] = 'foreign'
    elif mutation == 'payload': source['payloads'][member['name']] += b' '
    else: source['payloads'].clear()
    before = deepcopy(source)
    direct = bundle.reconcile_bundle(**source)
    encoded = bundle.encode_replay_json(**encoder_args(source))
    # Deliberately invalid producer inputs cannot be encoded by the safe API.
    # Use the existing wire schema to exercise the consumer's rejection too.
    wire = encoded['text'] if encoded['ok'] else json.dumps(replay_envelope(source))
    replayed = bundle.replay_bundle_json(wire)
    exit_code, raw, errors = in_process_cli(monkeypatch, ShortInput(wire.encode(), width=8192))
    assert exit_code == 4 and not errors
    assert json.loads(raw) == json.loads(bundle.reconciliation.serialize_result(replayed))
    assert direct['semantic']['verdict'] == replayed['semantic']['verdict'] == 'UNREADABLE'
    if mutation in ('fixture', 'stage'):
        section = direct['integrity']
    elif mutation.startswith('reference_'):
        section = direct['bindings']
    else:
        section = direct['integrity']
    observed = replayed['integrity']
    assert codes(section) == codes(observed)
    assert section['diagnostics'] == observed['diagnostics']
    if mutation not in ('fixture', 'stage', 'reference_name', 'reference_version'):
        assert direct == replayed
    assert source == before


def test_small_inventory_coverage_matches_independent_multiset_reference():
    from itertools import product
    expected_stages = ['one', 'two', 'three']
    for counts in product(range(3), repeat=3):
        for foreign in (0, 1):
            members = []
            for stage, count in zip(expected_stages + ['foreign'], counts + (foreign,)):
                for ordinal in range(count):
                    members.append(dict(name='unit/%s%d' % (stage, ordinal), fixture='unit', stage=stage,
                        design_version='v1', byte_length=0, sha256=REFERENCE_VECTORS[0][1]))
            expected = {}
            missing = sum(count == 0 for count in counts)
            duplicate = sum(max(0, count - 1) for count in counts)
            for key, count in [('FIXTURE_STAGE_MISSING', missing), ('FIXTURE_STAGE_DUPLICATE', duplicate), ('MEMBER_STAGE_INVALID', foreign)]:
                if count: expected[key] = count
            before = deepcopy(members)
            result = bundle.validate_inventory(members, expected_fixture_ids=['unit'], required_stages=expected_stages)
            assert codes(result) == expected and result['diagnostic_total'] == missing + duplicate + foreign
            assert result['ok'] == (not expected)
            assert result == bundle.validate_inventory(list(reversed(members)), expected_fixture_ids=['unit'], required_stages=expected_stages)
            assert members == before


@pytest.mark.parametrize('lexeme', ['lower', 'upper', 'mixed', 'odd', 'space', 'newline', 'fullwidth', 'cyrillic'])
def test_hex_lexemes_have_explicit_support_and_rejection_before_semantics(monkeypatch, lexeme):
    envelope = json.loads(cli_payload())
    key = next(iter(envelope['member_bytes_hex']))
    text = envelope['member_bytes_hex'][key]
    transforms = dict(lower=text.lower(), upper=text.upper(),
        mixed=''.join(char.upper() if index % 2 else char for index, char in enumerate(text)),
        odd=text[:-1], space=text[:2] + '  ' + text[2:], newline=text[:2] + '\n\n' + text[2:],
        fullwidth='ａ０' + text[2:], cyrillic='а0' + text[2:])
    envelope['member_bytes_hex'][key] = transforms[lexeme]
    calls = []
    original = bundle.reconciliation.reconcile_bound_seven_steps
    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', observed)
    result = bundle.replay_bundle_json(json.dumps(envelope))
    if lexeme in ('lower', 'upper', 'mixed'):
        assert result['semantic']['verdict'] == 'PASS' and calls == [1]
    else:
        assert result['semantic']['verdict'] == 'UNREADABLE' and calls == []
        assert codes(result['integrity']) == {'MEMBER_HEX_INVALID': 1}


@pytest.mark.parametrize('location', ['malformed_member', 'foreign_payload'])
def test_private_payloads_never_escape_audit_replay_or_cli(monkeypatch, location):
    source = reconciliation_bundle()
    sentinel = b'SYNTHETIC_PRIVATE_PAYLOAD_739'
    if location == 'malformed_member':
        raw = b'{"private":"' + sentinel
        source['payloads']['alpha/seven.json'] = raw
        source['members'][0].update(byte_length=len(raw), sha256=sha256(raw).hexdigest())
    else:
        source['payloads']['foreign/private.bin'] = sentinel
    audit_result = bundle.audit_bundle_bytes(source['members'], source['payloads'],
        expected_fixture_ids=['alpha'], required_stages=source['required_stages'])
    direct = bundle.reconcile_bundle(**source)
    wire = json.dumps(replay_envelope(source))
    replayed = bundle.replay_bundle_json(wire)
    code, raw, errors = in_process_cli(monkeypatch, ShortInput(wire.encode(), width=8192))
    assert code == 4 and not errors and direct == replayed
    for result in (audit_result, direct, replayed, json.loads(raw)):
        serialized = bundle.reconciliation.serialize_result(result)
        assert sentinel.decode() not in serialized and sentinel.hex() not in serialized
        assert len(serialized) < 32768
    assert direct['semantic']['verdict'] == 'UNREADABLE'


@pytest.mark.parametrize('left,right', [('import', 'grid'), ('import', 'assurance'), ('grid', 'assurance')])
def test_hash_correct_stage_reference_substitutions_are_named_before_semantics(monkeypatch, left, right):
    source = reconciliation_bundle()
    raw = source['payloads']['alpha/seven.json']
    source['required_stages'] = ['import', 'grid', 'assurance']
    source['evidence_stage'] = 'assurance'
    source['members'], source['references'], source['payloads'] = [], [], {}
    for stage in source['required_stages']:
        name = 'alpha/' + stage + '.json'
        source['payloads'][name] = raw
        source['members'].append(dict(name=name, fixture='alpha', stage=stage,
            design_version='design-alpha', byte_length=len(raw), sha256=sha256(raw).hexdigest()))
        source['references'].append(dict(name=name, fixture='alpha', stage=stage, design_version='design-alpha'))
    assert bundle.reconcile_bundle(**source)['semantic']['verdict'] == 'PASS'
    a, b = [next(ref for ref in source['references'] if ref['stage'] == stage) for stage in (left, right)]
    a['name'], b['name'] = b['name'], a['name']
    calls = []
    monkeypatch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', lambda *args: calls.append(1))
    result = bundle.reconcile_bundle(**source)
    assert result['integrity']['integrity_verified'] and not calls
    assert result['semantic']['fixtures']['alpha']['result']['reason'] == 'bundle_stage_references_unreadable'
    assert codes(result['bindings']) == {'REFERENCE_STAGE_CONFLICT': 2}


@pytest.mark.parametrize('role', ['import', 'grid', 'assurance'])
def test_hash_correct_wrong_role_evidence_cannot_be_a_seven_step_record(monkeypatch, role):
    source = reconciliation_bundle()
    record = json.loads(source['payloads']['alpha/seven.json'])
    raw = json.dumps(record['envelope'][role]).encode()
    source['payloads']['alpha/seven.json'] = raw
    source['members'][0].update(byte_length=len(raw), sha256=sha256(raw).hexdigest())
    calls = []
    monkeypatch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', lambda *args: calls.append(1))
    result = bundle.reconcile_bundle(**source)
    assert result['integrity']['integrity_verified'] and not calls
    assert result['semantic']['fixtures']['alpha']['result']['reason'] == 'bundle_evidence_binding_unreadable'


@pytest.mark.parametrize('framing', ['bom', 'double_bom', 'middle_bom', 'concat', 'tail', 'nbsp'])
def test_json_framing_rejects_before_hashing_allocation_or_semantics(monkeypatch, framing):
    inventory_text = json_inventory()
    replay_text = cli_payload().decode()
    sentinel = 'SYNTHETIC_PRIVATE_FRAMING'
    def frame(text):
        return {'bom': '\ufeff' + text, 'double_bom': '\ufeff\ufeff' + text,
                'middle_bom': text + '\ufeff', 'concat': text + text,
                'tail': text + sentinel, 'nbsp': '\u00a0' + text}[framing]
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError('invalid framing reached work')
    with monkeypatch.context() as patch:
        patch.setattr(bundle, 'sha256', forbidden)
        patch.setattr(bundle, '_member_bytes_from_hex', forbidden)
        patch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', forbidden)
        decoded = bundle.decode_inventory_json(frame(inventory_text))
        assert codes(decoded) == {'JSON_INVALID': 1} and decoded['inventory'] is None
        decoded = bundle.decode_replay_json(frame(replay_text))
        assert codes(decoded) == {'JSON_INVALID': 1} and decoded['envelope'] is None
        direct = bundle.replay_bundle_json(frame(replay_text))
        code, raw, errors = in_process_cli(patch, ShortInput(frame(replay_text).encode(), width=8192))
        assert code == 4 and not errors and json.loads(raw) == direct
        assert direct['semantic']['verdict'] == 'UNREADABLE'
        assert codes(direct['integrity']) == {'JSON_INVALID': 1}
        assert sentinel not in raw.decode() and calls == []
    assert bundle.decode_inventory_json(inventory_text)['ok']
    assert bundle.replay_bundle_json(replay_text)['semantic']['verdict'] == 'PASS'


@pytest.mark.parametrize('whitespace', ['', ' ', '\t\r\n', '\r\n \t'])
def test_json_legal_whitespace_preserves_inventory_replay_and_cli(monkeypatch, whitespace):
    text = cli_payload().decode()
    assert bundle.decode_inventory_json(whitespace + json_inventory() + whitespace)['ok']
    expected = json.loads(bundle.reconciliation.serialize_result(bundle.replay_bundle_json(text)))
    assert bundle.decode_replay_json(whitespace + text + whitespace)['ok']
    code, raw, errors = in_process_cli(monkeypatch, ShortInput((whitespace + text + whitespace).encode(), width=8192))
    assert code == 0 and not errors and json.loads(raw) == expected


@pytest.mark.parametrize('failure_stage', ['construction', 'update', 'finalization'])
def test_digest_provider_failures_are_sanitized_and_recover(monkeypatch, failure_stage):
    source = reconciliation_bundle()
    preserved = deepcopy(source)
    sentinel = 'SYNTHETIC_PRIVATE_DIGEST'
    calls = []
    class BrokenDigest:
        def update(self, data):
            if failure_stage == 'update': raise OSError(sentinel)
        def hexdigest(self):
            raise ValueError(sentinel)
    def constructor(data):
        if failure_stage == 'construction': raise RuntimeError(sentinel)
        return BrokenDigest()
    with monkeypatch.context() as patch:
        patch.setattr(bundle, 'sha256', constructor)
        patch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', lambda **kwargs: calls.append(kwargs))
        audit_result = bundle.audit_bundle_bytes(source['members'], source['payloads'],
            expected_fixture_ids=['alpha'], required_stages=source['required_stages'])
        assert codes(audit_result) == {'PAYLOAD_DIGEST_UNREADABLE': 1}
        assert audit_result['members'] == [{'member_index': 0, 'bytes_equal': False}]
        result = bundle.reconcile_bundle(**source)
        assert result['semantic']['verdict'] == 'UNREADABLE' and calls == []
        assert sentinel not in json.dumps(result) and len(result['integrity']['diagnostics']) <= bundle.MAX_DIAGNOSTICS
    assert source == preserved
    assert bundle.reconcile_bundle(**source)['semantic']['verdict'] == 'PASS'


def test_returned_nested_diagnostics_do_not_alias_inputs_siblings_or_next_call():
    source = reconciliation_bundle(('alpha', 'beta', 'gamma'))
    source['payloads']['alpha/seven.json'] += b' '
    source['payloads']['beta/seven.json'] += b' '
    preserved = deepcopy(source)
    baseline = bundle.reconcile_bundle(**source)
    result = bundle.reconcile_bundle(**source)
    sibling = deepcopy(result['semantic']['fixtures']['beta'])
    result['integrity']['diagnostics'][0]['code'] = 'MUTATED'
    result['semantic']['fixtures']['alpha']['result']['reason'] = 'MUTATED'
    assert result['semantic']['fixtures']['beta'] == sibling
    assert source == preserved and baseline == bundle.reconcile_bundle(**source)
    assert 'MUTATED' not in bundle.reconciliation.serialize_result(baseline)
    members, payloads, references = synthetic_bundle()
    payloads[members[0]['name']] += b' '
    before = deepcopy((members, payloads, references))
    first = audit(members, payloads)
    expected = deepcopy(first)
    first['diagnostics'][0]['code'] = 'MUTATED'
    first['diagnostic_counts'].clear()
    first['members'][0]['bytes_equal'] = True
    assert audit(members, payloads) == expected
    assert (members, payloads, references) == before


def test_encoder_metadata_and_byte_work_is_linear_without_hash_or_rejected_hex(monkeypatch):
    import builtins
    counts = {'metadata': 0, 'bytes': 0, 'hex': 0}
    original_name, original_len, original_hex = bundle._name, builtins.len, bundle._encode_member_hex
    def counted_name(value):
        counts['metadata'] += 1
        return original_name(value)
    def counted_len(value):
        if type(value) is bytes: counts['bytes'] += 1
        return original_len(value)
    def counted_hex(value):
        counts['hex'] += 1
        return original_hex(value)
    def forbidden(*args): raise AssertionError('encoder hashed bytes')
    monkeypatch.setattr(bundle, '_name', counted_name)
    monkeypatch.setattr(bundle, 'len', counted_len, raising=False)
    monkeypatch.setattr(bundle, '_encode_member_hex', counted_hex)
    monkeypatch.setattr(bundle, 'sha256', forbidden)
    measured = []
    for size in (4, 8, 16):
        stages = ['stage%02d' % i for i in range(size)]
        members = [dict(name='unit/' + stage, fixture='unit', stage=stage, design_version='v1',
                        byte_length=1, sha256=REFERENCE_VECTORS[1][1]) for stage in stages]
        references = [{k: row[k] for k in ('name', 'fixture', 'stage', 'design_version')} for row in members]
        args = dict(inventory=dict(members=members, expected_fixture_ids=['unit'], required_stages=stages),
                    references=references, member_bytes={row['name']: b'\x00' for row in members},
                    expected_fixture_versions={'unit': 'v1'}, evidence_stage=stages[0])
        for key in counts: counts[key] = 0
        assert bundle.encode_replay_json(**args)['ok']
        measured.append(dict(counts))
        assert counts['hex'] == size and counts['bytes'] == 3 * size
        for key in counts: counts[key] = 0
        rejected = bundle.encode_replay_json(**args, max_wire_bytes=0)
        assert codes(rejected) == {'ENCODER_WIRE_BUDGET_EXCEEDED': 1}
        assert counts['hex'] == 0 and counts['bytes'] == 2 * size
    assert measured[1]['metadata'] == 2 * measured[0]['metadata']
    assert measured[2]['metadata'] == 2 * measured[1]['metadata']


def test_concurrent_replays_keep_exact_results_and_decimal_contexts_isolated(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from decimal import getcontext, localcontext, ROUND_FLOOR, ROUND_CEILING
    from threading import Barrier, Event
    texts = [cli_payload().decode(), exact_decimal_cli_payload(30, 1).decode(), cli_payload('FAIL').decode(), cli_payload('UNREADABLE').decode()]
    expected = [bundle.reconciliation.serialize_result(bundle.replay_bundle_json(text)) for text in texts]
    assert [json.loads(raw)['semantic']['verdict'] for raw in expected] == ['PASS', 'FAIL', 'FAIL', 'UNREADABLE']
    barrier, release = Barrier(4), Event()
    original = bundle.audit_bundle_bytes
    def interleaved(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.wait(timeout=10)
        assert release.wait(timeout=10)
        return result
    monkeypatch.setattr(bundle, 'audit_bundle_bytes', interleaved)
    parent = getcontext().copy()
    def run(index):
        with localcontext() as context:
            context.prec = (7, 18, 40, 60)[index]
            context.rounding = ROUND_FLOOR if index % 2 else ROUND_CEILING
            before = (context.prec, context.rounding, dict(context.flags), dict(context.traps))
            result = bundle.reconciliation.serialize_result(bundle.replay_bundle_json(texts[index]))
            assert (context.prec, context.rounding, dict(context.flags), dict(context.traps)) == before
            return result
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(run, i) for i in range(4)]
        release.set()
        observed = [future.result(timeout=15) for future in futures]
    assert observed == expected
    assert (getcontext().prec, getcontext().rounding, dict(getcontext().flags)) == (parent.prec, parent.rounding, dict(parent.flags))


def test_complete_bundle_has_integrity_but_no_origin_authentication_or_grading():
    members, payloads, references = synthetic_bundle()
    before = deepcopy((members, payloads, references, FIXTURES, STAGES, VERSIONS))
    assert inventory(members)["ok"] and refs(members, references)["ok"]
    assert inventory(members)["integrity_verified"] is False
    result = audit(members, payloads)
    assert result["ok"] and result["integrity_verified"]
    assert result["origin_authenticated"] is False
    assert "verdict" not in result
    assert all(m["bytes_equal"] for m in result["members"])
    assert (members, payloads, references, FIXTURES, STAGES, VERSIONS) == before


@pytest.mark.parametrize("field,value,code", [
    ("fixture", None, "MEMBER_FIXTURE_INVALID"), ("fixture", [], "MEMBER_FIXTURE_INVALID"),
    ("stage", "foreign", "MEMBER_STAGE_INVALID"), ("stage", {}, "MEMBER_STAGE_INVALID"),
    ("design_version", "", "MEMBER_VERSION_INVALID"),
    ("sha256", "F" * 64, "MEMBER_SHA256_INVALID"), ("sha256", "0" * 63, "MEMBER_SHA256_INVALID"),
    ("sha256", None, "MEMBER_SHA256_INVALID"), ("byte_length", True, "MEMBER_SIZE_INVALID"),
])
def test_explicit_member_metadata_is_required(field, value, code):
    members, _, _ = synthetic_bundle()
    members[0][field] = value
    result = inventory(members)
    assert not result["ok"] and code in codes(result)


def test_duplicate_names_duplicate_stages_and_missing_stages():
    members, _, _ = synthetic_bundle()
    duplicate = dict(members[0], name="alpha/other.json")
    assert "FIXTURE_STAGE_DUPLICATE" in codes(inventory(members + [duplicate]))
    assert "MEMBER_NAME_DUPLICATE" in codes(inventory(members + [dict(members[0])]))
    assert codes(inventory(members[1:]))["FIXTURE_STAGE_MISSING"] == 1


@pytest.mark.parametrize("fixtures,stages", [(None, STAGES), ([], STAGES), (["alpha", "alpha"], STAGES),
                                          (FIXTURES, []), (FIXTURES, ["../x"]), (FIXTURES, [True])])
def test_expected_sets_are_explicit_unique_and_bounded(fixtures, stages):
    assert codes(bundle.validate_inventory([], expected_fixture_ids=fixtures, required_stages=stages)) == {"EXPECTED_SET_INVALID": 1}


@pytest.mark.parametrize("name", ["/root/x", "../x", "a/../x", "a/./x", "a//x", "a/", "", ".", "..",
                                  "C:/x", "C:x", "\\\\host\\share\\x", "\\root\\x", "//host/x",
                                  "a\x00x", "a\nx", "a/%2e%2e/x", "a:" , "x" * 161, None, []])
def test_member_names_reject_nonliteral_or_escaping_names(name):
    assert codes(bundle.validate_member_names([name])) == {"MEMBER_NAME_INVALID": 1}


def test_slash_normalization_collisions_are_rejected_without_extracting():
    assert bundle.validate_member_names(["alpha/table.json", "beta\\table.json"])["ok"]
    result = bundle.validate_member_names(["alpha/table.json", "alpha\\table.json"])
    assert codes(result) == {"MEMBER_NAME_DUPLICATE": 1}
    assert "alpha" not in json.dumps(result)


def test_resource_budget_exact_edges_and_one_over():
    members = [{"byte_length": 3}, {"byte_length": 2}]
    options = dict(max_members=2, max_member_bytes=3, max_total_bytes=5)
    result = bundle.check_resource_budget(members, **options)
    assert result["ok"] and result["declared_bytes"] == 5
    assert "MEMBER_COUNT_EXCEEDED" in codes(bundle.check_resource_budget(members + [{"byte_length": 0}], **options))
    assert "MEMBER_BYTES_EXCEEDED" in codes(bundle.check_resource_budget([{"byte_length": 4}], **options))
    assert "TOTAL_BYTES_EXCEEDED" in codes(bundle.check_resource_budget([{"byte_length": 3}] * 2, **options))
    assert bundle.check_resource_budget([], max_members=0, max_member_bytes=0, max_total_bytes=0)["ok"]


@pytest.mark.parametrize("size", [None, True, -1, 1.5, "1", float("nan"), float("inf"), 10**1000])
def test_bad_sizes_are_rejected_before_payload_access_or_hashing(size, monkeypatch):
    members, _, _ = synthetic_bundle()
    members[0]["byte_length"] = size
    def forbidden(*args, **kwargs):
        raise AssertionError("payload/hash accessed before metadata budget")
    class PayloadTrap(dict):
        __iter__ = items = __getitem__ = forbidden
    monkeypatch.setattr(bundle, "sha256", forbidden)
    result = audit(members, PayloadTrap())
    assert not result["ok"]
    assert any(c in codes(result) for c in ("MEMBER_SIZE_INVALID", "MEMBER_BYTES_EXCEEDED"))
    assert len(json.dumps(result)) < 2000


@pytest.mark.parametrize("option,value", [("max_members", True), ("max_member_bytes", -1),
                                          ("max_total_bytes", 10**1000), ("max_members", 129)])
def test_invalid_budget_limits(option, value):
    assert codes(bundle.check_resource_budget([], **{option: value})) == {"BUDGET_LIMIT_INVALID": 1}


def test_byte_audit_localizes_missing_foreign_truncated_and_substituted_members():
    members, payloads, _ = synthetic_bundle()
    del payloads[members[0]["name"]]
    payloads[members[1]["name"]] = payloads[members[1]["name"]][:-1]
    payloads[members[2]["name"]] = b'{"panels":2}'
    payloads["foreign/data.json"] = b""
    result = audit(members, payloads)
    assert codes(result) == {"PAYLOAD_FOREIGN": 1, "PAYLOAD_LENGTH_MISMATCH": 1,
                             "PAYLOAD_MISSING": 1, "PAYLOAD_SHA256_MISMATCH": 2}
    assert result["integrity_verified"] is False and result["origin_authenticated"] is False
    assert [m["bytes_equal"] for m in result["members"]] == [False] * 3 + [True] * 11
    assert result == audit(members, dict(reversed(list(payloads.items()))))


def test_supplied_pins_can_be_rewritten_without_authenticating_origin():
    members, payloads, _ = synthetic_bundle()
    payloads[members[0]["name"]] = b'{"panels":2}'
    members[0]["sha256"] = sha256(payloads[members[0]["name"]]).hexdigest()
    result = audit(members, payloads)
    assert result["integrity_verified"] and not result["origin_authenticated"]


def test_missing_inventory_stage_retains_unchecked_siblings_without_hashing():
    members, payloads, _ = synthetic_bundle()
    del payloads[members[0]["name"]]
    result = audit(members[1:], payloads)
    assert codes(result) == {"FIXTURE_STAGE_MISSING": 1}
    assert all(m["bytes_equal"] is None for m in result["members"])


def test_stage_references_retain_independent_conflicts_even_for_equal_bytes():
    members, _, references = synthetic_bundle()
    references[0]["name"] = "beta/import.json"
    references[1]["name"] = "alpha/absent.json"
    references[2]["design_version"] = "other-version"
    members[3]["design_version"] = "mixed-version"
    before = deepcopy((members, references))
    result = refs(members, references)
    assert codes(result) == {"MEMBER_DESIGN_VERSION_CONFLICT": 1, "REFERENCE_CROSS_FIXTURE": 1,
                             "REFERENCE_DANGLING": 1, "REFERENCE_DESIGN_VERSION_CONFLICT": 1,
                             "REFERENCE_MEMBER_VERSION_CONFLICT": 3}
    assert (members, references) == before
    assert result == refs(members, references)


def test_reference_coverage_duplicates_and_wrong_stage_are_named():
    members, _, references = synthetic_bundle()
    references[0]["name"] = "alpha/table.json"
    references.pop()
    references.append(dict(references[1]))
    result = refs(members, references)
    assert codes(result) == {"REFERENCE_STAGE_CONFLICT": 1, "REFERENCE_STAGE_DUPLICATE": 1, "REFERENCE_STAGE_MISSING": 1}


def test_exact_diagnostic_totals_survive_bounded_examples_and_composition():
    members, payloads, references = synthetic_bundle()
    for member in members:
        member["fixture"] = "foreign"
        member["stage"] = "foreign"
        member["sha256"] = "bad"
    result = audit(members, payloads)
    assert len(result["diagnostics"]) == bundle.MAX_DIAGNOSTICS
    assert result["diagnostic_total"] == 56
    assert sum(codes(result).values()) == 56
    assert all(m["bytes_equal"] is None for m in result["members"])
    # Invalid metadata prevents hashing. Fourteen errors in each of fixture,
    # stage, hash syntax and missing coverage are still counted separately.


def json_inventory(members=None):
    if members is None:
        members = synthetic_bundle()[0]
    return json.dumps(dict(members=members, expected_fixture_ids=FIXTURES, required_stages=STAGES))


@pytest.mark.parametrize("text,code", [
    ('{"members":[],"members":[]}', "JSON_DUPLICATE_KEY"),
    ('{"x":{"a":0,"a":1}}', "JSON_DUPLICATE_KEY"),
    ('{"x":NaN}', "JSON_NONFINITE_NUMBER"),
    ('{"x":Infinity}', "JSON_NONFINITE_NUMBER"),
    ('{"x":-Infinity}', "JSON_NONFINITE_NUMBER"),
    ('{"x":1e9999}', "JSON_NONFINITE_NUMBER"),
    ('[0]', "INVENTORY_FIELDS_INVALID"), ('{}', "INVENTORY_FIELDS_INVALID"),
    ('{"members":[]}', "INVENTORY_FIELDS_INVALID"),
    ('{"x":', "JSON_INVALID"), ('{}{}', "JSON_INVALID"),
    ('[' * 17 + '0' + ']' * 17, "JSON_DEPTH_EXCEEDED"),
    (' ' * (bundle.MAX_JSON_CHARS + 1), "JSON_INPUT_UNREADABLE"),
    (None, "JSON_INPUT_UNREADABLE"), (b'{}', "JSON_INPUT_UNREADABLE"),
])
def test_strict_json_rejections_are_sanitized_before_auditing(text, code, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("hash or byte audit reached invalid JSON")
    monkeypatch.setattr(bundle, "sha256", forbidden)
    monkeypatch.setattr(bundle, "audit_bundle_bytes", forbidden)
    result = bundle.audit_inventory_json(text, object())
    assert codes(result) == {code: 1}
    assert result["inventory"] is None and not result["origin_authenticated"]


def test_strict_json_accepts_exact_limit_ignores_quoted_brackets_and_rejects_missing_fields():
    members, payloads, _ = synthetic_bundle()
    text = json_inventory(members)
    assert bundle.audit_inventory_json(text, payloads)["integrity_verified"]
    padded = text + ' ' * (bundle.MAX_JSON_CHARS - len(text))
    assert bundle.decode_inventory_json(padded)["ok"]
    assert codes(bundle.decode_inventory_json('[' * 16 + '0' + ']' * 16)) == {"INVENTORY_FIELDS_INVALID": 1}
    assert codes(bundle.decode_inventory_json(json.dumps({'x': '[\\\"' * 40}))) == {"INVENTORY_FIELDS_INVALID": 1}
    for field in members[0]:
        changed = deepcopy(members); del changed[0][field]
        assert codes(bundle.decode_inventory_json(json_inventory(changed))) == {"MEMBER_FIELDS_INVALID": 1}
    sentinel = 'SYNTHETIC_PRIVATE_X05'
    result = bundle.decode_inventory_json('{"x":"' + sentinel + '","x":0}')
    assert sentinel not in json.dumps(result)


# Literal reference vectors, independently cross-checked using Windows .NET
# SHA256 rather than calculating expected values with this module's digest.
REFERENCE_VECTORS = [
    (b'', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
    (b'\x00', '6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d'),
    (b'\xff', 'a8100ae6aa1940d0b663bb31cd466142ebbdbd5187131b92d93818987832eb89'),
]


@pytest.mark.parametrize("data,digest", REFERENCE_VECTORS)
def test_literal_binary_reference_vectors_and_one_byte_changes(data, digest):
    member = dict(name='unit/saved.bin', fixture='unit', stage='saved', design_version='v1',
                  byte_length=len(data), sha256=digest)
    options = dict(expected_fixture_ids=['unit'], required_stages=['saved'])
    assert bundle.audit_bundle_bytes([member], {member['name']: data}, **options)["integrity_verified"]
    changed = bytes([data[0] ^ 1]) + data[1:] if data else b'\x00'
    result = bundle.audit_bundle_bytes([member], {member['name']: changed}, **options)
    assert codes(result)["PAYLOAD_SHA256_MISMATCH"] == 1
    assert codes(result).get("PAYLOAD_LENGTH_MISMATCH", 0) == (0 if data else 1)


@pytest.mark.parametrize("mutation", ['name', 'fixture', 'stage', 'hash', 'version', 'size', 'coverage', 'count', 'total'])
def test_all_invalid_inventory_metadata_short_circuits_hashes(mutation, monkeypatch):
    members, payloads, _ = synthetic_bundle()
    if mutation == 'name': members[0]['name'] = '../bad'
    elif mutation == 'fixture': members[0]['fixture'] = 'foreign'
    elif mutation == 'stage': members[0]['stage'] = 'foreign'
    elif mutation == 'hash': members[0]['sha256'] = 'bad'
    elif mutation == 'version': members[0]['design_version'] = None
    elif mutation == 'size': members[0]['byte_length'] = True
    elif mutation == 'coverage': members.pop()
    elif mutation == 'count': members *= 10
    elif mutation == 'total':
        for member in members: member['byte_length'] = bundle.MAX_MEMBER_BYTES
    calls = []
    monkeypatch.setattr(bundle, 'sha256', lambda data: calls.append(data) or sha256(data))
    assert not audit(members, payloads)['ok']
    assert calls == []
    assert not bundle.audit_inventory_json(json_inventory(members), payloads)['ok']
    assert calls == []


def test_valid_members_are_hashed_once_and_foreign_payloads_are_not(monkeypatch):
    members, payloads, _ = synthetic_bundle()
    calls = []
    monkeypatch.setattr(bundle, 'sha256', lambda data: calls.append(data) or sha256(data))
    assert audit(members, payloads)['ok']; assert len(calls) == len(members)
    calls.clear(); payloads['foreign/data.bin'] = b'foreign'
    assert codes(audit(members, payloads)) == {'PAYLOAD_FOREIGN': 1}
    assert len(calls) == len(members) and b'foreign' not in calls


def diagnostic_signature(result):
    return {k: result[k] for k in ('ok', 'diagnostic_total', 'diagnostic_counts', 'diagnostics', 'origin_authenticated')}


def test_bundle_permutations_preserve_exact_conflicts_past_example_cap_and_inputs():
    members, payloads, references = synthetic_bundle()
    for member in members:
        payloads[member['name']] = b'{"panels":22}'
        member['design_version'] = 'other-version'
    for i in range(10): payloads[f'foreign/{i}.bin'] = b''
    references *= 4
    references = deepcopy(references)
    references[0]['name'] = 'beta/import.json'
    references[1]['name'] = 'alpha/absent.json'
    expected_audit = diagnostic_signature(audit(members, payloads))
    expected_refs = diagnostic_signature(refs(members, references))
    assert expected_audit['diagnostic_total'] == 38
    assert expected_refs['diagnostic_total'] > bundle.MAX_DIAGNOSTICS
    for shift in (0, 1, 5, 13):
        permuted_members = list(reversed(members[shift:] + members[:shift]))
        permuted_payloads = dict(reversed(list(payloads.items())))
        permuted_refs = list(reversed(references[shift:] + references[:shift]))
        before = deepcopy((permuted_members, permuted_payloads, permuted_refs))
        assert diagnostic_signature(audit(permuted_members, permuted_payloads)) == expected_audit
        assert diagnostic_signature(refs(permuted_members, permuted_refs)) == expected_refs
        assert (permuted_members, permuted_payloads, permuted_refs) == before


@pytest.mark.parametrize("bad", ['text', None, [], {}, bytearray(b'x'), memoryview(b'x')])
def test_payload_type_uncertainty_keeps_binary_siblings_and_recovers(bad):
    members, payloads, _ = synthetic_bundle()
    original = dict(payloads)
    payloads[members[0]['name']] = bad
    payloads[members[1]['name']] = None
    result = audit(members, payloads)
    assert codes(result) == {'PAYLOAD_TYPE_INVALID': 2}
    assert [m['bytes_equal'] for m in result['members']] == [False] * 2 + [True] * 12
    assert payloads[members[0]['name']] is bad and payloads[members[1]['name']] is None
    payloads.update(original)  # Repair the same caller-owned mapping, not a fresh auditor instance.
    assert audit(members, payloads)['integrity_verified']
    assert not audit(members, payloads)['origin_authenticated']


def reconciliation_bundle(fixture_ids=('alpha',), failed=()):
    # Reuse the existing fully populated seven-step positive fixture, rather
    # than replacing reconciliation with an injected verdict callback.
    from .test_x05_reconcile import bound_seven_envelope
    members, payloads, references, versions = [], {}, [], {}
    for fixture in fixture_ids:
        record = bound_seven_envelope()
        assurance = record['envelope']['assurance']
        version = 'design-' + fixture
        versions[fixture] = version
        claims = [record[k] for k in ('manifest', 'import_binding', 'grid_binding')]
        claims += [assurance['manifest'], assurance['evidence']] + list(assurance['bindings'].values())
        for claim in claims: claim['fixture_id'] = fixture
        for identity in [assurance['identity']] + list(assurance['stage_identities'].values()):
            identity['version_id'] = version
        if fixture in failed: record['envelope']['import']['normal_user'] = False
        raw = json.dumps(record, sort_keys=True).encode()
        name = fixture + '/seven.json'
        payloads[name] = raw
        members.append(dict(name=name, fixture=fixture, stage='seven_step', byte_length=len(raw),
                            sha256=sha256(raw).hexdigest(), design_version=version))
        references.append(dict(name=name, fixture=fixture, stage='seven_step', design_version=version))
    return dict(members=members, payloads=payloads, references=references,
                expected_fixture_versions=versions, required_stages=['seven_step'], evidence_stage='seven_step')


def replay_envelope(source):
    return dict(inventory=dict(members=deepcopy(source['members']),
                               expected_fixture_ids=sorted(source['expected_fixture_versions']),
                               required_stages=source['required_stages']),
                references=deepcopy(source['references']), expected_fixture_versions=source['expected_fixture_versions'],
                evidence_stage=source['evidence_stage'], member_bytes_hex={k: v.hex() for k, v in source['payloads'].items()})


@pytest.mark.parametrize('failed,verdict', [((), 'PASS'), (('alpha',), 'FAIL')])
def test_adapter_preserves_integrity_separately_from_real_semantics(failed, verdict):
    source = reconciliation_bundle(failed=failed)
    before = deepcopy(source)
    result = bundle.reconcile_bundle(**source)
    assert result['integrity']['integrity_verified'] and result['bindings']['ok']
    assert result['semantic']['verdict'] == verdict
    assert result['semantic']['fixtures']['alpha']['result']['reconciliation']['steps']['1']['verdict'] == verdict
    assert result['origin_authenticated'] is False and source == before
    assert bundle.replay_bundle_json(json.dumps(replay_envelope(source))) == result


@pytest.mark.parametrize('mutation', ['missing', 'substituted', 'unbound'])
def test_adapter_never_decodes_or_reconciles_unreadable_members(mutation, monkeypatch):
    source = reconciliation_bundle()
    name = source['members'][0]['name']
    if mutation == 'missing': del source['payloads'][name]
    elif mutation == 'substituted': source['payloads'][name] += b' '
    else: source['references'][0]['name'] = 'foreign/seven.json'
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError('semantic path reached unverified or unbound member')
    monkeypatch.setattr(bundle.reconciliation, 'decode_evidence_json', forbidden)
    monkeypatch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', forbidden)
    result = bundle.reconcile_bundle(**source)
    assert result['semantic']['verdict'] == 'UNREADABLE' and calls == []
    assert result['integrity']['integrity_verified'] == (mutation == 'unbound')


def test_multi_fixture_replay_retains_clean_failure_and_unreadable_siblings_under_permutations():
    from itertools import permutations
    source = reconciliation_bundle(('alpha', 'beta', 'gamma'), failed=('gamma',))
    source['payloads']['beta/seven.json'] += b' '
    before = deepcopy(source)
    expected = bundle.reconcile_bundle(**source)
    assert expected['semantic']['verdict'] == 'UNREADABLE'
    assert {k: v['verdict'] for k, v in expected['semantic']['fixtures'].items()} == {
        'alpha': 'PASS', 'beta': 'UNREADABLE', 'gamma': 'FAIL'}
    for order in permutations(source['members']):
        changed = dict(source, members=list(order), payloads=dict(reversed(list(source['payloads'].items()))),
                       references=list(reversed(source['references'])))
        assert bundle.reconcile_bundle(**changed) == expected
        assert bundle.replay_bundle_json(json.dumps(replay_envelope(changed))) == expected
    assert source == before


@pytest.mark.parametrize('mutation,reason', [('duplicate', 'json_duplicate_key'),
    ('fixture', 'bundle_evidence_fixture_conflict'), ('version', 'bundle_evidence_design_version_conflict'),
    ('stage_version', 'bundle_evidence_design_version_conflict'), ('utf8', 'bundle_evidence_utf8_unreadable')])
def test_hash_correct_evidence_cannot_cross_fixture_or_version_boundary(mutation, reason, monkeypatch):
    source = reconciliation_bundle()
    member = source['members'][0]
    record = json.loads(source['payloads'][member['name']])
    if mutation == 'duplicate': raw = b'{"envelope":{},"envelope":{}}'
    elif mutation == 'utf8': raw = b'\xff'
    else:
        if mutation == 'fixture': record['manifest']['fixture_id'] = 'foreign'
        elif mutation == 'version': record['envelope']['assurance']['identity']['version_id'] = 'foreign'
        else: record['envelope']['assurance']['stage_identities']['csv']['version_id'] = 'foreign'
        raw = json.dumps(record).encode()
    source['payloads'][member['name']] = raw
    member.update(byte_length=len(raw), sha256=sha256(raw).hexdigest())
    before = deepcopy(source)
    def forbidden(*args, **kwargs): raise AssertionError('foreign or ambiguous evidence reconciled')
    monkeypatch.setattr(bundle.reconciliation, 'reconcile_bound_seven_steps', forbidden)
    result = bundle.reconcile_bundle(**source)
    assert result['integrity']['integrity_verified'] and result['semantic']['verdict'] == 'UNREADABLE'
    assert result['semantic']['fixtures']['alpha']['result']['reason'] == reason
    assert not result['origin_authenticated'] and source == before


@pytest.mark.parametrize('value', ['0', 'gg', 'ab cd', None, [], {}, True])
def test_replay_hex_invalidity_precedes_all_byte_allocation(value, monkeypatch):
    envelope = replay_envelope(reconciliation_bundle())
    envelope['member_bytes_hex']['alpha/seven.json'] = value
    def forbidden(*args, **kwargs): raise AssertionError('invalid hex allocated')
    monkeypatch.setattr(bundle, '_member_bytes_from_hex', forbidden)
    result = bundle.decode_replay_json(json.dumps(envelope))
    assert codes(result) == {'MEMBER_HEX_INVALID': 1} and result['envelope'] is None


def tiny_replay():
    members, references = [], []
    for fixture in ('alpha', 'beta'):
        name = fixture + '/seven.bin'
        members.append(dict(name=name, fixture=fixture, stage='seven_step', byte_length=1,
                            sha256=sha256(b'0').hexdigest(), design_version='v1'))
        references.append(dict(name=name, fixture=fixture, stage='seven_step', design_version='v1'))
    return dict(inventory=dict(members=members, expected_fixture_ids=['alpha', 'beta'], required_stages=['seven_step']),
                references=references, expected_fixture_versions={'alpha': 'v1', 'beta': 'v1'}, evidence_stage='seven_step',
                member_bytes_hex={'alpha/seven.bin': '30', 'beta/seven.bin': '30'})


@pytest.mark.parametrize('sizes,expected', [((9, 1), 'MEMBER_HEX_BYTES_EXCEEDED'), ((7, 7), 'MEMBER_HEX_TOTAL_EXCEEDED')])
def test_replay_decoded_budgets_are_checked_before_any_allocation(sizes, expected, monkeypatch):
    envelope = tiny_replay()
    for key, size in zip(envelope['member_bytes_hex'], sizes): envelope['member_bytes_hex'][key] = '00' * size
    def forbidden(*args, **kwargs): raise AssertionError('over-budget decoded bytes allocated')
    monkeypatch.setattr(bundle, '_member_bytes_from_hex', forbidden)
    result = bundle.decode_replay_json(json.dumps(envelope), max_member_bytes=8, max_total_bytes=12)
    assert codes(result) == {expected: 1} and result['envelope'] is None


def test_replay_exact_decoded_boundaries_allocate_each_member_once(monkeypatch):
    envelope = tiny_replay()
    envelope['member_bytes_hex'] = {'alpha/seven.bin': '00' * 8, 'beta/seven.bin': '00' * 4}
    calls = []
    def allocated(text): calls.append(len(text) // 2); return bytes.fromhex(text)
    monkeypatch.setattr(bundle, '_member_bytes_from_hex', allocated)
    result = bundle.decode_replay_json(json.dumps(envelope), max_member_bytes=8, max_total_bytes=12)
    assert result['ok'] and calls == [8, 4] and not result['origin_authenticated']
    assert [len(v) for v in result['envelope']['payloads'].values()] == [8, 4]


@pytest.mark.parametrize('mutation,code', [('fields', 'REPLAY_FIELDS_INVALID'), ('fixtures', 'REPLAY_FIXTURES_INVALID'),
    ('stage', 'REPLAY_STAGE_INVALID'), ('reference', 'REFERENCE_STAGE_MISSING'), ('hex_map', 'MEMBER_HEX_UNREADABLE')])
def test_replay_envelope_rejects_missing_and_foreign_bindings(mutation, code, monkeypatch):
    envelope = tiny_replay()
    if mutation == 'fields': del envelope['references']
    elif mutation == 'fixtures': envelope['expected_fixture_versions']['foreign'] = 'v1'
    elif mutation == 'stage': envelope['evidence_stage'] = 'foreign'
    elif mutation == 'reference': envelope['references'].pop()
    else: envelope['member_bytes_hex'] = None
    def forbidden(*args, **kwargs): raise AssertionError('unbound envelope allocated bytes')
    monkeypatch.setattr(bundle, '_member_bytes_from_hex', forbidden)
    result = bundle.decode_replay_json(json.dumps(envelope))
    assert codes(result) == {code: 1} and not result['origin_authenticated']


@pytest.mark.parametrize('text,code', [('{}', 'REPLAY_FIELDS_INVALID'),
    ('{"inventory":{},"inventory":{}}', 'JSON_DUPLICATE_KEY'), ('{"x":NaN}', 'JSON_NONFINITE_NUMBER')])
def test_replay_json_is_strict_and_never_reaches_semantics(text, code, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('bad replay reached semantic decoding')
    monkeypatch.setattr(bundle.reconciliation, 'decode_evidence_json', forbidden)
    result = bundle.replay_bundle_json(text)
    assert codes(result['integrity']) == {code: 1}
    assert result['semantic']['verdict'] == 'UNREADABLE' and not result['origin_authenticated']


def cli_payload(kind='PASS', *, unicode_fixture=False):
    source = reconciliation_bundle(failed=('alpha',) if kind == 'FAIL' else ())
    if kind == 'UNREADABLE': source['payloads']['alpha/seven.json'] += b' '
    envelope = replay_envelope(source)
    if unicode_fixture:
        # The bundle's explicit ASCII identifier policy is preserved. Unicode
        # IDs must be rejected identically under every interpreter encoding.
        envelope['inventory']['expected_fixture_ids'] = ['\u00e5\u2600']
        envelope['inventory']['members'][0]['fixture'] = '\u00e5\u2600'
        envelope['expected_fixture_versions'] = {'\u00e5\u2600': 'design-alpha'}
        envelope['references'][0]['fixture'] = '\u00e5\u2600'
    return json.dumps(envelope, ensure_ascii=False).encode('utf-8') if kind != 'malformed' else b'{'


def cli_command(*args):
    import sys
    return [sys.executable, '-P', '-B', str(__import__('pathlib').Path(bundle.__file__).resolve()), *args]


@pytest.mark.parametrize('kind,exit_code', [('PASS', 0), ('FAIL', 1), ('UNREADABLE', 4), ('malformed', 4)])
@pytest.mark.parametrize('args', [[], ['--replay']])
def test_stdin_cli_subprocess_verdicts_are_single_ascii_json(tmp_path, kind, exit_code, args):
    import subprocess
    raw = cli_payload(kind)
    expected = bundle.reconciliation.serialize_result(bundle.replay_bundle_json(raw.decode('utf-8'))).encode('ascii') + b'\n'
    result = subprocess.run(cli_command(*args), input=raw, capture_output=True, cwd=tmp_path, timeout=15)
    assert result.returncode == exit_code and result.stderr == b''
    assert result.stdout == expected and len(result.stdout.splitlines()) == 1
    assert not json.loads(result.stdout)['origin_authenticated']


class ShortInput:
    def __init__(self, data, width=7, failure=None):
        self.data, self.width, self.failure, self.pos, self.requests = data, width, failure, 0, []
    def read(self, count):
        self.requests.append(count)
        if self.pos == len(self.data) and self.failure is not None:
            raise self.failure('synthetic private read failure')
        result = self.data[self.pos:self.pos + min(count, self.width)]
        self.pos += len(result)
        return result


def in_process_cli(monkeypatch, reader, stdout=None):
    import io
    import sys
    from types import SimpleNamespace
    output = io.BytesIO()
    errors = io.StringIO()
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=reader))
    monkeypatch.setattr(sys, 'stdout', stdout if stdout is not None else SimpleNamespace(buffer=output))
    monkeypatch.setattr(sys, 'stderr', errors)
    code = bundle.main(['--replay'])
    return code, output.getvalue(), errors.getvalue()


@pytest.mark.parametrize('width', [1, 2, 7, 8192])
@pytest.mark.parametrize('unicode_fixture', [False, True])
def test_cli_short_reads_preserve_utf8_and_json_boundaries(monkeypatch, width, unicode_fixture):
    raw = cli_payload(unicode_fixture=unicode_fixture)
    reader = ShortInput(raw, width)
    original, calls = bundle.replay_bundle_json, []
    def replayed(text): calls.append(text); return original(text)
    monkeypatch.setattr(bundle, 'replay_bundle_json', replayed)
    expected = bundle.reconciliation.serialize_result(original(raw.decode('utf-8'))).encode('ascii') + b'\n'
    code, output, errors = in_process_cli(monkeypatch, reader)
    assert code == (4 if unicode_fixture else 0) and output == expected and not errors
    assert calls == [raw.decode('utf-8')] and reader.pos == len(raw)
    assert all(0 < count <= 8192 for count in reader.requests)


@pytest.mark.parametrize('failure', [OSError, ValueError])
@pytest.mark.parametrize('prefix', [False, True])
def test_cli_read_failures_never_replay_a_valid_prefix(monkeypatch, failure, prefix):
    reader = ShortInput(cli_payload() if prefix else b'', failure=failure)
    def forbidden(*args): raise AssertionError('read failure reached replay')
    monkeypatch.setattr(bundle, 'replay_bundle_json', forbidden)
    code, output, errors = in_process_cli(monkeypatch, reader)
    assert code == 4 and not errors
    assert codes(json.loads(output)['integrity']) == {'CLI_INPUT_UNREADABLE': 1}
    assert b'synthetic private' not in output and len(output.splitlines()) == 1


def test_cli_enforces_exact_wire_limit_before_replay(monkeypatch):
    raw = cli_payload()
    monkeypatch.setattr(bundle, 'MAX_STDIN_BYTES', len(raw))
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw))
    assert code == 0 and json.loads(output)['semantic']['verdict'] == 'PASS' and not errors
    def forbidden(*args): raise AssertionError('over-limit bytes reached replay')
    monkeypatch.setattr(bundle, 'replay_bundle_json', forbidden)
    reader = ShortInput(raw + b' ')
    code, output, errors = in_process_cli(monkeypatch, reader)
    assert code == 4 and not errors and reader.pos == len(raw) + 1
    assert codes(json.loads(output)['integrity']) == {'CLI_INPUT_TOO_LARGE': 1}


@pytest.mark.parametrize('raw', [b'\xff', b'\xf0\x9f\x99', b'{"x":"\xff"}'])
def test_cli_invalid_utf8_is_sanitized(monkeypatch, raw):
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw, 1))
    assert code == 4 and not errors and codes(json.loads(output)['integrity']) == {'CLI_INPUT_UNREADABLE': 1}


@pytest.mark.parametrize('kind', ['PASS', 'FAIL', 'UNREADABLE'])
@pytest.mark.parametrize('stage', ['write', 'flush'])
def test_cli_delivery_failure_overrides_every_semantic_exit(monkeypatch, kind, stage):
    class FailedOutput:
        def write(self, data):
            if stage == 'write': raise OSError('synthetic private output failure')
            return len(data)
        def flush(self):
            raise ValueError('synthetic private flush failure')
    code, output, errors = in_process_cli(monkeypatch, ShortInput(cli_payload(kind)), FailedOutput())
    assert code == 5 and not output and not errors


@pytest.mark.parametrize('binary', [False, True])
def test_cli_positive_short_writes_deliver_one_complete_result(monkeypatch, binary):
    from types import SimpleNamespace
    class Partial:
        def __init__(self): self.parts, self.flushes = [], 0
        def write(self, value): self.parts.append(value[:7]); return len(value[:7])
        def flush(self): self.flushes += 1
    sink = Partial()
    code, _, errors = in_process_cli(monkeypatch, ShortInput(cli_payload()), SimpleNamespace(buffer=sink) if binary else sink)
    output = (b''.join(sink.parts) if binary else ''.join(sink.parts).encode('ascii'))
    assert code == 0 and not errors and sink.flushes == 1
    assert len(output.splitlines()) == 1 and json.loads(output)['semantic']['verdict'] == 'PASS'


def test_real_closed_stdout_pipe_returns_five_without_traceback(tmp_path):
    import subprocess
    process = subprocess.Popen(cli_command('--replay'), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path)
    try:
        process.stdout.close(); process.stdout = None
        _, errors = process.communicate(input=cli_payload(), timeout=15)
        assert process.returncode == 5 and errors == b''
    finally:
        if process.poll() is None:
            process.kill(); process.communicate(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None: pipe.close()


@pytest.mark.parametrize('args,expected', [(['--help'], 0), (['--unknown'], 2), (['payload.json'], 2),
    (['--replay', 'payload.json'], 2), (['--replay=path.json'], 2)])
def test_cli_usage_never_reads_stdin_or_replays(monkeypatch, args, expected):
    import io
    import sys
    class ForbiddenInput:
        @property
        def buffer(self): raise AssertionError('usage consumed stdin')
    output, errors = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, 'stdin', ForbiddenInput())
    monkeypatch.setattr(sys, 'stdout', output); monkeypatch.setattr(sys, 'stderr', errors)
    def forbidden(*args): raise AssertionError('usage reached replay')
    monkeypatch.setattr(bundle, 'replay_bundle_json', forbidden)
    with pytest.raises(SystemExit) as stopped: bundle.main(args)
    assert stopped.value.code == expected
    if expected == 0:
        assert '0 PASS; 1 FAIL; 2 usage; 4 UNREADABLE; 5 output failure.' in output.getvalue()
        assert errors.getvalue() == ''
    else:
        assert output.getvalue() == '' and 'usage:' in errors.getvalue()


@pytest.mark.parametrize('unicode_fixture,expected_exit', [(False, 0), (True, 4)])
def test_cli_is_byte_identical_across_explicit_encoding_and_hash_profiles(tmp_path, unicode_fixture, expected_exit):
    import os
    import subprocess
    outputs = []
    for seed in ('0', '19'):
        for utf8 in ('0', '1'):
            for encoding in ('ascii:strict', 'utf-8'):
                # Do not read or copy the operator environment or credentials.
                env = dict(PYTHONHASHSEED=seed, PYTHONUTF8=utf8, PYTHONIOENCODING=encoding)
                if os.name == 'nt': env['SystemRoot'] = 'C:\\Windows'
                result = subprocess.run(cli_command('--replay'), input=cli_payload(unicode_fixture=unicode_fixture),
                                        capture_output=True, cwd=tmp_path, env=env, timeout=15)
                assert result.returncode == expected_exit and result.stderr == b''
                assert all(value < 128 for value in result.stdout)
                assert not json.loads(result.stdout)['origin_authenticated']
                outputs.append(result.stdout)
    assert len(set(outputs)) == 1


@pytest.mark.parametrize('invalid', ['none', 'text', 'bytearray', 'oversized'])
def test_stdin_return_contract_after_complete_valid_prefix_and_clean_recovery(monkeypatch, invalid):
    raw = cli_payload()
    class InvalidReturn(ShortInput):
        def read(self, count):
            if self.pos < len(self.data): return super().read(count)
            self.requests.append(count)
            return {'none': None, 'text': 'synthetic private stdin text',
                    'bytearray': bytearray(b'x'), 'oversized': b'x' * (count + 1)}[invalid]
    calls, original = [], bundle.replay_bundle_json
    def replayed(text): calls.append(text); return original(text)
    monkeypatch.setattr(bundle, 'replay_bundle_json', replayed)
    reader = InvalidReturn(raw)
    code, output, errors = in_process_cli(monkeypatch, reader)
    assert code == 4 and not errors and calls == [] and reader.pos == len(raw)
    assert codes(json.loads(output)['integrity']) == {'CLI_INPUT_UNREADABLE': 1}
    assert b'synthetic private' not in output and len(output.splitlines()) == 1
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw))
    assert code == 0 and not errors and calls == [raw.decode('utf-8')]
    assert json.loads(output)['semantic']['verdict'] == 'PASS'


@pytest.mark.parametrize('reported', [0, -1, True, None, 'overreported'])
@pytest.mark.parametrize('binary', [False, True])
def test_stdout_invalid_progress_after_prefix_stops_once(monkeypatch, reported, binary):
    from types import SimpleNamespace
    raw = cli_payload()
    expected = bundle.reconciliation.serialize_result(bundle.replay_bundle_json(raw.decode())) + '\n'
    class InvalidProgress:
        def __init__(self): self.parts, self.calls, self.flushes = [], 0, 0
        def write(self, data):
            self.calls += 1
            if self.calls == 1: self.parts.append(data[:7]); return 7
            return len(data) + 1 if reported == 'overreported' else reported
        def flush(self): self.flushes += 1
    sink = InvalidProgress()
    code, _, errors = in_process_cli(monkeypatch, ShortInput(raw), SimpleNamespace(buffer=sink) if binary else sink)
    delivered = b''.join(sink.parts).decode('ascii') if binary else ''.join(sink.parts)
    assert code == 5 and not errors and sink.calls == 2 and sink.flushes == 0
    assert delivered == expected[:7]


def borrowed_cli(monkeypatch, raw, *, args=('--replay',), fail_write=False):
    import io
    import sys
    class Buffer(io.BytesIO):
        closes = 0
        def close(self): self.closes += 1; super().close()
        def write(self, data):
            if fail_write: raise OSError('synthetic interrupted borrowed output')
            return super().write(data)
    class Stream:
        def __init__(self, data=b''): self.buffer, self.closes, self.text = Buffer(data), 0, []
        def write(self, value): self.text.append(value); return len(value)
        def flush(self): pass
        def close(self): self.closes += 1; self.buffer.close()
        def fileno(self): raise OSError('borrowed memory stream has no descriptor')
    incoming, outgoing, errors = Stream(raw), Stream(), Stream()
    monkeypatch.setattr(sys, 'stdin', incoming); monkeypatch.setattr(sys, 'stdout', outgoing); monkeypatch.setattr(sys, 'stderr', errors)
    try:
        code = bundle.main(list(args))
    except SystemExit as stopped:
        code = stopped.code
    assert all(s.closes == 0 and s.buffer.closes == 0 and not s.buffer.closed for s in (incoming, outgoing, errors))
    return code, outgoing.buffer.getvalue(), incoming, outgoing, errors


@pytest.mark.parametrize('kind,expected', [('PASS', 0), ('malformed', 4), ('delivery', 5), ('help', 0), ('usage', 2)])
def test_cli_never_closes_borrowed_streams_or_buffers(monkeypatch, kind, expected):
    args = ('--help',) if kind == 'help' else ('--unknown',) if kind == 'usage' else ('--replay',)
    code, output, incoming, outgoing, errors = borrowed_cli(monkeypatch,
        cli_payload('malformed' if kind == 'malformed' else 'PASS'), args=args, fail_write=kind == 'delivery')
    assert code == expected
    if kind in ('help', 'usage'): assert incoming.buffer.tell() == 0 and output == b''
    if kind == 'PASS': assert json.loads(output)['semantic']['verdict'] == 'PASS'
    if kind == 'malformed': assert codes(json.loads(output)['integrity']) == {'JSON_INVALID': 1}


def test_interrupted_stdin_after_partial_progress_is_sanitized_and_recovers(monkeypatch):
    raw = cli_payload()
    calls, original = [], bundle.replay_bundle_json
    def replayed(text): calls.append(text); return original(text)
    monkeypatch.setattr(bundle, 'replay_bundle_json', replayed)
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw[:len(raw)//2], failure=InterruptedError))
    assert code == 4 and not errors and calls == []
    assert codes(json.loads(output)['integrity']) == {'CLI_INPUT_UNREADABLE': 1}
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw))
    assert code == 0 and not errors and len(calls) == 1 and json.loads(output)['semantic']['verdict'] == 'PASS'


@pytest.mark.parametrize('stage', ['write', 'flush'])
@pytest.mark.parametrize('binary', [False, True])
def test_interrupted_delivery_does_not_repeat_prefix_or_replay(monkeypatch, stage, binary):
    from types import SimpleNamespace
    raw = cli_payload()
    calls, original = [], bundle.replay_bundle_json
    expected = bundle.reconciliation.serialize_result(original(raw.decode())) + '\n'
    def replayed(text): calls.append(text); return original(text)
    monkeypatch.setattr(bundle, 'replay_bundle_json', replayed)
    class Interrupted:
        def __init__(self): self.parts, self.writes, self.flushes = [], 0, 0
        def write(self, data):
            self.writes += 1
            if stage == 'write' and self.writes == 2: raise InterruptedError('synthetic private interrupted write')
            piece = data[:7]; self.parts.append(piece); return len(piece)
        def flush(self): self.flushes += 1; raise InterruptedError('synthetic private interrupted flush')
    sink = Interrupted()
    code, _, errors = in_process_cli(monkeypatch, ShortInput(raw), SimpleNamespace(buffer=sink) if binary else sink)
    delivered = b''.join(sink.parts).decode('ascii') if binary else ''.join(sink.parts)
    assert code == 5 and not errors and len(calls) == 1
    assert delivered == (expected[:7] if stage == 'write' else expected)
    assert sink.flushes == (0 if stage == 'write' else 1)
    code, output, errors = in_process_cli(monkeypatch, ShortInput(raw))
    assert code == 0 and not errors and len(calls) == 2 and output.decode('ascii') == expected


def exact_decimal_cli_payload(exponent, direction):
    source = reconciliation_bundle()
    member = source['members'][0]
    record = json.loads(source['payloads'][member['name']])
    record['envelope']['assurance']['evidence']['saved_strings'][0]['optimizers'] = 9007199254740991
    record['envelope']['grid']['dc_power'][0]['kw_dc'] = (
        '4.' + '0' * (exponent - 1) + '1' if direction > 0 else '3.' + '9' * exponent)
    raw = json.dumps(record, sort_keys=True).encode()
    source['payloads'][member['name']] = raw
    member.update(byte_length=len(raw), sha256=sha256(raw).hexdigest())
    return json.dumps(replay_envelope(source)).encode()


@pytest.mark.parametrize('exponent', [18, 30, 60])
@pytest.mark.parametrize('direction', [-1, 1])
def test_cli_serializes_tiny_deltas_and_large_counts_exactly_without_context_changes(monkeypatch, exponent, direction):
    from decimal import Decimal, getcontext, localcontext, ROUND_FLOOR
    from fractions import Fraction
    raw = exact_decimal_cli_payload(exponent, direction)
    def context_state(ctx):
        return (ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax, ctx.capitals, ctx.clamp, dict(ctx.flags), dict(ctx.traps))
    prior = context_state(getcontext())
    with localcontext() as ctx:
        ctx.prec = 7; ctx.rounding = ROUND_FLOOR; ctx.clear_flags()
        before = context_state(ctx)
        code, output, errors = in_process_cli(monkeypatch, ShortInput(raw))
        assert context_state(getcontext()) == before
    assert context_state(getcontext()) == prior
    result = json.loads(output, parse_float=Decimal)
    assert code == 1 and not errors and result['integrity']['integrity_verified']
    assert result['semantic']['verdict'] == 'FAIL' and not result['origin_authenticated']
    def nodes(value):
        yield value
        if isinstance(value, dict):
            for child in value.values(): yield from nodes(child)
        elif isinstance(value, list):
            for child in value: yield from nodes(child)
    observed = list(nodes(result))
    assert any(type(value) is int and value == 9007199254740991 for value in observed)
    assert any(type(value) is int and value == 9007199254740998 for value in observed)
    tiny = Fraction(direction, 10**exponent)
    matching = [row for row in observed if isinstance(row, dict) and 'delta' in row
                and isinstance(row['delta'], (Decimal, int)) and Fraction(row['delta']) == tiny]
    assert matching
    assert any(Fraction(row['saved']) == Fraction(48, 5) + tiny for row in matching if 'saved' in row)


def encoder_args(source):
    return dict(inventory=dict(members=source['members'], expected_fixture_ids=sorted(source['expected_fixture_versions']),
                               required_stages=source['required_stages']), references=source['references'], member_bytes=source['payloads'],
                expected_fixture_versions=source['expected_fixture_versions'], evidence_stage=source['evidence_stage'])


ENCODER_WIRE_VECTOR = (
    '{"evidence_stage":"saved","expected_fixture_versions":{"unit":"v1"},'
    '"inventory":{"expected_fixture_ids":["unit"],"members":[{"byte_length":$length,'
    '"design_version":"v1","fixture":"unit","name":"unit/saved.bin",'
    '"sha256":"$digest","stage":"saved"}],"required_stages":["saved"]},'
    '"member_bytes_hex":{"unit/saved.bin":"$hex"},"references":[{"design_version":"v1",'
    '"fixture":"unit","name":"unit/saved.bin","stage":"saved"}]}')


def literal_encoder_case(data, digest):
    member = dict(name='unit/saved.bin', fixture='unit', stage='saved', byte_length=len(data), sha256=digest, design_version='v1')
    return dict(inventory=dict(members=[member], expected_fixture_ids=['unit'], required_stages=['saved']),
                references=[dict(name=member['name'], fixture='unit', stage='saved', design_version='v1')],
                member_bytes={member['name']:data}, expected_fixture_versions={'unit':'v1'}, evidence_stage='saved')


@pytest.mark.parametrize('data,digest,raw_hex,length', [(data,digest,h,n) for (data,digest),(h,n) in
    zip(REFERENCE_VECTORS,[('',0),('00',1),('ff',1)])])
def test_encoder_matches_independent_literal_wire_vectors(data,digest,raw_hex,length,monkeypatch):
    args = literal_encoder_case(data,digest)
    before = deepcopy(args)
    def forbidden(*args): raise AssertionError('encoder recomputed a supplied digest')
    monkeypatch.setattr(bundle,'sha256',forbidden)
    expected = ENCODER_WIRE_VECTOR.replace('$length',str(length)).replace('$digest',digest).replace('$hex',raw_hex)
    result = bundle.encode_replay_json(**args)
    assert result['ok'] and result['text']==expected and result['wire_bytes']==len(expected.encode('ascii'))
    assert not result['integrity_verified'] and not result['origin_authenticated']
    assert json.loads(result['text'])['inventory']['members'][0]['sha256']==digest and args==before


def test_encoder_exact_wire_and_backslash_escape_boundaries_precede_output_allocation(monkeypatch):
    data,digest=REFERENCE_VECTORS[1]
    args=literal_encoder_case(data,digest)
    name='unit\\saved.bin'
    args['inventory']['members'][0]['name']=name;args['references'][0]['name']=name
    args['member_bytes']={name:data}
    expected=ENCODER_WIRE_VECTOR.replace('$length','1').replace('$digest',digest).replace('$hex','00').replace('unit/saved.bin',r'unit\\saved.bin')
    limit=len(expected.encode('ascii'))
    result=bundle.encode_replay_json(**args,max_wire_bytes=limit)
    assert result['ok'] and result['text']==expected and result['wire_bytes']==limit
    def forbidden(*args):raise AssertionError('over-budget encoder allocated output')
    monkeypatch.setattr(bundle,'_encode_member_hex',forbidden);monkeypatch.setattr(bundle,'_dump_replay',forbidden)
    result=bundle.encode_replay_json(**args,max_wire_bytes=limit-1)
    assert codes(result)=={'ENCODER_WIRE_BUDGET_EXCEEDED':1} and result['text'] is None
    assert result['wire_bytes_lower_bound']==limit


@pytest.mark.parametrize('limit',[True,-1,bundle.MAX_STDIN_BYTES+1,None])
def test_encoder_rejects_invalid_wire_limits(limit):
    args=literal_encoder_case(*REFERENCE_VECTORS[0])
    assert codes(bundle.encode_replay_json(**args,max_wire_bytes=limit))=={'ENCODER_WIRE_LIMIT_INVALID':1}


@pytest.mark.parametrize('kind,exit_code',[('PASS',0),('FAIL',1),('UNREADABLE',4)])
def test_encoder_round_trips_real_replay_and_cli_without_changing_pins(tmp_path,kind,exit_code):
    import subprocess
    source=reconciliation_bundle(('alpha','beta'),failed=('alpha',) if kind=='FAIL' else ())
    if kind=='UNREADABLE':source['payloads']['alpha/seven.json']+=b' '
    args=encoder_args(source);before=deepcopy(args)
    result=bundle.encode_replay_json(**args)
    assert result['ok'] and args==before
    encoded=json.loads(result['text'])
    assert encoded['inventory']['members']==sorted(source['members'],key=lambda row:row['name'])
    expected=bundle.reconcile_bundle(**source)
    assert bundle.replay_bundle_json(result['text'])==expected
    completed=subprocess.run(cli_command('--replay'),input=result['text'].encode('ascii'),capture_output=True,cwd=tmp_path,timeout=15)
    assert completed.returncode==exit_code and completed.stderr==b''
    assert completed.stdout==(bundle.reconciliation.serialize_result(expected)+'\n').encode('ascii')
    assert not json.loads(completed.stdout)['origin_authenticated']
    changed=deepcopy(args)
    changed['inventory']['members'].reverse();changed['inventory']['expected_fixture_ids'].reverse();changed['references'].reverse()
    changed['member_bytes']=dict(reversed(list(changed['member_bytes'].items())))
    changed['expected_fixture_versions']=dict(reversed(list(changed['expected_fixture_versions'].items())))
    assert bundle.encode_replay_json(**changed)['text']==result['text']


def test_encoder_preserves_explicitly_mismatched_length_and_digest_without_hashing(monkeypatch):
    args=literal_encoder_case(b'\x00','0'*64)
    args['inventory']['members'][0]['byte_length']=9
    def forbidden(*args):raise AssertionError('encoder replaced an explicit supplied pin')
    monkeypatch.setattr(bundle,'sha256',forbidden)
    encoded=bundle.encode_replay_json(**args)
    assert encoded['ok']
    member=json.loads(encoded['text'])['inventory']['members'][0]
    assert member['byte_length']==9 and member['sha256']=='0'*64


@pytest.mark.parametrize('mutation,code',[('metadata','MEMBER_FIELDS_INVALID'),('name','MEMBER_NAME_INVALID'),
    ('payload','PAYLOAD_TYPE_INVALID'),('missing','ENCODER_MEMBER_COVERAGE_INVALID'),
    ('reference','REFERENCE_DANGLING'),('version','REFERENCE_DESIGN_VERSION_CONFLICT'),
    ('extra_reference','ENCODER_REFERENCE_FIELDS_INVALID')])
def test_encoder_failures_are_named_immutable_and_recover(mutation,code,monkeypatch):
    args=literal_encoder_case(*REFERENCE_VECTORS[1]);original=deepcopy(args)
    if mutation=='metadata':del args['inventory']['members'][0]['sha256']
    elif mutation=='name':args['inventory']['members'][0]['name']='../bad'
    elif mutation=='payload':args['member_bytes']['unit/saved.bin']=None
    elif mutation=='missing':args['member_bytes'].clear()
    elif mutation=='reference':args['references'][0]['name']='unit/foreign.bin'
    elif mutation=='version':args['references'][0]['design_version']='foreign'
    else:args['references'][0]['extra']='foreign'
    before=deepcopy(args)
    with monkeypatch.context() as patch:
        def forbidden(*args):raise AssertionError('invalid encoder allocated output')
        patch.setattr(bundle,'_encode_member_hex',forbidden);patch.setattr(bundle,'_dump_replay',forbidden)
        result=bundle.encode_replay_json(**args)
    assert not result['ok'] and code in codes(result) and result['text'] is None
    assert args==before and len(json.dumps(result))<4096
    args.clear();args.update(original)
    assert bundle.encode_replay_json(**args)['ok']


@pytest.mark.parametrize('bad',['text',[],{},bytearray(b'x')])
def test_encoder_rejects_nonbinary_payloads_without_consuming_or_mutating_them(bad):
    args=literal_encoder_case(*REFERENCE_VECTORS[1]);args['member_bytes']['unit/saved.bin']=bad
    result=bundle.encode_replay_json(**args)
    assert codes(result)=={'PAYLOAD_TYPE_INVALID':1} and args['member_bytes']['unit/saved.bin'] is bad


def test_literal_encoder_vectors_are_stable_across_explicit_hash_profiles(tmp_path):
    import os,subprocess,sys
    from pathlib import Path
    script='''import json,sys
sys.path.insert(0,sys.argv[1])
from benches import x05_bundle as b
d=b.decode_replay_json(sys.stdin.buffer.read().decode('ascii'))['envelope']
i=dict(members=d['members'],expected_fixture_ids=sorted(d['expected_fixture_versions']),required_stages=d['required_stages'])
r=b.encode_replay_json(i,d['references'],d['payloads'],expected_fixture_versions=d['expected_fixture_versions'],evidence_stage=d['evidence_stage'])
assert r['ok'] and not r['origin_authenticated']
sys.stdout.buffer.write(r['text'].encode('ascii'))
'''
    for (data,digest),raw_hex,length in [(REFERENCE_VECTORS[0],'',0),(REFERENCE_VECTORS[1],'00',1),(REFERENCE_VECTORS[2],'ff',1)]:
        expected=ENCODER_WIRE_VECTOR.replace('$length',str(length)).replace('$digest',digest).replace('$hex',raw_hex)
        raw=dict(reversed(list(json.loads(expected).items())))
        raw['inventory']['members'][0]=dict(reversed(list(raw['inventory']['members'][0].items())))
        for seed in ('0','19'):
            env={'PYTHONHASHSEED':seed,'PYTHONUTF8':'1','PYTHONIOENCODING':'ascii:strict'}
            if os.name=='nt':env['SystemRoot']='C:\\Windows'
            completed=subprocess.run([sys.executable,'-P','-B','-c',script,str(Path(bundle.__file__).resolve().parents[1])],
                input=json.dumps(raw).encode('ascii'),capture_output=True,cwd=tmp_path,env=env,timeout=15)
            assert completed.returncode==0 and completed.stderr==b'' and completed.stdout==expected.encode('ascii')
