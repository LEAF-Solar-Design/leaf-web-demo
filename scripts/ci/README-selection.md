# Web gate selection adapter

This adapter is S3 of the CI test-selection program for Leaf's platform factory.
It adds conservative selection inputs and first-attempt evidence to the existing
CodeBuild gate. It does not activate selection.

## Trusted inputs and execution

Before installing candidate dependencies, `.codebuild/ci.sh` freezes the commits
at `refs/remotes/origin/main` and `HEAD`. A supplied `LEAF_LOADER_TRUSTED_SHA`
must match unless the proof override below is active. The script extracts the selector, map, runner and capture helpers
from that commit with replacement objects disabled. It uses a private temporary
directory and read-only helper files. It never fetches or changes Git credential
configuration. Candidate selector and map files are imported only with that proof override.

The extracted runner's `--list --catalog-root DIR` interface emits
`leaf.ci.catalog.v1`. It lists every suite, normalized command and working
directory, floors, skip rules and collection state. `catalog_sha256` is the
existing runner catalog fingerprint. Listing does not collect tests, probe a
database, reset authored tools, create logs or spawn processes. Unknown test
collections have a null identity and an empty ID list, not invented IDs.

The accepted S1 core has a different catalog interface:
`leaf.ci.test-catalog.v1`, with a digest over its entire document. The trusted
inline adapter retains the original listing as `runner-catalog.json`, then
writes the S1 projection to `catalog.json`. That projection carries
`runner_catalog_sha256` and the trusted runner blob identity separately. These
two digests must not be compared as if they described the same bytes. The
initial map deliberately has no admitted catalog or collection digest.

CODEBUILD metadata supplies the event input. A provider-resolved candidate SHA
must agree with the frozen head. The adapter normalizes CodeBuild's
`GitHub-Hookshot` spelling to S1's `GitHub-Hook` spelling. Webhook presence does
not establish queue protection, a webhook filter, full-status enforcement or
revocation state. This slice does not invent that missing producer evidence.
Until the enforcement producer and an admitted map supply the required
identities, S1 returns a full decision. Listing declarations alone cannot
establish collection stability or authorize activation.

Runner blob changes force full execution with `runner_changed`. Extraction,
listing, selector or argument-validation failures also choose full before
execution. Filtering uses only the trusted emitter's NUL-delimited argument
file and `mapfile`; no selector output is evaluated as shell code. The array
is populated only for a validated decision with `apply_filter=true`.

The initial policy has `selection_enabled=false` and `phase=shadow`. Every
current declaration is mandatory because no traced classification has been
admitted. This includes every exact ID and expanded prefix in L4's mandatory
table, every contract suite, and all remaining unclassified suites.
`unresolved_mandatory` is empty because all named catalog entries resolved.
Workflow contracts, web build and license checks remain unconditional outside
the runner. Existing database conditions, opt-ins and skip rules still apply.
Non-Python suites stay unmappable and always selected.

## Environment contract

A status-off proof may load the candidate's own `scripts/ci` by passing
`--env LEAF_PROOF_TRUSTED_SHA=<sha>` to `proof_build.py`. The value must be
exactly 40 hexadecimal characters and equal `HEAD_SHA` as a string, and all
three variables `CODEBUILD_WEBHOOK_EVENT`, `CODEBUILD_WEBHOOK_HEAD_REF` and
`CODEBUILD_WEBHOOK_TRIGGER` must be unset. Webhook builds ignore the override
so a candidate cannot change the selector that judges it. Any supplied but
ineligible value produces one stderr `WARNING: LEAF_PROOF_TRUSTED_SHA ignored
(<reason>)` line and leaves the normal trusted load and loader check in place.
An active override sets `TRUSTED_SHA` to `HEAD_SHA`, skips the loader check with
`loader_check="override"`, and records `trusted_sha_override=true` in the mode
receipt, detail, `LEAF_SELECTION`, `LEAF_SELECTION_FINAL` and `LEAF_SHADOW`.
Otherwise the flag is false. Override receipts prove candidate helper behavior;
they are never trusted selection evidence and must be excluded from shadow and
mode cohorts by the collector.

