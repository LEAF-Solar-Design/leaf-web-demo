/**
 * Binding check for the ops usage scoreboard.
 *
 * The scoreboard's whole history is drift: it was written against an OpsDrawer
 * that had since grown an account-controls section, and by the time anyone
 * looked again it described a component that no longer existed. A comment
 * recording the version it was derived against rots quietly. This renders the
 * REAL component, so if the scoreboard is ever unwired from the drawer these
 * cases fail instead of silently becoming untrue.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'

vi.mock('../api.js', () => ({
  getOpsUsage: vi.fn(),
  getAccountControls: vi.fn(),
  setTenantDisabled: vi.fn(),
  updateAccountControls: vi.fn(),
}))

import { getAccountControls, getOpsUsage } from '../api.js'
import OpsDrawer from './OpsDrawer.jsx'

afterEach(cleanup)

const denied = () => Object.assign(new Error('nope'), { status: 401 })

const body = (over = {}) => ({
  tenants: [
    { tenant_id: 'tenant-alpha', runs: 7, usd_est: 0.125, disabled: false,
      llm_turns: 3, llm_cost_tokens: 1250, llm_usd_est: 0.03 },
    { tenant_id: 'tenant-beta', runs: 2, usd_est: 0.02, disabled: false,
      llm_turns: 1, llm_cost_tokens: 400, llm_usd_est: 0.01 },
  ],
  platform: {
    profiles: 5,
    autocad_backend: { runs: 9, usd_est: 0.145 },
    llm: { turns: 40, cost_tokens: 9000, usd_est: 0.9 },
  },
  ...over,
})

const scope = (label) => screen.getByText(label).closest('.ops-score-scope')

beforeEach(() => {
  // The account-controls half is a separate authority and is denied here, so
  // these cases speak only to the scoreboard.
  getAccountControls.mockRejectedValue(denied())
})

describe('ops usage scoreboard', () => {
  it('renders both scopes and selects the first profile by default', async () => {
    getOpsUsage.mockResolvedValue(body())
    render(<OpsDrawer />)

    const board = await screen.findByRole('region', { name: 'Usage scoreboard' })
    expect(board).toBeInTheDocument()

    const profile = scope('Profile · lifetime')
    expect(within(profile).getByText('tenant-alpha')).toBeInTheDocument()
    expect(within(profile).getByText('1.3K')).toBeInTheDocument()   // LLM tokens
    expect(within(profile).getByText('runs · $0.125 est.')).toBeInTheDocument()

    const platform = scope('Platform · lifetime')
    expect(within(platform).getByText('5 profiles')).toBeInTheDocument()
    expect(within(platform).getByText('9K')).toBeInTheDocument()
    expect(within(platform).getByText('runs · $0.145 est.')).toBeInTheDocument()
  })

  it('moves the profile scope when another tenant is picked', async () => {
    getOpsUsage.mockResolvedValue(body())
    render(<OpsDrawer />)

    const pick = await screen.findByRole('button', { name: 'tenant-beta' })
    expect(pick).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(pick)

    const profile = scope('Profile · lifetime')
    expect(within(profile).getByText('tenant-beta')).toBeInTheDocument()
    expect(within(profile).getByText('400')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'tenant-beta' }))
      .toHaveAttribute('aria-pressed', 'true')
  })

  it('shows an em dash, not $0.000, when the LLM ledger could not be read', async () => {
    getOpsUsage.mockResolvedValue({
      tenants: [{ tenant_id: 'tenant-alpha', runs: 7, usd_est: 0.125, disabled: false,
        llm_turns: null, llm_cost_tokens: null, llm_usd_est: null }],
      platform: {
        profiles: 1,
        autocad_backend: { runs: 7, usd_est: 0.125 },
        llm: { turns: null, cost_tokens: null, usd_est: null },
      },
    })
    render(<OpsDrawer />)

    await screen.findByRole('region', { name: 'Usage scoreboard' })
    const profile = scope('Profile · lifetime')
    expect(within(profile).getByText('metered · — est.')).toBeInTheDocument()
    expect(within(profile).queryByText('metered · $0.000 est.')).toBeNull()
  })

  it('stays off the screen entirely when the operator grant is missing', async () => {
    getOpsUsage.mockRejectedValue(Object.assign(new Error('nope'), { status: 403 }))
    render(<OpsDrawer />)

    await screen.findByText(/operator grant is required/i)
    expect(screen.queryByRole('region', { name: 'Usage scoreboard' })).toBeNull()
  })
})
