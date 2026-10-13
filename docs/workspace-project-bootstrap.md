# Workspace and project bootstrap

Parent: the Leaf Platform project graph. This process creates or reuses the
tenant and project identity that every app profile, including iOS, binds to.

## Contract

1. Sign in through Leaf Platform. The verified subject's active durable
   identity binding selects the tenant in live mode; an org claim does not.
2. `POST /api/orgs` with the workspace display name. A replay for the same
   identity and normalized name returns the same `org_id`. A different bound
   org name returns `409`. Never search all tenants by display name.
3. `POST /api/projects` with the project display name. The active normalized
   name is unique inside that org. A replay returns the same `project_id`.
4. Store and pass the returned `project.project_id` UUID as the platform
   project identity. Do not substitute a display name, bundle ID, Terraform map
   key, catalog key, or repository name.
5. Before provider activation, require exact equality between the approved
   project UUID, the provider project grant, the projected runtime config, and
   the provider receipt. Stop on a duplicate, name mismatch, UUID drift, or a
   non-canonical project authority mode.

The two POSTs are safe to retry after a lost response. They are not a license to
create a second tenant or project. In live mode the server derives tenancy from
the verified session and ignores client-supplied tenant identity.

## Authorized provisioning handoff

Live bootstrap checks the verified subject's active durable binding first. An
already-bound member reuses that workspace on normalized-name replay, even if
the token's org claim differs. A different or unavailable workspace returns
HTTP 409 with this string `detail`:

> This identity is already assigned to a workspace. Contact your workspace administrator for authorized provisioning or access recovery.

Use the existing workspace. If access needs recovery, contact its administrator;
bootstrap never moves a binding, changes its role, or renames the workspace.

An unbound identity with the configured namespaced `org_id` key receives HTTP
409 with this string `detail`, before any workspace or binding is created:

> This identity requires authorized workspace provisioning. Contact your workspace administrator to complete provisioning, then sign in again.

Key presence is sufficient, including null, empty, malformed, unknown, and
inactive organization values. The namespace comes from `LEAF_TENANT_CLAIM_NS`
and is normalized with a trailing slash. The hint neither selects an
organization nor authorizes membership. An administrator must establish the
durable membership through authorized provisioning before the member signs in
again and retries access. Bootstrap never invokes provisioning automatically.
An unbound identity without that key can still bootstrap its first workspace.
Both refusals retain string `detail` responses so the workspace controller's
existing 409 handling can surface these instructions.

`platform.store.create_identity_binding` is a trusted server-side writer, not
an enrollment endpoint. Its caller must independently establish authorization,
the intended active organization, and the permitted role; a sign-in token's
org hint cannot supply that authority. There is no browser-facing binding API.
The protected `POST /api/operator/identity-bindings/pair` route has a narrower
scope: exactly two distinct Auth0 subjects in two distinct tenants, both with
`read_only` roles, under its operator and environment checks. It is not a generic
single-user onboarding API.

## iOS source and approval

1. The platform reconciles its copy from the provider's catalog projection when a member reads the project's sources. The internal POST remains an authenticated compatibility route.
2. A project member reads sources and approvals through `GET /api/projects/{id}/ios/sources`.
3. A project owner approves the exact catalog tuple through `POST /api/projects/{id}/ios/approvals`.
4. Readiness and launch consume that approval as before.

The platform refuses an absent source with `catalog_entry_missing` and a tuple
that differs from the catalog with `approval_tuple_mismatch`. The latter names
the first field that differs, without its value. No browser free text ever
becomes an approval of source fields.
The `revision` is the approval's bounded project revision label (Studio's
`canonicalVersionId`), distinct from the catalog's `source_revision` key; only
`source_sha256`, `bundle_identifier`, `marketing_version`, and `build_number`
must equal the entry selected by that key.
