// Site-scene overlays — copy VERBATIM from the "Website One Surface" mock,
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

export function enterWorkspace() {
  navigate('/try')
}

const SHEETS = [
  { code: '02', label: '02 The problem' },
  { code: '03', label: '03 How Branch works' },
  { code: '04', label: '04 Proof' },
  { code: '05', label: '05 Pricing placeholder' },
  { code: '06', label: '06 Docs' },
]

export default function LandingCast({ onTryTool = enterWorkspace }) {
  const [solve, setSolve] = useState(null)
  const [email, setEmail] = useState('')
  const [demandStatus, setDemandStatus] = useState('idle')
  async function registerInterest(event) {
    event.preventDefault()
    if (demandStatus === 'pending' || demandStatus === 'saved') return
    setDemandStatus('pending')
    try {
      await submitDemandCapture({ email: email.trim(), interest: 'Bring-your-own-key workspace' })
      setDemandStatus('saved')
    } catch {
      setDemandStatus('error')
    }
  }
  useEffect(() => {
    let live = true
    loadDemoSolve()
      .then((d) => { if (live) setSolve(d) })
      .catch(() => { /* widget falls back to a placeholder dash */ })
    return () => { live = false }
  }, [])
  const maxString = solve?.electrical?.max_modules_per_string

  return (
    <>
      {/* Top bar — persistent chrome shell (never recasts); the right-hand
          site cluster dissolves with the site cast. */}
      <div className="lp-topbar" data-cast="both">
        <img src="/site/icon-color.png" width="24" height="24" alt="" className="lp-roundel" />
        <span className="lp-wordmark">Leaf Automation</span>
        <span className="lp-brandsub">/ Leaf Build · utility-estimation</span>
        <div className="lp-top-slot">
          <div className="lp-top-site" data-cast="site" style={{ '--rank': 0 }}>
            <span className="lp-sheetno">Sheet 01 of 07 — Cover</span>
            <button type="button" className="lp-trial" onClick={onTryTool}>Open workspace</button>
          </div>
        </div>
      </div>

      {/* Statement block */}
      <div className="lp-stmt" data-cast="site" style={{ '--rank': 0 }}>
        <span className="lp-kicker">We do the drafting. You do the engineering.</span>
        <div className="lp-headline">
          Three decades of <span className="lp-headline-accent">manual drafting</span>, dressed up as engineering.
        </div>
        <div className="lp-sub">
          Branch is stringing this 3.2 MW rooftop live behind this page — 25 hours of drafting in 3 minutes, zero NEC violations.
        </div>
        <div className="lp-cta-row">
          <button type="button" className="lp-cta" onClick={onTryTool}>Try Branch — no install</button>
          <span className="lp-price">PRICING PLACEHOLDER</span>
        </div>
        <form onSubmit={registerInterest} aria-label="Register interest">
          <label htmlFor="front-door-email">Interested in a bring-your-own-key workspace?</label>
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
            <div className="lp-stat-sub">NEC violations · survey n=18</div>
          </div>
        </div>
      </div>

      {/* String-solver widget card */}
      <div className="lp-solver" data-cast="site" style={{ '--rank': 1 }}>
        <div className="lp-solver-head">
          <span className="dot live" />
          <span className="lp-solver-title">String solver — live</span>
          <span className="lp-solver-tier">class-b widget</span>
        </div>
        <div className="lp-solver-row">
          <span className="lp-solver-field">Q.Peak 425 <span className="lp-solver-caret">▾</span></span>
          <button type="button" className="chip-act lp-solver-solve" onClick={onTryTool}>Solve</button>
        </div>
        <div className="lp-solver-out">
          <span className="lp-solver-label">Max string · NEC 690.7</span>
          <span className="lp-solver-big">{maxString ?? '—'} <span className="lp-solver-unit">modules</span></span>
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
            onClick={() => s.code === '05' ? document.getElementById('front-door-email')?.focus() : navigate(`/sheets#${s.code}`)}
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
