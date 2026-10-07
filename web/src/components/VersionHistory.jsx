import { useEffect, useRef, useState } from 'react'
import EscCap from './EscCap.jsx'
import useEscapeOwner from '../lib/useEscapeOwner.js'
import { config, getDrawingVersions, restoreDrawingVersion } from '../api.js'
import VersionList, { VersionPreviewStrip } from './VersionList.jsx'
import { relativeTime } from '../lib/railTime.js'
import useRelativeNow from '../lib/useRelativeNow.js'
import './popovers.css'

// Version-history browser: a DT2 right drawer (title + Esc cap header) listing
// the drawing's version chain (GET /api/drawings/{id}/versions) newest first.
// Rows follow the O2 resolver anatomy — 2px accent left bar + tint on the
// active (previewed) row, Enter cap — with mono reserved for the version seq,
// the tool slug, and the sha256 prefix (provenance). Clicking a version is a
// READ-ONLY PREVIEW (the parent fetches that version's intake and seats it in
// the viewer); it never mutates head. While previewing, a "Viewing vN of M —
// back to head" strip restores the head intake. Esc (key or cap) closes.
//
// TM1 time: relative under a day ("2 m", "2 h"), "Jul 12" after; the absolute
// clock rides the row's hover title.
//
// STANDARDIZATION SLICE 6a: this file is now the drawer CHROME only — the
// header, the Esc contract, the loading/error/empty states, the drawer-level
// unreadable-head warning, and the drawer's own time presentation. The rows
// themselves (ordering, delta chip, authored-tool provenance chip, the
// two-step restore confirm state machine, the preview strip) come from the ONE
// primitive, components/VersionList.jsx, which /try's version tab renders too.
// Every testid and class the drawer shipped before is unchanged, and
// versionList.test.jsx pins the rendered element sequence byte for byte
// against a capture taken from the pre-slice component.
//
// ruling-4 (lane S5): each non-root row carries a compact delta chip
// (+added ~modified -deleted, computed server-side at read time — see
// server/routers/drawings.py; hidden when `delta` is null: the root version
// or an unparseable payload on either side of the diff). Every non-head row
// also gets a "Restore" action (two-step confirm) that composes the
// POST .../versions/{v}/restore endpoint: it appends a NEW head whose content
// equals that version's, so history is never rewritten.
//
// INTEGRATION: App.jsx's call site passes `mock`, `capability`, and
// `onRestored` (wired to the version controller's refreshHead). The
// `config.mockDefault` / no-capability fallbacks below remain only for a
// caller that omits the props (none in-tree today).
//
// S23 agent checkpoints: App also passes `onUndo` (the ribbon's Undo) and
// `undoDisabled`. An agent-made head row (VersionList's agent-turn marker)
// then shows one Rewind beside .vh-restore that runs exactly that Undo.

// E2 empty state: the action chip focuses the composer directly (the docked
// bar's input, falling back to the legacy prompt textarea) — no parent wiring
// needed, same move as JobRail's empty state.
function focusComposer() {
  const el = document.querySelector('.bar textarea') || document.querySelector('.bar input')
    || document.querySelector('.prompt textarea')
  if (el) el.focus()
}

