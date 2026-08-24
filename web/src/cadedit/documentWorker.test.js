// @vitest-environment node
//
// Oracle for the worker-side message loop: the boundary vocabulary it
// answers, the fail-closed states (a refused parse leaves NO document), and
// the write-back leg reporting state read back from the WRITTEN bytes rather
// than from the in-memory model.
import { beforeEach, describe, expect, it } from 'vitest'

import { handleMessage } from './documentWorker.js'

const ONE_LINE_DXF = [
  '0', 'SECTION', '2', 'HEADER', '9', '$ACADVER', '1', 'AC1009', '0', 'ENDSEC',
  '0', 'SECTION', '2', 'ENTITIES',
  '0', 'LINE', '8', '0',
  '10', '0.0', '20', '0.0', '30', '0.0',
  '11', '100.0', '21', '50.0', '31', '0.0',
  '0', 'ENDSEC', '0', 'EOF',
].join('\n') + '\n'

const WITH_CIRCLE = ONE_LINE_DXF.replace(
  '0\nENDSEC\n0\nEOF\n', '0\nCIRCLE\n8\n0\n10\n1.0\n20\n1.0\n40\n5.0\n0\nENDSEC\n0\nEOF\n')

function bytes(text) {
  return new TextEncoder().encode(text)
}

function load(text, documentId = 'one_line.dxf') {
  return handleMessage({ type: 'loadDocument', documentId, bytes: bytes(text) })
}

beforeEach(() => {
  // The worker holds exactly one document; drop it so each case starts clean.
  handleMessage({ type: 'dispose' })
})

describe('documentWorker message vocabulary', () => {
  it('answers init with ready', () => {
    expect(handleMessage({ type: 'init' })).toEqual({ type: 'ready' })
  })

  it('rejects a message that fails the shared boundary schema, never throwing', () => {
    expect(handleMessage({ type: 'loadDocument' })).toEqual({
      type: 'error', message: 'bad_message:missing_fields:documentId',
    })
    expect(handleMessage(null)).toEqual({ type: 'error', message: 'bad_message:not_an_object' })
    expect(handleMessage({ type: 'nonsense' })).toEqual({ type: 'error', message: 'bad_message:unknown_type' })
  })

  it('returns no reply to dispose', () => {
    expect(handleMessage({ type: 'dispose' })).toBeNull()
  })
})

describe('documentWorker loadDocument', () => {
  it('reports the parsed entity count, the entities, and a writable document', () => {
    const response = load(ONE_LINE_DXF)

    expect(response.type).toBe('documentLoaded')
    expect(response.documentId).toBe('one_line.dxf')
    expect(response.entityCount).toBe(1)
    expect(response.entities).toEqual([
      { id: 'e0', type: 'LINE', layer: '0', start: [0, 0, 0], end: [100, 50, 0] },
    ])
    expect(response.writable).toBe(true)
    expect(response.refusal).toBeNull()
  })

  it('loads a document it can read but not rewrite, and says so by name', () => {
    const response = load(WITH_CIRCLE)

    expect(response.type).toBe('documentLoaded')
    expect(response.entityCount).toBe(1)
    expect(response.writable).toBe(false)
    expect(response.refusal).toMatch(/CIRCLE/)
    expect(response.unsupported.entityTypes).toEqual(['CIRCLE'])
  })

  it('a refused parse leaves NO document loaded (fail closed, not stale)', () => {
    expect(load(ONE_LINE_DXF).type).toBe('documentLoaded')

    const bad = load('NOTACODE\nSECTION\n', 'bad.dxf')
    expect(bad.type).toBe('error')
    expect(bad.message).toMatch(/^parse_failed:bad_group_code:/)

    expect(handleMessage({ type: 'applyEdit', op: 'delete', payload: { entityId: 'e0' } })).toEqual({
      type: 'editApplied', op: 'delete', ok: false, reason: 'no_document_loaded',
    })
  })

  it('refuses an empty documentId', () => {
    expect(handleMessage({ type: 'loadDocument', documentId: '', bytes: bytes(ONE_LINE_DXF) })).toEqual({
      type: 'error', message: 'bad_document_id',
    })
  })
})

describe('documentWorker applyEdit write-back', () => {
  it('delete reports the count re-parsed FROM THE WRITTEN BYTES, plus those bytes', () => {
    load(ONE_LINE_DXF)
    const response = handleMessage({ type: 'applyEdit', op: 'delete', payload: { entityId: 'e0' } })

    expect(response.type).toBe('editApplied')
    expect(response.ok).toBe(true)
    expect(response.entityCount).toBe(0)
    expect(response.entities).toEqual([])
    expect(response.bytes).toBeInstanceOf(Uint8Array)
    expect(response.byteLength).toBe(response.bytes.length)
    expect(new TextDecoder().decode(response.bytes)).not.toContain('LINE')
  })

  it('move reports the translated geometry as a reader of the written file would see it', () => {
    load(ONE_LINE_DXF)
    const response = handleMessage({
      type: 'applyEdit', op: 'move', payload: { entityId: 'e0', dx: 10, dy: 20 },
    })

    expect(response.ok).toBe(true)
    expect(response.entityCount).toBe(1)
    expect(response.entities[0].start).toEqual([10, 20, 0])
    expect(response.entities[0].end).toEqual([110, 70, 0])
  })

  it('successive edits compound against the edited document, not the original', () => {
    load(ONE_LINE_DXF)
    handleMessage({ type: 'applyEdit', op: 'move', payload: { entityId: 'e0', dx: 1, dy: 1 } })
    const second = handleMessage({ type: 'applyEdit', op: 'move', payload: { entityId: 'e0', dx: 2, dy: 3 } })

    expect(second.entities[0].start).toEqual([3, 4, 0])
  })

  it('refuses every edit on a read-but-not-rewritable document, by name', () => {
    load(WITH_CIRCLE)
    const response = handleMessage({ type: 'applyEdit', op: 'delete', payload: { entityId: 'e0' } })

    expect(response).toMatchObject({ type: 'editApplied', op: 'delete', ok: false })
    expect(response.reason).toMatch(/^not_writable:.*CIRCLE/)
    expect(response.bytes).toBeUndefined()
  })

  it('a refused edit leaves the loaded document untouched', () => {
    load(ONE_LINE_DXF)
    handleMessage({ type: 'applyEdit', op: 'move', payload: { entityId: 'e0', dx: 'far', dy: 0 } })
    const after = handleMessage({ type: 'applyEdit', op: 'move', payload: { entityId: 'e0', dx: 0, dy: 0 } })

    expect(after.ok).toBe(true)
    expect(after.entities[0].start).toEqual([0, 0, 0])
  })

  it('propagates the model refusal reason for a bad payload', () => {
    load(ONE_LINE_DXF)
    expect(handleMessage({ type: 'applyEdit', op: 'delete', payload: { entityId: 'e9' } })).toEqual({
      type: 'editApplied', op: 'delete', ok: false, reason: 'unknown_entity:e9',
    })
  })
})
