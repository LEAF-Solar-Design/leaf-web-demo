import React, { useSyncExternalStore } from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, cleanup, fireEvent, render, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

vi.mock('./telemetry.js', () => ({ track: vi.fn(), setTourStep: vi.fn() }))
vi.mock('./engineChanges.js', async (original) => ({
  ...await original(), listEngineChanges: vi.fn(async () => ({ kind: 'forbidden' })),
}))
vi.mock('./converse.js', () => ({
  openStream: vi.fn(), postMessage: vi.fn(), resolveApproval: vi.fn(),
  listPendingApprovals: vi.fn(async () => []), cancelTurn: vi.fn(),
  classifyAgentError: vi.fn(() => 'unreachable'),
}))

import { openStream } from './converse.js'
import ConversePanel from './components/ConversePanel.jsx'
import CapabilityCatalog from './components/CapabilityCatalog.jsx'
import NavRail from './site/NavRail.jsx'
import { PersistentSeat } from './workspace/ProjectWorkspacePanels.jsx'
import { createCatalogController } from './controllers/catalog/createCatalogController.js'
import { shellWiring } from './test-support/catalogPublicationWiring.mjs'

const sources = {
  studio: readFileSync(resolve(process.cwd(), 'src/App.jsx'), 'utf8'),
  toolcast: readFileSync(resolve(process.cwd(), 'src/site/ToolCast.jsx'), 'utf8'),
}
const subscriptions = [], owners = [], hosts = []
const oldTool = { name: 'old-tool', description: 'Existing tool', params: {}, capabilities: [] }
const newTool = { ...oldTool, name: 'server-new-tool' }
const catalogOf = (tool) => ({ source: 'endpoint', families: [
  { family_id: 'custom', label: 'Custom tools', capabilities: [tool] },
] })
const publication = { sessionId: 'session-1', changeSetId: 'change-1', catalogDigest: 'digest-1' }
const event = (patch = {}) => ({
  v: 1, session_id: 'session-1', turn_id: 'turn-1', seq: 1, type: 'tool_result',
  data: {
    tool: 'request_publication', ok: true, summary: 'Published',
    result: { contract: 'leaf.customization.v1', status: 'published',
      change_set_id: 'change-1', catalog_digest: 'digest-1' },
    toolRecord: { ...newTool, name: 'chat-only-tool' },
  },
  ...patch,
})
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })
async function deliver(subscription, env = event()) {
  await act(async () => { subscription.handlers.onEvent(env); await Promise.resolve() })
}
function host() {
  const node = document.createElement('div')
  document.body.appendChild(node)
  hosts.push(node)
  return node
}
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
async function owner() {
  const services = {
    getTools: vi.fn(async () => [oldTool]),
    getCapabilities: vi.fn(async () => catalogOf(oldTool)),
    routePrompt: vi.fn(),
  }
  const controller = createCatalogController({ services, adapters: { draftStorage: null }, context: { mock: true } })
  owners.push(controller)
  controller.start()
  await flush()
  expect(services.getTools).toHaveBeenCalledTimes(1)
  expect(services.getCapabilities).toHaveBeenCalledTimes(1)
  services.getTools.mockClear()
  services.getCapabilities.mockClear()
  controller.actions.setFamilyOpen('custom', true)
  const changed = vi.fn(controller.actions.refreshCatalog)
  const claim = vi.fn(controller.consumeCatalogPublication)
  const upsert = vi.fn(controller.actions.upsertTool)
  const author = vi.fn(), job = vi.fn()
  return { controller, services, changed, claim, upsert, author, job,
    value: { actions: { ...controller.actions, refreshCatalog: changed, upsertTool: upsert },
      controller: { ...controller, consumeCatalogPublication: claim } } }
}

