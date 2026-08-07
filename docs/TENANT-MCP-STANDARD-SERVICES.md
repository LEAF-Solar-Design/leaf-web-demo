# Tenant MCP standard services

Parent: the universal tenant-safe standard-services rollout. This document
covers the Leaf app and harness consumer boundary.

Leaf stores each human approval as `pending`, `approved`, `executing`,
`completed`, or `uncertain`. Only the authenticated human host can move pending
to approved. The transition from approved to executing is atomic and has a
unique execution claim. Only that claimant may call the broker. A completed row
stores the safe broker result, so a retry can return the same receipt without
another broker call. Observers never execute an existing executing row. An
uncertain row never runs again.

The public broker does not yet provide a receipt lookup that can reconcile a
lost execution response. Until that contract exists, the tenant catalog omits
`mutate-tenant` and `operator-privileged` tools. A lost response becomes
`uncertain`, and Leaf reports that safe status without retrying the operation.
This protects against duplicate effects, but it does not claim that an
uncertain operation completed or failed.

Leaf preserves the broker provider's artifact contract end to end. An artifact
ID is 16 to 256 ASCII letters, digits, underscores, or hyphens, including a
leading underscore or hyphen. A completion receipt carries at most 64 artifact
IDs. Approval IDs keep their separate, stricter contract.
