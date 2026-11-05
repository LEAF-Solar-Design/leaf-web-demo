// @vitest-environment jsdom
import { useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import LinkDeviceDrawer from './LinkDeviceDrawer.jsx'
import useDeviceRegistry from '../useDeviceRegistry.js'

vi.mock('../api.js', () => ({ authHeaders: () => ({}), config: { apiBase: '' }, noteUnauthorized: (response) => response }))
const json = (body, status = 200) => ({ ok: status < 400, status, json: async () => body })
const catalog = [{ package: 'homeassistant', display_name: 'Home Assistant', version: 'catalog-v2', port: 8123, health_path: '/api/', relay_allow: [] }]
const device = (over = {}) => ({
  device_id: 'd1', project_id: 'p1', label: 'Mac mini', hostname: 'mini.local',
  agent_version: '1.0', status: 'online', last_seen_at: new Date(Date.now() - 5000).toISOString(), revoked_at: null,
  packages: [{ package: 'homeassistant', version: 'pinned-v1', status: 'installed', last_health_at: null }], ...over,
})
function Harness({ mock = false, initiallyOpen = true }) {
  const registry = useDeviceRegistry({ projectId: 'p1', mock })
  const [open, onToggle] = useState(initiallyOpen)
  return <LinkDeviceDrawer devices={registry.devices} mock={mock} open={open} onToggle={onToggle} loading={registry.loading} busy={registry.busy} error={registry.error}
    catalog={registry.catalog} catalogLoading={registry.catalogLoading} catalogError={registry.catalogError} onLoadCatalog={registry.loadCatalog}
    onEnroll={registry.enroll} onRefresh={registry.refresh} onCommand={registry.command} onGetCommand={registry.getCommand} onAudit={registry.audit} onRevoke={registry.revoke} onRelayToken={registry.relayToken} />
}
beforeEach(() => {
  vi.useRealTimers()
  vi.stubGlobal('fetch', vi.fn(async (url) => json(url.endsWith('/packages') ? { packages: catalog } : { devices: [device()] })))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve() }) }
const ownerMessage = 'Only a project owner can link devices. Ask your workspace owner to make you an owner of this project.'

it.each([
  [403, { error: { code: 'forbidden', message: 'private owner gate diagnostics' } }, ownerMessage],
  [403, { detail: 'tenant owner authority required' }, ownerMessage],
  [403, 'private proxy response', ownerMessage],
  [401, { detail: 'private session diagnostics' }, 'Your session expired. Sign in again.'],
  [429, { error: { code: 'unexpected', message: 'private rate diagnostics' } }, 'Too many requests. Try again in a minute.'],
  [503, { detail: 'private upstream diagnostics' }, 'Device management is temporarily unavailable. Try again.'],
  [409, { error: { code: 'device_revoked', message: 'private device diagnostics' } }, 'This device was revoked. Link it again.'],
  [409, { detail: 'private conflict diagnostics' }, "That didn't work. Try again."],
  [500, { error: { code: 'forbidden', message: 'private failure diagnostics' } }, ownerMessage],
  [0, null, 'Device management is temporarily unavailable. Try again.'],
])('shows a safe enrollment refusal for HTTP %s with body %j', async (status, body, message) => {
  fetch.mockImplementation(async (url) => {
    if (url.endsWith('/enrollments')) {
      if (!status) throw new TypeError('private network diagnostics')
      if (typeof body === 'string') return { ok: false, status, json: async () => { throw new SyntaxError(body) } }
      return json(body, status)
    }
    return json(url.endsWith('/packages') ? { packages: catalog } : { devices: [] })
  })
  const { container } = render(<Harness />)
  fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'New Mac' } })
  const button = screen.getByRole('button', { name: 'Link a device' })
  await waitFor(() => expect(button).not.toBeDisabled())
  fireEvent.click(button)
  expect(await within(button.parentElement).findByRole('alert')).toHaveTextContent(message)
  expect(screen.getAllByRole('alert')).toHaveLength(1)
  expect(button).not.toBeDisabled()
  expect(container.innerHTML).not.toMatch(/private|tenant owner authority required|"error"|"detail"/)
})

