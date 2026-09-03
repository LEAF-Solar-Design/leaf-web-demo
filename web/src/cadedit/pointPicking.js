// W4f slice A1: canvas point picking for the command line's prompts, as pure
// rules. A prompted op has a PICK SEQUENCE (what a click on the drawing
// means, in order): a point writes two operand keys, a radius writes r as the
// distance from the picked centre, a delta writes dx/dy as the vector from a
// base point, an append adds "x,y" to the point list. `applyPick` turns one
// world click into operand writes; `ghostFor` is the rubber band from what
// was picked to the cursor. Bounded: coordinates are rounded to three
// decimals (the engine parses strings), non-finite input is refused, and a
// polyline list is capped by the store's own point-list limit downstream.
const round3 = (v) => {
  const r = Math.round(v * 1000) / 1000
  return Object.is(r, -0) ? '0' : String(r)
}
const finite = (v) => typeof v === 'number' && Number.isFinite(v)
const num = (s) => { const n = Number.parseFloat(s); return Number.isFinite(n) ? n : null }

/** The pick sequence per op, or null for ops with nothing to pick. */
export const PICK_SEQUENCES = Object.freeze({
  createLine: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'point', keys: ['x2', 'y2'] }],
  createCircle: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'radius', key: 'r', from: ['x', 'y'] }],
  createArc: [{ kind: 'point', keys: ['x', 'y'] }, { kind: 'radius', key: 'r', from: ['x', 'y'] }],
  createPolyline: [{ kind: 'append', key: 'pts' }],
  move: [{ kind: 'base' }, { kind: 'delta', keys: ['dx', 'dy'] }],
  moveVertex: [{ kind: 'base' }, { kind: 'delta', keys: ['dx', 'dy'] }],
  addVertex: [{ kind: 'base' }, { kind: 'delta', keys: ['dx', 'dy'] }],
})

/**
 * Fresh pick state for an armed op: which step is next, plus the points
 * picked so far. W4f-3: a chain point `from` ([x, y]) answers the first
 * point step up front (LINE's next segment starts where the last one ended),
 * so the sequence opens at step 1 with that point picked and the rubber band
 * runs from it; a non-finite point, or an op whose first step is not a
 * point, opens normally.
 */
export function startPicking(op, from = null) {
  const sequence = PICK_SEQUENCES[op] || null
  const chained = !!(sequence && sequence[0].kind === 'point' && Array.isArray(from) && finite(from[0]) && finite(from[1]))
  return chained
    ? { op, sequence, step: 1, picked: [[from[0], from[1]]], base: null }
    : { op, sequence, step: 0, picked: [], base: null }
}

/** The step the next click answers, or null when the sequence is done (an append repeats forever). */
export function currentStep(state) {
  if (!state?.sequence) return null
  const { sequence, step } = state
  if (step < sequence.length) return sequence[step]
  const last = sequence[sequence.length - 1]
  return last.kind === 'append' ? last : null
}

/**
 * One click at world (x, y) -> { state, writes: [[key, value], ...] }.
 * Refuses non-finite input (no writes, same state). `inputs` is the operator
 * record (strings), read for the current polyline list on an append.
 */
export function applyPick(state, x, y, inputs = {}) {
  const step = currentStep(state)
  if (!step || !finite(x) || !finite(y)) return { state, writes: [] }
  const picked = [...state.picked, [x, y]]
  const next = { ...state, picked, step: state.step + 1 }
  if (step.kind === 'point') return { state: next, writes: [[step.keys[0], round3(x)], [step.keys[1], round3(y)]] }
  if (step.kind === 'radius') {
    const [cx, cy] = state.picked[state.picked.length - 1] || [num(inputs[step.from[0]]) ?? 0, num(inputs[step.from[1]]) ?? 0]
    const r = Math.hypot(x - cx, y - cy)
    if (r <= 0) return { state, writes: [] }
    return { state: next, writes: [[step.key, round3(r)]] }
  }
  if (step.kind === 'base') return { state: { ...next, base: [x, y] }, writes: [] }
  if (step.kind === 'delta') {
    const base = state.base || [0, 0]
    return { state: next, writes: [[step.keys[0], round3(x - base[0])], [step.keys[1], round3(y - base[1])]] }
  }
  if (step.kind === 'append') {
    const current = String(inputs[step.key] || '').trim()
    // The first click REPLACES the default list (the operator is drawing
    // this polyline, not extending the sample); later clicks append.
    const list = picked.length === 1 ? `${round3(x)},${round3(y)}` : `${current} ${round3(x)},${round3(y)}`.trim()
    return { state: next, writes: [[step.key, list]] }
  }
  return { state, writes: [] }
}

/** The rubber band for the cursor at world (x, y): [[x,y],...] plus closed, or null. */
export function ghostFor(state, x, y) {
  if (!state?.sequence || !finite(x) || !finite(y)) return null
  const { op, picked, base } = state
  const last = picked[picked.length - 1]
  if (op === 'createCircle' || op === 'createArc') {
    if (!last) return null
    const r = Math.hypot(x - last[0], y - last[1])
    if (r <= 0) return null
    const n = 48
    const pts = new Array(n)
    for (let i = 0; i < n; i += 1) {
      const a = (i / n) * Math.PI * 2
      pts[i] = [last[0] + r * Math.cos(a), last[1] + r * Math.sin(a)]
    }
    return { pts, closed: true }
  }
  if (op === 'createPolyline') {
    if (!picked.length) return null
    return { pts: [...picked, [x, y]], closed: false }
  }
  if (op === 'createLine') {
    if (!last || picked.length >= 2) return null
    return { pts: [last, [x, y]], closed: false }
  }
  if (base) return { pts: [base, [x, y]], closed: false }
  return null
}

/** True when the sequence still wants a click (an append always does). */
export function wantsPick(state) {
  return currentStep(state) !== null
}
