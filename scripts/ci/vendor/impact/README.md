# Change-impact manifest package

This is the local change-impact checker. It authors and validates
ASPECTS.yaml, measures tracked-file coverage at a git revision, loads the five
rule families, builds the host's repository index, and records planned and actual
change obligations. Python 3.11+ and PyYAML are the only runtime requirements.

Run from the repository root:

```text
python scripts/impact/impact.py init --repo PATH [--rev REF] [--out FILE] [--force]
python scripts/impact/impact.py validate --manifest FILE [--repo PATH] [--rev REF] [--json]
python scripts/impact/impact.py sync [--json]
python scripts/impact/impact.py doctor [--json]
python scripts/impact/impact.py where --repo OWNER/NAME
python scripts/impact/impact.py version
python scripts/impact/impact.py plan --workdir PATH --change-id ID --owned FILE --record RECORD [--target ID] [--json]
python scripts/impact/impact.py check --workdir PATH --change-id ID --base REF --head REF --record RECORD --receipt RECEIPT [--strict] [--json]
python scripts/impact/impact.py dismiss --record RECORD --row ROW_ID --reason TEXT
python scripts/impact/impact.py resolve --record RECORD --row ROW_ID --evidence REF [--outcome changed|unchanged-compatible]
python scripts/impact/impact.py defer --record RECORD --row ROW_ID --task TASK_ID
```

`init` groups tracked files by top-level directory and lists root files under
`unmapped`. It refuses an existing output unless `--force` is supplied. A tree
with no directories gets a single `root` component. Names that cannot fit the
manifest's glob grammar or size limits cause refusal before writing.

`validate` rejects unknown fields, invalid types, unsupported vocabulary, and
out-of-range values. Only schema_version, project_id, repository,
authored_at_revision, and components are required. Missing list fields become
empty lists. Missing concern fields get empty evidence, false monitor obligation,
and unresolved outcome. These defaults affect the SHA-256 digest, but do not
rewrite the file. The draft-07 schema documents the contract; validation is
hand-written and does not depend on jsonschema.

Globs use `/`, `*`, `?`, and whole-segment `**`. They are anchored: `a/**` matches
descendants, and `**/x` also matches root `x`. Absolute paths, `..` segments,
backslashes, braces, and characters outside the documented grammar are refused.
Component matches take precedence over `unmapped`. Coverage examines the selected
git tree, not untracked or uncommitted files, and prints up to 200 uncovered paths.
Cross-repository DRIFT is informational, including unknown index or head state.

`IMPACT_HOME` defaults to `~/.claude`. Discovery reads `pair-runs/*/status.json`,
the optional `ASPECTS.yaml` repository map, and `ci-registry.json` there. Device
names come from the sibling `<home>-dispatch/devices.yaml`. `sync` writes
`state/impact-index.json` atomically. Repository-map discoveries win conflicts;
local repositories use `local:<project_id>`. `doctor` reports manifests missing
from the index and gives informational notices for registry repos without one.

All text is UTF-8; writes use LF. Input files and the index are bounded to 1 MiB,
tree listings to 200,000 paths, and discovery to 5,000 status files. Git uses list
argv with a 60-second timeout and no shell. Host verbs report one-line errors.
Exit codes: **0** success, **1** invalid/refused or doctor needs sync, **2** usage,
**3** clean absence, **4** unreadable/internal error.

Library surface: `manifest.load_manifest`, `validate_manifest`, `manifest_digest`,
`glob_to_regex`, `match_path`, `coverage` (a `Coverage` dataclass), and
`contract_counts`; `rules.load_families` returns `Family` dataclasses;
`index.lookup(repository, home=None)` returns an entry or `None` and raises on
unreadable state. `gitio` exposes bounded revision, tree, root, remote, and default
head reads. Module docstrings describe these interfaces for the next slice.

## Plan and check

`plan` accepts repeated `--owned` and `--target` arguments. It writes a record
and prints a `## Impact` markdown block for the pair driver, or the record as
JSON. All twelve concerns appear. Required rows come from touched contracts,
component facets, targets, monitor obligations, and companion suggestions.
Companions outside the owned paths remain visible as obligations. External
`EXTERNAL: ` and `repo:` companion references are not local-file suggestions.
Monitor obligations without an evidence reference begin unresolved.

