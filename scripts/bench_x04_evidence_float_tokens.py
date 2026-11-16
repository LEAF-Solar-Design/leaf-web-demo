"""Observe X04 float-token parsing and finite-value consistency checks."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import math
from pathlib import Path
import tempfile
import time
import tracemalloc
from export_x04_offline_evidence import SCHEMA, export_evidence, main as export_main, read_json
from drawing_dependency_report import from_pairs
from validate_x04_offline_evidence import main as validate_main, validate_evidence


def measure(operation):
    tracemalloc.start()
    started = time.perf_counter()
    refused = False
    result = None
    try:
        try:
            result = operation()
        except ValueError:
            refused = True
        observation = {'seconds': time.perf_counter() - started,
                       'peak_bytes': tracemalloc.get_traced_memory()[1], 'refused': refused}
    finally:
        tracemalloc.stop()
    return result, observation


def main():
    pairs = [[0, 'STYLE'], [3, 'synthetic.shx']]
    template = export_evidence({'schema': SCHEMA, 'synthetic': True,
        'drawing': {'id': 'synthetic-float', 'dependency_pairs': pairs,
                    'future_extension': {'numeric': 'NUMBER_TOKEN'},
                    'intake': {'inserts': [{'layer': 'Panels', 'name': 'Module', 'handle': 'A'}]}},
        'observed_dependencies': from_pairs(pairs),
        'request': {'drawing_id': 'synthetic-float', 'set_ids': ['["INSERT","Panels","Module"]']}})
    text = json.dumps(template, ensure_ascii=False, separators=(',', ':'))
    assert text.count('"NUMBER_TOKEN"') == 1
    observations = []
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / 'input.json'
        target = Path(directory) / 'output.json'
        control = Path(directory) / 'control.json'
        control.write_text(text, encoding='utf-8')
        for digits in (1000, 10000, 100000):
            tokens = [('fraction', '0.' + '1' * digits, True),
                      ('leading-zero-exponent', '1e-' + '0' * (digits - 1) + '1', True),
                      ('overflow', '1e' + '9' * digits, False),
                      ('underflow', '1e-' + '9' * digits, True)]
            for kind, token, finite in tokens:
                encoded = text.replace('"NUMBER_TOKEN"', token).encode('utf-8')
                source.write_bytes(encoded)
                parsed, parsing = measure(lambda: read_json(source, len(encoded)))
                assert not parsing['refused']
                assert math.isfinite(parsed['drawing']['future_extension']['numeric']) is finite
                artifact, exporting = measure(lambda: export_evidence(parsed))
                validated, validating = measure(lambda: validate_evidence(parsed))
                assert exporting['refused'] is (not finite) and validating['refused'] is (not finite)
                if finite:
                    assert artifact['result']['count'] == 1 and validated['valid']
                for command in (export_main, validate_main):
                    target.write_bytes(b'previous')
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        status = command([str(source), '--max-input-bytes', str(len(encoded)), '--out', str(target)])
                    assert source.read_bytes() == encoded and stdout.getvalue() == ''
                    if finite:
                        assert status == 0 and stderr.getvalue() == ''
                    else:
                        assert status == 2 and 'refused' in stderr.getvalue()
                        assert target.read_bytes() == b'previous'
                        with redirect_stdout(stdout), redirect_stderr(stderr):
                            assert command([str(control), '--out', str(target)]) == 0
                    result = json.loads(target.read_bytes())
                    assert result.get('result', result.get('recomputed_result'))['count'] == 1
                    assert not list(Path(directory).glob('.x04-*'))
                observations.append({'kind': kind, 'token_digits': digits, 'input_bytes': len(encoded),
                                     'finite': finite, 'parse': parsing, 'export': exporting, 'validate': validating})
    print(json.dumps({'synthetic': True, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
