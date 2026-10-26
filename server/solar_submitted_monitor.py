"""Explicit, one-shot observation of aged queued Solar jobs without coverage.

Importing this module does not load a database driver or application bootstrap.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
import sys
from uuid import UUID

from solar_project_tools import SUPPORTED_TOOLS


DIAGNOSTIC_PREFIX = "LEAF_SUBMITTED_JOB_DIAGNOSTIC "
SUMMARY_PREFIX = "LEAF_SUBMITTED_JOB_MONITOR "
_TOOLS = tuple(sorted(SUPPORTED_TOOLS))
_TOOL_PARAMS = {f"tool_{index}": tool for index, tool in enumerate(_TOOLS)}
# Keep the canonical project population aligned with canonical_jobs._graph_population.
# Only placeholders are generated here; every value is bound by the driver.
SELECTION_SQL = """
SELECT j.job_id, j.tool_name, j.created_at
FROM jobs j
WHERE j.deleted_at IS NULL AND j.kind='run'
  AND j.execution_context->>'schema'=%(schema)s
  AND j.tool_name IN (""" + ", ".join(f"%({key})s" for key in _TOOL_PARAMS) + """)
  AND j.request_tenant_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM live_projects p
              WHERE p.org_id=j.org_id AND p.project_id=j.project_id)
  AND EXISTS (SELECT 1 FROM orgs o WHERE o.org_id=j.org_id AND o.status='active')
  AND COALESCE((SELECT authority_mode FROM live_project_authority_modes
                WHERE org_id=j.org_id AND project_id=j.project_id),
               (SELECT authority_mode FROM tenant_authority_modes WHERE org_id=j.org_id),
               'legacy_sqlite')='postgres_canonical'
  AND j.status='queued' AND j.attempt<j.max_attempts
  AND j.created_at<=%(age_cutoff)s
  AND NOT EXISTS (SELECT 1 FROM canonical_worker_heartbeats h
                  WHERE h.tool_name=j.tool_name
                    AND h.observed_at>=%(heartbeat_cutoff)s
                    AND h.observed_at<=%(scan_time)s)
