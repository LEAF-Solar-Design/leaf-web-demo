// Crash barrier — a React render error must never turn the stage into a white
// screen. Renders a calm dark-emerald card with a Reload button instead.
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
  background: '#0a0f0d',
  color: '#e6f2ec',
  fontFamily: "'Inter Tight', 'Inter', system-ui, -apple-system, sans-serif",
}

const CARD = {
  maxWidth: '420px',
  width: '100%',
  padding: '28px',
  borderRadius: '14px',
  background: '#0f1714',
  border: '1px solid #1d2b25',
  boxShadow: '0 18px 48px rgba(0,0,0,0.45)',
  textAlign: 'center',
}

const DIAMOND = {
  width: '18px',
  height: '18px',
  margin: '0 auto 18px',
  background: '#22c55e',
  transform: 'rotate(45deg)',
  borderRadius: '4px',
}

const TITLE = { margin: '0 0 8px', fontSize: '18px', fontWeight: 600, letterSpacing: '-0.01em' }
const BODY = { margin: '0 0 20px', fontSize: '14px', lineHeight: 1.55, color: '#9db3a8' }

const BUTTON = {
  appearance: 'none',
  border: '1px solid #22c55e',
  background: '#22c55e',
  color: '#06120c',
  fontSize: '14px',
  fontWeight: 600,
  padding: '10px 22px',
  borderRadius: '9px',
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
    try {
      import('./telemetry.js').then((t) => {
        let hash = 0
        const stack = String(info?.componentStack || '')
        for (let i = 0; i < stack.length; i++) hash = ((hash << 5) - hash + stack.charCodeAt(i)) | 0
        t.trackException({
          message_class: (error && error.name) || 'Error',
          component_stack_hash: String(hash >>> 0),
        })
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
