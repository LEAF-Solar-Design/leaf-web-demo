# Leaf project repository edit contract

Status: **FROZEN v1, 2026-08-09**

Contract identifier: `leaf.project-repository-edit.v1`

This contract defines the post-launch backend boundary for editing one Leaf
project repository. It is separate from tenant catalog customization and from
global platform self-edit. A model may propose file changes, but only trusted
Leaf services may select a repository, stage a commit, accept a confirmation,
publish a commit, reconcile an interrupted operation, or roll back a change.

The machine-readable schema is
`contract/project-repository-edit.v1.schema.json`.

## 1. Authority tuple

Every operation is bound to this server-derived tuple:

- active actor binding ID;
- canonical tenant ID;
- canonical organization ID;
- owned project ID;
- server-minted repository key.

The trusted app derives the actor, tenant, and organization from the verified
identity binding. It loads the project with both organization ID and project ID
before every read or write. It then loads the repository key from the durable
project-repository mapping. Tenant input may name a project ID, but it never
selects a repository key, Git remote, filesystem root, checkout path, or ref.

The repository key is an opaque UUID. The harness maps it below the configured
project repository root. The resolved root must remain a strict descendant of
that configured root after canonical path resolution. An absolute path is not a
wire field. `repoDir` MUST NOT be accepted as request or model authority.

Unknown and foreign organization, project, repository, edit, confirmation, and
rollback identifiers return the same 404 status and response shape. No route
may disclose which ownership check failed.

## 2. Gates

Before every repository read or mutation, the trusted app must recheck:

1. verified active identity binding;
2. active organization;
3. project ownership by that organization;
4. active owner or editor role;
5. explicit `project_repo_edit` entitlement;
6. repository mapping for that exact organization and project;
7. the expected edit state and version.

Missing or invalid policy fails closed. The browser, model, harness request
body, and tenant repository cannot grant or widen these checks.

The harness accepts only authenticated internal dispatches from the trusted
app. It validates the UUID shapes and uses the app-provided authority tuple only
as identifiers. It never accepts a path. App authorization remains mandatory,
and the harness still enforces repository containment, lease, ref, and Git
compare-and-swap rules.

## 3. Repository and lease

There is one repository mapping per `(organization_id, project_id)`. The
mapping stores an opaque repository key, not an absolute path. A repository is
created only from a server-owned seed that matches the frozen project
repository contract.

Every read lease and writer lease uses the same contention key, the immutable
repository authority `(tenant_id, organization_id, project_id, repo_key)`.
Stage, publish, reconcile, and rollback require a writer lease. The
actor binding is recorded and authorized, but it is not part of the contention
key. Two actors targeting the same repository must contend for the same lease.
Read and writer leases contend on this same key. Different repository keys have
independent leases. Every lease carries a unique generation. A read or writer
must generation-fence every root resolution, Git read, worktree operation,
model edit, validation, commit, ref update, recovery step, and cleanup, and must
stop after lease loss.

The canonical repository is bare. Model work occurs only in a detached,
isolated worktree owned by one private ref:

`refs/leaf/project-edits/{edit_id}`

Neither the model nor a request may choose a ref. No model session receives Git
commands, native filesystem tools, a shell, or the canonical bare repository.

## 4. Path and tree rules

The trusted harness applies all rules before a path is read and again before a
staged commit is accepted:

- paths are non-empty, repository-relative UTF-8 in Unicode NFC;
- `/` is the only separator in receipts;
- absolute paths, drive paths, NUL, empty segments, `.`, and `..` are rejected;
- `.git` is forbidden as any path segment, without case sensitivity;
- case-fold or Unicode aliases within the changed set are rejected;
- every existing path component is checked without following links;
- symbolic links, Windows junctions, and other reparse points are rejected;
- Git submodules and gitlinks, including mode `160000`, are rejected;
- a path whose canonical parent escapes the isolated worktree is rejected;
- rename and copy records are normalized to their old and new paths, and both
  paths must pass every rule;
- changed paths come from the trusted Git diff, never from model output.

JSON Schema enforces the lexical subset only. A mandatory semantic validator
must separately prove that every path is valid UTF-8 and Unicode NFC, that the
array is sorted by UTF-8 bytes, and that no two paths collide after NFC plus
Unicode case folding. It must run before any read, write, commit, receipt digest,
or publication. A trailing slash is invalid because receipts name files and
gitlinks, never directories.

The server-owned policy may further restrict allowed paths and file sizes. A
tenant file cannot contain, replace, or widen that policy.

## 5. Editor boundary

The existing free-form `SdkRepoEditor` MUST NOT be mounted or used for this
capability. It is a single-operator editor whose input accepts an absolute
repository directory and whose tools mutate that checkout directly. That is
not project SaaS authority.

A future project editor must expose a closed repository-relative tool set over
the isolated worktree. The trusted lifecycle, not the model, owns root
selection, changed-path discovery, validation, commit creation, private-ref
updates, receipts, confirmation, publication, audit, recovery, and rollback.

The exact instruction may be supplied to the model. Only its SHA-256 digest may
enter a staged receipt or audit record. Free text and credential values are
never persisted in those records.

## 6. States

The only non-terminal transition path is:

`created -> staging -> staged -> awaiting_confirmation -> publishing -> published`

Terminal and recovery states are:

- `rejected`;
- `conflicted`;
- `failed`;
- `superseded`;
- `rolled_back`.

