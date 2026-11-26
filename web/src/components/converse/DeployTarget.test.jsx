// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { getTenantToolStatus } from '../../api.js'
import { useAccountIdentity } from '../../auth/accountIdentity.js'
import DeployTarget from './DeployTarget.jsx'

vi.mock('../../api.js', () => ({ getTenantToolStatus: vi.fn() }))
vi.mock('../../auth/accountIdentity.js', () => ({ useAccountIdentity: vi.fn() }))

const currentTime = Date.parse('2026-11-26T12:00:00Z')
const now = () => currentTime
const workspaceId = '12345678-abcd-4321-9876-123456789abc'
const email = 'designer@example.com'
const stamp = minutes => new Date(currentTime - minutes * 60000).toISOString()
const mount = async () => {
  let view
  await act(async () => { view = render(<dl><DeployTarget now={now} /></dl>) })
  return view
}

beforeEach(() => {
  useAccountIdentity.mockReturnValue({ email, workspace: null })
  getTenantToolStatus.mockResolvedValue(null)
})
afterEach(() => { cleanup(); vi.resetAllMocks() })

it.each([
  [{ id: workspaceId, name: 'Roof team' }, null, 'Deployed to workspace Roof team (12345678).'],
  [{ id: workspaceId, name: null }, null, 'Deployed to workspace 12345678.'],
  [null, { workspaceId, workspaceName: 'Roof team' }, 'Deployed to workspace Roof team (12345678).'],
  [null, { workspaceId, workspaceName: null }, 'Deployed to workspace 12345678.'],
  [null, null, 'Deployed to your workspace.'],
  [null, { workspaceId: null, workspaceName: 'Roof team' }, 'Deployed to your workspace.'],
])('names the deployment workspace for identity %j and status %j', async (workspace, status, text) => {
  useAccountIdentity.mockReturnValue({ email, workspace })
  getTenantToolStatus.mockResolvedValue(status)
  await mount()
  expect(screen.getByText(text)).toBeVisible()
  const label = screen.getByText('AutoCAD')
  expect(label.tagName).toBe('DT')
  expect(label.nextElementSibling.tagName).toBe('DD')
  expect(label.nextElementSibling).toContainElement(screen.getByText(text))
})

it.each([
  [email, 'It arrives in AutoCAD signed in as designer@example.com.'],
  [null, 'It arrives in AutoCAD signed in with the same account as Studio.'],
])('describes the destination account when email is %s', async (knownEmail, text) => {
  useAccountIdentity.mockReturnValue({ email: knownEmail, workspace: null })
  await mount()
  expect(screen.getByText(text)).toBeVisible()
})

it.each([null, stamp(25 * 60), 'not a timestamp'])(
  'warns when the plugin check timestamp is %s', async lastPluginCheckAt => {
    getTenantToolStatus.mockResolvedValue({ workspaceId, lastPluginCheckAt })
    await mount()
    const warning = screen.getByRole('status')
    expect(warning.tagName).toBe('P')
    expect(warning).toHaveClass('deploy-target-warning')
    expect(warning.textContent.trim()).toBe('No AutoCAD has checked this workspace in the last day. In AutoCAD, run LEAFSTATUS and check that it shows designer@example.com.')
  })

it('uses the account fallback in the warning when the email is unknown', async () => {
  useAccountIdentity.mockReturnValue({ email: null, workspace: null })
  getTenantToolStatus.mockResolvedValue({ lastPluginCheckAt: null })
  await mount()
  expect(screen.getByRole('status').textContent.trim()).toBe('No AutoCAD has checked this workspace in the last day. In AutoCAD, run LEAFSTATUS and check that it shows the account you use here.')
})

it.each([
  [5, 'AutoCAD last checked this workspace 5 minutes ago.'],
  [1, 'AutoCAD last checked this workspace 1 minute ago.'],
  [90, 'AutoCAD last checked this workspace 1 hour ago.'],
  [60, 'AutoCAD last checked this workspace 1 hour ago.'],
  [120, 'AutoCAD last checked this workspace 2 hours ago.'],
  [5.9, 'AutoCAD last checked this workspace 5 minutes ago.'],
  [0, 'AutoCAD last checked this workspace 1 minute ago.'],
  [24 * 60, 'AutoCAD last checked this workspace 24 hours ago.'],
])('shows the last check at %s minutes ago', async (minutes, text) => {
  getTenantToolStatus.mockResolvedValue({ lastPluginCheckAt: stamp(minutes) })
  await mount()
  expect(screen.getByText(text)).toBeVisible()
  expect(screen.queryByRole('status')).toBeNull()
})

it('shows no activity or warning while the status is loading', async () => {
  let finish
  getTenantToolStatus.mockReturnValue(new Promise(resolve => { finish = resolve }))
  await mount()
  expect(screen.getByText('Deployed to your workspace.')).toBeVisible()
  expect(screen.getByText('It arrives in AutoCAD signed in as designer@example.com.')).toBeVisible()
  expect(screen.queryByRole('status')).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByText(/AutoCAD last checked/)).toBeNull()
  await act(async () => finish(null))
})

it('shows no activity or warning when status is unavailable on the server', async () => {
  await mount()
  expect(screen.queryByRole('status')).toBeNull()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByText(/AutoCAD last checked/)).toBeNull()
})

it('keeps the identity workspace and replaces the warning with an alert on mismatch', async () => {
  useAccountIdentity.mockReturnValue({ email, workspace: { id: workspaceId, name: 'Studio team' } })
  getTenantToolStatus.mockResolvedValue({ workspaceId: 'different-workspace', workspaceName: 'Other team', lastPluginCheckAt: null })
  await mount()
  expect(screen.getByText('Deployed to workspace Studio team (12345678).')).toBeVisible()
  const alert = screen.getByRole('alert')
  expect(alert.tagName).toBe('P')
  expect(alert.textContent).toBe("This card's workspace and the signed-in Studio workspace differ. Reload Studio before deploying again.")
  expect(screen.queryByRole('status')).toBeNull()
})

it('compares workspace ids without case sensitivity', async () => {
  useAccountIdentity.mockReturnValue({ email, workspace: { id: workspaceId, name: 'Studio team' } })
  getTenantToolStatus.mockResolvedValue({ workspaceId: workspaceId.toUpperCase(), lastPluginCheckAt: stamp(5) })
  await mount()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.getByText('AutoCAD last checked this workspace 5 minutes ago.')).toBeVisible()
})

it('reads status once per mount even when identity and now change', async () => {
  const view = await mount()
  useAccountIdentity.mockReturnValue({ email: null, workspace: { id: workspaceId, name: null } })
  view.rerender(<dl><DeployTarget now={() => currentTime + 60000} /></dl>)
  expect(screen.getByText('Deployed to workspace 12345678.')).toBeVisible()
  expect(getTenantToolStatus).toHaveBeenCalledTimes(1)
  view.unmount()
  await mount()
  expect(getTenantToolStatus).toHaveBeenCalledTimes(2)
})

it('ignores status that resolves after unmount', async () => {
  let finish
  const clock = vi.fn(now)
  getTenantToolStatus.mockReturnValue(new Promise(resolve => { finish = resolve }))
  const view = render(<dl><DeployTarget now={clock} /></dl>)
  view.unmount()
  await act(async () => finish({ workspaceId, lastPluginCheckAt: null }))
  expect(clock).not.toHaveBeenCalled()
  expect(screen.queryByText('AutoCAD')).toBeNull()
})
