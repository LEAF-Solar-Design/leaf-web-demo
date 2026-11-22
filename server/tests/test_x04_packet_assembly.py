"""Explicit-file assembly validates before publication and preserves every source."""
import json
from copy import deepcopy
from pathlib import Path
import pytest
from test_x04_packet_cross_binding import packet
import assemble_x04_evidence_packet as assembler
import export_x04_offline_evidence as writer
from validate_x04_evidence_packet import validate_packet


def manifest_files(directory):
    value = packet()
    manifest = {'schema': assembler.MANIFEST_SCHEMA}
    for section in ('artifacts', 'comparisons', 'batches'):
        manifest[section] = []
        for row in value[section]:
            field = 'artifact' if section == 'artifacts' else 'report'
            name = row['id'] + '.json'
            (directory / name).write_text(json.dumps(row[field]), encoding='utf-8')
            manifest[section].append({**{k: deepcopy(v) for k, v in row.items() if k != field}, 'path': name})
    path = directory / 'manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    return path, manifest, value


def test_success_from_unrelated_cwd_and_explicit_reads_only(tmp_path, monkeypatch, capsys):
    source, manifest, value = manifest_files(tmp_path)
    snapshots = {p: p.read_bytes() for p in tmp_path.iterdir()}
    opened = []; read = assembler.read_json
    def counted(path, limit):
        opened.append(Path(path))
        return read(path, limit)
    monkeypatch.setattr(assembler, 'read_json', counted)
    other = tmp_path / 'other'; other.mkdir(); monkeypatch.chdir(other)
    output = tmp_path / 'packet.json'
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert capsys.readouterr().out == ''
    assert json.loads(output.read_text()) == value
    assert validate_packet(json.loads(output.read_text()))['valid']
    assert set(opened) == set(snapshots)
    assert all(p.read_bytes() == data for p, data in snapshots.items())


@pytest.mark.parametrize('failure', ['forged', 'stale-report', 'missing', 'output-budget', 'replace'])
def test_refusal_preserves_output_and_retry(tmp_path, monkeypatch, capsys, failure):
    source, manifest, value = manifest_files(tmp_path)
    output = tmp_path / 'packet.json'; output.write_bytes(b'prior output')
    arguments = [str(source), '--out', str(output)]
    a = tmp_path / 'a.json'; original = a.read_bytes()
    report = tmp_path / 'c.json'; original_report = report.read_bytes()
    with monkeypatch.context() as patch:
        if failure == 'forged':
            bad = json.loads(original); bad['result']['count'] += 1
            a.write_text(json.dumps(bad), encoding='utf-8')
        elif failure == 'stale-report':
            from compare_x04_offline_evidence import compare_evidence
            item = value['artifacts'][0]['artifact']
            report.write_text(json.dumps(compare_evidence(item, item)), encoding='utf-8')
        elif failure == 'missing':
            a.unlink()
        elif failure == 'output-budget':
            arguments += ['--max-output-bytes', '1']
        else:
            def fail(*args): raise OSError('injected replacement failure')
            patch.setattr(writer.os, 'replace', fail)
        assert assembler.main(arguments) == 2
    assert output.read_bytes() == b'prior output'
    assert not list(tmp_path.glob('.x04-*')) and capsys.readouterr().out == ''
    a.write_bytes(original)
    report.write_bytes(original_report)
    assert assembler.main([str(source), '--out', str(output)]) == 0
    assert json.loads(output.read_text()) == value


@pytest.mark.parametrize('source_name', ['manifest.json', 'a.json', 'c.json', 'batch.json'])
@pytest.mark.parametrize('alias', ['same', 'normalized', 'hardlink', 'symlink'])
def test_refuses_alias_to_every_source_before_payload_reads(tmp_path, monkeypatch, source_name, alias):
    source, manifest, value = manifest_files(tmp_path)
    target = tmp_path / source_name
    before = target.read_bytes()
    output = target
    if alias == 'normalized':
        directory = tmp_path / 'nested'; directory.mkdir()
        output = directory / '..' / source_name
    elif alias in ('hardlink', 'symlink'):
        output = tmp_path / 'alias.json'
        try:
            output.hardlink_to(target) if alias == 'hardlink' else output.symlink_to(target)
        except OSError as error:
            pytest.skip(str(error))
    read = assembler.read_json; opened = []
    def counted(path, limit):
        opened.append(Path(path)); return read(path, limit)
    monkeypatch.setattr(assembler, 'read_json', counted)
    assert assembler.main([str(source), '--out', str(output)]) == 2
    assert opened == ([] if source_name == 'manifest.json' else [source.resolve()])
    assert target.read_bytes() == before and not list(tmp_path.glob('.x04-*'))
