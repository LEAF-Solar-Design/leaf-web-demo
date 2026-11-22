"""Comparison and batch output aliases refuse before any temporary publication."""
import json
import os
import pytest
from test_x04_untrusted_derived_counts import worked_example
from compare_x04_offline_evidence import main as compare_main
from batch_validate_x04_evidence import main as batch_main


@pytest.mark.parametrize('position', ['comparison-first', 'comparison-last', 'batch-first', 'batch-middle', 'batch-last'])
@pytest.mark.parametrize('alias', ['identical', 'normalized', 'hardlink', 'symlink'])
def test_each_input_alias_refuses(position, alias, tmp_path, capsys):
    paths = [tmp_path / f'input-{index}.json' for index in range(3)]
    for path in paths:
        path.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = [path.read_bytes() for path in paths]
    if position.startswith('comparison'):
        sources = paths[:2]
        chosen = sources[0 if position.endswith('first') else 1]
        main = compare_main
    else:
        sources = paths
        chosen = paths[{'batch-first': 0, 'batch-middle': 1, 'batch-last': 2}[position]]
        main = batch_main
    if alias == 'identical': output = str(chosen)
    elif alias == 'normalized':
        (tmp_path / 'sub').mkdir()
        output = str(tmp_path / 'sub' / '..' / chosen.name)
    else:
        output = str(tmp_path / 'output-alias.json')
        if alias == 'hardlink': os.link(chosen, output)
        else: os.symlink(chosen, output)
    assert main([*map(str, sources), '--out', output]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'refused' in streams.err and 'Traceback' not in streams.err
    assert [path.read_bytes() for path in paths] == before
    assert not list(tmp_path.glob('.x04-*'))
