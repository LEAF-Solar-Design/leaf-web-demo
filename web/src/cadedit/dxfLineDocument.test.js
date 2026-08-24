// @vitest-environment node
//
// Node, not the workspace-default jsdom: this module is pure and runs inside
// a worker, so a DOM is neither needed nor honest here.
//
// Oracle for the bounded, fail-closed document model: the parse subset, the
// caps, the lossless-or-refuse gate, the two edit ops, and the entity-level
// (NOT byte-level) round trip.
import { runInNewContext } from 'node:vm'

import { describe, expect, it } from 'vitest'

import {
  MAX_DOCUMENT_BYTES,
  MAX_EDIT_DELTA,
  MAX_ENTITIES,
  applyEditToDocument,
  isByteArray,
  parseDxfDocument,
  serializeDxfDocument,
  writeRefusal,
} from './dxfLineDocument.js'

// The same one-LINE fixture shape the day-1/day-3 wasm engine spike
// round-tripped (the fixture under vendor/), inlined so this suite does not
// reach across the vendor boundary for a test input — and so this file names
// no vendored crate, which the license fence denies from web/.
const ONE_LINE_DXF = [
  '0', 'SECTION', '2', 'HEADER', '9', '$ACADVER', '1', 'AC1009', '0', 'ENDSEC',
  '0', 'SECTION', '2', 'ENTITIES',
  '0', 'LINE', '8', '0',
  '10', '0.0', '20', '0.0', '30', '0.0',
  '11', '100.0', '21', '50.0', '31', '0.0',
  '0', 'ENDSEC', '0', 'EOF',
].join('\n') + '\n'

function bytes(text) {
  return new TextEncoder().encode(text)
}

function parseOrThrow(text) {
  const result = parseDxfDocument(bytes(text))
  expect(result.ok).toBe(true)
  return result.doc
}

describe('dxfLineDocument parse', () => {
  it('parses the one-LINE fixture into one editable entity with exact coordinates', () => {
    const doc = parseOrThrow(ONE_LINE_DXF)

    expect(doc.acadver).toBe('AC1009')
    expect(doc.entities).toHaveLength(1)
    expect(doc.entities[0]).toEqual({
      id: 'e0', type: 'LINE', layer: '0', start: [0, 0, 0], end: [100, 50, 0],
    })
    expect(writeRefusal(doc)).toBeNull()
  })

  it('accepts CRLF line endings the same as LF', () => {
    const doc = parseOrThrow(ONE_LINE_DXF.replace(/\n/g, '\r\n'))
    expect(doc.entities).toHaveLength(1)
    expect(doc.entities[0].end).toEqual([100, 50, 0])
  })

  it('assigns stable ids across multiple entities', () => {
    const two = ONE_LINE_DXF.replace(
      '0\nENDSEC\n0\nEOF\n',
      '0\nLINE\n8\nWALLS\n10\n1.0\n20\n2.0\n30\n0.0\n11\n3.0\n21\n4.0\n31\n0.0\n0\nENDSEC\n0\nEOF\n')
    const doc = parseOrThrow(two)
    expect(doc.entities.map((e) => e.id)).toEqual(['e0', 'e1'])
    expect(doc.entities[1].layer).toBe('WALLS')
  })

  it('fails closed on a dangling group code rather than guessing', () => {
    const result = parseDxfDocument(bytes('0\nSECTION\n2'))
    expect(result).toEqual({ ok: false, reason: 'malformed_group_pairs:dangling_code' })
  })

  it('fails closed on a SECTION with no name', () => {
    expect(parseDxfDocument(bytes('0\nSECTION\n2\n\n0\nENDSEC\n'))).toEqual({
      ok: false, reason: 'malformed_section:missing_name',
    })
  })

  it('fails closed on a non-numeric group code', () => {
    const result = parseDxfDocument(bytes('NOTACODE\nSECTION\n'))
    expect(result.ok).toBe(false)
    expect(result.reason).toMatch(/^bad_group_code:/)
  })

  it('fails closed on a non-numeric coordinate', () => {
    const result = parseDxfDocument(bytes(ONE_LINE_DXF.replace('100.0', 'over-there')))
    expect(result).toEqual({ ok: false, reason: 'bad_coordinate:e0:11' })
  })

  it('fails closed on bytes that are not UTF-8 text', () => {
    const result = parseDxfDocument(new Uint8Array([0xff, 0xfe, 0xff]))
    expect(result).toEqual({ ok: false, reason: 'not_utf8_text' })
  })

  it('fails closed on anything that is not a Uint8Array or string', () => {
    expect(parseDxfDocument(null)).toEqual({ ok: false, reason: 'not_bytes' })
    expect(parseDxfDocument({ length: 3 })).toEqual({ ok: false, reason: 'not_bytes' })
    expect(parseDxfDocument(new Int16Array(4))).toEqual({ ok: false, reason: 'not_bytes' })
  })

  it('accepts a byte array whose constructor came from ANOTHER realm', () => {
    // Regression: an `instanceof Uint8Array` check reported false for bytes
    // that crossed a worker/jsdom realm boundary, so the write-back leg
    // failed closed on its own correct output. The brand check must not care
    // which realm minted the array.
    const foreign = runInNewContext('new Uint8Array([48, 10])')
    expect(foreign instanceof Uint8Array).toBe(false)   // the trap, reproduced
    expect(isByteArray(foreign)).toBe(true)             // the fix

    expect(isByteArray(bytes(ONE_LINE_DXF))).toBe(true)
    expect(isByteArray([0, 1, 2])).toBe(false)
    expect(isByteArray(new Int16Array(2))).toBe(false)

    // End to end: a foreign-realm byte array parses instead of being refused.
    const parsed = parseDxfDocument(runInNewContext(
      `new Uint8Array(${JSON.stringify([...bytes(ONE_LINE_DXF)])})`))
    expect(parsed.ok).toBe(true)
    expect(parsed.doc.entities).toHaveLength(1)
  })

  it('refuses an oversized document with one length check, not a decode', () => {
    const oversized = new Uint8Array(MAX_DOCUMENT_BYTES + 1)
    const result = parseDxfDocument(oversized)
    expect(result).toEqual({ ok: false, reason: `document_too_large:${MAX_DOCUMENT_BYTES + 1}` })
  })

  it('refuses a document with more than MAX_ENTITIES LINE records', () => {
    const record = '0\nLINE\n8\n0\n10\n0.0\n20\n0.0\n30\n0.0\n11\n1.0\n21\n1.0\n31\n0.0\n'
    const many = `0\nSECTION\n2\nENTITIES\n${record.repeat(MAX_ENTITIES + 1)}0\nENDSEC\n0\nEOF\n`
    const result = parseDxfDocument(bytes(many))
    expect(result).toEqual({ ok: false, reason: `too_many_entities:${MAX_ENTITIES}` })
  })
})