Every transition uses an idempotency key and expected current version. A skipped
state or competing writer fails without changing effective state.

## 7. Staged receipt

A staged receipt is a strict `leaf.project-repository-edit.v1` object with
exactly these fields:

- `contract`;
- `edit_id`;
- `state`;
- `operation`;
- `source_edit_id`;
- `actor_binding_id`;
- `tenant_id`;
- `organization_id`;
- `project_id`;
- `repo_key`;
- `base_commit`;
- `staged_head_commit`;
- `changed_paths`;
- `diff_digest`;
- `instruction_digest`;
- `idempotency_key`.

`tenant_id` and `organization_id` are independently recorded from the same
verified active binding and must be equal for this platform. An ordinary edit
uses `operation: "edit"` and `source_edit_id: null`. A rollback uses
`operation: "rollback"` and names the published source edit.

`changed_paths` is sorted by UTF-8 byte order and contains no duplicates. The
diff digest is SHA-256 over UTF-8 canonical JSON for a sorted trusted manifest
whose entries contain path, status, old blob, new blob, old mode, and new mode.
Keys use the stated order and the encoding has no insignificant whitespace. The
instruction digest is SHA-256 over the exact UTF-8 instruction bytes.

The staged receipt digest is lowercase SHA-256 over the RFC 8785 JSON
Canonicalization Scheme serialization of the parsed staged receipt. The staged
receipt schema contains only strings, null, and arrays of strings, so no
implementation-specific number serialization is involved. Parsers must reject
duplicate object keys, lone Unicode surrogates, invalid UTF-8, and values outside
the strict schema before canonicalization. JCS preserves Unicode code points and
does not normalize strings. The semantic path validator therefore rejects a
non-NFC path before the digest is computed. The trusted app and harness each
recompute the digest from parsed fields and compare the lowercase digest bytes
in constant time. Neither hashes caller-provided JSON text directly.

The receipt is generated after the private ref advances with expected-old-SHA
compare-and-swap. Staging never changes `refs/heads/main` and never makes bytes
effective.

## 8. Confirmation and publish

Publication requires a fresh confirmation bound to:

- confirmation ID;
- exact staged-receipt digest;
- approver binding ID;
- tenant, organization, project, repository key, and edit ID;
- issued and expiry timestamps.

The confirmation has a bounded TTL and can be consumed once. It is never a
session grant. At publish, the trusted app repeats every gate, verifies the
receipt fields and recomputed receipt digest, consumes the confirmation while
moving the row to `publishing`, and sends the exact receipt plus expected main
SHA to the harness.

The harness rechecks the private ref and requires main to equal the expected old
SHA. The expected main SHA must equal the staged receipt's `base_commit`. It
updates `refs/heads/main` with Git compare-and-swap. Force push, ref
deletion, and an unqualified branch update are forbidden. A replay that observes
main already at the exact staged commit is idempotent. Any other drift is a
conflict and must not be overwritten.

## 9. Git and database recovery

Git and the coordination database are separate transactional domains. Required
ordering is:

1. reserve the database row and authority tuple;
2. reserve the private ref at the expected base with compare-and-swap;
3. edit and validate an isolated worktree;
4. commit and advance the private ref with compare-and-swap;
5. record the exact staged receipt and audit event in one database transaction;
6. issue and consume one exact confirmation;
7. enter `publishing` before the main-ref compare-and-swap;
8. record `published` and its audit event after the exact main commit is known.

Recovery compares the database receipt, private ref, and main ref. It may finish
an idempotent transition only when every bound value matches. An unexplained ref
or digest mismatch becomes `conflicted`. A Git update alone never proves a
published database state, and a database pointer alone never proves Git
publication.

## 10. Rollback

Rollback never rewinds main and never only hides a visual or database result.
It creates a new isolated rollback edit from the exact current main commit. The
trusted harness computes an inverse commit for one exact published source edit,
rejects conflicts, validates the resulting tree, and emits a staged rollback
receipt.

That receipt needs a fresh one-use confirmation. Publication repeats all gates
and updates main with expected-old-SHA compare-and-swap. Success records the new
head and one idempotent rollback audit event, then marks the source edit
`rolled_back`. A rollback of an already rolled-back source is idempotent only
when the same inverse commit is already the exact published head.

## 11. Audit

Every transition appends an immutable audit record with the event ID, timestamp,
authority tuple, edit and source edit IDs, prior and next states, actor and
approver bindings, base and staged commits, changed-path digest, receipt digest,
idempotency key, result, and reason code.

Audit records contain hashes and stable identifiers only. They never contain
instructions, file contents, diffs, credentials, filesystem roots, or grant
values.

## 12. HTTP surface

The project-scoped routes are reserved as follows:

- `POST /api/projects/{project_id}/repository-edits` stages a new edit;
- `GET /api/projects/{project_id}/repository-edits/{edit_id}` returns status;
- `POST /api/projects/{project_id}/repository-edits/{edit_id}/confirm` creates
  one exact, TTL-bound confirmation;
- `POST /api/projects/{project_id}/repository-edits/{edit_id}/publish` consumes
  that confirmation and performs CAS publication;
- `POST /api/projects/{project_id}/repository-edits/{edit_id}/rollback` stages
  a new inverse edit that must be separately confirmed and published.

These routes remain unmounted until the backend, entitlement, lease, recovery,
Sol review, and adversarial tenancy review are complete. This frozen contract
does not authorize a deployment or a live feature flag.
