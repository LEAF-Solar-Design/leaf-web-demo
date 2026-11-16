"""Zero, one and all-invalid batches have distinct complete terminal outcomes."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example
from batch_validate_x04_evidence import main


@pytest.mark.parametrize('mode', ['none', 'one', 'all-invalid'])
def test_terminal_boundary_and_valid_retry(mode, tmp_path, capsys):
    good = tmp_path / 'good'; good.write_text(json.dumps(worked_example()), encoding='utf-8')
    broken = tmp_path / 'broken'; broken.write_bytes(b'{broken')
    directory = tmp_path / 'directory'; directory.mkdir()
    missing = tmp_path / 'missing'
    target = tmp_path / 'output'; target.write_bytes(b'previous')
    paths = [] if mode == 'none' else ([good] if mode == 'one' else [missing, directory, broken, missing])
    args = [*map(str, paths), '--out', str(target)]
    if mode == 'none':
        with pytest.raises(SystemExit) as caught: main(args)
        assert caught.value.code == 2 and target.read_bytes() == b'previous'
        streams = capsys.readouterr()
        assert streams.out == '' and 'usage:' in streams.err
    else:
        assert main(args) == (0 if mode == 'one' else 1)
        streams = capsys.readouterr(); assert streams.out == streams.err == ''
        report = json.loads(target.read_bytes())
        assert [row['index'] for row in report['results']] == list(range(len(paths)))
        assert [row['path'] for row in report['results']] == list(map(str, paths))
        assert (report['total'], report['passed'], report['failed']) == ((1, 1, 0) if mode == 'one' else (4, 0, 4))
        assert report['original_bytes_integrity'] == 'unverified'
    assert not list(tmp_path.glob('.x04-*'))
    assert main([str(good), '--out', str(target)]) == 0
    assert json.loads(target.read_bytes())['passed'] == 1 and capsys.readouterr().err == ''
