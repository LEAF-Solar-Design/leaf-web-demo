import { useEffect, useRef } from 'react'
import { keyboardTable } from '../lib/actionRegistry.js'

// Slice 10b: the shortcut sheet. GENERATED straight from the action
// registry's own `kbd` fields (keyboardTable()) — a cap added to the
// registry shows up here with no second list to keep in sync, and a cap
// this file invented would already fail actionRegistry's own honesty gate
// before it got here. Not a modal (the spec's own rule for this slice): an
// anchored panel, same `.resolver` anatomy as the bar's own menus, closed by
// its own Escape / outside-click pair rather than trapping focus.
export default function ShortcutSheet({ open, onClose }) {
  const rootRef = useRef(null)

  // Same pattern as PromptBox's scope-menu listener: capture-phase Escape so
  // the global ladder's own Escape rung never also fires, and an
  // outside-mousedown close so a click elsewhere dismisses it like every
  // other resolver on this bar.
  useEffect(() => {
    if (!open) return undefined
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) onClose?.() }
    const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); onClose?.() } }
    document.addEventListener('mousedown', onDoc)
    window.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      window.removeEventListener('keydown', onKey, true)
    }
  }, [open, onClose])

  if (!open) return null
  const rows = keyboardTable()

  return (
    <div className="resolver shortcut-sheet" role="dialog" aria-label="Keyboard shortcuts" ref={rootRef}>
      <div className="resolver-header">
        Keyboard shortcuts
        <button type="button" className="chip-neutral" onClick={onClose}>Close</button>
      </div>
      {rows.map((row) => (
        <div className="resolver-row" key={row.id} data-testid="shortcut-row">
          <span className="lbar" aria-hidden="true" />
          <span className="label">{row.label}</span>
          <span className="key hot">{row.kbd}</span>
        </div>
      ))}
      {/* Honest gap (slice 10b spec): sheets anchors and receipts have no
          index today. Named here rather than rendered as a row nothing
          backs. */}
      <div className="resolver-header">Sheet anchors and receipts: no shortcut index yet</div>
    </div>
  )
}
