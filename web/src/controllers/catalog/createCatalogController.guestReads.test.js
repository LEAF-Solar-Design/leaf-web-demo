import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../telemetry.js', () => ({ track: vi.fn() }))

import { createCatalogController } from './createCatalogController.js'

const controllers = []
function setup(context = {}, overrides = {}) {
  const services = {
    getTools: vi.fn(async () => []),
    getCapabilities: vi.fn(async () => ({ families: [] })),
    routePrompt: vi.fn(),
    ...overrides,
  }
  const controller = createCatalogController({ services, context, adapters: { draftStorage: null } })
  controllers.push(controller)
  return { controller, services }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

afterEach(() => {
  for (const controller of controllers.splice(0)) controller.destroy()
  vi.clearAllMocks()
})

describe('catalog reads for known guests', () => {
  it('starts paused without requests or errors, then loads each service once when resumed', async () => {
    const { controller, services } = setup({ guestReadsPaused: true })
    controller.start()
    await Promise.resolve()

    expect(services.getTools).not.toHaveBeenCalled()
    expect(services.getCapabilities).not.toHaveBeenCalled()
    expect(controller.getState()).toMatchObject({ toolsError: null, catalogError: null })

    controller.setContext({ guestReadsPaused: false })
    await Promise.resolve()

    expect(services.getTools).toHaveBeenCalledExactlyOnceWith(false)
    expect(services.getCapabilities).toHaveBeenCalledTimes(1)
  })

  it.each(['result', 'error'])('discards an in-flight %s when reads become paused', async (outcome) => {
    const tools = deferred()
    const catalog = deferred()
    const { controller, services } = setup({}, {
      getTools: vi.fn(() => tools.promise),
      getCapabilities: vi.fn(() => catalog.promise),
    })
    controller.start()
    await Promise.resolve()
    expect(services.getTools).toHaveBeenCalledTimes(1)
    expect(services.getCapabilities).toHaveBeenCalledTimes(1)

    controller.setContext({ guestReadsPaused: true })
    const before = controller.getState()
    const publish = vi.fn()
    controller.subscribe(publish)
    if (outcome === 'result') {
      tools.resolve([{ name: 'late-tool' }])
      catalog.resolve({ families: [{ family_id: 'late-family', capabilities: [] }] })
    } else {
      tools.reject(new Error('late tools failure'))
      catalog.reject(new Error('late catalog failure'))
    }
    await Promise.allSettled([tools.promise, catalog.promise])

    expect(publish).not.toHaveBeenCalled()
    expect(controller.getState()).toBe(before)
    expect(controller.getState()).toMatchObject({
      tools: [], toolsError: null, catalog: { families: [], source: null }, catalogError: null,
    })
  })

  it('loads each service once when resuming and switching to mock together', async () => {
    const { controller, services } = setup({ guestReadsPaused: true })
    controller.start()
    controller.setContext({ mock: true, guestReadsPaused: false })
    await Promise.resolve()

    expect(services.getTools).toHaveBeenCalledExactlyOnceWith(true)
    expect(services.getCapabilities).toHaveBeenCalledTimes(1)
    expect(services.getCapabilities.mock.calls[0][0]).toBe(true)
  })

  it('keeps the default start behavior of loading both services once', async () => {
    const { controller, services } = setup()
    controller.start()
    await Promise.resolve()

    expect(services.getTools).toHaveBeenCalledExactlyOnceWith(false)
    expect(services.getCapabilities).toHaveBeenCalledTimes(1)
  })
})
