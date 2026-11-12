import { describe, expect, it, vi } from 'vitest'
import { createProjectDrawingEditor } from './projectDrawingEditor.js'

// Undo restore and Save while the project checkout is already held elsewhere (the project client).
const O = '11111111-1111-4111-8111-111111111111'
const P = '22222222-2222-4222-8222-222222222222'
const D = '33333333-3333-4333-8333-333333333333'
const V2 = '44444444-4444-4444-8444-444444444444'
const V1 = '55555555-5555-4555-8555-555555555555'
const V3 = '66666666-6666-4666-8666-666666666666'
const scope = { organization_id: O, project_id: P, drawing_id: D, input_version_id: V2 }
const sha = (bytes) => globalThis.crypto.subtle.digest('SHA-256', bytes)
  .then((hash) => Array.from(new Uint8Array(hash), (value) => value.toString(16).padStart(2, '0')).join(''))
const receipt = (requestId, changes = {}) => ({ organization_id: O, project_id: P, drawing_id: D,
  parent_version_id: V2, request_id: requestId, source_sha256: 'a'.repeat(64), intake_sha256: 'b'.repeat(64),
  drawing_version: { version_id: V3, drawing_id: D, project_id: P, org_id: O, seq: 3 }, ...changes })
const conflict = () => Object.assign(new Error('SIP_R1_CHECKOUT_CONFLICT'), { status: 409 })

function setup({ held = true, heldResult } = {}) {
  const requestId = '77777777-7777-4777-8777-777777777777'
  // The server refuses a second acquire while the project lease is active.
  const acquireCheckout = vi.fn(async () => { if (held) throw conflict(); return { capability: 'lco1.' + 'c'.repeat(64) } })
  const releaseCheckout = vi.fn(async () => {})
  const transport = { read: vi.fn(), save: vi.fn(async (_scope, _bytes, options) => receipt(options.requestId)),
    restore: vi.fn(async (_scope, _version, options) => receipt(options.requestId)) }
  const publishHeld = vi.fn(async (kind, target, value, options) => {
    if (!held) return null
    return heldResult ? heldResult(kind, target, value, options) : receipt(options.requestId)
  })
  const editor = createProjectDrawingEditor({ getScope: () => scope, transport, acquireCheckout, releaseCheckout,
    publishHeld, newRequestId: () => requestId })
  return { editor, transport, acquireCheckout, releaseCheckout, publishHeld, requestId }
}

describe('project editor under a held project checkout', () => {
  it('Undo restore publishes through the held checkout without a second acquire', async () => {
    const lane = setup()
    const result = await lane.editor.restoreVersion(V1)
    expect(result.projectOperation).toBe('restore')
    expect(result.drawing_version.version_id).toBe(V3)
    expect(result.parent_version_id).toBe(V2)
    expect(result.request_id).toBe(lane.requestId)
    expect(lane.publishHeld).toHaveBeenCalledWith('restore', scope, V1, { requestId: lane.requestId })
    expect(lane.acquireCheckout).not.toHaveBeenCalled()
    expect(lane.releaseCheckout).not.toHaveBeenCalled()
    expect(lane.transport.restore).not.toHaveBeenCalled()
  })

  it('Save publishes the exact bytes through the held checkout and keeps the digest check', async () => {
    const bytes = new Uint8Array([48, 10, 69, 79, 70, 10])
    const digest = await sha(bytes)
    const lane = setup({ heldResult: (_kind, _scope, _value, options) => receipt(options.requestId, { source_sha256: digest }) })
    const result = await lane.editor.save(bytes, V2, digest)
    expect(result.projectOperation).toBe('save')
    expect(result.source_sha256).toBe(digest)
    const [kind, target, sent, options] = lane.publishHeld.mock.calls[0]
    expect([kind, target, options]).toEqual(['save', scope, { requestId: lane.requestId }])
    expect(Array.from(sent)).toEqual(Array.from(bytes))
    expect(lane.acquireCheckout).not.toHaveBeenCalled()
    await expect(setup().editor.save(bytes, V2, 'f'.repeat(64))).rejects.toThrow('changed before saving')
  })

  it('refuses a held result for another parent, drawing or request and keeps the request for retry', async () => {
    for (const changes of [{ parent_version_id: V1 }, { drawing_id: V1 }, { request_id: V1 }]) {
      const lane = setup({ heldResult: (_kind, _scope, _value, options) => receipt(options.requestId, changes) })
      await expect(lane.editor.restoreVersion(V1)).rejects.toMatchObject({ outcomeUnknown: true })
      // Unknown outcome: the same request id is replayed, never a fresh one.
      await expect(lane.editor.restoreVersion(V1)).rejects.toMatchObject({ outcomeUnknown: true })
      expect(lane.publishHeld.mock.calls.map((call) => call[3].requestId)).toEqual([lane.requestId, lane.requestId])
      expect(lane.acquireCheckout).not.toHaveBeenCalled()
    }
  })

  it('surfaces a held-checkout refusal as settled, without falling back to a second acquire', async () => {
    const lane = setup({ heldResult: () => { throw Object.assign(new Error('SIP_R1_CHECKOUT_DENIED'), { status: 403 }) } })
    await expect(lane.editor.restoreVersion(V1)).rejects.toMatchObject({ status: 403 })
    expect(lane.acquireCheckout).not.toHaveBeenCalled()
    expect(lane.transport.restore).not.toHaveBeenCalled()
  })

  it('with no held checkout keeps the bounded acquire, publish and release path', async () => {
    const lane = setup({ held: false })
    const result = await lane.editor.restoreVersion(V1)
    expect(result.drawing_version.version_id).toBe(V3)
    expect(lane.publishHeld).toHaveBeenCalledTimes(1)
    expect(lane.acquireCheckout).toHaveBeenCalledTimes(1)
    expect(lane.transport.restore).toHaveBeenCalledWith(scope, V1,
      { requestId: lane.requestId, capability: 'lco1.' + 'c'.repeat(64) })
    expect(lane.releaseCheckout).toHaveBeenCalledWith(scope, 'lco1.' + 'c'.repeat(64))
  })

  it('an editor built without a held-checkout source behaves as before', async () => {
    const transport = { read: vi.fn(), save: vi.fn(), restore: vi.fn(async (_s, _v, options) => receipt(options.requestId)) }
    const acquireCheckout = vi.fn(async () => ({ capability: 'lco1.' + 'd'.repeat(64) }))
    const editor = createProjectDrawingEditor({ getScope: () => scope, transport, acquireCheckout,
      releaseCheckout: vi.fn(async () => {}), newRequestId: () => '77777777-7777-4777-8777-777777777777' })
    expect((await editor.restoreVersion(V1)).drawing_version.version_id).toBe(V3)
    expect(acquireCheckout).toHaveBeenCalledTimes(1)
  })
})
