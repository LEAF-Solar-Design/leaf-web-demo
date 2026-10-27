import { describe, expect, it, vi } from 'vitest'
import { resolveDrawingStore, pull, publish } from './drawingSync.js'

const drawingStoreId = 'roof-1'
const resultDigest = (version) => version.toString(16).padStart(64, '0')
const ready = { drawingVersionId: 'bound-version' }
const mappedProject = (source) => ({ drawing_versions: [{ version_id: ready.drawingVersionId, provenance: { source } }] })
function dependencies(head = 4) {
  return {
    api: {
      getDrawingVersions: vi.fn().mockResolvedValue({ head }),
      getDrawingVersionPlan: vi.fn(async (_, version) => ({ plan_text: `plan ${version}`, plan_sha256: `sha ${version}`, result_sha256: resultDigest(version) })),
      takeCheckout: vi.fn().mockResolvedValue({ acquired: true, checkout_capability: 'capability' }),
      releaseCheckout: vi.fn().mockResolvedValue({ released: true }),
      saveEditedDrawingVersion: vi.fn().mockResolvedValue({ new_version: { version: head + 1 } }),
    },
    bridge: {
      applyPlan: vi.fn().mockResolvedValue({ status: 'applied' }),
      exportDxf: vi.fn().mockResolvedValue({ bytes: new Uint8Array([1, 2]), sha256: 'digest' }),
      syncCursor: vi.fn().mockResolvedValue({ status: 'applied' }),
    }, drawingStoreId,
  }
}

describe('drawing store mapping', () => {
  it('uses the bound platform version provenance', () => {
    expect(resolveDrawingStore(mappedProject({ drawing_id: drawingStoreId, version: 3 }), ready))
      .toEqual({ drawingStoreId, boundVersion: 3 })
  })
  it.each([undefined, {}, { drawing_versions: [] }, mappedProject({}),
    mappedProject({ drawing_id: '../bad', version: 1 }), mappedProject({ drawing_id: 'Upper', version: 1 }),
    mappedProject({ drawing_id: 'a'.repeat(64), version: 1 }), mappedProject({ drawing_id: drawingStoreId, version: 0 }),
    mappedProject({ drawing_id: drawingStoreId, version: 1.5 }), mappedProject({ drawing_id: drawingStoreId, version: '1' }),
  ])('rejects an unmapped or invalid source %#', (project) => {
    expect(() => resolveDrawingStore(project, ready)).toThrow('drawing_store_unmapped')
  })
  it('does not use a different platform version', () => {
    expect(() => resolveDrawingStore(mappedProject({ drawing_id: drawingStoreId, version: 1 }), { drawingVersionId: 'other' }))
      .toThrow('drawing_store_unmapped')
  })
})

