"""Two saved-report writers keep temporary ownership and complete final JSON."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
import validate_x04_review_report as validator
import export_x04_offline_evidence as exporter


@pytest.mark.parametrize('failed_writer', [None, 'comparison', 'batch'])
def test_two_writers_and_owned_failure_cleanup(failed_writer, tmp_path, monkeypatch, capsys):
    sources, expected, originals = {}, {}, {}
    for factory in (comparison, batch):
        path = tmp_path / f'{factory.__name__}.json'
        path.write_text(json.dumps(factory()), encoding='utf-8')
        sources[factory.__name__] = path
        originals[path] = path.read_bytes()
        expected[factory.__name__] = validator.validate_report(factory())
    target = tmp_path / 'shared.json'; target.write_bytes(b'previous')
    barrier = threading.Barrier(2)
    cleaned, first_done = threading.Event(), threading.Event()
    local = threading.local()
    paths, complete, unlinks = {}, {}, []
    real_replace, real_unlink = exporter.os.replace, Path.unlink
    def replace(source, destination):
        name = local.name
        paths[name] = Path(source)
        complete[name] = json.loads(paths[name].read_bytes())
        barrier.wait(timeout=20)
        assert len(set(paths.values())) == 2 and all(path.exists() for path in paths.values())
        barrier.wait(timeout=20)
        if name == failed_writer:
            raise OSError('Synthetic writer replacement failure')
        if failed_writer:
            assert cleaned.wait(timeout=20)
            assert paths[name].exists() and not paths[failed_writer].exists()
        elif name == 'batch':
            assert first_done.wait(timeout=20)
        real_replace(source, destination)
        if name == 'comparison':
            first_done.set()
    def unlink(path, *args, **kwargs):
        unlinks.append((local.name, path))
        result = real_unlink(path, *args, **kwargs)
        cleaned.set()
        return result
    def run(name):
        local.name = name
        return validator.main([str(sources[name]), '--out', str(target)])
    with monkeypatch.context() as patch:
        patch.setattr(exporter.os, 'replace', replace)
        patch.setattr(Path, 'unlink', unlink)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {name: executor.submit(run, name) for name in sources}
            statuses = {name: future.result(timeout=30) for name, future in futures.items()}
    assert complete == expected
    assert statuses == {name: (2 if name == failed_writer else 0) for name in sources}
    winner = 'comparison' if failed_writer == 'batch' else 'batch'
    assert json.loads(target.read_bytes()) == expected[winner]
    assert unlinks == ([(failed_writer, paths[failed_writer])] if failed_writer else [])
    assert not list(tmp_path.glob('.x04-*'))
    for path, raw in originals.items():
        assert path.read_bytes() == raw
    captured = capsys.readouterr()
    assert captured.out == '' and ('refused' in captured.err) == bool(failed_writer)
