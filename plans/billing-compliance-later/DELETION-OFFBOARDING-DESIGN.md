# Deletion & Offboarding — Day-One Design (must be built in from the start)

> **Mission pointer.** Canon: `~/.claude/MISSION.md` (absolute: `C:/Users/ehaug/.claude/MISSION.md`).
> Leaf is a WEB platform where each user gets a hosted Claude-Code-style agent harness (web auth =
> the user's own Claude subscription via the Agent SDK credit program; enterprise = API key), a
> per-tenant "mushy codebase" git repo of deterministic tool files, an in-browser three.js CAD render,
> and CAD engine ops on Autodesk APS Design Automation. Registered tools run with ZERO LLM. This design
> is a DO-NOW requirement: deletion-on-request cannot be retrofitted, so the tenant storage and the
> Project/Job schema must carry it from day one.

## Why this is a DO-NOW design (not deferred like billing/compliance)
Billing (Stripe) and compliance (SOC2) are correctly deferred until their triggers fire
(see `BILLING-COMPLIANCE-LATER.md`, Doc 1). Deletion/offboarding is **not** deferrable: it is a
prerequisite control for the compliance build (deletion-on-request is a SOC2/GDPR line item), and if
the schema and stores do not carry the deletion columns and the purge cascade from the start, then when
an enterprise buyer demands "delete my data," there is retained tenant data across five independent
stores with no sanctioned, audited way to remove it. MATRIX line 66 names the exact tension this design
resolves: "B2B PE-stamp buyers will demand ... deletion-on-request (which conflicts with the fleet's
'never hard-delete' rule)."

## Grounding artifacts (read; cited)
- `C:/tmp/mushy-platform/MATRIX.md` line 66 — the compliance/offboarding risk and the never-hard-delete
  conflict this design carves the exception out of.
- `C:/Users/ehaug/claudewalk-build/cadwalk-studio/src/lib/tenancy/postgres.ts:80-92` — the `deployments`
  table (the Postgres tenant row). `postgres.ts:104-126` — the upsert that round-trips it.
- `C:/Users/ehaug/claudewalk-build/cadwalk-studio/src/lib/tenancy/vault.ts:5-9` — the `SecretsVault`
  interface with `deleteSecret(key)`; `vault.ts:48-55` — `KeytarVault.deleteSecret`; `vault.ts:204-208`
  — `assertAwsSecretRef` proving the AWS secret ref shape `cadwalk/<deployment_id>/<key_name>`.
- `C:/Users/ehaug/claudewalk-build/cadwalk-studio/src/app/api/ops/credentials/route.ts:191-193` — the
  single-user credentials file `~/.cadwalk/credentials.json`; `route.ts:10-16` — `CREDENTIAL_KEYS`
  (`anthropic_api_key`, `openai_api_key`, `github_token`, `cloudflare_api_token`, `vercel_token`). It is
  **not** per-tenant and has **no** tenant-delete path.
- `C:/tmp/mushy-platform/plans/project-job-schema.md` — the sibling building the canonical Project/Job
  entity (`platform/migrations/0001_project_job.sql`), the schema this design places a binding column
  requirement on.
- `C:/tmp/leaf-web-demo/server/app.py` — the demo backend; the origin of the (future, per-tenant)
  authored-tools/mushy store (`AUTHORED_STORE = server/authored_tools.json`, `app.py:40`).

---

## 1. Every store that holds tenant data (enumerated) + its purge action
A tenant's data is spread across **five independent stores**. The Postgres `ON DELETE CASCADE` reaches
only the DB rows; the other four hold data **out of band** and each needs an explicit purge action.
A purge that walks only the database silently leaves tenant IP behind — this enumeration is the point.

