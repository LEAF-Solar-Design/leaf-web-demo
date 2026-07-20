// The SB3 command bar well (crib §1), docked at the bottom of the center
// column. Two rows — a 40 px input row (accent › caret, 13 px text, placeholder
// "Find, act, or build…") and a controls row inside the well: the run / solve /
// build lane scope chips with their live hit dots left; project context, the
// summon keycap (⌘K, swapping to an emphasized Enter cap while focused) and the
// Dispatch primary right. Enter dispatches (Ctrl/Cmd+Enter still works); while
// a route decision is showing above the well, Enter belongs to the resolver
// (routeActive) and typing clears the stale route (App-side).
//
// Slash commands: a leading "/" lists the runnable tool catalog as an O2
// resolver menu above the well (same anatomy as the route resolver), filtered
// live by the text after the slash — ↑/↓ move, Tab completes the name into the
// input, Enter completes AND dispatches (the route decision strip still asks
// for confirmation — paid actions never auto-execute). Esc closes just the
// menu; a space after the tool name closes it too (args mode).

import { useEffect, useMemo, useState } from 'react'

const LANES = ['run', 'solve', 'build']

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
  tools = [],
}) {
  const [focused, setFocused] = useState(false)
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

  // While a route decision is showing, the resolver owns the surface AND the
  // keys — the menu stands down entirely (typing clears the route App-side,
  // which brings the menu straight back).
  const menuOpen = completing && !menuDismissed && !routeActive

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
      if (routeActive) return // the resolver / decision strip owns Enter
      e.preventDefault()
      onDispatch()
    }
  }

  return (
    <div className="bar">
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
        {LANES.map((l) => (
          <span key={l} className="bar-scope">
            <span className={laneDotClass(l, hintLane === l)} aria-hidden="true" />
            {l}
          </span>
        ))}
        <span className="bar-proj">{projectName}</span>
        {focused ? <span className="key hot">Enter</span> : <span className="key">⌘K</span>}
        <button
          type="button"
          className="primary"
          onClick={() => onDispatch()}
          disabled={routing || !value.trim()}
        >
          {routing ? 'Routing…' : 'Dispatch'}
        </button>
      </div>
    </div>
  )
}