describe('pull', () => {
  it('applies three consecutive plans and advances only after each applied reply', async () => {
    const deps = dependencies()
    expect(await pull({ ...deps, cursor: 1 })).toEqual({ cursor: 4, applied: [2, 3, 4], reason: null })
    expect(deps.bridge.applyPlan.mock.calls.map(([args]) => args)).toEqual([2, 3, 4].map((version) => ({
      drawingStoreId, planText: `plan ${version}`, planSha256: `sha ${version}`, parentVersion: version - 1, resultVersion: version,
      resultContentSha256: resultDigest(version),
    })))
  })
  it('requires a first publish without reading the head', async () => {
    const deps = dependencies()
    expect(await pull({ ...deps, cursor: null })).toEqual({ cursor: null, applied: [], reason: 'publish_first' })
    expect(deps.api.getDrawingVersions).not.toHaveBeenCalled()
  })
  it('stops at a missing plan and retains progress', async () => {
    const deps = dependencies()
    deps.api.getDrawingVersionPlan.mockResolvedValueOnce({ plan_text: 'plan', plan_sha256: 'sha', result_sha256: resultDigest(2) })
      .mockRejectedValueOnce(Object.assign(new Error('missing'), { status: 404 }))
    expect(await pull({ ...deps, cursor: 1 })).toEqual({ cursor: 2, applied: [2], reason: 'no_plan' })
    expect(deps.bridge.applyPlan).toHaveBeenCalledTimes(1)
  })
  it.each([null, undefined, '', 'a'.repeat(63), 'a'.repeat(65), 'A'.repeat(64), 'g'.repeat(64), `${'a'.repeat(64)}\n`, 123])(
    'stops before a second plan with an invalid result digest and retains progress %#', async (result_sha256) => {
      const deps = dependencies()
      deps.api.getDrawingVersionPlan.mockResolvedValueOnce({ plan_text: 'plan 2', plan_sha256: 'sha 2', result_sha256: resultDigest(2) })
        .mockResolvedValueOnce({ plan_text: 'plan 3', plan_sha256: 'sha 3', result_sha256 })
      expect(await pull({ ...deps, cursor: 1 })).toEqual({ cursor: 2, applied: [2], reason: 'no_result_digest' })
      expect(deps.bridge.applyPlan).toHaveBeenCalledTimes(1)
      expect(deps.bridge.applyPlan).toHaveBeenCalledWith({
        drawingStoreId, planText: 'plan 2', planSha256: 'sha 2', parentVersion: 1, resultVersion: 2,
        resultContentSha256: resultDigest(2),
      })
      expect(deps.api.getDrawingVersionPlan).toHaveBeenCalledTimes(2)
    })
  it('stops on a stale callback', async () => {
    const deps = dependencies()
    deps.bridge.applyPlan.mockResolvedValueOnce({ status: 'stale', reason: 'sync_cursor_mismatch' })
    expect(await pull({ ...deps, cursor: 1 })).toEqual({ cursor: 1, applied: [], reason: 'sync_cursor_mismatch' })
    expect(deps.api.getDrawingVersionPlan).toHaveBeenCalledTimes(1)
  })
  it('caps one pull at fifty versions', async () => {
    const deps = dependencies(100)
    const result = await pull({ ...deps, cursor: 1 })
    expect(result.cursor).toBe(51)
    expect(result.applied).toHaveLength(50)
    expect(deps.bridge.applyPlan).toHaveBeenCalledTimes(50)
  })
})

describe('publish', () => {
  it('requires pull before checkout when the head differs', async () => {
    const deps = dependencies()
    expect(await publish({ ...deps, cursor: 2 })).toEqual({ reason: 'pull_first' })
    expect(deps.api.takeCheckout).not.toHaveBeenCalled()
    expect(deps.bridge.exportDxf).not.toHaveBeenCalled()
  })
  it.each(['export', 'save'])('releases the exact capability after %s failure', async (failure) => {
    const deps = dependencies()
    if (failure === 'export') deps.bridge.exportDxf.mockRejectedValue(new Error('export_failed'))
    else deps.api.saveEditedDrawingVersion.mockRejectedValue(new Error('save_failed'))
    expect(await publish({ ...deps, cursor: 4 })).toEqual({ reason: `${failure}_failed` })
    expect(deps.api.releaseCheckout).toHaveBeenCalledTimes(1)
    expect(deps.api.releaseCheckout).toHaveBeenCalledWith(drawingStoreId, 'capability')
    expect(deps.bridge.syncCursor).not.toHaveBeenCalled()
  })
  it.each([null, 4])('publishes from cursor %s and syncs only after checkout release', async (cursor) => {
    const deps = dependencies()
    deps.bridge.syncCursor.mockImplementation(async () => {
      expect(deps.api.releaseCheckout).toHaveBeenCalledWith(drawingStoreId, 'capability')
      return { status: 'applied' }
    })
    expect(await publish({ ...deps, cursor })).toEqual({ cursor: 5 })
    expect(deps.api.saveEditedDrawingVersion).toHaveBeenCalledWith(drawingStoreId, new Uint8Array([1, 2]), 4, 'digest', 'capability')
    expect(deps.bridge.syncCursor).toHaveBeenCalledWith({ drawingStoreId, version: 5, exportSha256: 'digest' })
  })
  it('leaves the cursor unchanged if native cursor update is stale', async () => {
    const deps = dependencies()
    deps.bridge.syncCursor.mockResolvedValue({ status: 'stale', reason: 'document_changed' })
    expect(await publish({ ...deps, cursor: 4 })).toEqual({ reason: 'document_changed' })
  })
  it('does not export when checkout is held elsewhere', async () => {
    const deps = dependencies()
    deps.api.takeCheckout.mockResolvedValue({ acquired: false })
    expect((await publish({ ...deps, cursor: null })).reason).toBe('checkout_locked')
    expect(deps.bridge.exportDxf).not.toHaveBeenCalled()
    expect(deps.api.releaseCheckout).not.toHaveBeenCalled()
  })
})
