/**
 * OperatorEntry — the ONE thing App.jsx mounts (acceptance #6). Probes
 * GET /api/operator/sessions exactly once on mount (operatorClient.
 * probeOperatorConsole is itself a singleton cache, so even a StrictMode
 * double-mount issues one request); a 404/401/503 hides the entry with no
 * error surfaced to the tenant-facing app, and no further probing is
 * attempted for the lifetime of the page.
 */
import { useEffect, useRef, useState } from 'react'

import { probeOperatorConsole } from '../../operatorClient.js'
import OperatorConsole from './OperatorConsole.jsx'
import './operator.css'

export default function OperatorEntry() {
  const [visible, setVisible] = useState(false)
  const [open, setOpen] = useState(false)
  const openButtonRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    probeOperatorConsole().then((enabled) => { if (!cancelled) setVisible(enabled) })
    return () => { cancelled = true }
  }, [])

  const close = () => {
    setOpen(false)
    // Dialog-close focus return (acceptance #2), one level up: closing the
    // console returns focus to the rail button that opened it.
    openButtonRef.current?.focus()
  }

  if (!visible) return null

  return (
    <>
      <button
        type="button"
        ref={openButtonRef}
        className="operator-entry-button"
        aria-label="Open operator console"
        onClick={() => setOpen(true)}
      >
        Operator
      </button>
      {open && <OperatorConsole onClose={close} />}
    </>
  )
}
