export default function SessionGate({ configured, onSignIn, onDemo }) {
  return (
    <section className="session-gate" aria-labelledby="session-gate-title">
      <h3 id="session-gate-title">You are not signed in</h3>
      <p>Sign in to load your tools and drawings from the cloud workspace.</p>
      <div>
        {configured && <button type="button" className="chip-act" onClick={onSignIn}>Sign in</button>}
        {onDemo && <button type="button" className="tc-bar-chip" onClick={onDemo}>Explore the demo</button>}
      </div>
    </section>
  )
}
