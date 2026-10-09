import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'

vi.mock('../telemetry.js', () => ({ track: vi.fn(), setTourStep: vi.fn() }))
vi.mock('../engineChanges.js', async (importOriginal) => ({
  ...await importOriginal(),
  listEngineChanges: vi.fn(async () => ({ kind: 'forbidden' })),
}))
vi.mock('../converse.js', () => ({
  openStream: vi.fn(() => ({ close: vi.fn() })),
  postMessage: vi.fn(),
  resolveApproval: vi.fn(),
  listPendingApprovals: vi.fn(async () => []),
  cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'unreachable'),
}))

import { openStream } from '../converse.js'
import ConversePanel from './ConversePanel.jsx'
import { createCatalogController } from '../controllers/catalog/createCatalogController.js'

const C = 'leaf.customization.v1'
const I = '7f3a51f0-9d9a-43be-8d29-cbdba31249c8'
const H = 'a'.repeat(64)
const P = { contract: C, change_set_id: I, status: 'published', catalog_digest: H }
const publication = { sessionId: 'session-1', changeSetId: I, catalogDigest: H }
const oldTools = [{ name: 'old-tool' }]
const oldCatalog = { families: [{ family_id: 'custom', capabilities: oldTools }], source: 'server' }
const subscriptions = []
const owners = []
const event = (patch = {}) => ({
  v: 1, session_id: 'session-1', turn_id: 'turn-1', seq: 1, type: 'tool_result',
  data: { tool: 'request_publication', ok: true, summary: 'The tool was published', result: { ...P } },
  ...patch,
})
const resultEvent = (patch) => event({ data: { ...event().data, result: { ...P, ...patch } } })
const flush = async () => { await act(async () => { await Promise.resolve() }) }
async function deliver(env, subscription = subscriptions.at(-1)) {
  await act(async () => { subscription.handlers.onEvent(env); await Promise.resolve() })
}
async function owner() {
  const services = {
    getTools: vi.fn(async () => oldTools),
    getCapabilities: vi.fn(async () => oldCatalog),
    routePrompt: vi.fn(async () => ({ lane: 'run', tool: null })),
  }
  const controller = createCatalogController({ services, adapters: { draftStorage: null }, context: { mock: true } })
  owners.push(controller)
  controller.start()
  await flush()
  expect(services.getTools).toHaveBeenCalledTimes(1)
  expect(services.getCapabilities).toHaveBeenCalledTimes(1)
  services.getTools.mockClear()
  services.getCapabilities.mockClear()
  const changed = vi.fn(controller.actions.refreshCatalog)
  const claim = vi.fn(controller.consumeCatalogPublication)
  return { controller, services, changed, claim }
}
function panel(o, props = {}) {
  return <ConversePanel sessionId="session-1" onDismiss={vi.fn()}
    onCatalogChanged={o.changed} consumeCatalogPublication={o.claim} {...props} />
}
function cycle(o, count = 1) {
  expect(o.changed).toHaveBeenCalledTimes(count)
  expect(o.services.getTools).toHaveBeenCalledTimes(count)
  expect(o.services.getCapabilities).toHaveBeenCalledTimes(count)
}
beforeEach(() => {
  subscriptions.length = 0
  openStream.mockImplementation((_session, _seq, handlers) => {
    const close = vi.fn()
    subscriptions.push({ handlers, close })
    return { close }
  })
})
afterEach(() => {
  cleanup()
  for (const controller of owners.splice(0)) controller.destroy()
  vi.clearAllMocks()
})

