"""Synthetic X06 grading boundaries, independent of any product response."""
import base64
from contextlib import contextmanager
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from benches import x06

from benches.x06 import (author_verdict, author_window, budget_ok, compare_rows, csv_equals_table,
                        no_solar_provenance, normalize_rows, unreadable_steps)


def rows(count=2, length=100):
    return [{'layer': 'steel_test', 'count': count, 'total_length': length}]


@pytest.mark.parametrize('ids', [[], ['first'], ['second'], ['first', 'first'],
                                ['first', 'second', 'second'], ['first', 'other'],
                                ['first', 'second', 'other'], [None], [[]]])
def test_fixture_ids_rejected_before_oracle_read(tmp_path, ids):
    with pytest.raises(ValueError, match='fixture drawing'):
        x06.load_oracle(tmp_path / 'absent.json', [{'id': item} for item in ids])


def test_fixture_set_accepts_both_drawings_in_either_order(tmp_path, monkeypatch):
    oracle = {'schema': 'leaf.xbench.x06-oracle.v1', 'drawings': {
        key: {'sha256': char * 64, 'rows': rows()}
        for key, char in [('first', 'a'), ('second', 'b')]}}
    raw = json.dumps(oracle).encode('utf-8')
    path = tmp_path / 'oracle.json'
    path.write_bytes(raw)
    monkeypatch.setattr(x06, 'ORACLE_SHA256', hashlib.sha256(raw).hexdigest())
    fixtures = [{'id': key, 'sha256': char * 64}
                for key, char in [('first', 'a'), ('second', 'b')]]
    assert x06.load_oracle(path, fixtures) == oracle['drawings']
    assert x06.load_oracle(path, list(reversed(fixtures))) == oracle['drawings']


def test_exact_counts_and_half_percent_length_boundary():
    assert compare_rows(rows(length=100.5), rows()) == []
    assert compare_rows(rows(length=100.5001), rows())
    assert compare_rows(rows(count=3), rows())


def test_layer_set_is_exact_and_order_is_not_significant():
    first = rows() + [{'layer': 'beam_test', 'count': 1, 'total_length': 4}]
    assert compare_rows(list(reversed(first)), first) == []
    assert compare_rows(rows(), first)
    assert compare_rows(first, rows())


@pytest.mark.parametrize('bad', [[], rows()*2, rows(count=True), rows(count=2.5),
                                rows(length=float('nan')), rows(length=float('inf')),
                                rows(length=-1), rows(length=True)])
def test_malformed_schedule_cannot_pass(bad):
    with pytest.raises(ValueError):
        normalize_rows(bad)


@pytest.mark.parametrize('bad', [None, 7, True, 'rows', b'rows', {},
                                [None], [7], ['row'], [[]], [{}],
                                [{'layer': 'steel_test'}],
                                [{'layer': 'steel_test', 'count': 2}],
                                [{'count': 2, 'total_length': 100}],
                                [{'layer': 'steel_test', 'total_length': 100}]])
def test_row_shape_errors_are_controlled_value_errors(bad):
    with pytest.raises(ValueError):
        normalize_rows(bad)


def test_row_shape_accepts_tuple_without_changing_tolerance():
    assert normalize_rows(tuple(rows())) == normalize_rows(rows())
    assert compare_rows(rows(length=100.5), rows()) == []
    assert compare_rows(rows(length=100.5001), rows())


def test_zero_length_requires_exact_zero():
    assert compare_rows(rows(length=0), rows(length=0)) == []
    assert compare_rows(rows(length=0.00001), rows(length=0))


@pytest.mark.parametrize('bad', [rows(count=10**400), rows(count=-(10**400)),
                                rows(count=x06.MAX_EXACT_COUNT + 1),
                                rows(length=10**400), rows(length=-(10**400))])
def test_numeric_huge_integers_are_controlled_validation_errors(bad):
    with pytest.raises(ValueError):
        normalize_rows(bad)
    with pytest.raises(ValueError):
        compare_rows(bad, bad)


