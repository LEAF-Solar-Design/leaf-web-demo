// Site-scene overlays: copy VERBATIM from the "Website One Surface" mock,
// EXCEPT numbers that describe the live demo solve: those come from
// loadDemoSolve() (the same data animating the stage) so the page can never
// claim what the data doesn't show.
// Every pane carries data-cast (site | both) + a --rank custom property; the
// dissolve/condense choreography lives entirely in landing.css, keyed off the
// stage-root's data-scene attribute.

import { useEffect, useState } from 'react'
import { navigate } from './router.js'
import { loadDemoSolve } from './intakeCache.js'
import { submitDemandCapture } from '../api.js'
import { humanizeError } from '../errorHumanize.js'

export function enterWorkspace() {
  navigate('/try')
}

export function enterDemo() {
  // Full navigation, not the SPA router: the demo flag is read once at boot
  // (demoState.js, drawingIdentity.js), so a client-side route change leaves
  // the signed-out live shell mounted with the query ignored.
  // Lands in the cockpit demo (the one shell: sample rooftop, guided tour,
  // the four profiles), not the ToolCast stage; the website's forwarded
  // /try?demo=1 reaches the same console via authBoot.js under ONE_SHELL.
  window.location.assign('/app?demo=1')
}

const SHEETS = [
  { code: '02', target: 'a-101', label: '02 The problem' },
  { code: '03', target: 'a-102', label: '03 How Branch works' },
  { code: '04', target: 'e-401', label: '04 Proof' },
  { code: '05', target: 'c-201', label: '05 Pricing' },
  { code: '06', target: 's-501', label: '06 Docs' },
]

export default function LandingCast({ onTryTool = enterWorkspace, onTryDemo = enterDemo }) {
  const [solve, setSolve] = useState(null)
  const [solveError, setSolveError] = useState('')
  const [solveAttempt, setSolveAttempt] = useState(0)
  const [email, setEmail] = useState('')
  const [demandStatus, setDemandStatus] = useState('idle')
  async function registerInterest(event) {
    event.preventDefault()
    if (demandStatus === 'pending' || demandStatus === 'saved') return
    setDemandStatus('pending')
    try {
      await submitDemandCapture({ email: email.trim(), interest: 'Workspace with your own API key' })
      setDemandStatus('saved')
    } catch {
      setDemandStatus('error')
    }
  }
  useEffect(() => {
    let live = true
    setSolveError('')
    loadDemoSolve()
      .then((d) => {
        if (d?.ok === false || d?.electrical?.max_modules_per_string == null) {
          throw new Error('String length unavailable')
        }
        if (live) setSolve(d)
      })
      .catch(() => {
        if (live) setSolveError(humanizeError('Could not load the string length. Please retry.'))
      })
    return () => { live = false }
  }, [solveAttempt])
  const maxString = solve?.electrical?.max_modules_per_string

  return (
    <>
      {/* Top bar: persistent chrome shell (never recasts); the right-hand
          site cluster dissolves with the site cast. */}
      <div className="lp-topbar" data-cast="both">
        <img className="lp-lockup" src="/brand/leaf-automation-horizontal-on-dark.svg" alt="Leaf Automation" height="24" />
        <span className="lp-brandsub">/ Leaf Automation Studio</span>
        <div className="lp-top-slot">
          <div className="lp-top-site" data-cast="site" style={{ '--rank': 0 }}>
            <span className="lp-sheetno">Sheet 01 of {String(SHEETS.length + 1).padStart(2, '0')}, Cover</span>
            <button type="button" className="lp-trial" onClick={onTryTool}>Open workspace</button>
          </div>
        </div>
      </div>

      {/* Statement block */}
      <div className="lp-stmt" data-cast="site" style={{ '--rank': 0 }}>
        <span className="lp-kicker">We do the drafting. You do the engineering.</span>
        <h1 className="lp-headline">
          Three decades of <span className="lp-headline-accent">manual drafting</span>, dressed up as engineering.
        </h1>
        <div className="lp-sub">
          Leaf Automation Studio builds CAD tools from plain English and runs them on your drawing.
          Explore a sample rooftop and its stringing results. This preview can show saved sample results instead of a new calculation.
        </div>
        <div className="lp-cta-row">
          <button type="button" className="lp-cta" onClick={onTryDemo}>Try Branch on the sample rooftop</button>
          <span className="lp-price">Free to try. Pricing coming soon.</span>
        </div>
        <form onSubmit={registerInterest} aria-label="Register interest">
          <label htmlFor="front-door-email">Want a workspace that uses your own API key?</label>
          <input id="front-door-email" type="email" autoComplete="email" required disabled={demandStatus === 'pending' || demandStatus === 'saved'} value={email} onChange={(event) => setEmail(event.target.value)} placeholder="Email address" />
          <button type="submit" className="chip-act" disabled={demandStatus === 'pending' || demandStatus === 'saved'}>
            {demandStatus === 'pending' ? 'Saving interest' : 'Register interest'}
          </button>
          {demandStatus === 'saved' && <span role="status">Interest saved. No payment required.</span>}
          {demandStatus === 'error' && <span role="alert">Could not save your interest. Please try again.</span>}
        </form>
        <div className="lp-stats">
          <div>
            <div className="lp-stat-big">25 hrs <span className="lp-stat-accent">→ 3 min</span></div>
            <div className="lp-stat-sub">Stringing · this rooftop</div>
          </div>
          <div>
            <div className="lp-stat-mid">0 of 12k</div>
            <div className="lp-stat-sub">NEC violations · 18 projects surveyed</div>
          </div>
        </div>
      </div>

      {/* String-solver widget card */}
      <div className="lp-solver" data-cast="site" style={{ '--rank': 1 }}>
        <div className="lp-solver-head">
          <span className="dot live" />
          <span className="lp-solver-title">Sample stringing results</span>
        </div>
        <div className="lp-solver-row">
          <span>Q.Peak 425</span>
          <button type="button" className="lp-solver-open" onClick={onTryTool}>Open in workspace</button>
        </div>
        <div className="lp-solver-out">
          <span className="lp-solver-label">Max string · NEC 690.7</span>
          {solveError
            ? <><span role="alert">{solveError}</span><button type="button" className="lp-solver-open" onClick={() => setSolveAttempt((attempt) => attempt + 1)}>Retry</button></>
            : maxString == null
            ? <span className="lp-solver-pending"><span className="lp-pulse" /> Solving</span>
            : <span className="lp-solver-big">{maxString} <span className="lp-solver-unit">modules</span></span>}
        </div>
      </div>

      {/* Sheet-index footer */}
      <div className="lp-index" data-cast="site" style={{ '--rank': 0 }}>
        <span className="lp-tab active">01 Cover</span>
        {SHEETS.map((s) => (
          <button
            key={s.code}
            type="button"
            className="lp-tab"
            onClick={() => navigate(`/sheets#${s.target}`)}
          >
            {s.label}
          </button>
        ))}
        <span className="lp-index-meta">
          Leaf Automation · drawing set <span className="lp-mono">R2026.07</span> · Columbus, OH
        </span>
      </div>
    </>
  )
}
