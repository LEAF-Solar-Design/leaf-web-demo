"""Synthetic X06 grading boundaries, independent of any product response."""
import pytest

from benches.x06 import (author_verdict, author_window, budget_ok, compare_rows, csv_equals_table,
                        no_solar_provenance, normalize_rows, unreadable_steps)


def rows(count=2, length=100):
    return [{'layer': 'steel_test', 'count': count, 'total_length': length}]


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


def test_csv_is_cell_for_cell_not_numeric_tolerance():
    cells = [['Layer', 'Count', 'Total length'], ['steel_test', '2', '100.000']]
    assert csv_equals_table(b'\xef\xbb\xbfLayer,Count,Total length\r\nsteel_test,2,100.000\r\n', cells)
    assert not csv_equals_table(b'Layer,Count,Total length\nsteel_test,2,100\n', cells)
    assert not csv_equals_table(b'', cells)


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
