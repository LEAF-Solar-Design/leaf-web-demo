// DT2 right drawer (crib §3): every quiet "Details" opens this panel sliding
// over the events rail — hairline left edge + deep shadow, title + Esc cap
// header, provenance in mono, ONE quiet action. Rendered at App level inside
// .drawer-layer (structural.css) so the rail behind never re-flows. Esc closes
// (App's global key ladder) and the header cap mirrors it.

import { useEffect, useRef } from 'react'
import useExit from '../useExit.js'

export default function DetailsDrawer({ data, onClose }) {
  // M1 exit: hold the last payload through the 180 ms .exit fade (useExit
  // follows Toast.jsx's pattern), then unmount.
  const { shown, exiting } = useExit(data)
  const drawerRef = useRef(null)
  const closeRef = useRef(null)
  const restoreRef = useRef(null)
  const open = !!data

  useEffect(() => {
    if (!open) return undefined
    restoreRef.current = document.activeElement
    closeRef.current?.focus()
    return () => {
      const target = restoreRef.current
      if (target?.isConnected && typeof target.focus === 'function') target.focus()
    }
  }, [open])

  const ownKeyboard = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      onClose?.()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = [...(drawerRef.current?.querySelectorAll('button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])') || [])]
      .filter((element) => element.getClientRects().length > 0)
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (focusable.length === 1 || (!event.shiftKey && document.activeElement === last)) {
      event.preventDefault()
      first.focus()
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    }
  }
  if (!shown) return null
  const { title, rows = [], action, foot } = shown
  return (
    <div className="drawer-layer">
      <aside ref={drawerRef} className={`drawer ${exiting ? 'exit' : 'enter'}`} role="dialog" aria-modal="true" aria-label={title} onKeyDown={ownKeyboard}>
        <div className="drawer-head">
          <span className="drawer-title">{title}</span>
          <button ref={closeRef} type="button" className="key hot" onClick={onClose} aria-label="Close details">
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
