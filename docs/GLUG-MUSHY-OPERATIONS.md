# Glug Mushy control operations

Glug is one organization-owned specialization inside the shared Malleable workspace. This service is its trusted maintenance boundary. Mushy runs here, never in the iOS process or browser.

## Required production configuration

Set `GLUG_MUSHY_ENABLED=true` only after the control-plane values below are present. The profile is recorded in `deploy/required-config.glug-mushy.json` and extends the ordinary app requirements.

| Variable | Purpose |
| --- | --- |
| `GLUG_MUSHY_AUTHOR_URL` | Exact authenticated harness endpoint. Its path must be `/internal/glug/mushy/author`. |
| `GLUG_MUSHY_CANONICAL_GIT_SOURCE` | Clean standalone Glug clone used as the source for fresh job workspaces. |
| `GLUG_MUSHY_WORKSPACE_ROOT` | Dedicated directory for isolated, short-lived job clones. It must not overlap the canonical source. |
| `LEAF_GLUG_MUSHY_ARTIFACT_ROOT` | Immutable built artifact for Mushy source pin `c3fdc0869692c804ae69fe00b5b6f0722c80943a`. |
| `GLUG_MUSHY_CLAIM_SIGNING_SECRET` | Server-only HMAC key for claims, workspace state, and receipts. |
| `GLUG_MUSHY_JOB_DATABASE` | Durable SQLite file used for jobs and single-use publication approvals. |
| `GLUG_GITHUB_REVIEW_TOKEN` | Fine-grained token that can push branches and open pull requests in Glug. It must not have merge, release, deployment, payment, or App Store rights. |
| `GLUG_REPOSITORY_SLUG` | Exact alumni-organization repository slug. |
| `GLUG_MUSHY_CONTROL_TENANT_ID` | Control-plane tenant resolved from the verified service identity. |
| `GLUG_MUSHY_CONTROL_SUBJECTS` | Direct internal operator subjects plus the dedicated Next.js proxy subject. |
| `GLUG_MUSHY_PROXY_SUBJECT` | The one service subject allowed to forward a signed Glug board actor. |
| `GLUG_MUSHY_PROXY_SIGNING_SECRET` | Shared HMAC key for the Next.js board proxy. Use at least 32 random bytes. |

`LEAF_HARNESS_SECRET` is inherited from the app profile and authenticates this one internal hop. The app startup hook validates the pinned artifact, its manifest-declared entrypoint, canonical clone, isolated workspace root, durable database, approval store, and review provider. It rechecks the entrypoint digest before every author request.

Enable the author in the harness only after `deploy/required-config.glug-mushy-harness.json` is satisfied:

| Variable | Purpose |
| --- | --- |
| `GLUG_MUSHY_AUTHOR_ENABLED` | Opt-in switch for the Glug-only author route. |
| `GLUG_MUSHY_AUTHOR_TENANT_ID` | Fixed harness grant identity used for Glug maintenance. |
| `GLUG_MUSHY_WORKSPACE_ROOT` | The same disposable-workspace volume mounted in the control plane. |
| `LEAF_GLUG_MUSHY_ARTIFACT_ROOT` | Read-only exact built artifact from the pinned Mushy source. |
| `LEAF_GLUG_ADOPTION_MANIFEST_FILE` | Optional manifest path override. The image default is `/app/glug/glug_adoption_manifest.json`. |

The harness inherits `LEAF_HARNESS_SECRET` and its grant-store controls from the ordinary harness profile. Install the fixed Glug grant under `GLUG_MUSHY_AUTHOR_TENANT_ID`. Do not place that grant in the control plane. Seed the same immutable artifact volume at `/data/glug/mushy-artifact` in the app and `/app/glug-mushy-artifact` in the harness before enabling either side.

The control plane sends only the closed author request, source pin, timeout, and harness secret. The harness verifies the full manifest-declared artifact tree before every run and imports only `src/ports/impl/repoEditRunner.js` from it. No environment value can replace the editor module. The harness has the model grant but no GitHub, Stripe, deployment, or App Store credential. The control plane has the narrow GitHub review credential but no model grant.

The author has one 240-second cancellation budget that starts before artifact verification, workspace checks, grant lookup, and editor loading. The harness gives the pinned editor only the remaining budget and passes the same outer abort signal into its Agent SDK query. At the limit, the SDK closes the child input, gives it about two seconds to exit cleanly, then forwards the abort signal to the transport process. The harness restores an aborted workspace before responding. The control-plane HTTP wrapper stops at 280 seconds, and a stale workspace becomes reclaimable at 300 seconds. These three limits leave cleanup time without letting a late author publish or complete an old claim.

## Authority boundary

The Next.js proxy first resolves an active Glug `board_admin` membership from the Glug session. It then signs the actor, timestamp, method, exact path, and raw-body digest. The control plane accepts that forwarded actor only when the caller is the configured proxy service subject and the signature is fresh and valid.

The six available powers are `code_question`, `announcement_draft`, `schedule_draft`, `stage_change`, `create_review_branch`, and `create_pull_request`. Publication requires a completed stage job and a separate, exact, ten-minute, single-use approval. The service has no merge, deploy, treasury, membership, raw member, raw finance, or App Store publication power.

## Restart and failure behavior

- Code questions, drafts, and staged changes receive at most two attempts. A process restart requeues a safe interrupted job only while one attempt remains.
- Publication jobs receive one attempt and never resume automatically after a restart. A board administrator must inspect the repository and issue a new exact approval before another publication job.
- Jobs expire after 24 hours. An expired queued job becomes terminal without invoking the author.
- Claim tokens are stored only as hashes. Completed results and stable error codes remain in the job database for polling after process restarts.
- A GitHub failure does not grant merge or deployment authority. Inspect the exact branch before retrying because the remote branch may have been created before pull-request creation failed.

Back up the job database as one SQLite unit before host replacement. Restore it with the same claim-signing secret and the same canonical source identity. Never copy it into the Glug app repository or a browser-visible record.

## Monitoring and response

Alert on repeated terminal codes `author_unavailable`, `author_failed`, `provider_unavailable`, `receipt_invalid`, `unsafe_repository`, `dirty_result`, `job_expired`, and `restart_recovery_required`. Correlate with the actor-scoped job ID, never with prompt text or credentials.

For a suspected credential or authority incident:

1. Set `GLUG_MUSHY_ENABLED=false` and restart the service.
2. Revoke `GLUG_GITHUB_REVIEW_TOKEN` at GitHub.
3. Rotate `GLUG_MUSHY_PROXY_SIGNING_SECRET` in both services and rotate the proxy machine credential.
4. Inspect only `glug/mushy/*` branches and open Glug pull requests created by the review provider.
5. Preserve the job database and service logs as evidence. Do not replay failed publication jobs.

Disabling this rail does not remove a remote branch or close a pull request. Those are visible, non-merged review artifacts and require an explicit repository action.
