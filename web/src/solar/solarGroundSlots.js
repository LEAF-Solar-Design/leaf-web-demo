// Linear in total slots plus template nodes (and text bytes). Decode each column once;
// no whole-column re-encoding per slot or intermediate string per byte. Template traversal
// is iterative. Numeric lexemes lost by JSON.parse remain server-authoritative.
export const GROUND_SLOT_CODEC = 'leaf.solar-ground-slots.v1'
export const MAX_FRAME_SLOTS = 10_000
export const MAX_GRAPH_SLOTS = 100_000
export const GROUND_SLOT_ROW_BYTES = 16
export const MAX_SLOT_TEMPLATE_REV = 1_000_000
export const MAX_SLOT_TEMPLATE_BYTES = 16 * 1024 * 1024
export const MAX_SLOT_TEMPLATE_NODES = 500_000
export const MAX_SLOT_TEMPLATE_DEPTH = 32

const MAX_CONTAINER_ITEMS = 100_000
const plain = (value) => value !== null && typeof value === 'object' &&
  [Object.prototype, null].includes(Object.getPrototypeOf(value))
const exactKeys = (value, keys) => plain(value) && Object.keys(value).length === keys.length &&
  keys.every((key) => Object.hasOwn(value, key))
const finite = (value) => typeof value === 'number' && Number.isFinite(value)
const HEX = Array.from({ length: 256 }, (_, byte) => byte.toString(16).padStart(2, '0'))

// Count UTF-8 bytes without replacement-encoding lone surrogates or allocating a byte buffer.
function utf8Size(value) {
  let size = 0
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index)
    if (code < 0x80) size += 1
    else if (code < 0x800) size += 2
    else if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(index + 1)
      if (!(next >= 0xdc00 && next <= 0xdfff)) return null
      index += 1
      size += 4
    } else if (code >= 0xdc00 && code <= 0xdfff) return null
    else size += 3
    if (size > MAX_SLOT_TEMPLATE_BYTES) return null
  }
  return size
}

function boundedTemplate(panel) {
  const stack = [[panel, 0]]
  let nodes = 0
  let size = 0
  while (stack.length) {
    const [value, depth] = stack.pop()
    nodes += 1
    if (nodes > MAX_SLOT_TEMPLATE_NODES || depth > MAX_SLOT_TEMPLATE_DEPTH) return false
    if (plain(value)) {
      const keys = Object.keys(value)
      if (keys.length > MAX_CONTAINER_ITEMS || Object.getOwnPropertySymbols(value).length) return false
      for (const key of keys) stack.push([key, depth + 1], [value[key], depth + 1])
    } else if (Array.isArray(value)) {
      if (value.length > MAX_CONTAINER_ITEMS) return false
      for (let index = 0; index < value.length; index += 1) stack.push([value[index], depth + 1])
    } else if (typeof value === 'string') {
      const bytes = utf8Size(value)
      if (bytes === null) return false
      size += bytes
    } else if (typeof value === 'number') {
      if (!finite(value)) return false
      // JS cannot distinguish a large Python int from a finite Python float.
      size += 24
    } else if (value === null || typeof value === 'boolean') size += 5
    else return false
    if (size > MAX_SLOT_TEMPLATE_BYTES) return false
  }
  return true
}

function preflight(block) {
  if (!exactKeys(block, ['codec', 'count', 'panel_ids', 'centres', 'angle', 'panel']) ||
      block.codec !== GROUND_SLOT_CODEC || !Number.isInteger(block.count) ||
      block.count < 1 || block.count > MAX_FRAME_SLOTS) return false
  const length = Math.floor((block.count * GROUND_SLOT_ROW_BYTES + 2) / 3) * 4
  if (typeof block.panel_ids !== 'string' || block.panel_ids.length !== length ||
      typeof block.centres !== 'string' || block.centres.length !== length || !finite(block.angle)) return false
  const panel = block.panel
  return exactKeys(panel, ['rev', 'provenance', 'validity', 'extra']) &&
    Number.isInteger(panel.rev) && panel.rev >= 0 && panel.rev <= MAX_SLOT_TEMPLATE_REV &&
    plain(panel.provenance) && plain(panel.validity) && plain(panel.extra) && boundedTemplate(panel)
}

function decodeColumn(text, count) {
  // atob accepts whitespace; reject it and non-standard alphabets before decoding.
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(text)) return null
  try {
    const bytes = globalThis.atob(text)
    return bytes.length === count * GROUND_SLOT_ROW_BYTES && globalThis.btoa(bytes) === text ? bytes : null
  } catch { return null }
}

function decodeRows(block, seen) {
  const ids = decodeColumn(block.panel_ids, block.count)
  if (ids === null) return null
  const panelIds = []
  for (let start = 0; start < ids.length; start += GROUND_SLOT_ROW_BYTES) {
    if ((ids.charCodeAt(start + 6) & 0xf0) !== 0x40 ||
        (ids.charCodeAt(start + 8) & 0xc0) !== 0x80) return null
    let hex = ''
    for (let index = start; index < start + GROUND_SLOT_ROW_BYTES; index += 1) hex += HEX[ids.charCodeAt(index)]
    const id = `leaf:panel:${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
    if (seen.has(id)) return null
    seen.add(id)
    panelIds.push(id)
  }
  const centres = decodeColumn(block.centres, block.count)
  if (centres === null) return null
  const buffer = new Uint8Array(centres.length)
  for (let index = 0; index < centres.length; index += 1) buffer[index] = centres.charCodeAt(index)
  const view = new DataView(buffer.buffer)
  const slots = []
  for (let index = 0; index < block.count; index += 1) {
    const x = view.getFloat64(index * GROUND_SLOT_ROW_BYTES, true)
    const y = view.getFloat64(index * GROUND_SLOT_ROW_BYTES + 8, true)
    if (!finite(x) || !finite(y)) return null
    slots.push({ panelId: panelIds[index], slotNumber: index + 1, centre: { x, y } })
  }
  return { angle: block.angle, slots, panelIds }
}

export function decodeGroundSlots(block) {
  return preflight(block) ? decodeRows(block, new Set()) : null
}

export function decodeGroundFrameSlots(frames) {
  if (!Array.isArray(frames)) return null
  const selected = []
  let count = 0
  for (let frameIndex = 0; frameIndex < frames.length; frameIndex += 1) {
    const frame = frames[frameIndex]
    if (!plain(frame) || !Object.hasOwn(frame, 'ground_slots')) continue
    if (!preflight(frame.ground_slots)) return null
    count += frame.ground_slots.count
    if (count > MAX_GRAPH_SLOTS) return null
    selected.push({ frame, frameIndex })
  }
  const result = { frames: [], panelIds: [] }
  const seen = new Set()
  for (const { frame, frameIndex } of selected) {
    const decoded = decodeRows(frame.ground_slots, seen)
    if (decoded === null) return null
    result.frames.push({ frameIndex, frameId: typeof frame.id === 'string' ? frame.id : null,
      angle: decoded.angle, slots: decoded.slots })
    for (const id of decoded.panelIds) result.panelIds.push(id)
  }
  return result
}
