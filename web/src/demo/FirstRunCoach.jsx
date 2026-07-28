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

// At <=980px landing.css hands the whole bottom half to the rails, so there
// is no placement for the card (coach.css hides it as a belt). This media
// gate is the suspenders: while it matches, the component renders null and
// installs NO document listeners -- otherwise an Escape or stray pointerdown
// would silently dismiss/hide a coach the user never saw, and resizing wider
// would reveal nothing.
const SMALL_VIEWPORT_QUERY = '(max-width: 980px)'

function useSmallViewport() {
  const [small, setSmall] = useState(() =>
    typeof window !== 'undefined' && window.matchMedia(SMALL_VIEWPORT_QUERY).matches)
  useEffect(() => {
    const mql = window.matchMedia(SMALL_VIEWPORT_QUERY)
    const onChange = (event) => setSmall(event.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [])
  return small
}

export default function FirstRunCoach({ signedIn = false, active = true }) {
  const [dismissed, setDismissed] = useState(readDismissed)
  const [sessionHidden, setSessionHidden] = useState(false)
  const smallViewport = useSmallViewport()

  const visible = active && !smallViewport && !sessionHidden && shouldOfferCoach({
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
    // The coach must never cost a click: the first pointerdown anywhere
    // outside the card hides it for this page view (without recording a
    // dismissal, so a genuinely fresh visitor still gets offered next load).
    const onPointerDown = (event) => {
      if (!(event.target instanceof Element) || !event.target.closest('.coach-root')) {
        setSessionHidden(true)
      }
    }
    window.addEventListener('keydown', onKey)
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => {
      window.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onPointerDown, true)
    }
  }, [visible, dismiss])

  if (!visible) return null

  return (
    <div
      className="coach-root"
      role="dialog"
      aria-label="Try the command bar"
      data-testid="first-run-coach"
      data-cast="tool"
      style={{ '--rank': 4 }}
    >
      <div className="coach-card">
        <div className="coach-title">Type a request, or use a keycap</div>
        <p className="coach-body">
          The command bar turns plain English into a reviewed drawing change. A few keys get you there faster.
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
