// Tool-scene overlays — verbatim from the "Website One Surface" mock: the two
// 300px rails, the command-bar ghost, the tool top-bar cluster, and the
// caption. Choreography (ranks: rails 0-1 · bar 2 · top-bar cluster 3) lives
// in landing.css. Reuses the console's shared atoms (.key keycaps, .dot,
// .pulse, .chip-act) where they exactly match the mock.

import { useEffect, useState } from 'react'
import { navigate } from './router.js'
import { loadDemoSolve } from './intakeCache.js'

// mm:ss elapsed clock, seeded at the mock's 02:41 and ticking while the tool
// cast is live (text swaps are instant per the motion standard's exception
// list). `running` gates the interval so the hidden cast doesn't tick.
function useClock(running) {
  const [secs, setSecs] = useState(2 * 60 + 41)
  useEffect(() => {
    if (!running) return undefined
    const id = setInterval(() => setSecs((s) => s + 1), 1000)
    return () => clearInterval(id)
  }, [running])
  const mm = String(Math.floor(secs / 60)).padStart(2, '0')
  const ss = String(secs % 60).padStart(2, '0')
  return `${mm}:${ss}`
}

export default function ToolCast({ active }) {
  const clock = useClock(!!active)
  // Real numbers from the live demo solve — the events ledger describes THIS
  // drawing, so its counts must match what the stage is actually rendering.
  const [solve, setSolve] = useState(null)
  useEffect(() => {
    let live = true
    loadDemoSolve()
      .then((d) => { if (live) setSolve(d) })
      .catch(() => { /* event rows fall back to countless labels */ })
    return () => { live = false }
  }, [])
  const panelCount = solve?.stats?.panel_count

  const goApp = () => navigate('/app')
  const onBarKeyDown = (e) => {
    if (e.key === 'Enter') goApp()
  }

  return (
    <>
      {/* Tool top-bar cluster (overlays the top bar's right slot) */}
      <div className="tc-topcluster" data-cast="tool" style={{ '--rank': 3 }}>
        <span className="tc-solve pulse"><span className="dot" />String solve · {clock}</span>
        <button type="button" className="tc-back" onClick={() => navigate('/')}>← Back to the site</button>
        <span className="key">Esc</span>
      </div>

      {/* Left rail — feature requests */}
      <div className="tc-rail tc-rail-l" data-cast="tool" style={{ '--rank': 0 }}>
        <div className="tc-rail-head">
          <span className="tc-rail-title">Feature requests</span>
          <span className="tc-rail-sub">3 open</span>
        </div>
        <div className="tc-rail-list">
          <div className="tc-req">
            <span className="tc-req-name hot">"Glance card for permit readiness"</span>
            <span className="tc-req-state pulse accent">building · started 2 min ago</span>
          </div>
          <div className="tc-req">
            <span className="tc-req-name">"Rate-card diff between revisions"</span>
            <span className="tc-req-state">queued · #2</span>
          </div>
          <div className="tc-req">
            <span className="tc-req-name">"Export BOM grouped by combiner"</span>
            <span className="tc-req-state">new · Jul 15</span>
          </div>
        </div>
        <div className="tc-rail-note">
          <span>Asks typed at the bar become requests — builds start sandboxed.</span>
        </div>
        <div className="tc-rail-foot">
          <span className="tc-link">← Projects</span>
          <span className="tc-link muted">Details</span>
        </div>
      </div>

      {/* Right rail — events ledger */}
      <div className="tc-rail tc-rail-r" data-cast="tool" style={{ '--rank': 1 }}>
        <div className="tc-rail-head">
          <span className="tc-rail-title">Events</span>
          <span className="tc-rail-sub">live</span>
        </div>
        <div className="tc-events">
          <div className="tc-event current">
            <span className="dot live" />
            <span className="tc-event-text hot">String solve — this drawing</span>
            <span className="tc-event-time">{clock}</span>
          </div>
          <div className="tc-event">
            <span className="dot" />
            <span className="tc-event-text">
              Panel layout placed{panelCount ? ` · ${panelCount.toLocaleString('en-US')}` : ''}
            </span>
            <span className="tc-event-time">just now</span>
          </div>
          <div className="tc-event">
            <span className="dot hollow" />
            <span className="tc-event-text">seq 5 ingested · sandboxed</span>
            <span className="tc-event-time">2 h</span>
          </div>
          <div className="tc-event">
            <span className="dot" />
            <span className="tc-avatar">MC</span>
            <span className="tc-event-text">Promoted to class-b · m.chen</span>
            <span className="tc-event-time">Jul 12</span>
          </div>
        </div>
        <div className="tc-rail-foot">
          <span className="tc-link muted">All events</span>
        </div>
      </div>

      {/* Command-bar ghost — Enter in the input (or a controls-row chip) opens
          the real console at /app. */}
      <div className="tc-bar-wrap" data-cast="tool" style={{ '--rank': 2 }}>
        <div className="tc-bar">
          <div className="tc-bar-input-row">
            <span className="tc-bar-caret">›</span>
            <input
              type="text"
              className="tc-bar-input"
              placeholder="Find, act, or build — this rooftop is yours now…"
              onKeyDown={onBarKeyDown}
              aria-label="Command bar"
            />
            <span className="tc-bar-blink" />
          </div>
          <div className="tc-bar-controls">
            <button type="button" className="tc-bar-chip sq" onClick={goApp}>+</button>
            <button type="button" className="tc-bar-chip" onClick={goApp}>Scope · All ▾</button>
            <span className="tc-bar-scopes">find · act · build</span>
            <button type="button" className="tc-bar-proj" onClick={goApp}>utility-estimation ▾</button>
            <button type="button" className="key tc-bar-key" onClick={goApp}>⌘K</button>
          </div>
        </div>
      </div>

      {/* Caption — fades in ~600ms after the cast lands */}
      <div className="tc-caption" data-cast="tool">
        The drawing never moved — the site was the platform the whole time. Esc returns to the cover.
      </div>
    </>
  )
}
