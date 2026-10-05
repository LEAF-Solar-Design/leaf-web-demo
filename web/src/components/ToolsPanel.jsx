import { useState } from 'react'
import ToolHistory from './ToolHistory.jsx'
import { isWriteTool } from '../lib/toolRecord.js'
import SchemaForm, { defaultsOf, sentence } from './SchemaForm.jsx'
import './panels.css'

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
export default function ToolsPanel({ tools, error, running, selectedTool, onRequestRun, onOpenTool, onReviseTool, onCatalogChanged, onRetry, retryKey, subtitle, writeLocked, writeLockNote = null, writeEntitled = true, runDisabled = false, runDisabledNote = null }) {
  const [openName, setOpenName] = useState(null)
  const [paramsByTool, setParamsByTool] = useState({})
  const [validByTool, setValidByTool] = useState({})

  return (
    <div className="tools-inner">
      <p className="panel-sub">{subtitle || 'The classic catalog: click one, set params, run on Leaf. The prompt box above is the primary path.'}</p>
      {error && (
        <div className="inline-error">
          <span>Couldn’t load tools: {error}</span>
          <button className="chip-act" onClick={onRetry || (() => window.location.reload())}>Retry</button>
          {retryKey && <span className="key" aria-hidden="true">R</span>}
        </div>
      )}
      <div className="tool-list">
        {tools.map((t) => {
          const open = openName === t.name
          const params = paramsByTool[t.name] ?? defaultsOf(t.params)
          const isRunningThis = running && selectedTool?.name === t.name
          const isWrite = isWriteTool(t)
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
                  if (next) setValidByTool((s) => ({ ...s, [next]: true }))
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
                  <SchemaForm
                    schema={t.params}
                    values={params}
                    onChange={(v) => setParamsByTool((s) => ({ ...s, [t.name]: v }))}
                    onValidityChange={(valid) => setValidByTool((s) => s[t.name] === valid ? s : ({ ...s, [t.name]: valid }))}
                  />
                  {/* catalog runs are the secondary path: a quiet accent chip,
                      never a second haloed primary in the pane */}
                  <button
                    className="chip-act tool-run"
                    disabled={running || locked || entBlocked || runDisabled || validByTool[t.name] === false}
                    title={runDisabled ? runDisabledNote || undefined : undefined}
                    onClick={() => { if (!running && !locked && !entBlocked && !runDisabled && validByTool[t.name] !== false) onRequestRun(t, params) }}
                  >
                    {isRunningThis ? 'Running on Leaf…' : 'Review & run'}
                  </button>
                  {onReviseTool && (
                    <button
                      type="button"
                      className="chip-act"
                      disabled={running || runDisabled}
                      title={runDisabled ? runDisabledNote || undefined : undefined}
                      onClick={() => onReviseTool(t)}
                    >
                      Revise
                    </button>
                  )}
                  {onReviseTool && <ToolHistory tool={t} onCatalogChanged={onCatalogChanged} />}
                  {/* runDisabled (no drawing open yet) is the more fundamental
                      gate, so it wins over the write-entitlement notes below —
                      those describe why a *tool* can't run, this describes why
                      *nothing* can run yet. */}
                  {runDisabled && runDisabledNote ? (
                    <p className="lock-note">{runDisabledNote}</p>
                  ) : (
                    <>
                      {entBlocked && !locked && (
                        <p className="lock-note">Your plan doesn’t include editing tools. Upgrade to run write tools. Read tools still run.</p>
                      )}
                      {/* A disabled run chip with no reason is the gap this closes.
                          The note is written mid-sentence for RoutePanel, so lift
                          the first letter for this standalone line. */}
                      {locked && writeLockNote && (
                        <p className="lock-note">
                          {writeLockNote.charAt(0).toUpperCase() + writeLockNote.slice(1)}
                        </p>
                      )}
                    </>
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
