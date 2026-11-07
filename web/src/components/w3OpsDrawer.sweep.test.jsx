// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getAccountControls, getArloReviews, getCost, getOpsUsage, saveArloReview, setTenantDisabled } from '../api.js'
import ArloProposalReview from './ArloProposalReview.jsx'
import CapabilityCatalog from './CapabilityCatalog.jsx'
import CostTransparencyPanel from './CostTransparencyPanel.jsx'
import EntitlementGate from './EntitlementGate.jsx'
import OpsDrawer from './OpsDrawer.jsx'

vi.mock('../api.js', async importOriginal => ({
  ...await importOriginal(),
  getAccountControls: vi.fn(),
  getOpsUsage: vi.fn(),
  setTenantDisabled: vi.fn(),
  getArloReviews: vi.fn(),
  saveArloReview: vi.fn(),
  getCost: vi.fn(),
}))

const context = {
  org_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222',
  job_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444',
}
const proposal = { proposal_id: 'first', production_valid: false, violations: [], quantities: [], routes: [], placements: [] }
const envelope = {
  result_sha256: 'result-hash',
  solver_input: { organization_id: context.org_id, project_id: context.project_id, input_version_id: context.input_version_id },
  solver_result: { status: 'complete', proposals: [proposal, { ...proposal, proposal_id: 'second' }] },
}

