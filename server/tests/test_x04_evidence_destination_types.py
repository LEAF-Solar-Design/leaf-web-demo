"""X04 output destination failures preserve unrelated filesystem bytes."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('destination', ['directory', 'missing-parent', 'sentinel-symlink'])
def test_destination_semantics_preserve_sentinel_and_cleanup(main, destination, tmp_path, capsys):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    original = source.read_bytes()
    sentinel = tmp_path / 'sentinel'
    sentinel.write_bytes(b'unrelated sentinel bytes\x00\xff')
    sentinel_before = sentinel.read_bytes()
    target = tmp_path / 'output'
    if destination == 'directory':
        target.mkdir()
        (target / 'marker').write_bytes(b'directory marker')
    elif destination == 'missing-parent':
        target = tmp_path / 'missing' / 'output.json'
    else:
        target.symlink_to(sentinel)
    status = main([str(source), '--out', str(target)])
    streams = capsys.readouterr()
    assert streams.out == ''
    assert source.read_bytes() == original and sentinel.read_bytes() == sentinel_before
    assert not list(tmp_path.glob('.x04-*'))
    if destination == 'sentinel-symlink':
        assert status == 0 and streams.err == '' and not target.is_symlink()
        output = json.loads(target.read_bytes())
        result = output.get('result', output.get('recomputed_result'))
        assert result['count'] == 7 and result['highlight_handles'] == ['A', 'B']
    else:
        assert status == 2 and 'refused' in streams.err and 'Traceback' not in streams.err
        if destination == 'directory':
            assert (target / 'marker').read_bytes() == b'directory marker'
            assert sorted(path.name for path in target.iterdir()) == ['marker']
        else:
            assert not target.parent.exists()
