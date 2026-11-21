"""Batch ceilings apply independently to every path occurrence."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example
from batch_validate_x04_evidence import main


@pytest.mark.parametrize('rotation', range(4))
def test_per_occurrence_ceilings_and_recovery(rotation, tmp_path, capsys):
    encoded = json.dumps(worked_example()).encode('utf-8')
    limit = len(encoded) + 45
    small, exact, oversized = [tmp_path / name for name in ('small', 'exact', 'oversized')]
    small.write_bytes(encoded)
    exact.write_bytes(encoded + b' ' * 45)
    oversized.write_bytes(encoded + b' ' * 46)
    originals = {path: path.read_bytes() for path in (small, exact, oversized)}
    paths = [exact, oversized, small, exact, oversized, small]
    paths = paths[rotation:] + paths[:rotation] + [small]
    assert sum(path.stat().st_size for path in paths) > limit
    assert main([*map(str, paths), '--max-input-bytes', str(limit)]) == 1
    streams = capsys.readouterr(); report = json.loads(streams.out)
    assert streams.err == '' and (report['total'], report['passed'], report['failed']) == (7, 5, 2)
    assert [row['index'] for row in report['results']] == list(range(7))
    assert [row['path'] for row in report['results']] == list(map(str, paths))
    assert [row['valid'] for row in report['results']] == [path != oversized for path in paths]
    for row in report['results']:
        if row['valid']: assert row['recomputed_result']['count'] == 7
        else: assert 'ceiling' in row['error']
    assert {path: path.read_bytes() for path in originals} == originals
    assert main([*map(str, paths), '--max-input-bytes', str(limit + 1)]) == 0
    assert json.loads(capsys.readouterr().out)['passed'] == 7