it('clears an enrollment refusal when retrying, before the retry finishes', async () => {
  render(<Harness />)
  fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'New Mac' } })
  const button = screen.getByRole('button', { name: 'Link a device' })
  await waitFor(() => expect(button).not.toBeDisabled())
  fetch.mockResolvedValueOnce(json({ detail: 'tenant owner authority required' }, 403))
  fireEvent.click(button)
  expect(await screen.findByRole('alert')).toHaveTextContent(ownerMessage)
  let settle
  fetch.mockImplementationOnce(() => new Promise((resolve) => { settle = resolve }))
  fireEvent.click(button)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(button).toBeDisabled()
  await act(async () => settle(json({ install_command: 'official installer', expires_at: new Date(Date.now() + 600000).toISOString() }, 201)))
  expect(screen.getByLabelText('Install command')).toHaveTextContent('official installer')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('clears an enrollment refusal on close and keeps it cleared on reopening', async () => {
  render(<Harness />)
  fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'New Mac' } })
  const button = screen.getByRole('button', { name: 'Link a device' })
  await waitFor(() => expect(button).not.toBeDisabled())
  fetch.mockResolvedValueOnce(json({ error: { code: 'forbidden', message: 'private diagnostics' } }, 403))
  fireEvent.click(button)
  expect(await screen.findByRole('alert')).toHaveTextContent(ownerMessage)
  fireEvent.click(screen.getByRole('button', { name: 'Close devices panel' }))
  expect(screen.queryByText(ownerMessage)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Devices/ }))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it.each([
  ['command', 403, { detail: 'tenant owner authority required' }, ownerMessage],
  ['command', 409, { error: { code: 'device_revoked', message: 'private command diagnostics' } }, 'This device was revoked. Link it again.'],
  ['command', 422, { error: { code: 'version_unknown', message: 'private catalog diagnostics' } }, 'This version is not in the device catalog. Refresh the agent and retry.'],
  ['command', 422, { error: { code: 'path_denied' } }, 'This action is not allowed by the device catalog.'],
  ['command', 422, { error: { code: 'package_unknown' } }, 'This package is not in the device catalog.'],
  ['command', 422, { error: { code: 'unsupported_op' } }, 'This agent does not support that action. Update the agent and retry.'],
  ['command', 503, { error: { code: 'device_offline' } }, 'This device is offline. Check its connection and retry.'],
  ['command', 503, { error: { code: 'service_unavailable' } }, 'Device management is temporarily unavailable. Try again.'],
  ['command', 422, { error: { code: 'unknown_failure', message: 'private failure diagnostics' } }, "That didn't work. Try again."],
  ['revoke', 403, { error: { code: 'forbidden', message: 'private revoke diagnostics' } }, ownerMessage],
  ['revoke', 503, { detail: 'private revoke diagnostics' }, 'Device management is temporarily unavailable. Try again.'],
])('shows %s HTTP %s failures in the device row and clears them on retry and close', async (action, status, body, message) => {
  let retry = false
  let settle
  fetch.mockImplementation(async (url) => {
    if (url.endsWith(action === 'command' ? '/commands' : '/revoke')) {
      if (retry) return new Promise((resolve) => { settle = resolve })
      return json(body, status)
    }
    return json(url.endsWith('/packages') ? { packages: catalog } : { devices: [device()] })
  })
  const { container } = render(<Harness />)
  const install = await screen.findByRole('button', { name: 'Install Home Assistant' })
  await waitFor(() => expect(install).not.toBeDisabled())
  if (action === 'revoke') fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
  const button = action === 'command' ? install : screen.getByRole('button', { name: 'Confirm revoke' })
  fireEvent.click(button)
  const row = button.closest('.ca-account')
  expect(await within(row).findByRole('alert')).toHaveTextContent(message)
  if (action === 'command') {
    const request = fetch.mock.calls.find(([url]) => url.endsWith('/commands'))
    expect(JSON.parse(request[1].body)).toEqual({ op: 'pkg.install', args: { package: 'homeassistant', version: 'catalog-v2' } })
  }
  expect(screen.getAllByRole('alert')).toHaveLength(1)
  expect(container.innerHTML).not.toMatch(/private|tenant owner authority required/)
  await waitFor(() => expect(button).not.toBeDisabled())
  retry = true
  fireEvent.click(button)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  await act(async () => settle(json(body, status)))
  expect(await within(row).findByRole('alert')).toHaveTextContent(message)
  fireEvent.click(screen.getByRole('button', { name: 'Close devices panel' }))
  expect(screen.queryByText(message)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Devices/ }))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('renders nothing without device wiring', () => {
  const { container } = render(<LinkDeviceDrawer />)
  expect(container).toBeEmptyDOMElement()
})

