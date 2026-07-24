// Layer 0 of the one-surface stage — permanent for the life of the session.
// #0a0a0a void + 24px grid lines + the left scrim (opacity driven by the
// stage-root's data-scene attribute) + a lazy full-bleed <Viewer> rendering the
// real rooftop intake with animated string routes. The viewer condenses in
// ONCE when ready (single enter transition per the motion standard), then
// will-change is removed and it never transitions again. If WebGL is
// unavailable (or the intake fails), it degrades to the Canvas2D mock port.

import React, { Suspense, useEffect, useRef, useState } from 'react'
import { loadIntake, loadDemoSolve } from './intakeCache.js'
import StageFallback2D from './StageFallback2D.jsx'

const Viewer = React.lazy(() => import('../components/Viewer.jsx'))

// Stable module-level layer color fn — the Viewer's build effect depends on
// its identity, so it must never change (muted drafting gray, mock's module
// outline family).
const stageColorForLayer = () => '#96a0ac'

// Fires onReady on the second animation frame after the Suspense subtree
// resolves — i.e. once the viewer has actually mounted and built its scene.
function ReadySentinel({ onReady }) {
  useEffect(() => {
    let r2
    const r1 = requestAnimationFrame(() => { r2 = requestAnimationFrame(onReady) })
    return () => { cancelAnimationFrame(r1); if (r2) cancelAnimationFrame(r2) }
  }, [onReady])
  return null
}

export default function StageLayer({ intakeOverride = null }) {
  const [intake, setIntake] = useState(null)
  const [routes, setRoutes] = useState([])
  const [fallback, setFallback] = useState(false)
  const [entered, setEntered] = useState(false)
  const [settled, setSettled] = useState(false)
  const settleTimer = useRef(null)

  useEffect(() => {
    let live = true
    loadIntake()
      .then((d) => { if (live) setIntake(d) })
      .catch(() => { if (live) setFallback(true) })
    loadDemoSolve()
      .then((d) => { if (live) setRoutes(Array.isArray(d.strings) ? d.strings : []) })
      .catch(() => { if (live) setRoutes([]) })
    return () => { live = false; if (settleTimer.current) clearTimeout(settleTimer.current) }
  }, [])

  const handleReady = () => {
    setEntered(true)
    // After the focus pull (2250ms) completes, drop will-change and freeze —
    // the drawing never transitions again for the life of the session.
    settleTimer.current = setTimeout(() => setSettled(true), 2400)
  }

  return (
    <div className="stage-layer" aria-hidden="true">
      <div className="stage-grid" />
      {fallback ? (
        <StageFallback2D />
      ) : (
        <div className={`stage-viewer${entered ? ' in' : ''}${settled ? ' settled' : ''}`}>
          <Suspense fallback={null}>
            {(intakeOverride || intake) && (
              <>
                <Viewer
                  intake={intakeOverride || intake}
                  colorForLayer={stageColorForLayer}
                  background="transparent"
                  controlsEnabled={false}
                  stringRoutes={intakeOverride ? [] : routes}
                  onGlError={() => setFallback(true)}
                />
                <ReadySentinel onReady={handleReady} />
              </>
            )}
          </Suspense>
        </div>
      )}
      <div className="stage-scrim" />
    </div>
  )
}
