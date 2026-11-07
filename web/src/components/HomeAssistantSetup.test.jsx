// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createHash, webcrypto } from 'node:crypto'
import { TextDecoder, TextEncoder } from 'node:util'
import HomeAssistantSetup from './HomeAssistantSetup.jsx'
import useDeviceRegistry from '../useDeviceRegistry.js'

vi.mock('../api.js', () => ({ authHeaders: () => ({}), config: { apiBase: '' }, noteUnauthorized: (response) => response }))
const ops = vi.hoisted(() => Object.fromEntries(['importConnector', 'getConnector', 'publishConnector', 'getConnectorVersion', 'createConnection', 'getConnectionSummary', 'setConnectionCredential', 'getActionCatalog'].map((name) => [name, vi.fn()])))
vi.mock('../integrations/client.js', () => ({ createIntegrationsClient: () => ops }))
const json = (body, status = 200) => ({ ok: status < 400, status, json: async () => body })
let commands
let outcomes
const ok = (data = {}) => ({ status: 'ok', data })
function Harness({ active = true, mock = false }) {
  const registry = useDeviceRegistry({ projectId: 'p1', mock })
  return <HomeAssistantSetup deviceId="d1" active={active} mock={mock} disabled={registry.busy} onCommand={registry.command} onGetCommand={registry.getCommand} onRelayToken={registry.relayToken} />
}
beforeEach(() => {
  Object.values(ops).forEach((method) => method.mockReset())
  commands = []
  outcomes = []
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } })
  vi.stubGlobal('fetch', vi.fn(async (url, init) => {
    if (url.endsWith('/relay-tokens')) return json({ relay_token: 'single-display-relay-secret', relay_origin: 'https://studio.example.test', path_prefix: '/api/device-relay/homeassistant', ha_token: 'never-render-ha-secret' }, 201)
    if (url.endsWith('/commands')) { commands.push(JSON.parse(init.body)); return json({ command_id: `c${commands.length}` }, 202) }
    if (url.includes('/commands/c')) return json({ result: outcomes.shift() || ok() })
    return json({ devices: [] })
  }))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); delete navigator.clipboard })
const step = (button) => within(screen.getByRole('button', { name: button }).closest('li'))

it('onboards only after an ok device result and never sends credentials', async () => {
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Set up Home Assistant' }))
  expect(await step('Set up Home Assistant').findByText('Done')).toBeInTheDocument()
  expect(commands).toEqual([{ op: 'ha.onboard', args: {} }])
})

it.each(['error', 'refused'])('does not claim onboarding success for a %s result', async (status) => {
  outcomes.push({ status, error: { code: 'paused', message: 'sensitive failure payload' } })
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Set up Home Assistant' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('This device is paused.')
  expect(step('Set up Home Assistant').queryByText('Done')).not.toBeInTheDocument()
  expect(screen.queryByText('sensitive failure payload')).not.toBeInTheDocument()
})