def test_numeric_finite_extremes_do_not_overflow_comparison():
    largest = sys.float_info.max
    assert compare_rows(rows(length=largest), rows(length=largest)) == []
    assert compare_rows(rows(length=largest), rows(length=5e-324))
    assert compare_rows(rows(length=5e-324), rows(length=largest))
    assert compare_rows(rows(length=largest), rows(length=0))
    assert compare_rows(rows(length=10**308), rows(length=1e308)) == []
    assert normalize_rows(rows(count=x06.MAX_EXACT_COUNT))


def test_numeric_huge_budget_values_are_unknown():
    assert budget_ok(10**400, 1) is None
    assert budget_ok(-(10**400), 1) is None
    assert budget_ok(1, 10**400) is None


@pytest.mark.parametrize('change,expected', [
    ({'drawing_sha256': 'b' * 64}, 'FAIL'),
    ({'drawing_sha256': None}, 'UNREADABLE'),
    ({'length_units': None}, 'UNREADABLE'),
    ({'length_units': ''}, 'UNREADABLE'),
    ({'length_units': 'feet'}, 'FAIL'),
])
def test_schedule_identity_and_units_precede_rows(monkeypatch, change, expected):
    evidence = {'drawing_sha256': 'a' * 64, 'length_units': 'inches', 'rows': rows()}
    evidence.update(change)
    def forbidden_compare(*args):
        pytest.fail('Rows compared before drawing identity and units')
    monkeypatch.setattr(x06, 'compare_rows', forbidden_compare)
    assert x06.schedule_verdict(evidence, {'sha256': 'a' * 64, 'rows': rows()}) == expected


@pytest.mark.parametrize('observed,expected', [(rows(), 'PASS'),
    (rows(length=100.5), 'PASS'), (rows(length=100.5001), 'FAIL'),
    (rows(count=3), 'FAIL'), (None, 'UNREADABLE')])
def test_schedule_valid_synthetic_table(observed, expected):
    evidence = {'drawing_sha256': 'a' * 64, 'length_units': 'inches', 'rows': observed}
    assert x06.schedule_verdict(evidence, {'sha256': 'a' * 64, 'rows': rows()}) == expected


def test_csv_is_cell_for_cell_not_numeric_tolerance():
    cells = [['Layer', 'Count', 'Total length'], ['steel_test', '2', '100.000']]
    assert csv_equals_table(b'\xef\xbb\xbfLayer,Count,Total length\r\nsteel_test,2,100.000\r\n', cells)
    assert not csv_equals_table(b'Layer,Count,Total length\nsteel_test,2,100\n', cells)
    assert not csv_equals_table(b'', cells)


@pytest.mark.parametrize('content,cells,expected', [
    (b'Layer,Count\n"beam, joist",2\n', [['Layer', 'Count'], ['beam, joist', '2']], True),
    (b'Layer,Count\n"beam\njoist",2\n', [['Layer', 'Count'], ['beam\njoist', '2']], True),
    ('Layer,Count\n梁_steel,2\n'.encode('utf-8'), [['Layer', 'Count'], ['梁_steel', '2']], True),
    (b'Layer,Count\n"beam ""A""",2\n', [['Layer', 'Count'], ['beam "A"', '2']], True),
    (b'Count,Layer\n2,steel_test\n', [['Layer', 'Count'], ['steel_test', '2']], False),
    (b'Layer,Count\nsteel_test,2\nextra,1\n', [['Layer', 'Count'], ['steel_test', '2']], False),
])
def test_csv_cell_cases_use_exact_in_memory_values(content, cells, expected):
    assert csv_equals_table(content, cells) is expected


@pytest.mark.parametrize('content', [b'\xff', b'"unterminated',
                                    b'"Layer"broken,Count\n', None])
def test_csv_malformed_exports_are_unreadable(content):
    assert csv_equals_table(content, [['Layer', 'Count']]) is None


