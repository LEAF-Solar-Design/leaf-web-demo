import { useState } from 'react'
import './panels.css'

function schemaTypes(param) {
  return Array.isArray(param?.type) ? param.type : [param?.type]
}

function valueMatchesSchemaType(value, param) {
  const types = schemaTypes(param)
  return (value === null && types.includes('null'))
    || (Array.isArray(value) && types.includes('array'))
    || (value !== null && !Array.isArray(value) && typeof value === 'object' && types.includes('object'))
    || (typeof value === 'boolean' && types.includes('boolean'))
    || (typeof value === 'string' && types.includes('string'))
    || (typeof value === 'number' && Number.isFinite(value) && types.includes('number'))
    || (typeof value === 'number' && Number.isInteger(value) && types.includes('integer'))
}

function JsonParamInput({ value, expectedType, onChange }) {
  const emptyValue = expectedType === 'array' ? [] : {}
  const [draft, setDraft] = useState(() => JSON.stringify(value ?? emptyValue))
  const [valid, setValid] = useState(true)

  return (
    <input
      type="text"
      value={draft}
      aria-invalid={!valid}
      onChange={(event) => {
        const next = event.target.value
        setDraft(next)
        try {
          const parsed = JSON.parse(next)
          const matches = expectedType === 'array'
            ? Array.isArray(parsed)
            : parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
          if (!matches) throw new TypeError(`Expected a JSON ${expectedType}`)
          setValid(true)
          onChange(parsed)
        } catch {
          setValid(false)
        }
      }}
    />
  )
}

// Renders a JSON-Schema params object (CONTRACT §2 .params) as a small form.
function ParamForm({ schema, values, onChange }) {
  const props = schema?.properties || {}
  const keys = Object.keys(props)
  if (keys.length === 0) return <p className="params-none">No parameters.</p>
  return (
    <div className="params">
      {keys.map((k) => {
        const p = props[k]
        const types = schemaTypes(p)
        const isArray = types.includes('array')
        const isObject = types.includes('object')
        const isNumber = types.includes('number') || types.includes('integer')
        const isBoolean = types.includes('boolean')
        const isNullable = types.includes('null')
        const label = sentence((p.title || k).replace(/_/g, ' '))
        const hasValidDefault = p.default !== undefined && valueMatchesSchemaType(p.default, p)
        const val = Object.prototype.hasOwnProperty.call(values, k)
          ? values[k]
          : hasValidDefault ? p.default : (isArray ? [] : isObject ? {} : isNumber && !isNullable ? 0 : isBoolean ? false : '')
        return (
          <label key={k} className="param">
            <span>{label}</span>
            {isArray || isObject ? (
              <JsonParamInput
                value={val}
                expectedType={isArray ? 'array' : 'object'}
                onChange={(next) => onChange({ ...values, [k]: next })}
              />
            ) : isBoolean ? (
              <input
                type="checkbox"
                checked={Boolean(val)}
                onChange={(e) => onChange({ ...values, [k]: e.target.checked })}
              />
            ) : (
              <input
                type={isNumber ? 'number' : 'text'}
                step={types.includes('integer') ? '1' : undefined}
                value={val ?? ''}
                onChange={(e) => onChange({
                  ...values,
                  [k]: isNumber
                    ? (isNullable && e.target.value === '' ? null : Number(e.target.value))
                    : e.target.value,
                })}
              />
            )}
          </label>
        )
      })}
    </div>
  )
}

function defaultsOf(schema) {
  const out = {}
  for (const [k, p] of Object.entries(schema?.properties || {})) {
    if (p.default !== undefined && valueMatchesSchemaType(p.default, p)) out[k] = p.default
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

// `retryKey` (App's R ladder): the R keycap renders only while this row is the
// ladder's active rung — a shown cap is never inert, never double-firing.
export default function ToolsPanel({ tools, error, running, selectedTool, onRequestRun, onOpenTool, onReviseTool, onRetry, retryKey, subtitle, writeLocked, writeLockNote = null, writeEntitled = true }) {
  const [openName, setOpenName] = useState(null)
  const [paramsByTool, setParamsByTool] = useState({})

  return (
    <div className="tools-inner">
      <p className="panel-sub">{subtitle || 'The classic catalog — click one, set params, run on Leaf. The prompt box above is the primary path.'}</p>
      {error && (
        <div className="inline-error">
          <span>Couldn’t load tools — {error}</span>
          <button className="chip-act" onClick={onRetry || (() => window.location.reload())}>Retry</button>
          {retryKey && <span className="key" aria-hidden="true">R</span>}
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
                    onClick={() => onRequestRun(t, params)}
                  >
                    {isRunningThis ? 'Running on Leaf…' : 'Review & run'}
                  </button>
                  {onReviseTool && (
                    <button
                      type="button"
                      className="chip-act"
                      disabled={running}
                      onClick={() => onReviseTool(t)}
                    >
                      Revise
                    </button>
                  )}
                  {entBlocked && !locked && (
                    <p className="lock-note">Your plan doesn’t include editing tools — upgrade to run write tools. Read tools still run.</p>
                  )}
                  {/* A disabled run chip with no reason is the gap this closes.
                      The note is written mid-sentence for RoutePanel, so lift
                      the first letter for this standalone line. */}
                  {locked && writeLockNote && (
                    <p className="lock-note">
                      {writeLockNote.charAt(0).toUpperCase() + writeLockNote.slice(1)}
                    </p>
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
