# Reusing project-bound widget authoring

Parent: the Leaf workspace's Lab widget capability. A workspace profile changes
how the same project is presented; it does not create another project or another
product. This document describes the editor/client contract. It does not grant
service access, publication, financial-data access or production exposure.

## Host inputs and lifetime

Mount `WidgetsView` from `web/src/lab/widgets/WidgetsView.jsx` with the current
`projectId` and `accountScope`. Keep that identity stable across profile changes.
Changing either remounts the project editor and clears the previous in-memory
draft. The host must preserve the editor seat when it wants unsaved drafts to
survive profile or panel changes; a new mount can reopen saved state, not recover
an unsaved draft. Hiding the editor cancels an unfinished pointer gesture.

The account scope partitions client journeys. It is not authentication or server
authorization. The document service must authorize every request independently.
Never reuse a previous user's project context while a new context is unresolved.

A nonempty `scopeKey` selects the separate local-draft mode: no server save or
definition download, and drafts live only in mounted React state. Do not pass it
when the caller expects project persistence. The `host` input supplies supported
board values; literal synthetic sample text requires no host or provider.
The optional `requestPublication` callback exposes a distinct publication action.
Omit it for internal authoring. Save and Download do not publish anything.

## Document identity and persistence

Document IDs use 1–128 ASCII letters, digits, underscores, dots and hyphens, with
an alphanumeric first character. The visible name is mapped by `projectWidgetId`
to a deterministic project-bound artifact ID. Two projects can use the same
visible name without sharing a newly saved artifact. Treat the returned artifact
ID as opaque; do not construct a competing mapping in another host.

The last-opened name in local storage includes API base, tenant, account scope
and project. It is a convenience pointer, not saved document content. Reopen can
still work by entering the name when local storage is unavailable. Project-owned
head lookup precedes compatibility lookup of older unscoped widget documents.
Legacy association uses a client-side first-owner record; it is a compatibility
mechanism, not a cross-device ownership or tenant-security guarantee.

New creates a blank `widget-doc.v1`. Save sends the validated current document,
`kind: widget` and `expected_head` to the artifact's save endpoint. The previous
head is the optimistic-concurrency token, not a timestamp. A successful save
must acknowledge a positive safe-integer `revision`. An HTTP 200 object without
that receipt is a request error and must not advance the local head or report
the draft saved. Do not invent a revision when a response is incomplete.

Edits made while a save is pending remain local. A valid acknowledgement advances
the head of the sent document, while newer local edits keep the editing status.
Late results from an invalidated project/scope request must not replace another
draft. New and Load can replace the current draft: preserve a valid unsaved
definition before choosing those actions if its content is still needed.

## Failure and export rules

| Observation | Expected recovery |
|---|---|
| 404 on head | Start a blank editable document. |
| 409 on save | Keep local edits and previous head; explicit Reload chooses the stored revision. Do not silently merge or overwrite. |
| 422 on save | Keep local content and show the validation problem; correct the field before saving. |
| Network failure, timeout, 503 or malformed save receipt | Keep local edits and previous head. Permit explicit retry or reopen. No automatic save retry. |
| Missing or invalid successful head body | Show an error instead of replacing the draft with an empty document. |

The client has a 10-second default request deadline, clamped to 1 ms through
30 seconds when a caller supplies one. A lost acknowledgement does not prove
the write failed on the server. A subsequent explicit save can therefore return
409; preserve the draft and use the conflict flow rather than bypassing it.

Download creates an actual JSON Blob from the validated current draft. It can
include unsaved edits and does not save them. The editor requires a matching
loaded/document name, a valid draft and no unfinished gesture. Download remains
disabled while loading/saving and after unresolved invalid/conflict/error states.
This is the existing product policy, not a backup guarantee after a failed save.
The temporary anchor is removed and its Blob URL is revoked after the click.

## Geometry, readable defaults and performance

The lock-screen board uses logical 361 × 160 points. Resizing the viewport scales
the preview; it does not change stored geometry or automatically fit text.
SVG text is clipped to its element box. A narrow shell that has no horizontal
overflow can still show clipped text. For the synthetic four-row summary, the
proved recipe sets each row to X14, W330, H24 with Y14/46/78/110, leaving room for
the sample values. Check actual glyph bounds and the screenshot after changing
text, font, scale or geometry. Increase the box or reduce scale when needed.

Preview paints prefer defined semantic palette roles. If a role is absent, the
renderer falls back to host foreground, then host ink, then current color. The
host must provide a readable foreground/background pairing. This does not
rewrite a saved `slate` color to `ink`, nor replace an explicitly defined role.

Each format supports at most 32 elements. Group a pointer gesture into one
history transaction; Undo and Redo restore exact geometry and clear inspector
selection. Read rendered element geometry when testing history rather than
assuming selection persists. Performance receipts must name their viewport,
element count and method. Browser automation wall time is not frame latency.

## Reusable verification

The browser regression guide is `docs/lab/WIDGET-REGRESSION-REPLAY.md` and its
suite is `web/e2e/widgets/widget-journey.spec.mjs`. It uses the real production-built
editor/client with a synthetic document API. It covers pointer history, actual
download/save/reload, project isolation and explicit 409/503 recovery. The
focused `saveReceipt.test.jsx` adds malformed-acknowledgement discrimination;
`designer.test.jsx` and `widgets.test.jsx` retain the wider component contracts.

Keep these evidence levels separate: isolated browser with fixture API; real
service integration with server authorization/durability; and delivered source
with rendered host behavior. A passing fixture suite proves only its first
level. Use normal project creation and visible controls for live readiness,
synthetic data, exact source binding and a disposable project. Close owned
browser sessions, preserve exports/screenshots and leave a distilled replay.
Accepted benchmark evidence is immutable and is not replaced by this guide.
