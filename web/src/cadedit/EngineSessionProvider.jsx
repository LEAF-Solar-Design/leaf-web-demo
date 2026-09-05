/**
 * EngineSessionProvider — the ONE mount of the engine session (W4d Slice A).
 *
 * Before this, CadEditSurface both named the engine worker path and called
 * useEngineSession, so the import pane was the engine's only consumer. The
 * ribbon's Modify group needs the same session, and a second
 * useEngineSession call is a second worker, which engineSession.js forbids.
 * So the ONE call lives here, and every cockpit surface (the import pane,
 * the ribbon's engine clusters, later the dock's engine-truth line)
 * CONSUMES it through context, exactly the shape the frozen contract names
 * (docs/convergence/ACCEPTANCE.md, "Engine-session ownership": "The
 * PropertiesDock and every other cockpit surface CONSUME that store").
 *
 * LICENSE FENCE (docs/CAD-ENGINE-LICENSE-FENCE.md, deny rule 3): this file
 * is now the ONE non-test module under web/src that names the engine worker
 * path, in the one legal spawn shape. CadEditSurface dropped its literal;
 * engineSession.js still takes the factory as an injected argument and never
 * names the path. web/src/cadedit/engineOwnership.test.js counts the spawn
 * shape and the boundary construction BY SHAPE, so "exactly one" holds by
 * construction after the move rather than by naming this file.
 *
 * FLAG: App mounts this behind ENV_CAD_EDIT as the FIRST operand, the same
 * call-site contract CadEditSurface carries, so a flag-off build folds this
 * module, engineSession.js and the worker chunk it spawns out of the bundle
 * (web/src/cadedit/bundleFence.test.js is the oracle).
 *
 * OPERATOR INPUTS (dx, dy, vertex, layer) live here too, as ONE bounded
 * record: the ribbon's Modify group and the import pane's fields must agree
 * on what was typed. They are UI state, not session state, so they sit
 * beside the store rather than inside it, and every write is type- and
 * length-checked (fails closed: an unknown key or a non-string is dropped).
 *
 * THE ARMED COMMAND (W4e slice H) sits beside them for the same reason: the
 * ribbon tool the command line is prompting for is part of what the
 * operator is doing, not of the document, and it must outlive the ribbon's
 * tab remounts. Bounded the same way (a group from the fixed pair and a
 * short op token, or null), and it disarms itself when the document goes.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { useDrawingIdentityOptional } from '../drawing/DrawingIdentityProvider.jsx'

import useEngineSession, { SESSION_ERROR } from './engineSession.js'

const EngineSessionContext = createContext(null)

function defaultCreateWorker() {
  // The one legal spawn shape, and the only place this repo's web tree names
  // the engine worker's path (license fence deny rule 3). It stays at the
  // ONE mount: the store takes this factory as an argument so there is
  // exactly one such site to bless, never two.
  return new Worker(
    new URL('../../../vendor/acadrust-worker/worker-browser.mjs', import.meta.url),
    { type: 'module' },
  )
}

export const DEFAULT_EDIT_INPUTS = Object.freeze({
  // Modify operands.
  dx: '10', dy: '0', vertexIndex: '0', layer: '',
  // Draw operands (W4d Slice B): a point, a second point, a radius, an
  // angle span in degrees, a point list, and the closed flag as a string
  // (every input is a string; the store parses and refuses).
  x: '0', y: '0', x2: '100', y2: '0', r: '10', a0: '0', a1: '90', pts: '0,0 100,0 100,50', closed: 'false',
  // W4g-4 Modify verbs: the mirror line's two points and the keep flag, the
  // base point, the rotation angle (degrees) and the scale factor.
  x1: '0', y1: '0', keep: 'true', cx: '0', cy: '0', deg: '90', factor: '2',
  // W4g-5 OFFSET: how far the parallel copy sits from its source; the SIDE
  // is the point clicked (x, y), so the verb needs no side operand of its own.
  dist: '5',
  // W4g-5b ARRAY: a 2 x 3 grid ten apart, and four items round a full
  // turn. Every default is a command the store accepts, so a fresh
  // prompt shows no refusal sentence.
  rows: '2', cols: '3', rowGap: '10', colGap: '10', count: '4', totalDeg: '360',
  // W4g-5d TEXT: a readable height, no rotation, a value the store accepts.
  height: '2.5', rot: '0', text: 'TEXT',
  // W4g-6: the second entity an intersection verb names (its id and the
  // point clicked on it), empty until picked so the step waits; the
  // chamfer distances default to the reference's D1=0 D2=0 (a corner).
  edge: '', ex: '', ey: '', d1: '0', d2: '0',
  // The world aperture of the last canvas edge pick; empty for a typed edge.
  etol: '',
  // W4g-4b: ELLIPSE's minor-to-major ratio (a step still waiting until typed).
  ratio: '',
})

const INPUT_KEYS = new Set(Object.keys(DEFAULT_EDIT_INPUTS))
// A typed delta or a layer name never needs more; a paste of a whole file
// into the dx field costs a slice, not a render of 16 MB of text. The point
// list is the one field that legitimately runs long.
export const MAX_INPUT_CHARS = 64
export const MAX_POINT_LIST_CHARS = 4096
const INPUT_LIMITS = Object.freeze({ pts: MAX_POINT_LIST_CHARS })

// The two ribbon groups whose tools prompt for operands, and the op token's
// shape (a JS identifier the clusters own; the engine validates the op
// again when it runs).
// W4g-5c: the clipboard is the third engine group whose commands take a
// prompt (PASTE asks where). A group missing here is dropped SILENTLY by
// setArmed, which is the bound doing its job against an unknown group;
// the fourth proof of the clipboard slice found Paste clicked and no
// prompt opened, because this line still knew two groups.
const ARMED_GROUPS = new Set(['draw', 'modify', 'clipboard'])
const ARMED_OP = /^[a-zA-Z]{1,32}$/
const sameFrom = (a, b) => (!a && !b) || (!!a && !!b && a[0] === b[0] && a[1] === b[1])

/**
 * W4g-1b: engine reach, the state of opening the console's own drawing in
 * the engine (EngineHeadOpener writes it, the ribbon's reason ladder reads
 * it). A closed vocabulary; `sentence` is what the ribbon shows while the
 * engine holds no document.
 */
