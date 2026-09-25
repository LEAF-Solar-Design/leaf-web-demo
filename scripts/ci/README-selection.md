# Web gate selection adapter

This adapter is S3 of the CI test-selection program for Leaf's platform factory.
It adds conservative selection inputs and first-attempt evidence to the existing
CodeBuild gate. It does not activate selection.

## Trusted inputs and execution

Before installing candidate dependencies, `.codebuild/ci.sh` freezes the commits
at `refs/remotes/origin/main` and `HEAD`. A supplied `LEAF_LOADER_TRUSTED_SHA`
must match. The script extracts the selector, map, runner and capture helpers
from that commit with replacement objects disabled. It uses a private temporary
directory and read-only helper files. It never fetches or changes Git credential
configuration. Candidate selector and map files are never imported.

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
by suite. Failures from an internal retry are retained. Collection errors,
crashes, missing reports and scripts without actual test IDs are incomplete;
they never become synthetic test failures for containment.

Each child receives `LEAF_READSET_DIR`, `LEAF_READSET_SUITE`,
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