describe('chat publication delivery to the catalog owner', () => {
  it('C41-01 a published result notifies the owner once with its identity and digest', async () => {
    const o = await owner()
    render(panel(o))
    await deliver(event())
    expect(o.changed).toHaveBeenCalledWith(publication)
    expect(o.claim).toHaveBeenCalledWith(publication)
    cycle(o)
  })

  it('C41-02 the same event delivered twice refreshes once', async () => {
    const o = await owner()
    render(panel(o))
    const env = event()
    await deliver(env); await deliver(env)
    cycle(o)
  })

  it('C41-03 another sequence, turn or digest for the same publication refreshes nothing more', async () => {
    const o = await owner()
    render(panel(o))
    await deliver(event())
    await deliver(event({ seq: 2 }))
    await deliver(event({ seq: 3, turn_id: 'other-turn' }))
    await deliver(resultEvent({ catalog_digest: 'b'.repeat(64) }))
    cycle(o)
  })

  it('C41-04 a remount replaying from zero refreshes nothing more', async () => {
    const o = await owner()
    const first = render(panel(o))
    await deliver(event())
    first.unmount()
    render(panel(o))
    expect(openStream.mock.calls.map((call) => call[1])).toEqual([0, 0])
    await deliver(event())
    cycle(o)
  })

  it('C41-05 a StrictMode replay of the subscription refreshes once', async () => {
    const o = await owner()
    render(<React.StrictMode>{panel(o)}</React.StrictMode>)
    expect(subscriptions).toHaveLength(2)
    expect(subscriptions[0].close).toHaveBeenCalledTimes(1)
    await deliver(event(), subscriptions[0])
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event(), subscriptions[1])
    await deliver(event(), subscriptions[1])
    cycle(o)
  })

  it('C41-06 awaiting approval consumes nothing and the later publication refreshes once', async () => {
    const o = await owner()
    render(panel(o))
    await deliver(event({ data: { ...event().data, result: { contract: C, change_set_id: I, status: 'awaiting_approval' } } }))
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it.each(['denied', 'staging', 'failed'])('C41-07 a %s result consumes nothing and a later success still refreshes', async (status) => {
    const o = await owner()
    render(panel(o))
    await deliver(event({ data: { ...event().data,
      ok: status !== 'failed',
      result: status === 'failed' ? { ...P } : { contract: C, change_set_id: I, status },
    } }))
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it.each([
    ['data that is null', (env) => { env.data = null }],
    ['data that is an array', (env) => { env.data = [] }],
    ['an absent result', (env) => { delete env.data.result }],
    ['a null result', (env) => { env.data.result = null }],
    ['an array result', (env) => { env.data.result = [] }],
    ['another contract', (env) => { env.data.result.contract = 'other.v1' }],
    ['another tool', (env) => { env.data.tool = 'other_tool' }],
    ['another event type', (env) => { env.type = 'tool_call' }],
    ['ok as the string true', (env) => { env.data.ok = 'true' }],
    ['an absent change set id', (env) => { delete env.data.result.change_set_id }],
    ['a null change set id', (env) => { env.data.result.change_set_id = null }],
    ['a numeric change set id', (env) => { env.data.result.change_set_id = 7 }],
    ['an empty change set id', (env) => { env.data.result.change_set_id = '' }],
    ['a padded change set id', (env) => { env.data.result.change_set_id = ` ${I} ` }],
    ['a 129 character change set id', (env) => { env.data.result.change_set_id = 'a'.repeat(129) }],
    ['an absent catalog digest', (env) => { delete env.data.result.catalog_digest }],
    ['a null catalog digest', (env) => { env.data.result.catalog_digest = null }],
    ['a numeric catalog digest', (env) => { env.data.result.catalog_digest = 7 }],
    ['an empty catalog digest', (env) => { env.data.result.catalog_digest = '' }],
    ['a padded catalog digest', (env) => { env.data.result.catalog_digest = ` ${H} ` }],
    ['a 129 character catalog digest', (env) => { env.data.result.catalog_digest = 'a'.repeat(129) }],
  ])('C41-08 %s is rejected and consumes nothing', async (_label, corrupt) => {
    const o = await owner()
    render(panel(o))
    const env = event()
    corrupt(env)
    // Even malformed data carries persuasive prose; only the structured result counts.
    if (env.data === null) env.summary = 'The tool was published'
    else env.data.summary = 'The tool was published'
    await deliver(env)
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it('C41-08 identifiers of length 1 and 128 are accepted unchanged', async () => {
    const o = await owner()
    render(panel(o))
    for (const length of [1, 128]) {
      const id = 'Z'.repeat(length), digest = 'Q'.repeat(length)
      await deliver(resultEvent({ change_set_id: id, catalog_digest: digest }))
      expect(o.changed).toHaveBeenLastCalledWith({ sessionId: 'session-1', changeSetId: id, catalogDigest: digest })
    }
    cycle(o, 2)
  })

  it('C41-08 a missing callback or claim consumes nothing', async () => {
    const o = await owner()
    const view = render(panel(o, { onCatalogChanged: null }))
    await deliver(event())
    expect(o.claim).not.toHaveBeenCalled()
    view.rerender(panel(o, { consumeCatalogPublication: null }))
    await deliver(event())
    cycle(o, 0)
    view.unmount()
    render(panel(o))
    await deliver(event())
    cycle(o)
  })

  it('C41-09 two change sets refresh twice and the same id in another session is its own publication', async () => {
    const o = await owner()
    const view = render(panel(o))
    await deliver(event())
    await deliver(resultEvent({ change_set_id: 'second-change' }))
    cycle(o, 2)
    view.rerender(panel(o, { sessionId: 'session-2' }))
    await deliver(event({ session_id: 'session-2' }))
    cycle(o, 3)
  })

  it('C41-10 a replaced callback is the one that runs and the stream is not reopened', async () => {
    const o = await owner()
    const view = render(panel(o))
    await deliver(event())
    const next = vi.fn(o.controller.actions.refreshCatalog)
    const nextClaim = vi.fn(o.controller.consumeCatalogPublication)
    view.rerender(panel(o, { onCatalogChanged: next, consumeCatalogPublication: nextClaim }))
    expect(openStream).toHaveBeenCalledTimes(1)
    expect(subscriptions[0].close).not.toHaveBeenCalled()
    await deliver(resultEvent({ change_set_id: 'new-change' }))
    expect(o.changed).toHaveBeenCalledTimes(1)
    expect(o.claim).toHaveBeenCalledTimes(1)
    expect(next).toHaveBeenCalledTimes(1)
    expect(nextClaim).toHaveBeenCalledTimes(1)
    expect(o.services.getTools).toHaveBeenCalledTimes(2)
    expect(o.services.getCapabilities).toHaveBeenCalledTimes(2)
    expect(openStream).toHaveBeenCalledTimes(1)
    expect(subscriptions[0].close).not.toHaveBeenCalled()
  })

  it("C41-11 an old session's handler claims nothing after a switch or cleanup", async () => {
    const o = await owner()
    const view = render(panel(o))
    const old = subscriptions[0]
    view.rerender(panel(o, { sessionId: 'session-2' }))
    expect(old.close).toHaveBeenCalledTimes(1)
    await deliver(event(), old)
    expect(o.claim).not.toHaveBeenCalled()
    const current = subscriptions[1]
    view.unmount()
    await deliver(event(), old)
    await deliver(event({ session_id: 'session-2' }), current)
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
  })

  it('C41-11 an envelope with a missing or different session is rejected', async () => {
    const o = await owner()
    render(panel(o))
    const missing = event()
    delete missing.session_id
    await deliver(missing)
    await deliver(event({ session_id: '' }))
    await deliver(event({ session_id: 'session-other' }))
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it.each(['the flat list', 'the grouped catalog', 'both lists'])('C41-12 %s failing keeps the publication consumed and the retry action recovers', async (label) => {
    const o = await owner()
    render(panel(o))
    const flatFails = label !== 'the grouped catalog', groupedFails = label !== 'the flat list'
    if (flatFails) o.services.getTools.mockRejectedValueOnce(new Error('flat failed'))
    if (groupedFails) o.services.getCapabilities.mockRejectedValueOnce(new Error('grouped failed'))
    await deliver(event())
    cycle(o)
    expect(o.controller.getState().tools).toEqual(oldTools)
    expect(o.controller.getState().toolsError).toBe(flatFails ? 'flat failed' : null)
    expect(o.controller.getState().catalog).toEqual(groupedFails ? { families: [], source: null } : oldCatalog)
    expect(o.controller.getState().catalogError).toBe(groupedFails ? 'grouped failed' : null)
    await deliver(event())
    cycle(o)
    if (flatFails) await o.controller.actions.retryTools()
    if (groupedFails) await o.controller.actions.loadCatalog()
    expect(o.controller.getState().toolsError).toBeNull()
    expect(o.controller.getState().catalogError).toBeNull()
    expect(o.controller.getState().tools).toEqual(oldTools)
    expect(o.controller.getState().catalog).toEqual(oldCatalog)
    expect(o.services.getTools).toHaveBeenCalledTimes(flatFails ? 2 : 1)
    expect(o.services.getCapabilities).toHaveBeenCalledTimes(groupedFails ? 2 : 1)
    expect(o.changed).toHaveBeenCalledTimes(1)
  })

  it("C41-14 the refreshed catalog shows the server's new tool and never the chat's", async () => {
    const o = await owner()
    render(panel(o))
    const serverTool = { name: 'new-server-tool' }
    const chatTool = { name: 'chat-invention' }
    const serverCatalog = { families: [{ family_id: 'custom', capabilities: [serverTool] }], source: 'server' }
    o.services.getTools.mockResolvedValueOnce([serverTool])
    o.services.getCapabilities.mockResolvedValueOnce(serverCatalog)
    await deliver(event({ data: { ...event().data, invented_tool: chatTool } }))
    cycle(o)
    expect(o.controller.getState().tools).toEqual([serverTool])
    expect(o.controller.getState().catalog.families).toEqual(serverCatalog.families)
    expect(o.controller.getState().tools).not.toContainEqual(chatTool)
    expect(o.controller.getState().catalog.families.flatMap((family) => family.capabilities)).not.toContainEqual(chatTool)
  })

  it.each([
    ['the number 1', 1],
    ['the string true', 'true'],
    ['an object', {}],
  ])('C41-16 a claim returning %s notifies nothing', async (_label, value) => {
    const o = await owner()
    let claims = 0
    render(panel(o, { consumeCatalogPublication: () => { claims++; return value } }))
    await deliver(event())
    expect(claims).toBe(1)
    cycle(o, 0)
  })

  it.each(['a claim', 'a callback'])('C41-16 %s that throws does not interrupt the transcript', async (label) => {
    const o = await owner()
    let claims = 0, callbacks = 0
    const claim = label === 'a claim' ? () => {
      if (++claims === 1) throw new Error('claim failed')
      return true
    } : o.controller.consumeCatalogPublication
    const changed = (value) => {
      callbacks++
      if (label === 'a callback' && callbacks === 1) throw new Error('callback failed')
      return o.changed(value)
    }
    const view = render(panel(o, { consumeCatalogPublication: claim, onCatalogChanged: changed }))
    await deliver(event({ type: 'turn_started', data: {} }))
    await deliver(event({ type: 'tool_call', data: { tool: 'request_publication', args_summary: 'first change' } }))
    await expect(deliver(event())).resolves.toBeUndefined()
    const row = view.container.querySelector('.converse-step')
    expect(row).toHaveAttribute('data-state', 'done')
    expect(row.querySelector('.converse-step-state')).toHaveTextContent('Done')
    expect(row).toHaveTextContent('The tool was published')
    const before = callbacks
    await deliver(resultEvent({ change_set_id: 'second-change-after-throw' }))
    expect(callbacks).toBe(before + 1)
    expect(o.changed).toHaveBeenLastCalledWith({ ...publication, changeSetId: 'second-change-after-throw' })
  })

  it('C41-11 an event delivered by close during unmount claims nothing', async () => {
    const o = await owner()
    openStream.mockImplementation((_session, _seq, handlers) => {
      const close = () => handlers.onEvent(event())
      subscriptions.push({ handlers, close })
      return { close }
    })
    const view = render(panel(o))
    view.unmount()
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
  })

  it('C41-11 an envelope whose session only coerces to the opened session is rejected', async () => {
    const o = await owner()
    render(panel(o, { sessionId: '42' }))
    await deliver(event({ session_id: 42 }))
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event({ session_id: '42' }))
    expect(o.changed).toHaveBeenCalledWith({ ...publication, sessionId: '42' })
    cycle(o)
  })

  it.each(['awaiting_approval', 'denied', 'staging'])('C41-07 a %s result that carries a change set and a digest consumes nothing', async (status) => {
    const o = await owner()
    render(panel(o))
    await deliver(resultEvent({ status }))
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it.each([
    ['data that is an array carrying valid fields', (env) => { env.data = Object.assign([], env.data) }],
    ['a result that is an array carrying valid fields', (env) => { env.data.result = Object.assign([], env.data.result) }],
    ['a change set id padded only at the start', (env) => { env.data.result.change_set_id = ` ${I}` }],
    ['a change set id padded only at the end', (env) => { env.data.result.change_set_id = `${I} ` }],
    ['a catalog digest padded only at the start', (env) => { env.data.result.catalog_digest = ` ${H}` }],
    ['a catalog digest padded only at the end', (env) => { env.data.result.catalog_digest = `${H} ` }],
  ])('C41-08 %s consumes nothing', async (_label, corrupt) => {
    const o = await owner()
    render(panel(o))
    const env = event()
    corrupt(env)
    await deliver(env)
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    await deliver(event())
    cycle(o)
  })

  it('C41-08 a 1 character change set id with a 128 character digest is accepted unchanged', async () => {
    const o = await owner()
    render(panel(o))
    const changeSetId = 'Z', catalogDigest = 'Q'.repeat(128)
    await deliver(resultEvent({ change_set_id: changeSetId, catalog_digest: catalogDigest }))
    expect(o.changed).toHaveBeenCalledWith({ sessionId: 'session-1', changeSetId, catalogDigest })
    cycle(o)
  })

  it.each(['a callback', 'a claim'])('C41-08 %s that is truthy but not a function consumes nothing', async (label) => {
    const o = await owner()
    const view = render(panel(o, label === 'a callback' ? { onCatalogChanged: {} } : { consumeCatalogPublication: {} }))
    await deliver(event())
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    view.rerender(panel(o))
    await deliver(event())
    expect(o.claim).toHaveBeenCalledTimes(1)
    cycle(o)
  })

  it('C41-10 removing the callback stops notifications and the stream is not reopened', async () => {
    const o = await owner()
    const view = render(panel(o))
    view.rerender(panel(o, { onCatalogChanged: null }))
    await deliver(event())
    expect(o.claim).not.toHaveBeenCalled()
    cycle(o, 0)
    expect(openStream).toHaveBeenCalledTimes(1)
    expect(subscriptions[0].close).not.toHaveBeenCalled()
  })
})
