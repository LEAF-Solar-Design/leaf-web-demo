# Self-serve sign-up

Added with #3626. A new user signs in through Auth0, Studio sees that no workspace exists, and the user creates one
with `POST /api/orgs`. The server creates exactly one org owned by that user on the free tier. A repeat call
returns the same org.

## What the server checks

- The post-login Action stamps `org_id = null` and `tenant_id = <Auth0 sub>` on a user with no workspace.
- `create_org` treats an absent, null or blank `org_id` claim as "no org". Any real value (a provisioned member,
  an unknown or inactive org, or a malformed value) still returns the 409 provisioning handoff.
- Self-provisioning is allowed when `LEAF_ORG_SELF_PROVISION=1`, or on a named non-production runtime
  (`LEAF_RUNTIME_ENV` = staging, development, test or local). Otherwise the answer is 403 `invite_only`.
- After the org exists, the tenant check accepts a `tenant_id` claim equal to the verified subject. The binding is
  resolved from that same subject, so the fallback reaches only that user's own org. Every other mismatch is 409.

## Turning it off

Unset `LEAF_ORG_SELF_PROVISION` on the environment. Existing orgs and bindings are unaffected.

## Linking AutoCAD to the same account

LEAFLOGIN opens the system default browser. If that browser already holds a session for a different Leaf Automation
account, the consent page signs AutoCAD in to that account, and LEAFTOOLSYNC then pulls that account's tools. Sign in
to AutoCAD from the same browser profile that signed in to Studio.
