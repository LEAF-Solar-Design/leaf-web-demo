# Browser and iOS finish contract

## 1. Scope

This is the frozen P0 contract for the W4h program's Browser/workspace and iOS profiles in the Leaf Automation platform. The plan of record is the W4h plan of record. Every slice in section 6 builds against this document. A slice that needs a different decision amends this document first and appends a version record in section 8.

The profiles project one project graph across intent, material, capability, and execution. Tenant, principal, project, source, version, approval, conversation, history, and receipt identity survive profile changes. Availability may be claimed only when the enabled route and receipt contract exist. The iOS profile consumes the existing one-shot ship lane through sanitized readiness and terminal receipts; it never collects Apple credential material in the browser. Source: `MISSION.md` 60-100, the mission doctrine.

This document changes no code and authorizes no infrastructure activation. The decisions below are the planner's frozen P0 specification. Measured code citations below come only from the scout inventory.

## 2. What exists today (measured)

The claudewalk provider exposes three routes in `scripts/ios_ship_provider_service.py`:

| Route | Behavior and auth | Scout citation |
| --- | --- | --- |
| `GET /healthz` | No auth; health returns 200 or 503. | `ios_ship_provider_service.py:1185-1190` |
| `GET /v1/readiness` | Bearer token; exact validated source and project query tuple; returns `leaf.ios-ship-provider-readiness.v1`. | `ios_ship_provider_service.py:1192-1210` |
| `POST /v1/ios-ship/executions` | Bearer token plus `Idempotency-Key`; admits the intent, returns 202, rejects conflicting execution identity with 409. | `ios_ship_provider_service.py:1212-1228` |

The provider compares a static bearer token with `hmac.compare_digest`, reads it from a mode-0600 file, and requires TLS. Sources: `ios_ship_provider_service.py:583-586`, `ios_ship_provider_service.py:1256-1263`. The callback uses a platform bearer token, `X-Leaf-Ios-Ship-Provider`, verified TLS, and the projected `callback-route.json`. Source: `ios_ship_provider_service.py:1151-1179`.

The control ledger fixes this stage order: `SOURCE_APPROVED`, `GRANT_READY`, `APP_RECORD`, `BUNDLE_READY`, `MAC_ALLOCATED`, `XCODE_READY`, `SIGNING_READY`, `BUILT`, `UPLOADED`, `COMPLIANCE`, `BETA_ASSIGNED`, `CREDENTIALS_SCRUBBED`, `MAC_RELEASED`, `RECEIPT`. Source: `ios_ship_control.py:38-53`. Its `ExternalRunner` invokes the adapter for each stage; the ledger validates outputs. Sources: `ios_ship_control.py:1031-1069`, `ios_ship_control.py:565-651`.

Every remote action in the stage adapter uses SSM through `_ssm_wait_online`, `_ssm_send`, `_ssm_capture`, `_ssm_run`, `_ssm_json`, and `_ssm_push_file`. Execution uses an EC2 instance ID; file transfer uses S3 presign plus remote curl. No SSH exists in that code. Sources: `ios_ship_stage_adapter.py:802-881`, `ios_ship_stage_adapter.py:884-909`.

Allocation creates a dedicated host and EC2 instance; release terminates the instance and releases the host. Sources: `ios_ship_stage_adapter.py:1128-1175`, `ios_ship_stage_adapter.py:1632-1758`. The current allocation minimum is 24 hours. Sources: `ios_ship_control.py:257-258`, `ios_ship_stage_adapter.py:266-267`.

The current receipt schema is `leaf.ios-testflight-receipt.v1`. Its fields are `schema`, `run_id`, `request_digest`, `review_id`, `tenant_id`, `project_id`, `source_revision`, `source_artifact_digest`, `bundle_id`, `marketing_version`, `build_number`, `image_id`, `image_digest`, `host_id`, `instance_id`, `region`, `availability_zone`, `instance_type`, `minimum_allocation_hours`, `estimated_cost_usd`, `xcode_version`, `xcode_build`, `app_store_connect_app_id`, `app_store_connect_build_id`, `status`, `beta_group`, `compliance_answered`, `credentials_scrubbed`, `mac_instance_state`, `dedicated_host_state`, `teardown_receipt_id`, and `completed_at`. Sources: `ios_ship_control.py:36`, `ios_ship_control.py:654-701`.

