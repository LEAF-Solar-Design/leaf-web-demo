// @vitest-environment jsdom
//
// Standardization slice 8c. Renders the REAL component (no mock backend of
// its own) against a caller-shaped `servers` array, the same wiring shape
// useTenantMcpRegistry hands it, so a drift in either shape fails a test
// here first.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react'

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
  it('closes an unlink confirmation after a readable failure without removing the service', async () => {
    render(<LinkServiceDrawer {...baseProps({
      servers: [server()],
      onUnlink: vi.fn().mockRejectedValue(new TypeError('Failed to fetch')),
    })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Unlink' }))
    fireEvent.click(screen.getByRole('button', { name: 'Unlink' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't reach the service.")
    expect(screen.queryByRole('button', { name: 'Keep' })).not.toBeInTheDocument()
    expect(screen.getByText('Billing tool')).toBeInTheDocument()
  })

  it('preserves a readable server reason for a failed connect', async () => {
    render(<LinkServiceDrawer {...baseProps({
      servers: [server()],
      onConnect: vi.fn().mockRejectedValue({ body: { error: { message: 'Sign in to this service first.' } } }),
    })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Sign in to this service first.')
  })

  it('renders nothing at all in mock mode, same posture as ClaudeAccountPanel', () => {
    const { container } = render(<LinkServiceDrawer {...baseProps({ mock: true, open: true })} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('lists servers with authorization state chips per row', () => {
    render(<LinkServiceDrawer {...baseProps({
      servers: [
        server({ id: 'a', label: 'Billing tool', state: 'registered' }),
        server({ id: 'b', label: 'Roof scanner', state: 'connected' }),
        server({ id: 'c', label: 'Flaky server', state: 'error' }),
        server({ id: 'd', label: 'Mid-flight', state: 'connecting' }),
      ],
    })} />)

    expect(screen.getByText('Billing tool')).toBeInTheDocument()
    expect(screen.getAllByText('Sign in needed')).toHaveLength(3)
    expect(screen.getByText('Roof scanner')).toBeInTheDocument()
    expect(screen.getByText('Connected')).toBeInTheDocument()
    expect(screen.getByText('Flaky server')).toBeInTheDocument()
    expect(screen.getByText('Mid-flight')).toBeInTheDocument()
  })

  it('Connect reserves a tab before awaiting the sign-in URL and then navigates it', async () => {
    const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn() }
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(popup)
    const onConnect = vi.fn().mockResolvedValue({ authorize_url: 'https://as.example.test/authorize?x=1' })
    render(<LinkServiceDrawer {...baseProps({
      servers: [server({ id: 'a', label: 'Billing tool' })],
      onConnect,
    })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(popup.location.replace).toHaveBeenCalledWith('https://as.example.test/authorize?x=1'))
    expect(onConnect).toHaveBeenCalledWith('a')
    expect(openSpy).toHaveBeenCalledWith('about:blank', '_blank')
    expect(popup.opener).toBeNull()
    openSpy.mockRestore()
  })

  it('resolves a relative sign-in URL against the app and keeps the reserved tab open', async () => {
    const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn() }
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(popup)
    const onConnect = vi.fn().mockResolvedValue({ authorize_url: '/api/registry/start?x=1' })
    try {
      render(<LinkServiceDrawer {...baseProps({ servers: [server()], onConnect })} />)

      fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
      await waitFor(() => expect(popup.location.replace).toHaveBeenCalledWith(`${window.location.origin}/api/registry/start?x=1`))
      expect(openSpy).toHaveBeenCalledWith('about:blank', '_blank')
      expect(popup.close).not.toHaveBeenCalled()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    } finally {
      openSpy.mockRestore()
    }
  })

  it('closes the reserved tab and explains when no sign-in URL is returned', async () => {
    const popup = { close: vi.fn() }
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(popup)
    const onConnect = vi.fn().mockResolvedValue({})
    render(<LinkServiceDrawer {...baseProps({
      servers: [server({ id: 'a' })],
      onConnect,
    })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The service did not provide a sign-in link. Try Connect again.')
    expect(popup.close).toHaveBeenCalled()
    openSpy.mockRestore()
  })

  it('Health shows reachability without upgrading the registry authorization state', async () => {
    const onHealth = vi.fn().mockResolvedValue({ id: 'a', state: 'connected' })
    render(<LinkServiceDrawer {...baseProps({
      servers: [server({ id: 'a', label: 'Billing tool' })],
      onHealth,
    })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Health' }))
    expect(await screen.findByText('Connection checked: Reachable')).toBeInTheDocument()
    expect(screen.getByText('Sign in needed', { selector: '.state' })).toBeInTheDocument()
    expect(onHealth).toHaveBeenCalledWith('a')
  })

  it('keeps sign-in required when a 401 health response reports the service as reachable', async () => {
    const onHealth = vi.fn().mockResolvedValue({ id: 'a', state: 'connected', status: 401 })
    render(<LinkServiceDrawer {...baseProps({
      servers: [server({ id: 'a', state: 'registered' })],
      onHealth,
    })} />)

    expect(await screen.findByText('Connection checked: Reachable')).toBeInTheDocument()
    expect(screen.getByText('Sign in needed', { selector: '.state' })).toBeInTheDocument()
    expect(screen.queryByText('Connected', { selector: '.state' })).not.toBeInTheDocument()
  })

  it('checks rows that arrive after opening exactly once', async () => {
    const onHealth = vi.fn().mockResolvedValue({ id: 'late', state: 'connected' })
    const props = baseProps({ onHealth })
    const view = render(<LinkServiceDrawer {...props} />)
    expect(onHealth).not.toHaveBeenCalled()

    view.rerender(<LinkServiceDrawer {...props} servers={[server({ id: 'late' })]} />)
    await waitFor(() => expect(onHealth).toHaveBeenCalledTimes(1))
    expect(onHealth).toHaveBeenCalledWith('late')
    expect(await screen.findByText('Connection checked: Reachable')).toBeInTheDocument()

    view.rerender(<LinkServiceDrawer {...props} servers={[server({ id: 'late' })]} />)
    await waitFor(() => expect(onHealth).toHaveBeenCalledTimes(1))
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

  it('keeps focus in an outside editor when the drawer closes', () => {
    const props = baseProps()
    const view = render(<><input aria-label="Drawing name" /><LinkServiceDrawer {...props} /></>)
    const editor = screen.getByLabelText('Drawing name')
    editor.focus()
    view.rerender(<><input aria-label="Drawing name" /><LinkServiceDrawer {...props} open={false} /></>)
    expect(editor).toHaveFocus()
  })
})


function retainedKey(target, key, marks, modifiers = {}) {
  const event = createEvent.keyDown(target, {
    key, bubbles: true, cancelable: true, ...modifiers, ...marks,
  })
  expect(event.isComposing).toBe(marks.isComposing)
  expect(event.keyCode).toBe(marks.keyCode)
  const stop = event.stopPropagation.bind(event)
  event.stopPropagation = vi.fn(() => stop())
  return event
}

function expectYielded(event) {
  expect(event.defaultPrevented).toBe(false)
  expect(event.stopPropagation).not.toHaveBeenCalled()
}

async function serviceComposition(marks) {
  const onRegister = vi.fn().mockResolvedValue({ id: 'new-service' })
  render(<LinkServiceDrawer {...baseProps({ onRegister })} />)
  const label = screen.getByLabelText('Service label')
  const input = screen.getByLabelText('Service URL')
  fireEvent.change(label, { target: { value: 'Draft service' } })
  fireEvent.change(input, { target: { value: 'service-value' } })
  input.focus()
  expect(input).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Link service' })).toBeEnabled()
  const event = retainedKey(input, 'Enter', marks)
  fireEvent(input, event)
  expectYielded(event)
  expect(onRegister).not.toHaveBeenCalled()
  expect(label.value).toBe('Draft service')
  expect(input.value).toBe('service-value')
  expect(input).toHaveFocus()
  const ordinary = retainedKey(input, 'Enter', { isComposing: false, keyCode: 0 })
  fireEvent(input, ordinary)
  expect(ordinary.defaultPrevented).toBe(false)
  expect(onRegister).toHaveBeenCalledTimes(1)
  expect(onRegister).toHaveBeenCalledWith('service-value', 'Draft service')
  await waitFor(() => {
    expect(label.value).toBe('')
    expect(input.value).toBe('')
  })
}

it('KEYS-D21 service URL Enter yields native composition', async () => {
  await serviceComposition({ isComposing: true, keyCode: 0 })
})

it('KEYS-D22 service URL Enter yields key code 229', async () => {
  await serviceComposition({ isComposing: false, keyCode: 229 })
})
