// Stand-in for the wasm-bindgen JS glue that `wasm-pack build --target web`
// would generate from src/lib.rs, once a Rust/wasm toolchain is available in
// this workdir (none is: no rustc/cargo/wasm-pack on PATH — see
// docs/ACADRUST-SPIKE-DAY2.md, OPEN QUESTION OQ-2). Exported function names
// mirror src/lib.rs's #[wasm_bindgen] exports 1:1 (parseDxf/writeDxf/
// bytesEqual via js_name; parsed.entities is a getter there, a plain array
// here, same element shape {type, layer, start, end}) so worker-entry.mjs's import line is the ONLY
// edit needed to swap this for the real generated
// `pkg/acadrust_worker.js` later.
//
// Implements only the DXF group-code subset the day-1 fixture needs: a
// HEADER section ($ACADVER) and an ENTITIES section of LINE records (group
// codes 0/8/10/20/30/11/21/31 — see fixtures/one_line.dxf, and the day-1
// postmortem's note that this minimal shape needs no TABLES/BLOCKS for R12).
// This is NOT a general DXF engine — that is acadrust's job in the real
// crate; this file exists only so the day-2 message-schema round trip has
// something real to execute against without a toolchain.

const LINE_SPLIT_RE = /\r\n|\r|\n/

function tokenizeGroupCodes(text) {
  const lines = text.split(LINE_SPLIT_RE).filter((line) => line.length > 0)
  const pairs = []
  for (let i = 0; i + 1 < lines.length; i += 2) {
    pairs.push([Number.parseInt(lines[i].trim(), 10), lines[i + 1].trim()])
  }
  return pairs
}

function decode(bytes) {
  return typeof bytes === 'string' ? bytes : new TextDecoder().decode(bytes)
}

// Mirrors parse_dxf_line_count / parse_dxf_first_line combined: the JS stand-in
// returns the whole parsed document since there is no wasm-bindgen marshalling
// cost to economize on here.
export function parseDxf(bytes) {
  const pairs = tokenizeGroupCodes(decode(bytes))
  let acadver = null
  const entities = []
  let i = 0
  while (i < pairs.length) {
    const [code, value] = pairs[i]
    if (code === 9 && value === '$ACADVER') {
      acadver = pairs[i + 1]?.[1] ?? null
      i += 2
      continue
    }
    if (code === 0 && value === 'LINE') {
      const entity = { type: 'LINE', layer: '0', start: [0, 0, 0], end: [0, 0, 0] }
      i += 1
      while (i < pairs.length && pairs[i][0] !== 0) {
        const [fieldCode, fieldValue] = pairs[i]
        const n = Number.parseFloat(fieldValue)
        if (fieldCode === 8) entity.layer = fieldValue
        else if (fieldCode === 10) entity.start[0] = n
        else if (fieldCode === 20) entity.start[1] = n
        else if (fieldCode === 30) entity.start[2] = n
        else if (fieldCode === 11) entity.end[0] = n
        else if (fieldCode === 21) entity.end[1] = n
        else if (fieldCode === 31) entity.end[2] = n
        i += 1
      }
      entities.push(entity)
      continue
    }
    i += 1
  }
  return { acadver, entities }
}

function formatCoord(n) {
  return Number.isInteger(n) ? `${n}.0` : String(n)
}

// Mirrors roundtrip_dxf's serialization half (parse_dxf_line_count acts as
// the wasm_bindgen entry point that would call this internally on the real
// acadrust::io::dxf::DxfWriter).
export function writeDxf(doc) {
  const out = ['0', 'SECTION', '2', 'HEADER']
  if (doc.acadver) out.push('9', '$ACADVER', '1', doc.acadver)
  out.push('0', 'ENDSEC', '0', 'SECTION', '2', 'ENTITIES')
  for (const entity of doc.entities) {
    if (entity.type !== 'LINE') continue
    out.push(
      '0', 'LINE',
      '8', entity.layer ?? '0',
      '10', formatCoord(entity.start[0]), '20', formatCoord(entity.start[1]), '30', formatCoord(entity.start[2]),
      '11', formatCoord(entity.end[0]), '21', formatCoord(entity.end[1]), '31', formatCoord(entity.end[2]),
    )
  }
  out.push('0', 'ENDSEC', '0', 'EOF')
  return new TextEncoder().encode(out.join('\n') + '\n')
}

export function bytesEqual(a, b) {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) return false
  }
  return true
}
