// DT2 right drawer (crib §3): every quiet "Details" opens this panel sliding
// over the events rail — hairline left edge + deep shadow, title + Esc cap
// header, provenance in mono, ONE quiet action. Rendered at App level inside
// .drawer-layer (structural.css) so the rail behind never re-flows. Esc closes
// (App's global key ladder) and the header cap mirrors it.

import useExit from '../useExit.js'

export default function DetailsDrawer({ data, onClose }) {
  // M1 exit: hold the last payload through the 180 ms .exit fade (useExit
  // follows Toast.jsx's pattern), then unmount.
  const { shown, exiting } = useExit(data)
  if (!shown) return null
  const { title, rows = [], action, foot } = shown
  return (
    <div className="drawer-layer">
      <aside className={`drawer ${exiting ? 'exit' : 'enter'}`} role="dialog" aria-label={title}>
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