def test_csv_export_size_is_bounded_before_parsing(monkeypatch):
    monkeypatch.setattr(x06, 'MAX_CSV_BYTES', 4)
    assert csv_equals_table(b'12345', [['12345']]) is None


def test_csv_end_of_file_cannot_impersonate_a_missing_table_row():
    assert csv_equals_table(b'', [None]) is False


def csv_artifact(content=b'Layer,Count\nsteel_test,2\n'):
    return {'bytes': len(content), 'mime': 'text/csv',
            'base64': base64.b64encode(content).decode('ascii')}


@pytest.mark.parametrize('change', [
    {'bytes': None}, {'bytes': True}, {'bytes': -1}, {'bytes': 10**400},
    {'bytes': 1}, {'bytes': 26}, {'mime': None}, {'mime': 'text/plain'},
    {'base64': None}, {'base64': '!!!'}, {'base64': 'YQ'},
    {'base64': 'YQ==\n'}, {'base64': '非ASCII'},
])
def test_spreadsheet_corrupt_artifact_is_unreadable(change):
    artifact = csv_artifact()
    artifact.update(change)
    assert x06.spreadsheet_verdict(artifact, [['Layer', 'Count'], ['steel_test', '2']]) == 'UNREADABLE'


def test_spreadsheet_valid_artifact_and_exact_cells():
    artifact = csv_artifact()
    cells = [['Layer', 'Count'], ['steel_test', '2']]
    assert x06.spreadsheet_verdict(artifact, cells) == 'PASS'
    artifact['mime'] = 'text/csv; charset=utf-8'
    assert x06.spreadsheet_verdict(artifact, cells) == 'PASS'
    assert x06.spreadsheet_verdict(artifact, [['Layer', 'Count'], ['steel_test', '3']]) == 'FAIL'
    assert x06.spreadsheet_verdict(artifact, None) == 'UNREADABLE'
    assert x06.spreadsheet_verdict(None, cells) == 'UNREADABLE'
    assert x06.spreadsheet_verdict(csv_artifact(b'\xff'), cells) == 'UNREADABLE'


@pytest.mark.parametrize('change,expected', [
    ({'published_tool_id': None}, 'UNREADABLE'),
    ({'published_tool_id': ''}, 'UNREADABLE'),
    ({'published_tool_id': 'different-tool'}, 'FAIL'),
    ({'session_id': None}, 'UNREADABLE'),
    ({'session_id': 'first-session'}, 'FAIL'),
    ({'drawing_sha256': None}, 'UNREADABLE'),
    ({'drawing_sha256': 'a' * 64}, 'FAIL'),
    ({}, 'PASS'),
])
def test_reuse_requires_same_published_tool_new_session_second_fixture(change, expected):
    first = {'published_tool_id': 'xbench-x06-synthetic', 'session_id': 'first-session'}
    second = {'published_tool_id': 'xbench-x06-synthetic', 'session_id': 'second-session',
              'drawing_sha256': 'b' * 64}
    second.update(change)
    assert x06.reuse_verdict(first, second, {'id': 'second', 'sha256': 'b' * 64}) == expected


def test_reuse_missing_original_or_wrong_fixture_is_unreadable():
    first = {'published_tool_id': 'xbench-x06-synthetic', 'session_id': 'first-session'}
    second = {'published_tool_id': 'xbench-x06-synthetic', 'session_id': 'second-session',
              'drawing_sha256': 'b' * 64}
    fixture = {'id': 'second', 'sha256': 'b' * 64}
    assert x06.reuse_verdict(None, second, fixture) == 'UNREADABLE'
    assert x06.reuse_verdict({}, second, fixture) == 'UNREADABLE'
    assert x06.reuse_verdict(first, None, fixture) == 'UNREADABLE'
    assert x06.reuse_verdict(first, second, {'id': 'first', 'sha256': 'b' * 64}) == 'UNREADABLE'


def test_solar_source_or_engine_fails_and_missing_provenance_is_unknown():
    assert no_solar_provenance('import math', ['dxf']) is True
    assert no_solar_provenance('from solar import tool', ['dxf']) is False
    assert no_solar_provenance('import math', ['SOLAR_builtin']) is False
    assert no_solar_provenance('', ['dxf']) is None
    assert no_solar_provenance('import math', []) is None