The v1 host, instance, region, zone, type, allocation, cost, and teardown fields assume EC2. The constructor refuses a receipt without a released dedicated host. Source: `ios_ship_control.py:661-662`. Three enforcement points bind the wire shape: `ios_ship_stage_adapter.py:1817-1835`, `ios_ship_control.py:631-650`, and `platform/ios_ship.py:851-893`. The platform requires `VALID`, answered compliance, scrubbed credentials, a terminated instance, and a released host. Source: `platform/ios_ship.py:887-892`.

`record_approval` is defined at `platform/ios_ship.py:261` and has NO caller outside tests, according to the scout's call-site search. The importer verifies source catalog inputs but writes no platform approval. Source: `ios_ship_source_importer.py:21-24`.

The source publisher role `leaf-ios-ship-source-publisher-staging` uses the GitHub OIDC `ios-source-publish` environment for three named repositories. Its S3 keys are terraform-enumerated, including each revision's `source.bundle` and `producer-receipt.json`; each new revision requires an added key. Sources: `leaf_ios_ship_provider_runtime.tf:24-35`, `leaf_ios_ship_provider_runtime.tf:88-91`, `leaf_ios_ship_provider_runtime.tf:97-140`. The importer writes immutable catalog entries. Source: scout's quotation of `tasks/ios-ship-catalog-contract.md`. The wiring between the S3 publish and the importer's invocation was not found in code.

The projector is an ECS Fargate task launched by the provider through `ecs run-task`. Source: `ios_ship_provider_service.py:772-935`. It validates signing inputs and resolves callback routes. Sources: `ios_ship_projector.py:212-240`, `ios_ship_projector.py:291-292`.

`tasks/ios-ship-provider-integration.md` says the adapter uses public-IP SSH plus local PEM files. This contradicts the SSM-only code at `ios_ship_stage_adapter.py:802-909`. This contract resolves the contradiction in favour of the code; the document is stale. Source: scout section 6.

The Browser workspace backend is live on staging: `GET /api/projects` answers 200 with the org resolved from the verified identity binding. A verified token whose subject has no binding is answered 403 with the detail `verified subject has no active platform identity binding` (`platform/deps.py` `get_org_id`), which is the first-run state slice B1 reads as unbound; the client's `X-Org-Id` header is ignored in live mode. `POST /api/orgs` is idempotent by identity plus normalized name and answers 409 for a different name. `leaf.org_id` in localStorage is the dev seam only. The lifecycle panel invites by `binding_id` with no email lookup. Source: planner's P0 Browser facts; the scout supplies no Browser file line numbers.

The executor fleet fact comes from the fleet's Mac-host notice of 2026-09-17:

As of 2026-09-17 the account holds no EC2 Mac host or instance. All iOS work runs on the operator's Mac mini, whose signing keychain holds the App Store distribution identity and the Apple intermediate certificates, and which built, signed and uploaded valid TestFlight builds that day.

## 3. Frozen decisions

1. **Executor.** The executor is the operator's Mac mini, reached over SSH on the operator's private tailnet (host name, account and pinned host key live in private infra configuration), never Bonjour and never a LAN IP. The host key is pinned in a repository-committed `known_hosts` file in private infra configuration. No lane allocates or launches an EC2 Mac without a fresh operator yes.
   Reason: the Mac build-host rule and the fleet's Mac-host notice select the mini.

2. **Transport.** The `_ssm_*` family in `ios_ship_stage_adapter.py` becomes one `RemoteExec` interface with `ssm` and `mini` backends. Keep `ssm` exercised by existing tests. The new `mini` backend uses `ssh -o BatchMode=yes` plus `scp`; scp file push replaces the S3-presign-plus-curl pull. Every later stage calls the interface, never a backend.
   Reason: the transport dependency spans every remote stage, as measured in section 2.

3. **Allocation.** Allocation on the mini is a LOCK, not a host. `MAC_ALLOCATED` acquires `~/leaf-ios-runs/.lock` by atomic `mkdir`, carrying the run id, and stages material under `~/leaf-ios-runs/<run_id>/`. `MAC_RELEASED` removes that run directory, verifies its material census is empty, and releases the lock. Retarget the existing `_REMOTE_SCRUB_COMMAND` idea to the run directory. A held lock from another run is the specific recoverable readiness state "mini busy", never a failure.
   Reason: one shared mini needs serialized runs and exact cleanup, not EC2 allocation proof.

