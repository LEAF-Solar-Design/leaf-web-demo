"""Sanitized diagnostics for durable corpus jobs and Lab reports."""

_FAILURE_MESSAGES = {
    'Corpus capability is unavailable.': 'capability_unavailable',
    'Corpus package identity mismatch.': 'package_identity',
    'Installed corpus module identity mismatch.': 'module_identity',
    'Corpus material custody mismatch.': 'material_custody',
    'Corpus attempt lost its lease or execution budget.': 'lease_or_budget',
    'Corpus kernel network isolation is unavailable.': 'kernel_isolation_unavailable',
    'Corpus execution requires an unprivileged app identity.': 'privileged_runtime',
    'Corpus isolated runtime is unavailable.': 'runtime_unavailable',
    'Pinned corpus evaluator did not produce a bounded result.': 'bounded_result_absent',
    'Corpus result identity mismatch.': 'result_identity',
    'Corpus terminal identity mismatch.': 'terminal_identity',
}
_FAILURE_REASONS = frozenset(_FAILURE_MESSAGES.values())


def failure_reason(error):
    """Project only fixed executor diagnostics, never paths or child output."""
    return _FAILURE_MESSAGES.get(str(error), 'unclassified')


def failure_code(error):
    reason = error.get('corpus_failure') if isinstance(error, dict) else None
    if isinstance(reason, str) and reason in _FAILURE_REASONS:
        return 'lab_corpus_failed_' + reason
    return 'lab_corpus_failed'
