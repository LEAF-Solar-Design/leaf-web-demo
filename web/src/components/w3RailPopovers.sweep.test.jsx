// @vitest-environment jsdom
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import ClaudeAccountPanel from './ClaudeAccountPanel.jsx'
import JobRail from './JobRail.jsx'
import LinkServiceDrawer from './LinkServiceDrawer.jsx'
import SurfaceFrame from '../site/SurfaceFrame.jsx'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

const service = { id: 'billing', label: 'Billing', state: 'connecting' }
const drawerProps = {
  mock: false, loading: false, busy: false, servers: [], open: true,
  onToggle: () => {}, onRegister: async () => null,
  onConnect: async () => ({}), onHealth: async () => null, onUnlink: async () => null,
}

function Account() {
  const [open, setOpen] = useState(false)
  return <ClaudeAccountPanel mock={false} open={open} onToggle={setOpen}
    grant={{ linked: false }} loading={false} busy={false} />
}

function Drawer() {
  const [open, setOpen] = useState(false)
  return <LinkServiceDrawer {...drawerProps} open={open} onToggle={setOpen} />
}

function Monitor({ initialSpine }) {
  const [spine, setSpine] = useState(initialSpine)
  return <JobRail mock jobs={[]} spine={spine} onExpand={() => setSpine(false)} onCollapse={() => setSpine(true)} />
}