// One turn the shell already dispatched into this session before the publication arrives.
const history = [{ turnId: 'turn-0', text: 'Publish a tool' }]
function Seat({ shell, o, destination, shown = true, turns = [] }) {
  const state = useSyncExternalStore(o.controller.subscribe, o.controller.getState, o.controller.getState)
  const wiring = shellWiring(sources[shell], shell, { ...o.value, state }, {
    onPublishAuthor: o.author, publishAuthoredTool: o.author,
    onAttachAgentJob: o.job, refreshJobs: o.job, attachJob: o.job,
    agentTurns: turns, turns,
  })
  expect(wiring.panel.userTurns).toBe(turns)
  const panel = shown ? <ConversePanel {...wiring.panel} /> : null
  const catalogProps = shell === 'studio'
    ? wiring.props('NavRail', ['railFamilies', 'catalogFamilyCount', 'capCount', 'catalogSource',
      'catalogErr', 'onRetryCatalog', 'tools', 'toolsErr', 'onRetryTools', 'openFamilies', 'onToggleFamily'])
    : wiring.props('CapabilityCatalog', ['catalog', 'catalogError', 'openFamilies', 'onToggleFamily',
      'onRetryCatalog', 'tools', 'toolsError', 'onRetryTools'])
  return <>
    {shell === 'studio' ? <PersistentSeat destination={destination}>{panel}</PersistentSeat> : panel}
    <div data-testid="publication-catalog">
      {shell === 'studio'
        ? <NavRail {...catalogProps} activeSurface="cad" studio toolsOpen />
        : <CapabilityCatalog {...catalogProps} />}
    </div>
  </>
}
function cycle(o, count) {
  expect(o.changed).toHaveBeenCalledTimes(count)
  expect(o.services.getTools).toHaveBeenCalledTimes(count)
  expect(o.services.getCapabilities).toHaveBeenCalledTimes(count)
  expect(o.upsert).not.toHaveBeenCalled()
  expect(o.author).not.toHaveBeenCalled()
  expect(o.job).not.toHaveBeenCalled()
}
beforeEach(() => {
  subscriptions.length = 0
  openStream.mockImplementation((sessionId, after, handlers) => {
    const close = vi.fn()
    subscriptions.push({ sessionId, after, handlers, close })
    return { close }
  })
})
afterEach(() => {
  cleanup()
  owners.splice(0).forEach((controller) => controller.destroy())
  hosts.splice(0).forEach((node) => node.remove())
  vi.clearAllMocks()
})

async function rendersOnce(shell) {
  const o = await owner(), destination = host()
  const view = render(<Seat shell={shell} o={o} destination={destination} turns={history} />)
  const subscription = subscriptions.at(-1)
  const catalog = within(view.getByTestId('publication-catalog'))
  expect(view.getByText('Publish a tool')).toBeTruthy()
  const flat = deferred(), grouped = deferred()
  o.services.getTools.mockImplementationOnce(() => flat.promise)
  o.services.getCapabilities.mockImplementationOnce(() => grouped.promise)
  await deliver(subscription, event({ session_id: 'foreign-session' }))
  cycle(o, 0)
  expect(o.claim).not.toHaveBeenCalled()
  await deliver(subscription)
  cycle(o, 1)
  expect(o.changed).toHaveBeenCalledWith(publication)
  expect(o.claim).toHaveBeenCalledWith(publication)
  expect(o.controller.getState().tools).toEqual([oldTool])
  expect(catalog.queryByText('server-new-tool')).toBeNull()
  expect(catalog.queryByText('chat-only-tool')).toBeNull()
  await deliver(subscription, event({ seq: 99, turn_id: 'another-turn', data: {
    ...event().data, result: { ...event().data.result, catalog_digest: 'another-digest' },
  } }))
  cycle(o, 1)
  await act(async () => { flat.resolve([newTool]); grouped.resolve(catalogOf(newTool)) })
  expect(catalog.getByText('server-new-tool')).toBeTruthy()
  expect(catalog.queryByText('old-tool')).toBeNull()
  expect(catalog.queryByText('chat-only-tool')).toBeNull()
  expect(o.controller.getState().tools).toEqual([newTool])
  expect(o.controller.getState().catalog).toEqual(catalogOf(newTool))
  expect(openStream).toHaveBeenCalledTimes(1)
  expect(subscription.close).not.toHaveBeenCalled()
  cycle(o, 1)
}

