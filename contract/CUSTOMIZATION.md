# Leaf customization contract

Status: **FROZEN v1, 2026-07-23**

Contract identifier: `leaf.customization.v1`

This contract defines how tenant customization bytes move from an agent-authored
proposal to the effective tenant catalog. It also defines how a platform release
becomes effective for a tenant. The model may propose bytes, but only trusted
Leaf services may stage, approve, publish, reconcile, or roll back them.

The machine-readable schema is `contract/customization.v1.schema.json`.

## 1. Authorities

| State | Authority |
| --- | --- |
| Tenant customization bytes | Tenant Git repository |
| Platform source and release bytes | Platform Git repository and immutable image digests |
| Frozen and slushy path rules | Platform release policy bundle |
| Desired platform release | Tenant Git workspace reference |
| Effective platform release | Customization coordination store |
| Effective tenant catalog | Customization coordination store, pinned to a tenant Git commit and catalog digest |
| Change-set state, approval, audit, and rollback | Customization coordination store |
| Tenant identity and role | Existing verified identity binding |
| Builder access | Existing entitlement capability |
| Production promotion | Existing protected GitHub workflow and staff operator approval |

Runtime code MUST load the effective catalog and effective platform release from
the coordination store. A mutable Git branch head is never runtime authority.

## 2. Mutability

The platform release policy classifies normalized paths as:

- `frozen`: no tenant or tenant agent may change the path.
- `slushy`: an entitled builder may propose a change, subject to policy.
- `tenant_owned`: the tenant may propose a change, subject to policy.

A tenant workspace reference may select a declared `workspace_contract` and a
desired `platform_release`. It MUST NOT contain path rules or other fields that
widen platform policy.

The trusted server MUST normalize and validate every staged path. It MUST reject
missing or unknown contract versions, contract digest mismatch, duplicate or
ambiguous rules, tenant-supplied policy keys, traversal, symlink escape, and
case, Unicode, or separator aliases.

## 3. Change-set states

The only non-terminal transition path is:

`created -> staging -> staged -> awaiting_approval -> approved -> publishing -> published`

Terminal and recovery states are:

- `rejected`
- `conflicted`
- `failed`
- `superseded`
- `rolled_back`

Every transition uses an idempotency key and an expected current version. A
transition that skips a predecessor or races another writer fails without
changing effective state.

## 4. Git change refs

Each change set owns one isolated Git ref:

`refs/leaf/changes/{change_set_id}`

The staged receipt binds:

- tenant ID
- change-set ID
- expected base commit
- staged commit
- catalog digest
- platform release
- workspace-contract digest

Git ref updates use compare-and-swap with the expected old commit. Force push is
forbidden. A staged ref is not effective until a trusted publish transaction
selects its exact staged commit and catalog digest.

## 5. Git and coordination-store recovery

Git and the coordination store are separate transactional domains. The required
ordering is:

1. Reserve the change-set row with an idempotency key and expected base commit.
2. Create the isolated Git commit and update its change ref with compare-and-swap.
3. Record the staged commit and immutable digests in one coordination-store transaction.
4. Recover interrupted rows by comparing the recorded expected ref with Git.
5. At publish, re-check approval, current base, compatibility, and all digests.
6. Flip the effective catalog pointer and append its audit event in one transaction.

A Git update never makes bytes effective by itself.

## 6. HTTP surface

### R5 stage

`POST /api/author/stage`

Request:

```json
{
  "description": "Create a deterministic panel grouping tool",
  "mode": "build",
  "idempotency_key": "client-generated-stable-key"
}
```

The response carries a `leaf.customization.v1` staged receipt, tool preview,
server validation, and server-generated diff summary. Staging MUST NOT change
the effective catalog or tenant main ref.

### R6 publish

`POST /api/author/register`

This is the only R6 publish route. `POST /api/tools/register` is not part of
this contract.

Request:

```json
{
  "change_set_id": "uuid",
  "staged_commit": "40-hex-git-sha",
  "catalog_digest": "64-hex-sha256",
  "platform_release": "immutable-release-id",
  "workspace_contract_digest": "64-hex-sha256",
  "confirmation_id": "args-bound-approval-id",
  "idempotency_key": "client-generated-stable-key"
}
```

R6 is always-confirm and is never softened by tier or tenant policy. Approval
binds the exact request fields above plus author and approver stable subjects.
High-impact author and approver subjects must differ.

### Rollback

`POST /api/author/rollback`

Rollback selects a previously published catalog commit and digest. It is an
audited change-set transition and is idempotent.

## 7. Legacy authoring

In live-auth mode, `POST /api/author` MUST NOT publish or persist bytes. It may
delegate to the R5 stage operation or fail closed while the staged path is
disabled. A harness failure MUST NOT fall back to local persisted authoring.

The compatibility route MUST preserve the approved `mode` through dispatch and
request identity. The protected R5 stage supports `build`; a `one_off` request
that is not supported by that path MUST fail explicitly and MUST NOT be silently
converted to `build`. A disabled or unavailable customization path MUST return
its stable `reason_code` with a safe, actionable message. It MUST NOT describe
an environment or rollout refusal as a rejection of the user's tool request.

Auth-off demo mode may retain the legacy direct authoring path for compatibility,
but its response MUST identify the legacy mode and it is never production
evidence.

## 8. Feature flags

- `LEAF_CUSTOMIZATION_R5_MODE`: `off`, `internal`, or `all`
- `LEAF_CUSTOMIZATION_R6_MODE`: `off`, `internal`, or `all`
- `LEAF_CUSTOMIZATION_INTERNAL_TENANTS`: comma-separated tenant IDs

Unknown values fail closed as `off`. R6 cannot be enabled for a tenant unless
R5 is enabled for the same tenant. R7 remains disabled and has no dispatch route.

## 9. Audit receipt

Every state transition appends a `leaf.customization.audit.v1` record containing:

- event ID and timestamp
- tenant ID and change-set ID
- prior and next state
- author and approver stable subjects where applicable
- base and staged commits
- catalog, platform-release, and workspace-contract digests
- idempotency key
- result and reason code

Free-text prompts and secret values are never recorded.

## 10. Production promotion and rollback

A platform deployment binds its immutable application image digests to an
effective-catalog snapshot and audit ID. The production operator must differ
from the initiating platform author.

Rollback restores and verifies:

1. the prior digest-pinned task definition;
2. the prior effective catalog commit and digest;
3. the prior effective platform release;
4. one durable, idempotent rollback audit event.

Restoring containers without catalog state is an incomplete rollback.