export const REACH_STATE = Object.freeze({
  IDLE: 'idle',
  OPENING: 'opening',
  OPEN: 'open',
  FAILED: 'failed',
  STALE: 'stale',
})
const REACH_STATES = new Set(Object.values(REACH_STATE))
const MAX_REACH_SENTENCE = 300
export const REACH_IDLE = Object.freeze({ state: REACH_STATE.IDLE, sentence: '' })

export default function EngineSessionProvider({
  createWorker = defaultCreateWorker,
  saveTarget = null,
  onSaved = null,
  // W4g-2 (one head): the host learns when the engine holds edits nobody
  // saved (session.dirty), so a catalog write tool that would move the
  // server head under them can be refused with the reason. Called only on
  // a change, with a boolean.
  onDirtyChange = null,
  children,
}) {
  // No identity provider means no drawing identity, which is a real state
  // (a standalone embed). With one, a drawing switch resets the session.
  const identity = useDrawingIdentityOptional()
  const session = useEngineSession({
    createWorker,
    saveTarget,
    onSaved,
    drawingId: identity?.drawingId ?? null,
  })

  const [inputs, setInputs] = useState(DEFAULT_EDIT_INPUTS)
  const setInput = useCallback((key, value) => {
    if (!INPUT_KEYS.has(key) || typeof value !== 'string') return
    const limit = INPUT_LIMITS[key] ?? MAX_INPUT_CHARS
    const bounded = value.length > limit ? value.slice(0, limit) : value
    setInputs((current) => {
      if (key === 'edge') return Object.freeze({ ...current, edge: bounded, etol: '' })
      return current[key] === bounded ? current : Object.freeze({ ...current, [key]: bounded })
    })
  }, [])

  // The armed command: null, or { group, op }. Fails closed on any other
  // shape (a consumer bug never leaves the prompt pointing at a non-command).
  const [armed, setArmedState] = useState(null)
  const setArmed = useCallback((next) => {
    if (next === null) { setArmedState(null); return }
    if (!next || typeof next !== 'object') return
    const { group, op } = next
    if (!ARMED_GROUPS.has(group) || typeof op !== 'string' || !ARMED_OP.test(op)) return
    // W4f-3: an optional chain point `from` ([x, y], two finite numbers): the
    // command continues from there (LINE's next segment starts where the
    // last one ended). Any other shape is dropped, never stored.
    const from = Array.isArray(next.from) && next.from.length === 2
      && next.from.every((v) => typeof v === 'number' && Number.isFinite(v))
      ? Object.freeze([next.from[0], next.from[1]])
      : null
    setArmedState((current) => (
      current && current.group === group && current.op === op && sameFrom(current.from, from)
        ? current
        : Object.freeze(from ? { group, op, from } : { group, op })
    ))
  }, [])
  // No parsed document (closed, or the worker died): nothing to prompt for,
  // so a stale prompt never outlives its drawing. A NEW document disarms
  // too (openDocument keeps engineParsed while it loads, so the identity is
  // the signal): opening a file cancels the running command, as in the
  // reference.
  const documentGone = !session.engineParsed || session.errorKind === SESSION_ERROR.CRASHED
  useEffect(() => { if (documentGone) setArmedState(null) }, [documentGone])
  useEffect(() => { setArmedState(null) }, [session.documentId])

  // W4f-4: the drafting mode a pick obeys. ORTHO constrains a picked point
  // (and the rubber band) to the axis of the larger delta from the last
  // point, as the reference's F8 does. A boolean, nothing else is stored.
  const [ortho, setOrthoState] = useState(false)
  const setOrtho = useCallback((next) => { setOrthoState(next === true) }, [])
  // W4f-5: OSNAP (F3) snaps a pick to the document's endpoints, midpoints
  // and centres within a pixel tolerance. W4f-7: ON by default, the
  // convention a drafter arrives with (AutoCAD's OSMODE initial value 4133
  // and BricsCAD's 4135 both leave running object snap on); the reference
  // itself starts suppressed, so this is a deliberate departure for flow,
  // decided 2026-09-04 (askall w4fnext). F3 or the prompt's chip turns it off.
  const [osnap, setOsnapState] = useState(true)
  const setOsnap = useCallback((next) => { setOsnapState(next === true) }, [])

  // W4g-1b: engine reach. Fails closed on any shape outside the vocabulary
  // (a consumer bug never puts a non-sentence on the ribbon); bounded text.
  const [reach, setReachState] = useState(REACH_IDLE)
  const setReach = useCallback((next) => {
    if (!next || typeof next !== 'object' || !REACH_STATES.has(next.state)) return
    const sentence = typeof next.sentence === 'string' ? next.sentence.slice(0, MAX_REACH_SENTENCE) : ''
    const version = Number.isInteger(next.version) && next.version > 0 ? next.version : null
    const head = Number.isInteger(next.head) && next.head > 0 ? next.head : null
    const source = typeof next.source === 'string' ? next.source.slice(0, 32) : ''
    setReachState((current) => (
      current.state === next.state && current.sentence === sentence
        && current.version === version && current.head === head && current.source === source
        ? current
        : Object.freeze({ state: next.state, sentence, version, head, source })
    ))
  }, [])

  const dirty = session.dirty === true
  const onDirtyChangeRef = useRef(onDirtyChange)
  onDirtyChangeRef.current = onDirtyChange
  useEffect(() => { onDirtyChangeRef.current?.(dirty) }, [dirty])
  // Unmount: the host must not keep a stale "dirty" over a provider that is gone.
  useEffect(() => () => { onDirtyChangeRef.current?.(false) }, [])

  const canSave = saveTarget !== null && saveTarget !== undefined
  const value = useMemo(
    () => ({ session, inputs, setInput, canSave, armed, setArmed, ortho, setOrtho, osnap, setOsnap, reach, setReach }),
    [session, inputs, setInput, canSave, armed, setArmed, ortho, setOrtho, osnap, setOsnap, reach, setReach],
  )
  return <EngineSessionContext.Provider value={value}>{children}</EngineSessionContext.Provider>
}

/**
 * The engine session, or null when no provider is mounted above. Null is an
 * honest state for a surface that can render without an engine (a flag-off
 * page, a standalone embed); a consumer that NEEDS the engine uses
 * useEngineSessionContext and fails loudly instead.
 */
export function useEngineSessionOptional() {
  return useContext(EngineSessionContext)
}

/** The engine session; throws when no provider is mounted (a wiring bug, never a silent second session). */
export function useEngineSessionContext() {
  const value = useContext(EngineSessionContext)
  if (!value) {
    throw new Error('useEngineSessionContext requires an EngineSessionProvider above it (the ONE engine-session mount)')
  }
  return value
}
