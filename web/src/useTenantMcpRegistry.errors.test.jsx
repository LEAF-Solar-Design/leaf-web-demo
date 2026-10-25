// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { connectTenantMcpServer, listTenantMcpServers, registerTenantMcpServer, unlinkTenantMcpServer } from './api.js'
import { MSG_NETWORK, MSG_SERVICE } from './errorHumanize.js'
import useTenantMcpRegistry from './useTenantMcpRegistry.js'
import LinkServiceDrawer from './components/LinkServiceDrawer.jsx'

vi.mock('./api.js', () => ({
  listTenantMcpServers: vi.fn(),
  connectTenantMcpServer: vi.fn(),
  healthTenantMcpServer: vi.fn(),
  registerTenantMcpServer: vi.fn(),
  unlinkTenantMcpServer: vi.fn(),
}))

const servers = [{ id: 's1', label: 'Roof scanner', state: 'connected' }]
beforeEach(() => {
  vi.resetAllMocks()
  listTenantMcpServers.mockResolvedValue(servers)
})
afterEach(cleanup)

function Drawer() {
  const registry = useTenantMcpRegistry()
  return <LinkServiceDrawer {...registry} open onToggle={() => {}}
    onRegister={registry.register} onConnect={registry.connect}
    onHealth={registry.health} onUnlink={registry.unlink} />
}

it('retains the previous list and exposes a readable error after a failed refresh', async () => {
  const { result } = renderHook(() => useTenantMcpRegistry())
  await waitFor(() => expect(result.current.servers).toEqual(servers))
  listTenantMcpServers.mockRejectedValue(new TypeError('Failed to fetch'))
  await act(async () => { await result.current.refresh() })
  expect(result.current.servers).toEqual(servers)
  expect(result.current.error.message).toBe(MSG_NETWORK)
  expect(result.current.loading).toBe(false)
})

it('retains the previous list after a malformed response and recovers to a valid empty list', async () => {
  const api = await vi.importActual('./api.js')
  listTenantMcpServers.mockImplementation(api.listTenantMcpServers)
  vi.stubGlobal('fetch', vi.fn()
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ servers }) })
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({}) })
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ servers: [] }) }))
  try {
    const { result } = renderHook(() => useTenantMcpRegistry())
    await waitFor(() => expect(result.current.servers).toEqual(servers))
    expect(result.current.error).toBeNull()

    await act(async () => { await result.current.refresh() })
    expect(result.current.servers).toEqual(servers)
    expect(result.current.error).toMatchObject({
      kind: 'list', message: 'Linked services could not be loaded. Try again.',
    })
    expect(result.current.loading).toBe(false)

    await act(async () => { expect(await result.current.refresh()).toEqual([]) })
    expect(result.current.servers).toEqual([])
    expect(result.current.error).toBeNull()
    expect(result.current.loading).toBe(false)
    expect(fetch).toHaveBeenCalledTimes(3)
  } finally {
    vi.unstubAllGlobals()
  }
})

it('shows a failed initial read and retries to a successful empty list', async () => {
  listTenantMcpServers.mockRejectedValueOnce(new Error('GET /api/tenant/mcp-servers -> 502'))
  render(<Drawer />)
  expect(await screen.findByText('Linked services could not be loaded.')).toBeInTheDocument()
  expect(screen.getByText(MSG_SERVICE)).toBeInTheDocument()
  expect(screen.queryByText('No services linked yet.')).not.toBeInTheDocument()
  expect(screen.queryByText('0 linked')).not.toBeInTheDocument()
  listTenantMcpServers.mockResolvedValue([])
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(await screen.findByText('No services linked yet.')).toBeInTheDocument()
  expect(screen.getByText('0 linked')).toBeInTheDocument()
})

