/**
 * Drawing identity — the pure seeding rules behind DrawingIdentityProvider
 * (convergence W1, docs/convergence/ACCEPTANCE.md "Route and boot-state
 * matrix" + "Scope-reset contract").
 *
 * Kept free of React and of `window` so every rule below is testable as a
 * function of its inputs, and so ONE reading of the search string can serve
 * both consumers the matrix names: the boot decision (site/authBoot.js
 * bootWantsApp) and the drawing selection (here). The matrix's `?demo` note is
 * precisely about a second, drifting reading; `classifyDemo` exists so there
 * is only ever one.
 *
 * Two names, deliberately distinct, because the console needs both:
 *   * `source`    — the INTAKE SOURCE name a surface asks the API for
 *                   (App's DRAWING_SOURCE: what `?drawing=` names).
 *   * `drawingId` — the STORE drawing id that same surface addresses
 *                   (App's REQUESTED_DRAWING_ID: `rooftop_demo` -> `demo`).
 * On the operator stage the two have always been the same value — the stage
 * never applied the store mapping — and this module preserves that exactly.
 *
 * `origin` is provenance, never an id: which matrix rule produced this
 * identity. It is what lets a consumer tell a boot seed from an upload
 * promotion from a scope reset without re-deriving anything.
 *
 * HARDENING CONTRACT: every entry point is total — malformed search strings,
 * missing receipts and absent storage all resolve to the EMPTY identity
 * rather than throwing or fabricating a drawing. No allocation beyond the
 * returned frozen record; no I/O; callers inject storage and auth reads.
 *
 * DELIBERATELY NOT VALIDATED: the raw `?drawing=` value is passed through
 * exactly as App has always passed it. The server's tenant id validator
 * (server/tenant_id_validator.py) is the authority on what is addressable;
 * narrowing it here would silently change which deep links work.
 */
import { storeDrawingIdForSource } from '../controllers/checkout/createCheckoutController.js'

export const DRAWING_MODE_CONSOLE = 'console'
export const DRAWING_MODE_OPERATOR = 'operator'

// The console's intake source when `?drawing=` names none. This is App.jsx's
// former DEFAULT_DRAWING_ID, moved rather than copied.
export const CONSOLE_DEFAULT_SOURCE = 'rooftop_demo'

// Which matrix rule produced the current identity.
export const IDENTITY_ORIGIN = Object.freeze({
  EMPTY: 'empty',       // no drawing — an empty browser session has none
  QUERY: 'query',       // `?drawing=` named it
  MODE: 'mode',         // the mode's live/demo/proof constant selected it
  STORED: 'stored',     // a previous upload remembered it for this session
  UPLOAD: 'upload',     // an upload receipt promoted it in this session
  RESET: 'reset',       // a tenant/project scope change cleared it
})

export const EMPTY_DRAWING_IDENTITY = Object.freeze({
  drawingId: null,
  source: null,
  origin: IDENTITY_ORIGIN.EMPTY,
})

export const RESET_DRAWING_IDENTITY = Object.freeze({
  drawingId: null,
  source: null,
  origin: IDENTITY_ORIGIN.RESET,
})

function frozenIdentity(drawingId, source, origin) {
  return Object.freeze({ drawingId, source, origin })
}

// URLSearchParams does not throw on a malformed search today, but this module
// must be total for any string a URL bar can carry: fail closed to "no param".
function param(search, key) {
  try {
    return new URLSearchParams(search || '').get(key)
  } catch {
    return null
  }
}

/**
 * The `?drawing=` reading, with App's exact falsiness: an EMPTY `?drawing=`
 * is not a request (App's `get('drawing') || DEFAULT_DRAWING_ID`), even
 * though bootWantsApp's `q.has('drawing')` still routes it to the console.
 */
export function readDrawingParam(search) {
  return param(search, 'drawing') || null
}

