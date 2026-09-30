import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import CostTransparencyPanel from './CostTransparencyPanel.jsx'
import { config, getCost, getUsage } from '../api.js'

const title = 'What Leaf costs to operate'
const copy = 'This page shows what Leaf actually costs to run and your share of it. It is not a bill and does not change your plan, quotas, or limits.'
const fixture = {
  period: '2026-09', publication_id: 'pub-1', published_at: '2026-09-28T12:00:00Z',
  missing_sources: ['aws-storage'],
  your_total_implied_cost_usd: '0.300009', stale: false, stale_reason: null,
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
  await waitFor(() => expect(screen.queryByText('Loading costs…')).not.toBeInTheDocument())
  return screen.getByRole('region', { name: title })
}

describe('cost transparency', () => {
  it.each(['success', 'failure-with-retry', 'unpublished', 'unreadable'])('has no button name matching run in the %s state', async (state) => {
    const fetchMock = respond(state === 'unpublished' || state === 'unreadable'
      ? { ...fixture, publication_id: null, resources: [], degraded_mode: state === 'unreadable' }
      : fixture)
    if (state === 'failure-with-retry') fetchMock.mockRejectedValueOnce(new TypeError('offline'))
    await openPanel()
    expect(screen.getByRole('button', { name: title })).toBeInTheDocument()
    if (state === 'failure-with-retry') expect(screen.getByRole('button', { name: 'Retry costs' })).toBeInTheDocument()
    expect(screen.queryAllByRole('button', { name: /run/i })).toHaveLength(0)
  })

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
    expect(within(totals).getByText('$100.30')).toBeInTheDocument()
    expect(within(totals).getByText('$10.00')).toBeInTheDocument()
    // The API total is authoritative, even when it differs from the row sum.
    expect(within(totals).getByText('$0.300009')).toBeInTheDocument()
    expect(within(table).getByText('Estimated share, not metered')).toBeInTheDocument()
    expect(screen.queryByText(/Cost publication is stale/)).not.toBeInTheDocument()
    expect(screen.getByText(/Missing sources: aws-storage\. Published: 2026-09-28T12:00:00Z/)).toBeInTheDocument()
    expect(panel.textContent).not.toContain('other-private-tenant')
    expect(panel.textContent).not.toContain('my-tenant')
    expect(panel.textContent).not.toMatch(/[\u2013\u2014]/)
    expect(screen.getByRole('region', { name: 'Resource costs' })).toHaveAttribute('tabindex', '0')
  })

  it.each([
    ['321714', 'build-minutes', 'complete', 'share'],
    ['5003.04', 'instance-hours', 'partial', 'usd-by-environment'],
    ['0', 'build-minutes', 'unknown', 'share'],
  ])('shows physical use %s %s separately from the allocation basis', async (amount, unit, coverage, allocationUnit) => {
    respond({ ...fixture, resources: [{ ...fixture.resources[0], total_usage: '1', unit: allocationUnit,
      physical_usage: { quantity: amount, unit, coverage } }] })
    const panel = await openPanel()
    const row = screen.getByRole('rowheader', { name: /APS engine/ }).closest('tr')
    const cell = within(row).getAllByRole('cell')[0]
    expect(within(cell).getByText(`${amount} ${unit}`)).toBeInTheDocument()
    expect(within(cell).getByText(`Physical use coverage: ${coverage}`)).toBeInTheDocument()
    expect(within(cell).getByText(`Allocation basis: 1 ${allocationUnit}`)).toBeInTheDocument()
    expect(screen.queryAllByRole('button', { name: /run/i })).toHaveLength(0)
    expect(panel.textContent).not.toMatch(/[\u2013\u2014]/)
  })

  it.each([null, undefined])('preserves allocation-only cells when physical usage is %s', async (physical_usage) => {
    respond({ ...fixture, resources: fixture.resources.map((row) => ({ ...row, physical_usage })) })
    await openPanel()
    const engine = screen.getByRole('rowheader', { name: /APS engine/ }).closest('tr')
    const storage = screen.getByRole('rowheader', { name: /Storage/ }).closest('tr')
    expect(within(engine).getAllByRole('cell')[0]).toHaveTextContent(/^245 engine-second$/)
    expect(within(storage).getAllByRole('cell')[0]).toHaveTextContent(/^Unavailable GB-month$/)
    expect(screen.queryByText(/Physical use coverage:|Allocation basis:/)).not.toBeInTheDocument()
  })

  it('shows each payer bucket without adding tenant-funded value to Leaf totals', async () => {
    respond({ ...fixture, own_use: { ...fixture.own_use, llm: { ...fixture.own_use.llm,
      payer: 'mixed', usd_est: '1020.1534567', by_payer: {
        tenant_plan: { turns: 4, usd_est: '1000.1234567' },
        tenant_api_key: { turns: 3, usd_est: '20.00' },
        leaf: { turns: 2, usd_est: '0.03' },
        unknown: { turns: 1, usd_est: null },
        mixed: { turns: 2, usd_est: '0' },
      },
    } } })
    const panel = await openPanel()
    const breakdown = screen.getByText('LLM API-equivalent value').nextElementSibling
    for (const line of [
      'Your Claude plan: 4 turns · $1000.123457 API-equivalent value',
      'Your own API key: 3 turns · $20.00 API-equivalent value',
      'Leaf: 2 turns · $0.03 API-equivalent value',
      'Payer unknown: 1 turn · Unavailable API-equivalent value',
      'Mixed payers: 2 turns · $0.00 API-equivalent value',
    ]) expect(within(breakdown).getByText(line)).toBeInTheDocument()
    expect(breakdown.querySelectorAll('div')).toHaveLength(5)
    const totals = screen.getByRole('rowheader', { name: 'Totals' }).closest('tr')
    expect(within(totals).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      'Not applicable', '$100.30', '$10.00', 'Not applicable', '$0.300009', 'Not applicable', 'Not applicable',
    ])
    expect(screen.queryAllByRole('button', { name: /run/i })).toHaveLength(0)
    expect(panel.textContent).not.toMatch(/[\u2013\u2014]/)
  })

  it('shows stale publication details and the environment estimate basis', async () => {
    respond({ ...fixture, stale: true, stale_reason: 'This publication is more than 36 hours old.',
      resources: fixture.resources.map((row) => ({ ...row, unit: 'usd-by-environment' })) })
    const panel = await openPanel()
    expect(screen.getByText(/Cost publication is stale.*more than 36 hours old/)).toBeInTheDocument()
    expect(screen.getAllByText('Split by environment tag and activity')).toHaveLength(1)
    expect(screen.queryAllByRole('button', { name: /run/i })).toHaveLength(0)
    expect(panel.textContent).not.toMatch(/[\u2013\u2014]/)
  })

  it.each([['0.001000', '$0.001'], ['0.000001', '$0.000001'], ['0.000000', '$0.00']])('displays the API total %s precisely', async (amount, displayed) => {
    respond({ ...fixture, your_total_implied_cost_usd: amount })
    await openPanel()
    const totals = screen.getByRole('rowheader', { name: 'Totals' }).closest('tr')
    expect(within(totals).getByText(displayed)).toBeInTheDocument()
  })

  it('does not infer a missing API total from resource rows', async () => {
    respond({ ...fixture, your_total_implied_cost_usd: undefined })
    await openPanel()
    const totals = screen.getByRole('rowheader', { name: 'Totals' }).closest('tr')
    expect(within(totals).getByText('Unavailable')).toBeInTheDocument()
  })

  it('preserves large decimal amounts without floating-point loss', async () => {
    respond({ ...fixture, totals: { gross_cost_usd: '9007199254740993.1234567', credits_usd: '0' } })
    await openPanel()
    expect(screen.getByText('$9007199254740993.123457')).toBeInTheDocument()
  })

  it('shows the empty publication and requests a previous month', async () => {
    const fetchMock = respond({ ...fixture, publication_id: null, resources: [] })
    await openPanel()
    expect(screen.getByText('The publication for this month has not been made yet.')).toBeInTheDocument()
    expect(screen.queryByText(/first|unreadable/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Month')).toHaveValue(new Date().toISOString().slice(0, 7))
    fireEvent.change(screen.getByLabelText('Month'), { target: { value: '2025-01' } })
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(
      `${config.apiBase}/api/cost?period=2025-01`, expect.any(Object),
    ))
    await screen.findByText('The publication for this month has not been made yet.')
    fireEvent.keyDown(screen.getByLabelText('Month'), { key: 'Escape' })
    expect(screen.queryByRole('region', { name: title })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: title })).toHaveFocus()
  })

  it.each(['null', 'offline', '503'])('keeps the entry and retries after %s', async (failure) => {
    const fetchMock = respond()
    if (failure === 'offline') fetchMock.mockRejectedValueOnce(new TypeError('offline'))
    else fetchMock.mockResolvedValueOnce(new Response(failure === 'null' ? 'null' : '{}', { status: failure === '503' ? 503 : 200 }))
    await openPanel()
    expect(screen.getByRole('button', { name: title })).toBeInTheDocument()
    expect(screen.getByText('Costs are unavailable for this month.')).toBeInTheDocument()
    expect(screen.queryByText(/has not been made yet/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry costs' }))
    await screen.findByRole('table')
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[1][0]).toBe(fetchMock.mock.calls[0][0])
    expect(screen.queryByRole('button', { name: 'Retry costs' })).not.toBeInTheDocument()
  })

  it('distinguishes an unreadable publication from an unpublished month', async () => {
    respond({ ...fixture, publication_id: null, degraded_mode: true, resources: [] })
    const panel = await openPanel()
    expect(screen.getByText('Cost data for this month is unreadable. Publication details are unavailable.')).toBeInTheDocument()
    expect(screen.queryByText(/has not been made yet/)).not.toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(panel.textContent).not.toMatch(/[\u2013\u2014]/)
  })

  it.each(['tenant_api_key', 'unknown', 'mixed', null])('labels payer %s without claiming plan coverage', async (payer) => {
    respond({ ...fixture, own_use: { ...fixture.own_use, llm: { ...fixture.own_use.llm, payer } } })
    await openPanel()
    expect(screen.queryByText('Covered by your Claude plan')).not.toBeInTheDocument()
    expect(screen.getByText(payer === 'tenant_api_key' ? 'Paid through your own API key' : 'Payer may vary or is unknown')).toBeInTheDocument()
  })

  it.each(['partial', 'unknown', 'complete'])('shows incomplete own-use coverage: %s', async (coverage) => {
    const own_use = Object.fromEntries(['llm', 'cad', 'marathon'].map((key) => [key, { ...fixture.own_use[key], coverage }]))
    respond({ ...fixture, own_use })
    await openPanel()
    for (const label of ['LLM', 'CAD', 'Marathon']) {
      if (coverage === 'complete') expect(screen.queryByText(new RegExp(`${label} use coverage:`))).not.toBeInTheDocument()
      else expect(screen.getByText(new RegExp(`${label} use coverage: ${coverage}`))).toBeInTheDocument()
    }
  })

  it('shows unknown coverage when own use is unavailable', async () => {
    respond({ ...fixture, own_use: null, degraded_mode: true })
    await openPanel()
    expect(screen.getAllByText(/use coverage: unknown/)).toHaveLength(3)
    expect(screen.getByRole('table')).toBeInTheDocument()
  })

  it('hides mock mode without making a request', async () => {
    const fetchMock = respond()
    await act(async () => { render(<CostTransparencyPanel mock />) })
    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: title })).not.toBeInTheDocument()
  })

  it('forwards the period and the same tenant and auth headers as usage', async () => {
    const fakeJwt = 'test-token'
    localStorage.setItem('leaf.jwt', fakeJwt)
    const fetchMock = respond()
    await expect(getCost('2026-08')).resolves.toEqual(fixture)
    await getUsage()
    const [costUrl, costInit] = fetchMock.mock.calls[0]
    const [, usageInit] = fetchMock.mock.calls[1]
    expect(costUrl).toBe(`${config.apiBase}/api/cost?period=2026-08`)
    expect(costInit.headers).toEqual(usageInit.headers)
    expect(Object.values(costInit.headers).join(' ')).toContain(fakeJwt)
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
