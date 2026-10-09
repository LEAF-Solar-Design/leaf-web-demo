import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn(), setTourStep: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'

const identity = { sessionId: 'session-1', changeSetId: 'change-1' }
const tools = [{ name: 'server-tool' }]
const catalog = { families: [{ family_id: 'custom', capabilities: tools }], source: 'server' }
function setup() {
  const services = {
    getTools: vi.fn(async () => tools),
    getCapabilities: vi.fn(async () => catalog),
    routePrompt: vi.fn(async () => ({ lane: 'run', tool: null })),
  }
  const controller = createCatalogController({ services, adapters: { draftStorage: null }, context: { mock: true } })
  return { controller, services }
}
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
afterEach(() => vi.clearAllMocks())

describe('catalog publication ownership', () => {
  it('C41-01 a new identity is claimed once and refreshCatalog starts one load of each list', async () => {
    const { controller, services } = setup()
    const claim = controller.consumeCatalogPublication
    expect(claim(identity)).toBe(true)
    expect(claim(identity)).toBe(false)
    const pending = controller.actions.refreshCatalog({ ignored: true })
    expect(services.getTools).toHaveBeenCalledTimes(1)
    expect(services.getCapabilities).toHaveBeenCalledTimes(1)
    expect(services.getTools.mock.invocationCallOrder[0]).toBeLessThan(services.getCapabilities.mock.invocationCallOrder[0])
    expect(Object.keys(controller).sort()).toEqual(['actions', 'consumeCatalogPublication', 'destroy', 'getState', 'setContext', 'start', 'subscribe'].sort())
    expect(Object.values(controller).some((value) => value instanceof Set)).toBe(false)
    expect(Object.keys(controller.actions)).not.toContain('allowSecretOnce')
    await expect(pending).resolves.toBeUndefined()
  })

  it('C41-01 a second claim made while the first refresh is in flight is refused', async () => {
    const { controller, services } = setup()
    const flat = deferred(), grouped = deferred()
    services.getTools.mockReturnValueOnce(flat.promise)
    services.getCapabilities.mockReturnValueOnce(grouped.promise)
    expect(controller.consumeCatalogPublication(identity)).toBe(true)
    const pending = controller.actions.refreshCatalog()
    expect(controller.consumeCatalogPublication(identity)).toBe(false)
    flat.resolve(tools); grouped.resolve(catalog)
    await pending
  })

  it('C41-03 the identity is the session and the change set, never the turn, sequence or digest', () => {
    const { controller } = setup()
    expect(controller.consumeCatalogPublication(identity)).toBe(true)
    expect(controller.consumeCatalogPublication({ ...identity, turnId: 'other', seq: 999, catalogDigest: 'other' })).toBe(false)
    expect(controller.consumeCatalogPublication({ sessionId: 'a,b', changeSetId: 'c' })).toBe(true)
    expect(controller.consumeCatalogPublication({ sessionId: 'a', changeSetId: 'b,c' })).toBe(true)
  })

  it('C41-05 start and destroy keep every claim', async () => {
    const { controller } = setup()
    expect(controller.consumeCatalogPublication(identity)).toBe(true)
    controller.destroy()
    controller.start()
    controller.setContext({ mock: false, drawingId: 'new' })
    controller.actions.resetTransient()
    expect(controller.consumeCatalogPublication(identity)).toBe(false)
    await Promise.resolve()
    controller.destroy()
  })

  it('C41-09 512 identities are retained and the oldest is evicted first', () => {
    const { controller } = setup()
    const claim = (i) => controller.consumeCatalogPublication({ sessionId: 's', changeSetId: String(i) })
    for (let i = 0; i < 512; i++) expect(claim(i)).toBe(true)
    for (let i = 0; i < 512; i++) expect(claim(i)).toBe(false)
    expect(claim(512)).toBe(true)
    expect(claim(0)).toBe(true)
    expect(claim(2)).toBe(false)
    expect(claim(1)).toBe(true)
  })

  it('C41-09 two controllers do not share claims', () => {
    const a = setup().controller, b = setup().controller
    expect(a.consumeCatalogPublication(identity)).toBe(true)
    expect(b.consumeCatalogPublication(identity)).toBe(true)
    expect(a.consumeCatalogPublication(identity)).toBe(false)
    expect(b.consumeCatalogPublication(identity)).toBe(false)
  })

  it.each([
    ['a missing argument', undefined],
    ['a null session', { ...identity, sessionId: null }],
    ['an empty session', { ...identity, sessionId: '' }],
    ['a numeric change set', { ...identity, changeSetId: 7 }],
    ['a padded change set', { ...identity, changeSetId: ' change-1 ' }],
    ['a 129 character change set', { ...identity, changeSetId: 'a'.repeat(129) }],
  ])('C41-15 %s is not an identity', (_label, argument) => {
    const { controller } = setup()
    expect(() => expect(controller.consumeCatalogPublication(argument)).toBe(false)).not.toThrow()
    expect(controller.consumeCatalogPublication(identity)).toBe(true)
  })

  it.each(['the flat list', 'the grouped catalog', 'both lists'])('C41-12 %s failing still starts the other load and keeps its own error state', async (label) => {
    const { controller, services } = setup()
    await controller.actions.refreshCatalog()
    const flatFails = label !== 'the grouped catalog', groupedFails = label !== 'the flat list'
    if (flatFails) services.getTools.mockRejectedValueOnce(new Error('flat failed'))
    if (groupedFails) services.getCapabilities.mockRejectedValueOnce(new Error('grouped failed'))
    const pending = controller.actions.refreshCatalog()
    expect(services.getTools).toHaveBeenCalledTimes(2)
    expect(services.getCapabilities).toHaveBeenCalledTimes(2)
    await expect(pending).resolves.toBeUndefined()
    const state = controller.getState()
    expect(state.tools).toEqual(tools)
    expect(state.toolsError).toBe(flatFails ? 'flat failed' : null)
    expect(state.catalog).toEqual(groupedFails ? { families: [], source: null } : catalog)
    expect(state.catalogError).toBe(groupedFails ? 'grouped failed' : null)
  })

  it('C41-12 refreshCatalog settles after both loads and never rejects', async () => {
    const { controller, services } = setup()
    for (const first of ['flat', 'grouped']) {
      const flat = deferred(), grouped = deferred()
      services.getTools.mockReturnValueOnce(flat.promise)
      services.getCapabilities.mockReturnValueOnce(grouped.promise)
      const settled = vi.fn()
      const pending = controller.actions.refreshCatalog().then(settled)
      await Promise.resolve()
      expect(settled).not.toHaveBeenCalled()
      if (first === 'flat') flat.resolve(tools)
      else grouped.resolve(catalog)
      await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
      expect(settled).not.toHaveBeenCalled()
      if (first === 'flat') grouped.resolve(catalog)
      else flat.resolve(tools)
      await pending
      expect(settled).toHaveBeenCalledWith(undefined)
    }
    services.getTools.mockRejectedValueOnce(new Error('flat'))
    services.getCapabilities.mockRejectedValueOnce(new Error('grouped'))
    await expect(controller.actions.refreshCatalog()).resolves.toBeUndefined()
  })

  it('C41-16 refreshCatalog waits for the other load when one load itself rejects', async () => {
    const { controller, services } = setup()
    const grouped = deferred()
    services.getTools.mockImplementationOnce(() => Promise.reject({ toString: 'offline' }))
    services.getCapabilities.mockReturnValueOnce(grouped.promise)
    const outcomes = []
    const pending = controller.actions.refreshCatalog()
    pending.then(() => outcomes.push('resolved'), () => outcomes.push('rejected'))
    await new Promise((r) => setTimeout(r, 0))
    expect(outcomes).toEqual([])
    grouped.resolve(catalog)
    await expect(pending).resolves.toBeUndefined()
    expect(outcomes).toEqual(['resolved'])
  })

  it('C41-16 refreshCatalog resolves when a subscriber throws during a load', async () => {
    const { controller } = setup()
    const off = controller.subscribe(() => { throw new Error('subscriber failed') })
    try {
      await expect(controller.actions.refreshCatalog()).resolves.toBeUndefined()
    } finally {
      off()
    }
  })

  it('C41-16 a rejection that is not an Error is contained like one', async () => {
    const { controller, services } = setup()
    services.getTools.mockRejectedValueOnce('offline')
    services.getCapabilities.mockRejectedValueOnce(42)
    await expect(controller.actions.refreshCatalog()).resolves.toBeUndefined()
    expect(controller.getState().toolsError).toBe('offline')
    expect(controller.getState().catalogError).toBe('42')
  })

  it('C41-09 a replayed identity keeps its place in the eviction order', () => {
    const { controller } = setup()
    const claim = (i) => controller.consumeCatalogPublication({ sessionId: 's', changeSetId: `k${i}` })
    for (let i = 0; i < 512; i++) expect(claim(i)).toBe(true)
    expect(claim(0)).toBe(false)
    expect(claim(512)).toBe(true)
    expect(claim(1)).toBe(false)
    expect(claim(0)).toBe(true)
  })

  it('C41-03 identities that differ only by a separator or by letter case stay distinct', () => {
    const { controller } = setup()
    for (const [sessionId, changeSetId] of [
      ['a|b', 'c'], ['a', 'b|c'],
      ['a","b', 'c'], ['a', 'b","c'],
      ['Session-1', 'x'], ['session-1', 'x'],
    ]) expect(controller.consumeCatalogPublication({ sessionId, changeSetId })).toBe(true)
  })

  it('C41-05 claims survive a finished refresh and a destroy after start', async () => {
    const { controller } = setup()
    controller.start()
    await new Promise((r) => setTimeout(r, 0))
    expect(controller.getState().tools).toEqual(tools)
    expect(controller.getState().catalog).toEqual(catalog)
    expect(controller.consumeCatalogPublication(identity)).toBe(true)
    await controller.actions.refreshCatalog()
    expect(controller.consumeCatalogPublication(identity)).toBe(false)
    controller.destroy()
    expect(controller.consumeCatalogPublication(identity)).toBe(false)
  })

  it('C41-09 two controllers built on one services object do not share claims', () => {
    const { controller: a, services } = setup()
    const b = createCatalogController({ services, adapters: { draftStorage: null }, context: { mock: true } })
    expect(a.consumeCatalogPublication(identity)).toBe(true)
    expect(b.consumeCatalogPublication(identity)).toBe(true)
    expect(a.consumeCatalogPublication(identity)).toBe(false)
    expect(b.consumeCatalogPublication(identity)).toBe(false)
  })

  it.each([
    ['a 257 character session', () => ({ ...identity, sessionId: 's'.repeat(257) })],
    ['an argument whose getter throws', () => ({ get sessionId() { throw new Error('getter failed') }, changeSetId: identity.changeSetId })],
    ['a revoked proxy', () => {
      const { proxy, revoke } = Proxy.revocable({}, {})
      revoke()
      return proxy
    }],
  ])('C41-15 %s is refused without throwing', (_label, argument) => {
    const { controller } = setup()
    const value = argument()
    expect(() => expect(controller.consumeCatalogPublication(value)).toBe(false)).not.toThrow()
    expect(controller.consumeCatalogPublication(identity)).toBe(true)
  })

  it('C41-15 a 256 character session with a 128 character change set is accepted', () => {
    const { controller } = setup()
    const value = { sessionId: 's'.repeat(256), changeSetId: 'c'.repeat(128) }
    expect(controller.consumeCatalogPublication(value)).toBe(true)
    expect(controller.consumeCatalogPublication(value)).toBe(false)
  })

  it('C41-01 the published state never carries the ledger', () => {
    const { controller } = setup()
    const ids = ['ledger-only-change-one', 'ledger-only-change-two']
    for (const changeSetId of ids) expect(controller.consumeCatalogPublication({ sessionId: 's', changeSetId })).toBe(true)
    const state = controller.getState()
    const inspect = (value) => {
      expect(value instanceof Set || value instanceof Map).toBe(false)
      if (value && typeof value === 'object') Object.values(value).forEach(inspect)
    }
    inspect(state)
    for (const id of ids) expect(JSON.stringify(state)).not.toContain(id)
  })
})
