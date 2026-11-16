"""Refresh only affected synthetic byte boundaries in a composed pin chain."""
import hashlib
import json
from test_x04_assembly_source_pins import pinned_files
import assemble_x04_evidence_packet as assembler
import validate_x04_evidence_packet as checker


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def test_source_row_manifest_packet_chain_refusal_and_recovery(tmp_path, capsys):
    source, manifest, expected = pinned_files(tmp_path); source.write_text(json.dumps(manifest), encoding='utf-8')
    output = tmp_path / 'packet.json'; summary = tmp_path / 'summary.json'
    manifest_pin = digest(source)
    def assemble(pin):
        return assembler.main([str(source), '--require-source-pins', '--expected-manifest-sha256', pin, '--out', str(output)])
    def validate(pin):
        return checker.main([str(output), '--expected-packet-sha256', pin, '--out', str(summary)])
    assert assemble(manifest_pin) == 0; packet_bytes = output.read_bytes(); packet_pin = digest(output)
    assert validate(packet_pin) == 0; summary_bytes = summary.read_bytes()
    target = tmp_path / 'a.json'; target.write_bytes(target.read_bytes() + b' ')
    assert assemble(manifest_pin) == 2 and output.read_bytes() == packet_bytes
    manifest['artifacts'][0]['expected_sha256'] = digest(target)
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assemble(manifest_pin) == 2 and output.read_bytes() == packet_bytes
    manifest_pin = digest(source)
    assert assemble(manifest_pin) == 0 and output.read_bytes() == packet_bytes
    assert validate(packet_pin) == 0 and summary.read_bytes() == summary_bytes
    # A row-pin-only edit changes manifest bytes and then fails its source check.
    correct = manifest['artifacts'][0]['expected_sha256']
    manifest['artifacts'][0]['expected_sha256'] = '0' * 64
    source.write_text(json.dumps(manifest), encoding='utf-8')
    assert assemble(manifest_pin) == 2 and output.read_bytes() == packet_bytes
    assert assemble(digest(source)) == 2 and output.read_bytes() == packet_bytes
    manifest['artifacts'][0]['expected_sha256'] = correct
    source.write_text(json.dumps(manifest), encoding='utf-8'); manifest_pin = digest(source)
    assert assemble(manifest_pin) == 0
    # Manifest-only whitespace requires only its separately retained digest.
    source.write_bytes(source.read_bytes() + b' ')
    assert assemble(manifest_pin) == 2 and output.read_bytes() == packet_bytes
    manifest_pin = digest(source)
    assert assemble(manifest_pin) == 0 and output.read_bytes() == packet_bytes
    assert validate(packet_pin) == 0
    # Packet-only whitespace leaves source and manifest boundaries untouched.
    source_hash, target_hash = digest(source), digest(target)
    output.write_bytes(packet_bytes + b' ')
    assert validate(packet_pin) == 2 and summary.read_bytes() == summary_bytes
    assert validate(digest(output)) == 0 and summary.read_bytes() == summary_bytes
    assert (digest(source), digest(target)) == (source_hash, target_hash)
    result = json.loads(summary.read_bytes())
    assert [r['recomputed_result']['count'] for r in result['validated_artifacts']] == [6, 1]
    assert result['comparisons'][0]['report']['count'] == {'before': 6, 'after': 1, 'delta': -5}
    assert result['original_bytes_integrity'] == 'unverified' and not list(tmp_path.glob('.x04-*'))
    assert capsys.readouterr().out == ''
