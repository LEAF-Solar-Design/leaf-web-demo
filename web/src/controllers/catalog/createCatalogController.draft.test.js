import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn() }))
vi.mock('../../lib/notifications.js', () => ({ notificationBus: { push: vi.fn() } }))

import { notificationBus } from '../../lib/notifications.js'
import { composerDraftKey } from '../../lib/composerDraft.js'
import { guardedText, SecretRefusedError } from '../../lib/secretGuardTransport.js'
import { createCatalogController } from './createCatalogController.js'

const SECRET = `AKIA${'A'.repeat(16)}`
const controllers = []
function memoryStorage() {
  const values = new Map()
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: vi.fn((key, value) => values.set(key, value)),
    removeItem: (key) => values.delete(key),
  }
}
function setup(storage, context = {}, adapters = {}) {
  const services = {
    getTools: vi.fn(async () => []),
    getCapabilities: vi.fn(async () => ({ families: [] })),
    routePrompt: vi.fn(async (_mock, text, _tools, options) => {
      const verdict = guardedText(text, options)
      if (!verdict.ok) throw new SecretRefusedError(verdict.refusal)
      return { lane: 'run', tool: 'fence', confidence: 1 }
    }),
  }
  const controller = createCatalogController({
    services, adapters: { draftStorage: storage, ...adapters },
    context: { accountScope: 'guest', draftRoute: '/try', ...context },
  })
  controllers.push(controller)
  return { controller, services }
}
beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  for (const controller of controllers.splice(0)) controller.destroy()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('catalog command-bar draft integration', () => {
  it('saves setPrompt and restores after the console boot reset, with one toast', async () => {
    const storage = memoryStorage()
    const first = setup(storage).controller
    first.actions.setPrompt('draw a 20 ft fence')
    vi.advanceTimersByTime(400)
    first.destroy()
    const onDraftRestored = vi.fn()
    const restored = setup(storage, {}, { onDraftRestored }).controller
    restored.start()
    restored.actions.resetTransient()
    await Promise.resolve()
    expect(restored.getState().prompt).toBe('draw a 20 ft fence')
    expect(onDraftRestored).toHaveBeenCalledExactlyOnceWith('draw a 20 ft fence')
    expect(notificationBus.push).toHaveBeenCalledExactlyOnceWith({ text: 'Draft restored' })
    restored.start()
    restored.setContext({ running: true })
    restored.destroy()
    restored.start()
    await Promise.resolve()
    expect(notificationBus.push).toHaveBeenCalledTimes(1)
  })

  it('does not replace text typed before deferred restoration', async () => {
    const storage = memoryStorage()
    storage.setItem(composerDraftKey('guest', '/try'), 'old draft')
    const { controller } = setup(storage)
    controller.start()
    controller.actions.setPrompt('fresh edit')
    await Promise.resolve()
    expect(controller.getState().prompt).toBe('fresh edit')
    expect(notificationBus.push).not.toHaveBeenCalled()
  })

  it('never writes a credential, and a send override cannot authorise storage', async () => {
    const storage = memoryStorage()
    const { controller } = setup(storage)
    controller.actions.setPrompt(SECRET)
    vi.advanceTimersByTime(400)
    expect(storage.setItem).not.toHaveBeenCalled()
    await controller.actions.dispatch(undefined, { allowSecretOnce: true })
    expect(controller.getState().secretRefusal.id).toBe('aws_access_key')
    controller.actions.setPrompt(`api_key: ${'x'.repeat(24)}`)
    vi.advanceTimersByTime(400)
    await controller.actions.dispatch(undefined, { allowSecretOnce: true })
    expect(controller.getState().secretRefusal).toBeNull()
    expect(storage.setItem).not.toHaveBeenCalled()
  })

  it('isolates account and route and resets visible text when scope changes', async () => {
    const storage = memoryStorage()
    const { controller } = setup(storage, { accountScope: 'a' })
    controller.start()
    await Promise.resolve()
    controller.actions.setPrompt('a on try')
    vi.advanceTimersByTime(400)
    controller.setContext({ accountScope: 'b' })
    expect(controller.getState().prompt).toBe('')
    controller.actions.setPrompt('b on try')
    vi.advanceTimersByTime(400)
    controller.setContext({ draftRoute: '/app' })
    expect(controller.getState().prompt).toBe('')
    const reader = setup(storage, { accountScope: 'a' }).controller
    reader.start()
    await Promise.resolve()
    expect(reader.getState().prompt).toBe('a on try')
    expect(storage.getItem(composerDraftKey('b', '/try'))).toBe('b on try')
    expect(storage.getItem(composerDraftKey('b', '/app'))).toBeNull()
  })

  it('a throwing storage never interrupts editing, start or submit', async () => {
    const fail = () => { throw new Error('denied') }
    const { controller } = setup({ getItem: fail, setItem: fail, removeItem: fail })
    controller.start()
    await Promise.resolve()
    controller.actions.setPrompt('draw a fence')
    expect(() => vi.advanceTimersByTime(400)).not.toThrow()
    expect(controller.getState().prompt).toBe('draw a fence')
    await expect(controller.actions.dispatch()).resolves.toMatchObject({ lane: 'run' })
  })

  it.each([false, true])('clears saved and pending drafts on submit (pending=%s)', async (pending) => {
    const storage = memoryStorage()
    const { controller, services } = setup(storage)
    controller.actions.setPrompt('draw a fence')
    if (!pending) vi.advanceTimersByTime(400)
    await controller.actions.dispatch()
    vi.advanceTimersByTime(400)
    expect(services.routePrompt).toHaveBeenCalledTimes(1)
    expect(storage.getItem(composerDraftKey('guest', '/try'))).toBeNull()
    const reader = setup(storage).controller
    reader.start()
    await Promise.resolve()
    expect(reader.getState().prompt).toBe('')
  })

  it('a blocked submit keeps the draft until the controller can dispatch', async () => {
    const storage = memoryStorage()
    const { controller, services } = setup(storage, { running: true })
    controller.actions.setPrompt('draw a fence')
    vi.advanceTimersByTime(400)
    await controller.actions.dispatch()
    expect(services.routePrompt).not.toHaveBeenCalled()
    expect(storage.getItem(composerDraftKey('guest', '/try'))).toBe('draw a fence')
  })

  it('a drawing command clears the draft before the local fast path returns', async () => {
    const storage = memoryStorage()
    const { controller, services } = setup(storage, {}, { drawingCommand: () => true })
    controller.actions.setPrompt('LINE')
    vi.advanceTimersByTime(400)
    await controller.actions.dispatch()
    expect(services.routePrompt).not.toHaveBeenCalled()
    expect(controller.getState().prompt).toBe('')
    expect(storage.getItem(composerDraftKey('guest', '/try'))).toBeNull()
  })

  it('does not submit a previous account draft after browser identity changes', async () => {
    const storage = memoryStorage()
    vi.stubGlobal('localStorage', storage)
    vi.stubGlobal('window', { location: { pathname: '/try' } })
    const { controller, services } = setup(storage, { accountScope: undefined, draftRoute: undefined })
    try {
      controller.actions.setPrompt('guest draft')
      vi.advanceTimersByTime(400)
      // An undecodable signed-in token disables persistence and clears the
      // guest text before dispatch can read it as an account's prompt.
      storage.setItem('leaf.jwt', 'opaque-account-token')
      await controller.actions.dispatch()
      expect(controller.getState().prompt).toBe('')
      expect(services.routePrompt).not.toHaveBeenCalled()
      expect(storage.getItem(composerDraftKey('guest', '/try'))).toBe('guest draft')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
