// The SB3 command bar well (crib §1), docked at the bottom of the center
// column. Two rows — a 40 px input row (accent › caret, 13 px text, placeholder
// "Find, act, or build…") and a light controls row inside the well: the 20 px
// "+" add chip and ONE "scope ▾" chip left (the chip opens the O2 resolver
// offering the find · act · build scopes, each row carrying its lane's live
// hit dot); project context, the summon keycap (⌘K, swapping to an emphasized
// Enter cap while focused) and a quiet Run chip right (SB3: no primary in the
// well). Enter dispatches; while a route decision is showing above the well,
// Enter belongs to the resolver (routeActive) and typing clears the stale
// route (App-side).
//
// G2 ingest: the bar is the ONE drop catcher — mid-drag the well border goes
// dashed accent with "Drop manifest to ingest — runs sandboxed". No ingest
// path exists in src/api.js, so a drop surfaces the honest X1-style red strip
// (never a silent ignore).

import { useEffect, useRef, useState } from 'react'
import useExit from '../useExit.js'

// The bar's scopes, mapped onto the app's lanes (find→run · act→solve ·
// build→author). Selecting find/act returns you to the composer (the router
// still decides the lane from what you type — no fake filter); build opens
// the real author flow, which also makes the "+" add chip an honest action.
const SCOPES = [
  { id: 'find', lane: 'run', desc: 'run an existing tool on the drawing' },
  { id: 'act', lane: 'solve', desc: 'cloud solve jobs — not wired in this demo' },
  { id: 'build', lane: 'build', desc: 'author a new capability' },
]

// Lane hit dots per the standard's dot grammar: run/build are actionable lanes
// (solid green on hit); solve is honestly not wired (amber square on hit);
// every lane rests as a hollow not-in-play dot.
function laneDotClass(lane, hit) {
  if (!hit) return 'dot hollow'
  if (lane === 'solve') return 'dot square'
  return 'dot'
}

export default function PromptBox({
  value, onChange, onDispatch, routing, hintLane, projectName, inputRef, routeActive,
  onOpenAuthor,
}) {
  const [focused, setFocused] = useState(false)
  const [scopeOpen, setScopeOpen] = useState(false)
  const [scopeIdx, setScopeIdx] = useState(0)
  const [dragging, setDragging] = useState(false)
  const [dropErr, setDropErr] = useState(null)
  const dragDepth = useRef(0) // dragenter/leave pair counter (children re-fire them)
  const rootRef = useRef(null)
  const scopeMenu = useExit(scopeOpen) // 180 ms M1 exit fade

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { // isComposing: IME confirm-Enter must not dispatch
      if (routeActive || scopeOpen) return // the resolver / decision strip owns Enter
      e.preventDefault()
      onDispatch()
    }
  }

  const openScope = (idx) => { setScopeIdx(idx); setScopeOpen(true) }
  const pickScope = (s) => {
    setScopeOpen(false)
    if (s.lane === 'build' && onOpenAuthor) { onOpenAuthor(); return }
    inputRef?.current?.focus()
  }

  // Scope resolver keys (capture, so the app's global Esc/Enter ladders stand
  // down while the menu is open) + outside-click close — the ProjectSwitcher
  // popover's pattern.
  useEffect(() => {
    if (!scopeOpen) return undefined
    const onDoc = (e) => { if (rootRef.current && !rootRef.current.contains(e.target)) setScopeOpen(false) }
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); setScopeOpen(false); return }
      if (e.key === 'ArrowDown') { e.preventDefault(); setScopeIdx((i) => Math.min(i + 1, SCOPES.length - 1)); return }
      if (e.key === 'ArrowUp') { e.preventDefault(); setScopeIdx((i) => Math.max(i - 1, 0)); return }
      if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); pickScope(SCOPES[scopeIdx]) }
    }
    document.addEventListener('mousedown', onDoc)
    window.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      window.removeEventListener('keydown', onKey, true)
    }
  }, [scopeOpen, scopeIdx]) // eslint-disable-line react-hooks/exhaustive-deps

  // --- G2 drop catcher -----------------------------------------------------
  const onDragEnter = (e) => {
    e.preventDefault()
    dragDepth.current += 1
    setDragging(true)
  }
  const onDragOver = (e) => { e.preventDefault() }
  const onDragLeave = () => {
    dragDepth.current = Math.max(0, dragDepth.current - 1)
    if (dragDepth.current === 0) setDragging(false)
  }
  const onDrop = (e) => {
    e.preventDefault()
    dragDepth.current = 0
    setDragging(false)
    // Honest failure: there is no ingest endpoint in api.js — say so plainly
    // (X1 anatomy: red dot + sentence naming what failed + honest note).
    const name = e.dataTransfer?.files?.[0]?.name
    setDropErr(`Ingest isn’t connected in this demo${name ? ` — ${name} wasn’t ingested` : ''}`)
  }

  return (
    <>
      {dropErr && (
        <div className="strip-failed enter">
          <span className="dot red" aria-hidden="true" />
          <span className="strip-sentence">
            {dropErr}
            <span className="dim"> · nothing was uploaded</span>
          </span>
          <button type="button" className="chip-act" onClick={() => setDropErr(null)}>Dismiss</button>
        </div>
      )}
      <div
        className={`bar${dragging ? ' drag' : ''}`}
        ref={rootRef}
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
      >
        {dragging && (
          <div className="bar-drop-hint" aria-hidden="true">Drop manifest to ingest — runs sandboxed</div>
        )}
        <div className="bar-input">
          <span className="bar-caret" aria-hidden="true">›</span>
          <input
            ref={inputRef}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            onKeyDown={onKeyDown}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            placeholder="Find, act, or build…"
            spellCheck={false}
            aria-label="Command bar"
          />
        </div>
        <div className="bar-controls">
          <button
            type="button"
            className="bar-add"
            onClick={() => openScope(2)}
            aria-label="Add — build a new capability"
            title="Add — build a new capability"
          >
            +
          </button>
          <button
            type="button"
            className="bar-scope"
            onClick={() => (scopeOpen ? setScopeOpen(false) : openScope(0))}
            aria-expanded={scopeOpen}
            aria-haspopup="listbox"
          >
            scope ▾
          </button>
          <span className="bar-proj">{projectName}</span>
          {focused ? <span className="key hot">Enter</span> : <span className="key">⌘K</span>}
          <button
            type="button"
            className="chip-act"
            onClick={onDispatch}
            disabled={routing || !value.trim()}
          >
            {routing ? 'Routing…' : 'Run'}
          </button>
        </div>
        {scopeMenu.shown && (
          <div
            className={`resolver${scopeMenu.exiting ? ' exit' : ''}`}
            role="listbox"
            aria-label="Scope"
          >
            <div className="resolver-header">Scope — one prompt, three lanes</div>
            {SCOPES.map((s, i) => (
              <div
                key={s.id}
                className={`resolver-row ${i === scopeIdx ? 'active' : ''}`}
                role="option"
                aria-selected={i === scopeIdx}
                onMouseEnter={() => setScopeIdx(i)}
                onClick={() => pickScope(s)}
              >
                <span className="lbar" aria-hidden="true" />
                <span className={laneDotClass(s.lane, hintLane === s.lane)} aria-hidden="true" />
                <span className="label">
                  {s.id}
                  <span className="dim"> · {s.desc}</span>
                </span>
                <span className="count">{s.lane}</span>
                {i === scopeIdx && <span className="key hot">Enter</span>}
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  )
}