ORDER BY j.created_at, j.job_id
LIMIT %(row_limit)s
"""


def _seconds(value, maximum):
    number = float(value)
    if not math.isfinite(number) or not 1 <= number <= maximum:
        raise ValueError("age outside bounds")
    return number


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid database timestamp")
    return value.astimezone(timezone.utc)


def _timestamp(value):
    return _utc(value).isoformat().replace("+00:00", "Z")


def _diagnostic(row, observed_at, min_age_seconds):
    job_id, tool_name, created_at = row
    if not isinstance(job_id, (UUID, str)):
        raise ValueError("invalid job identity")
    canonical_id = str(UUID(str(job_id)))
    if isinstance(job_id, str) and canonical_id != job_id:
        raise ValueError("noncanonical job identity")
    if tool_name not in SUPPORTED_TOOLS:
        raise ValueError("invalid tool identity")
    age = (observed_at - _utc(created_at)).total_seconds()
    if age < min_age_seconds:
        raise ValueError("invalid selected job age")
    return {
        "schema": "leaf.solar-submitted-job-diagnostic.v1",
        "reason": "no_live_covering_worker",
        "job_id": canonical_id,
        "tool_name": tool_name,
        "age_seconds": math.floor(age),
        "observed_at": _timestamp(observed_at),
    }


def _connect(url, **kwargs):
    # No dotenv, application pool, schema helper, or readiness cache.
    import psycopg

    return psycopg.connect(url, **kwargs)


def read_snapshot(url, *, min_age_seconds=300, max_heartbeat_age_seconds=30,
                  limit=500, connect=None):
    """Return fully validated diagnostics from one bounded read-only snapshot.

    Errors propagate to the CLI's unavailable boundary, never to an empty result.
    The injected connector has the same context-manager contract as psycopg.
    """
    min_age_seconds = _seconds(min_age_seconds, 86400)
    max_heartbeat_age_seconds = _seconds(max_heartbeat_age_seconds, 300)
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise ValueError("limit outside bounds")
    connector = _connect if connect is None else connect
    with connector(url, connect_timeout=2, prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '2000ms'")
            cur.execute("SET LOCAL lock_timeout = '500ms'")
            cur.execute("SELECT transaction_timestamp()")
            observed_at = _utc(cur.fetchone()[0])
            args = {
                **_TOOL_PARAMS,
                "schema": "leaf.solar-project-graph-job.v1",
                "age_cutoff": observed_at - timedelta(seconds=min_age_seconds),
                "heartbeat_cutoff": observed_at - timedelta(seconds=max_heartbeat_age_seconds),
                "scan_time": observed_at,
                "row_limit": limit + 1,
            }
            cur.execute(SELECTION_SQL, args)
            rows = cur.fetchmany(limit + 1)
            diagnostics = [_diagnostic(row, observed_at, min_age_seconds) for row in rows]
            if len({item["job_id"] for item in diagnostics}) != len(diagnostics):
                raise ValueError("duplicate selected job identity")
    return observed_at, diagnostics[:limit], len(diagnostics) > limit


def _summary(state, observed_at=None, count=0, truncated=False):
    return {
        "schema": "leaf.solar-submitted-monitor.v1",
        "state": state,
        "observed_at": None if observed_at is None else _timestamp(observed_at),
        "diagnostic_count": count,
        "truncated": truncated,
    }


class _Parser(argparse.ArgumentParser):
    def __init__(self, stdout, stderr):
        super().__init__(prog="solar_submitted_monitor.py", allow_abbrev=False,
                         description="One read-only scan of aged queued Solar graph jobs.")
        self.stdout = stdout
        self.stderr = stderr

    def _print_message(self, message, file=None):
        if message:
            (self.stderr if file is sys.stderr else self.stdout).write(message)

    def error(self, message):
        # Bound usage errors and never repeat arbitrary argument/credential text.
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid invocation; use --help\n")


def _age_argument(maximum):
    def parse(value):
        try:
            return _seconds(value, maximum)
        except (ValueError, OverflowError):
            raise argparse.ArgumentTypeError("age outside bounds") from None
    return parse


def _limit_argument(value):
    try:
        limit = int(value)
        if not 1 <= limit <= 5000:
            raise ValueError()
        return limit
    except ValueError:
        raise argparse.ArgumentTypeError("limit outside bounds") from None


def main(argv=None, *, environ=None, connect=None, stdout=None, stderr=None):
    """Run once; return 0 clear/disabled, 1 uncovered, 2 unavailable, 3 truncated."""
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        parser = _Parser(stdout, stderr)
        parser.add_argument("--once", action="store_true", required=True)
        parser.add_argument("--min-age-seconds", type=_age_argument(86400), default=300)
        parser.add_argument("--max-heartbeat-age-seconds", type=_age_argument(300), default=30)
        parser.add_argument("--limit", type=_limit_argument, default=500)
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code
    except Exception:
        return 2

    diagnostics = []
    try:
        env = os.environ if environ is None else environ
        if env.get("LEAF_CANONICAL_WORKER_REQUIRED", "").strip().lower() not in {
                "1", "true", "yes", "on"}:
            summary, code = _summary("disabled"), 0
        else:
            url = env.get("DATABASE_URL", "")
            if not isinstance(url, str) or not url.strip():
                raise ValueError("missing runtime configuration")
            observed_at, diagnostics, truncated = read_snapshot(
                url, min_age_seconds=args.min_age_seconds,
                max_heartbeat_age_seconds=args.max_heartbeat_age_seconds,
                limit=args.limit, connect=connect)
            state, code = ("truncated", 3) if truncated else (
                ("uncovered", 1) if diagnostics else ("clear", 0))
            summary = _summary(state, observed_at, len(diagnostics), truncated)
    except Exception:
        diagnostics = []
        summary, code = _summary("unavailable"), 2

    try:
        # Validate and encode every row before the first observable job line.
        lines = [DIAGNOSTIC_PREFIX + json.dumps(item, allow_nan=False) + "\n"
                 for item in diagnostics]
        lines.append(SUMMARY_PREFIX + json.dumps(summary, allow_nan=False) + "\n")
        for line in lines:
            if stdout.write(line) != len(line):
                raise OSError("incomplete monitor output")
        stdout.flush()
    except Exception:
        # A broken output stream cannot reliably carry another summary.
        return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
