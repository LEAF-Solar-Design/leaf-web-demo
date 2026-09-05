// W4f slice B: the command line's typed COMMAND WORDS. The reference (and
// every CAD a drafter has used) takes LINE, L, CIRCLE, C, MOVE, M ... typed on
// the command line, Enter, and the prompt starts. Our Command bar is the
// natural-language input first, so a typed word is a command ONLY when the
// whole text is exactly one known word (optionally prefixed with ">", the
// explicit command marker); a sentence, a slash tool, or anything else falls
// through to the normal dispatch untouched. Pure, bounded, allocation-free on
// the miss path; the consumer (CommandLineArmer) turns the record into an
// armed prompt or a direct edit.
export const COCKPIT_COMMAND_EVENT = 'cockpit:command'
export const MAX_COMMAND_CHARS = 32

// word -> { group, op, verb }. `op` is the engine session op the ribbon's
// PROMPTS table (draw/modify) or OPS (delete) knows; `verb` is the reference's
// command name, shown back to the user.
const WORDS = Object.freeze({
  line: { group: 'draw', op: 'createLine', verb: 'LINE' },
  l: { group: 'draw', op: 'createLine', verb: 'LINE' },
  pline: { group: 'draw', op: 'createPolyline', verb: 'PLINE' },
  pl: { group: 'draw', op: 'createPolyline', verb: 'PLINE' },
  polyline: { group: 'draw', op: 'createPolyline', verb: 'PLINE' },
  circle: { group: 'draw', op: 'createCircle', verb: 'CIRCLE' },
  c: { group: 'draw', op: 'createCircle', verb: 'CIRCLE' },
  arc: { group: 'draw', op: 'createArc', verb: 'ARC' },
  a: { group: 'draw', op: 'createArc', verb: 'ARC' },
  move: { group: 'modify', op: 'move', verb: 'MOVE' },
  m: { group: 'modify', op: 'move', verb: 'MOVE' },
  // W4g-4: the reference's Modify verbs the crate carries, and RECTANG.
  copy: { group: 'modify', op: 'copy', verb: 'COPY' },
  co: { group: 'modify', op: 'copy', verb: 'COPY' },
  cp: { group: 'modify', op: 'copy', verb: 'COPY' },
  mirror: { group: 'modify', op: 'mirror', verb: 'MIRROR' },
  mi: { group: 'modify', op: 'mirror', verb: 'MIRROR' },
  rotate: { group: 'modify', op: 'rotate', verb: 'ROTATE' },
  ro: { group: 'modify', op: 'rotate', verb: 'ROTATE' },
  scale: { group: 'modify', op: 'scale', verb: 'SCALE' },
  sc: { group: 'modify', op: 'scale', verb: 'SCALE' },
  explode: { group: 'modify', op: 'explode', verb: 'EXPLODE' },
  x: { group: 'modify', op: 'explode', verb: 'EXPLODE' },
  // W4g-5: the reference's OFFSET, one letter like the rest of its Modify row.
  offset: { group: 'modify', op: 'offset', verb: 'OFFSET' },
  o: { group: 'modify', op: 'offset', verb: 'OFFSET' },
  // W4g-5b: the reference splits ARRAY into ARRAYRECT and ARRAYPOLAR, and
  // so do we, because the two take different operands. AR is the
  // rectangular one, as it is in AutoCAD after the default option.
  array: { group: 'modify', op: 'arrayRect', verb: 'ARRAYRECT' },
  arrayrect: { group: 'modify', op: 'arrayRect', verb: 'ARRAYRECT' },
  ar: { group: 'modify', op: 'arrayRect', verb: 'ARRAYRECT' },
  arraypolar: { group: 'modify', op: 'arrayPolar', verb: 'ARRAYPOLAR' },
  pa: { group: 'modify', op: 'arrayPolar', verb: 'ARRAYPOLAR' },
  // W4g-5d: the reference's TEXT, one letter like the rest of its Draw row.
  text: { group: 'draw', op: 'createText', verb: 'TEXT' },
  t: { group: 'draw', op: 'createText', verb: 'TEXT' },
  // W4g-5c: the reference's clipboard commands, in its own words.
  copyclip: { group: 'clipboard', op: 'copyClip', verb: 'COPYCLIP' },
  cutclip: { group: 'clipboard', op: 'cutClip', verb: 'CUTCLIP' },
  pasteclip: { group: 'clipboard', op: 'pasteClip', verb: 'PASTECLIP' },
  rectang: { group: 'draw', op: 'createRectangle', verb: 'RECTANG' },
  rectangle: { group: 'draw', op: 'createRectangle', verb: 'RECTANG' },
  rec: { group: 'draw', op: 'createRectangle', verb: 'RECTANG' },
  erase: { group: 'modify', op: 'delete', verb: 'ERASE' },
  e: { group: 'modify', op: 'delete', verb: 'ERASE' },
  delete: { group: 'modify', op: 'delete', verb: 'ERASE' },
  del: { group: 'modify', op: 'delete', verb: 'ERASE' },
  // W4f slice F: the engine's own history (a bytes-snapshot stack), never
  // the console's version undo.
  undo: { group: 'modify', op: 'undo', verb: 'UNDO' },
  u: { group: 'modify', op: 'undo', verb: 'UNDO' },
  redo: { group: 'modify', op: 'redo', verb: 'REDO' },
})

export const COMMAND_WORDS = Object.freeze(Object.keys(WORDS))

/**
 * Parse the Command bar text as a drawing command. Returns a frozen
 * { group, op, verb, word } or null. Exact single-token match only (case-
 * insensitive), with an optional leading ">" and surrounding whitespace;
 * text longer than MAX_COMMAND_CHARS is never a command (a pasted paragraph
 * costs one length check, not a regex over 16 MB).
 */
export function parseDrawingCommand(text) {
  if (typeof text !== 'string' || text.length === 0 || text.length > MAX_COMMAND_CHARS) return null
  let s = text.trim()
  if (s.startsWith('>')) s = s.slice(1).trim()
  if (!s || /\s/.test(s)) return null
  const hit = WORDS[s.toLowerCase()]
  return hit ? Object.freeze({ ...hit, word: s }) : null
}
