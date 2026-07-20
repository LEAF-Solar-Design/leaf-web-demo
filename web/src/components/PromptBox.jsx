// The SB3 command bar well (crib §1), docked at the bottom of the center
// column. Two rows — a 40 px input row (accent › caret, 13 px text, placeholder
// "Find, act, or build…") and a controls row inside the well: the run / solve /
// build lane scope chips with their live hit dots left; project context, the
// summon keycap (⌘K, swapping to an emphasized Enter cap while focused) and the
// Dispatch primary right. Enter dispatches (Ctrl/Cmd+Enter still works); while
// a route decision is showing above the well, Enter belongs to the resolver
// (routeActive) and typing clears the stale route (App-side).

import { useState } from 'react'

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
}) {
  const [focused, setFocused] = useState(false)
  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      if (routeActive) return // the resolver / decision strip owns Enter
      e.preventDefault()
      onDispatch()
    }
  }
  return (
    <div className="bar">
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
          onClick={onDispatch}
          disabled={routing || !value.trim()}
        >
          {routing ? 'Routing…' : 'Dispatch'}
        </button>
      </div>
    </div>
  )
}