describe('dxfLineDocument lossless-or-refuse gate', () => {
  it('reads a non-LINE entity but names it as unwritable rather than dropping it', () => {
    const withCircle = ONE_LINE_DXF.replace(
      '0\nENDSEC\n0\nEOF\n', '0\nCIRCLE\n8\n0\n10\n1.0\n20\n1.0\n40\n5.0\n0\nENDSEC\n0\nEOF\n')
    const doc = parseOrThrow(withCircle)

    expect(doc.entities).toHaveLength(1)          // the LINE is still readable
    expect(doc.unsupported.entityTypes).toEqual(['CIRCLE'])
    expect(writeRefusal(doc)).toBe('this build can read but not rewrite entity types CIRCLE')
  })

  it('names an unsupported section', () => {
    const withTables = `0\nSECTION\n2\nTABLES\n0\nENDSEC\n${ONE_LINE_DXF}`
    const doc = parseOrThrow(withTables)
    expect(doc.unsupported.sections).toEqual(['TABLES'])
    expect(writeRefusal(doc)).toBe('this build can read but not rewrite sections TABLES')
  })

  it('names an unsupported header variable', () => {
    const withVar = ONE_LINE_DXF.replace('9\n$ACADVER\n1\nAC1009\n', '9\n$ACADVER\n1\nAC1009\n9\n$INSUNITS\n70\n1\n')
    const doc = parseOrThrow(withVar)
    expect(doc.unsupported.headerVars).toEqual(['$INSUNITS'])
    expect(writeRefusal(doc)).toBe('this build can read but not rewrite header variables $INSUNITS')
  })

  it('refuses an edit on an unwritable document without mutating it', () => {
    const withCircle = ONE_LINE_DXF.replace(
      '0\nENDSEC\n0\nEOF\n', '0\nCIRCLE\n8\n0\n10\n1.0\n20\n1.0\n40\n5.0\n0\nENDSEC\n0\nEOF\n')
    const doc = parseOrThrow(withCircle)
    // The gate lives in documentWorker.js; this asserts the model still
    // reports the refusal that gate reads.
    expect(writeRefusal(doc)).not.toBeNull()
  })

  it('reports no refusal for a document entirely inside the writable subset', () => {
    expect(writeRefusal(parseOrThrow(ONE_LINE_DXF))).toBeNull()
    expect(writeRefusal(null)).toBe('no_document')
  })
})

