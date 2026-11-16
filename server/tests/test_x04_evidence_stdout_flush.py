"""X04 flush failures become exit2 before interpreter shutdown can retry them."""
import io
import json
import subprocess
import sys
import pytest
from test_x04_untrusted_derived_counts import X04_ROOT, worked_example, export_main, validate_main


class FailedFlush:
    def __init__(self, error):
        self.error = error
        self.pending = ''
        self.flushes = 0

    def write(self, value):
        self.pending += value
        return len(value)

    def flush(self):
        self.flushes += 1
        raise self.error('injected deferred flush failure')


@pytest.mark.parametrize('main', [export_main, validate_main])
@pytest.mark.parametrize('error', [BrokenPipeError, ValueError])
def test_flush_only_failure_is_nonzero_without_output_replacement(main, error, tmp_path, monkeypatch):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    preserved = tmp_path / 'existing.json'
    preserved.write_bytes(b'previous')
    broken = FailedFlush(error)
    diagnostic = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', broken)
    monkeypatch.setattr(sys, 'stderr', diagnostic)
    assert main([str(source)]) == 2
    assert broken.flushes == 1 and sys.stdout is None
    assert 'refused' in diagnostic.getvalue() and 'Traceback' not in diagnostic.getvalue()
    assert preserved.read_bytes() == b'previous' and not list(tmp_path.glob('.x04-*'))


@pytest.mark.parametrize('script', ['export_x04_offline_evidence.py', 'validate_x04_offline_evidence.py'])
@pytest.mark.parametrize('error_name', ['BrokenPipeError', 'ValueError'])
def test_deferred_shutdown_flush_failure_has_no_interpreter_traceback(script, error_name, tmp_path):
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(worked_example()), encoding='utf-8')
    wrapper = '''
import runpy,sys
class DeferredFailure:
    def write(self, value):
        return len(value)
    def flush(self):
        raise ERROR_TYPE('injected shutdown flush failure')
sys.stdout = DeferredFailure()
sys.argv = [sys.argv[1], sys.argv[2]]
runpy.run_path(sys.argv[0], run_name='__main__')
'''.replace('ERROR_TYPE', error_name)
    result = subprocess.run([sys.executable, '-P', '-c', wrapper,
                             str(X04_ROOT / 'scripts' / script), str(source)],
                            capture_output=True, cwd=tmp_path, timeout=30)
    assert result.returncode == 2 and result.stdout == b''
    assert b'refused' in result.stderr
    assert b'Traceback' not in result.stderr and b'Exception ignored' not in result.stderr
