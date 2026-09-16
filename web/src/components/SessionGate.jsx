import { useState } from 'react'
import { submitDemandCapture } from '../api.js'

export function DemandCaptureCard({ compact = false }) {
  const [email, setEmail] = useState('')
  const [interest, setInterest] = useState('')
  const [status, setStatus] = useState('idle')
  const [error, setError] = useState('')

  async function submit(event) {
    event.preventDefault()
    setStatus('submitting')
    setError('')
    try {
      await submitDemandCapture({ email, interest, org: null })
      setStatus('success')
    } catch (caught) {
      setStatus('idle')
      setError(caught?.message || 'We could not save your request. Please try again.')
    }
  }

  if (status === 'success') return <p role="status">Thanks, we’ll notify you.</p>

  return (
    <form className="demand-capture-card" data-testid="demand-capture-card" onSubmit={submit}>
      <h4>{compact ? 'Keep me posted' : 'Need a plan that fits?'}</h4>
      {!compact && <p>Tell us what you need. We’ll contact you when it is available.</p>}
      <label>
        Work email
        <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
      </label>
      <label>
        What are you trying to automate?
        <textarea value={interest} onChange={(event) => setInterest(event.target.value)} maxLength="1200" required />
      </label>
      {error && <p role="alert">{error}</p>}
      <button type="submit" className="chip-act" disabled={status === 'submitting'}>
        {status === 'submitting' ? 'Saving…' : 'Notify me'}
      </button>
    </form>
  )
}

// One filled primary per gate: the demo is the only route to the sample
// rooftop, so it carries chip-act and Sign in stays a quiet chip whenever both
// render. The demand form is folded closed behind a quiet toggle so a stranger
// is not asked for an email before seeing the product.
export default function SessionGate({ configured, onSignIn, onDemo }) {
  const [demandOpen, setDemandOpen] = useState(false)
  const signInClass = onDemo ? 'tc-bar-chip' : 'chip-act'
  return (
    <section className="session-gate" aria-labelledby="session-gate-title">
      <h3 id="session-gate-title">You are not signed in</h3>
      <p>Sign in to load your tools and drawings from the cloud workspace.</p>
      <div>
        {onDemo && <button type="button" className="chip-act" onClick={onDemo}>Explore the demo</button>}
        {configured && <button type="button" className={signInClass} onClick={onSignIn}>Sign in</button>}
      </div>
      <button
        type="button"
        className="tc-bar-chip"
        aria-expanded={demandOpen}
        aria-controls="session-gate-demand"
        onClick={() => setDemandOpen((open) => !open)}
      >
        Need a plan that fits?
      </button>
      {demandOpen && (
        <div id="session-gate-demand">
          <DemandCaptureCard compact />
        </div>
      )}
    </section>
  )
}