Test-ID reporters stay active on all build classes when their files and
`pytest_selection.py` load from `TRUSTED_SHA`, through `LEAF_TRUSTED_CI_DIR`.
Read-set tracing requires the trusted capture helpers and the explicit
`LEAF_PROOF_TRACING=1` override. PR, merge-group and main-push builds keep
reporters but skip tracing; a status-off proof without the override does the
same. Every build exports `PYTHONPATH` to the trusted helper directory so
pytest can load `pytest_selection`. Only tracing builds export `LEAF_READSET_*`;
other builds unset them. The plugin enables capture only when `LEAF_READSET_DIR`
is set. This keeps webhook verdicts fast and avoids tracing-induced test
failures on shared CI while dedicated proofs collect read sets for the map
builder. The detail receipt and `LEAF_SHADOW` record `tracing_active` and
`reporters_active` separately.

The runner adds `LEAF_READSET_DIR` and `LEAF_READSET_ROOT` to suite environments only when the parent carries `LEAF_READSET_DIR`; otherwise it supplies only suite, attempt, run and test-report directory metadata so reporting cannot re-enable tracing through defaults.

After the gate, tracing builds first compute completeness from attempt records
and write the pre-publication detail and `full-run.json`. The trusted
`full_run_manifest.py` runs from the extracted selection directory under
`python -I -B`. It accepts explicit JSON input on stdin, never environment
values. It validates source, tree and capture SHAs, recomputes the packed
catalog fingerprint with `select_tests.catalog_info`, and requires equality
with the catalog's own `catalog_sha256`. Shards now receive that canonical
fingerprint through `LEAF_READSET_CATALOG_SHA256`; the shadow row retains the
separate runner fingerprint.

Step A judges only attempt rows whose run ID matches the build and whose suite
is in `executed_suite_ids`. Other rows increment `attempt_rows_rejected` and
are excluded from the combined attempt stream and completeness calculations.
The detail document, `LEAF_SELECTION_FINAL`, and `LEAF_SHADOW` carry sorted
`completeness_reasons`, naming each failed predicate and its count where
applicable, or an empty list for a complete full run. The archive includes
explicit `reports/<encoded-suite>/<attempt>/collection-*.json` and
`completion-*.json` members for accepted suites, and `readsets_archive_members`
lists `reports`. The manifest's `collection_ids_by_suite` supplies sorted,
non-empty `<suite_id>::<nodeid>` lists from collection documents when the
catalog has no test IDs. Suites without collected IDs are omitted. All
collection documents for a suite must agree; a mismatch omits that suite,
marks completeness false, and adds `collection_ids_differ:<suite>`.
The manifest helper reads this mapping from `--collection <path>` and rejects
malformed, empty, unsorted, duplicate, or incorrectly qualified ID lists.

Before the manifest and archive, Step A partitions read sets using the packed
catalog and `CODEBUILD_BUILD_ID`. Only shards whose suite is in that catalog
and whose run ID matches the build stay in `readsets/`. Other shards and their
attempt streams move to `$selection_dir/readsets-rejected/`, which is never
packed. Shared outcome streams referenced by accepted shards stay with those
shards. The detail document, `LEAF_SELECTION_FINAL`, and `LEAF_SHADOW` report
the count as `readsets_rejected_shards` (zero without tracing). If partitioning
fails, no archive is published. The manifest still rejects accepted shards
with missing or mismatched provenance.

The `leaf.ci.full-run.v1` manifest binds run, source, tree, capture and catalog
through `provider_binding`. It records the execution mode, completeness flags,
catalog suite IDs, observed workers per suite, and `toolchain_fingerprint` over
the Python version, explicit CodeBuild image and capture SHA. Missing workers
are not invented. Invalid manifest inputs write no manifest.

