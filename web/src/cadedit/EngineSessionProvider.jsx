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
 */
import { createContext, useCallback, useContext, useMemo, useState } from 'react'

import { useDrawingIdentityOptional } from '../drawing/DrawingIdentityProvider.jsx'

import useEngineSession from './engineSession.js'

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
})

const INPUT_KEYS = new Set(Object.keys(DEFAULT_EDIT_INPUTS))
// A typed delta or a layer name never needs more; a paste of a whole file
// into the dx field costs a slice, not a render of 16 MB of text. The point
// list is the one field that legitimately runs long.
export const MAX_INPUT_CHARS = 64
export const MAX_POINT_LIST_CHARS = 4096
const INPUT_LIMITS = Object.freeze({ pts: MAX_POINT_LIST_CHARS })

export default function EngineSessionProvider({
  createWorker = defaultCreateWorker,
  saveTarget = null,
  onSaved = null,
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
    setInputs((current) => (
      current[key] === bounded ? current : Object.freeze({ ...current, [key]: bounded })
    ))
  }, [])

  const canSave = saveTarget !== null && saveTarget !== undefined
  const value = useMemo(
    () => ({ session, inputs, setInput, canSave }),
    [session, inputs, setInput, canSave],
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
