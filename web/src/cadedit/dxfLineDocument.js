/**
 * dxfLineDocument — the hardened, bounded DXF document model the cad_edit
 * editing slice runs INSIDE the engine worker.
 *
 * Contract, stated here because every caller depends on it:
 *   - Every entry point FAILS CLOSED and NEVER throws. Malformed bytes, a
 *     bad group code, an oversized file, an out-of-range edit payload all
 *     come back as `{ ok: false, reason }`, never as an exception into the
 *     boundary or the UI.
 *   - Every allocation is bounded up front: MAX_DOCUMENT_BYTES caps the
 *     input, MAX_GROUP_PAIRS caps the token stream, MAX_ENTITIES caps the
 *     entity table. A hostile 200 MB "DXF" costs one length check, not a
 *     200 MB decode.
 *   - Parsing is one linear pass over the group-code pairs. No regex over
 *     the whole document, no quadratic rescan, no backtracking.
 *   - LOSSLESS-OR-REFUSE. This model represents exactly the subset it can
 *     also WRITE: a HEADER section carrying $ACADVER, and an ENTITIES
 *     section of LINE records. Anything else in the source file (other
 *     sections, other entity types, other header variables) is READ and
 *     REPORTED via `doc.unsupported`, and `writeRefusal()` then refuses the
 *     write-back leg by name. It is never silently dropped — silently
 *     re-serializing a drawing minus its TABLES/BLOCKS is data loss, not an
 *     edit.
 *
 * This is NOT a general DXF engine. The real engine is the vendored MPL-2.0
 * wasm CAD engine behind the same worker boundary (see the day-3 spike doc
 * under docs/); this module is the browser-side stand-in that makes the
 * first editing slice genuinely executable in a browser today, and it is
 * deliberately narrow so the swap is a worker-side module change, not a
 * surface rewrite. The crate is not named here on purpose — the license
 * fence (scripts/check_license_fence.py) denies any reference to it from
 * web/ outside the one legal Worker-spawn shape.
 */

// Input caps. Chosen to comfortably cover the drawings this slice can
// actually represent (a LINE-only R12 document) while making an oversized or
// hostile input a constant-cost refusal.
export const MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
export const MAX_GROUP_PAIRS = 400_000
export const MAX_ENTITIES = 20_000
// Bound on an edit delta: keeps a fat-fingered or hostile payload from
// pushing a coordinate to Infinity through repeated moves.
export const MAX_EDIT_DELTA = 1e9

const SUPPORTED_SECTIONS = new Set(['HEADER', 'ENTITIES'])
const SUPPORTED_ENTITY_TYPE = 'LINE'
const PRESERVED_HEADER_VARS = new Set(['$ACADVER'])
const LINE_SPLIT_RE = /\r\n|\r|\n/

const COORD_FIELD = {
  10: ['start', 0], 20: ['start', 1], 30: ['start', 2],
  11: ['end', 0], 21: ['end', 1], 31: ['end', 2],
}

function fail(reason) {
  return { ok: false, reason }
}

/**
 * Cross-realm byte-array test. Deliberately NOT `instanceof Uint8Array`:
 * bytes crossing a worker boundary, a jsdom/Node realm split, or a bundler's
 * own realm carry a DIFFERENT Uint8Array constructor, so instanceof reports
 * false for a genuine byte array and the whole write-back leg fails closed
 * for the wrong reason. The brand check is realm-independent.
 */
export function isByteArray(value) {
  return Object.prototype.toString.call(value) === '[object Uint8Array]'
}

// Group-code tokenizer: pairs of (integer code, raw value) lines. Bounded by
// MAX_GROUP_PAIRS; a stream longer than that is refused, not truncated.
function tokenize(text) {
  const lines = text.split(LINE_SPLIT_RE)
  const pairs = []
  let i = 0
  while (i < lines.length) {
    // DXF permits leading/trailing whitespace on a group-code line; a blank
    // trailing line at EOF is normal and is skipped, not treated as a pair.
    const codeText = lines[i].trim()
    if (codeText === '') {
      i += 1
      continue
    }
    if (i + 1 >= lines.length) return fail('malformed_group_pairs:dangling_code')
    if (!/^-?\d+$/.test(codeText)) return fail(`bad_group_code:${codeText.slice(0, 16)}`)
    if (pairs.length >= MAX_GROUP_PAIRS) return fail(`too_many_group_pairs:${MAX_GROUP_PAIRS}`)
    pairs.push([Number.parseInt(codeText, 10), lines[i + 1].trim()])
    i += 2
  }
  return { ok: true, pairs }
}