/**
 * The ONE `?demo` reading (matrix note: "one reading must serve both
 * consumers"). Mirrors SiteRoot's and ToolCast's long-standing rule exactly:
 * `?demo=1` signed out is the fully local public demo; `?demo=1` signed in,
 * or `?demo=tour`, is the live tour on the same surface.
 */
export function classifyDemo(search, signedIn) {
  const value = param(search, 'demo')
  return Object.freeze({
    value,
    publicDemo: value === '1' && !signedIn,
    liveDemo: value === 'tour' || (value === '1' && !!signedIn),
  })
}

/**
 * The proof-surface reading (`VITE_CAT_PROOF=1` or `?proof=1`), passed in
 * from the build env so this module stays env-free and testable.
 */
export function classifyProof(search, envProof) {
  return envProof === '1' || param(search, 'proof') === '1'
}

/**
 * The drawing a MODE selects on its own, before storage or an upload. Null
 * means "this mode names no drawing" — the empty operator session, which
 * must never be filled in with a fabricated id (site/workbenchId.js).
 *
 * The operator arm is SiteRoot's INITIAL_OPERATOR_DRAWING_ID and ToolCast's
 * MODE_DRAWING_ID, which were the same ladder written twice.
 */
export function modeDrawingId({ mode, proofMode = false, publicDemo = false, liveDemo = false } = {}) {
  if (mode === DRAWING_MODE_CONSOLE) return CONSOLE_DEFAULT_SOURCE
  if (proofMode) return 'cat-panels'
  if (publicDemo) return 'demo'
  if (liveDemo) return 'rooftop_demo'
  return null
}

/**
 * Seeding order, straight off the frozen route matrix:
 *   1. `?drawing=` (which the matrix also routes to mode `console`)
 *   2. the mode's live/demo/proof constant
 *   3. (operator only) the drawing a previous upload remembered for this
 *      browser session
 *   4. nothing — the empty identity
 *
 * The console applies the store mapping (`rooftop_demo` -> `demo`) to both
 * (1) and (2), exactly as App's module constants did. The operator stage
 * applies no mapping, exactly as SiteRoot's state did.
 */
export function seedDrawingIdentity({
  mode = DRAWING_MODE_OPERATOR,
  search = '',
  proofMode = false,
  publicDemo = false,
  liveDemo = false,
  liveId = null,
} = {}) {
  const requested = readDrawingParam(search)
  if (requested) {
    // A named drawing is addressed by the store id the console's checkout,
    // version chain and save path all use.
    return frozenIdentity(storeDrawingIdForSource(requested), requested, IDENTITY_ORIGIN.QUERY)
  }

  const selected = modeDrawingId({ mode, proofMode, publicDemo, liveDemo })
  if (mode === DRAWING_MODE_CONSOLE) {
    return frozenIdentity(storeDrawingIdForSource(selected), selected, IDENTITY_ORIGIN.MODE)
  }
  if (selected) return frozenIdentity(selected, selected, IDENTITY_ORIGIN.MODE)
  if (liveId) return frozenIdentity(liveId, liveId, IDENTITY_ORIGIN.STORED)
  return EMPTY_DRAWING_IDENTITY
}

/**
 * The upload promotion (SiteRoot's promoteOperatorDrawing). A receipt with no
 * drawing id promotes NOTHING — the caller keeps the identity it has, which
 * is what the old early `return` did.
 */
export function identityFromUploadReceipt(receipt) {
  const drawingId = typeof receipt?.drawing_id === 'string' ? receipt.drawing_id : ''
  if (!drawingId) return null
  return frozenIdentity(drawingId, drawingId, IDENTITY_ORIGIN.UPLOAD)
}

/**
 * Scope-reset predicate (ACCEPTANCE "Scope-reset contract"). A SWITCH is a
 * move away from a project that was already open — opening the first project
 * of a session is not one, and must not clear a drawing the user just
 * uploaded into it. Closing a project (id -> null) IS a scope exit.
 */
export function isScopeSwitch(previousProjectId, nextProjectId) {
  const previous = previousProjectId || null
  const next = nextProjectId || null
  return previous != null && previous !== next
}
