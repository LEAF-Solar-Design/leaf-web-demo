import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'

vi.mock('../api.js', () => ({
  getOpsUsage: vi.fn(),
  getAccountControls: vi.fn(),
  setTenantDisabled: vi.fn(),
  updateAccountControls: vi.fn(),
}))

import { getAccountControls, getOpsUsage, setTenantDisabled, updateAccountControls } from '../api.js'
import OpsDrawer from './OpsDrawer.jsx'

const transportError = (path) => Object.assign(new Error(`${path} -> 502`), { status: 502 })
const serviceMessage = 'The service had a temporary problem. Try again.'

beforeEach(() => {
  vi.resetAllMocks()
  getOpsUsage.mockResolvedValue({ tenants: [{ tenant_id: 'tenant-alpha', disabled: false }] })
  getAccountControls.mockRejectedValue(Object.assign(new Error('Unauthorized'), { status: 401 }))
})

afterEach(cleanup)

describe('Ops drawer errors', () => {
  it('keeps the tenant table and state after a failed disable, then allows a retry', async () => {
    setTenantDisabled.mockRejectedValueOnce(transportError('POST /api/operator/tenants/tenant-alpha/disable'))
      .mockResolvedValueOnce({})
    render(<OpsDrawer />)
    const table = await screen.findByRole('table')
    fireEvent.click(within(table).getByRole('button', { name: 'Disable' }))
    fireEvent.click(within(table).getByRole('button', { name: 'Disable' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(serviceMessage)
    expect(screen.getByRole('table')).toBe(table)
    expect(within(table).getByText('Active')).toBeInTheDocument()
    expect(screen.queryByText(/load tenants/i)).toBeNull()
    expect(screen.getByRole('dialog')).not.toHaveTextContent(/\/api\/|POST|502/)
    expect(setTenantDisabled).toHaveBeenCalledWith('tenant-alpha', true)

    fireEvent.click(within(table).getByRole('button', { name: 'Disable' }))
    fireEvent.click(within(table).getByRole('button', { name: 'Disable' }))
    expect(await within(table).findByText('Disabled')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('keeps a disabled tenant visible when enabling fails', async () => {
    getOpsUsage.mockResolvedValue({ tenants: [{ tenant_id: 'tenant-alpha', disabled: true }] })
    setTenantDisabled.mockRejectedValue(transportError('POST /api/operator/tenants/tenant-alpha/enable'))
    render(<OpsDrawer />)
    const table = await screen.findByRole('table')
    fireEvent.click(within(table).getByRole('button', { name: 'Enable' }))
    fireEvent.click(within(table).getByRole('button', { name: 'Enable' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(serviceMessage)
    expect(screen.getByRole('table')).toBe(table)
    expect(within(table).getByText('Disabled')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).not.toHaveTextContent(/\/api\/|POST|502/)
    expect(setTenantDisabled).toHaveBeenCalledWith('tenant-alpha', false)
  })

  it('shows a readable list-load error with a retry', async () => {
    getOpsUsage.mockRejectedValue(transportError('GET /api/operator/tenants'))
    render(<OpsDrawer />)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/load tenants/i)
    expect(alert).toHaveTextContent(serviceMessage)
    expect(alert).not.toHaveTextContent(/\/api\/|GET|502/)
    expect(within(alert).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('humanizes account-control load failures', async () => {
    getAccountControls.mockRejectedValue(transportError('GET /api/admin/account-controls'))
    render(<OpsDrawer />)

    expect(await screen.findByRole('alert')).toHaveTextContent(serviceMessage)
    expect(screen.getByRole('dialog')).not.toHaveTextContent(/\/api\/|GET|502/)
  })

  it('humanizes account-control save failures', async () => {
    getAccountControls.mockResolvedValue({ tool_publication_approval_required: true, revision: 1 })
    updateAccountControls.mockRejectedValue(transportError('POST /api/admin/account-controls'))
    render(<OpsDrawer />)
    fireEvent.click(await screen.findByRole('button', { name: 'On: require independent approval before publishing authored tools' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(serviceMessage)
    expect(screen.getByRole('dialog')).not.toHaveTextContent(/\/api\/|POST|502/)
  })
})
