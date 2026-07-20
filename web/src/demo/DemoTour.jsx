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
    if (!el) { setRect(null); return }
    const r = el.getBoundingClientRect()
    if (!r.width || !r.height) { setRect(null); return }
    setRect({ top: r.top - 6, left: r.left - 6, width: r.width + 12, height: r.height + 12 })
  }, [step])

  useLayoutEffect(() => {
    measure()
    const el = resolveTarget(step?.target)
    if (el && el.scrollIntoView) {
      try { el.scrollIntoView({ block: 'nearest', behavior: 'smooth' }) } catch { /* older browsers */ }
    }
    const id = setTimeout(measure, 320) // re-measure after the smooth scroll settles
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', measure, true)
    return () => {
      clearTimeout(id)
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', measure, true)
    }
  }, [measure, step])

  // --- fire the canned prompt once per beat --------------------------------
  const firedFor = useRef(null)
  useEffect(() => {
    if (!step || !step.prompt) return
    if (firedFor.current === step.id) return
    firedFor.current = step.id
    if (onCannedPrompt) onCannedPrompt(step.prompt, step)
  }, [step, onCannedPrompt])

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

  // Esc leaves the tour (the app's own Esc ladder resumes once we are gone).
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); if (onExit) onExit() }
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [onExit])

  if (!step) return null

  // Card sits under the spotlight when there is room, else above it, else centered.
  const cardStyle = (() => {
    if (!rect) return { left: '50%', top: '50%', transform: 'translate(-50%, -50%)' }
    const below = rect.top + rect.height + MARGIN
    const left = Math.max(16, Math.min(rect.left, window.innerWidth - 400))
    if (below + 240 < window.innerHeight) return { left, top: below }
    return { left, top: Math.max(64, rect.top - 240 - MARGIN) }
  })()

  return (
    <div className="tour-root" role="dialog" aria-modal="false" aria-label="Guided demo tour">
      {rect
        ? <div className="tour-spot" style={rect} />
        : <div className="tour-dim" />}

      <div className="tour-banner">
        <span className="tour-banner-title">Guided demo — sample rooftop</span>
        <span className="tour-banner-sub">
          Real drawing, real tools — every number below is computed live.
        </span>
        <button type="button" className="tour-btn quiet tour-banner-exit" onClick={onExit}>
          Exit — explore freely
        </button>
      </div>

      <div className="tour-card" style={cardStyle}>
        <div className="tour-card-step">Step {index + 1} of {steps.length}</div>
        <div className="tour-card-title">{step.title}</div>
        <p className="tour-card-body">{step.body}</p>

        {step.prompt && (
          <div className="tour-card-prompt">
            {typed}<span className="tour-caret">▌</span>
          </div>
        )}

        {needsEffect && !landed && (
          <div className="tour-card-wait">Running it for real — Next unlocks when the result lands.</div>
        )}

        <div className="tour-dots" aria-hidden="true">
          {steps.map((s, i) => <span key={s.id} className={`tour-dot${i <= index ? ' on' : ''}`} />)}
        </div>

        <div className="tour-card-actions">
          <button
            type="button"
            className="tour-btn"
            onClick={() => setIndex(index - 1)}
            disabled={index === 0}
          >
            Back
          </button>
          <button type="button" className="tour-btn quiet" onClick={onExit}>Skip</button>
          <span className="tour-spacer" />
          <button
            type="button"
            className="tour-btn primary"
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
