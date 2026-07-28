# Deployed authored CAD acceptance

This driver tests the real staging web and API services. It does not intercept
browser routes or load proof fixtures.

Run it only with two non-customer Auth0 tenants. Each tenant must have a linked
Claude grant. Use a new run ID and two acceptance drawing IDs for every run.

## Required live deployment identity

The staging deployment controller must set `LEAF_DEPLOYMENT_IDENTITY` in the
running API task. This deployment-owned runtime evidence is not an input to the
acceptance driver. The authenticated `/api/deployment-identity` endpoint returns
only the validated identity below. A missing or invalid identity returns 503.
The staging controller injects the receipt after it validates the configuration
baseline. `LEAF_RUNTIME_ENV=staging` binds the receipt to staging; the endpoint
also preserves the staging default for older non-production task definitions.

```json
{
  "schema": "leaf.deployment-identity.v1",
  "environment": "staging",
  "source_revision": "<application commit>",
  "services": {
    "app": {
      "image_digest": "sha256:<64 lowercase hex characters>",
      "source_revision": "<application commit>"
    },
    "broker": {
      "image_digest": "sha256:<64 lowercase hex characters>",
      "source_revision": "<application commit>"
    },
    "canonical-worker": {
      "image_digest": "sha256:<64 lowercase hex characters>",
      "source_revision": "<application commit>"
    },
    "harness": {
      "image_digest": "sha256:<64 lowercase hex characters>",
      "source_revision": "<application commit>"
    },
    "web": {
      "image_digest": "sha256:<64 lowercase hex characters>",
      "source_revision": "<application commit>"
    }
  }
}
```

`source_revision` must be the full 40-character lowercase application SHA. The
deployment controller must record the five resolved task image digests after it
selects the live task definitions. The driver rejects a missing service, mutable
image tag, mixed revision, or caller-authored manifest assertion.

The endpoint can validate a production receipt only when the task sets both
`LEAF_RUNTIME_ENV=production` and `LEAF_DEPLOYMENT_ENVIRONMENT=production`.
A production runtime with a missing or staging deployment binding fails closed.
This staging driver still rejects every production target. Production smoke
needs its own protected driver and approval contract; do not weaken this
driver's denylist.

## Required environment

Set these values in a protected CI environment. Do not put JWTs in command
arguments, files, logs, or the receipt.

```text
LEAF_ACCEPTANCE_ENVIRONMENT=staging
LEAF_ACCEPTANCE_RUN_ID=<lowercase unique run id>
LEAF_ACCEPTANCE_WEB_URL=https://<staging web host>
LEAF_ACCEPTANCE_API_URL=https://<staging API host>
LEAF_ACCEPTANCE_ALLOWED_HOSTS=<exact web host>,<exact API host>
LEAF_ACCEPTANCE_EXPECTED_REVISION=<application commit>
LEAF_ACCEPTANCE_PUBLICATION_APPROVAL_SECRET=<protected independent approver secret>

LEAF_ACCEPTANCE_TENANT_A_ID=<resolved Auth0 tenant id>
LEAF_ACCEPTANCE_TENANT_A_JWT=<short-lived tenant A JWT>
LEAF_ACCEPTANCE_TENANT_A_DRAWING_ID=acceptance-<run id>-a
LEAF_ACCEPTANCE_TENANT_A_REQUEST=<novel request that contains the run id>

LEAF_ACCEPTANCE_TENANT_B_ID=<resolved Auth0 tenant id>
LEAF_ACCEPTANCE_TENANT_B_JWT=<short-lived tenant B JWT>
LEAF_ACCEPTANCE_TENANT_B_DRAWING_ID=acceptance-<run id>-b
LEAF_ACCEPTANCE_TENANT_B_REQUEST=<different novel request that contains the run id>
```

The exact web and API hosts must appear in
`LEAF_ACCEPTANCE_ALLOWED_HOSTS`. The driver always rejects known production
hosts, including a terminal DNS dot and explicit or default ports. The
publication approval secret is required only with `--execute`. It
must equal the staging app's `LEAF_CUSTOMIZATION_APPROVAL_SECRET`. Store it as a
masked CI secret. The driver sends it only to the internal approval endpoint.
It never places it in the browser, application JWT headers, logs, or receipt.

## Preflight

Run this command from `web/`:

```powershell
node scripts/deployed_authored_cad_acceptance.mjs `
  --receipt C:\secure-receipts\authored-cad-preflight.json
```

Preflight checks:

1. The authenticated live deployment identity has the expected full source SHA
   and all five immutable service digests.
2. Broker, harness, database, worker, durable stores, and build identity are
   ready.
3. Both JWTs resolve to different tenant IDs.
4. Forged tenant headers cannot override either JWT.
5. Both tenants have linked Claude grants.
6. Each browser starts with a blank command bar and its exact acceptance
   drawing ID.
7. Reload keeps the same drawing ID.
8. The browser does not contact localhost or the proof host.

Loading the workbench can initialize version 1 of the two acceptance drawings.
Preflight does not submit a prompt or approve a change.

## Execute

Use `--execute` only after preflight passes:

```powershell
node scripts/deployed_authored_cad_acceptance.mjs `
  --execute `
  --receipt C:\secure-receipts\authored-cad-execute.json