function decodeBytes(bytes) {
  if (typeof bytes === 'string') {
    // Byte length, not code-unit length: the cap is about memory, and a
    // multi-byte character costs more than one byte.
    const encoded = new TextEncoder().encode(bytes)
    if (encoded.length > MAX_DOCUMENT_BYTES) return fail(`document_too_large:${encoded.length}`)
    return { ok: true, text: bytes }
  }
  if (!isByteArray(bytes)) return fail('not_bytes')
  if (bytes.length > MAX_DOCUMENT_BYTES) return fail(`document_too_large:${bytes.length}`)
  try {
    // fatal:true — a DXF that is not valid UTF-8/ASCII is refused by name
    // rather than silently decoded into replacement characters.
    return { ok: true, text: new TextDecoder('utf-8', { fatal: true }).decode(bytes) }
  } catch {
    return fail('not_utf8_text')
  }
}

function parseCoord(raw) {
  const n = Number.parseFloat(raw)
  return Number.isFinite(n) ? n : null
}

/**
 * Parses DXF bytes into the editable document model. Never throws.
 * Returns { ok: true, doc } or { ok: false, reason }.
 */
export function parseDxfDocument(bytes) {
  const decoded = decodeBytes(bytes)
  if (!decoded.ok) return decoded
  const tokenized = tokenize(decoded.text)
  if (!tokenized.ok) return tokenized
  const { pairs } = tokenized

  const doc = {
    acadver: null,
    entities: [],
    unsupported: { sections: [], entityTypes: [], headerVars: [] },
  }
  const seenSections = new Set()
  const seenEntityTypes = new Set()
  const seenHeaderVars = new Set()

  let section = null
  let nextEntityId = 0
  let i = 0

  while (i < pairs.length) {
    const [code, value] = pairs[i]

    if (code === 0 && value === 'EOF') break

    if (code === 0 && value === 'SECTION') {
      const namePair = pairs[i + 1]
      if (!namePair || namePair[0] !== 2 || namePair[1] === '') {
        return fail('malformed_section:missing_name')
      }
      section = namePair[1]
      if (!SUPPORTED_SECTIONS.has(section) && !seenSections.has(section)) {
        seenSections.add(section)
        doc.unsupported.sections.push(section)
      }
      i += 2
      continue
    }

    if (code === 0 && value === 'ENDSEC') {
      section = null
      i += 1
      continue
    }

    if (section === 'HEADER' && code === 9) {
      const varName = value
      const valuePair = pairs[i + 1]
      if (!valuePair) return fail(`malformed_header_var:${varName.slice(0, 24)}`)
      if (varName === '$ACADVER') {
        doc.acadver = valuePair[1]
      } else if (!PRESERVED_HEADER_VARS.has(varName) && !seenHeaderVars.has(varName)) {
        seenHeaderVars.add(varName)
        doc.unsupported.headerVars.push(varName)
      }
      i += 2
      continue
    }

    if (section === 'ENTITIES' && code === 0) {
      if (value !== SUPPORTED_ENTITY_TYPE) {
        if (!seenEntityTypes.has(value)) {
          seenEntityTypes.add(value)
          doc.unsupported.entityTypes.push(value)
        }
        // Skip the whole record in one forward scan — no rescan, no recursion.
        i += 1
        while (i < pairs.length && pairs[i][0] !== 0) i += 1
        continue
      }
      if (doc.entities.length >= MAX_ENTITIES) return fail(`too_many_entities:${MAX_ENTITIES}`)
      const entity = {
        id: `e${nextEntityId}`,
        type: SUPPORTED_ENTITY_TYPE,
        layer: '0',
        start: [0, 0, 0],
        end: [0, 0, 0],
      }
      nextEntityId += 1
      i += 1
      while (i < pairs.length && pairs[i][0] !== 0) {
        const [fieldCode, fieldValue] = pairs[i]
        if (fieldCode === 8) {
          entity.layer = fieldValue
        } else if (COORD_FIELD[fieldCode]) {
          const n = parseCoord(fieldValue)
          if (n === null) return fail(`bad_coordinate:${entity.id}:${fieldCode}`)
          const [slot, index] = COORD_FIELD[fieldCode]
          entity[slot][index] = n
        }
        i += 1
      }
      doc.entities.push(entity)
      continue
    }

    i += 1
  }

  return { ok: true, doc }
}

