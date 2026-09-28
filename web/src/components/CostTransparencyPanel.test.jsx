import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CostTransparencyPanel from './CostTransparencyPanel.jsx'
import { config, getCost } from '../api.js'

const title = 'What it costs to run Leaf'
const copy = 'This page shows what Leaf actually costs to run and your share of it. It is not a bill and does not change your plan, quotas, or limits.'
const fixture = {
  period: '2026-09', publication_id: 'pub-1', published_at: '2026-09-28T12:00:00Z',
  missing_sources: ['aws-storage'],
  own_use: {
    tenant_id: 'my-tenant',
    llm: { turns: 12, tokens: { input: 100, output: 50, cache_read: 25, cache_write: 5 }, payer: 'tenant_plan', usd_est: '0.1234567' },
    cad: { runs: 3, engine_seconds: '24.50' },
    marathon: { runs: 2, additive: false },
  },
  totals: { gross_cost_usd: '100.30', credits_usd: '10.00' },
  resources: [
    { resource_id: 'aps:engine', display_name: 'APS engine', unit: 'engine-second', total_usage: '245', gross_cost_usd: '100.10', credits_usd: '10', your_share: '0.123456789012', your_implied_cost_usd: '0.1000004', status: 'MEASURED', coverage: 'complete', leaf_share: { development: '0.1', ci: '0.2', fleet: '0.3', unattributed: '0.05' }, other_customers_share: '0.226543210988' },
    { resource_id: 'aws:s3', display_name: 'Storage', unit: 'GB-month', total_usage: null, gross_cost_usd: '0.20', credits_usd: '0', your_share: '0.2', your_implied_cost_usd: '0.2000004', status: 'ESTIMATED', coverage: 'partial', leaf_share: { development: '0', ci: '0', fleet: '0', unattributed: '0.3' }, other_customers_share: '0.5' },
  ],
  // Deliberately ignored: the UI projects only its public fields.
  tenants: [{ tenant_id: 'other-private-tenant', share: '0.9' }],
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.removeItem('leaf.jwt')
})

function respond(body = fixture) {
  const fetchMock = vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}
async function openPanel() {
  render(<CostTransparencyPanel />)
  fireEvent.click(await screen.findByRole('button', { name: title }))
  return screen.getByRole('region', { name: title })
}

describe('cost transparency', () => {
  it('shows only the caller’s use and aggregate shares with non-billing copy', async () => {
    respond()
    const panel = await openPanel()
    expect(within(panel).getByText(copy)).toBeInTheDocument()
    expect(screen.getByText('Covered by your Claude plan')).toBeInTheDocument()
    expect(screen.getByText('LLM API-equivalent value')).toBeInTheDocument()
    expect(screen.getByText(/Input: 100 · Output: 50/)).toBeInTheDocument()
    expect(screen.getByText('24.50 seconds · 3 runs')).toBeInTheDocument()
    expect(screen.getByText('not added to totals')).toBeInTheDocument()
    expect(screen.getByText('Unavailable. Direct storage use is not published.')).toBeInTheDocument()
    const table = screen.getByRole('table')
    expect(within(table).getByText('0.123457 (12.3457%)')).toBeInTheDocument()
    expect(within(table).getByText('Development: 0.100000 (10.0000%)')).toBeInTheDocument()
    expect(within(table).getByText('CI: 0.200000 (20.0000%)')).toBeInTheDocument()
    expect(within(table).getByText('Fleet: 0.300000 (30.0000%)')).toBeInTheDocument()
    expect(within(table).getByText('Unattributed: 0.050000 (5.0000%)')).toBeInTheDocument()
    expect(within(table).getByText('0.226543 (22.6543%)')).toBeInTheDocument()
    expect(within(table).getByText('MEASURED · complete')).toBeInTheDocument()
    expect(within(table).getByText('ESTIMATED · partial')).toBeInTheDocument()
    expect(within(table).getAllByText('covered by credits')).toHaveLength(2)
    const totals = within(table).getByRole('rowheader', { name: 'Totals' }).closest('tr')
    expect(within(totals).getByText('$100.300000')).toBeInTheDocument()
    expect(within(totals).getByText('$10.000000')).toBeInTheDocument()
    // Sum the original decimals before display rounding, not the rounded cells.
    expect(within(totals).getByText('$0.300001')).toBeInTheDocument()
    expect(screen.getByText(/Missing sources: aws-storage\. Published: 2026-09-28T12:00:00Z/)).toBeInTheDocument()
    expect(panel.textContent).not.toContain('other-private-tenant')
    expect(panel.textContent).not.toContain('my-tenant')
    expect(screen.getByRole('region', { name: 'Resource costs' })).toHaveAttribute('tabindex', '0')
  })

  it('preserves large decimal amounts without floating-point loss', async () => {
    respond({ ...fixture, totals: { gross_cost_usd: '9007199254740993.1234567', credits_usd: '0' } })
    await openPanel()
    expect(screen.getByText('$9007199254740993.123457')).toBeInTheDocument()
  })

  it('shows the empty publication and requests a previous month', async () => {
    const fetchMock = respond({ ...fixture, publication_id: null, resources: [] })
    await openPanel()
    expect(screen.getByText('The first monthly publication has not been made yet.')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Month')).toHaveValue(new Date().toISOString().slice(0, 7))
    fireEvent.change(screen.getByLabelText('Month'), { target: { value: '2025-01' } })
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
      `${config.apiBase}/api/cost?period=2025-01`, expect.any(Object),
    ))
    await screen.findByText('The first monthly publication has not been made yet.')
    fireEvent.keyDown(screen.getByLabelText('Month'), { key: 'Escape' })
    expect(screen.queryByRole('region', { name: title })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: title })).toHaveFocus()
  })

  it('hides the entry when getCost returns null', async () => {
    respond(null)
    await act(async () => { render(<CostTransparencyPanel />) })
    expect(screen.queryByRole('button', { name: title })).not.toBeInTheDocument()
  })

  it('hides mock mode without making a request', async () => {
    const fetchMock = respond()
    await act(async () => { render(<CostTransparencyPanel mock />) })
    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: title })).not.toBeInTheDocument()
  })

  it('forwards the period and the same tenant and auth headers as usage', async () => {
    localStorage.setItem('leaf.jwt', 'test-token')
    const fetchMock = respond()
    await expect(getCost('2026-08')).resolves.toEqual(fixture)
    expect(fetchMock).toHaveBeenCalledWith(`${config.apiBase}/api/cost?period=2026-08`, {
      headers: { 'X-Tenant-Id': config.tenant, Authorization: 'Bearer test-token' },
    })
  })

  it.each([404, 503])('returns null for HTTP %s', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status })))
    await expect(getCost('2026-08')).resolves.toBeNull()
  })

  it('returns null when unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('offline') }))
    await expect(getCost('2026-08')).resolves.toBeNull()
  })
})