it('hue init step auto-selects manual then asks for the bridge IP', async () => {
  outcomes.push(ok({ type: 'form', step_id: 'init', flow_id: 'f1' }))
  let settle
  const originalFetch = fetch.getMockImplementation()
  fetch.mockImplementation((url, init) => url.includes('/commands/c2')
    ? new Promise((resolve) => { settle = resolve }) : originalFetch(url, init))
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Link Hue bridge' }))
  await waitFor(() => expect(settle).toBeTypeOf('function'))
  expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
  expect(screen.queryByText(/Press the round button/)).not.toBeInTheDocument()
  expect(screen.queryByText(/s remaining|Button window expired/)).not.toBeInTheDocument()
  await act(async () => settle(json({ result: ok({ type: 'form', step_id: 'manual', flow_id: 'f1' }) })))
  expect(await screen.findByLabelText('Hue bridge IP address')).toBeInTheDocument()
  expect(step('Link Hue bridge').getByText('Enter bridge IP')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
  expect(commands).toEqual([
    { op: 'ha.flow.start', args: { handler: 'hue' } },
    { op: 'ha.flow.step', args: { flow_id: 'f1', data: { id: 'manual' } } },
  ])
})

it('hue init auto step is sent only once', async () => {
  outcomes.push(
    ok({ type: 'form', step_id: 'init', flow_id: 'f1' }),
    ok({ type: 'form', step_id: 'init', flow_id: 'f1' }),
  )
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Link Hue bridge' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The Hue setup did not provide a link step.')
  expect(step('Link Hue bridge').getByText('Needs attention')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
  expect(commands).toEqual([
    { op: 'ha.flow.start', args: { handler: 'hue' } },
    { op: 'ha.flow.step', args: { flow_id: 'f1', data: { id: 'manual' } } },
  ])
})

it.each([
  { status: 'error', error: { code: 'upstream_error' } },
  ok({ type: 'form', step_id: 'init', flow_id: 'f1', errors: { base: 'upstream_error' } }),
])('shows Needs attention when the Hue init auto step fails: %j', async (result) => {
  outcomes.push(ok({ type: 'form', step_id: 'init', flow_id: 'f1' }), result)
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Link Hue bridge' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Home Assistant could not be reached')
  expect(step('Link Hue bridge').getByText('Needs attention')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
  expect(commands).toHaveLength(2)
})

it('hue manual step asks for the bridge IP and submits host', async () => {
  outcomes.push(
    ok({ type: 'form', step_id: 'manual', flow_id: 'f1' }),
    ok({ type: 'form', step_id: 'link', flow_id: 'f1' }),
  )
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Link Hue bridge' }))
  const field = await screen.findByLabelText('Hue bridge IP address')
  expect(step('Link Hue bridge').getByText('Enter bridge IP')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
  expect(screen.queryByText(/Press the round button/)).not.toBeInTheDocument()
  expect(screen.queryByText(/s remaining|Button window expired/)).not.toBeInTheDocument()
  fireEvent.change(field, { target: { value: '10.0.0.226' } })
  fireEvent.click(screen.getByRole('button', { name: 'Use this bridge' }))
  expect(await screen.findByText('Press the round button on top of your Hue bridge, then click Continue')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Continue' })).toBeInTheDocument()
  expect(await screen.findByText('30 s remaining')).toBeInTheDocument()
  expect(screen.queryByLabelText('Hue bridge IP address')).not.toBeInTheDocument()
  expect(commands).toEqual([
    { op: 'ha.flow.start', args: { handler: 'hue' } },
    { op: 'ha.flow.step', args: { flow_id: 'f1', data: { host: '10.0.0.226' } } },
  ])
})

it('hue manual step rejects an invalid IP', async () => {
  outcomes.push(ok({ type: 'form', step_id: 'manual', flow_id: 'f1' }))
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Link Hue bridge' }))
  const field = await screen.findByLabelText('Hue bridge IP address')
  fireEvent.change(field, { target: { value: '999.0.0.226' } })
  fireEvent.click(screen.getByRole('button', { name: 'Use this bridge' }))
  expect(screen.getByRole('alert')).toHaveTextContent('Enter an IPv4 address, such as 192.168.1.20.')
  expect(field).toHaveAttribute('aria-invalid', 'true')
  expect(commands).toEqual([{ op: 'ha.flow.start', args: { handler: 'hue' } }])
  fireEvent.change(field, { target: { value: '10.0.0.226' } })
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('walks Hue link, retains the flow after not_linked and retries Continue to success', async () => {
  vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
  outcomes.push(
    ok({ flow_id: 'hue-flow', type: 'form', step_id: 'link' }),
    ok({ flow_id: 'hue-flow', type: 'form', step_id: 'link', errors: { base: 'not_linked' } }),
    ok({ type: 'create_entry' }),
  )
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Link Hue bridge' }))
  expect(await screen.findByText('Press the round button on top of your Hue bridge, then click Continue')).toBeInTheDocument()
  expect(screen.getByText('30 s remaining')).toBeInTheDocument()
  expect(step('Link Hue bridge').queryByText('Done')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The Hue button has not been detected yet.')
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  expect(await step('Link Hue bridge').findByText('Done')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
  expect(commands).toEqual([
    { op: 'ha.flow.start', args: { handler: 'hue' } },
    { op: 'ha.flow.step', args: { flow_id: 'hue-flow', data: {} } },
    { op: 'ha.flow.step', args: { flow_id: 'hue-flow', data: {} } },
  ])
})

it('explains an expired Hue button window and still allows the owner to retry', async () => {
  vi.useFakeTimers()
  outcomes.push(ok({ flow_id: 'hue-flow', type: 'form', step_id: 'link' }))
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Link Hue bridge' }))
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  await act(async () => vi.advanceTimersByTimeAsync(30000))
  expect(screen.getByText(/Button window expired/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Continue' })).not.toBeDisabled()
})

it('does not present aborted or unrecognized Hue flow results as success', async () => {
  outcomes.push(ok({ type: 'abort', reason: 'upstream_error' }))
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Link Hue bridge' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Home Assistant could not be reached')
  expect(step('Link Hue bridge').queryByText('Done')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Continue' })).not.toBeInTheDocument()
})

it('discovers WiZ lights and adds each discovered host without rendering opaque discovery data', async () => {
  outcomes.push(ok({ flows: [
    { handler: 'wiz', context: { host: '192.168.1.20' }, token: 'discovery-secret' },
    { handler: 'wiz', data: { host: '192.168.1.21' } },
    { handler: 'hue', context: { host: '192.168.1.22' } },
    { handler: 'wiz', host: 'https://unsafe.test' },
  ] }), ok({ type: 'create_entry' }), ok({ type: 'create_entry' }))
  const { container } = render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Add WiZ lights' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Add all' }))
  await waitFor(() => expect(commands).toHaveLength(3))
  await waitFor(() => expect(step('Add WiZ lights').getByText('Done')).toBeInTheDocument())
  expect(commands).toEqual([
    { op: 'ha.flow.list', args: {} },
    { op: 'ha.flow.start', args: { handler: 'wiz', data: { host: '192.168.1.20' } } },
    { op: 'ha.flow.start', args: { handler: 'wiz', data: { host: '192.168.1.21' } } },
  ])
  expect(container.innerHTML).not.toMatch(/discovery-secret|unsafe.test/)
})

it('validates manual WiZ IPv4 input before enqueuing', async () => {
  outcomes.push(ok({ type: 'create_entry' }))
  render(<Harness />)
  const field = screen.getByLabelText('WiZ IPv4 address')
  fireEvent.change(field, { target: { value: '999.168.1.20' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add by IP' }))
  expect(screen.getByRole('alert')).toHaveTextContent('Enter an IPv4 address')
  expect(commands).toHaveLength(0)
  fireEvent.change(field, { target: { value: '192.168.1.20' } })
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Add by IP' }))
  expect(await step('Add WiZ lights').findByText('Done')).toBeInTheDocument()
  expect(commands[0]).toEqual({ op: 'ha.flow.start', args: { handler: 'wiz', data: { host: '192.168.1.20' } } })
})

it('loads only light entities through the proxy and creates the selected All lights group', async () => {
  outcomes.push(ok({ status: 200, body_json: [
    { entity_id: 'light.kitchen', attributes: { friendly_name: 'Kitchen', ha_token: 'private-state' } },
    { entity_id: 'light.hall', attributes: { friendly_name: 'Hall' } },
    { entity_id: 'switch.socket', attributes: { friendly_name: 'Socket' } },
  ] }), ok({ type: 'create_entry' }))
  const { container } = render(<Harness />)
  expect(screen.getByRole('button', { name: 'Create All lights group' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Load light entities' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Kitchen' }))
  expect(screen.queryByRole('checkbox', { name: 'Socket' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Create All lights group' }))
  expect(await step('Create All lights group').findByText('Done')).toBeInTheDocument()
  expect(commands).toEqual([
    { op: 'http.proxy', args: { package: 'homeassistant', method: 'GET', path: '/api/states' } },
    { op: 'ha.flow.start', args: { handler: 'group', data: { group_type: 'light', name: 'All lights', entities: ['light.kitchen'] } } },
  ])
  expect(container.innerHTML).not.toContain('private-state')
})

it('does not turn an upstream HTTP failure into a successful entity load', async () => {
  outcomes.push(ok({ status: 401, body_json: { message: 'private-state' } }))
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Load light entities' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Home Assistant could not be reached')
  expect(screen.getByRole('button', { name: 'Create All lights group' })).toBeDisabled()
})

it('mints an Ops relay token with exact fields, displays once, and clears on copy', async () => {
  const { container, rerender } = render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Connect to Ops' }))
  expect(await screen.findByLabelText('Relay token')).toHaveTextContent('single-display-relay-secret')
  expect(screen.getByText(/It will not be shown again/)).toBeInTheDocument()
  expect(screen.getByText('https://studio.example.test')).toBeInTheDocument()
  expect(screen.getByText('/api/device-relay/homeassistant')).toBeInTheDocument()
  expect(container.innerHTML).not.toContain('never-render-ha-secret')
  const post = fetch.mock.calls.find(([url]) => url.endsWith('/relay-tokens'))
  expect(JSON.parse(post[1].body)).toEqual({ package: 'homeassistant', label: 'Ops' })
  fireEvent.click(screen.getByRole('button', { name: 'Copy token' }))
  await waitFor(() => expect(screen.queryByLabelText('Relay token')).not.toBeInTheDocument())
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith('single-display-relay-secret')
  rerender(<Harness active={false} />)
  rerender(<Harness active />)
  expect(screen.queryByText('single-display-relay-secret')).not.toBeInTheDocument()
})

it('clears a displayed relay token on close even without copying', async () => {
  const { rerender } = render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Connect to Ops' }))
  await screen.findByLabelText('Relay token')
  rerender(<Harness active={false} />)
  expect(screen.queryByText('single-display-relay-secret')).not.toBeInTheDocument()
  rerender(<Harness active />)
  expect(screen.queryByLabelText('Relay token')).not.toBeInTheDocument()
})

it('discards a relay credential whose response arrives after close', async () => {
  let settle
  fetch.mockImplementation(async (url) => url.endsWith('/relay-tokens')
    ? new Promise((resolve) => { settle = resolve }) : json({ devices: [] }))
  const { rerender } = render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Connect to Ops' }))
  await waitFor(() => expect(settle).toBeTypeOf('function'))
  rerender(<Harness active={false} />)
  await act(async () => settle(json({ relay_token: 'late-secret', relay_origin: 'https://studio.example.test', path_prefix: '/api/device-relay/homeassistant' }, 201)))
  rerender(<Harness active />)
  expect(screen.queryByText('late-secret')).not.toBeInTheDocument()
})

it('stops command polling on close and ignores a late success', async () => {
  vi.useFakeTimers()
  outcomes.push(null)
  fetch.mockImplementation(async (url, init) => {
    if (url.endsWith('/commands')) { commands.push(JSON.parse(init.body)); return json({ command_id: 'c1' }, 202) }
    if (url.includes('/commands/c1')) return json({ result: null })
    return json({ devices: [] })
  })
  const { rerender } = render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Set up Home Assistant' }))
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  rerender(<Harness active={false} />)
  const calls = fetch.mock.calls.length
  await act(async () => vi.advanceTimersByTimeAsync(5000))
  expect(fetch).toHaveBeenCalledTimes(calls)
  expect(step('Set up Home Assistant').queryByText('Done')).not.toBeInTheDocument()
})

it('makes no network calls for any setup action in mock mode', () => {
  render(<Harness mock />)
  for (const button of screen.getAllByRole('button')) fireEvent.click(button)
  expect(fetch).not.toHaveBeenCalled()
})

function opsFixture() {
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('TextDecoder', TextDecoder)
  const projectId = '11111111-1111-4111-8111-111111111111'
  const deviceId = 'abcdef12-3456-4567-8901-234567890123'
  const id = 'ha-relay-abcdef12'
  const operationIds = ['list-states', 'light-turn-on', 'light-turn-off', 'scene-turn-on']
  const reference = { id, version: 1, digest: 'a'.repeat(64) }
  const definition = { operations: operationIds.map((id) => ({ id })) }
  const connector = { id, reference, revision: 1, state: 'draft', draft_definition: definition }
  const published = { reference, revision: 2, definition }
  const connection = { id, title: 'Kitchen lights', revision: 1, state: 'setup', connector_ref: reference,
    origin: window.location.origin, operation_ids: operationIds }
  const document = { openapi: '3.0.3', servers: [{ url: window.location.origin }], paths: {} }
  const bytes = new TextEncoder().encode(JSON.stringify(document))
  const digest = createHash('sha256').update(bytes).digest('hex')
  const order = []
  fetch.mockImplementation(async () => {
    order.push('document')
    return { ok: true, headers: new Headers({ 'X-Leaf-Document-Sha256': digest }), arrayBuffer: async () => bytes.buffer }
  })
  for (const [name, value] of Object.entries({ importConnector: { reference, revision: 1, issues: [] }, getConnector: connector,
    publishConnector: published, getConnectorVersion: published, createConnection: connection, getConnectionSummary: connection,
    setConnectionCredential: { ...connection, revision: 2 },
    getActionCatalog: { actions: [{ connection_id: id, title: 'Turn lights on', operation_ref: { id: 'light-turn-on' } }], next_cursor: null } })) {
    ops[name].mockImplementation(async (_, options) => {
      if (name === 'getActionCatalog' && options?.limit === 25) return { actions: [], next_cursor: null }
      order.push(name)
      return value
    })
  }
  const onRelayToken = vi.fn(async () => {
    order.push('mint')
    return { relay_token: 'ops-only-secret', relay_origin: window.location.origin, path_prefix: '/api/device-relay/homeassistant' }
  })
  const props = { projectId, deviceId, onRelayToken }
  return { props, id, document, reference, operationIds, connector, published, connection, order, onRelayToken }
}

const clickUseInOps = () => fireEvent.click(screen.getByRole('button', { name: 'Use in Ops' }))
const labelOps = () => fireEvent.change(screen.getByLabelText('Connector label'), { target: { value: 'Kitchen lights' } })

it('connects the relay to the current project in order, including all write operations, without rendering its token', async () => {
  const fixture = opsFixture()
  const { container } = render(<HomeAssistantSetup {...fixture.props} />)
  const snapshots = []
  const observer = new MutationObserver(() => snapshots.push(container.innerHTML))
  observer.observe(container, { subtree: true, childList: true, characterData: true, attributes: true })
  labelOps()
  clickUseInOps()
  expect(step('Use in Ops').getByText('Connecting')).toBeInTheDocument()
  expect(await screen.findByText('Connected to Ops')).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Lights control' })).toBeInTheDocument()
  expect(await screen.findByRole('button', { name: 'Load lights' })).toBeInTheDocument()
  expect(step('Use in Ops').getByText('Kitchen lights')).toBeInTheDocument()
  expect(fixture.order).toEqual(['document', 'importConnector', 'getConnector', 'publishConnector', 'createConnection', 'mint', 'setConnectionCredential', 'getActionCatalog', 'getActionCatalog'])
  const options = { signal: expect.any(AbortSignal) }
  expect(fetch).toHaveBeenCalledWith('/api/devices/relay-openapi/homeassistant.json', { ...options, cache: 'no-store', redirect: 'error' })
  expect(ops.importConnector).toHaveBeenCalledWith(fixture.props.projectId, { document: fixture.document, options: { id: fixture.id, title: 'Kitchen lights' } }, options)
  expect(ops.publishConnector).toHaveBeenCalledWith(fixture.props.projectId, fixture.id, { version: 1, expectedRevision: 1, readOperationIds: fixture.operationIds }, options)
  expect(ops.createConnection).toHaveBeenCalledWith(fixture.props.projectId, { id: fixture.id, title: 'Kitchen lights', connectorRef: fixture.reference, origin: window.location.origin, operationIds: fixture.operationIds }, options)
  expect(fixture.onRelayToken).toHaveBeenCalledWith(fixture.props.deviceId, 'Kitchen lights')
  expect(ops.setConnectionCredential).toHaveBeenCalledWith(fixture.props.projectId, fixture.id, { purpose: 'access_token', value: 'ops-only-secret', expectedRevision: 1 }, options)
  expect(ops.getActionCatalog).toHaveBeenCalledWith(fixture.props.projectId, { ...options, limit: 50 })
  expect(container.innerHTML).not.toContain('ops-only-secret')
  observer.disconnect()
  expect(snapshots.join('')).not.toContain('ops-only-secret')
  expect(screen.queryByLabelText('Relay token')).not.toBeInTheDocument()
})

it('stops before import if the document digest differs', async () => {
  const fixture = opsFixture()
  const original = fetch.getMockImplementation()
  fetch.mockImplementation(async () => ({ ...await original(), headers: new Headers({ 'X-Leaf-Document-Sha256': '0'.repeat(64) }) }))
  render(<HomeAssistantSetup {...fixture.props} />)
  clickUseInOps()
  expect(await screen.findByRole('alert')).toHaveTextContent('document could not be verified')
  expect(ops.importConnector).not.toHaveBeenCalled()
  expect(fixture.onRelayToken).not.toHaveBeenCalled()
})

it.each([
  ['importConnector', 'Could not import'],
  ['publishConnector', 'Could not publish'],
  ['createConnection', 'Could not create the Ops connection'],
  ['setConnectionCredential', 'Could not save the relay credential'],
  ['getActionCatalog', 'Turn lights on is not available'],
])('humanizes a %s failure without reflecting secret-bearing errors', async (method, message) => {
  const fixture = opsFixture()
  ops[method].mockRejectedValue(new Error('ops-only-secret'))
  const { container } = render(<HomeAssistantSetup {...fixture.props} />)
  clickUseInOps()
  expect(await screen.findByRole('alert')).toHaveTextContent(message)
  expect(container.innerHTML).not.toContain('ops-only-secret')
  expect(screen.queryByText('Connected to Ops')).not.toBeInTheDocument()
  if (method === 'setConnectionCredential') expect(ops.getActionCatalog.mock.calls.every(([, options]) => options.limit === 25)).toBe(true)
})

it('reuses the published connector and connection on a repeat click', async () => {
  const fixture = opsFixture()
  render(<HomeAssistantSetup {...fixture.props} />)
  labelOps()
  clickUseInOps()
  await screen.findByText('Connected to Ops')
  ops.importConnector.mockRejectedValue({ status: 409 })
  ops.getConnector.mockResolvedValue({ ...fixture.connector, state: 'published', revision: 2 })
  ops.createConnection.mockRejectedValue({ status: 409 })
  ops.getConnectionSummary.mockResolvedValue({ ...fixture.connection, revision: 3, state: 'active' })
  clickUseInOps()
  await screen.findByText('Connected to Ops')
  expect(ops.publishConnector).toHaveBeenCalledTimes(1)
  expect(ops.getConnectorVersion).toHaveBeenCalledWith(fixture.props.projectId, fixture.id, 1, { signal: expect.any(AbortSignal) })
  expect(ops.setConnectionCredential).toHaveBeenLastCalledWith(fixture.props.projectId, fixture.id, { purpose: 'access_token', value: 'ops-only-secret', expectedRevision: 3 }, { signal: expect.any(AbortSignal) })
})

it('recovers a publish conflict by reading the published version', async () => {
  const fixture = opsFixture()
  ops.publishConnector.mockRejectedValue({ status: 409 })
  render(<HomeAssistantSetup {...fixture.props} />)
  clickUseInOps()
  expect(await screen.findByText('Connected to Ops')).toBeInTheDocument()
  expect(ops.getConnectorVersion).toHaveBeenCalledTimes(1)
})

it('retries a failed credential handoff using the existing setup connection', async () => {
  const fixture = opsFixture()
  ops.setConnectionCredential.mockRejectedValueOnce(new Error('ops-only-secret'))
  render(<HomeAssistantSetup {...fixture.props} />)
  labelOps()
  clickUseInOps()
  await screen.findByRole('alert')
  ops.importConnector.mockRejectedValue({ code: 'already_exists' })
  ops.getConnector.mockResolvedValue({ ...fixture.connector, state: 'published', revision: 2 })
  ops.createConnection.mockRejectedValue({ status: 409 })
  clickUseInOps()
  expect(await screen.findByText('Connected to Ops')).toBeInTheDocument()
  expect(ops.getConnectionSummary).toHaveBeenCalledTimes(1)
  expect(fixture.onRelayToken).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('does not dispatch duplicate attempts while connecting', async () => {
  const fixture = opsFixture()
  let settle
  ops.importConnector.mockImplementation(() => new Promise((resolve) => { settle = resolve }))
  render(<HomeAssistantSetup {...fixture.props} />)
  clickUseInOps()
  await waitFor(() => expect(settle).toBeTypeOf('function'))
  clickUseInOps()
  expect(ops.importConnector).toHaveBeenCalledTimes(1)
  await act(async () => settle({ reference: fixture.reference, revision: 1, issues: [] }))
  expect(await screen.findByText('Connected to Ops')).toBeInTheDocument()
})

it('requires the action to belong to this connection and follows catalog pages', async () => {
  const fixture = opsFixture()
  const catalog = ops.getActionCatalog.getMockImplementation()
  ops.getActionCatalog.mockImplementation((projectId, options) => options.limit === 50 && !options.after
    ? Promise.resolve({ actions: [{ connection_id: 'other', title: 'Turn lights on', operation_ref: { id: 'light-turn-on' } }], next_cursor: 'next' })
    : catalog(projectId, options))
  render(<HomeAssistantSetup {...fixture.props} />)
  clickUseInOps()
  await screen.findByText('Connected to Ops')
  expect(ops.getActionCatalog).toHaveBeenCalledWith(fixture.props.projectId, { signal: expect.any(AbortSignal), limit: 50, after: 'next' })
})

it('does not claim success when this connection has no Turn lights on action', async () => {
  const fixture = opsFixture()
  ops.getActionCatalog.mockResolvedValue({ actions: [], next_cursor: null })
  render(<HomeAssistantSetup {...fixture.props} />)
  clickUseInOps()
  expect(await screen.findByRole('alert')).toHaveTextContent('Turn lights on is not available')
  expect(screen.queryByText('Connected to Ops')).not.toBeInTheDocument()
})

it('stops after a late mint result when the project changes, without saving or exposing the token', async () => {
  const fixture = opsFixture()
  let settle
  fixture.onRelayToken.mockImplementation(() => new Promise((resolve) => { settle = resolve }))
  const { container, rerender } = render(<HomeAssistantSetup {...fixture.props} />)
  clickUseInOps()
  await waitFor(() => expect(settle).toBeTypeOf('function'))
  rerender(<HomeAssistantSetup {...fixture.props} projectId="22222222-2222-4222-8222-222222222222" />)
  await act(async () => settle({ relay_token: 'ops-only-secret', relay_origin: window.location.origin }))
  expect(ops.setConnectionCredential).not.toHaveBeenCalled()
  expect(container.innerHTML).not.toContain('ops-only-secret')
  expect(screen.queryByText('Connected to Ops')).not.toBeInTheDocument()
})

it('requires a project before enabling Use in Ops', () => {
  const fixture = opsFixture()
  render(<HomeAssistantSetup {...fixture.props} projectId={undefined} />)
  expect(screen.getByRole('button', { name: 'Use in Ops' })).toBeDisabled()
  expect(screen.getByText('Open a project first')).toBeInTheDocument()
  clickUseInOps()
  expect(fetch).not.toHaveBeenCalled()
})

it('restores this device connection from the catalog on a fresh mount and loads its lights', async () => {
  const fixture = opsFixture()
  ops.getActionCatalog.mockResolvedValue({ actions: [{ connection_id: fixture.id, operation_ref: { id: 'light-turn-on' } }], next_cursor: null })
  const onCommand = vi.fn().mockResolvedValue({ command_id: 'load-lights' })
  const onGetCommand = vi.fn().mockResolvedValue({ result: ok({ status: 200, body_json: [
    { entity_id: 'light.desk', attributes: { friendly_name: 'Desk', token: 'private-state' } },
  ] }) })
  const { container } = render(<HomeAssistantSetup {...fixture.props} onCommand={onCommand} onGetCommand={onGetCommand} />)
  expect(await screen.findByText('Connected to Ops')).toBeInTheDocument()
  fireEvent.click(await screen.findByRole('button', { name: 'Load lights' }))
  expect(await screen.findByRole('group', { name: 'Desk' })).toBeInTheDocument()
  expect(onCommand).toHaveBeenCalledWith(fixture.props.deviceId, 'http.proxy', { package: 'homeassistant', method: 'GET', path: '/api/states' })
  expect(fixture.onRelayToken).not.toHaveBeenCalled()
  expect(container.innerHTML).not.toContain('private-state')
})
