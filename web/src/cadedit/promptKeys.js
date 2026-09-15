/**
 * W4e slice H: the command line's prompt grammar, in the reference's own
 * vocabulary (a VERB, then "Specify …:" steps; words only, nothing copied).
 * A tool listed here ARMS on click and the command line prompts for its
 * operands; Enter (or Run) fires the same create/applyEdit the button used
 * to fire directly. A tool absent here (delete) has no operands and runs on
 * click. Field: [inputKey, label, inputMode, wide]; the accessible name is
 * `ribbon <key>` (with legacy names for existing descriptive labels), so every existing row
 * still finds its field once the command is armed.
 */
// Keep positional labels stable for input parsing; shown is display-only copy.
const shownField = (key, label, shown) => Object.assign([key, label], { shown })

export const PROMPTS = Object.freeze({
  createBlock: { verb: 'BLOCK', steps: [
    { ask: 'Select objects to add:', fields: [['members', 'members', 'edge']], pickKeys: ['membersDone'] },
    { ask: 'Specify base point:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
    { ask: 'Enter new block name:', fields: [['name', 'block name', 'text']] },
  ] },
  createMleader: { verb: 'MLEADER', steps: [
    { ask: 'Specify leader arrowhead location:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
    { ask: 'Specify leader landing location:', fields: [shownField('x2', 'x2', 'next point x'), shownField('y2', 'y2', 'next point y')] },
    { ask: 'Enter text:', fields: [['text', 'text', 'text']] },
    { ask: 'Multileader style <Standard>:', fields: [['style', 'style', 'text']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  group: { verb: 'GROUP', steps: [
    { ask: 'Select objects to add:', fields: [['members', 'members', 'edge']], pickKeys: ['membersDone'] },
    { ask: 'Enter group name:', fields: [['groupName', 'group name', 'text']] },
  ] },
  ungroup: { verb: 'UNGROUP', steps: [
    { ask: 'Enter group name:', fields: [['groupName', 'group name', 'text']] },
  ] },
  createLine: { verb: 'LINE', steps: [
    { ask: 'Specify first point:', fields: [shownField('x', 'x', 'first point x'), shownField('y', 'y', 'first point y')] },
    { ask: 'Specify next point:', fields: [shownField('x2', 'x2', 'next point x'), shownField('y2', 'y2', 'next point y')] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createPolyline: { verb: 'PLINE', steps: [
    { ask: 'Specify points (x,y pairs):', fields: [['pts', 'points', 'text', true]] },
    { ask: 'Close:', fields: [['closed', 'closed', 'checkbox']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createCircle: { verb: 'CIRCLE', steps: [
    { ask: 'Specify center point:', fields: [shownField('x', 'x', 'center x'), shownField('y', 'y', 'center y')] },
    { ask: 'Specify radius:', fields: [shownField('r', 'r', 'radius')] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createArc: { verb: 'ARC', steps: [
    { ask: 'Specify center:', fields: [shownField('x', 'x', 'center x'), shownField('y', 'y', 'center y')] },
    { ask: 'Specify radius:', fields: [shownField('r', 'r', 'radius')] },
    { ask: 'Specify start angle:', fields: [shownField('a0', 'start', 'start angle')] },
    { ask: 'Specify end angle:', fields: [shownField('a1', 'end', 'end angle')] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  // W4g-5d: the reference's TEXT asks where, how tall, which way, then what.
  createText: { verb: 'TEXT', steps: [
    { ask: 'Specify start point of text:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
    { ask: 'Specify height:', fields: [['height', 'height']] },
    { ask: 'Specify rotation angle of text:', fields: [['rot', 'rotation']] },
    { ask: 'Enter text:', fields: [['text', 'text', 'text']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  // W4g-4b: the reference's POINT, ELLIPSE (centre, axis endpoint, then the
  // minor-to-major ratio typed) and MATCHPROP (the destination picked).
  createPoint: { verb: 'POINT', steps: [
    { ask: 'Specify a point:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createEllipse: { verb: 'ELLIPSE', steps: [
    { ask: 'Specify center of ellipse:', fields: [shownField('x', 'x', 'center x'), shownField('y', 'y', 'center y')] },
    { ask: 'Specify endpoint of axis:', fields: [shownField('x2', 'x2', 'next point x'), shownField('y2', 'y2', 'next point y')] },
    { ask: 'Specify ratio (minor to major, 0 to 1):', fields: [['ratio', 'ratio']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  matchprop: { verb: 'MATCHPROP', steps: [
    { ask: 'Select destination object:', fields: [['edge', 'edge', 'edge']], pickKeys: ['edge', 'ex', 'ey'] },
  ] },
  // W4g-7b-02c: the reference's INSERT: a block name (with a datalist of the
  // catalogue's complete definitions), the insertion point, the X and Y
  // scale factors and the rotation (each a DEFAULT when left empty, never a
  // waiting step: 'decimal-default'), then the layer.
  createInsert: { verb: 'INSERT', steps: [
    { ask: 'Enter block name:', fields: [['name', 'block name', 'text']] },
    { ask: 'Specify insertion point:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
    { ask: 'Enter X scale factor <1>:', fields: [['sx', 'x scale', 'decimal-default']] },
    { ask: 'Enter Y scale factor <use X scale factor>:', fields: [['sy', 'y scale', 'decimal-default']] },
    { ask: 'Specify rotation angle <0>:', fields: [['rot', 'rotation', 'decimal-default']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  // W4g-7b-04c: DIMLINEAR/DIMALIGNED, the reference's own prompts; ALIGNED
  // has no rotation step (its dimension line always runs parallel to
  // def1-def2, never a typed axis).
  dimLinear: { verb: 'DIMLINEAR', steps: [
    { ask: 'Specify first extension line origin:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
    { ask: 'Specify second extension line origin:', fields: [shownField('x2', 'x2', 'next point x'), shownField('y2', 'y2', 'next point y')] },
    { ask: 'Specify dimension line location:', fields: [shownField('dx', 'dx', 'dimension line x'), shownField('dy', 'dy', 'dimension line y')] },
    { ask: 'Specify rotation angle <0>:', fields: [['rot', 'rotation', 'decimal-default']] },
    { ask: 'Dimension style <Standard>:', fields: [['style', 'style', 'text']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  dimAligned: { verb: 'DIMALIGNED', steps: [
    { ask: 'Specify first extension line origin:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
    { ask: 'Specify second extension line origin:', fields: [shownField('x2', 'x2', 'next point x'), shownField('y2', 'y2', 'next point y')] },
    { ask: 'Specify dimension line location:', fields: [shownField('dx', 'dx', 'dimension line x'), shownField('dy', 'dy', 'dimension line y')] },
    { ask: 'Dimension style <Standard>:', fields: [['style', 'style', 'text']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createRectangle: { verb: 'RECTANG', steps: [
    { ask: 'Specify first corner point:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
    { ask: 'Specify other corner point:', fields: [shownField('x2', 'x2', 'next point x'), shownField('y2', 'y2', 'next point y')] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  move: { verb: 'MOVE', steps: [{ ask: 'Specify displacement:', fields: [shownField('dx', 'dx', 'displacement x'), shownField('dy', 'dy', 'displacement y')] }] },
  // W4g-4: the reference's verbs, in its words.
  copy: { verb: 'COPY', steps: [{ ask: 'Specify displacement:', fields: [shownField('dx', 'dx', 'displacement x'), shownField('dy', 'dy', 'displacement y')] }] },
  mirror: { verb: 'MIRROR', steps: [
    { ask: 'Specify first point of mirror line:', fields: [shownField('x1', 'x1', 'first point x'), shownField('y1', 'y1', 'first point y')] },
    { ask: 'Specify second point of mirror line:', fields: [shownField('x2', 'x2', 'next point x'), shownField('y2', 'y2', 'next point y')] },
    { ask: 'Keep source:', fields: [['keep', 'keep source', 'checkbox']] },
  ] },
  rotate: { verb: 'ROTATE', steps: [
    { ask: 'Specify base point:', fields: [shownField('cx', 'cx', 'base point x'), shownField('cy', 'cy', 'base point y')] },
    { ask: 'Specify rotation angle:', fields: [['deg', 'angle']] },
  ] },
  scale: { verb: 'SCALE', steps: [
    { ask: 'Specify base point:', fields: [shownField('cx', 'cx', 'base point x'), shownField('cy', 'cy', 'base point y')] },
    { ask: 'Specify scale factor:', fields: [['factor', 'factor']] },
  ] },
  // W4g-5: the reference asks for the distance, then a point on the side the
  // copy goes; the click IS the side, so there is no third step.
  // W4g-6: the intersection verbs. The second entity is an `edge` field the
  // canvas click fills (its id, and the point clicked on it); the last step
  // is the point on the selection that says which part to remove, extend
  // or keep.
  trim: { verb: 'TRIM', steps: [
    { ask: 'Select cutting edge:', fields: [['edge', 'edge', 'edge']], pickKeys: ['edge', 'ex', 'ey', 'etol'] },
    { ask: 'Select object to trim (point on the part to remove):', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
  ] },
  extend: { verb: 'EXTEND', steps: [
    { ask: 'Select boundary edge:', fields: [['edge', 'edge', 'edge']], pickKeys: ['edge', 'ex', 'ey', 'etol'] },
    { ask: 'Select object to extend (point near the end):', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
  ] },
  fillet: { verb: 'FILLET', steps: [
    { ask: 'Enter fillet radius:', fields: [['r', 'radius']] },
    { ask: 'Select second object:', fields: [['edge', 'edge', 'edge'], ['ex', 'edge x'], ['ey', 'edge y']], pickKeys: ['edge', 'ex', 'ey', 'etol'] },
    { ask: 'Point on the first object, on the part to keep:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
  ] },
  chamfer: { verb: 'CHAMFER', steps: [
    { ask: 'Enter first chamfer distance:', fields: [['d1', 'first distance']] },
    { ask: 'Enter second chamfer distance:', fields: [['d2', 'second distance']] },
    { ask: 'Select second line:', fields: [['edge', 'edge', 'edge'], ['ex', 'edge x'], ['ey', 'edge y']], pickKeys: ['edge', 'ex', 'ey', 'etol'] },
    { ask: 'Point on the first line, on the part to keep:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
  ] },
  offset: { verb: 'OFFSET', steps: [
    { ask: 'Specify offset distance:', fields: [['dist', 'distance']] },
    { ask: 'Specify point on side to offset:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
  ] },
  // W4g-5b: the reference asks for the grid, then the spacing; the polar
  // form asks how many and how far round. The count INCLUDES the source in
  // both, which is what a drafter means by "4 copies around a circle".
  arrayRect: { verb: 'ARRAYRECT', steps: [
    { ask: 'Enter number of rows and columns:', fields: [['rows', 'rows', 'numeric'], ['cols', 'columns', 'numeric']] },
    { ask: 'Specify distance between rows and columns:', fields: [['rowGap', 'row spacing'], ['colGap', 'column spacing']] },
  ] },
  arrayPolar: { verb: 'ARRAYPOLAR', steps: [
    { ask: 'Specify centre point of array:', fields: [shownField('cx', 'cx', 'center x'), shownField('cy', 'cy', 'center y')] },
    { ask: 'Enter number of items, including the source:', fields: [['count', 'items', 'numeric']] },
    { ask: 'Specify angle to fill:', fields: [['totalDeg', 'angle to fill']] },
  ] },
  // W4g-5c: PASTE asks where to put it. The record's anchor (a centre for
  // a circle or an arc, the first vertex otherwise) lands on this point.
  pasteClip: { verb: 'PASTE', steps: [
    { ask: 'Specify insertion point:', fields: [shownField('x', 'x', 'point x'), shownField('y', 'y', 'point y')] },
  ] },
  moveVertex: { verb: 'MOVE VERTEX', steps: [
    { ask: 'Specify vertex:', fields: [['vertexIndex', 'vertex', 'numeric']] },
    { ask: 'Specify displacement:', fields: [shownField('dx', 'dx', 'displacement x'), shownField('dy', 'dy', 'displacement y')] },
  ] },
  addVertex: { verb: 'ADD VERTEX', steps: [
    { ask: 'Insert after vertex:', fields: [['vertexIndex', 'vertex', 'numeric']] },
    { ask: 'Specify offset:', fields: [shownField('dx', 'dx', 'displacement x'), shownField('dy', 'dy', 'displacement y')] },
  ] },
  deleteVertex: { verb: 'DELETE VERTEX', steps: [{ ask: 'Specify vertex:', fields: [['vertexIndex', 'vertex', 'numeric']] }] },
  setLayer: { verb: 'SET LAYER', steps: [{ ask: 'Specify layer:', fields: [['layer', 'set layer', 'text']] }] },
  // W4g-7b-03c: colour, linetype and lineweight, typed on the command line
  // (the ribbon's three combos run at once instead, with no prompt).
  setColor: { verb: 'COLOR', steps: [{ ask: 'Enter new color <ByLayer>:', fields: [['aci', 'color', 'text']] }] },
  setLinetype: { verb: 'LINETYPE', steps: [{ ask: 'Enter new linetype name <ByLayer>:', fields: [['linetype', 'linetype', 'text']] }] },
  setLineweight: { verb: 'LWEIGHT', steps: [{ ask: 'Enter new lineweight <ByLayer>:', fields: [['lineweight', 'lineweight', 'text']] }] },
})

const refusalPatterns = new WeakMap()
const refusalWordCharacter = /[\p{L}\p{N}_]/u

function refusalPattern(prompt) {
  const cached = refusalPatterns.get(prompt)
  if (cached) return cached
  const protectedSpans = []
  const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  for (const step of prompt.steps) {
    for (const field of step.fields) {
      if (/\s/.test(field[1])) protectedSpans.push(field[1])
      if (field.shown && /\s/.test(field.shown)) protectedSpans.push(field.shown)
    }
  }
  protectedSpans.sort((a, b) => b.length - a.length)
  for (let i = 0; i < protectedSpans.length; i += 1) {
    protectedSpans[i] = `${escape(protectedSpans[i])}(?![\\p{L}\\p{N}_])`
  }
  // This literal teaches point-entry grammar, not field names; preserve it whole.
  // Complete quoted inputs and existing labels also stay byte-identical.
  const boundedSpans = protectedSpans.length ? protectedSpans.join('|') : '(?!)'
  const spans = new RegExp(`(${escape('x,y, @dx,dy, dist<angle or @dist<angle')}|"[^"]*?")|(${boundedSpans})|([A-Za-z_][A-Za-z0-9_]*(?![\\p{L}\\p{N}_]))`, 'gu')
  refusalPatterns.set(prompt, spans)
  return spans
}

/** Replace only whole input-key tokens with this prompt's shown labels. */
export function humanizeRefusal(sentence, prompt) {
  if (!sentence || !prompt) return sentence
  const spans = refusalPattern(prompt)
  spans.lastIndex = 0
  let result = ''
  let copiedThrough = 0
  let match
  while ((match = spans.exec(sentence)) !== null) {
    const [token, unboundedSpan, , keyToken] = match
    const index = match.index
    // Check the leading boundary in code for browsers without lookbehind.
    if (unboundedSpan === undefined && index > 0 && refusalWordCharacter.test(sentence[index - 1])) {
      spans.lastIndex = index + 1
      continue
    }
    let replacement = token
    if (keyToken !== undefined) {
      findField: for (const step of prompt.steps) {
        for (const field of step.fields) {
          if (field[0] === token) {
            replacement = field.shown ?? field[1]
            break findField
          }
        }
      }
    }
    result += sentence.slice(copiedThrough, index) + replacement
    copiedThrough = spans.lastIndex
  }
  return result + sentence.slice(copiedThrough)
}

/** All prompt input keys, including canvas pick state; no provider or ribbon dependency. */
export function promptKeys(op) {
  const prompt = Object.prototype.hasOwnProperty.call(PROMPTS, op) ? PROMPTS[op] : null
  return new Set(prompt ? prompt.steps.flatMap((step) => [...step.fields.map(([key]) => key), ...(step.pickKeys || [])]) : [])
}