```

Execute mode runs each tenant in a separate browser context. It opens the
collapsed authoring panel from a blank workbench and submits the tenant's novel
tool request. It requires a server-issued staged receipt for a new
`drawing.write` tool. The driver then uses the protected approval credential,
outside the browser, to approve that exact staged change through
`/internal/customization/confirm`.

The browser publishes the approved tool, selects **Run it now**, and displays
the exact tool confirmation. The driver requires distinct new tool names,
change-set IDs, and exact `drawing.write` capability sets. Each staged browser
request must carry that tenant's exact request. It accepts only a run request
for that tool, the exact acceptance drawing, and drawing version 1. It does not
click generic approval buttons. After the server creates version 2, it records a
stable camera pose, performs the real drag, and rejects an unchanged pose. It
then runs Undo and Redo. It opens version 1 as a read-only preview, proves that
the write control is disabled, proves that the head stays at version 2, and
proves that preview sends no mutating API request.

Before each staged change is published, the driver calls the protected
`/internal/customization/confirm` authority with the other tenant identity and
the exact staged change set. That call must return 403 or 404. It then calls
the same authority with the owner identity and requires a valid confirmation.
This proves both directions of publication approval isolation before the
confirmation is consumed.

The later API phase checks both directions of the remaining tenant isolation.
The other tenant's authored tool must be absent from the catalog. Direct reads
of the other staged change, drawing, and job must return only 403 or 404.
Forged tenant headers must not change the caller's audit projection. Every 2xx
response for a cross-tenant authority probe fails.

Finally, the driver resubmits the exact version-1 write after the head advanced
to version 2 and submits the same request with a stale catalog digest. Both
must return 409, and the drawing head must remain at version 2. This proves
stale-head, stale-catalog, duplicate-request, and exact-request replay denial.
It does not claim that a time-expired approval was exercised.

The driver creates the receipt with exclusive file creation. It never
overwrites an earlier receipt. The receipt contains tenant and drawing hashes,
authenticated live image digests, source revision, checks, and timestamps. It
does not contain JWTs, Claude grants, tenant IDs, drawing IDs, prompts, or
browser traces.

## Stop conditions

Stop the run when any check fails. Do not retry by changing the target host,
expected revision, tenant ID, or drawing ID. Fix the deployment or create a new
run ID.

This driver does not provision secrets, run migrations, deploy images, enable
authored execution, or promote production. Those actions need their own
reviewed workflows and receipts.

The receipt marks these items as `external_evidence.status = required` because
the browser driver cannot prove them honestly: time-expired approval rejection,
service restart persistence, canonical worker lease ownership, CloudWatch logs
and metrics, and durable audit-row inspection. The protected staging operator
must attach those receipts separately. In particular, staging has no safe
clock-control endpoint, so this driver does not wait out or simulate an approval
expiry.

## Protected production web publication

After an executed staging receipt produces a production handoff candidate, use
`deploy-platform-web-production.yml` from `main`. Store `VERCEL_TOKEN` and
`VERCEL_AUTOMATION_BYPASS_SECRET` as repository Actions secrets. The dispatcher
supplies the exact source SHA, release and handoff run IDs and attempts,
reviewed web artifact SHA-256, and the confirmation string printed by the
workflow contract.

Supply an open approval issue number when dispatching. The workflow prints and
waits for an approval string that includes its exact run ID and attempt. A
different collaborator with write access must add that exact string to the
issue within five minutes. The comment author cannot be the actor or triggering
actor. The workflow rechecks the unchanged comment, open issue, live permission,
and 24-hour expiry immediately before promotion. This issue gate provides
independent, single-execution approval on private repositories whose GitHub plan
cannot enforce environment reviewers.

The workflow downloads only the attempt-bound handoff and `web-dist` artifacts.
It checks their GitHub workflow identity, successful conclusion, protected
branch, source, five-service staging evidence, and exact web bytes. It creates a
Vercel Build Output API package from those bytes, so it does not run a build. A
terminal 404 route keeps `/api/*` from falling through to the SPA.

The candidate is first deployed without assigning production domains. The
workflow verifies the immutable URL, source health document, entry asset, SPA
routes, and terminal API boundary before promotion. Candidate probes use the
Vercel automation bypass secret so Deployment Protection stays enabled. It then
verifies the stable project URL and uploads an attempt-bound
`leaf.production-web-deployment.v1`
receipt. If promotion or any later required gate fails, it restores the exact
baseline deployment ID. This receipt proves the Vercel half of the production
identity and retains the sanitized immutable approval proof. The backend
identity remains four OCI digests.
