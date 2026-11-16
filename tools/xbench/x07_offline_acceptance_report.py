"""Aggregate source-bound X07 synthetic receipts without network access."""
import argparse
import json
import hashlib
import math
from pathlib import Path
import re
import os
import tempfile


class EvidenceError(ValueError):
    pass


REQUIRED_SOURCE_FILES = frozenset({
    'tools/xbench/x07_synthetic_journey.py',
    'tools/xbench/x07_offline_acceptance_report.py',
    'server/solar_drawing_roles.py', 'server/x07_customer_profile_store.py',
    'server/x07_profile_compatibility.py', 'server/x07_role_selection.py',
})


REQUIRED_ASSERTIONS = {
    1: ('fresh_proposal', 'correct_first_choice', 'confirmation_model_count'),
    2: ('competing_sets', 'counts_match_oracles', 'one_copy_count'),
    3: ('customer_scopes_differ', 'profile_ids_differ', 'initial_versions'),
    4: ('both_counts_match', 'no_profile_edits'),
    5: ('own_profile', 'alias_explicit', 'single_copy_count'),
    6: ('both_incompatible_drawings_fresh', 'no_applied_foreign_profile', 'explicit_scope_refused', 'no_wrong_application'),
    7: ('new_version', 'stable_profile_id', 'old_project_retains_version', 'new_project_pins_latest'),
}