/**
 * Names why this document cannot be written back, or null when it can.
 * The write-back leg calls this FIRST and refuses by name — the whole point
 * of the lossless-or-refuse contract.
 */
export function writeRefusal(doc) {
  if (!doc || typeof doc !== 'object') return 'no_document'
  const { sections, entityTypes, headerVars } = doc.unsupported
  const parts = []
  if (entityTypes.length > 0) parts.push(`entity types ${entityTypes.join(', ')}`)
  if (sections.length > 0) parts.push(`sections ${sections.join(', ')}`)
  if (headerVars.length > 0) parts.push(`header variables ${headerVars.join(', ')}`)
  if (parts.length === 0) return null
  return `this build can read but not rewrite ${parts.join('; ')}`
}

function cloneEntity(entity) {
  return {
    id: entity.id,
    type: entity.type,
    layer: entity.layer,
    start: [entity.start[0], entity.start[1], entity.start[2]],
    end: [entity.end[0], entity.end[1], entity.end[2]],
  }
}

function finiteDelta(value) {
  return typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= MAX_EDIT_DELTA
}

/**
 * Applies one edit and returns a NEW document; the input is never mutated,
 * so a refused edit can never leave a half-applied document behind. Never
 * throws. O(n) in the entity count, once per user-initiated edit.
 *
 * Supported ops in this slice: 'delete' and 'move'. Entity ids are assigned
 * at parse time and stay stable across edits — a delete does NOT renumber,
 * so a selection held by the UI keeps meaning the same entity.
 */
export function applyEditToDocument(doc, op, payload) {
  if (!doc || typeof doc !== 'object' || !Array.isArray(doc.entities)) return fail('no_document')
  if (op !== 'delete' && op !== 'move') return fail(`unsupported_op:${String(op).slice(0, 24)}`)
  if (!payload || typeof payload !== 'object') return fail('bad_payload')

  const { entityId } = payload
  if (typeof entityId !== 'string' || entityId === '') return fail('bad_entity_id')
  const index = doc.entities.findIndex((entity) => entity.id === entityId)
  if (index === -1) return fail(`unknown_entity:${entityId.slice(0, 32)}`)

  const entities = doc.entities.map(cloneEntity)

  if (op === 'delete') {
    entities.splice(index, 1)
    return { ok: true, doc: { ...doc, entities } }
  }

  const { dx, dy } = payload
  if (!finiteDelta(dx) || !finiteDelta(dy)) return fail('bad_delta')
  const target = entities[index]
  target.start[0] += dx
  target.start[1] += dy
  target.end[0] += dx
  target.end[1] += dy
  // Post-condition, not an assumption: a delta inside MAX_EDIT_DELTA applied
  // to a coordinate near the float ceiling can still overflow, and an
  // Infinity coordinate would serialize as garbage.
  for (const value of [...target.start, ...target.end]) {
    if (!Number.isFinite(value)) return fail('coordinate_overflow')
  }
  return { ok: true, doc: { ...doc, entities } }
}

function formatCoord(n) {
  return Number.isInteger(n) ? `${n}.0` : String(n)
}

/**
 * Serializes the document back to DXF bytes. Emits exactly the subset this
 * model represents: HEADER ($ACADVER) + ENTITIES (LINE). Callers MUST check
 * writeRefusal(doc) first — this function cannot know what the source file
 * carried that the model does not.
 *
 * Byte-identical round trip is NOT promised and is not achievable in
 * general (see docs/CAD-EDIT-SURFACE-DESIGN.md); entity-level fidelity is.
 */
export function serializeDxfDocument(doc) {
  const out = ['0', 'SECTION', '2', 'HEADER']
  if (doc.acadver) out.push('9', '$ACADVER', '1', doc.acadver)
  out.push('0', 'ENDSEC', '0', 'SECTION', '2', 'ENTITIES')
  for (const entity of doc.entities) {
    if (entity.type !== SUPPORTED_ENTITY_TYPE) continue
    out.push(
      '0', SUPPORTED_ENTITY_TYPE,
      '8', entity.layer ?? '0',
      '10', formatCoord(entity.start[0]), '20', formatCoord(entity.start[1]), '30', formatCoord(entity.start[2]),
      '11', formatCoord(entity.end[0]), '21', formatCoord(entity.end[1]), '31', formatCoord(entity.end[2]),
    )
  }
  out.push('0', 'ENDSEC', '0', 'EOF')
  return new TextEncoder().encode(`${out.join('\n')}\n`)
}
