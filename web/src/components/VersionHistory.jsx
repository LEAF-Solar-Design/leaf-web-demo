// Version-history browser: a calm popover off the viewer toolbar's version note.
// Lists the drawing's version chain (GET /api/drawings/{id}/versions) newest
// first — each row `v{n} · {tool} · {created}` with the note, head marked.
// Clicking a version is a READ-ONLY PREVIEW (the parent fetches that version's
// intake and seats it in the viewer); it never mutates head. While previewing, a
// "viewing vN of M — back to head" line restores the head intake.
//
// Stays inside the Unified Surface's calm vocabulary: no emoji, no rounded
// status pills, no spinners — square mono rows, a static word for state.

function fmtWhen(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export default function VersionHistory({
  data, error, loading, previewingVersion, onPreview, onBackToHead, onClose,
}) {
  const head = data?.head
  const latest = data?.latest
  const rows = [...(data?.versions || [])].sort((a, b) => (b.v || 0) - (a.v || 0))

  return (
    <div className="vh-pop" role="dialog" aria-label="Version history">
      <div className="vh-head">
        <span>Version history{data ? ` · ${rows.length}` : ''}</span>
        <button className="vh-x" onClick={onClose} aria-label="Close version history">close</button>
      </div>

      {previewingVersion != null && (
        <div className="vh-previewing">
          <span>viewing v{previewingVersion}{latest != null ? ` of ${latest}` : ''} — read-only preview</span>
          <button className="btn ghost vh-back" onClick={onBackToHead}>Back to head</button>
        </div>
      )}

      {loading && <div className="vh-note">Loading history…</div>}
      {error && <div className="vh-note vh-err">History unavailable: {error}</div>}

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
                  className={`vh-row ${isPreview ? 'active' : ''}`}
                  onClick={() => onPreview(r.v)}
                  title={r.sha256 ? `sha256 ${String(r.sha256).slice(0, 12)}…` : undefined}
                >
                  <div className="vh-row-top">
                    <span className="vh-v">v{r.v}</span>
                    {r.tool && <span className="vh-tool">{r.tool}</span>}
                    {isHead && <span className="vh-mark">head</span>}
                  </div>
                  <div className="vh-row-sub">
                    {r.note && <span className="vh-note-txt">{r.note}</span>}
                    <span className="vh-when">{fmtWhen(r.created)}</span>
                  </div>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
