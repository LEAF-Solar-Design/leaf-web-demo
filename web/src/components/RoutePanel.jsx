// The §12 nl-prompt routing decision, rendered as SB3 bar furniture inside
// .bar-dock (crib §1): a confident route is a tinted DECISION STRIP with quiet
// chips (never a card with a halo'd primary); a low-confidence / unresolvable
// route is a RESOLVER card of rows attached above the well — active row = 2 px
// accent left bar on the accent tint + Enter cap, counts muted right, lane
// dots per the standard's grammar. Keyboard: ↑/↓ move the active resolver row,
// Enter confirms; Esc (App's global key ladder) dismisses the route. The user
// still confirms before anything runs — paid actions never auto-execute.

import { useEffect, useMemo, useRef, useState } from 'react'

function defaultsOf(schema) {
  const out = {}
  for (const [k, p] of Object.entries(schema?.properties || {})) {
    if (p.default !== undefined) out[k] = p.default
  }
  return out
}

// Calm inline parameter summary for the decision strip ("layer roofline · n 4").
function paramsSummary(params) {
  const entries = Object.entries(params || {})
  if (entries.length === 0) return null
  return entries.map(([k, v]) => `${k.replace(/_/g, ' ')} ${String(v)}`).join(' · ')
}

export default function RoutePanel({
  route, tools, running, writeLocked, writeEntitled = true,
  onRun, onPickAlternative, onOpenAuthor, onDismiss,
}) {
  const [activeIdx, setActiveIdx] = useState(0)
  // Enter arms 350ms AFTER the route mounts: the keypress that dispatched the
  // prompt (or its OS key-repeat) must never also confirm the run — one Enter,
  // one act. A deliberate second Enter lands after the cooldown.
  const enterArmedAtRef = useRef(0)

  const toolObj = useMemo(
    () => (route && route.lane === 'run' ? tools.find((t) => t.name === route.tool) || null : null),
    [route, tools],
  )
  const confident = !!route && route.lane === 'run' && route.confidence >= 0.7
  const params = useMemo(
    () => (toolObj ? { ...defaultsOf(toolObj.params), ...(route?.params || {}) } : (route?.params || {})),
    [toolObj, route],
  )
  const isWrite = (toolObj?.capabilities || []).includes('drawing.write')
  const locked = !!writeLocked && isWrite
  // Plan gate (real, enforced server-side): a write tool the tenant's plan
  // doesn't include. Distinct from `locked` (another session holds the checkout).
  const entBlocked = isWrite && !writeEntitled

  // Resolver rows (low-confidence best guess, or a tool missing from this
  // catalog): the direct-run best guess first, then the alternatives. The LIVE
  // server router returns alternatives as {tool, confidence} (no description),
  // so resolve descriptions from the catalog; the stub's inline one wins.
  const rows = useMemo(() => {
    if (!route || route.lane !== 'run' || (confident && toolObj)) return []
    const descOf = (name) => tools.find((t) => t.name === name)?.description || ''
    const out = []
    if (toolObj) {
      out.push({ kind: 'run', tool: route.tool, confidence: route.confidence, description: toolObj.description || '' })
    }
    for (const a of route.alternatives || []) {
      out.push({ kind: 'pick', tool: a.tool, confidence: a.confidence, description: a.description || descOf(a.tool) })
    }
    return out
  }, [route, confident, toolObj, tools])

  useEffect(() => { setActiveIdx(0); enterArmedAtRef.current = performance.now() + 350 }, [route])

  // Enter / arrow keys while a decision surface is up (Esc lives in App's
  // global ladder). Enter on a focused button/link is left to that control.
  useEffect(() => {
    if (!route) return undefined
    const onKey = (e) => {
      if (e.key === 'ArrowDown' && rows.length > 0) {
        e.preventDefault(); setActiveIdx((i) => Math.min(i + 1, rows.length - 1)); return
      }
      if (e.key === 'ArrowUp' && rows.length > 0) {
        e.preventDefault(); setActiveIdx((i) => Math.max(i - 1, 0)); return
      }
      if (e.key !== 'Enter') return
      if (performance.now() < enterArmedAtRef.current) return // dispatch keypress cooling down
      if (e.target && /^(button|a)$/i.test(e.target.tagName || '')) return
      e.preventDefault()
      if (route.lane === 'build') { onOpenAuthor(); return }
      if (route.lane === 'solve') { if (onDismiss) onDismiss(); return }
      if (confident && toolObj) {
        if (!running && !locked && !entBlocked) onRun(toolObj, params)
        return
      }
      const row = rows[activeIdx]
      if (!row) return
      if (row.kind === 'run' && toolObj) {
        if (!running && !locked && !entBlocked) onRun(toolObj, params)
      } else {
        onPickAlternative(row.tool)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [route, rows, activeIdx, confident, toolObj, params, running, locked, entBlocked,
      onRun, onPickAlternative, onOpenAuthor, onDismiss])

  if (!route) return null

  // ---- BUILD lane: decision strip pointing at the author flow ---------------
  if (route.lane === 'build') {
    return (
      <div className="strip-decision enter">
        <span className="dot square" aria-hidden="true" />
        <span className="strip-sentence">
          Build — <span className="route-title">author a new capability</span>. Your description is
          prefilled in “Author a tool”.
        </span>
        <button type="button" className="chip-act" onClick={onOpenAuthor}>Open author flow</button>
        <span className="key hot">Enter</span>
        <span className="key">Esc</span>
      </div>
    )
  }

  // ---- SOLVE lane: honest not-wired advisory strip --------------------------
  if (route.lane === 'solve') {
    return (
      <div className="strip-decision enter">
        <span className="dot square" aria-hidden="true" />
        <span className="strip-sentence">
          <span className="route-title">Solve lane</span> — string / combiner / route jobs run on
          the cloud solver, which is not connected in this demo. Nothing was
          executed.
        </span>
        <span className="key">Esc</span>
      </div>
    )
  }

  // ---- RUN lane, confident: decision strip (Run · Enter · Esc) --------------
  if (confident && toolObj) {
    const summary = paramsSummary(params)
    return (
      <div className="strip-decision enter">
        <span className="dot square" aria-hidden="true" />
        <span className="strip-sentence">
          Run <span className="route-tool">{route.tool}</span>
          {isWrite && <> <span className="cap write">drawing.write</span></>}
          {summary && <span className="dim"> · {summary}</span>}
          <span className="dim">
            {' — '}
            {locked
              ? 'editing is locked by another session; this write tool is paused.'
              : entBlocked
                ? 'your plan doesn’t include editing tools.'
                : isWrite
                  ? 'creates a new version — you confirm before it runs.'
                  : 'you confirm before it runs.'}
          </span>
        </span>
        <button
          type="button"
          className="chip-act"
          disabled={running || locked || entBlocked}
          onClick={() => onRun(toolObj, params)}
        >
          {running ? 'Running…' : 'Run'}
        </button>
        <span className="key hot">Enter</span>
        <span className="key">Esc</span>
      </div>
    )
  }

  // ---- RUN lane, low confidence / not in this catalog: resolver rows --------
  const conf = Math.round((route.confidence || 0) * 100)
  return (
    <div className="resolver enter" role="listbox" aria-label="Route resolver">
      <div className="resolver-header">
        {toolObj
          ? <>Run · best guess {conf}% match</>
          : route.slash
            ? <>“/{route.tool}” isn’t a tool in this catalog. Pick an alternative:</>
            : <>“{route.tool}” is live-only — not in this catalog. Pick an alternative:</>}
      </div>
      {rows.map((row, i) => (
        <div
          key={`${row.kind}-${row.tool}`}
          className={`resolver-row ${i === activeIdx ? 'active' : ''}`}
          role="option"
          aria-selected={i === activeIdx}
          onMouseEnter={() => setActiveIdx(i)}
          onClick={() => {
            if (row.kind === 'run' && toolObj) {
              if (!running && !locked && !entBlocked) onRun(toolObj, params)
            } else {
              onPickAlternative(row.tool)
            }
          }}
        >
          <span className="lbar" aria-hidden="true" />
          <span className={row.kind === 'run' ? 'dot' : 'dot hollow'} aria-hidden="true" />
          <span className="label">
            <span className="route-tool">{row.tool}</span>
            {row.description && <span className="dim"> · {row.description}</span>}
          </span>
          <span className="count">
            {typeof row.confidence === 'number' ? `${Math.round(row.confidence * 100)}%` : ''}
          </span>
          {i === activeIdx && <span className="key hot">Enter</span>}
        </div>
      ))}
      {rows.length === 0 && (
        <div className="resolver-row">
          <span className="lbar" aria-hidden="true" />
          <span className="dot hollow" aria-hidden="true" />
          <span className="label">No matching capability — rephrase, or open the author flow.</span>
        </div>
      )}
    </div>
  )
}
