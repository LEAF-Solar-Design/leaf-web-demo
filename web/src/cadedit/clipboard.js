// W4g-5c: the session clipboard, CUT / COPY / PASTE. Like OFFSET this needs
// NO new engine operation: a copied entity is a record of its own geometry,
// and pasting it is one create at a base point through the path the Draw
// group already uses. So one paste costs exactly one engine round trip, and
// the clipboard itself never touches the document.
//
// It holds a RECORD, not bytes and not a live entity. A live entity would go
// stale the moment the document is edited (handles and indices move under an
// edit, and the entity list is replaced wholesale on every apply), and bytes
// would make a paste a document merge rather than a create. The record is a
// plain frozen object, so what a drafter copied is exactly what they paste
// however much the drawing changes in between, including into a DIFFERENT
// drawing, which is the honest behaviour of every clipboard they already use.
//
// Fail-closed and bounded by contract: an unsupported kind, a degenerate
// geometry and a non-finite base point are REFUSALS with the sentence the
// drafter reads, never a silently wrong paste.

/** The most vertices one clipboard record may carry. The store's own create
 *  bound, so a record that copies could always be pasted. */
export const MAX_CLIPBOARD_POINTS = 1000

/** The kinds a record is defined for; everything else refuses on COPY, which
 *  is the honest moment to say so rather than at the paste. */
const COPYABLE = new Set(['LINE', 'LWPOLYLINE', 'POLYLINE', 'CIRCLE', 'ARC'])

const finite = (v) => typeof v === 'number' && Number.isFinite(v)

function vertices2(entity) {
  const raw = Array.isArray(entity?.vertices) ? entity.vertices : []
  const out = []
  for (const v of raw) {
    if (!Array.isArray(v) || !finite(v[0]) || !finite(v[1])) return null
    out.push([v[0], v[1]])
  }
  return out
}

/**
 * The ANCHOR of a record: the point a paste puts on the base point. The
 * centre for a circle or an arc, the first vertex otherwise, which is what a
 * drafter means by "paste it here" for each kind.
 */
export function anchorOf(record) {
  if (!record) return null
  if (record.type === 'CIRCLE' || record.type === 'ARC') return [record.cx, record.cy]
  const first = record.points?.[0]
  return Array.isArray(first) ? [first[0], first[1]] : null
}

/**
 * A frozen record of the entity's own geometry, or a refusal sentence. The
 * verb ('Copy' or 'Cut') only names the sentence, so a cut refuses for the
 * same reasons a copy does and refuses BEFORE anything is deleted.
 */
export function clipboardRecord(entity, verb = 'Copy') {
  const kind = String(entity?.type || '')
  if (!COPYABLE.has(kind)) {
    return { refusal: `${verb} refused: a ${kind || 'entity'} of this kind cannot go on the clipboard yet.` }
  }
  const layer = typeof entity?.layer === 'string' ? entity.layer : ''
  if (kind === 'CIRCLE' || kind === 'ARC') {
    const centre = vertices2(entity)
    const c = centre?.[0]
    if (!c) return { refusal: `${verb} refused: this entity has no centre to copy.` }
    if (!finite(entity?.radius) || entity.radius <= 0) {
      return { refusal: `${verb} refused: this entity has no usable radius.` }
    }
    if (kind === 'ARC' && !(finite(entity?.startDeg) && finite(entity?.endDeg))) {
      return { refusal: `${verb} refused: this arc has no usable angles.` }
    }
    return {
      record: Object.freeze({
        type: kind,
        layer,
        cx: c[0],
        cy: c[1],
        radius: entity.radius,
        startDeg: kind === 'ARC' ? entity.startDeg : null,
        endDeg: kind === 'ARC' ? entity.endDeg : null,
        points: null,
        closed: false,
      }),
    }
  }
  const points = vertices2(entity)
  if (!points || points.length < 2) {
    return { refusal: `${verb} refused: this entity has too little geometry to copy.` }
  }
  if (points.length > MAX_CLIPBOARD_POINTS) {
    return { refusal: `${verb} refused: this entity has more than ${MAX_CLIPBOARD_POINTS} points.` }
  }
  return {
    record: Object.freeze({
      type: kind,
      layer,
      cx: null,
      cy: null,
      radius: null,
      startDeg: null,
      endDeg: null,
      points: Object.freeze(points.map((p) => Object.freeze([p[0], p[1]]))),
      closed: kind !== 'LINE' && entity?.closed === true,
    }),
  }
}

/**
 * The create the store dispatches to paste `record` with its anchor on
 * (bx, by), in the same `{ op, inputs }` shape OFFSET returns, or a refusal.
 * The record is translated, never scaled or rotated: a paste puts the same
 * geometry somewhere else.
 */
export function pasteOp(record, bx, by) {
  if (!record) return { refusal: 'Paste refused: the clipboard is empty.' }
  if (!finite(bx) || !finite(by)) {
    return { refusal: 'Paste refused: the base point x and y must both be numbers.' }
  }
  const anchor = anchorOf(record)
  if (!anchor || !finite(anchor[0]) || !finite(anchor[1])) {
    return { refusal: 'Paste refused: the clipboard record has no anchor point.' }
  }
  const dx = bx - anchor[0]
  const dy = by - anchor[1]
  const layer = record.layer || ''
  if (record.type === 'CIRCLE') {
    return { op: 'createCircle', inputs: { x: record.cx + dx, y: record.cy + dy, r: record.radius, layer } }
  }
  if (record.type === 'ARC') {
    return {
      op: 'createArc',
      inputs: {
        x: record.cx + dx,
        y: record.cy + dy,
        r: record.radius,
        a0: record.startDeg,
        a1: record.endDeg,
        layer,
      },
    }
  }
  const moved = record.points.map((p) => [p[0] + dx, p[1] + dy])
  if (record.type === 'LINE') {
    return {
      op: 'createLine',
      inputs: { x: moved[0][0], y: moved[0][1], x2: moved[1][0], y2: moved[1][1], layer },
    }
  }
  return {
    op: 'createPolyline',
    inputs: {
      pts: moved.map((p) => `${p[0]},${p[1]}`).join(' '),
      closed: record.closed ? 'true' : 'false',
      layer,
    },
  }
}

/** What the status says a paste is about to draw, for the drafter's sake. */
export function describeRecord(record) {
  if (!record) return 'nothing'
  if (record.type === 'CIRCLE' || record.type === 'ARC') {
    return `a ${record.type.toLowerCase()} on layer ${record.layer || '0'}`
  }
  return `a ${record.type.toLowerCase()} of ${record.points.length} points on layer ${record.layer || '0'}`
}
