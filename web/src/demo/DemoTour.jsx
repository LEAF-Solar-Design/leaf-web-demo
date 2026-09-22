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

// Phone width (lane 3, one-shell narrow): at or under this width the card is
// a compact coach inside the drawing area, its bottom edge resting on the
// top of the fixed bottom chrome, never over the drawers, the command line
// or the status bar, and never taller than NARROW_MAX_VH of the viewport.
// The reserve stands in when no bottom chrome is laid out (status bar 46px
// + command line at bottom 50px + a gap), see cockpit.css.
const NARROW_MAX = 600
const NARROW_BOTTOM_RESERVE = 110
const NARROW_INSET = 8
const NARROW_MAX_VH = 0.4

// Folded panel headings sit just above the command bar. An open drawer
// raises the coach so its controls never cover the drawer's content.
const NARROW_CHROME = '.studio-drawer-tabs, .rail-stack, aside.nav, .bar-dock, footer.foot-bar, .app[data-drawer="result"] .result-block, .app[data-drawer="plan"] .ent-panel, .app[data-drawer="plan"] .studio-profile-info'
const NARROW_CHROME_MIN_TOP = 0.4

function readNarrow() {
  return typeof window !== 'undefined' && window.innerWidth <= NARROW_MAX
}

// The top edge of the highest bottom-chrome box, or null when none is laid
// out (jsdom, or a page without the cockpit's drawers).
function readChromeTop() {
  if (typeof document === 'undefined') return null
  const vh = window.innerHeight
  let top = null
  for (const el of document.querySelectorAll(NARROW_CHROME)) {
    const r = el.getBoundingClientRect()
    const studio = el.closest('.app[data-studio-shell="cockpit"]')
    if (!r.height || r.top < (studio ? 0 : vh * NARROW_CHROME_MIN_TOP) || r.top >= vh) continue
    if (top === null || r.top < top) top = r.top
  }
  return top
}

// Tracks the phone breakpoint. Resize is the source of truth (jsdom has it);
// a matchMedia change listener is added when the API exists, so a browser
// that resizes without a resize event (split view) still flips the coach.
function useNarrowViewport() {
  const [narrow, setNarrow] = useState(readNarrow)
  useEffect(() => {
    const update = () => setNarrow(readNarrow())
    window.addEventListener('resize', update)
    let mql = null
    if (typeof window.matchMedia === 'function') {
      mql = window.matchMedia(`(max-width: ${NARROW_MAX}px)`)
      if (mql && typeof mql.addEventListener === 'function') mql.addEventListener('change', update)
      else mql = null
    }
    update()
    return () => {
      window.removeEventListener('resize', update)
      if (mql) mql.removeEventListener('change', update)
    }
  }, [])
  return narrow
}

// A tour anchor id is a short lowercase slug (`shell`, `viewer`,
// `command-bar`, `right-rail`). Anything else is refused before it reaches
// querySelector, so a contract typo or a hostile string can never become a
// selector: the step falls back to its className target, exactly as before.
const ANCHOR_ID = /^[a-z][a-z0-9-]{0,63}$/

function resolveSelectorChain(selector) {
  if (!selector) return null
  for (const part of String(selector).split(',')) {
    const el = document.querySelector(part.trim())
    if (el) return el
  }
  return null
}

/**
 * Slice 4b: the spotlight resolves `[data-tour="<id>"]` FIRST, then the
 * step's className chain. `anchors` is the per-shell map from
 * `surfaceContract(id).tourAnchors` (step id -> anchor id), or null where the
 * shell declares no tour for that surface; a step the map does not name keeps
 * its className resolution unchanged. The anchor lookup returns the first
 * match in TREE ORDER, so an outer shell element wins over a shared inner
 * component that carries the same id (the stage's `.stage-viewer` wraps the
 * shared Viewer; its `.tc-rail-r` wraps the shared JobRail), which is what
 * keeps each shell's spotlight on the element its className target chose.
 * Exported for the anchor-resolution unit test.
 */
export function resolveTourTarget(step, anchors = null) {
  if (!step) return null
  const anchorId = anchors && typeof anchors === 'object' ? anchors[step.id] : null
  if (typeof anchorId === 'string' && ANCHOR_ID.test(anchorId)) {
    const el = document.querySelector(`[data-tour="${anchorId}"]`)
    if (el) return el
  }
  return resolveSelectorChain(step.target)
}

