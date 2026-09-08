"""Resume canonical campaign releases without keeping a second work ledger."""
from __future__ import annotations

import logging
import math

_LOG = logging.getLogger(__name__)
_WAIT_KINDS = {'authoring', 'job', 'capacity', 'publication', 'approval'}


def run_once(runtime=None, limit=20):
    if runtime is None:
        import campaign_release_service as runtime
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('invalid release worker limit')
    counts = {'considered': 0, 'advanced': 0, 'resumed': 0, 'skipped': 0, 'failed': 0}
    rows = runtime._store().runnable_releases(limit=limit)
    seen = set()
    for row in rows[:limit]:
        identity = tuple(str(row.get(key, '')) for key in ('org_id', 'project_id', 'campaign_id', 'release_id'))
        if identity in seen:
            continue
        seen.add(identity)
        counts['considered'] += 1
        state = row.get('status')
        if state not in ('active', 'queued', 'waiting') or (
                state == 'waiting' and (row.get('next_action') or {}).get('wait_kind') not in _WAIT_KINDS):
            counts['skipped'] += 1
            continue
        try:
            actor = runtime.actor_for_release(row)
            if actor is None:
                counts['skipped'] += 1
                continue
            args = (actor, row['project_id'], row['campaign_id'], row['release_id'])
            if state == 'active':
                runtime.advance(*args)
                counts['advanced'] += 1
            else:
                runtime.resume_pending(*args)
                counts['resumed'] += 1
        except Exception as exc:
            # Exception messages may contain credentials or provider responses.
            _LOG.warning('campaign_release_worker_row_failed class=%s', type(exc).__name__)
            counts['failed'] += 1
    return counts


def serve(stop_event, interval_seconds=5):
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)) or not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError('release worker interval must be positive')
    while not stop_event.is_set():
        try:
            run_once()
        except Exception as exc:
            _LOG.warning('campaign_release_worker_scan_failed class=%s', type(exc).__name__)
        if stop_event.wait(interval_seconds):
            break
