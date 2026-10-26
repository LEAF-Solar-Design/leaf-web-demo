# One-shot Solar submitted-job monitor

This internal operator CLI reports aged queued canonical project Solar graph jobs
that have no fresh heartbeat for their exact tool. The API calls queued jobs
`submitted`; the database status is `queued`. A diagnostic describes coverage at
the observation time. It does not establish why a job remained queued or prove
that a worker crashed.

From the repository's `server` directory, in an explicitly authorized existing
runtime with its environment already provisioned:

```sh
python solar_submitted_monitor.py --once --min-age-seconds 300 --max-heartbeat-age-seconds 30 --limit 500
```

`--once` is mandatory. Import, `--help`, and invalid arguments do not connect or
read runtime configuration. There is no loop, scheduler, notification sender,
public endpoint, or change to admission/readiness.

## Configuration and bounds

The process scans only when `LEAF_CANONICAL_WORKER_REQUIRED`, stripped and
case-insensitive, is `1`, `true`, `yes`, or `on`. Otherwise it emits `disabled`,
exits 0, and does not read `DATABASE_URL` or connect. This flag does not establish
that the target is staging; the activating owner must confirm the runtime and
database binding.

Scanning uses only `DATABASE_URL` from that process's existing environment. The
monitor does not load dotenv files, retrieve credentials, discover environments,
or create access. Missing or invalid configuration is `unavailable`, exit 2.
Do not put credentials in CLI arguments or paste connection information into
receipts.

| Option | Default | Accepted values |
| --- | --- | --- |
| `--min-age-seconds` | 300 | Finite number, 1 through 86400 seconds inclusive |
| `--max-heartbeat-age-seconds` | 30 | Finite number, 1 through 300 seconds inclusive |
| `--limit` | 500 | Integer, 1 through 5000 inclusive |

These defaults are diagnostic policy, not a service SLO. Zero, negative, NaN,
infinite, fractional limits, and out-of-range values are invalid. Invalid
invocations emit bounded argparse usage on stderr and exit 2, without a scan.

Each invocation uses a dedicated psycopg connection with `connect_timeout=2`
and `prepare_threshold=None`. Its transaction explicitly uses `REPEATABLE READ
READ ONLY`, with transaction-local `statement_timeout=2000ms` and
`lock_timeout=500ms`. One database transaction timestamp supplies `observed_at`;
one parameterized selection query uses that same timestamp. The cursor and
connection close on success and failure. There is no application pool, retry,
schema setup, migration, row lock, or worker helper call.

## What qualifies

The selection includes only nondeleted `kind='run'` jobs with exact execution
context schema `leaf.solar-project-graph-job.v1`, an exact member of
`solar_project_tools.SUPPORTED_TOOLS`, a nonnull request tenant, a matching live
project, and an active org. Effective authority must be `postgres_canonical`:
project override, then tenant fallback, then default `legacy_sqlite`, matching
the canonical graph population. Project legacy overrides exclude jobs even
when their tenant is canonical. Only `status='queued'` jobs with
`attempt < max_attempts` qualify. Running jobs, including expired leases, and
terminal or exhausted jobs are excluded.

Age is database observation time minus original `created_at`, inclusive at the
minimum age. Future creation timestamps cannot qualify. A retry returned to
queued retains its original age even if `updated_at` is recent. This is job age;
it does not mean continuous time waiting or establish coverage when admitted.

A covering heartbeat has the exact job tool and a timestamp in the inclusive
interval `[observed_at - max_heartbeat_age_seconds, observed_at]`. Any one fresh
heartbeat covers the tool, including when another worker has a future or stale
timestamp. Absent, stale, and future heartbeats alone do not cover. Worker
revision, the default readiness tool, hostname, and process liveness are not
coverage criteria. A heartbeat proves recent coverage, not idle capacity or an
immediate ability to claim.

Eligibility, age, and the missing-coverage `NOT EXISTS` predicate are applied
before the limit. Rows sort by `created_at`, then `job_id`; multiple heartbeats
cannot multiply a job. The query fetches at most `limit + 1` rows and emits at
most `limit` diagnostics. The extra row means `truncated=true`, exit 3. Covered
old jobs never consume the result limit. A statement timeout is unavailable,
never an empty or clear result. This slice adds no indexes; scan performance
must be rehearsed against representative data before activation.

