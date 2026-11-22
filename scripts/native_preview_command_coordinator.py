"""Dormant callback coordinator; no native, server or runtime entrypoint.

Injected producers remain responsible for real membership/lease authority,
fresh inspector handles and native permit enforcement at the effect boundary.
The coordinator records uncertainty rather than repeating an unknown effect.
Close only requests owner teardown; it never claims cleanup or native closure.
"""
import copy
import hashlib
import json

import native_preview_session_contract as contract


class CoordinatorError(RuntimeError):
    def __init__(self):
        super().__init__('Native session command unavailable or uncertain.')


def _need(condition):
    if not condition:
        raise CoordinatorError()


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


class CommandCoordinator:
    def __init__(self, journal, *, authorize, effect, clock, resolve_handles=None):
        _need(callable(authorize) and callable(effect) and callable(clock)
              and (resolve_handles is None or callable(resolve_handles)))
        self.journal = journal
        self.authorize = authorize
        self.effect = effect
        self.clock = clock
        self.resolve_handles = resolve_handles

    def _now(self):
        value = self.clock()
        contract._finite(value)
        return value

    def _authority(self, mode):
        context = copy.deepcopy(self.journal.context)
        _need(hashlib.sha256(_encode(context)).hexdigest() == self.journal.binding)
        value = self.authorize(context, mode)
        _need(type(value) is dict and set(value) == {'context_sha256', 'mode', 'allowed'}
              and value['context_sha256'] == self.journal.binding
              and value['mode'] == mode and value['allowed'] is True)

    def _validate(self, raw, sequence, phase):
        context = dict(self.journal.context, next_sequence=sequence,
                       state='active' if phase == 'active' else 'closing')
        return contract.validate_command(raw, context, now=self._now())

    def _handle(self, value):
        _need(self.resolve_handles is not None)
        args = value['arguments']
        witness = self.resolve_handles(copy.deepcopy(self.journal.context),
                                       args['snapshot_id'], args['element_id'])
        _need(type(witness) is dict and set(witness) ==
              {'context_sha256', 'source_commit', 'snapshot_id', 'element_id', 'resolved'}
              and witness['context_sha256'] == self.journal.binding
              and witness['source_commit'] == value['source_commit']
              and witness['snapshot_id'] == args['snapshot_id']
              and witness['element_id'] == args['element_id']
              and witness['resolved'] is True)

    def _result(self, result, decision):
        _need(type(result) is dict and set(result) ==
              {'context_sha256', 'request_id', 'command_sha256', 'sequence', 'outcome', 'evidence_sha256'}
              and result['context_sha256'] == self.journal.binding
              and type(result['sequence']) is int
              and all(result[key] == decision[key] for key in
                      ('request_id', 'command_sha256', 'sequence'))
              and type(result['outcome']) is str
              and result['outcome'] in ('complete', 'failed', 'unknown'))
        contract._digest(result['evidence_sha256'], 64)

    def _unknown(self, decision, evidence):
        if evidence is None:
            evidence = hashlib.sha256(_encode(dict(context_sha256=self.journal.binding,
                command_sha256=decision['command_sha256'], disposition='coordinator-uncertain'))).hexdigest()
        try:
            self.journal.finish(decision['request_id'], decision['command_sha256'], 'unknown', evidence)
        except Exception:
            pass  # A failed write leaves the durable pending intent intact.

    def _execute(self, raw):
        _need(type(raw) is bytes and 0 < len(raw) <= contract.MAX_COMMAND)
        parsed = json.loads(raw.decode('utf-8'), object_pairs_hook=contract._pairs,
                            parse_constant=contract._constant)
        _need(type(parsed) is dict)
        snapshot = self.journal.snapshot()
        # A historical same-request replay can use its original sequence. The
        # journal checks the exact stored digest and never dispatches it again.
        value = self._validate(raw, parsed.get('sequence'), snapshot['phase'])
        if value['operation'] == 'close':
            self._validate(raw, snapshot['next_sequence'], snapshot['phase'])
            self._authority('teardown')
            self.journal.start_closing()
            state = self.journal.snapshot()
            return dict(status='closed' if state['phase'] == 'closed' else 'teardown-required',
                        dispatch=False, context_sha256=self.journal.binding)
        self._authority('effect')
        decision = self.journal.prepare(raw, now=self._now())
        if not decision['dispatch']:
            return dict(record=decision, replayed=True)
        evidence = None
        try:
            self._authority('effect')
            self._validate(raw, decision['sequence'], self.journal.snapshot()['phase'])
            if value['operation'] in ('tap', 'set_text'):
                self._handle(value)
                # Resolution may block or lose authority. Check again before
                # the callback, whose real adapter must also enforce permits.
                self._authority('effect')
                self._validate(raw, decision['sequence'], self.journal.snapshot()['phase'])
            result = self.effect(copy.deepcopy(value), copy.deepcopy(self.journal.context), decision.copy())
            self._result(result, decision)
            evidence = result['evidence_sha256']
            self._authority('effect')
            self._validate(raw, decision['sequence'], self.journal.snapshot()['phase'])
            record = self.journal.finish(decision['request_id'], decision['command_sha256'],
                                         result['outcome'], evidence)
            return dict(record=record, replayed=False)
        except Exception:
            self._unknown(decision, evidence)
        raise CoordinatorError() from None

    def execute(self, raw):
        """Produce a bound recorded outcome or a fixed error, never a retry."""
        try:
            return self._execute(raw)
        except Exception:
            pass
        raise CoordinatorError() from None
