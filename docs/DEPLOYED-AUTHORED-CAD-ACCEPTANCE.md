# Deployed authored CAD acceptance

This driver tests the real staging web and API services. It does not intercept
browser routes or load proof fixtures.

Run it only with two non-customer Auth0 tenants. Each tenant must have a linked
Claude grant. Use a new run ID and two acceptance drawing IDs for every run.

## Required deployment manifest

Export the exact staging task images before the run. Save them in this format:

```json
{
  "schema": "leaf.deployment-image-manifest.v1",
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

The driver rejects a missing service, a mutable image tag, or a mixed source
revision.

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
LEAF_ACCEPTANCE_IMAGE_MANIFEST=<absolute manifest path>
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
`LEAF_ACCEPTANCE_ALLOWED_HOSTS`. Each host must use its canonical spelling
without a trailing DNS dot. The driver always rejects known production hosts.
The publication approval secret is required only with `--execute`. It
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

1. The live source revision equals the deployment manifest.
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
the exact tool confirmation. The driver accepts only a run request for that
tool, the exact acceptance drawing, and drawing version 1. It does not click
generic approval buttons. After the server creates version 2, the driver orbits
the 3D view, runs Undo, and runs Redo. It then proves that forged tenant headers
cannot return the other tenant's executed drawing bytes.

The driver creates the receipt with exclusive file creation. It never
overwrites an earlier receipt. The receipt contains tenant and drawing hashes,
image digests, source revision, checks, and timestamps. It does not contain
JWTs, Claude grants, tenant IDs, drawing IDs, prompts, or browser traces.

## Stop conditions

Stop the run when any check fails. Do not retry by changing the target host,
expected revision, tenant ID, or drawing ID. Fix the deployment or create a new
run ID.

This driver does not provision secrets, run migrations, deploy images, enable
authored execution, or promote production. Those actions need their own
reviewed workflows and receipts.
