# Role system — operator runbook

The one place to manage *who may do what* across the platform's capability
gates. Design contract: `contract/AUTH.md` §11.5. Code: `server/roles.py`,
`server/roles.json`, the two Auth0 Actions in `server/auth0-actions/`.

Two dimensions, one effective answer:

| Dimension | Question | Source of truth | Toggle surface |
|---|---|---|---|
| **Tier** | what the workspace's plan bought | billing → stored org row (claim is a hint) | `server/entitlements.json` (per-tier booleans) |
| **Roles** | what this identity may additionally do | Auth0 role assignment → `…/roles` claim | `server/roles.json` (per-role grants) |

Effective capabilities = tier baseline **OR** role grants (additive-only;
roles never revoke what the plan granted). Every enforcement point — run,
author, converse, uploads, R7, the context packet, the queued-turn re-check —
resolves through this one function (`entitlements.entitlements_for`).

## Assign a role to a user (two equivalent paths)

1. **Auth0 dashboard (preferred):** User Management → Roles → Create Role
   (name must match a `roles.json` entry, e.g. `platform_admin`) → assign the
   user. The Post-Login Action reads `event.authorization.roles`.
2. **API/app_metadata:** set the root-level `app_metadata.leaf_roles`
   array on the user, e.g. `{"leaf_roles": ["platform_admin"]}`. Root level
   is deliberate: leaf_website subscription PATCHes replace
   `app_metadata.leaf` and can never mint or erase a role.

Either path lands in the same verified claim on next login. Revocation =
remove the assignment (or the metadata entry); takes effect on next token.

## Give a staff account everything (the admin recipe)

Both factors, each operator-held:

1. `app_metadata.leaf_admin: true` on the user (root level, strict boolean).
   This mints BOTH `tier=admin` and the `platform_admin` role — one flag,
   one staff identity. (Assigning the Auth0 `platform_admin` role instead
   also works for the role half.)
2. Add the user's Auth0 subject (`auth0|…`) to the server env
   `LEAF_PLATFORM_ADMIN_SUBJECTS` (comma-separated; deployed via the
   terraform env config, targeted apply).

With both: every capability including `platform_customize` (the R7 rollout
mode + always-confirm approval still apply per docs/ADMIN-SELF-EDIT-LANE.md).
With either alone: no elevated capability. Machine (M2M) clients can never
hold `platform_admin` (Action-forbidden).

## Toggle what a role grants

Edit `server/roles.json` — each role has:

- `grants`: capabilities the role turns ON (the frozen 9-capability
  vocabulary, `contract/AUTH.md` §11.3);
- `elevated_grants`: capabilities that ALSO require the subject to be on
  `LEAF_PLATFORM_ADMIN_SUBJECTS`. `platform_customize` may only ever appear
  here (freeze-gated).

Rules the gate suite enforces: only `true` grants (anything unreadable grants
nothing); additive-only; `roles.py _HARDCODED_ROLE_DEFAULTS` must mirror the
file byte-for-byte (update both in the same PR — `tests/test_roles.py` +
`tests/test_auth_vocab_freeze.py` fail on drift).

Deployment note: `roles.json` ships in the image, so a policy change is an
ordinary PR → staging → production deploy. For an out-of-band emergency
override point `LEAF_ROLES_FILE` at an alternate file (same schema); an
unreadable override file fails closed to NO role grants (tier baseline
stands).

## Add a new role

1. Add the entry to `server/roles.json` AND the mirror in `server/roles.py`.
2. Create the same-named Auth0 role (or document the `leaf_roles` value).
3. Names: `^[a-z0-9][a-z0-9_-]{0,62}$`, max 16 honored per token. Unknown
   names in tokens are inert, so claim-first rollouts are safe.

`org_admin` / `org_member` are reserved empty presets for the
org-configuration phase — assigning them today grants nothing extra.

## Auth0 dashboard deploy (manual, like every Action change)

Editing the Action files on disk changes nothing live. Re-paste/redeploy:

1. Actions → Library → the Post-Login action ←
   `server/auth0-actions/post-login-add-tenant-claim.js` → Deploy (stays in
   the Login flow).
2. Actions → the credentials-exchange action ←
   `server/auth0-actions/credentials-exchange-add-tenant-claim.js` → Deploy.
3. Verify locally first: `node post-login-add-tenant-claim.js` (dry-run
   cases) and `node credentials-exchange-add-tenant-claim.test.js` (must
   print PASSED).

Tokens minted before the redeploy simply lack the roles claim → no roles →
tier baseline (safe).

## What roles do NOT cover (deliberately)

- Env/deployment toggles (`LEAF_AUTH_LIVE`, R7 rollout mode, broker store
  modes, ops secret, …) — those are operations config, not identity.
- Quotas, the Claude-credential grant store, per-tool approval policy
  (`agent_policy.json`) — separate per-identity systems; candidates for role
  coupling in a later phase, on purpose not smuggled into v1.
- Mid-turn harness back-edge re-checks are tier-only in v1 (see
  `contract/AUTH.md` §11.5 depth boundary).
