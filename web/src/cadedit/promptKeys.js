/**
 * W4e slice H: the command line's prompt grammar, in the reference's own
 * vocabulary (a VERB, then "Specify …:" steps; words only, nothing copied).
 * A tool listed here ARMS on click and the command line prompts for its
 * operands; Enter (or Run) fires the same create/applyEdit the button used
 * to fire directly. A tool absent here (delete) has no operands and runs on
 * click. Field: [inputKey, label, inputMode, wide]; the accessible name is
 * `ribbon <label>`, the locator contract since W4d, so every existing row
 * still finds its field once the command is armed.
 */
export const PROMPTS = Object.freeze({
  createBlock: { verb: 'BLOCK', steps: [
    { ask: 'Select objects to add:', fields: [['members', 'members', 'edge']], pickKeys: ['membersDone'] },
    { ask: 'Specify base point:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Enter new block name:', fields: [['name', 'block name', 'text']] },
  ] },
  createMleader: { verb: 'MLEADER', steps: [
    { ask: 'Specify leader arrowhead location:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify leader landing location:', fields: [['x2', 'x2'], ['y2', 'y2']] },
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
    { ask: 'Specify first point:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify next point:', fields: [['x2', 'x2'], ['y2', 'y2']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createPolyline: { verb: 'PLINE', steps: [
    { ask: 'Specify points (x,y pairs):', fields: [['pts', 'points', 'text', true]] },
    { ask: 'Close:', fields: [['closed', 'closed', 'checkbox']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createCircle: { verb: 'CIRCLE', steps: [
    { ask: 'Specify center point:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify radius:', fields: [['r', 'r']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createArc: { verb: 'ARC', steps: [
    { ask: 'Specify center:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify radius:', fields: [['r', 'r']] },
    { ask: 'Specify start angle:', fields: [['a0', 'start']] },
    { ask: 'Specify end angle:', fields: [['a1', 'end']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  // W4g-5d: the reference's TEXT asks where, how tall, which way, then what.
  createText: { verb: 'TEXT', steps: [
    { ask: 'Specify start point of text:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify height:', fields: [['height', 'height']] },
    { ask: 'Specify rotation angle of text:', fields: [['rot', 'rotation']] },
    { ask: 'Enter text:', fields: [['text', 'text', 'text']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  // W4g-4b: the reference's POINT, ELLIPSE (centre, axis endpoint, then the
  // minor-to-major ratio typed) and MATCHPROP (the destination picked).
  createPoint: { verb: 'POINT', steps: [
    { ask: 'Specify a point:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createEllipse: { verb: 'ELLIPSE', steps: [
    { ask: 'Specify center of ellipse:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify endpoint of axis:', fields: [['x2', 'x2'], ['y2', 'y2']] },
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
    { ask: 'Specify insertion point:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Enter X scale factor <1>:', fields: [['sx', 'x scale', 'decimal-default']] },
    { ask: 'Enter Y scale factor <use X scale factor>:', fields: [['sy', 'y scale', 'decimal-default']] },
    { ask: 'Specify rotation angle <0>:', fields: [['rot', 'rotation', 'decimal-default']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  // W4g-7b-04c: DIMLINEAR/DIMALIGNED, the reference's own prompts; ALIGNED
  // has no rotation step (its dimension line always runs parallel to
  // def1-def2, never a typed axis).
  dimLinear: { verb: 'DIMLINEAR', steps: [
    { ask: 'Specify first extension line origin:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify second extension line origin:', fields: [['x2', 'x2'], ['y2', 'y2']] },
    { ask: 'Specify dimension line location:', fields: [['dx', 'dx'], ['dy', 'dy']] },
    { ask: 'Specify rotation angle <0>:', fields: [['rot', 'rotation', 'decimal-default']] },
    { ask: 'Dimension style <Standard>:', fields: [['style', 'style', 'text']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  dimAligned: { verb: 'DIMALIGNED', steps: [
    { ask: 'Specify first extension line origin:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify second extension line origin:', fields: [['x2', 'x2'], ['y2', 'y2']] },
    { ask: 'Specify dimension line location:', fields: [['dx', 'dx'], ['dy', 'dy']] },
    { ask: 'Dimension style <Standard>:', fields: [['style', 'style', 'text']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  createRectangle: { verb: 'RECTANG', steps: [
    { ask: 'Specify first corner point:', fields: [['x', 'x'], ['y', 'y']] },
    { ask: 'Specify other corner point:', fields: [['x2', 'x2'], ['y2', 'y2']] },
    { ask: 'Layer:', fields: [['layer', 'layer', 'text']] },
  ] },
  move: { verb: 'MOVE', steps: [{ ask: 'Specify displacement:', fields: [['dx', 'dx'], ['dy', 'dy']] }] },
  // W4g-4: the reference's verbs, in its words.
  copy: { verb: 'COPY', steps: [{ ask: 'Specify displacement:', fields: [['dx', 'dx'], ['dy', 'dy']] }] },
  mirror: { verb: 'MIRROR', steps: [
    { ask: 'Specify first point of mirror line:', fields: [['x1', 'x1'], ['y1', 'y1']] },
    { ask: 'Specify second point of mirror line:', fields: [['x2', 'x2'], ['y2', 'y2']] },
    { ask: 'Keep source:', fields: [['keep', 'keep source', 'checkbox']] },
  ] },
  rotate: { verb: 'ROTATE', steps: [
    { ask: 'Specify base point:', fields: [['cx', 'cx'], ['cy', 'cy']] },
    { ask: 'Specify rotation angle:', fields: [['deg', 'angle']] },
  ] },
  scale: { verb: 'SCALE', steps: [
    { ask: 'Specify base point:', fields: [['cx', 'cx'], ['cy', 'cy']] },
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
    { ask: 'Select object to trim (point on the part to remove):', fields: [['x', 'x'], ['y', 'y']] },
  ] },
  extend: { verb: 'EXTEND', steps: [
    { ask: 'Select boundary edge:', fields: [['edge', 'edge', 'edge']], pickKeys: ['edge', 'ex', 'ey', 'etol'] },
    { ask: 'Select object to extend (point near the end):', fields: [['x', 'x'], ['y', 'y']] },
  ] },
  fillet: { verb: 'FILLET', steps: [
    { ask: 'Enter fillet radius:', fields: [['r', 'radius']] },
    { ask: 'Select second object:', fields: [['edge', 'edge', 'edge'], ['ex', 'edge x'], ['ey', 'edge y']], pickKeys: ['edge', 'ex', 'ey', 'etol'] },
    { ask: 'Point on the first object, on the part to keep:', fields: [['x', 'x'], ['y', 'y']] },
  ] },
  chamfer: { verb: 'CHAMFER', steps: [
    { ask: 'Enter first chamfer distance:', fields: [['d1', 'first distance']] },
    { ask: 'Enter second chamfer distance:', fields: [['d2', 'second distance']] },
    { ask: 'Select second line:', fields: [['edge', 'edge', 'edge'], ['ex', 'edge x'], ['ey', 'edge y']], pickKeys: ['edge', 'ex', 'ey', 'etol'] },
    { ask: 'Point on the first line, on the part to keep:', fields: [['x', 'x'], ['y', 'y']] },
  ] },
  offset: { verb: 'OFFSET', steps: [
    { ask: 'Specify offset distance:', fields: [['dist', 'distance']] },
    { ask: 'Specify point on side to offset:', fields: [['x', 'x'], ['y', 'y']] },
  ] },
  // W4g-5b: the reference asks for the grid, then the spacing; the polar
  // form asks how many and how far round. The count INCLUDES the source in
  // both, which is what a drafter means by "4 copies around a circle".
  arrayRect: { verb: 'ARRAYRECT', steps: [
    { ask: 'Enter number of rows and columns:', fields: [['rows', 'rows', 'numeric'], ['cols', 'columns', 'numeric']] },
    { ask: 'Specify distance between rows and columns:', fields: [['rowGap', 'row spacing'], ['colGap', 'column spacing']] },
  ] },
  arrayPolar: { verb: 'ARRAYPOLAR', steps: [
    { ask: 'Specify centre point of array:', fields: [['cx', 'cx'], ['cy', 'cy']] },
    { ask: 'Enter number of items, including the source:', fields: [['count', 'items', 'numeric']] },
    { ask: 'Specify angle to fill:', fields: [['totalDeg', 'angle to fill']] },
  ] },
  // W4g-5c: PASTE asks where to put it. The record's anchor (a centre for
  // a circle or an arc, the first vertex otherwise) lands on this point.
  pasteClip: { verb: 'PASTE', steps: [
    { ask: 'Specify insertion point:', fields: [['x', 'x'], ['y', 'y']] },
  ] },
  moveVertex: { verb: 'MOVE VERTEX', steps: [
    { ask: 'Specify vertex:', fields: [['vertexIndex', 'vertex', 'numeric']] },
    { ask: 'Specify displacement:', fields: [['dx', 'dx'], ['dy', 'dy']] },
  ] },
  addVertex: { verb: 'ADD VERTEX', steps: [
    { ask: 'Insert after vertex:', fields: [['vertexIndex', 'vertex', 'numeric']] },
    { ask: 'Specify offset:', fields: [['dx', 'dx'], ['dy', 'dy']] },
  ] },
  deleteVertex: { verb: 'DELETE VERTEX', steps: [{ ask: 'Specify vertex:', fields: [['vertexIndex', 'vertex', 'numeric']] }] },
  setLayer: { verb: 'SET LAYER', steps: [{ ask: 'Specify layer:', fields: [['layer', 'set layer', 'text']] }] },
  // W4g-7b-03c: colour, linetype and lineweight, typed on the command line
  // (the ribbon's three combos run at once instead, with no prompt).
  setColor: { verb: 'COLOR', steps: [{ ask: 'Enter new color <ByLayer>:', fields: [['aci', 'color', 'text']] }] },
  setLinetype: { verb: 'LINETYPE', steps: [{ ask: 'Enter new linetype name <ByLayer>:', fields: [['linetype', 'linetype', 'text']] }] },
  setLineweight: { verb: 'LWEIGHT', steps: [{ ask: 'Enter new lineweight <ByLayer>:', fields: [['lineweight', 'lineweight', 'text']] }] },
})

/** All prompt input keys, including canvas pick state; no provider or ribbon dependency. */
export function promptKeys(op) {
  const prompt = Object.prototype.hasOwnProperty.call(PROMPTS, op) ? PROMPTS[op] : null
  return new Set(prompt ? prompt.steps.flatMap((step) => [...step.fields.map(([key]) => key), ...(step.pickKeys || [])]) : [])
}
