import { useState } from 'react'
import './panels.css'

// Renders a JSON-Schema params object (CONTRACT §2 .params) as a small form.
function ParamForm({ schema, values, onChange }) {
  const props = schema?.properties || {}
  const keys = Object.keys(props)
  if (keys.length === 0) return <p className="params-none">No parameters.</p>
  return (
    <div className="params">
      {keys.map((k) => {
        const p = props[k]
        const label = sentence((p.title || k).replace(/_/g, ' '))
        const val = values[k] ?? p.default ?? (p.type === 'number' ? 0 : '')
        return (
          <label key={k} className="param">
            <span>{label}</span>
            <input
              type={p.type === 'number' ? 'number' : 'text'}
              value={val}
              onChange={(e) =>
                onChange({ ...values, [k]: p.type === 'number' ? Number(e.target.value) : e.target.value })
              }
            />
          </label>
        )
      })}
    </div>
  )
}

function defaultsOf(schema) {
  const out = {}
  for (const [k, p] of Object.entries(schema?.properties || {})) {
    if (p.default !== undefined) out[k] = p.default
  }
  return out
}

// Sentence-case a description: leading capital, rest untouched (calm rule).
function sentence(s) {
  if (!s || typeof s !== 'string') return s
  return s.charAt(0).toUpperCase() + s.slice(1)
}

// Widget-card spec: agent-authored/sandboxed cards always carry a mono
// provenance line (run · sha · grants · reviewer) — built only from fields that
// actually exist on tool.provenance, absent-safe.
function provenanceLine(t) {
  const p = t?.provenance
  if (!p || p.author !== 'agent') return null
  const parts = []
  const run = p.run_id || p.run || p.session_id
  if (run) parts.push(`run ${String(run).slice(0, 12)}`)
  const sha = p.sha256 || p.sha || p.hash
  if (sha) parts.push(String(sha).slice(0, 12))
  if (p.grants !== undefined && p.grants !== null) {
    const grants = Array.isArray(p.grants) ? (p.grants.length ? p.grants.join(' ') : 'none') : String(p.grants)
    parts.push(`grants ${grants}`)
  }
  if (p.reviewer !== undefined) parts.push(`reviewer ${p.reviewer || '—'}`)
  return parts.length ? parts.join(' · ') : null
}

export default function ToolsPanel({ tools, error, running, selectedTool, onRun, onOpenTool, onRetry, subtitle, writeLocked, writeEntitled = true }) {
  const [openName, setOpenName] = useState(null)
  const [paramsByTool, setParamsByTool] = useState({})

  return (
    <div className="tools-inner">
      <p className="panel-sub">{subtitle || 'The classic catalog — click one, set params, run on Leaf. The prompt box above is the primary path.'}</p>
      {error && (
        <div className="inline-error">
          <span>Couldn’t load tools — {error}</span>
          <button className="chip-act" onClick={onRetry || (() => window.location.reload())}>Retry</button>
          <span className="key" aria-hidden="true">R</span>
        </div>
      )}
      <div className="tool-list">
        {tools.map((t) => {
          const open = openName === t.name
          const params = paramsByTool[t.name] ?? defaultsOf(t.params)
          const isRunningThis = running && selectedTool?.name === t.name
          const isWrite = (t.capabilities || []).includes('drawing.write')
          const locked = !!writeLocked && isWrite
          // Real plan gate: a write tool the tenant's plan doesn't include.
          const entBlocked = isWrite && !writeEntitled
          const agentAuthored = (t.provenance?.author || 'agent') === 'agent'
          const provLine = provenanceLine(t)
          return (
            <div key={t.name} className={`tool-card ${open ? 'open' : ''}`}>
              <button
                className="tool-head"
                onClick={() => {
                  const next = open ? null : t.name
                  setOpenName(next)
                  onOpenTool?.(next ? t : null)
                }}
              >
                <div className="tool-head-main">
                  <span className="tool-name">{t.name}</span>
                  {/* tier status: dot + sentence-case word — hollow for
                      agent-authored (not in play until reviewed), never a pill */}
                  <span className={`tier-status ${agentAuthored ? 'sandboxed' : 'trusted'}`}>
                    <span className={`dot ${agentAuthored ? 'hollow' : ''}`} aria-hidden="true" />
                    {agentAuthored ? 'Agent' : 'User'}
                  </span>
                </div>
                <span className="tool-desc">{sentence(t.description)}</span>
                <div className="tool-tags">
                  {(t.capabilities || []).map((c) => (
                    <span key={c} className={`cap ${c.includes('write') ? 'write' : 'read'}`}>{c}</span>
                  ))}
                  <span className="cap kind">{t.kind}</span>
                </div>
                {provLine && <span className="prov-line">{provLine}</span>}
              </button>
              {open && (
                <div className="tool-body">
                  <ParamForm
                    schema={t.params}
                    values={params}
                    onChange={(v) => setParamsByTool((s) => ({ ...s, [t.name]: v }))}
                  />
                  {/* catalog runs are the secondary path: a quiet accent chip,
                      never a second haloed primary in the pane */}
                  <button
                    className="chip-act tool-run"
                    disabled={running || locked || entBlocked}
                    onClick={() => onRun(t, params)}
                  >
                    {isRunningThis ? 'Running on Leaf…' : 'Run'}
                  </button>
                  {entBlocked && !locked && (
                    <p className="lock-note">Your plan doesn’t include editing tools — upgrade to run write tools. Read tools still run.</p>
                  )}
                </div>
              )}
            </div>
          )
        })}
        {tools.length === 0 && !error && (
          <div className="skeleton-stack" aria-hidden="true">
            <div className="skeleton-row" />
            <div className="skeleton-row" />
            <div className="skeleton-row" />
          </div>
        )}
      </div>
    </div>
  )
}