Tracing builds then pack `readsets/`, `catalog.json`, `decision.json`,
`full-run.json` and the attempt streams referenced by the shards, and publish to
`s3://leaf-mq-transport-807034087062-us-east-1/mq/leaf-web-demo/selection/<build-uuid>.readsets.tar.gz`,
where the UUID is the part after the colon in `CODEBUILD_BUILD_ID`. The immutable
put uses `--if-none-match '*'`, `--checksum-algorithm SHA256`, and metadata
`build_id`, `head_sha`, `trusted_sha`, and `trusted_sha_override`. Archives over
200 MiB are not uploaded. The final detail document, `LEAF_SELECTION_FINAL`, and
`LEAF_SHADOW` carry `readsets_object`, `readsets_sha256`, `readsets_bytes`, and
`readsets_status`: `uploaded`, `empty`, `too_large`, `upload_failed`, or
`manifest_failed`. After a successful partition, a manifest failure still packs
and uploads accepted read sets and preserves the gate exit code. Publication fields are merged into
`detail.json` only after the upload attempt, then the final and shadow rows print.
They also carry `readsets_archive_members`, including the manifest and the
archive-relative attempt paths.
Missing `decision.json` is skipped without failing the upload; the partition requires `catalog.json`.
Upload failures produce one warning and preserve the gate result. Non-tracing
builds report `not_traced` and never pack or upload read sets.

`sitecustomize.py` writes the absolute `LEAF_TEST_REPORT_DIR` as `outcomes_ref`
today. Its capture bytes remain unchanged in S14. After the suite exits,
`scripts/run-all-gates.py::record_attempt` combines the actual pytest attempt
streams into `readsets/<encoded-suite>/<attempt>/attempts.jsonl` and replaces
that absolute reference in startup shards with the archive-relative document
path. `pytest_selection.py` also copies each completed per-process stream to
`readsets/<encoded-suite>/<attempt>/attempts-<shard>.jsonl`. Empty or missing
reports do not become complete evidence. The plugin's module-catalog path uses
decision/catalog provenance when present and otherwise takes each binding from
`LEAF_READSET_RUN`, `LEAF_READSET_SOURCE_SHA`, `LEAF_READSET_SOURCE_TREE`,
`LEAF_READSET_CAPTURE_SHA`, and `LEAF_READSET_CATALOG_SHA256`. Its `outcomes_ref`
names the copied stream as `readsets/<encoded-suite>/<attempt>/attempts-<shard>.jsonl`.
`sitecustomize.py` passes the exported provenance to `trace_reads.write_readset`,
which requires all five binding fields.

Interpreter isolation (`-I -B`) is for trusted processes only. The gate run
inherits no interpreter flags and explicitly unsets `PYTHONSAFEPATH`. The runner
owns each suite environment: it removes `PYTHONSAFEPATH` even if the parent sets
it, and keeps `PYTHONPATH` for capture. Report injection failures retain the
original command and environment and mark test-report evidence incomplete.
Vitest reporters run inside the Vite root from a per-attempt trusted copy at `node_modules/.leaf-ci/vitest-leaf.mjs`; copy failure injects nothing and records `reporter_copy_failed:<ExceptionType>`.
An injected attempt that exits nonzero with zero executed tests retries with the original command, un-instrumented, and records incomplete evidence with `reporter_startup_failure`.

## Receipts and result evidence

`LEAF_SELECTION`, `SELECTION` and `SELECTION_ARM` describe the final decision
before candidate code executes. The existing key/value `LEAF_EVENT` grammar and
timing stamps remain; `mode` records actual execution, which is full in shadow.

When `CODEBUILD_BUILD_ID` is present, the script writes immutable objects under
`s3://leaf-mq-transport-807034087062-us-east-1/mq/leaf-web-demo/selection/`:

* `<complete-build-id>.json`: the L2 `leaf.ci-selection-mode.v1` mode receipt.
* `<complete-build-id>.detail.json`: the L4 `leaf.ci.selection.v1` decision.

Both puts use JSON content type and `--if-none-match '*'`. `selected_count`
counts test IDs and is null when they were not calculated. A failed put aborts
selected execution before the runner with `receipt_write_failed`. Full runs
warn and continue. A receipt is never overwritten after execution. The initial
detail explicitly marks completion false; `LEAF_SELECTION_FINAL` supplies the
later evidence and preserves the runner's exit code.

Every scheduler appends a JSON line under `<log-dir>/attempts/` immediately
after each result, before retry replacement or cumulative-time adjustment.
Records retain suite, attempt, status, duration, log reference, exact failed
IDs, collection digest, report references and completeness. Suite path
components are encoded. Attempt evidence errors do not change gate verdicts.
Retries, scheduling, conflict locks, floors, audit classification and the
scoreboard retain their prior behavior.