it('refreshes on open and every 15 s while open, and stops after close', async () => {
  vi.useFakeTimers()
  const onRefresh = vi.fn().mockResolvedValue([])
  const devices = [device()]
  const { rerender, unmount } = render(<LinkDeviceDrawer devices={devices} onRefresh={onRefresh} />)
  expect(onRefresh).not.toHaveBeenCalled()
  rerender(<LinkDeviceDrawer open devices={devices} onRefresh={onRefresh} />)
  await flush()
  expect(onRefresh).toHaveBeenCalledTimes(1)
  await act(async () => vi.advanceTimersByTimeAsync(14999))
  expect(onRefresh).toHaveBeenCalledTimes(1)
  await act(async () => vi.advanceTimersByTimeAsync(1))
  expect(onRefresh).toHaveBeenCalledTimes(2)
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(onRefresh).toHaveBeenCalledTimes(3)
  rerender(<LinkDeviceDrawer devices={devices} onRefresh={onRefresh} />)
  await act(async () => vi.advanceTimersByTimeAsync(30000))
  expect(onRefresh).toHaveBeenCalledTimes(3)
  rerender(<LinkDeviceDrawer open devices={devices} onRefresh={onRefresh} />)
  await flush()
  expect(onRefresh).toHaveBeenCalledTimes(4)
  unmount()
  await act(async () => vi.advanceTimersByTimeAsync(30000))
  expect(onRefresh).toHaveBeenCalledTimes(4)
})

it('a stale device returns to Online after a background refresh and the setup panel reappears', async () => {
  vi.useFakeTimers()
  const packages = [{ package: 'homeassistant', version: 'pinned-v1', status: 'running', last_health_at: null }]
  const onRefresh = vi.fn().mockResolvedValue(undefined)
  function RefreshHarness() {
    const [devices, setDevices] = useState([device({ last_seen_at: new Date(Date.now() - 100000).toISOString(), packages })])
    return <LinkDeviceDrawer open devices={devices} onRefresh={() => onRefresh().then(() => {
      if (onRefresh.mock.calls.length > 1) setDevices([device({ packages })])
    })} />
  }
  render(<RefreshHarness />)
  await flush()
  expect(screen.getByText('Offline')).toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Home Assistant setup' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'Next Mac' } })
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(screen.getByText('Online')).toBeInTheDocument()
  const setup = screen.getByRole('region', { name: 'Home Assistant setup' })
  fireEvent.change(screen.getByLabelText('Connector label'), { target: { value: 'My connector' } })
  expect(screen.getByLabelText('Device label')).toHaveValue('Next Mac')
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(screen.getByRole('region', { name: 'Home Assistant setup' })).toBe(setup)
  expect(screen.getByLabelText('Connector label')).toHaveValue('My connector')
  expect(screen.getByLabelText('Device label')).toHaveValue('Next Mac')
})

it('a rejected background refresh shows no error', async () => {
  vi.useFakeTimers()
  const onRefresh = vi.fn().mockRejectedValue(new Error('Background refresh failed'))
  render(<LinkDeviceDrawer open devices={[device()]} onRefresh={onRefresh} />)
  await flush()
  expect(onRefresh).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(onRefresh).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.queryByText('Background refresh failed')).not.toBeInTheDocument()
})