export default function DemoTour({
  steps = TOUR_STEPS,
  anchors = null,
  index: controlledIndex,
  onIndexChange,
  onCannedPrompt,
  onExit,
  landed = false,
  busy = false,
  bannerTitle = 'Guided demo: sample rooftop',
  bannerSubtitle = 'Real drawing, real tools. Every number below is computed live.',
}) {
  const [uncontrolled, setUncontrolled] = useState(0)
  const index = typeof controlledIndex === 'number' ? controlledIndex : uncontrolled
  // Phone width: the card is a collapsed coach (step, title, controls) that
  // the reader expands for the body copy. Desktop never reads this.
  const narrow = useNarrowViewport()
  const [coachOpen, setCoachOpen] = useState(false)
  const step = steps[Math.max(0, Math.min(index, steps.length - 1))]

  const setIndex = useCallback((next) => {
    const clamped = Math.max(0, Math.min(next, steps.length - 1))
    if (typeof controlledIndex !== 'number') setUncontrolled(clamped)
    if (onIndexChange) onIndexChange(clamped)
  }, [controlledIndex, onIndexChange, steps.length])

  // --- spotlight geometry --------------------------------------------------
  const [rect, setRect] = useState(null)
  // Phone width: the bottom chrome's top edge, measured with the spotlight
  // so a resize or a drawer change moves the coach's floor with it.
  const [chromeTop, setChromeTop] = useState(null)
  const [chromeBottom, setChromeBottom] = useState(0)
  const measure = useCallback(() => {
    if (readNarrow()) {
      setChromeTop(readChromeTop())
      const shell = document.querySelector('.app[data-studio-shell="cockpit"]')
      let bottom = 0
      for (const band of shell?.querySelectorAll('header.top, .tc-product-nav, #drafting-ribbon, .viewer-toolbar') || []) {
        const box = band.getBoundingClientRect()
        if (box.height) bottom = Math.max(bottom, box.bottom)
      }
      setChromeBottom(bottom)
    }
    const el = resolveTourTarget(step, anchors)
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
  }, [step, anchors])

  useLayoutEffect(() => {
    measure()
    const el = resolveTourTarget(step, anchors)
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
      if (narrow) document.querySelectorAll(NARROW_CHROME).forEach((chrome) => ro.observe(chrome))
    }
    const shell = document.querySelector('.app[data-studio-shell="cockpit"]')
    const drawers = narrow && shell && typeof MutationObserver !== 'undefined' ? new MutationObserver(measure) : null
    drawers?.observe(shell, { attributes: true, attributeFilter: ['data-drawer'] })
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', measure, true)
    return () => {
      clearTimeout(id)
      if (ro) ro.disconnect()
      drawers?.disconnect()
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', measure, true)
    }
  }, [measure, step, anchors, narrow])

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
    const h = Math.round(Math.max(el.scrollHeight, el.getBoundingClientRect().height))
    if (h) setCardH((prev) => (prev === h ? prev : h))
  }, [step, landed, typed, needsEffect, narrow, coachOpen])

  // NOTE: the tour deliberately does NOT own Escape. A capture-phase Esc listener
  // here terminated the whole walkthrough when the user only meant to dismiss the
  // route card / History / a selection, with no way back in. Exit stays reachable
  // via the banner's "Exit and explore freely" and the card's "Skip"; Esc goes back
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
    const H = cardH
    const vh = window.innerHeight
    const vw = window.innerWidth
    if (narrow) {
      // Phone: full width less an inset, the bottom edge on the floor (the
      // top of the highest bottom-chrome box, the job strip in the cockpit,
      // less a gap), the box capped at NARROW_MAX_VH of the viewport. When
      // the spotlight itself overlaps that box (the command bar beat) the
      // floor moves above the spotlight instead, if the box still fits.
      // Every number is computed from the viewport, and maxHeight clamps
      // the box even when the measured height is stale, so Next and Skip
      // are always on screen.
      const width = Math.max(0, vw - NARROW_INSET * 2)
      const cap = Math.floor(vh * NARROW_MAX_VH)
      const ceiling = chromeBottom + NARROW_INSET
      let floor = (chromeTop === null ? vh - NARROW_BOTTOM_RESERVE : chromeTop) - NARROW_INSET
      const box = Math.max(0, Math.min(H, cap, floor - ceiling))
      if (rect && rect.top - MARGIN < floor && rect.top + rect.height > floor - box
        && rect.top - MARGIN - ceiling >= box) {
        floor = rect.top - MARGIN
      }
      const top = Math.max(ceiling, floor - box)
      return { left: NARROW_INSET, top, width, maxHeight: Math.max(0, Math.min(cap, floor - top)) }
    }
    if (!rect) return { left: '50%', top: '50%', transform: 'translate(-50%, -50%)' }
    const W = 380
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

      {/* Phone width: no banner strip (it covered the cockpit's one-line
          header); Skip in the coach is the same exit. */}
      {!narrow && (
        <div className="tour-banner">
          <span className="tour-banner-title">{bannerTitle}</span>
          <span className="tour-banner-sub">{bannerSubtitle}</span>
          <button type="button" className="chip-neutral tour-banner-exit" onClick={onExit}>
            Exit and explore freely
          </button>
        </div>
      )}

      <div
        ref={cardRef}
        className={`tour-card${!rect && !narrow ? ' is-centered' : ''}${narrow ? ' is-coach' : ''}${narrow && coachOpen ? ' is-open' : ''}`}
        style={cardStyle}
        data-placement={narrow ? 'coach' : undefined}
      >
        <div className="tour-card-step">
          Step {index + 1} of {steps.length}
          {narrow && (
            <button
              type="button"
              className="chip-neutral tour-coach-toggle"
              aria-expanded={coachOpen}
              onClick={() => setCoachOpen((open) => !open)}
            >
              {coachOpen ? 'Less' : 'More'}
            </button>
          )}
        </div>
        <div className="tour-card-title">{step.title}</div>
        {(!narrow || coachOpen) && <p className="tour-card-body">{step.body}</p>}

        {step.prompt && (!narrow || coachOpen) && (
          <div className="tour-card-prompt">
            {typed}<span className="tour-caret">▌</span>
          </div>
        )}

        {/* Kept permanently mounted so the live region pre-exists the unlock —
            a region that mounts with its text is not reliably announced. */}
        <div className="tour-card-wait" role="status" aria-live="polite">
          {needsEffect && !landed ? 'Running it for real. Next unlocks when the result lands.' : ''}
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
            {isLast || step.action === 'exit' ? 'Exit and explore freely' : 'Next'}
          </button>
        </div>
      </div>
    </div>
  )
}
