import { useEffect, useState } from 'react'
import { config, getDrawingVersions, restoreDrawingVersion } from '../api.js'
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
// ruling-4 (lane S5): each non-root row carries a compact delta chip
// (+added ~modified -deleted, computed server-side at read time — see
// server/routers/drawings.py; hidden when `delta` is null: the root version
// or an unparseable payload on either side of the diff). Every non-head row
// also gets a "Restore" action (two-step confirm) that composes the NEW
// POST .../versions/{v}/restore endpoint: it appends a NEW head whose content
// equals that version's, so history is never rewritten.
//
// INTEGRATION: App.jsx's call site passes `mock`, `capability`, and
// `onRestored` (wired to the version controller's refreshHead). The
// `config.mockDefault` / no-capability fallbacks below remain only for a
// caller that omits the props (none in-tree today).

function fmtWhen(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const mins = Math.round((Date.now() - d.getTime()) / 60000)
  if (mins >= 0 && mins < 60) return `${mins} m`
  if (mins >= 0 && mins < 1440) return `${Math.round(mins / 60)} h`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

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

// Compact "+a ~m -d" delta chip. Null/undefined `delta` (root version, or a
// payload either side of the diff failed to parse) renders nothing — the row
// simply carries no chip, per the design note (ship what IS derivable).
function DeltaChip({ delta }) {
  if (!delta) return null
  const { added = 0, modified = 0, deleted = 0 } = delta
  if (!added && !modified && !deleted) {
    return <span className="vh-delta vh-delta-none" data-testid="vh-delta" title="No entity changes from the parent version">±0</span>
  }
  return (
    <span
      className="vh-delta"
      data-testid="vh-delta"
      title={`${added} added · ${modified} modified · ${deleted} removed (vs. the parent version)`}
    >
      {added > 0 && <span className="vh-delta-add">+{added}</span>}
      {modified > 0 && <span className="vh-delta-mod">~{modified}</span>}
      {deleted > 0 && <span className="vh-delta-del">-{deleted}</span>}
    </span>
  )
}

// `retryKey`: App's R ladder marks this panel's error Retry as its active rung
// (the keycap only renders while R genuinely fires it). `exiting`: the parent
// holds the mount through the 180 ms M1 exit fade (useExit).
export default function VersionHistory({
  data, error, loading, previewingVersion, onPreview, onBackToHead, onClose, onRetry,
  retryKey, exiting, mock, capability, onRestored, headWarning, mutationBlocked = false,
}) {
  // Self-contained restore state (see the integration note above for why).
  const [overrideData, setOverrideData] = useState(null)
  const [confirmingVersion, setConfirmingVersion] = useState(null)
  const [restoringVersion, setRestoringVersion] = useState(null)
  const [restoreErr, setRestoreErr] = useState(null) // {version, message} | null
  // The controller owns committed-head warnings because this drawer can close.

  // A fresh `data` prop (a real reload) always wins over our own best-effort
  // local refresh, and closes out any stale confirm/error state.
  useEffect(() => {
    setOverrideData(null)
    setConfirmingVersion(null)
    setRestoreErr(null)
  }, [data])

  const effective = overrideData || data
  const head = effective?.head
  const latest = effective?.latest
  const drawingId = effective?.drawing_id
  const rows = [...(effective?.versions || [])].sort((a, b) => (b.v || 0) - (a.v || 0))
  const useMock = mock ?? config.mockDefault
  const recoveryRestoreAllowed = Boolean(headWarning && !headWarning.pending)
  const restoreBlocked = mutationBlocked && !recoveryRestoreAllowed

  // Esc closes — the header cap is the affordance, the key must actually work.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !document.querySelector('.drawer-layer .drawer')) onClose() } // an open drawer owns Esc
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  async function doRestore(v) {
    if (drawingId == null || restoringVersion != null || restoreBlocked) return
    setRestoringVersion(v)
    setRestoreErr(null)
    try {
      const result = await restoreDrawingVersion(useMock, drawingId, v, capability)
      setConfirmingVersion(null)
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
    } catch (e) {
      setRestoreErr({ version: v, message: e?.message || 'Restore failed.' })
    } finally {
      setRestoringVersion(null)
    }
  }

  // M-e: a bare `.drawer` has no positioning owner, so it injected a 300px
  // column into the toolbar. `.drawer-fixed` (styles.css) anchors it as a
  // floating right panel below the header / above the footer instead.
  return (
    <div className={`drawer drawer-fixed${exiting ? ' exit' : ''}`} role="dialog" aria-label="Version history">
      <div className="drawer-head">
        <span className="drawer-title">Version history{effective ? ` · ${rows.length}` : ''}</span>
        <button className="key hot" onClick={onClose} aria-label="Close version history">Esc</button>
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
        {previewingVersion != null && (
          <div className="vh-previewing">
            <span>Viewing v{previewingVersion}{latest != null ? ` of ${latest}` : ''} — read-only preview</span>
            <button className="chip-act" onClick={onBackToHead}>Back to head</button>
          </div>
        )}

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
          <ul className="vh-list">
            {rows.map((r) => {
              const isHead = r.v === head
              const isPreview = r.v === previewingVersion
              const isConfirming = confirmingVersion === r.v
              const isRestoring = restoringVersion === r.v
              return (
                <li key={r.v} data-testid={`vh-row-v${r.v}`}>
                  <div className="vh-row-line">
                    <button
                      className={`vh-row${isPreview ? ' active' : ''}`}
                      onClick={() => onPreview(r.v)}
                      title={fmtAbs(r.created)}
                    >
                      <span className="lbar" />
                      <span className="vh-main">
                        <span className="vh-row-top">
                          <span className="vh-v">v{r.v}</span>
                          {r.tool && <span className="vh-tool">{r.tool}</span>}
                          {isHead && <span className="vh-mark">head</span>}
                          <DeltaChip delta={r.delta} />
                        </span>
                        <span className="vh-row-sub">
                          {r.note && <span className="vh-note-txt">{r.note}</span>}
                          {r.sha256 && <span className="drawer-mono">{String(r.sha256).slice(0, 12)}</span>}
                          <span className="vh-when">{fmtWhen(r.created)}</span>
                        </span>
                      </span>
                      {isPreview && <span className="key hot">Enter</span>}
                    </button>

                    {/* Restoring the current head onto itself is a no-op-shaped
                        action the server would still accept; the UI skips it. */}
                    {!isHead && (
                      <span className="vh-restore" aria-label={`Restore version ${r.v}`}>
                        {isConfirming ? (
                          <>
                            <span className="confirm-q">Restore v{r.v} as the new head?</span>
                            <button
                              className="chip-act"
                              disabled={isRestoring || restoreBlocked}
                              onClick={() => doRestore(r.v)}
                            >
                              {isRestoring
                                ? (recoveryRestoreAllowed ? 'Recovering…' : 'Restoring…')
                                : `${recoveryRestoreAllowed ? 'Recover from' : 'Restore'} v${r.v}`}
                            </button>
                            <button
                              className="chip-neutral"
                              disabled={isRestoring}
                              onClick={() => setConfirmingVersion(null)}
                            >
                              Cancel
                            </button>
                          </>
                        ) : (
                          <button
                            className="chip-act"
                            disabled={restoringVersion != null || restoreBlocked}
                            onClick={() => { setRestoreErr(null); setConfirmingVersion(r.v) }}
                          >
                            {recoveryRestoreAllowed ? 'Recover' : 'Restore'}
                          </button>
                        )}
                      </span>
                    )}
                  </div>
                  {restoreErr?.version === r.v && (
                    <div className="field-err vh-restore-err" role="alert">{restoreErr.message}</div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
