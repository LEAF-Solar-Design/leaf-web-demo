import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { TOUR_STEPS } from './tourScript.js'
import './demo.css'

// M5 — the ?demo=tour coach-mark overlay.
//
// This component NEVER produces a result. For a beat with a canned prompt it
// calls onCannedPrompt(text, step); App feeds that straight into its existing
// nl-prompt -> setRoute -> onRun path, so what the audience sees is the real
// router and the real mock engine on the real drawing. The tour's only job is
// spotlighting, narrating, and pacing.
//
// Pacing rule: for a beat that has an effect (a prompt, an author beat, a
// version beat), Next stays disabled until App reports the effect landed via
// the `landed` prop. We never advance past something the app has not done.

const MARGIN = 14

function resolveTarget(selector) {
  if (!selector) return null
  for (const part of String(selector).split(',')) {
    const el = document.querySelector(part.trim())
    if (el) return el
  }
  return null
}

export default function DemoTour({
  steps = TOUR_STEPS,
  index: controlledIndex,
  onIndexChange,
  onCannedPrompt,
  onExit,
  landed = false,
  busy = false,
}) {
  const [uncontrolled, setUncontrolled] = useState(0)
  const index = typeof controlledIndex === 'number' ? controlledIndex : uncontrolled
  const step = steps[Math.max(0, Math.min(index, steps.length - 1))]

  const setIndex = useCallback((next) => {
    const clamped = Math.max(0, Math.min(next, steps.length - 1))
    if (typeof controlledIndex !== 'number') setUncontrolled(clamped)
    if (onIndexChange) onIndexChange(clamped)
  }, [controlledIndex, onIndexChange, steps.length])

  // --- spotlight geometry --------------------------------------------------
  const [rect, setRect] = useState(null)
  const measure = useCallback(() => {
    const el = resolveTarget(step?.target)
    const next = (() => {
      if (!el) return null
      const r = el.getBoundingClientRect()
      if (!r.width || !r.height) return null
      return { top: r.top - 6, left: r.left - 6, width: r.width + 12, height: r.height + 12 }
    })()
    // Shallow-equal guard: a ResizeObserver/scroll tick that reports identical
    // geometry must not force a re-render (and a re-measure loop).
    setRect((prev) => {
      if (prev === next) return prev
      if (!prev || !next) return next
      if (prev.top === next.top && prev.left === next.left
        && prev.width === next.width && prev.height === next.height) return prev
      return next
    })
  }, [step])

  useLayoutEffect(() => {
    measure()
    const el = resolveTarget(step?.target)
    if (el && el.scrollIntoView) {
      try { el.scrollIntoView({ block: 'nearest', behavior: 'smooth' }) } catch { /* older browsers */ }
    }
    const id = setTimeout(measure, 320) // re-measure after the smooth scroll settles
    // A spotlit section can expand AFTER we measured it (the author section is
    // collapsed on arrival and grows to hold the authored tool + its code). Without
    // this the ring frames an empty header and the card lands on the payload.
    let ro
    if (el && typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => measure())
      ro.observe(el)
    }
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', measure, true)
    return () => {
      clearTimeout(id)
      if (ro) ro.disconnect()
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', measure, true)
    }
  }, [measure, step])

  // --- fire the canned prompt once per beat --------------------------------
  const firedFor = useRef(null)
  useEffect(() => {
    if (!step || !step.prompt) return
    // A run/route already in flight makes App's onDispatch early-return undefined,
    // which would silently produce nothing while the beat reports it landed.
    if (busy) return
    if (firedFor.current === step.id) return
    firedFor.current = step.id
    if (onCannedPrompt) onCannedPrompt(step.prompt, step)
  }, [step, onCannedPrompt, busy])

  // Local self-typing echo in the card (cosmetic only — App drives the real bar).
  const [typed, setTyped] = useState('')
  useEffect(() => {
    if (!step?.prompt) { setTyped(''); return }
    setTyped('')
    let i = 0
    const id = setInterval(() => {
      i += 1
      setTyped(step.prompt.slice(0, i))
      if (i >= step.prompt.length) clearInterval(id)
    }, 28)
    return () => clearInterval(id)
  }, [step])

  const needsEffect = !!(step && (step.prompt || step.action === 'author' || step.action === 'version'))
  const canAdvance = !busy && (!needsEffect || landed)
  const isLast = index >= steps.length - 1

  const next = useCallback(() => {
    if (isLast || step?.action === 'exit') { if (onExit) onExit(); return }
    setIndex(index + 1)
  }, [isLast, step, index, setIndex, onExit])

  // --- card geometry -------------------------------------------------------
  const rootRef = useRef(null)
  const restoreRef = useRef(null)
  const cardRef = useRef(null)
  const [cardH, setCardH] = useState(260)
  useLayoutEffect(() => {
    const el = cardRef.current
    if (!el) return
    const h = Math.round(el.getBoundingClientRect().height)
    if (h) setCardH((prev) => (prev === h ? prev : h))
  }, [step, landed, typed, needsEffect])

  // NOTE: the tour deliberately does NOT own Escape. A capture-phase Esc listener
  // here terminated the whole walkthrough when the user only meant to dismiss the
  // route card / History / a selection, with no way back in. Exit stays reachable
  // via the banner's "Exit — explore freely" and the card's "Skip"; Esc goes back
  // to App's own Esc ladder, which is what the rest of the app expects.

  // Focus starts inside the overlay so the first Tab reaches Exit/Skip/Next
  // instead of walking the whole app, and is restored on exit.
  useEffect(() => {
    restoreRef.current = document.activeElement
    rootRef.current?.focus()
    return () => {
      const el = restoreRef.current
      if (el && el.isConnected && typeof el.focus === 'function') el.focus()
    }
  }, [])

  if (!step) return null

  // Card placement: below -> above -> beside, always clamped inside the viewport
  // and never laid over the spotlight. The height is MEASURED (cardH) rather than
  // assumed — a prompt beat renders well past the 240px this used to guess, which
  // pushed the Back/Skip/Next row below the fold on a 768px laptop.
  const cardStyle = (() => {
    if (!rect) return { left: '50%', top: '50%', transform: 'translate(-50%, -50%)' }
    const H = cardH
    const W = 380
    const vh = window.innerHeight
    const vw = window.innerWidth
    const clampTop = (t) => Math.max(64, Math.min(t, Math.max(64, vh - H - 16)))
    const left = Math.max(16, Math.min(rect.left, vw - W - 16))
    const below = rect.top + rect.height + MARGIN
    if (below + H + 16 <= vh) return { left, top: below }
    if (rect.top - H - MARGIN >= 64) return { left, top: rect.top - H - MARGIN }
    const right = rect.left + rect.width + MARGIN
    if (right + W + 16 <= vw) return { left: right, top: clampTop(rect.top) }
    if (rect.left - W - MARGIN >= 16) return { left: rect.left - W - MARGIN, top: clampTop(rect.top) }
    return { left, top: clampTop(below) }
  })()

  return (
    <div
      ref={rootRef}
      tabIndex={-1}
      className="tour-root"
      role="dialog"
      aria-modal="false"
      aria-label="Guided demo tour"
    >
      {rect
        ? <div className="tour-spot" style={rect} />
        : <div className="tour-dim" />}

      <div className="tour-banner">
        <span className="tour-banner-title">Guided demo — sample rooftop</span>
        <span className="tour-banner-sub">
          Real drawing, real tools — every number below is computed live.
        </span>
        <button type="button" className="chip-neutral tour-banner-exit" onClick={onExit}>
          Exit — explore freely
        </button>
      </div>

      <div ref={cardRef} className={`tour-card${rect ? '' : ' is-centered'}`} style={cardStyle}>
        <div className="tour-card-step">Step {index + 1} of {steps.length}</div>
        <div className="tour-card-title">{step.title}</div>
        <p className="tour-card-body">{step.body}</p>

        {step.prompt && (
          <div className="tour-card-prompt">
            {typed}<span className="tour-caret">▌</span>
          </div>
        )}

        {/* Kept permanently mounted so the live region pre-exists the unlock —
            a region that mounts with its text is not reliably announced. */}
        <div className="tour-card-wait" role="status" aria-live="polite">
          {needsEffect && !landed ? 'Running it for real — Next unlocks when the result lands.' : ''}
        </div>

        <div className="tour-dots" aria-hidden="true">
          {steps.map((s, i) => <span key={s.id} className={`tour-dot${i <= index ? ' on' : ''}`} />)}
        </div>

        <div className="tour-card-actions">
          <button
            type="button"
            className="chip-neutral"
            onClick={() => setIndex(index - 1)}
            disabled={index === 0}
          >
            Back
          </button>
          <button type="button" className="chip-neutral" onClick={onExit}>Skip</button>
          <span className="tour-spacer" />
          <button
            type="button"
            className="primary"
            onClick={next}
            disabled={!canAdvance}
          >
            {isLast || step.action === 'exit' ? 'Exit — explore freely' : 'Next'}
          </button>
        </div>
      </div>
    </div>
  )
}