it('no background refresh in mock mode', async () => {
  vi.useFakeTimers()
  const onRefresh = vi.fn()
  render(<LinkDeviceDrawer open mock devices={[]} onRefresh={onRefresh} />)
  await act(async () => vi.advanceTimersByTimeAsync(45000))
  expect(onRefresh).not.toHaveBeenCalled()
})

it('skips background ticks while a refresh is in flight', async () => {
  vi.useFakeTimers()
  let settle
  const onRefresh = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { settle = resolve })).mockResolvedValue([])
  render(<LinkDeviceDrawer open devices={[]} onRefresh={onRefresh} />)
  await act(async () => vi.advanceTimersByTimeAsync(30000))
  expect(onRefresh).toHaveBeenCalledTimes(1)
  await act(async () => settle([]))
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(onRefresh).toHaveBeenCalledTimes(2)
})

it('skips background ticks during an action and the enrollment countdown', async () => {
  vi.useFakeTimers()
  let settle
  const onEnroll = vi.fn(() => new Promise((resolve) => { settle = resolve }))
  const onRefresh = vi.fn().mockResolvedValue([])
  render(<LinkDeviceDrawer open devices={[]} onRefresh={onRefresh} onEnroll={onEnroll} />)
  await flush()
  fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'New Mac' } })
  fireEvent.click(screen.getByRole('button', { name: 'Link a device' }))
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  expect(onRefresh).toHaveBeenCalledTimes(1)
  await act(async () => settle({ install_command: 'official installer', expires_at: new Date(Date.now() + 60000).toISOString() }))
  await act(async () => vi.advanceTimersByTimeAsync(15000))
  // Only the countdown's five 3 s polls run alongside the opening refresh.
  expect(onRefresh).toHaveBeenCalledTimes(6)
  expect(screen.getByLabelText('Install command')).toBeInTheDocument()
})

it('renders a marked demo drawer and setup panel with no network calls', () => {
  render(<Harness mock />)
  expect(screen.getByRole('dialog', { name: 'Devices' })).toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Home Assistant setup' })).toBeInTheDocument()
  expect(screen.getByText(/Demo devices/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Set up Home Assistant' }))
  expect(fetch).not.toHaveBeenCalled()
})

it('shows offline, paused and revoked states with strict device field projection', () => {
  const { container } = render(<LinkDeviceDrawer open devices={[
    device({ token: 'hidden-token', public_key_private: 'hidden-private', ha_token: 'hidden-ha', status: 'online', last_seen_at: '2020-01-01T00:00:00Z' }),
    device({ device_id: 'd2', label: 'Paused Mac', status: 'paused' }),
    device({ device_id: 'd3', label: 'Retired Mac', status: 'paused', revoked_at: '2026-11-02T10:00:00Z' }),
  ]} />)
  expect(screen.getByText('Offline')).toBeInTheDocument()
  expect(screen.getByText('Paused')).toBeInTheDocument()
  expect(screen.getByText('Revoked')).toBeInTheDocument()
  expect(container.innerHTML).not.toMatch(/hidden-token|hidden-private|hidden-ha|public_key_private|ha_token/)
})

it('enrolls with an isolated label, counts down, polls every three seconds and reports Linked', async () => {
  vi.useFakeTimers()
  let linked = false
  fetch.mockImplementation(async (url) => {
    if (url.endsWith('/packages')) return json({ packages: catalog })
    if (url.endsWith('/enrollments')) return json({ token: 'not-displayed-separately', install_command: 'official-install --one-use-token', expires_at: new Date(Date.now() + 600000).toISOString() }, 201)
    return json({ devices: linked ? [device(), device({ device_id: 'd2', label: 'New Mac' })] : [device()] })
  })
  render(<Harness />)
  await flush()
  fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'New Mac' } })
  fireEvent.click(screen.getByRole('button', { name: 'Link a device' }))
  await flush()
  expect(screen.getByLabelText('Install command')).toHaveTextContent('official-install --one-use-token')
  expect(screen.getByText(/Expires in 600 s/)).toBeInTheDocument()
  expect(screen.queryByText('not-displayed-separately')).not.toBeInTheDocument()
  const post = fetch.mock.calls.find(([url]) => url.endsWith('/enrollments'))
  expect(JSON.parse(post[1].body)).toEqual({ project_id: 'p1', label: 'New Mac' })
  linked = true
  await act(async () => vi.advanceTimersByTimeAsync(3000))
  expect(screen.getByText('Linked', { exact: true })).toBeInTheDocument()
  expect(screen.queryByLabelText('Install command')).not.toBeInTheDocument()
  const calls = fetch.mock.calls.length
  await act(async () => vi.advanceTimersByTimeAsync(6000))
  expect(fetch).toHaveBeenCalledTimes(calls)
})