4. **Receipt.** The schema becomes `leaf.ios-testflight-receipt.v2`; common fields stay. Add a required `executor` object with `kind` (`mac-mini` or `ec2-mac`), `host` (Tailscale name or EC2 host id), `run_lock_released` (bool), and `run_material_removed` (bool). Move `instance_id`, `availability_zone`, `instance_type`, `minimum_allocation_hours`, `estimated_cost_usd`, `mac_instance_state`, and `dedicated_host_state` into `ec2`, required for `ec2-mac` and absent otherwise. The mini terminal-proof gate requires `status == "VALID"`, `compliance_answered`, `credentials_scrubbed`, `executor.run_material_removed`, and `executor.run_lock_released` all true. A v1 receipt stays readable everywhere as kind `ec2-mac`, with its EC2 fields lifted into `ec2`; historical AWS receipts never become invalid. No mini path may claim `mac_instance_state: terminated`. The three enforcement points move together in ONE coordinated change set: M1 owns the two claudewalk validators and S2 owns `platform/ios_ship.py`'s validator. This section fixes the wire shape.
   Reason: real executor cleanup must replace fictional EC2 teardown while preserving historical receipts.

5. **Approval.** The approval producer is ABSENT today. S1 creates the owner-only route `POST /api/projects/{project_id}/ios/approvals`. It takes the registered native manifest's tuple (`bundle_identifier`, `marketing_version`, `build_number`, `source_revision`, `source_sha256`), verifies it against the importer's immutable source catalog entry, and only then calls `record_approval`. The manifest shape is `ios-project-manifests/bakery-stock.json`, bundle `ai.leafautomation.bakerystock`. Never create approval from browser free text or for a revision absent from the catalog.
   Reason: a tested storage function without a production caller cannot supply approved source readiness.

6. **Source publishing.** The terraform-enumerated S3 keys and GitHub OIDC `ios-source-publish` environment remain the design of record. GitHub Actions is locked for the org's private repositories, per the planner's P0 specification. M2 must state the replacement path for a revision's `source.bundle` and `producer-receipt.json` to the catalog: an AWS-rail deploy step or a publish from the mini under the same write-once role. The importer stays the only consumer. The wiring between the S3 publish and the importer's invocation was not found in code.
   Reason: an enumerated publish permission does not establish an invoked importer.

7. **Readiness.** Readiness stays fail-closed. The iOS tab claims launch only when the approved revision, the grant AND the mini executor are all real. Each missing part has its own named state: "no approved revision", "grant not ready", "mini unavailable", "mini busy".
   Reason: MISSION.md permits availability claims only with an enabled route and receipt contract.

8. **Demo.** The anonymous demo shows a labelled sample workspace, decision D1's default. No sample id ever reaches a live request.
   Reason: sample data must not claim or mutate a real workspace.

## 4. Executor receipt shape (the JSON)

This synthetic example defines every field of a v2 `mac-mini` receipt; its values are examples, not a shipping claim. Common legacy `host_id` repeats `executor.host`; common `region` identifies the provider region. `image_id` and `image_digest` are null for a mini without an EC2 image. The `ec2` object is absent for this kind. The control ledger assembles the wire object from validated stage results and platform-bound request identity.

```json
{
  "schema": "leaf.ios-testflight-receipt.v2",
  "run_id": "example-run",
  "request_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "review_id": "example-approval",
  "tenant_id": "example-tenant",
  "project_id": "11111111-1111-4111-8111-111111111111",
  "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "source_artifact_digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "bundle_id": "ai.leafautomation.bakerystock",
  "marketing_version": "1.0.0",
  "build_number": "1",
  "image_id": null,
  "image_digest": null,
  "host_id": "mac-mini.example.internal",
  "region": "us-east-1",
  "xcode_version": "example-version",
  "xcode_build": "example-build",
  "app_store_connect_app_id": "1234567890",
  "app_store_connect_build_id": "22222222-2222-4222-8222-222222222222",
  "status": "VALID",
  "beta_group": "example-group",
  "compliance_answered": true,
  "credentials_scrubbed": true,
  "teardown_receipt_id": "example-cleanup",
  "completed_at": "2026-09-17T12:30:00Z",
  "executor": {
    "kind": "mac-mini",
    "host": "mac-mini.example.internal",
    "run_lock_released": true,
    "run_material_removed": true
  }
}
```

