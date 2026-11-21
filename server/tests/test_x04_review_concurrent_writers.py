"""Concurrent review writers publish complete reports and clean only owned files."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import pytest
from test_x04_untrusted_derived_counts import worked_example
import compare_x04_offline_evidence as comparison
import batch_validate_x04_evidence as batch
import export_x04_offline_evidence as exporter


@pytest.mark.parametrize('failed_writer', [None, 'comparison', 'batch'])
def test_shared_destination_atomicity_and_cleanup(failed_writer, tmp_path, monkeypatch, capsys):
    source = tmp_path / 'source'; source.write_text(json.dumps(worked_example()), encoding='utf-8')
    before = source.read_bytes(); output = tmp_path / 'output'; output.write_bytes(b'previous')
    barrier = threading.Barrier(2)
    cleaned, comparison_done = threading.Event(), threading.Event()
    local = threading.local(); paths = {}; temporary_bytes = {}; unlinks = []
    original_replace, original_unlink = exporter.os.replace, Path.unlink
    def replace(source_path, target):
        name = local.name
        paths[name] = Path(source_path); temporary_bytes[name] = paths[name].read_bytes()
        barrier.wait(timeout=20)
        assert len(set(paths.values())) == 2 and all(path.exists() for path in paths.values())
        barrier.wait(timeout=20)
        if name == failed_writer: raise OSError('injected owned replacement failure')
        if failed_writer:
            assert cleaned.wait(timeout=20)
            assert paths[name].exists() and not paths[failed_writer].exists()
        elif name == 'batch':
            assert comparison_done.wait(timeout=20)
        original_replace(source_path, target)
        if name == 'comparison': comparison_done.set()
    def unlink(path, *args, **kwargs):
        unlinks.append((local.name, path))
        result = original_unlink(path, *args, **kwargs)
        cleaned.set()
        return result
    def run(name):
        local.name = name
        main = comparison.main if name == 'comparison' else batch.main
        args = [str(source)] * (2 if name == 'comparison' else 1) + ['--out', str(output)]
        return main(args)
    with monkeypatch.context() as patch:
        patch.setattr(exporter.os, 'replace', replace); patch.setattr(Path, 'unlink', unlink)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {name: executor.submit(run, name) for name in ('comparison', 'batch')}
            statuses = {name: future.result(timeout=30) for name, future in futures.items()}
    assert statuses == {name: 2 if name == failed_writer else 0 for name in statuses}
    winner = 'batch' if failed_writer != 'batch' else 'comparison'
    assert output.read_bytes() == temporary_bytes[winner]
    report = json.loads(output.read_bytes())
    if winner == 'batch': assert (report['passed'], report['failed']) == (1, 0)
    else: assert report['count']['delta'] == 0
    assert unlinks == ([] if failed_writer is None else [(failed_writer, paths[failed_writer])])
    streams = capsys.readouterr()
    assert streams.out == '' and len(streams.err.splitlines()) == (0 if failed_writer is None else 1)
    assert source.read_bytes() == before and not list(tmp_path.glob('.x04-*'))
