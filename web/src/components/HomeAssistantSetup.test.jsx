// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import HomeAssistantSetup from './HomeAssistantSetup.jsx'
import useDeviceRegistry from '../useDeviceRegistry.js'

vi.mock('../api.js', () => ({ authHeaders: () => ({}), config: { apiBase: '' }, noteUnauthorized: (response) => response }))
const json = (body, status = 200) => ({ ok: status < 400, status, json: async () => body })
let commands
let outcomes
const ok = (data = {}) => ({ status: 'ok', data })
function Harness({ active = true, mock = false }) {
  const registry = useDeviceRegistry({ projectId: 'p1', mock })
  return <HomeAssistantSetup deviceId="d1" active={active} mock={mock} disabled={registry.busy} onCommand={registry.command} onGetCommand={registry.getCommand} onRelayToken={registry.relayToken} />
}
beforeEach(() => {
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

it('walks Hue link, retains the flow after not_linked and retries Continue to success', async () => {
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