Field writers below identify the producer; the control ledger serializes the final receipt.

| Field | Writer |
| --- | --- |
| `schema` | Control ledger selects v2. |
| `run_id` | Control ledger binds the stable run. |
| `request_digest` | Control ledger hashes the admitted request. |
| `review_id` | Platform supplies the approved identity. |
| `tenant_id` | Platform supplies the verified tenant. |
| `project_id` | Platform supplies the authorized project. |
| `source_revision` | Platform supplies the catalog-bound approved revision. |
| `source_artifact_digest` | Stage adapter attests the exact source. |
| `bundle_id` | Platform supplies the approved manifest bundle. |
| `marketing_version` | Platform supplies the approved manifest version. |
| `build_number` | Platform supplies the approved manifest build. |
| `image_id` | Stage adapter supplies the image identity or mini null. |
| `image_digest` | Stage adapter supplies the image digest or mini null. |
| `host_id` | Stage adapter repeats the executor host. |
| `region` | Stage adapter supplies the provider region. |
| `xcode_version` | Stage adapter records the observed tool version. |
| `xcode_build` | Stage adapter records the observed tool build. |
| `app_store_connect_app_id` | Stage adapter records the ASC app identity. |
| `app_store_connect_build_id` | Stage adapter records the ASC build identity. |
| `status` | Stage adapter records the terminal ASC status. |
| `beta_group` | Stage adapter records the assigned group. |
| `compliance_answered` | Stage adapter records compliance completion. |
| `credentials_scrubbed` | Stage adapter records the scrub and empty census. |
| `teardown_receipt_id` | Control ledger binds the validated cleanup result. |
| `completed_at` | Control ledger writes the timezone-aware completion time. |
| `executor` | Control ledger assembles validated executor facts. |
| `executor.kind` | Stage adapter records the selected executor kind. |
| `executor.host` | Stage adapter records the Tailscale name or EC2 host id. |
| `executor.run_lock_released` | Stage adapter records lock release proof. |
| `executor.run_material_removed` | Stage adapter records material removal proof. |
| `ec2` | Control ledger includes it only for `ec2-mac`. |
| `ec2.instance_id` | Stage adapter records the allocated instance. |
| `ec2.availability_zone` | Stage adapter records the allocation zone. |
| `ec2.instance_type` | Stage adapter records the allocated type. |
| `ec2.minimum_allocation_hours` | Stage adapter records the EC2 minimum. |
| `ec2.estimated_cost_usd` | Stage adapter records the EC2 cost estimate. |
| `ec2.mac_instance_state` | Stage adapter records observed instance teardown. |
| `ec2.dedicated_host_state` | Stage adapter records observed host release. |

For v1 reads, the control ledger and platform normalize the legacy shape without rewriting the stored receipt or adding new mini proof requirements. Existing EC2 terminal gates still apply to historical receipts. New EC2 receipts retain those teardown gates; mini receipts use the section 3.4 gate.

## 5. The mini's preconditions (from the fleet's Mac-host notice)

The mini must have the signing keychain, the distribution identity, and WWDR G3/G4/G6 intermediates before signing. Without the intermediates, `find-identity` reads 0 valid and codesign gives `errSecInternalComponent`. Source: the fleet's Mac-host notice summarized in section 2.

Both the user's MobileDevice provisioning-profile directory and the user's Xcode UserData provisioning-profile directory must exist before `ios_distribute` uploads profiles. Apple's ASC `uploadedDate` is NOT UTC; a consumer must not label that raw value UTC. Source: the fleet's Mac-host notice.

The LM Studio bench window can hold about 23 GB of mini memory. The specified action is to unload the local model bench before a memory-heavy run. Source: planner's P0 preconditions. This document does not perform that action or cancel another lane's bench.

