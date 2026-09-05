// THE ELEMENT IDENTITY SCHEME (standardization slice 9a). One DOM attribute,
// `data-element-id="<kind>:<id>"`, applied ADDITIVELY beside whatever
// selector or `data-*` attribute a surface already carries. Nothing here
// replaces an existing hook; this is the one new vocabulary the right-click
// ContextMenu (slice 9b) and any future consumer read to answer "what did
// the user just point at".
//
// Naming law: the neutral attribute name (`data-element-id`, not a
// mushy/cadwalk/claudewalk term) is deliberate — see the slice 9/10 spec.
//
// FAIL CLOSED, by construction: `formatElementId` returns null instead of a
// malformed string, and `parseElementId` returns null instead of throwing.
// A caller that forgets to check the return value gets `undefined` on a JSX
// attribute (React omits it) rather than a broken id in the DOM.

// The frozen kind vocabulary. Every kind this slice's render sites use:
//   tool      ribbon tools (beside `data-tool`) and board "Built tools" tiles
//   version   board "Versions" tiles (backend version_id)
//   job       board "Jobs" tiles (backend job_id)
//   family    board "Catalog" tiles (backend family_id)
//   rung      the iOS device-stage ship-lane rungs (fixed ids: revision,
//             readiness, build — real and stable, not backend-issued)
//   turn      a converse turn (turnId)
//   approval  a pending/decided approval chip (confirmation_id)
//   item      a feed item that itself carries a real id (item.id: proposal /
//             confirm rows only — feed rows keyed by index alone get none)
//   entity    the drawing canvas's current selection (a WebGL entity has no
//             DOM node of its own, so this rides the viewer wrapper instead)
export const ELEMENT_KINDS = Object.freeze([
  'tool', 'version', 'job', 'family', 'rung', 'turn', 'approval', 'item', 'entity',
])

const KIND_SET = new Set(ELEMENT_KINDS)

// Bounded by construction (build-doctrine: an unbounded id is a record
// nobody wrote). 128 chars comfortably covers a uuid, a DWG handle, or a
// composite `${group}:${op}` action id with room to spare.
export const MAX_ELEMENT_ID_CHARS = 128

// The id charset: alphanumerics plus the separators real backend ids and
// action ids already use (`-`, `_`, `.`), PLUS `:` — the action registry's
// own composite ids (`draw:createLine`, `clipboard:pasteClip`) are exactly
// `<group>:<op>`, and `tool:<tool id>` nests one of those as the id half of
// this scheme. That is safe because the kind/id split below only ever reads
// the FIRST colon: an id carrying further colons is still unambiguous, it
// is simply structured. No `/`, quote or whitespace: an id stays one CSS
// attribute-selector token away.
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/

function isValidId(id) {
  return typeof id === 'string' && id.length > 0 && id.length <= MAX_ELEMENT_ID_CHARS && ID_PATTERN.test(id)
}

/** Whether `kind` is one of the frozen vocabulary above. */
export function isValidElementKind(kind) {
  return typeof kind === 'string' && KIND_SET.has(kind)
}

/**
 * Build a `data-element-id` value, or `null` when either half is malformed.
 * Never throws: a render site spreads the result straight into JSX
 * (`data-element-id={formatElementId('tool', id) || undefined}`), so a bad
 * id renders as an ABSENT attribute, never a broken one.
 */
export function formatElementId(kind, id) {
  if (!isValidElementKind(kind)) return null
  if (!isValidId(String(id ?? ''))) return null
  return `${kind}:${id}`
}

/**
 * Parse a `data-element-id` value back into `{ kind, id }`, or `null` when
 * it is malformed: no colon, an unregistered kind, or an id outside the
 * bounded charset. The split is on the FIRST colon only, so a structured id
 * (`tool:draw:createLine` -> kind `tool`, id `draw:createLine`) round-trips
 * exactly, never over-split.
 */
export function parseElementId(value) {
  if (typeof value !== 'string' || value.length === 0) return null
  const at = value.indexOf(':')
  if (at <= 0 || at === value.length - 1) return null
  const kind = value.slice(0, at)
  const id = value.slice(at + 1)
  if (!isValidElementKind(kind)) return null
  if (!isValidId(id)) return null
  return { kind, id }
}

/**
 * The nearest ancestor (inclusive) carrying a well-formed `data-element-id`,
 * starting from `target`. Returns `null` on anything that is not a real
 * element, on no match, or on a match whose value fails `parseElementId`
 * (a malformed attribute is treated as absent, never surfaced as a partial
 * match) — the fail-closed contract a global event-delegation handler
 * (slice 9b's ContextMenu) needs, since it never controls what `event.target`
 * will be.
 */
export function closestElementIdentity(target) {
  if (!target || typeof target.closest !== 'function') return null
  const el = target.closest('[data-element-id]')
  if (!el) return null
  const parsed = parseElementId(el.getAttribute('data-element-id'))
  if (!parsed) return null
  return { ...parsed, element: el }
}