describe('w3 monitor and account popovers', () => {
  it('forwards the registry refresh through SurfaceFrame to the linked services drawer', async () => {
    const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const onRefresh = vi.fn().mockResolvedValue([{ ...service, state: 'connected' }])
    render(
      <SurfaceFrame scene="console" activeSurface="cad" integrations={{
        ...drawerProps,
        servers: [service],
        onConnect: async () => ({ authorize_url: 'https://signin.test' }),
        onRefresh,
      }}>
        <SurfaceFrame.Integrations />
      </SurfaceFrame>,
    )

    expect(onRefresh).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(popup.location.replace).toHaveBeenCalledWith('https://signin.test/'))
    fireEvent.focus(window)
    expect(await screen.findByText('Connected', { selector: '.state' })).toBeInTheDocument()
    expect(onRefresh).toHaveBeenCalledTimes(1)
  })

  it('refreshes once per authorization return and allows a new Connect to refresh again', async () => {
    const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const onRefresh = vi.fn().mockResolvedValue([service])
    render(<LinkServiceDrawer {...drawerProps} servers={[service]}
      onConnect={async () => ({ authorize_url: 'https://signin.test' })} onRefresh={onRefresh} />)

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(popup.location.replace).toHaveBeenCalledTimes(1))
    await act(async () => { fireEvent.focus(window) })
    expect(onRefresh).toHaveBeenCalledTimes(1)
    await act(async () => { fireEvent.focus(window) })
    expect(onRefresh).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(popup.location.replace).toHaveBeenCalledTimes(2))
    await act(async () => { fireEvent.focus(window) })
    expect(onRefresh).toHaveBeenCalledTimes(2)
  })

  it('keeps keyboard focus when expanding the job monitor', () => {
    render(<Monitor initialSpine />)
    const expand = screen.getByRole('button', { name: /Expand the job monitor/ })
    expand.focus()
    fireEvent.click(expand)
    expect(screen.getByRole('button', { name: /Collapse the job monitor/ })).toHaveFocus()
  })

  it('keeps keyboard focus when collapsing the job monitor', () => {
    render(<Monitor initialSpine={false} />)
    const collapse = screen.getByRole('button', { name: /Collapse the job monitor/ })
    collapse.focus()
    fireEvent.click(collapse)
    expect(screen.getByRole('button', { name: /Expand the job monitor/ })).toHaveFocus()
  })

  it('explains the demo job list without transport or development copy', () => {
    const { container } = render(<JobRail mock jobs={[]} />)
    expect(screen.getByText('This demo shows jobs from your current session. Sign in to see your recent cloud jobs.')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/GET|\/api\/|mock mode/)
  })

  it('moves focus into Claude accounts and restores it on Escape', () => {
    render(<Account />)
    const trigger = screen.getByRole('button', { name: /Claude accounts/ })
    trigger.focus()
    fireEvent.click(trigger)
    expect(screen.getByRole('button', { name: 'Close Claude account panel' })).toHaveFocus()
    fireEvent.keyDown(document.activeElement, { key: 'Escape' })
    expect(trigger).toHaveFocus()
  })

  it('moves focus into linked services and restores it on close', () => {
    render(<Drawer />)
    const trigger = screen.getByRole('button', { name: /Linked services/ })
    trigger.focus()
    fireEvent.click(trigger)
    const close = screen.getByRole('button', { name: 'Close linked services panel' })
    expect(close).toHaveFocus()
    fireEvent.click(close)
    expect(trigger).toHaveFocus()
  })

  it('uses Link and account wording throughout Claude setup', () => {
    render(<Account />)
    fireEvent.click(screen.getByRole('button', { name: /Claude accounts/ }))
    const dialog = screen.getByRole('dialog', { name: 'Claude accounts' })
    expect(within(dialog).getByLabelText('Account label')).toBeInTheDocument()
    expect(dialog.textContent).not.toMatch(/mount|tenant/i)
    expect(within(dialog).getByRole('button', { name: 'Link Claude account' })).toHaveAttribute('title', 'Enter your credential to link an account.')
  })

  it('retains inputs and offers sign-in guidance when registration is inert', async () => {
    render(<LinkServiceDrawer {...drawerProps} />)
    fireEvent.change(screen.getByLabelText('Service label'), { target: { value: 'Billing' } })
    fireEvent.change(screen.getByLabelText('Service URL'), { target: { value: 'https://billing.test' } })
    fireEvent.click(screen.getByRole('button', { name: 'Link service' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The service was not linked. Sign in to your workspace and try again.')
    expect(screen.getByLabelText('Service label')).toHaveValue('Billing')
    expect(screen.getByLabelText('Service URL')).toHaveValue('https://billing.test')
    expect(screen.getByRole('button', { name: 'Link service' })).toBeEnabled()
  })

  it('reports a blocked sign-in tab and keeps Connect available for retry', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    render(<LinkServiceDrawer {...drawerProps} servers={[service]} onConnect={async () => ({ authorize_url: 'https://signin.test' })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Allow pop-ups and try Connect again.')
    expect(screen.getByRole('button', { name: 'Connect' })).toBeEnabled()
  })

  it('reports a missing authorization link', async () => {
    const popup = { close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(popup)
    render(<LinkServiceDrawer {...drawerProps} servers={[service]} />)
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The service did not provide a sign-in link.')
    expect(popup.close).toHaveBeenCalled()
  })

  it('rechecks reachability after returning without overriding authorization', async () => {
    const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn() }
    vi.spyOn(window, 'open').mockReturnValue(popup)
    const onHealth = vi.fn().mockResolvedValue({ state: 'connecting' })
    const onConnect = vi.fn().mockResolvedValue({ authorize_url: 'https://signin.test' })
    render(<LinkServiceDrawer {...drawerProps} servers={[service]} onConnect={onConnect} onHealth={onHealth} />)
    await waitFor(() => expect(screen.getByText('Connection checked: Reachable')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    await waitFor(() => expect(popup.location.replace).toHaveBeenCalledWith('https://signin.test/'))
    onHealth.mockResolvedValue({ state: 'connected' })
    fireEvent.focus(window)
    await waitFor(() => expect(onHealth).toHaveBeenCalledTimes(2))
    expect(screen.getByText('Sign in needed', { selector: '.state' })).toBeInTheDocument()
    expect(screen.queryByText('Connected', { selector: '.state' })).not.toBeInTheDocument()
  })

  it('rechecks reachability when reopening the drawer', async () => {
    const onHealth = vi.fn().mockResolvedValue({ state: 'connecting' })
    const props = { ...drawerProps, servers: [service], onHealth }
    const view = render(<LinkServiceDrawer {...props} />)
    await waitFor(() => expect(screen.getByText('Connection checked: Reachable')).toBeInTheDocument())
    view.rerender(<LinkServiceDrawer {...props} open={false} />)
    onHealth.mockResolvedValue({ state: 'connected' })
    view.rerender(<LinkServiceDrawer {...props} />)
    await waitFor(() => expect(onHealth).toHaveBeenCalledTimes(2))
    expect(screen.getByText('Sign in needed', { selector: '.state' })).toBeInTheDocument()
  })
})
