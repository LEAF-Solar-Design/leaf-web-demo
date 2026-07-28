import { useCallback, useEffect, useState } from 'react'
import { shouldOfferCoach } from './tourEntry.js'
import './coach.css'

// HP-01 — first-run coach mark.
//
// ADDITIVE ONLY. This never starts, gates, or reads state for the
// ?demo=tour walkthrough (tourEntry.js's pin: "the tour is a deep-link,
// never a default" stays untouched). This is a separate, dismiss-once hint
// for a fresh, signed-out visitor discovering the command bar and its
// keycaps on /try. An explicit `?demo=` param of any kind keeps absolute
// priority and suppresses this overlay entirely (see shouldOfferCoach).
const STORAGE_KEY = 'leaf.coach.dismissed.v1'

function readDismissed() {
  try {
    return globalThis.localStorage?.getItem(STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

function writeDismissed() {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, '1')
  } catch {
    /* storage unavailable (private mode, disabled) — the coach just re-offers next load */
  }
}

export default function FirstRunCoach({ signedIn = false, active = true }) {
  const [dismissed, setDismissed] = useState(readDismissed)

  const visible = active && shouldOfferCoach({
    search: typeof window !== 'undefined' ? window.location.search : '',
    dismissed,
    signedIn,
  })

  const dismiss = useCallback(() => {
    writeDismissed()
    setDismissed(true)
  }, [])

  useEffect(() => {
    if (!visible) return undefined
    const onKey = (event) => {
      if (event.key === 'Escape') dismiss()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [visible, dismiss])

  if (!visible) return null

  return (
    <div className="coach-root" role="dialog" aria-label="Try the command bar" data-testid="first-run-coach">
      <div className="coach-card">
        <div className="coach-title">Type a request, or use a keycap</div>
        <p className="coach-body">
          The command bar above turns plain English into a reviewed drawing change. A few keys get you there faster.
        </p>
        <div className="coach-keys">
          <span className="key hot" title="Focus the command bar">⌘K</span>
          <span className="coach-key-label">focus the bar</span>
          <span className="key hot" title="Dismiss a proposal or panel">Esc</span>
          <span className="coach-key-label">back out</span>
        </div>
        <div className="coach-actions">
          <button type="button" className="chip-act" onClick={dismiss} data-testid="first-run-coach-dismiss">
            Got it
          </button>
        </div>
      </div>
    </div>
  )
}
