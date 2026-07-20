import { useEffect } from 'react'
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

function fmtWhen(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const mins = Math.round((Date.now() - d.getTime()) / 60000)
  if (mins >= 0 && mins < 60) return `${mins} m`
  if (mins >= 0 && mins < 1440) return `${Math.round(mins / 60)} h`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function fmtAbs(iso) {
  if (!iso) return undefined
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export default function VersionHistory({
  data, error, loading, previewingVersion, onPreview, onBackToHead, onClose, onRetry,
}) {
  const head = data?.head
  const latest = data?.latest
  const rows = [...(data?.versions || [])].sort((a, b) => (b.v || 0) - (a.v || 0))

  // Esc closes — the header cap is the affordance, the key must actually work.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !document.querySelector('.drawer-layer .drawer')) onClose() } // an open drawer owns Esc
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  // M-e: a bare `.drawer` has no positioning owner, so it injected a 300px
  // column into the toolbar. `.drawer-fixed` (styles.css) anchors it as a
  // floating right panel below the header / above the footer instead.
  return (
    <div className="drawer drawer-fixed" role="dialog" aria-label="Version history">
      <div className="drawer-head">
        <span className="drawer-title">Version history{data ? ` · ${rows.length}` : ''}</span>
        <button className="key hot" onClick={onClose} aria-label="Close version history">Esc</button>
      </div>

      <div className="drawer-body">
        {previewingVersion != null && (
          <div className="vh-previewing">
            <span>Viewing v{previewingVersion}{latest != null ? ` of ${latest}` : ''} — read-only preview</span>
            <button className="chip-act" onClick={onBackToHead}>Back to head</button>
          </div>
        )}

        {loading && (
          <div className="skeleton-stack" aria-label="Loading history">
            <div className="skeleton-row" />
            <div className="skeleton-row" />
            <div className="skeleton-row" />
          </div>
        )}

        {error && !loading && (
          <div className="pane-fail" role="alert">
            <span className="pane-fail-title"><span className="dot red" />Couldn’t load versions</span>
            <span className="pane-fail-reason">{error}</span>
            {onRetry && <button className="chip-act" onClick={onRetry}>Retry</button>}
          </div>
        )}

        {!loading && !error && rows.length === 0 && (
          <div className="vh-note">No versions yet.</div>
        )}

        {!loading && !error && rows.length > 0 && (
          <ul className="vh-list">
            {rows.map((r) => {
              const isHead = r.v === head
              const isPreview = r.v === previewingVersion
              return (
                <li key={r.v}>
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
                      </span>
                      <span className="vh-row-sub">
                        {r.note && <span className="vh-note-txt">{r.note}</span>}
                        {r.sha256 && <span className="drawer-mono">{String(r.sha256).slice(0, 12)}</span>}
                        <span className="vh-when">{fmtWhen(r.created)}</span>
                      </span>
                    </span>
                    {isPreview && <span className="key hot">Enter</span>}
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