The trusted pytest plugin runs in report-only mode within each selected suite.
It cannot deselect individual tests. Vitest keeps its default reporter and
adds `vitest-leaf.mjs`. Playwright keeps explicit CLI reporters, or uses a
runtime config wrapper to retain its configured list/HTML reporters and
relative output paths while adding `playwright-leaf.mjs`. IDs are namespaced
by suite. Failures from an internal retry are retained. Collection errors and
incomplete reporter documents never become synthetic test failures for containment.

Completeness follows the suite kind. Pytest requires complete completion and
collection documents and records `test_id_granularity: "test"`. Script, tsc and
npm-audit attempts use `test_id_granularity: "suite"`: PASS or FAIL is complete,
with empty test IDs and report references. Vitest uses test granularity when a
reporter document exists and suite granularity when none exists. An explicit
reporter setup or startup failure still leaves reporting incomplete. A SKIP
caused by the suite's own unmet `db_gated` or `opt_in_env` rule records
`skipped_by_gate`, complete reporting and empty test IDs. The final receipt
counts these as final and lists their IDs in `suites_skipped_by_gate`. Other
SKIPs remain nonfinal. Suite completeness does not imply test-ID or read-set
coverage, and the existing completeness-reason vocabulary is unchanged.

Every `--leaf-*` plugin option is passed as one `--leaf-x=value` token, for
example `--leaf-output=DIR`. An xdist worker rebuilds its config from the raw
argv and pre-parses it before `-p pytest_selection` has registered the
`--leaf-*` options. A path-valued option passed as two tokens therefore has its
value read as a positional path, pytest's rootdir moves to the common ancestor
of those paths, every node id gains a prefix, and `--deselect` silently matches
nothing. Measured 2026-09-25 on terraform tracing proofs, where the quarantine
ran and failed 109 tests.

On tracing builds, each child receives `LEAF_READSET_DIR`, `LEAF_READSET_SUITE`,
`LEAF_READSET_ATTEMPT`, `LEAF_READSET_ROOT` and `LEAF_READSET_RUN`. The trusted
directory is supplied through `PYTHONPATH`. Startup capture writes
per-process shards under `readsets/<encoded-suite>/<attempt>/`. Python `-I`
ignores the startup hook and is explicitly incomplete. Native reads, Node,
shells, browsers and escaped children do not gain Python-only completeness.
The startup hook makes no Python-only admission claim; it captures observations
for later classification. A successful process alone cannot make a read set
mappable. Hooks are observation, not a security sandbox.

In shadow phase, `LEAF_SHADOW` uses first-attempt records even if a retry passes.
It reports integer `selected_ids`, exact `full_failed_ids`, selected identities,
catalog and collection digests, attempt artifact reference and digest,
completion flags, assigned arm and actual execution mode. Containment is the
set comparison of failed IDs against selected IDs. It is never inferred from
suite counts or a green final verdict. Missing, duplicate, stale or incomplete
attempts prevent complete shadow evidence. The concatenated attempt artifact
is `/tmp/gate-results/selection-attempts.jsonl`; a collector must retain and
authenticate that artifact before using its reference as activation evidence.
The mode receipt alone is not proof of a complete shadow run.

## Verification and rollback

The planner owns verification through
`C:/Users/ehaug/.claude/program/ci-test-selection-20260925/specs/verify_s3_web_adapter.py`.
The adapter contracts in `tests/test_run_all_gates_selection.py` cover listing,
both retry schedulers, child attribution, isolated Python, pytest IDs and the
Node reporters, suite environment isolation, timeout output and report-injection
fallback. Use `-I -B` only for trusted helpers; suite tests keep their normal
interpreter flags.

The trusted map already disables selection. To remove filtering entirely,
remove `"${only_args[@]}"` from the single runner invocation. That restores
the exact unfiltered command while retaining timing and attempt evidence.
Do not rerun full to replace a failing selected verdict. Activation, the
provider status-off proof and map admission belong to their separate program
slices; they are not side effects of this adapter.