it('C41B-13 Studio renders the server tool once and ignores foreign and replayed results', async () => {
  await rendersOnce('studio')
})
it('C41B-14 ToolCast renders the server tool once and ignores foreign and replayed results', async () => {
  await rendersOnce('toolcast')
})
it('C41B-15 Studio reseating and remounting retain the publication claim', async () => {
  const o = await owner(), rail = host(), board = host()
  const view = render(<Seat shell="studio" o={o} destination={rail} />)
  const first = subscriptions.at(-1)
  await deliver(first)
  const log = within(rail).getByRole('log')
  view.rerender(<Seat shell="studio" o={o} destination={board} />)
  expect(within(board).getByRole('log')).toBe(log)
  expect(openStream).toHaveBeenCalledTimes(1)
  expect(first.close).not.toHaveBeenCalled()
  await deliver(first)
  view.rerender(<Seat shell="studio" o={o} destination={board} shown={false} />)
  expect(first.close).toHaveBeenCalledTimes(1)
  view.rerender(<Seat shell="studio" o={o} destination={rail} />)
  expect(openStream).toHaveBeenCalledTimes(2)
  expect(subscriptions.at(-1).after).toBe(0)
  await deliver(subscriptions.at(-1))
  cycle(o, 1)
})
it('C41B-16 ToolCast remounting retains the publication claim', async () => {
  const o = await owner()
  const view = render(<Seat shell="toolcast" o={o} />)
  const first = subscriptions.at(-1)
  await deliver(first)
  view.rerender(<Seat shell="toolcast" o={o} shown={false} />)
  expect(first.close).toHaveBeenCalledTimes(1)
  view.rerender(<Seat shell="toolcast" o={o} />)
  expect(openStream).toHaveBeenCalledTimes(2)
  expect(subscriptions.at(-1).after).toBe(0)
  await deliver(subscriptions.at(-1))
  cycle(o, 1)
})
it('C41B-17 the two shell owners consume the same identity independently', async () => {
  const studio = await owner(), toolcast = await owner()
  render(<Seat shell="studio" o={studio} destination={host()} />)
  const studioStream = subscriptions.at(-1)
  render(<Seat shell="toolcast" o={toolcast} />)
  const toolcastStream = subscriptions.at(-1)
  await deliver(studioStream)
  cycle(studio, 1); cycle(toolcast, 0)
  await deliver(toolcastStream)
  cycle(studio, 1); cycle(toolcast, 1)
  await deliver(studioStream); await deliver(toolcastStream)
  cycle(studio, 1); cycle(toolcast, 1)
})

async function retries(shell) {
  const o = await owner()
  const view = render(<Seat shell={shell} o={o} destination={host()} />)
  o.services.getTools.mockResolvedValue([newTool]).mockRejectedValueOnce(new Error('flat failed'))
  o.services.getCapabilities.mockResolvedValue(catalogOf(newTool)).mockRejectedValueOnce(new Error('grouped failed'))
  await deliver(subscriptions.at(-1))
  cycle(o, 1)
  await deliver(subscriptions.at(-1))
  cycle(o, 1)
  expect(o.controller.getState().tools).toEqual([oldTool])
  expect(o.controller.getState().catalog.families).toEqual([])
  const catalog = within(view.getByTestId('publication-catalog'))
  const buttons = shell === 'studio' ? catalog.getAllByRole('button', { name: 'Retry', exact: true })
    : [catalog.getByRole('button', { name: 'Retry families', exact: true }), catalog.getByRole('button', { name: 'Retry', exact: true })]
  expect(buttons).toHaveLength(2)
  await act(async () => { fireEvent.click(buttons[1]) })
  expect(o.services.getTools).toHaveBeenCalledTimes(2)
  expect(o.services.getCapabilities).toHaveBeenCalledTimes(1)
  await act(async () => { fireEvent.click(buttons[0]) })
  expect(o.services.getCapabilities).toHaveBeenCalledTimes(2)
  expect(catalog.getByText('server-new-tool')).toBeTruthy()
  await deliver(subscriptions.at(-1))
  expect(o.services.getTools).toHaveBeenCalledTimes(2)
  expect(o.services.getCapabilities).toHaveBeenCalledTimes(2)
  expect(o.changed).toHaveBeenCalledTimes(1)
  expect(o.upsert).not.toHaveBeenCalled()
  expect(o.author).not.toHaveBeenCalled()
  expect(openStream).toHaveBeenCalledTimes(1)
}
it('C41B-18 Studio retry controls recover without replaying publication', async () => {
  await retries('studio')
})
it('C41B-19 ToolCast retry controls recover without replaying publication', async () => {
  await retries('toolcast')
})