The mini is one shared device. Runs serialize on `~/leaf-ios-runs/.lock`, and cleanup is scoped to the owning run's material. Source: frozen decision 3.3.

## 6. File ownership by slice

Client paths below are relative to `web/src/` unless they already start with `web/src/`; server and platform paths are repository-relative. These assignments come from the planner's P0 specification. S1 completes its shared-file edit before S2; J1 completes before J2. X0's split precedes edits to its extracted files. B1, B2 and B3 add their new components under `web/src/workspace/`; `web/src/projects/` is B4's alone.

| Slice | Repository | Owned files | Contract section |
| --- | --- | --- | --- |
| M1 | claudewalk | `scripts/ios_ship_stage_adapter.py`, `scripts/ios_ship_control.py`, new `scripts/ios_ship_mini_executor.py`, their tests | 3.2, 3.3, 3.4 |
| M2 | infra | `terraform/environments/staging/us-east-1/leaf_ios_ship_provider_runtime.tf`, `leaf_ios_ship_integration.tf`, narrowly required runtime config for mini host name, pinned host key, and tailnet route | 3.1, 3.6; apply, IAM, ECS, and secrets are the operator's |
| S1 | leaf-web-demo | `platform/ios_ship.py`, `server/routers/ios_ship.py`, one migration if needed, `docs/workspace-project-bootstrap.md`, tests | 3.5 |
| S2, after S1 | leaf-web-demo | `platform/ios_ship.py`, `server/ios_surface_bridge.py`, `server/routers/ios_surface.py`, `server/routers/ios_ship_provider.py`, `server/ios_ship_provider.py`, tests | 3.4, 3.7 |
| I1 | leaf-web-demo client | `web/src/ios/DeviceGround.jsx`, `ios/IosSurface.jsx`, `ios/iosSurfaceStatus.js`, `ios/useIosSurface.js`, `ios/ShipReceipts.jsx`, `web/src/site/iosShipReadiness.js`, ship block of `web/src/site/ToolCast.jsx`, new `web/src/ios/useIosShipController.js` | 3.7 |
| B1 | leaf-web-demo client | `components/ProjectSwitcher.jsx`, `controllers/workspace/*`, new `workspace/ProjectStartPanel.jsx` | 1, 2 Browser facts |
| B2 | leaf-web-demo client | `site/ProjectBoardGround.jsx`, `site/BoardTiles.jsx`, new `workspace/ProjectWorkspacePanels.jsx`, `site/projectBoard.css` | 1, 2 Browser facts |
| B3 | leaf-web-demo client | `components/DrawingUploadControl.jsx`, `controllers/upload/*`, new `workspace/ProjectMaterialIntake.jsx` | 1, 2 Browser facts |
| B4 | leaf-web-demo | Client `projects/*`; `platform/project_lifecycle.py` and `platform/api.py` for the scoped member read only | 1, 2 Browser facts |
| D1 | leaf-web-demo client | New `demo/workspaceFixture.js`, `demo/useWorkspaceFixture.js` | 3.8 |
| J1 | leaf-web-demo client | Exclusively `App.jsx`, `site/productSurfaces.js`, `lib/ribbonClusters.js` | 1, 3.7, 3.8 |
| J2, after J1 | leaf-web-demo client | Exclusively `App.jsx`, `site/productSurfaces.js`, `lib/ribbonClusters.js` | 1, 3.7, 3.8 |
| X0 | leaf-web-demo client | `site/SurfaceGrounds.jsx` split into `site/ProjectBoardGround.jsx`, `site/BoardTiles.jsx`, `ios/DeviceGround.jsx`, `site/groundWindow.js` | 1, 6 |

## 7. Open questions with defaults

The planner's P0 specification fixes these defaults until an operator decision amends this contract.

| Operator item | Default |
| --- | --- |
| Demo fixture | A labelled sample workspace, D1. |
| Pilot app | Bakery Stock. |
| EC2 Mac fallback | None without a fresh yes. |
| Real signed-in staging identity for the Browser matrix | The staging acceptance principal (tier hosted_pro). |
| M2 activation hands | The operator applies. |

## 8. Version record

v1 (2026-09-17): frozen from the P0 read (the scout inventory, the mission doctrine, the fleet's Mac-host notice of the same day).
