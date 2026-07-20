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
//
// Slash commands: a leading "/" lists the runnable tool catalog as an O2
// resolver menu above the well (same anatomy as the route resolver), filtered
// live by the text after the slash — ↑/↓ move, Tab completes the name into the
// input, Enter completes AND dispatches (the route decision strip still asks
// for confirmation — paid actions never auto-execute). Esc closes just the
// menu; a space after the tool name closes it too (args mode).

import { useEffect, useMemo, useRef, useState } from 'react'
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
  tools = [],
}) {
  const [focused, setFocused] = useState(false)
  const [scopeOpen, setScopeOpen] = useState(false)
  const [scopeIdx, setScopeIdx] = useState(0)
  const [dragging, setDragging] = useState(false)
  const [dropErr, setDropErr] = useState(null)
  const dragDepth = useRef(0) // dragenter/leave pair counter (children re-fire them)
  const rootRef = useRef(null)
  const scopeMenu = useExit(scopeOpen) // 180 ms M1 exit fade
  const [menuIdx, setMenuIdx] = useState(0)
  const [menuDismissed, setMenuDismissed] = useState(false)

  // "/..." with no space yet = completing a tool name; a space after the name
  // means the user moved on to args, so the menu stands down.
  const afterSlash = value.startsWith('/') ? value.slice(1) : null
  const completing = afterSlash != null && !/\s/.test(afterSlash)

  // Name-prefix matches rank first (the Tab target reads left-to-right), then
  // name/description substring matches — case-insensitive, like Claude's picker.
  const matches = useMemo(() => {
    if (!completing) return []
    const q = afterSlash.toLowerCase()
    const pre = []
    const sub = []
    for (const t of tools) {
      const name = (t.name || '').toLowerCase()
      if (name.startsWith(q)) pre.push(t)
      else if (name.includes(q) || (t.description || '').toLowerCase().includes(q)) sub.push(t)
    }
    return [...pre, ...sub]
  }, [completing, afterSlash, tools])

  // While a route decision or the scope resolver is showing, that surface owns
  // the keys — the menu stands down entirely (typing clears the route App-side,
  // which brings the menu straight back). Gate on scopeMenu.shown, NOT scopeOpen:
  // the scope resolver stays mounted for its 180 ms exit fade after scopeOpen
  // flips false, and two listboxes must never be visible at once.
  const menuOpen = completing && !menuDismissed && !routeActive && !scopeMenu.shown

  // Any edit re-arms a dismissed menu and re-anchors the highlight.
  useEffect(() => { setMenuDismissed(false); setMenuIdx(0) }, [value])
  const idx = Math.min(menuIdx, Math.max(0, matches.length - 1))

  // Tab: complete the name into the input (trailing space closes the menu and
  // starts args mode). Enter: complete and hand off to dispatch in one act.
  const complete = (t) => onChange(`/${t.name} `)
  const pick = (t) => {
    onChange(`/${t.name} `)
    onDispatch(`/${t.name}`)
  }

  const onKeyDown = (e) => {
    if (menuOpen && !e.isComposing) { // IME candidate navigation keeps its keys
      if (e.key === 'ArrowDown' && matches.length > 0) {
        e.preventDefault(); setMenuIdx(Math.min(idx + 1, matches.length - 1)); return
      }
      if (e.key === 'ArrowUp' && matches.length > 0) {
        e.preventDefault(); setMenuIdx(Math.max(idx - 1, 0)); return
      }
      if (e.key === 'Tab' && matches[idx]) {
        e.preventDefault(); complete(matches[idx]); return
      }
      if (e.key === 'Escape') {
        // closes ONLY the menu — the global Esc ladder must not also fire
        e.preventDefault(); e.stopPropagation(); setMenuDismissed(true); return
      }
      if (e.key === 'Enter' && matches[idx]) {
        e.preventDefault(); pick(matches[idx]); return
      }
    }
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
        {menuOpen && (
          <div className="resolver slash-menu" id="slash-menu-listbox" role="listbox" aria-label="Tool commands">
            <div className="resolver-header">
              {matches.length > 0
                ? <>Tools · Tab completes · Enter picks — you still confirm before it runs</>
                : <>No tool matches “/{afterSlash}” — keep typing, or Esc to close</>}
            </div>
            {matches.map((t, i) => {
              const isWrite = (t.capabilities || []).includes('drawing.write')
              return (
                <div
                  key={t.name}
                  id={`slash-opt-${i}`}
                  className={`resolver-row ${i === idx ? 'active' : ''}`}
                  role="option"
                  aria-selected={i === idx}
                  onMouseEnter={() => setMenuIdx(i)}
                  onMouseDown={(e) => e.preventDefault()} // keep the input focused
                  onClick={() => pick(t)}
                >
                  <span className="lbar" aria-hidden="true" />
                  <span className={isWrite ? 'dot square' : 'dot'} aria-hidden="true" />
                  <span className="label">
                    <span className="route-tool">/{t.name}</span>
                    {t.description && <span className="dim"> · {t.description}</span>}
                  </span>
                  <span className="count">{isWrite ? 'write' : 'read'}</span>
                  {i === idx && <span className="key hot">Tab</span>}
                </div>
              )
            })}
          </div>
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
            placeholder="Find, act, or build… ( / for tools)"
            spellCheck={false}
            aria-label="Command bar"
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={menuOpen}
            aria-controls={menuOpen ? 'slash-menu-listbox' : undefined}
            aria-activedescendant={menuOpen && matches[idx] ? `slash-opt-${idx}` : undefined}
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