it('removes an expired install command and stops enrollment polling', async () => {
  vi.useFakeTimers()
  fetch.mockImplementation(async (url) => url.endsWith('/enrollments')
    ? json({ install_command: 'short-lived-command', expires_at: new Date(Date.now() + 2000).toISOString() }, 201)
    : json(url.endsWith('/packages') ? { packages: catalog } : { devices: [] }))
  render(<Harness />)
  await flush()
  fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'Mac' } })
  fireEvent.click(screen.getByRole('button', { name: 'Link a device' }))
  await flush()
  await act(async () => vi.advanceTimersByTimeAsync(2000))
  expect(screen.getByText(/Link expired/)).toBeInTheDocument()
  expect(screen.queryByText('short-lived-command')).not.toBeInTheDocument()
  const calls = fetch.mock.calls.length
  await act(async () => vi.advanceTimersByTimeAsync(6000))
  expect(fetch).toHaveBeenCalledTimes(calls)
})

it.each(['ok', 'error'])('polls installation to a terminal %s result without claiming early success', async (status) => {
  vi.useFakeTimers()
  let polls = 0
  fetch.mockImplementation(async (url) => {
    if (url.endsWith('/packages')) return json({ packages: catalog })
    if (url.endsWith('/commands')) return json({ command_id: 'c1' }, 202)
    if (url.endsWith('/commands/c1')) return json({ result: ++polls === 1 ? null : { status, ...(status === 'error' ? { error: { code: 'install_failed', message: 'secret failure details' } } : {}) } })
    return json({ devices: [device()] })
  })
  render(<Harness />)
  await flush()
  fireEvent.click(screen.getByRole('button', { name: 'Install Home Assistant' }))
  await flush()
  expect(screen.getByText('Installing Home Assistant')).toBeInTheDocument()
  expect(screen.queryByText('Home Assistant installed')).not.toBeInTheDocument()
  await act(async () => vi.advanceTimersByTimeAsync(1000))
  expect(screen.getByText(status === 'ok' ? 'Home Assistant installed' : 'Home Assistant could not be installed. Try Install again.')).toBeInTheDocument()
  expect(screen.queryByText('secret failure details')).not.toBeInTheDocument()
  const post = fetch.mock.calls.find(([url]) => url.endsWith('/commands'))
  expect(JSON.parse(post[1].body)).toEqual({ op: 'pkg.install', args: { package: 'homeassistant', version: 'catalog-v2' } })
})