function fmtAbs(iso) {
  if (!iso) return undefined
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

// `retryKey`: App's R ladder marks this panel's error Retry as its active rung
// (the keycap only renders while R genuinely fires it). `exiting`: the parent
// holds the mount through the 180 ms M1 exit fade (useExit).
export default function VersionHistory({
  data, error, loading, previewingVersion, onPreview, onBackToHead, onClose, onRetry,
  retryKey, exiting, mock, capability, onRestored, headWarning, mutationBlocked = false,
  onBeforeRestore = null, onUndo = null, undoDisabled = false,
}) {
  const now = useRelativeNow()
  // Self-contained restore state (see the integration note above for why).
  // The CONFIRM/PENDING/ERROR machine lives in VersionList; what stays here is
  // the drawer's own best-effort refresh of the chain after a commit.
  const [overrideData, setOverrideData] = useState(null)
  // The controller owns committed-head warnings because this drawer can close.

  // A fresh `data` prop (a real reload) always wins over our own best-effort
  // local refresh.
  useEffect(() => {
    setOverrideData(null)
  }, [data])

  const effective = overrideData || data
  const head = effective?.head
  const latest = effective?.latest
  const drawingId = effective?.drawing_id
  const rows = effective?.versions || []
  const useMock = mock ?? config.mockDefault
  const recoveryRestoreAllowed = Boolean(headWarning && !headWarning.pending)
  const restoreBlocked = mutationBlocked && !recoveryRestoreAllowed

  // Esc closes — the header cap is the affordance, the key must actually work.
  // S27: through the owner stack at the history layer, under the drawers (the
  // old "an open drawer owns Esc" check), and not while the exit fade holds the
  // mount. The root keeps its data-escape-owner marker for the command-line
  // armer; the stack knows the marker is its own through `scope`.
  const rootRef = useRef(null)
  useEscapeOwner('version-history', !exiting, () => onClose?.(), { layer: 'history', scope: rootRef })

  // The EFFECT of a restore; VersionList owns the confirm/pending/error UX and
  // rethrows nothing it did not receive, so a failure here surfaces on the row
  // exactly as it did before.
  async function doRestore(v) {
    if (drawingId == null || restoreBlocked) return
    onBeforeRestore?.()
    const result = await restoreDrawingVersion(useMock, drawingId, v, capability)
    // The restore is already committed. Record its new head before any
    // best-effort history refresh, which can fail or remain pending. This
    // makes an unreadable head lock edits at the first committed response.
    await onRestored?.(result)
    if (onRetry) {
      // The real integration path: the parent's own history state refreshes,
      // and `data` (a fresh prop) will clear `overrideData` above.
      await onRetry()
    } else {
      // No parent refresh wired — refetch ourselves so the new head shows up.
      try { setOverrideData(await getDrawingVersions(useMock, drawingId, { includeDeltas: true })) }
      catch { /* the restore itself already succeeded; the list just won't advance */ }
    }
  }

  // M-e: a bare `.drawer` has no positioning owner, so it injected a 300px
  // column into the toolbar. `.drawer-fixed` (styles.css) anchors it as a
  // floating right panel below the header / above the footer instead.
  return (
    <div ref={rootRef} className={`drawer drawer-fixed${exiting ? ' exit' : ''}`} role="dialog" aria-label="Version history" data-escape-owner>
      <div className="drawer-head">
        <span className="drawer-title">Version history{effective ? ` · ${rows.length}` : ''}</span>
        <EscCap onClick={onClose} label="Close version history" />
      </div>

      <div className="drawer-body">
        {headWarning && (
          // Drawer-level, not row-level: the refreshed history re-renders the
          // rows, and this warning must outlive that refresh (see headWarning
          // above). role=alert so the state change is announced.
          <div className="field-err vh-restore-err" role="alert" data-testid="vh-head-warning">
            {headWarning.message}
          </div>
        )}
        <VersionPreviewStrip
          variant="drawer"
          version={previewingVersion}
          latest={latest}
          onBackToHead={onBackToHead}
        />

        {loading && (
          <div className="skeleton-stack vh-skeleton" aria-label="Loading history">
            <div className="skeleton-row" />
            <div className="skeleton-row" />
            <div className="skeleton-row" />
          </div>
        )}

        {error && !loading && (
          <div className="pane-fail" role="alert">
            <span className="pane-fail-title"><span className="dot red" />Couldn’t load versions</span>
            <span className="pane-fail-reason">{error}</span>
            {onRetry && (
              <span className="pane-fail-act">
                <button className="chip-act" onClick={onRetry}>Retry</button>
                {retryKey && <span className="key" aria-hidden="true">R</span>}
              </span>
            )}
          </div>
        )}

        {!loading && !error && rows.length === 0 && (
          <div className="rail-empty">
            <div className="vh-note">Versions land here after your first edit runs.</div>
            <button className="chip-act" onClick={() => { onClose(); focusComposer() }}>Run an edit</button>
          </div>
        )}

        {!loading && !error && rows.length > 0 && (
          <VersionList
            variant="drawer"
            versions={rows}
            head={head}
            previewingVersion={previewingVersion}
            onPreview={onPreview}
            rowTitle={(r) => fmtAbs(r.created)}
            rowSub={(r) => (
              <span className="vh-row-sub">
                {r.note && <span className="vh-note-txt">{r.note}</span>}
                {r.sha256 && <span className="drawer-mono">{String(r.sha256).slice(0, 12)}</span>}
                <span className="vh-when">{relativeTime(Date.parse(r.created), now)}</span>
              </span>
            )}
            restore={{
              run: doRestore,
              mode: recoveryRestoreAllowed ? 'recover' : 'restore',
              // Every non-head row, exactly as before: restoring the head onto
              // itself is a no-op-shaped action the UI skips.
              eligible: (_row, isHead) => !isHead,
              disabled: restoreBlocked,
            }}
            rewind={onUndo ? {
              // S23: an agent-made head gets one Rewind, which is the ribbon's
              // own Undo (App's onUndo), held to the same blocks the ribbon's
              // Undo button honours. No onUndo, no Rewind.
              run: () => onUndo(),
              disabled: Boolean(undoDisabled || mutationBlocked || previewingVersion != null),
            } : null}
          />
        )}
      </div>
    </div>
  )
}