it('shows registration validation after an initial list failure and keeps the fields editable', async () => {
  listTenantMcpServers.mockRejectedValueOnce(new TypeError('Failed to fetch'))
  registerTenantMcpServer.mockRejectedValue({ body: { error: { message: 'Choose a different service label.' } } })
  render(<Drawer />)
  await screen.findByText('Linked services could not be loaded.')
  const label = screen.getByLabelText('Service label')
  const url = screen.getByLabelText('Service URL')
  fireEvent.change(label, { target: { value: 'Roof scanner' } })
  fireEvent.change(url, { target: { value: 'https://example.test' } })
  fireEvent.click(screen.getByRole('button', { name: 'Link service' }))
  expect(await screen.findByText('Choose a different service label.')).toBeInTheDocument()
  expect(screen.getByText('Choose a different service label.').closest('[role="alert"]')).toHaveTextContent('Choose a different service label.')
  expect(screen.getByText('Linked services could not be loaded.')).toBeInTheDocument()
  expect(screen.getByText(MSG_NETWORK)).toBeInTheDocument()
  expect(screen.getByText('Unavailable')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled()
  expect(screen.queryByText('0 linked')).not.toBeInTheDocument()
  expect(screen.queryByText('No services linked yet.')).not.toBeInTheDocument()
  expect(registerTenantMcpServer).toHaveBeenCalledWith('https://example.test', 'Roof scanner')
  expect(label).toHaveValue('Roof scanner')
  expect(url).toHaveValue('https://example.test')
  expect(label).toBeEnabled()
  expect(url).toBeEnabled()
  fireEvent.change(label, { target: { value: 'Roof scanner 2' } })
  expect(label).toHaveValue('Roof scanner 2')
  expect(screen.getByRole('button', { name: 'Link service' })).toBeEnabled()
})

it('records a failed unlink and catches the rejection in the click handler', async () => {
  unlinkTenantMcpServer.mockRejectedValue(new TypeError('Failed to fetch'))
  render(<Drawer />)
  await screen.findByText('Roof scanner')
  fireEvent.click(screen.getByRole('button', { name: 'Unlink' }))
  fireEvent.click(screen.getByRole('button', { name: 'Unlink' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Keep' })).not.toBeInTheDocument())
  expect(screen.getByText('Roof scanner')).toBeInTheDocument()
  // Both the hook's shared error and the row's caught failure are readable.
  expect(screen.getAllByText(MSG_NETWORK)).toHaveLength(2)
  expect(listTenantMcpServers).toHaveBeenCalledTimes(1)
})

it('shows a connect 502 without its transport path', async () => {
  connectTenantMcpServer.mockRejectedValue(new Error('POST /api/tenant/mcp-servers/s1/connect -> 502'))
  const { container } = render(<Drawer />)
  await screen.findByText('Roof scanner')
  fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
  expect(await screen.findByText(MSG_SERVICE)).toBeInTheDocument()
  expect(container.textContent).not.toMatch(/\/api\/|POST|502/)
})

it('keeps the last list when the refresh after each successful write fails', async () => {
  const { result } = renderHook(() => useTenantMcpRegistry())
  await waitFor(() => expect(result.current.servers).toEqual(servers))
  listTenantMcpServers.mockRejectedValue(new TypeError('Failed to fetch'))
  registerTenantMcpServer.mockResolvedValue({ id: 's2' })
  connectTenantMcpServer.mockResolvedValue({ authorize_url: 'https://example.test/connect' })
  unlinkTenantMcpServer.mockResolvedValue({ deleted: true })
  for (const mutate of [
    () => result.current.register('https://example.test', 'Roof scanner'),
    () => result.current.connect('s1'),
    () => result.current.unlink('s1'),
  ]) {
    await act(async () => { await mutate() })
    expect(result.current.servers).toEqual(servers)
    expect(result.current.error.message).toBe(MSG_NETWORK)
    expect(result.current.busy).toBe(false)
  }
})

it('humanizes registration transport failures and preserves readable server reasons', async () => {
  const { result } = renderHook(() => useTenantMcpRegistry())
  await waitFor(() => expect(result.current.servers).toEqual(servers))
  registerTenantMcpServer.mockRejectedValue(new TypeError('Failed to fetch'))
  await act(async () => {
    await expect(result.current.register('https://example.test', 'Roof scanner')).rejects.toThrow()
  })
  expect(result.current.error).toBe(MSG_NETWORK)
  registerTenantMcpServer.mockRejectedValue({ body: { error: { message: 'Choose a different service label.' } } })
  await act(async () => {
    await result.current.register('https://example.test', 'Roof scanner').catch(() => {})
  })
  expect(result.current.error).toBe('Choose a different service label.')
})