it('uses inline revoke confirmation and refreshes only after the confirmed request', async () => {
  let revoked = false
  fetch.mockImplementation(async (url) => {
    if (url.endsWith('/packages')) return json({ packages: catalog })
    if (url.endsWith('/revoke')) { revoked = true; return json({ revoked_at: '2026-11-02T10:00:00Z' }) }
    return json({ devices: [device(revoked ? { status: 'revoked' } : {})] })
  })
  render(<Harness />)
  fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
  expect(revoked).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Keep' }))
  expect(screen.queryByRole('button', { name: 'Confirm revoke' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm revoke' }))
  expect(await screen.findByText('Revoked')).toBeInTheDocument()
})

it('shows only the latest 50 whitelisted audit records', async () => {
  fetch.mockImplementation(async (url) => url.includes('/audit') ? json({ events: Array.from({ length: 55 }, (_, i) => ({ at: `2026-11-02T10:00:${String(i).padStart(2, '0')}Z`, actor: 'device', op: 'pkg.health', target: `target-${i}`, status: 'ok', ha_token: 'audit-secret', body: 'hidden-body' })) }) : json({ devices: [device()] }))
  const { container } = render(<Harness />)
  fireEvent.click(await screen.findByRole('button', { name: 'Activity' }))
  expect(await screen.findByText(/Newest 50 events/)).toBeInTheDocument()
  expect(container.innerHTML).not.toMatch(/audit-secret|hidden-body/)
  expect(screen.queryByText(/target-0 /)).not.toBeInTheDocument()
})

it('fails closed when Home Assistant is absent from the package catalog', async () => {
  fetch.mockImplementation(async (url) => json(url.endsWith('/packages') ? { packages: [] } : { devices: [device()] }))
  render(<Harness />)
  expect(await screen.findByRole('button', { name: 'Install Home Assistant' })).toBeDisabled()
  expect(await screen.findByText(/Home Assistant is unavailable in the package catalog/)).toBeInTheDocument()
})

it('loads the catalog once on opening, disables install while loading, and installs on a fresh device', async () => {
  let settle
  fetch.mockImplementation(async (url) => {
    if (url.endsWith('/packages')) return new Promise((resolve) => { settle = resolve })
    if (url.endsWith('/commands')) return json({ command_id: 'first-install' }, 202)
    if (url.endsWith('/commands/first-install')) return json({ result: { status: 'ok' } })
    return json({ devices: [device({ packages: [] })] })
  })
  const { container } = render(<Harness initiallyOpen={false} />)
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
  expect(fetch.mock.calls.some(([url]) => url.endsWith('/packages'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: /Devices/ }))
  expect(await screen.findByText(/Loading package catalog/)).toBeInTheDocument()
  const installButton = screen.getByRole('button', { name: 'Install Home Assistant' })
  expect(installButton).toBeDisabled()
  await act(async () => settle(json({ packages: [{ ...catalog[0], token: 'catalog-secret' }] })))
  await waitFor(() => expect(installButton).not.toBeDisabled())
  fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'Another device' } })
  expect(fetch.mock.calls.filter(([url]) => url.endsWith('/packages'))).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Install Home Assistant' }))
  expect(await screen.findByText('Home Assistant installed')).toBeInTheDocument()
  const post = fetch.mock.calls.find(([url]) => url.endsWith('/commands'))
  expect(JSON.parse(post[1].body)).toEqual({ op: 'pkg.install', args: { package: 'homeassistant', version: 'catalog-v2' } })
  expect(container.innerHTML).not.toContain('catalog-secret')
})

