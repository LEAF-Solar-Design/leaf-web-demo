# Authored CAD production readiness

Status: NOT READY

Updated: 2026-07-26

This plan defines the release contract for tenant-authored CAD tools. The cat
panel transformation is the first demanding example, not a production-only
special case.

## Product contract

An authenticated and entitled tenant user can:

1. Open one of that tenant's drawings.
2. Describe a new deterministic CAD tool in plain language.
3. Use the user's linked Claude grant to author a tenant-scoped tool package.
4. Review the staged tool and approve its publication.
5. Preview a write tool without granting permission for the later write.
6. Approve the exact write against an exact drawing head and catalog generation.
7. Create immutable drawing version `vN+1`.
8. Inspect the result in the browser, including orbit, pan, and zoom for 3D
   geometry.
9. Undo, redo, or select a prior version without deleting history.

The same path must work for a new tenant and a new request. A built-in cat tool,
a preloaded prompt, a fixture-only catalog, or a browser route mock does not
satisfy this contract.

## Security contract

- Auth0 supplies the user and tenant identity. Request bodies cannot select a
  different tenant.
- A tenant can read and mutate only its own repository, catalog, drawing,
  session, approval, audit, and job records.
- Claude grants stay tenant-scoped and never enter application logs, model
  prompts, broker requests, or generated tool sandboxes.
- The model cannot publish a staged tool. A separate exact approval publishes
  the exact staged commit and catalog digest.
- A preview is non-mutating and uses the read approval rung. It cannot mint or
  reuse a write grant.
- A write approval binds the tool, parameters, drawing, drawing head, catalog
  digest, catalog commit, effective catalog digest, and tool manifest digest.
- Generated code executes only inside the approved tool sandbox. It cannot read
  platform secrets, the instance metadata service, another tenant's data, or
  arbitrary network targets.
- Failed, expired, stale, duplicate, and replayed approvals fail closed.

## Durable authority contract

PostgreSQL is the shared authority for:

- application sessions and approvals
- agent policy rate state and audit coordination
- asynchronous jobs and terminal callbacks
- drawing manifests, versions, and checkout leases
- upload attempts and purge leases
- broker tenant state, ledger state, callbacks, and replay nonces
- harness sessions and tenant repository leases
- customization staging, publication, and effective catalog pins

Tenant Git and drawing payload storage remain durable, tenant-scoped stores.
Application containers do not create database schemas during startup.
Migrations run as a separate reviewed release stage.

## Current verified state

Read-only AWS inspection on 2026-07-26 found:

| Area | Staging | Production |
|---|---|---|
| ECS application services | web, app, broker, harness, and canonical worker healthy at 1/1 | combined `leaf-platform` task healthy at 1/1 |
| Source identity | five services use images from several different source commits | app, broker, and harness use `prod-6cf9b69` |
| Authored execution | explicitly off in app, broker, and harness | explicitly off in broker and harness |
| Harness session authority | file | file |
| Harness authoring mode | disabled | disabled |
| Tool sandbox credential | no E2B secret exists | no E2B secret exists |
| Database | Aurora PostgreSQL and TLS-only RDS Proxy are available | Aurora PostgreSQL and TLS-only RDS Proxy are available |
| Database wiring | app, broker, and canonical worker consume the staging URL | live application task does not consume the production URL |
| Browser proof | fixture-backed cat proof and a real-service no-preload proof exist | no authenticated production smoke |

The integrated application branch is
`codex/cat-production-integration-20260726`.

## Blocking design gap

Production startup correctly requires the E2B author provider when authored
execution is on. The current `E2bAgentRunner` does not run the general Claude
author loop. It selects one of three fixed, read-only count, list, or measure
templates. It cannot author a drawing-write tool such as the cat panel
transformation.

Do not enable authored execution in staging or production until this gap is
closed. A green health endpoint with this fixed-template runner would still
fail the product contract.

The accepted author boundary must:

1. Let Claude propose arbitrary source and a manifest through structured,
   tenant-scoped tools.
