"""Synthetic X06 grading boundaries, independent of any product response."""
import hashlib
import json
import sys

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