it('keeps install disabled on catalog failure and recovers through Retry catalog', async () => {
  let failed = true
  fetch.mockImplementation(async (url) => url.endsWith('/packages')
    ? failed ? json({ error: { code: 'service_unavailable', message: 'catalog-private-details' } }, 503) : json({ packages: catalog })
    : json({ devices: [device()] }))
  render(<Harness />)
  expect(await screen.findByText(/Package catalog unavailable/)).toHaveTextContent('Device management is temporarily unavailable.')
  expect(screen.getByRole('button', { name: 'Install Home Assistant' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Install Home Assistant' }))
  expect(fetch.mock.calls.some(([url]) => url.endsWith('/commands'))).toBe(false)
  expect(screen.queryByText('catalog-private-details')).not.toBeInTheDocument()
  failed = false
  fireEvent.click(screen.getByRole('button', { name: 'Retry catalog' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Install Home Assistant' })).not.toBeDisabled())
  expect(screen.queryByText(/Package catalog unavailable/)).not.toBeInTheDocument()
})

it('checks package health through the command queue and then exposes setup', async () => {
  fetch.mockImplementation(async (url) => {
    if (url.endsWith('/packages')) return json({ packages: catalog })
    if (url.endsWith('/commands')) return json({ command_id: 'health1' }, 202)
    if (url.endsWith('/commands/health1')) return json({ result: { status: 'ok', data: { package: 'homeassistant', version: 'pinned-v1', status: 'running', last_health_at: new Date().toISOString() } } })
    return json({ devices: [device()] })
  })
  render(<Harness />)
  const health = await screen.findByRole('button', { name: 'Health' })
  await waitFor(() => expect(health).not.toBeDisabled())
  fireEvent.click(health)
  expect(await screen.findByText('Home Assistant is healthy')).toBeInTheDocument()
  expect(await screen.findByRole('region', { name: 'Home Assistant setup' })).toBeInTheDocument()
  const post = fetch.mock.calls.find(([url]) => url.endsWith('/commands'))
  expect(JSON.parse(post[1].body)).toEqual({ op: 'pkg.health', args: { package: 'homeassistant' } })
})

it.each([
  ['stopped', 'Home Assistant is stopped. Start it on the device, then check Health again.'],
  ['error', 'Home Assistant reported an error. Check it on the device, then retry Health.'],
  ['installed', 'Home Assistant is installed but is not running. Start it on the device, then check Health again.'],
  [undefined, 'Home Assistant needs attention. Check it on the device, then retry Health.'],
])('keeps setup hidden for a successful health result with package status %s', async (status, message) => {
  fetch.mockImplementation(async (url) => {
    if (url.endsWith('/packages')) return json({ packages: catalog })
    if (url.endsWith('/commands')) return json({ command_id: 'health1' }, 202)
    if (url.endsWith('/commands/health1')) return json({ result: { status: 'ok', data: { package: 'homeassistant', version: 'pinned-v1', status, last_health_at: new Date().toISOString() } } })
    return json({ devices: [device({ packages: [{ package: 'homeassistant', version: 'pinned-v1', status: 'running', last_health_at: new Date().toISOString() }] })] })
  })
  render(<Harness />)
  expect(await screen.findByRole('region', { name: 'Home Assistant setup' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Health' }))
  expect(await screen.findByText(message)).toHaveAttribute('role', 'alert')
  expect(screen.queryByText('Home Assistant is healthy')).not.toBeInTheDocument()
  expect(screen.queryByRole('region', { name: 'Home Assistant setup' })).not.toBeInTheDocument()
})

it('exposes setup for a running package even when its health timestamp is null', () => {
  render(<LinkDeviceDrawer open devices={[device({ packages: [{ package: 'homeassistant', version: 'pinned-v1', status: 'running', last_health_at: null }] })]} />)
  expect(screen.getByRole('region', { name: 'Home Assistant setup' })).toBeInTheDocument()
})

it('rejects a late enrollment response after Escape closes the drawer', async () => {
  let settle
  fetch.mockImplementation(async (url) => url.endsWith('/enrollments')
    ? new Promise((resolve) => { settle = resolve }) : json({ devices: [] }))
  render(<Harness />)
  fireEvent.change(screen.getByLabelText('Device label'), { target: { value: 'Mac' } })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Link a device' })).not.toBeDisabled())
  fireEvent.click(screen.getByRole('button', { name: 'Link a device' }))
  await waitFor(() => expect(settle).toBeTypeOf('function'))
  fireEvent.keyDown(window, { key: 'Escape' })
  await act(async () => settle(json({ install_command: 'late-enrollment-secret', expires_at: new Date(Date.now() + 600000).toISOString() }, 201)))
  fireEvent.click(screen.getByRole('button', { name: /Devices/ }))
  expect(screen.queryByText('late-enrollment-secret')).not.toBeInTheDocument()
})

it('restores focus on close and immediately removes setup and enrollment credentials', async () => {
  fetch.mockResolvedValue(json({ devices: [device({ packages: [{ package: 'homeassistant', version: 'v1', status: 'running', last_health_at: '2026-11-02T10:00:00Z' }] })] }))
  render(<Harness />)
  expect(await screen.findByRole('region', { name: 'Home Assistant setup' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Close devices panel' }))
  expect(screen.queryByRole('region', { name: 'Home Assistant setup' })).not.toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: /Devices/ })).toHaveFocus())
})
