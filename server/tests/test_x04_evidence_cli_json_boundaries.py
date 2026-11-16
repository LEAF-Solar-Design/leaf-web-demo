"""Both X04 evidence CLIs require a single strict JSON document."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example, export_main, validate_main


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('variant', ['duplicate-root', 'duplicate-nested', 'NaN', 'Infinity', '-Infinity', 'overflow', 'trailing'])
def test_nonstandard_or_ambiguous_json_refuses_atomically(main, variant, tmp_path, capsys):
    text = json.dumps(worked_example())
    if variant == 'duplicate-root':
        text = '{"synthetic":false,' + text[1:]
    elif variant == 'duplicate-nested':
        text = text.replace('"count": 7', '"count": 0, "count": 7')
    elif variant == 'trailing':
        text += '\n{}'
    else:
        number = '1e999' if variant == 'overflow' else variant
        text = text[:-1] + ',"extra":' + number + '}'
    source = tmp_path / 'input.json'
    source.write_text(text, encoding='utf-8')
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous')
    assert main([str(source), '--out', str(target)]) == 2
    streams = capsys.readouterr()
    assert streams.out == '' and 'refused' in streams.err
    assert target.read_bytes() == b'previous'
    assert not list(tmp_path.glob('.x04-*'))
