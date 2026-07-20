// DT2 right drawer (crib §3): every quiet "Details" opens this panel sliding
// over the events rail — hairline left edge + deep shadow, title + Esc cap
// header, provenance in mono, ONE quiet action. Rendered at App level inside
// .drawer-layer (structural.css) so the rail behind never re-flows. Esc closes
// (App's global key ladder) and the header cap mirrors it.

export default function DetailsDrawer({ data, onClose }) {
  if (!data) return null
  const { title, rows = [], action, foot } = data
  return (
    <div className="drawer-layer">
      <aside className="drawer enter" role="dialog" aria-label={title}>
        <div className="drawer-head">
          <span className="drawer-title">{title}</span>
          <button type="button" className="key hot" onClick={onClose} aria-label="Close details">
            Esc
          </button>
        </div>
        <div className="drawer-body">
          {rows.map((r, i) => (
            <div className="drawer-mono" key={i}>{r}</div>
          ))}
          {action && (
            <button type="button" className="chip-act drawer-act" onClick={action.onClick}>
              {action.label}
            </button>
          )}
        </div>
        {foot && <div className="drawer-foot">{foot}</div>}
      </aside>
    </div>
  )
}
