"""Explicit receipt validation, independent of paths, git, or live readiness."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
import re

from benches.x08_metrics import score


def _hash(value, length, name):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-fA-F]{%d}' % length, value) is None:
        raise ValueError(name + ' must be a strict hexadecimal hash')
    return value.lower()


def validate_provenance(fixture, reference, *, expected_fixture_sha256, expected_source_sha):
    """Validate explicit bindings; hardware mismatch remains a diagnostic."""
    fixture_hash = _hash(expected_fixture_sha256, 64, 'expected_fixture_sha256')
    source_hash = _hash(expected_source_sha, 40, 'expected_source_sha')
    for name, receipt in (('fixture', fixture), ('reference', reference)):
        if not isinstance(receipt, Mapping):
            raise ValueError(name + ' receipt required')
        if (_hash(receipt.get('source_sha'), 40, name + '.source_sha') != source_hash
                or _hash(receipt.get('fixture_sha256'), 64, name + '.fixture_sha256') != fixture_hash
                or receipt.get('source_binding_verified') is not True):
            raise ValueError(name + ' explicit verified binding required')
        if 'sha256' in receipt and _hash(receipt['sha256'], 64, name + '.sha256') != fixture_hash:
            raise ValueError(name + ' conflicting sha256')
    if type(fixture.get('bytes')) is not int or fixture['bytes'] <= 0:
        raise ValueError('fixture bytes must be a positive integer')
    hardware = reference.get('reference')
    diagnostic = score({'reference': hardware}, 0, 'reference receipt')[5]
    if diagnostic.verdict == 'UNREADABLE':
        raise ValueError('complete reference browser/hardware metadata required')
    return {'fixture_sha256': fixture_hash, 'source_sha': source_hash,
            'source_binding_verified': True, 'bytes': fixture['bytes'],
            'reference': deepcopy(hardware), 'reference_diagnostic': asdict(diagnostic)}
