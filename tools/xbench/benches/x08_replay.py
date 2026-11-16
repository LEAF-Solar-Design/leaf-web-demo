"""Offline, non-scored X08 diagnostics; importing this module performs no IO."""

import argparse
from dataclasses import asdict
import json
from math import isfinite
import os
from pathlib import Path
import sys
import tempfile

from benches.x08_metrics import score


def _reject_constant(value):
    raise ValueError('nonstandard or nonfinite JSON number: ' + value)


def _finite_float(value):
    result = float(value)
    if not isfinite(result):
        _reject_constant(value)
    return result


def _refuse_alias(source, output):
    if source.resolve() == output.resolve() or (
            output.exists() and os.path.samefile(source, output)):
        raise ValueError('output must not alias sample input')


def _write_atomic(output, report):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=output.parent, prefix='.' + output.name + '.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, allow_nan=False, indent=2)
            stream.write('\n')
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('--samples', required=True, type=Path)
    parser.add_argument('--expected-modules', required=True, type=int)
    parser.add_argument('--out', required=True, type=Path)
    try:
        args = parser.parse_args(argv)
        if args.expected_modules < 0:
            raise ValueError('expected modules must be nonnegative')
        _refuse_alias(args.samples, args.out)
        samples = json.loads(args.samples.read_bytes(), parse_constant=_reject_constant,
                             parse_float=_finite_float)
        if not isinstance(samples, dict):
            raise ValueError('samples JSON must be an object')
        steps = score(samples, args.expected_modules, str(args.samples))
        verdicts = {step.verdict for step in steps}
        verdict = ('UNREADABLE' if 'UNREADABLE' in verdicts else
                   'FAIL' if 'FAIL' in verdicts else 'PASS')
        report = {'schema': 'x08_offline_replay_diagnostic_v1',
                  'diagnostic_kind': ('synthetic_non_scored' if samples.get('synthetic') is True
                                      else 'offline_non_scored'),
                  'qualified_live_grade': False, 'provenance_validated': False,
                  'steps': [asdict(step) for step in steps], 'verdict': verdict}
        _refuse_alias(args.samples, args.out)
        _write_atomic(args.out, report)
        return {'PASS': 0, 'FAIL': 1, 'UNREADABLE': 4}[verdict]
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        print('X08 replay unreadable: ' + str(exc), file=sys.stderr)
        return 4


if __name__ == '__main__':
    sys.exit(main())
