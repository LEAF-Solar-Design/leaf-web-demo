"""Strict JSON refusal is atomic for comparison and isolated within a batch."""
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example
from compare_x04_offline_evidence import main as compare_main
from batch_validate_x04_evidence import main as batch_main


@pytest.mark.parametrize('command', ['comparison', 'batch'])
@pytest.mark.parametrize('shape', ['duplicate', 'trailing', 'array-at', 'array-over', 'object-at', 'object-over'])
def test_strict_json_boundaries(command, shape, tmp_path, capsys):
    text = json.dumps(worked_example())
    if shape == 'duplicate': variant = '{"duplicate":0,"duplicate":1,' + text[1:]
    elif shape == 'trailing': variant = text + '{}'
    else:
        opening, closing = ('[', ']') if shape.startswith('array') else ('{"value":', '}')
        depth = 255 if shape.endswith('-at') else 256
        variant = text[:-1] + ',"opaque":' + opening * depth + '0' + closing * depth + '}'
    valid, source, output = [tmp_path / name for name in ('valid', 'variant', 'out')]
    valid.write_text(text, encoding='utf-8'); source.write_text(variant, encoding='utf-8')
    before = source.read_bytes(); output.write_bytes(b'previous')
    accepted = shape.endswith('-at')
    main = compare_main if command == 'comparison' else batch_main
    paths = [source, valid] if command == 'comparison' else [valid, source, valid]
    assert main([*map(str, paths), '--out', str(output)]) == (0 if accepted else 2 if command == 'comparison' else 1)
    streams = capsys.readouterr(); assert streams.out == '' and 'Traceback' not in streams.err
    if not accepted and command == 'comparison':
        assert 'refused' in streams.err and output.read_bytes() == b'previous'
    else:
        report = json.loads(output.read_bytes()); assert streams.err == ''
        if command == 'batch':
            assert [row['valid'] for row in report['results']] == [True, accepted, True]
            assert (report['total'], report['passed'], report['failed']) == (3, 3 if accepted else 2, 0 if accepted else 1)
        else: assert report['count']['delta'] == 0
    assert source.read_bytes() == before and not list(tmp_path.glob('.x04-*'))
    source.write_text(text, encoding='utf-8')
    assert main([*map(str, paths), '--out', str(output)]) == 0
