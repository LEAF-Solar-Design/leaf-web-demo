// Crash barrier — a React render error must never turn the stage into a white
// screen. Renders a calm paper card (the drafting-studio identity) with a
// Reload button instead; it takes over the full viewport, so it owns its own
// ground regardless of which surface broke beneath it.
//
// Deliberately dependency-light: React only, no JSON imports, no api.js — the
// boundary has to survive whatever broke below it.
import React from 'react'

const WRAP = {
  minHeight: '100vh',
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  padding: '24px',
  background: '#e9e7e0',
  color: '#191d1a',
  fontFamily: "'Barlow', system-ui, -apple-system, sans-serif",
}

const CARD = {
  maxWidth: '420px',
  width: '100%',
  padding: '28px',
  borderRadius: '6px',
  background: '#ffffff',
  border: '1px solid #d6d4cb',
  boxShadow: '0 12px 32px rgba(25,29,26,0.16)',
  textAlign: 'center',
}

const DIAMOND = {
  width: '18px',
  height: '18px',
  margin: '0 auto 18px',
  background: '#1e6b45',
  transform: 'rotate(45deg)',
  borderRadius: '4px',
}

const TITLE = { margin: '0 0 8px', fontSize: '18px', fontWeight: 600, letterSpacing: '-0.01em' }
const BODY = { margin: '0 0 20px', fontSize: '14px', lineHeight: 1.55, color: '#4f5851' }

const BUTTON = {
  appearance: 'none',
  border: '1px solid #1e6b45',
  background: '#1e6b45',
  color: '#f5faf6',
  fontSize: '14px',
  fontWeight: 600,
  padding: '10px 22px',
  borderRadius: '4px',
  cursor: 'pointer',
}

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false }
    this.onReload = this.onReload.bind(this)
  }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch(error, info) {
    // Console only — nothing raw is allowed onto the screen.
    try {
      // eslint-disable-next-line no-console
      console.error('[ErrorBoundary]', error, info && info.componentStack)
    } catch { /* noop */ }
    // P2 crash rate: message CLASS and a stack hash only, never raw text
    // (the boundary stays dependency-light: dynamic import, never throws).
    //
    // The hash comes from telemetry's own `digest`, not a loop copied here.
    // The ingest door requires a digest to be exactly DIGEST_WIDTH characters
    // so the field cannot hold a sentence, and a second hash implementation
    // meant this row's `component_stack_hash` was the wrong width and got
    // dropped at the door -- a whole label lost, silently, on the one event
    // that reports crashes. `message_class` is filtered inside
    // `trackException`, so passing a raw `error.name` here is safe.
    //
    // No cross-emitter coordination: this emits independently of the global
    // handler. React 18's development build ALSO re-throws a render error
    // through a synthetic DOM event, which reaches telemetry's global handler
    // too, so one crash can produce two `client.exception` rows in dev --
    // documented as a known, unsolved gap in telemetry.js (see the comment
    // above `emitGlobalException`). Production React uses a plain try/catch
    // and does not re-throw, so this is a dev-build-only cosmetic, not a
    // production double-count.
    try {
      import('./telemetry.js').then((t) => {
        t.trackException({
          message_class: (error && error.name) || 'Error',
          component_stack_hash: t.digest(String(info?.componentStack || '')),
        }, 'exception_boundary')
      }).catch(() => {})
    } catch { /* noop */ }
  }

  onReload() {
    try { window.location.reload() } catch { /* noop */ }
  }

  render() {
    if (!this.state.hasError) return this.props.children
    return (
      <div style={WRAP} role="alert">
        <div style={CARD}>
          <div style={DIAMOND} />
          <h1 style={TITLE}>Something went wrong — reload</h1>
          <p style={BODY}>
            The workspace hit an unexpected problem. Reloading usually clears it;
            your sample drawing is untouched.
          </p>
          <button type="button" style={BUTTON} onClick={this.onReload}>Reload</button>
        </div>
      </div>
    )
  }
}
