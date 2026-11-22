"""Drawing labels compare literally without case, Unicode, space or path rewriting."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_x04_comparison_delta_algebra import artifact
import validate_x04_offline_evidence as checker


@pytest.mark.parametrize('actual,wrong', [
    ('Panel', 'panel'), ('caf\u00e9', 'cafe\u0301'), (' id ', 'id'),
    ('../../private.dwg', 'private.dwg'),
])
@pytest.mark.parametrize('stdin', [False, True])
def test_exact_literal_drawing_ids_no_target_opens(actual, wrong, stdin, tmp_path, monkeypatch, capsys):
    value = artifact(0); value['drawing']['id'] = value['request']['drawing_id'] = actual
    raw = json.dumps(value, ensure_ascii=False).encode(); source = tmp_path / 'artifact.json'; source.write_bytes(raw)
    output = tmp_path / 'checked.json'; output.write_bytes(b'previous')
    original_open = Path.open; target_opens = []; export = checker.export_evidence; calls = []
    def guarded(path, *args, **kwargs):
        if str(path) in (actual, wrong) or path.suffix.lower() in ('.dwg', '.shx'):
            target_opens.append(path); raise AssertionError('Identity/dependency labels are not files to open')
        return original_open(path, *args, **kwargs)
    def counted(item): calls.append(item); return export(item)
    def run(selected):
        if stdin: monkeypatch.setattr(checker.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
        return checker.main(['-' if stdin else str(source), '--out', str(output), '--expected-drawing-id', selected])
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', guarded); patch.setattr(checker, 'export_evidence', counted)
        assert run(wrong) == 2 and calls == [] and output.read_bytes() == b'previous'
        assert run(actual) == 0 and len(calls) == 1
    result = json.loads(output.read_bytes())
    assert result['drawing_id'] == actual and result['recomputed_result']['count'] == 6
    assert result['recomputed_result']['highlight_handles'] == ['A']
    assert result['recomputed_result']['original_bytes_integrity'] == 'unverified'
    assert source.read_bytes() == raw and target_opens == [] and not list(tmp_path.glob('.x04-*'))
    assert capsys.readouterr().out == ''