| # | Store | Ref/shape | What it holds | Purge action |
|---|---|---|---|---|
| 1 | **Postgres `deployments` row + future `Project`/`Job` rows** | `deployment_id` (`.../tenancy/postgres.ts:80-92`); `projects`/`jobs`/`drawing_versions`/`built_tools` keyed by `org_id`/`project_id` (schema sibling, `project-job-schema.md`) | tenant config, projects, drawing versions, job history, built-tool registry | `DELETE` the tenant rows; the schema's `orgs ON DELETE CASCADE` (`project-job-schema.md:58`) wipes projects → versions/jobs/built_tools in one statement. Write a tombstone row (§2). |
| 2 | **Vault secret refs** | `cadwalk/<deployment_id>/<key_name>` (`.../tenancy/vault.ts:204-208`) | per-tenant LLM/API keys, gateway tokens, telegram bot tokens | Enumerate every ref under `cadwalk/<deployment_id>/*` and call **`SecretVault.deleteSecret(ref)`** once per ref (`.../tenancy/vault.ts:8`, impl `:48-55`). Injected as a hook; not implemented here. |
| 3 | **Per-tenant APS OSS bucket + objects** | APS OSS bucket/object keys (the `oss_object` refs on `drawing_versions`, `project-job-schema.md:72`) | uploaded DWGs, WorkItem outputs, versioned drawing blobs | APS OSS object-delete for every object, then bucket delete for the tenant bucket. These bytes live out of band; the DB cascade cannot reach them — a dedicated `blob_purge_hook(ref)`. |
| 4 | **Per-tenant "mushy" git repo** | the tenant's mushy-codebase repo (deterministic tool files the agent edits; `source_ref` on `built_tools`, `project-job-schema.md:108`) | agent-authored/edited deterministic tool source | Repo deletion (hard purge) or archival (soft delete): remove the tenant's git repo / worktree, or move it to a tombstoned archive location. |
| 5 | **Credentials file / its per-tenant successor** | `~/.cadwalk/credentials.json` (`.../ops/credentials/route.ts:191-193`), keys in `CREDENTIAL_KEYS` (`route.ts:10-16`) | anthropic/openai/github/cloudflare/vercel credentials | **Gap flagged:** this file is single-user and has **no** tenant-delete path. A per-tenant successor (or migration of these keys into store #2, the vault, under `cadwalk/<deployment_id>/*`) is required so purge can remove them. Until then, credentials are a store the purge cannot fully reach — call this out as a build obligation. |

## 2. Default = soft-delete + tombstone (honors the fleet "never hard-delete" rule)
For **routine** deletes (a user deletes a project, archives a drawing, or a tenant is deactivated
without a data-erasure demand), the default is **soft-delete + tombstone**, which honors the fleet-wide
"never hard-delete" default (global `CLAUDE.md` / memory):
- A **`deleted_at` marker** is set on the entity. The tenant/project/drawing disappears from every
  list, read, and API surface (every store read filters `WHERE deleted_at IS NULL`), but **the
  underlying data is retained and recoverable**.
- A **tombstone row** remains as an audit anchor (e.g. `orgs.status='deleted'` /
  `offboarded_at`/`deleted_at` set) — enough to prove the entity existed and was deactivated, without
  keeping it live.
- **Nothing is irreversibly removed on the soft path.** Recovery = clear `deleted_at`. The out-of-band
  stores (vault, OSS, git repo, credentials) are **left intact** on a soft delete; only the hard-PURGE
  path (§3) touches them.

## 3. Sanctioned exception = hard PURGE on request (the ONE override of never-hard-delete)
Deletion-on-request (GDPR "right to erasure" / enterprise contractual data-deletion) is the **single,
explicit, sanctioned exception** to the fleet's never-hard-delete rule. Stated in plain words:

> **This is the ONE place the fleet's "never hard-delete" default is deliberately overridden.** The
> hard-PURGE path irreversibly removes tenant data across every store in §1. It is not a general-purpose
> delete and it is not silent — it is gated, audited, and scoped to deletion-on-request only.

The hard-PURGE path:
1. **Gated** — invoked only through an admin/operator-gated action (e.g. `DELETE /api/orgs/{org_id}` in
   the schema sibling, `project-job-schema.md:133`), never a routine user delete, never a client can
   trigger it directly.
2. **Records intent, then completes** — set `purge_requested_at` when the erasure request is accepted;
   set `purge_completed_at` when the cascade below has finished. The gap between them is the auditable
   in-progress window.
3. **Walks every store in §1** — Postgres rows (via `ON DELETE CASCADE`) **and** the four out-of-band
   stores: `key_purge_hook(ref)` once per vault ref (store #2), `blob_purge_hook(ref)` once per APS OSS
   object/bucket (store #3), git-repo deletion (store #4), credentials removal (store #5). Each hook
   fires exactly once per ref (mirrors the schema sibling's `offboard_org` contract,
   `project-job-schema.md:136-140`).
4. **Audited** — **every hard PURGE emits an audit-log line** (who requested, which `org_id`/
   `deployment_id`, timestamps `purge_requested_at`/`purge_completed_at`, per-store purge results).
   This ties directly to Doc 1's compliance audit-log pipeline: the purge audit line is both the GDPR
   erasure record and a SOC2 evidence artifact.
5. **Leaves a minimal tombstone** — `orgs.status='deleted'` + timestamps remain (an audit line that
   preserves *that* a tenant existed and was erased, without retaining the tenant's IP/content).

## 4. BINDING requirement to the `project-job-schema` sibling (day-one column contract)
So the design is built in from day one and both sessions converge on identical column names, the
**Project/Job entity MUST carry these deletion columns**:

| Column | Type | Meaning |
|---|---|---|
| `deleted_at`         | `TIMESTAMPTZ NULL` | soft-delete marker (§2); `NULL` = live, non-null = soft-deleted/hidden but retained |
| `purge_requested_at` | `TIMESTAMPTZ NULL` | hard-PURGE requested/accepted (§3 step 2); opens the auditable erasure window |
| `purge_completed_at` | `TIMESTAMPTZ NULL` | hard-PURGE cascade finished across all §1 stores (§3 step 2) |

This is a **contract**, not a suggestion — both this session and the schema sibling must use these exact
three column names on the Project/Job (and ideally the `orgs`/tenant) rows.

### Integration obligation on the sibling's current schema (stated fact, 2026-07-17)
The `project-job-schema` sibling is **concurrently** building `platform/migrations/0001_project_job.sql`
per its brief at `C:/tmp/mushy-platform/plans/project-job-schema.md`. Its **current v1 DDL does NOT yet
carry `deleted_at` / `purge_requested_at` / `purge_completed_at`.** What it has today
(`project-job-schema.md:44-118`): `orgs.status` (`active|offboarding|deleted`) + `orgs.offboarded_at`
(`:49-52`), and a `projects.status` enum that includes the value `'deleted'` (`:60`). A `status` string
of `'deleted'` and an `offboarded_at` on the org are **not** the same as row-level
`deleted_at`/`purge_requested_at`/`purge_completed_at` on the Project/Job — the status enum cannot
distinguish "soft-deleted/recoverable" from "hard-purged/erased," and it carries no request-vs-completed
timestamps for the audit window.

**Integration obligation (binding):** the three columns above must be added to
`platform/migrations/0001_project_job.sql` (on `projects` and `jobs`, and ideally mirrored onto `orgs`)
before the deletion/offboarding design can be honored end to end. Because that migration uses
`CREATE TABLE IF NOT EXISTS` and the tables are additive, this is a small ALTER/column-addition, not a
redesign. This session **emits the requirement only**; it does not edit the sibling's schema (that file
is owned by `project-job-schema`, and is out of scope here per this work item's boundaries).

---

## Scope boundary
This document is a design + a binding requirement. It adds **no** code, edits **no** existing file, and
does **not** modify the Project/Job schema itself (owned by `project-job-schema`). The only hard-delete
path anywhere in the design is the gated, audited `offboard`/hard-PURGE on request — no general-purpose
delete is introduced. Rollback of this work item is trivial: delete the
`C:/tmp/leaf-web-demo/plans/billing-compliance-later/` directory; nothing else is touched.
