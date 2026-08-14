# Workspace and project bootstrap

Parent: the Leaf Platform project graph. This process creates or reuses the
tenant and project identity that every app profile, including iOS, binds to.

## Contract

1. Sign in through Leaf Platform. The verified identity is the only tenant
   selector in live mode.
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