beforeEach(() => {
  vi.resetAllMocks()
  getOpsUsage.mockResolvedValue({ tenants: [{ tenant_id: 'profile-one', runs: null, usd_est: null }] })
  getAccountControls.mockResolvedValue({ tool_publication_approval_required: true, revision: 1 })
  setTenantDisabled.mockResolvedValue({})
  getArloReviews.mockResolvedValue({ job_id: context.job_id, result_sha256: envelope.result_sha256, reviews: [] })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('operations drawer issue sweep', () => {
  it('keeps unknown tenant readings distinct from measured zero', async () => {
    getOpsUsage.mockResolvedValue({ tenants: [
      { tenant_id: 'unknown', runs: null, usd_est: null },
      { tenant_id: 'zero', runs: 0, usd_est: 0 },
    ] })
    render(<OpsDrawer />)
    const table = await screen.findByRole('table')
    const unknown = within(table).getByRole('button', { name: 'unknown' }).closest('tr')
    const zero = within(table).getByRole('button', { name: 'zero' }).closest('tr')
    expect(within(unknown).getAllByText('Unavailable')).toHaveLength(2)
    expect(within(zero).getByText('0')).toBeInTheDocument()
    expect(within(zero).getByText('$0.000')).toBeInTheDocument()
  })

  it('moves focus to Keep and restores the tenant action on cancel and completion', async () => {
    render(<OpsDrawer onDismiss={() => {}} />)
    const table = await screen.findByRole('table')
    let disable = within(table).getByRole('button', { name: 'Disable' })
    disable.focus()
    fireEvent.click(disable)
    expect(screen.getByRole('button', { name: 'Keep' })).toHaveFocus()
    fireEvent.click(screen.getByRole('button', { name: 'Keep' }))
    disable = within(table).getByRole('button', { name: 'Disable' })
    expect(disable).toHaveFocus()
    fireEvent.click(disable)
    fireEvent.click(within(table).getByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(within(table).getByRole('button', { name: 'Enable' })).toHaveFocus())
  })

  it('keeps account confirmation focus inside the drawer and restores its toggle', async () => {
    render(<OpsDrawer />)
    const toggle = await screen.findByRole('button', { name: 'On: require independent approval before publishing authored tools' })
    toggle.focus()
    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Keep current' })).toHaveFocus()
    fireEvent.click(screen.getByRole('button', { name: 'Keep current' }))
    expect(screen.getByRole('button', { name: 'On: require independent approval before publishing authored tools' })).toHaveFocus()
  })

  it('shows an unavailable live plan without claiming demo access', () => {
    const { container } = render(<EntitlementGate tier="team" mock={false} entitlements={null} />)
    expect(screen.getByText('Plan unavailable')).toBeInTheDocument()
    expect(container.querySelectorAll('.ent-state')).toHaveLength(4)
    expect([...container.querySelectorAll('.ent-state')].every(row => row.textContent === 'Unavailable')).toBe(true)
    expect(container.textContent).not.toMatch(/demo|full access|not signed in|included/)
  })

  it('does not grant omitted capability values in a partial plan response', () => {
    render(<EntitlementGate tier="team" entitlements={{ entitlements: { run_read: true } }} />)
    expect(screen.getAllByText('Unavailable')).toHaveLength(3)
    expect(screen.getAllByText('included')).toHaveLength(1)
    expect(screen.queryByText(/All capabilities are included/)).toBeNull()
  })

  it('describes general fallback tools without implying drawing availability', () => {
    const retry = vi.fn()
    render(<CapabilityCatalog catalog={{ source: 'flat-fallback', families: [{
      family_id: 'all', label: 'Tools', description: 'The classic flat catalog (families endpoint unavailable).',
      capabilities: [{ name: 'Inspect', description: 'Inspect the drawing', kind: 'read', capabilities: [], params: {} }],
    }] }} openFamilies={{ all: true }} onToggleFamily={() => {}} onRetryCatalog={retry} tools={[]} />)
    expect(screen.getByRole('status')).toHaveTextContent('Availability for this drawing could not be checked')
    expect(screen.getAllByText('General tools', { exact: false }).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: /Inspect/ }))
    expect(screen.getByRole('button', { name: 'Review & run' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Retry availability' }))
    expect(retry).toHaveBeenCalledOnce()
    expect(screen.getByRole('status').parentElement.textContent).not.toMatch(/flat fallback|endpoint unavailable|families/)
  })

  it('translates unknown history status values instead of exposing backend codes', async () => {
    getCost.mockResolvedValue(null)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({
      months: [{ period: '2026-09', status: 'INTERNAL_PENDING_STATE', your_implied_cost_usd: '0' }],
    }) }))
    render(<CostTransparencyPanel />)
    fireEvent.click(screen.getByRole('button', { name: 'What Leaf costs to operate' }))
    fireEvent.click(screen.getByRole('button', { name: 'History' }))
    const months = await screen.findByRole('list', { name: 'Cost history months' })
    expect(months).toHaveTextContent('Status unavailable')
    expect(months).not.toHaveTextContent('INTERNAL_PENDING_STATE')
  })

  it('reports missing cost sources without exposing their internal identifiers', async () => {
    getCost.mockResolvedValue({ publication_id: 'publication', missing_sources: ['private-source-id'], resources: [] })
    render(<CostTransparencyPanel />)
    fireEvent.click(screen.getByRole('button', { name: 'What Leaf costs to operate' }))
    expect(await screen.findByText(/Missing sources: 1 unavailable/)).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'What Leaf costs to operate' })).not.toHaveTextContent('private-source-id')
  })

  it('explains why incomplete proposals cannot be accepted or rejected', async () => {
    render(<ArloProposalReview context={context} envelope={{ ...envelope, solver_result: {
      ...envelope.solver_result, status: 'incomplete', proposals: [{ ...proposal, violations: ['INTERNAL_CHECK'] }],
    } }} />)
    await waitFor(() => expect(screen.queryByText('Loading saved review history…')).toBeNull())
    expect(screen.getByText(/This proposal is incomplete and has 1 design check to resolve/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Accept proposal' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reject proposal' })).toBeDisabled()
    expect(screen.queryByText('INTERNAL_CHECK')).toBeNull()
  })

  it('clears a failed save when switching proposals', async () => {
    saveArloReview.mockRejectedValue(new Error('Failed to fetch'))
    render(<ArloProposalReview context={context} envelope={envelope} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Accept proposal' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Accept proposal' }))
    expect(await screen.findByRole('alert')).not.toHaveTextContent('Failed to fetch')
    fireEvent.change(screen.getByLabelText('Feeder proposal'), { target: { value: 'second' } })
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
