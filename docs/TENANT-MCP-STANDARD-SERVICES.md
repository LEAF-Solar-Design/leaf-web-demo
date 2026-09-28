# Tenant MCP standard services

Parent: the universal tenant-safe standard-services rollout. This document
covers the Leaf app and harness consumer boundary.

Leaf stores each human approval as `pending`, `approved`, `executing`,
`completed`, or `uncertain`. Only the authenticated human host can move pending
to approved. The transition from approved to executing is atomic and has a
unique execution claim. Only that claimant may call the broker. A completed row
stores the safe broker result, so a retry can return the same receipt without
another broker call. Observers never execute an existing executing row.
An uncertain row never claims again; the human host can reconcile it only through
the broker's journaled confirm, described below.

The public broker journals every approved call. A repeated `services_confirm`
for the same approval and the same six identity fields returns the stored
completed or uncertain receipt, reports `executing` while the broker lease is
live, and never calls the adapter twice. Completed and uncertain receipts stay
readable for 24 hours. If the broker never received the first confirm and the
approval is still unexpired, the replay is that call's first and only execution.
The harness human host can reconcile an uncertain row through that replay: on
an execute with a valid attachment it replays the confirm, journals a completed
receipt with one conditional update from `uncertain`, and returns it. Any other
replay answer leaves the row uncertain, and a live `executing` row is never
replayed. The app gateway does not yet send an execute for an uncertain row,
so end to end a lost response still becomes `uncertain` and Leaf reports that
safe status without retrying the operation. Until that app path lands, the
tenant catalog omits `mutate-tenant` and `operator-privileged` tools. This protects
against duplicate effects, but it does not claim that an uncertain operation
completed or failed.

Leaf preserves the broker provider's artifact contract end to end. An artifact
ID is 16 to 256 ASCII letters, digits, underscores, or hyphens, including a
leading underscore or hyphen. A completion receipt carries at most 64 artifact
IDs. Approval IDs keep their separate, stricter contract.
