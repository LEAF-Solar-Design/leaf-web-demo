"""Evidence consumers refuse comparison and batch report schemas explicitly."""
import builtins
import io
import json
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main
from compare_x04_offline_evidence import compare_evidence, main as compare_main
from batch_validate_x04_evidence import validate_paths, main as batch_main


@pytest.mark.parametrize('report_kind', ['comparison', 'batch'])
@pytest.mark.parametrize('consumer', ['export', 'validate', 'compare', 'batch'])
def test_wrong_schema_refusal_and_retry(report_kind, consumer, tmp_path, monkeypatch, capsys):
    good = tmp_path / 'evidence'; good.write_text(json.dumps(worked_example()), encoding='utf-8')
    value = compare_evidence(worked_example(), worked_example()) if report_kind == 'comparison' else validate_paths([good])
    wrong = tmp_path / 'wrong'; wrong.write_text(json.dumps(value), encoding='utf-8')
    output = tmp_path / 'output'; output.write_bytes(b'previous')
    before = {path: path.read_bytes() for path in (good, wrong)}
    def guard(opener):
        def open_file(path, *args, **kwargs):
            assert not str(path).endswith('notes.shx')
            return opener(path, *args, **kwargs)
        return open_file
    for module in (builtins, io, os): monkeypatch.setattr(module, 'open', guard(module.open))
    main = {'export': export_main, 'validate': validate_main, 'compare': compare_main, 'batch': batch_main}[consumer]
    paths = [wrong, good] if consumer == 'compare' else [good, wrong, good] if consumer == 'batch' else [wrong]
    assert main([*map(str, paths), '--out', str(output)]) == (1 if consumer == 'batch' else 2)
    streams = capsys.readouterr(); assert streams.out == '' and 'Traceback' not in streams.err
    if consumer == 'batch':
        report = json.loads(output.read_bytes())
        assert [row['valid'] for row in report['results']] == [True, False, True]
        assert 'schema' in report['results'][1]['error']
        output.write_bytes(b'previous')
        assert main([*map(str, paths), '--out', str(output), '--max-output-bytes', '1']) == 2
        assert capsys.readouterr().out == '' and output.read_bytes() == b'previous'
    else: assert 'schema' in streams.err and output.read_bytes() == b'previous'
    valid_args = [good, good] if consumer == 'compare' else [good]
    assert main([*map(str, valid_args), '--out', str(output)]) == 0
    assert {path: path.read_bytes() for path in before} == before
    assert not list(tmp_path.glob('.x04-*'))
