// @vitest-environment jsdom
//
// Standardization slice 8c. Renders the REAL component (no mock backend of
// its own) against a caller-shaped `servers` array, the same wiring shape
// useTenantMcpRegistry hands it, so a drift in either shape fails a test
// here first.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import LinkServiceDrawer from './LinkServiceDrawer.jsx'

afterEach(cleanup)

const noop = () => {}

const baseProps = (over = {}) => ({
  mock: false,
  servers: [],
  loading: false,
  busy: false,
  error: null,
  open: true,
  onToggle: noop,
  onRegister: vi.fn(),
  onConnect: vi.fn(),
  onHealth: vi.fn(),
  onUnlink: vi.fn(),
  ...over,
})

const server = (over = {}) => ({
  id: 'srv-1',
  label: 'Billing tool',
  host: 'mcp.example.test',
  state: 'registered',
  linked_at: null,
  ...over,
})

describe('LinkServiceDrawer', () => {
  it('renders nothing at all in mock mode, same posture as ClaudeAccountPanel', () => {
    const { container } = render(<LinkServiceDrawer {...baseProps({ mock: true, open: true })} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('lists registered servers with a state chip per row', () => {
    render(<LinkServiceDrawer {...baseProps({
      servers: [
        server({ id: 'a', label: 'Billing tool', state: 'registered' }),
        server({ id: 'b', label: 'Roof scanner', state: 'connected' }),
        server({ id: 'c', label: 'Flaky server', state: 'error' }),
        server({ id: 'd', label: 'Mid-flight', state: 'connecting' }),
      ],
    })} />)

    expect(screen.getByText('Billing tool')).toBeInTheDocument()
    expect(screen.getByText('Registered')).toBeInTheDocument()
    expect(screen.getByText('Roof scanner')).toBeInTheDocument()
    expect(screen.getByText('Connected')).toBeInTheDocument()
    expect(screen.getByText('Flaky server')).toBeInTheDocument()
    expect(screen.getByText('Error')).toBeInTheDocument()
    expect(screen.getByText('Mid-flight')).toBeInTheDocument()
    expect(screen.getByText('Connecting…')).toBeInTheDocument()
  })

  it('Connect opens the server-side OAuth start URL returned by onConnect, in a new tab', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => {})
    const onConnect = vi.fn().mockResolvedValue({ authorize_url: 'https://as.example.test/authorize?x=1' })
    render(<LinkServiceDrawer {...baseProps({
      servers: [server({ id: 'a', label: 'Billing tool' })],
      onConnect,
    })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await screen.findByRole('button', { name: 'Connect' }) // let the async handler settle
    expect(onConnect).toHaveBeenCalledWith('a')
    expect(openSpy).toHaveBeenCalledWith('https://as.example.test/authorize?x=1', '_blank', 'noopener,noreferrer')
    openSpy.mockRestore()
  })

  it('never opens a tab when onConnect resolves with no authorize_url', async () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => {})
    const onConnect = vi.fn().mockResolvedValue({})
    render(<LinkServiceDrawer {...baseProps({
      servers: [server({ id: 'a' })],
      onConnect,
    })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await Promise.resolve()
    expect(openSpy).not.toHaveBeenCalled()
    openSpy.mockRestore()
  })

  it('Health shows the returned state word inline, keyed to its own row', async () => {
    const onHealth = vi.fn().mockResolvedValue({ id: 'a', state: 'connected' })
    render(<LinkServiceDrawer {...baseProps({
      servers: [server({ id: 'a', label: 'Billing tool' })],
      onHealth,
    })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Health' }))
    expect(await screen.findByText('health: connected')).toBeInTheDocument()
    expect(onHealth).toHaveBeenCalledWith('a')
  })

  it('Unlink requires a second confirming click, and Keep cancels it', async () => {
    const onUnlink = vi.fn().mockResolvedValue({ deleted: true })
    render(<LinkServiceDrawer {...baseProps({
      servers: [server({ id: 'a', label: 'Billing tool' })],
      onUnlink,
    })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Unlink' }))
    expect(onUnlink).not.toHaveBeenCalled()
    const keep = await screen.findByRole('button', { name: 'Keep' })
    fireEvent.click(keep)
    expect(onUnlink).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Keep' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Unlink' }))
    const confirm = await screen.findByRole('button', { name: 'Unlink' })
    fireEvent.click(confirm)
    expect(onUnlink).toHaveBeenCalledWith('a')
  })

  // Leak-invariant: the drawer reads only {id, label, host, state, linked_at}
  // off a server record (LinkServiceDrawer.jsx's header comment). A hostile
  // record carrying a token, an upstream tool name, or a credentialed URL in
  // an unexpected field must never reach the DOM even though the component
  // never spreads the record.
  it('never renders a token, an upstream tool name, or a credentialed URL from a hostile record', () => {
    const hostile = server({
      id: 'hostile',
      label: 'Billing tool',
      host: 'mcp.example.test',
      state: 'connected',
      token: 'sk-ant-hostile00000000000000000000000000000000000000',
      access_token: 'sk-ant-hostile00000000000000000000000000000000000000',
      upstream_tool: 'internal-billing-export',
      url: 'https://user:hunter2@mcp.example.test/sse',
    })
    const { container } = render(<LinkServiceDrawer {...baseProps({ servers: [hostile] })} />)

    const text = container.textContent
    expect(text).not.toContain('sk-ant-hostile')
    expect(text).not.toContain('internal-billing-export')
    expect(text).not.toContain('hunter2')
    expect(text).not.toContain('user:hunter2')
    expect(container.innerHTML).not.toContain('sk-ant-hostile')
    expect(container.innerHTML).not.toContain('internal-billing-export')
    expect(container.innerHTML).not.toContain('hunter2')
  })

  it('wears its own trigger class, never the account panel\'s .claude-trigger (trust-state.spec.mjs:98 locates that one alone)', () => {
    const { container } = render(<LinkServiceDrawer {...baseProps()} />)
    expect(container.querySelector('.claude-trigger')).toBeNull()
    expect(container.querySelector('.link-svc-trigger')).not.toBeNull()
  })
})