def test_budget_is_strict_and_missing_measurements_do_not_pass():
    assert budget_ok(1199.9, 59) is True
    assert budget_ok(1200, 59) is False
    assert budget_ok(1199, 60) is False
    assert budget_ok(float('nan'), 1) is None
    assert budget_ok(1, True) is None


def test_supply_probe_cannot_award_any_acceptance():
    steps = unreadable_steps('probe.json', {'generate_enabled': True})
    assert [step.n for step in steps] == list(range(1, 8))
    assert {step.verdict for step in steps} == {'UNREADABLE'}


def test_named_live_product_error_is_failure_but_auth_and_unknown_errors_are_not():
    body = {'error': {'error_code': 'BAD_PARAMS'},
            'reason_code': 'tenant_repository_unavailable'}
    assert author_verdict(503, body) == 'FAIL'
    assert author_verdict(401, body) == 'UNREADABLE'
    assert author_verdict(403, body) == 'UNREADABLE'
    assert author_verdict(503, {'error': 'down'}) == 'UNREADABLE'
    assert author_verdict(202, {'status': 'accepted'}) == 'UNREADABLE'


@pytest.mark.parametrize('body', [None, [], 'down', 503,
    {'error': None, 'reason_code': 'tenant_repository_unavailable'},
    {'error': [], 'reason_code': 'tenant_repository_unavailable'},
    {'error': 'BAD_PARAMS', 'reason_code': 'tenant_repository_unavailable'},
    {'error': {'error_code': 'BAD_PARAMS'}},
    {'error': {'error_code': 'BAD_PARAMS'}, 'reason_code': 'other'},
    {'error': {'error_code': 'INTERNAL'}, 'reason_code': 'tenant_repository_unavailable'},
    {'error': {}, 'reason_code': 'tenant_repository_unavailable'},
    {'error': {'error_code': 'BAD_PARAMS', 'reason_code': 'tenant_repository_unavailable'}},
])
def test_author_verdict_malformed_body_is_unreadable(body):
    assert author_verdict(503, body) == 'UNREADABLE'


@pytest.mark.parametrize('status', [None, '503', 503.0, True, 401, 403, 500, 502, 202])
def test_author_verdict_requires_exact_http_status(status):
    body = {'error': {'error_code': 'BAD_PARAMS'},
            'reason_code': 'tenant_repository_unavailable'}
    assert author_verdict(status, body) == 'UNREADABLE'


def test_author_window_serializes_and_releases_on_failure(tmp_path):
    path = tmp_path / 'author-window.lock'
    with pytest.raises(RuntimeError):
        with author_window(path):
            assert path.is_file()
            with pytest.raises(FileExistsError):
                with author_window(path):
                    pytest.fail('Concurrent author entered')
            raise RuntimeError('browser failed')
    assert not path.exists()


def test_author_window_preserves_foreign_replacement(tmp_path):
    path = tmp_path / 'author-window.lock'
    with author_window(path):
        path.write_text('replacement', encoding='utf-8')
    assert path.read_text() == 'replacement'


def test_author_window_does_not_overwrite_a_preexisting_lock(tmp_path):
    path = tmp_path / 'author-window.lock'
    original = b'{"bench":"X03","instance":"synthetic-peer"}'
    path.write_bytes(original)
    with pytest.raises(FileExistsError):
        with author_window(path):
            pytest.fail('A pre-existing window was entered')
    assert path.read_bytes() == original


def test_author_window_allows_its_lock_to_be_removed_during_ownership(tmp_path):
    path = tmp_path / 'author-window.lock'
    with author_window(path):
        path.unlink()
    assert not path.exists()


