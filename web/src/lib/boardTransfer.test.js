import { expect, it } from 'vitest'
import { BOARD_TRANSFER_TYPE, MAX_TRANSFER_CHARS, encodeVersionRef, resolveVersionTransfer } from './boardTransfer.js'

const ref = { projectId: 'project1', drawingId: 'drawing1', versionId: 'version1', seq: 2 }
const payload = { kind: 'version', ...ref }
const context = { projectId: ref.projectId, drawingId: ref.drawingId, versions: [{ version_id: ref.versionId, drawing_id: ref.drawingId, seq: ref.seq }] }

it('SSD1-24D round trips a version reference into a frozen result', () => {
  expect(BOARD_TRANSFER_TYPE).toBe('application/x-leaf-board-ref')
  expect(MAX_TRANSFER_CHARS).toBe(1024)
  const raw = encodeVersionRef(ref)
  expect(raw).toBe(JSON.stringify(payload))
  const result = resolveVersionTransfer(raw, context)
  expect(result).toEqual({ ok: true, seq: 2, versionId: 'version1' })
  expect(Object.isFrozen(result)).toBe(true)
})

it.each([
  ['non-string', null, context, 'unreadable'],
  ['oversize', ' '.repeat(MAX_TRANSFER_CHARS + 1), context, 'unreadable'],
  ['invalid JSON', '{', context, 'unreadable'],
  ['array', '[]', context, 'unreadable'],
  ['null JSON', 'null', context, 'unreadable'],
  ['extra key', JSON.stringify({ ...payload, extra: true }), context, 'unreadable'],
  ['missing key', JSON.stringify({ ...payload, versionId: undefined }), context, 'unreadable'],
  ['job kind', JSON.stringify({ ...payload, kind: 'job' }), context, 'unsupported-kind'],
  ['empty drawing', JSON.stringify(payload), { ...context, drawingId: '' }, 'no-drawing'],
  ['another project', JSON.stringify({ ...payload, projectId: 'other' }), context, 'wrong-project'],
  ['another drawing', JSON.stringify({ ...payload, drawingId: 'other' }), context, 'wrong-drawing'],
  ['unknown version', JSON.stringify({ ...payload, versionId: 'other' }), context, 'stale-version'],
  ['another sequence', JSON.stringify({ ...payload, seq: 3 }), context, 'stale-version'],
  ['version in another drawing', JSON.stringify(payload), { ...context, versions: [{ ...context.versions[0], drawing_id: 'other' }] }, 'stale-version'],
])('SSD1-24D refuses %s with its exact reason and a frozen result', (_name, raw, current, reason) => {
  const result = resolveVersionTransfer(raw, current)
  expect(result.ok).toBe(false)
  expect(result.reason).toBe(reason)
  expect(result.message).toEqual(expect.any(String))
  expect(result.message.length).toBeGreaterThan(0)
  expect(Object.isFrozen(result)).toBe(true)
})

it.each(['projectId', 'drawingId', 'versionId'])('SSD1-24D refuses empty and oversized %s in the encoder', (key) => {
  expect(encodeVersionRef({ ...ref, [key]: '' })).toBeNull()
  expect(encodeVersionRef({ ...ref, [key]: 'x'.repeat(201) })).toBeNull()
})

it.each([0, 1.5, Number.MAX_SAFE_INTEGER + 1])('SSD1-24D refuses invalid sequence %s in the encoder', (seq) => {
  expect(encodeVersionRef({ ...ref, seq })).toBeNull()
})