Both verbs call `scope.impact_scope(paths, manifest, families)`. It returns a
`Scope` containing matched entities and deduplicated `RequiredRow` values.
The predicate supports producer, consumer, and any-side contract triggers.
The shipped slice 1 family file still uses producer-side triggers. Explicit
targets add the same target-family obligations used for matched paths.
`impact.impact_scope` also exposes the predicate for callers using the package
directory on `sys.path`.

`check` resolves immutable base and head revisions and assesses the actual git
diff. `--head worktree` adds tracked edits and untracked files and reads local
HEAD contract endpoints from the working tree. Rename pairs count as deletion
plus addition. Deleted paths also use the base manifest, so removing a mapping
cannot remove the old obligation. Check can create a record without a prior
plan. Existing row assessments remain; rows no longer required become
informational. A new manifest digest creates new row IDs and reopens assessment.

For a runtime transaction with no source diff, add
`--transaction deploy|config-write|restart --target ID`. Base and head may be
equal. The target must exist in ASPECTS.yaml. The receipt records the kind and
target, and target families still fire even when changed_paths is empty.

Changed and unchanged-compatible outcomes require an evidence reference.
Deferred outcomes require a task ID. Not-applicable outcomes require a reason.
Contract-family rows also compare the declared producer and consumer surfaces.
`next-app-routes` extracts Next app API routes; `ts-fetch-paths` extracts literal
fetch paths and nearby methods. Parameter names normalize to `:param` and
trailing slashes are ignored. Unknown methods are wildcards. Where both
extractors expose fields, every consumer field must exist on the producer.
Missing revisions, local index entries, or extractor IDs leave rows unresolved.
An explicit changed assessment retains its authored evidence and appends the
contract result without growing evidence on repeated runs. Valid dismissals
and deferrals stand as recorded dispositions.

Receipts include identities, resolved revisions, both manifest digests, the
checker version and host, changed paths, touched entities, rows, summary, and
transaction. They have sorted keys, one-space indentation, LF endings, and no
timestamps. `<receipt>.meta.json` holds generated_at and the session ID. The
record, receipt, and metadata each use atomic replacement. Carried rows never
contribute to this change's summary. `--strict` returns 1 for INCOMPLETE;
ordinary check returns 0 after an executed assessment. Internal failures
return 4 with a one-line `impact:` diagnostic.

Plan and check skip without writing when `CHANGE_IMPACT_DISABLE=1` or
`C:/tmp/gates/CHANGE_IMPACT_OFF` exists. `CHANGE_IMPACT_GATE_FILE` overrides that
path. A missing or invalid workdir ASPECTS.yaml also prints a skip and returns
0. Use `validate` to enforce manifest authoring errors. Malformed records and
rule inputs are errors, not silent skips.

## Record contract

`schema/change.schema.json` documents the draft-07 record shape. `record.py`
validates it without a schema dependency and rejects unknown keys at every
level. Schema version is 1. The record holds change_id, repository, project_id,
manifest_digest, checker_version, base, head, touched, rows, dispositions, and
carried. Base is a SHA or null; head is a SHA, worktree, or null. Paths use
relative forward-slash spelling. Changes allow 5,000 paths and 500 rows.

A row ID is `<concern>:<subject>@<manifest digest first 12 hex>`. Subjects use
`[A-Za-z0-9._/@+:-]{1,200}`. Each row records its family, required flag, outcome,
evidence, reason, task, and superseded_by. Evidence is at most 512 characters;
reasons are at most 1,000. Task IDs use `[A-Za-z0-9._#/-]{1,128}`. Contract
subjects name the contract and consumer repository. Monitor rows use `monitor`;
untriggered concerns use `-` and are not required.

Disposition commands require an existing row and append the action, session,
UTC time, and detail. Missing row IDs return 1; missing CLI arguments return 2.
After a disposition, unresolved rows with the same ID in other records for
this repository receive the resolving change_id in superseded_by. The record
grammar accepts this change-ID form and legacy row-ID links. A changed digest
does not supersede an older digest's rows. Plan scans up to 5,000 records in
`$IMPACT_HOME/pair-runs/*/impact/record.yaml` and displays at most 200 unresolved,
unsuperseded rows from other changes. Unreadable prior records are skipped.

Record reads and writes are bounded to 8 MiB. Each contract endpoint is bounded
to 500 files and 2 MiB of source, using declared globs at the selected revision.
Other repositories are read only through the local repository index; no fetch
or network operation runs. All subprocesses use list argv and 60-second timeouts.

The planner's hermetic acceptance command is:

```bat
cmd /c "set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 && python -m pytest scripts/impact/tests -q"
```