## Output and exit codes

Stdout contains one JSON object per line, preceded by the indicated literal
prefix. Successful scans validate all fetched rows, including the extra row,
before emitting diagnostics. Invalid identities or timestamps and connection,
query, decode, timeout, or transaction failures emit only an `unavailable`
summary, with no partial diagnostics or exception text. Stderr is reserved for
CLI usage/help errors. Output-stream failure returns nonzero; a broken stream
may contain an incomplete receipt, which must not be treated as a completed
scan.

Example diagnostic:

```text
LEAF_SUBMITTED_JOB_DIAGNOSTIC {"schema": "leaf.solar-submitted-job-diagnostic.v1", "reason": "no_live_covering_worker", "job_id": "00000000-0000-0000-0000-000000000001", "tool_name": "solar-settings", "age_seconds": 301, "observed_at": "2026-10-26T12:00:00Z"}
LEAF_SUBMITTED_JOB_MONITOR {"schema": "leaf.solar-submitted-monitor.v1", "state": "uncovered", "observed_at": "2026-10-26T12:00:00Z", "diagnostic_count": 1, "truncated": false}
```

Diagnostic fields are exactly `schema`, `reason`, `job_id` (canonical UUID),
`tool_name`, `age_seconds` (nonnegative integer floor), and `observed_at` (UTC ISO
timestamp). Summary fields are exactly `schema`, `state`, `observed_at`,
`diagnostic_count`, and `truncated`. No params, execution context, org/tenant/user
identity, worker hostname, environment, source revision, results, errors,
headers, connection information, or free-form exception text are included.
Job/tool/age diagnostics belong in internal operator logs.

| State | Exit | Meaning |
| --- | --- | --- |
| `disabled` | 0 | Worker-required gate is off; no database access |
| `clear` | 0 | Complete bounded scan found no qualifying uncovered jobs |
| `uncovered` | 1 | Complete scan reported one or more qualifying jobs |
| `unavailable` | 2 | Configuration, database, decoding, or validation failed |
| `truncated` | 3 | More qualifying jobs exist than the requested limit |

`disabled` and `unavailable` have `observed_at=null`, `diagnostic_count=0`, and
`truncated=false`. Successful scan summaries use the database timestamp.
`diagnostic_count` counts emitted rows; it is neither the queued population nor,
on truncation, the total uncovered population. Exit 3 must never be interpreted
as clear. Invalid invocation also exits 2 but prints usage instead of a summary.

## Repeated scans, activation, and rollback

Exactly one diagnostic means once per distinct job within a successful scan.
A later explicit invocation can report the same still-affected job with a new
age and observation time. There is no durable deduplication, file marker, cache,
job annotation, or once-per-lifetime guarantee. Future scheduling or alert
suppression requires separate admission. Worker recovery or a job transition
may overtake the snapshot immediately and does not change its receipt.

The monitor never updates jobs or workers, changes leases/attempts, retries,
cancels, records a heartbeat, or changes terminal state. A diagnostic is never
authority to cancel or resubmit a job.

This implementation does not activate a monitor. Before activation the owner
must rehearse the query against an isolated PostgreSQL fixture, confirm
`transaction_read_only`, no writes, PostgreSQL syntax, and representative
planner/index behavior. Offline relational tests execute the selection in
SQLite and record the psycopg transaction boundary; they do not replace this
rehearsal. Any timeout on representative data is an activation blocker.

Only a separately authorized existing staging runtime and access may then be
used for one scan, retaining sanitized stdout and exit code with confirmed
database binding and observation time. Do not kill a worker, submit a live test
job, alter jobs, provision services/access, or create a scheduler as part of
demonstrating the condition. Deployment, production scans, scheduling, and
notification integration require their own authorization.

Rollback is to stop invoking the CLI and, if needed, revert these implementation
files through the reviewed change process. No database rollback, job repair,
or worker restart is required.