def unique_members(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError('DUPLICATE_RECEIPT_KEY')
        result[key] = value
    return result


def reject_constant(value):
    raise EvidenceError('NONFINITE_RECEIPT_VALUE')


def receipt_integer(token):
    # Explicit local bound, independent of the interpreter's configurable limit.
    if len(token.lstrip('-')) > 4096:
        raise EvidenceError('RECEIPT_INTEGER_BUDGET_EXCEEDED')
    return int(token)


def receipt_float(token):
    value = float(token)
    if not math.isfinite(value):
        raise EvidenceError('NONFINITE_RECEIPT_VALUE')
    return value


class SourceBudget:
    def __init__(self, limits=None):
        limits = limits or {}
        if set(limits) - {'file_bytes', 'total_bytes'} or any(type(value) is not int or value < 0 for value in limits.values()):
            raise EvidenceError('INVALID_SOURCE_BUDGET')
        self.file_bytes = limits.get('file_bytes', 8 * 1024 * 1024)
        self.total_bytes = limits.get('total_bytes', 64 * 1024 * 1024)
        self.used = 0

    def digest(self, path):
        digest = hashlib.sha256()
        file_read = 0
        try:
            with path.open('rb') as stream:
                while True:
                    size = min(65536, self.file_bytes - file_read + 1, self.total_bytes - self.used + 1)
                    chunk = stream.read(size)
                    if not chunk:
                        break
                    file_read += len(chunk)
                    self.used += len(chunk)
                    if file_read > self.file_bytes or self.used > self.total_bytes:
                        raise EvidenceError('SOURCE_READ_BUDGET_EXCEEDED')
                    digest.update(chunk)
        except OSError:
            raise EvidenceError('UNREADABLE_SOURCE_FILE') from None
        return digest.hexdigest()


def check_json_budget(text, limits):
    limits = limits or {}
    if (set(limits) - {'depth', 'nodes'} or any(type(value) is not int or value < 0 for value in limits.values())
            or limits.get('depth', 32) > 128 or limits.get('nodes', 100000) > 1000000):
        raise EvidenceError('INVALID_JSON_BUDGET')
    maximum_depth = limits.get('depth', 32)
    maximum_nodes = limits.get('nodes', 100000)
    depth = nodes = 0
    quoted = escaped = primitive = False
    for char in text:
        if quoted:
            if escaped: escaped = False
            elif char == '\\': escaped = True
            elif char == '"': quoted = False
            continue
        if char.isspace() or char in ',:':
            primitive = False
            continue
        if char in '}]':
            depth -= 1
            primitive = False
            continue
        if char in '{[':
            depth += 1
            nodes += 1
            primitive = False
        elif char == '"':
            quoted = True
            primitive = False
            nodes += 1
        elif not primitive:
            nodes += 1
            primitive = True
        if depth > maximum_depth or nodes > maximum_nodes:
            raise EvidenceError('JSON_STRUCTURE_BUDGET_EXCEEDED')


def read_receipt(path, *, limits=None):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError()
        text = raw.decode('utf-8')
        check_json_budget(text, limits)
        value = json.loads(text, object_pairs_hook=unique_members, parse_constant=reject_constant,
                           parse_int=receipt_integer, parse_float=receipt_float)
        if not isinstance(value, dict):
            raise ValueError()
        pending = [value]
        while pending:
            child = pending.pop()
            if isinstance(child, str):
                child.encode('utf-8', errors='strict')
            elif isinstance(child, dict):
                pending.extend(child.keys())
                pending.extend(child.values())
            elif isinstance(child, list):
                pending.extend(child)
        return value
    except FileNotFoundError:
        raise EvidenceError('MISSING_RECEIPT') from None
    except EvidenceError:
        raise
    except (ValueError, UnicodeError, OSError, RecursionError):
        raise EvidenceError('MALFORMED_RECEIPT') from None


def check_source(receipt, expected):
    identities = [receipt[key] for key in ('source_sha', 'local_source') if key in receipt]
    if not identities:
        raise EvidenceError('MISSING_SOURCE_BINDING')
    if any(value != expected for value in identities):
        raise EvidenceError('SOURCE_MISMATCH')


def verify_files(receipt, source_root, *, state=None):
    if state is None:
        state = {'budget': SourceBudget(), 'files': {}}
    try:
        root = Path(source_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise EvidenceError('UNREADABLE_SOURCE_ROOT') from None
    if not root.is_dir():
        raise EvidenceError('UNREADABLE_SOURCE_ROOT')
    files = receipt.get('files')
    if not isinstance(files, dict) or not files:
        raise EvidenceError('MISSING_FILE_DIGESTS')
    if not REQUIRED_SOURCE_FILES.issubset(files):
        raise EvidenceError('INCOMPLETE_SOURCE_INVENTORY')
    identities = set()
    for name, expected in files.items():
        if not isinstance(name, str) or not isinstance(expected, str) or re.fullmatch('[0-9a-f]{64}', expected) is None:
            raise EvidenceError('MALFORMED_FILE_DIGEST')
        path = Path(name)
        if path.is_absolute() or '..' in path.parts:
            raise EvidenceError('SOURCE_PATH_ESCAPE')
        if '\\' in name or path.as_posix() != name:
            raise EvidenceError('SOURCE_PATH_NOT_CANONICAL')
        try:
            resolved = (root / path).resolve(strict=True)
            resolved.relative_to(root)
            stat = resolved.stat()
        except FileNotFoundError:
            raise EvidenceError('MISSING_SOURCE_FILE') from None
        except ValueError:
            raise EvidenceError('SOURCE_PATH_ESCAPE') from None
        except (OSError, RuntimeError):
            raise EvidenceError('UNREADABLE_SOURCE_FILE') from None
        identity = (stat.st_dev, stat.st_ino)
        if identity in identities:
            raise EvidenceError('AMBIGUOUS_SOURCE_IDENTITY')
        identities.add(identity)
        key = (root / path, root)
        bindings = state.setdefault('bindings', {})
        if key in bindings and bindings[key] != resolved:
            raise EvidenceError('SOURCE_CHANGED_DURING_AGGREGATION')
        bindings[key] = resolved
        if state['budget'].digest(resolved) != expected:
            raise EvidenceError('SOURCE_DIGEST_MISMATCH')
        state['files'][resolved] = expected
        if state.get('protected') is not None:
            state['protected'].add(resolved)


def aggregate(journey_path, proof_paths, source_sha, source_root, *, limits=None, receipt_limits=None, protected_paths=None, proof_limit=16):
    state = {'budget': SourceBudget(limits), 'files': {}, 'protected': protected_paths}
    if not isinstance(source_sha, str) or re.fullmatch('[0-9a-f]{40}', source_sha) is None:
        raise EvidenceError('INVALID_SOURCE')
    if type(proof_limit) is not int or not 1 <= proof_limit <= 16:
        raise EvidenceError('INVALID_PROOF_BUDGET')
    if not 1 <= len(proof_paths) <= proof_limit:
        raise EvidenceError('MISSING_OR_EXCESS_PROOFS')
    journey = read_receipt(journey_path, limits=receipt_limits)
    check_source(journey, source_sha)
    verify_files(journey, source_root, state=state)
    if (journey.get('schema') != 'leaf.x07.synthetic-journey.v1'
            or journey.get('proof_mode') != 'synthetic_offline' or type(journey.get('source_dirty')) is not bool):
        raise EvidenceError('MALFORMED_JOURNEY')
    steps = journey.get('steps')
    if not isinstance(steps, list) or len(steps) != 7:
        raise EvidenceError('MALFORMED_STEPS')
    results = {}
    for step in steps:
        if not isinstance(step, dict) or type(step.get('n')) is not int or step['n'] not in range(1, 8) or step['n'] in results:
            raise EvidenceError('MALFORMED_STEPS')
        assertions = step.get('assertions')
        if not isinstance(assertions, dict) or not assertions or any(type(value) is not bool for value in assertions.values()):
            raise EvidenceError('MALFORMED_ASSERTIONS')
        if set(assertions) != set(REQUIRED_ASSERTIONS[step['n']]):
            raise EvidenceError('MISSING_OR_UNKNOWN_ASSERTIONS')
        passed = all(assertions.values())
        if type(step.get('passed')) is not bool or step['passed'] != passed:
            raise EvidenceError('INCONSISTENT_STEP')
        results[step['n']] = {'n': step['n'], 'state': 'SYNTHETIC_PASS' if passed else 'SYNTHETIC_FAIL',
            'assertions': len(assertions)}
    proofs = []
    for path in proof_paths:
        proof = read_receipt(path, limits=receipt_limits)
        check_source(proof, source_sha)
        verify_files(proof, source_root, state=state)
        files = proof.get('files')
        if proof.get('schema') != 'leaf.x07.offline-proof.v1' or not isinstance(files, dict) or not files:
            raise EvidenceError('MALFORMED_PROOF')
        if any(not isinstance(name, str) or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None
               for name, digest in files.items()):
            raise EvidenceError('MALFORMED_PROOF')
        proofs.append({'receipt': Path(path).name, 'source_sha': source_sha, 'file_digests': len(files)})
    for path, expected in state['files'].items():
        if state['budget'].digest(path) != expected:
            raise EvidenceError('SOURCE_CHANGED_DURING_AGGREGATION')
    for (name, root), expected in state.get('bindings', {}).items():
        try:
            actual = name.resolve(strict=True)
            actual.relative_to(root)
        except ValueError:
            raise EvidenceError('SOURCE_PATH_ESCAPE') from None
        except (OSError, RuntimeError):
            raise EvidenceError('UNREADABLE_SOURCE_FILE') from None
        if actual != expected:
            raise EvidenceError('SOURCE_CHANGED_DURING_AGGREGATION')
    return {'schema': 'leaf.x07.offline-acceptance-report.v1', 'proof_mode': 'synthetic_offline',
        'source_sha': source_sha, 'source_dirty': journey.get('source_dirty'),
        'steps': [results[number] for number in range(1, 8)], 'local_proofs': proofs}


def check_output_path(path, protected):
    try:
        destination = path.resolve()
        for source in protected:
            source = Path(source)
            if destination == source.resolve() or (path.exists() and source.exists() and path.samefile(source)):
                raise EvidenceError('OUTPUT_ALIASES_INPUT_OR_SOURCE')
    except OSError:
        raise EvidenceError('UNREADABLE_OUTPUT_PATH') from None


def atomic_publish(path, encoded):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', dir=path.parent, prefix='.' + path.name + '.x07-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def unreadable(error):
    return {'schema': 'leaf.x07.offline-acceptance-report.v1', 'proof_mode': 'synthetic_offline',
        'error': str(error), 'steps': [{'n': number, 'state': 'UNREADABLE'} for number in range(1, 8)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--journey', type=Path, required=True)
    parser.add_argument('--proof', type=Path, action='append', required=True)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--max-file-bytes', type=int, default=8 * 1024 * 1024)
    parser.add_argument('--max-source-bytes', type=int, default=64 * 1024 * 1024)
    parser.add_argument('--max-json-depth', type=int, default=32)
    parser.add_argument('--max-json-nodes', type=int, default=100000)
    parser.add_argument('--max-proof-receipts', type=int, default=16)
    args = parser.parse_args()
    code = 0
    protected = {args.journey, *args.proof}
    try:
        result = aggregate(args.journey, args.proof, args.source_sha, args.source_root,
            limits={'file_bytes': args.max_file_bytes, 'total_bytes': args.max_source_bytes},
            receipt_limits={'depth': args.max_json_depth, 'nodes': args.max_json_nodes}, protected_paths=protected,
            proof_limit=args.max_proof_receipts)
    except EvidenceError as error:
        code = 2
        result = unreadable(error)
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out:
        try:
            check_output_path(args.out, protected)
            atomic_publish(args.out, encoded)
        except EvidenceError as error:
            code = 2
            encoded = json.dumps(unreadable(error), indent=2) + '\n'
        except OSError:
            code = 2
            encoded = json.dumps(unreadable('REPORT_PUBLICATION_FAILED'), indent=2) + '\n'
    print(encoded, end='')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