2. Keep the tenant's Claude credential outside the generated-code sandbox.
3. Validate the manifest and source without executing generated code in the
   harness process.
4. Run generated code, tests, and preview only in the E2B sandbox.
5. Allow only the reviewed broker or health probe host and deny every other
   network target.
6. Return source and validation receipts to the harness.
7. Commit only the exact validated bytes under the tenant writer lease.

## Release gates

### Gate A: application integration

- Merge the recovered cat workflow onto current application `main`.
- Keep the current drawing-binding and exact-catalog approval fixes.
- Pass server, harness, web, and browser suites.
- Add a non-mocked acceptance driver that can target a deployed environment.

### Gate B: general sandboxed authoring

- Replace the fixed-template E2B author step with the accepted general author
  boundary.
- Prove that a novel write request can generate a new tool.
- Prove sandbox denial for metadata, loopback, private ranges, public sites,
  and a second tenant.
- Prove that Claude, E2B, broker, harness, and platform secrets do not cross
  their intended boundaries.

### Gate C: staging configuration

- Store a Leaf-owned E2B credential in Secrets Manager.
- Add an explicit HTTPS broker or health probe URL whose hostname equals the
  sandbox allowlist hostname.
- Confirm migration `0017_harness_sessions.sql` and all earlier migrations.
- Set harness sessions to PostgreSQL.
- Set harness authoring mode to `singleton`.
- Set the author and tool sandbox providers to E2B.
- Enable authored execution together on app, broker, and harness.
- Select PostgreSQL authorities one at a time and verify each before the next.
- Deploy app, broker, harness, web, and canonical worker from one exact
  application commit and record every image digest.

### Gate D: staging acceptance

Run with two real Auth0 users in two tenants:

- link separate Claude grants
- author separate new tools
- deny cross-tenant repository, catalog, drawing, approval, job, and audit reads
- preview a write with no mutation and no write grant
- approve publication independently
- approve one exact write
- create `vN+1`, reload it, orbit the 3D result, undo, and redo
- reject stale-head, stale-catalog, duplicate, expired, and replayed approvals
- restart app, broker, harness, and worker and prove the state survives
- prove one canonical worker owns each delivery lease
- record logs, metrics, audit rows, task definitions, and image digests

### Gate E: production infrastructure

- Wire the production database URL into app, broker, harness, and worker.
- Run the reviewed production database bootstrap and schema migration stages.
- Add the production canonical worker with single-writer and lease guards.
- Add the same E2B, PostgreSQL, sandbox, and authored-execution posture proven
  in staging.
- Keep desired count and deployment percentage compatible with every
  single-writer authority until all remaining state is shared.
- Merge a production deploy workflow whose rollback claims match what it can
  actually attempt.

### Gate F: promotion and cutover

- Promote the exact staging image digests. Do not rebuild for production.
- Retain the prior production task definition and image digests.
- Run the production migration receipt before application activation.
- Use a non-customer tenant and drawing for the first smoke.
- Verify target health, browser flow, recent logs, alarms, audit, and version
  persistence.
- Keep the rollback operator, task definition, image digests, database
  posture, and effective catalog snapshot in the cutover receipt.

## Ownership

| Lane | Owner | State |
|---|---|---|
| Application integration and cat browser flow | `codex/cat-production-integration-20260726` | in progress |
| Production rollback workflow | infrastructure PR 183 owner | blocked by adversarial review |
| Docker build-context cleanup | application PRs 207 and 208 owners | merged |
| General E2B author boundary | unowned | blocking |
| Staging authored-execution activation workflow | unowned | waits for general E2B boundary |
| Production Postgres and canonical-worker wiring | unowned | blocking |
| Authenticated two-tenant staging acceptance | unowned | waits for staging activation |

## Stop conditions

Do not cut over when any release gate is open. Do not use root credentials for
cloud mutation. Do not enable authored execution by editing a live task
definition without the reviewed source, migration, secret, sandbox, rollback,
and acceptance receipts.