def test_author_window_exception_preserves_foreign_replacement(tmp_path):
    path = tmp_path / 'author-window.lock'
    foreign = b'{"bench":"X03","instance":"replacement"}'
    with pytest.raises(RuntimeError, match='synthetic failure'):
        with author_window(path):
            path.write_bytes(foreign)
            raise RuntimeError('synthetic failure')
    assert path.read_bytes() == foreign


@pytest.mark.parametrize('failure,expected', [
    ('none', 'FAIL'), ('decode', 'UNREADABLE'), ('screenshot-before', 'UNREADABLE'),
    ('screenshot-after', 'FAIL'), ('ui-timeout', 'UNREADABLE'),
    ('proof-timeout', 'FAIL'), ('sign-in', 'UNREADABLE'),
])
def test_run_failures_preserve_truthful_steps_and_cleanup(tmp_path, monkeypatch, failure, expected):
    import xbench_session
    state = {'closed': False, 'captures': 0}
    body = {'error': {'error_code': 'BAD_PARAMS'},
            'reason_code': 'tenant_repository_unavailable'}

    class Locator:
        def __init__(self, name):
            self.name = str(name)
        @property
        def first(self):
            return self
        def is_visible(self):
            return True
        def is_enabled(self):
            return True
        def click(self):
            pass
        def fill(self, value):
            assert value == x06.REQUEST
        def get_attribute(self, name):
            return 'true'
        def wait_for(self, **kwargs):
            if failure == 'ui-timeout' and 'What should' in self.name:
                raise TimeoutError('synthetic UI timeout')
            if failure == 'proof-timeout' and 'Protected tool' in self.name:
                raise TimeoutError('synthetic proof timeout')
        def inner_text(self):
            return 'Protected tool authoring could not complete'

    class Response:
        status = 503
        url = 'https://offline.invalid/api/author/stage'
        request = SimpleNamespace(method='POST')
        def json(self):
            if failure == 'decode':
                raise ValueError('synthetic response decoding failure')
            return body

    class Page:
        def get_by_role(self, role, *, name, **kwargs):
            return Locator(name)
        def get_by_label(self, name, **kwargs):
            return Locator(name)
        def get_by_text(self, name, **kwargs):
            return Locator(name)
        def locator(self, name):
            return Locator(name)
        def screenshot(self, **kwargs):
            state['captures'] += 1
            if ((failure == 'screenshot-before' and state['captures'] == 1)
                    or (failure == 'screenshot-after' and state['captures'] == 2)):
                raise OSError('synthetic screenshot failure')
        @contextmanager
        def expect_response(self, predicate, **kwargs):
            response = Response()
            assert predicate(response)
            yield SimpleNamespace(value=response)

    ctx = SimpleNamespace(source_sha='c' * 40, base='https://offline.invalid',
        out_dir=tmp_path / 'run', options={'oracle': str(tmp_path / 'unused.json')},
        fixtures=[{'id': 'first', 'path': 'synthetic.dxf', 'sha256': 'a' * 64}])

    @contextmanager
    def signed_in(*args, **kwargs):
        try:
            if failure == 'sign-in':
                raise RuntimeError('synthetic sign-in failure')
            yield Page(), {'web_build': ctx.source_sha}
        finally:
            state['closed'] = True

    monkeypatch.setattr(xbench_session, 'signed_in', signed_in)
    monkeypatch.setattr(x06, 'load_oracle', lambda *args: {})
    steps = x06._run(ctx)
    assert [step.n for step in steps] == list(range(1, 8))
    assert steps[1].verdict == expected
    assert all(step.verdict == 'UNREADABLE' for step in steps if step.n != 2)
    observed = json.loads((ctx.out_dir / 'author-supply.json').read_text())
    assert state['closed'] and observed['browser_closed']
    assert observed['source_sha'] == ctx.source_sha
    if failure != 'none':
        assert 'synthetic' in observed['error']
    if expected == 'FAIL':
        assert observed['author_response'] == body
        assert observed['owner'] == 'e3cdcd02'
        assert 'tenant_repository_unavailable' in observed['blocker']
    if failure == 'decode':
        assert observed['author_http_status'] == 503
        assert 'author_response' not in observed
