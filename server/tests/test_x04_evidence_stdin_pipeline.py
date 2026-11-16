"""X04 synthetic evidence streams through the two real CLI entry points."""
import json
import subprocess
import sys
import pytest
from test_x04_untrusted_derived_counts import X04_ROOT, worked_example


@pytest.mark.parametrize('truncated', [False, True])
def test_export_to_validator_stdin_pipe(truncated, tmp_path):
    encoded = json.dumps(worked_example()).encode('utf-8')
    if truncated:
        encoded = encoded[:len(encoded) // 2]
    exporter = [sys.executable, '-P', str(X04_ROOT / 'scripts/export_x04_offline_evidence.py'), '-']
    validator = [sys.executable, '-P', str(X04_ROOT / 'scripts/validate_x04_offline_evidence.py'), '-']
    with subprocess.Popen(exporter, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=tmp_path) as producer:
        with subprocess.Popen(validator, stdin=producer.stdout, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, cwd=tmp_path) as consumer:
            producer.stdout.close()
            producer.stdin.write(encoded)
            producer.stdin.close()
            output, validation_error = consumer.communicate(timeout=30)
            export_error = producer.stderr.read()
            export_status = producer.wait(timeout=30)
    if truncated:
        assert export_status == consumer.returncode == 2
        assert output == b'' and b'refused' in export_error and b'refused' in validation_error
        assert b'Traceback' not in export_error + validation_error
    else:
        assert export_status == consumer.returncode == 0
        assert export_error == validation_error == b''
        result = json.loads(output)
        assert result['valid'] is True
        assert result['recomputed_result'] == {'count': 7, 'highlight_handles': ['A', 'B'],
                                                'original_bytes_integrity': 'unverified'}


@pytest.mark.parametrize('script', ['export_x04_offline_evidence.py', 'validate_x04_offline_evidence.py'])
def test_truncated_stdin_preserves_existing_output(script, tmp_path):
    target = tmp_path / 'output.json'
    target.write_bytes(b'previous complete output')
    result = subprocess.run([sys.executable, '-P', str(X04_ROOT / 'scripts' / script), '-',
                             '--out', str(target)], input=b'{"schema":', capture_output=True,
                            cwd=tmp_path, timeout=30)
    assert result.returncode == 2 and result.stdout == b''
    assert b'refused' in result.stderr and b'Traceback' not in result.stderr
    assert target.read_bytes() == b'previous complete output' and not list(tmp_path.glob('.x04-*'))