describe('dxfLineDocument edits', () => {
  it('delete removes exactly the selected entity and never mutates the input', () => {
    const doc = parseOrThrow(ONE_LINE_DXF)
    const edited = applyEditToDocument(doc, 'delete', { entityId: 'e0' })

    expect(edited.ok).toBe(true)
    expect(edited.doc.entities).toHaveLength(0)
    expect(doc.entities).toHaveLength(1)
  })

  it('move translates start and end by the delta and leaves z alone', () => {
    const doc = parseOrThrow(ONE_LINE_DXF)
    const edited = applyEditToDocument(doc, 'move', { entityId: 'e0', dx: 5, dy: -2 })

    expect(edited.ok).toBe(true)
    expect(edited.doc.entities[0].start).toEqual([5, -2, 0])
    expect(edited.doc.entities[0].end).toEqual([105, 48, 0])
    expect(doc.entities[0].start).toEqual([0, 0, 0])
  })

  it('keeps ids stable after a delete so a held selection still means the same entity', () => {
    const two = ONE_LINE_DXF.replace(
      '0\nENDSEC\n0\nEOF\n',
      '0\nLINE\n8\nWALLS\n10\n1.0\n20\n2.0\n30\n0.0\n11\n3.0\n21\n4.0\n31\n0.0\n0\nENDSEC\n0\nEOF\n')
    const doc = parseOrThrow(two)
    const edited = applyEditToDocument(doc, 'delete', { entityId: 'e0' })
    expect(edited.doc.entities.map((e) => e.id)).toEqual(['e1'])
  })

  it('refuses an unsupported op by name', () => {
    const doc = parseOrThrow(ONE_LINE_DXF)
    expect(applyEditToDocument(doc, 'explode', { entityId: 'e0' })).toEqual({
      ok: false, reason: 'unsupported_op:explode',
    })
  })

  it('refuses an unknown, missing or non-string entity id', () => {
    const doc = parseOrThrow(ONE_LINE_DXF)
    expect(applyEditToDocument(doc, 'delete', { entityId: 'e99' })).toEqual({
      ok: false, reason: 'unknown_entity:e99',
    })
    expect(applyEditToDocument(doc, 'delete', {})).toEqual({ ok: false, reason: 'bad_entity_id' })
    expect(applyEditToDocument(doc, 'delete', { entityId: 7 })).toEqual({ ok: false, reason: 'bad_entity_id' })
    expect(applyEditToDocument(doc, 'delete', null)).toEqual({ ok: false, reason: 'bad_payload' })
  })

  it('refuses a non-finite, non-numeric or over-cap delta', () => {
    const doc = parseOrThrow(ONE_LINE_DXF)
    for (const payload of [
      { entityId: 'e0', dx: Number.NaN, dy: 0 },
      { entityId: 'e0', dx: Number.POSITIVE_INFINITY, dy: 0 },
      { entityId: 'e0', dx: '5', dy: 0 },
      { entityId: 'e0', dx: 0, dy: MAX_EDIT_DELTA * 10 },
      { entityId: 'e0', dx: 1 },
    ]) {
      expect(applyEditToDocument(doc, 'move', payload)).toEqual({ ok: false, reason: 'bad_delta' })
    }
  })

  it('refuses an edit against a missing document', () => {
    expect(applyEditToDocument(null, 'delete', { entityId: 'e0' })).toEqual({
      ok: false, reason: 'no_document',
    })
  })
})

describe('dxfLineDocument write-back', () => {
  it('round-trips at the ENTITY level (byte identity is NOT promised)', () => {
    const doc = parseOrThrow(ONE_LINE_DXF)
    const written = serializeDxfDocument(doc)
    const reparsed = parseDxfDocument(written)

    expect(reparsed.ok).toBe(true)
    expect(reparsed.doc.entities).toEqual(doc.entities)
    expect(reparsed.doc.acadver).toBe('AC1009')
  })

  it('an edit survives serialization: the re-parse sees the edited geometry', () => {
    const doc = parseOrThrow(ONE_LINE_DXF)
    const edited = applyEditToDocument(doc, 'move', { entityId: 'e0', dx: 10, dy: 20 })
    const reparsed = parseDxfDocument(serializeDxfDocument(edited.doc))

    expect(reparsed.ok).toBe(true)
    expect(reparsed.doc.entities[0].start).toEqual([10, 20, 0])
    expect(reparsed.doc.entities[0].end).toEqual([110, 70, 0])
  })

  it('a delete survives serialization: the written document has no ENTITIES record', () => {
    const doc = parseOrThrow(ONE_LINE_DXF)
    const edited = applyEditToDocument(doc, 'delete', { entityId: 'e0' })
    const written = new TextDecoder().decode(serializeDxfDocument(edited.doc))

    expect(written).not.toContain('LINE')
    expect(parseDxfDocument(new TextEncoder().encode(written)).doc.entities).toEqual([])
  })
})
