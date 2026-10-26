import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import AddonsPanel from './AddonsPanel.jsx'
import * as addonsApi from '../addons.js'

const LABELS = ['Autodesk processing', 'Hosted execution', 'Long-running tasks', 'Repository hosting',
  'Cloud resources', 'File storage', 'Drawing solves', 'Tool checks', 'GPU inference', 'iOS builds', 'App sign-in']
const statement = {
  placeholder: true,
  lines: [
    { addon: 'e2b_exec', mode: 'managed', cost_usd: '12.34', fee_usd: '1.23', total_usd: '13.57' },
    { addon: 'storage', mode: 'managed', cost_usd: '2.00', fee_usd: '0.20', total_usd: '2.20' },
    { addon: 'forge_hosting', mode: 'byo', cost_usd: '0.00', fee_usd: '0.00', total_usd: '0.00' },
  ],
  totals: { cost_usd: '14.34', fee_usd: '1.43', total_usd: '15.77' },
}
const response = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

function mockFetch({ usage = statement, settings = {}, put } = {}) {
  const fetch = vi.fn(async (url, options) => {
    if (options?.method === 'PUT') return put ? put(url, options) : response({ addon: { mode: 'byo', byo_ref: 'my-e2b-key' } })
    if (url.includes('/billing/statement?')) return response({ statement: usage })
    return response({ addons: settings })
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}
function panel(props = {}) {
  return render(<AddonsPanel orgId="org-1" open onToggle={vi.fn()} {...props} />)
}
async function loadedRow(label = 'Hosted execution') {
  return screen.findByRole('region', { name: label })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('Studio Add-ons', () => {
  it('asks for a workspace without fetching or offering Retry when none is selected', () => {
    const fetch = mockFetch()
    panel({ orgId: null })
    expect(screen.getByText('Choose a workspace to manage add-ons.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('renders all eleven labels with equally styled mode buttons', async () => {
    mockFetch()
    panel()
    await loadedRow()
    expect(Object.keys(addonsApi.ADDON_LABELS)).toHaveLength(11)
    for (const label of LABELS) {
      const row = within(screen.getByRole('region', { name: label }))
      const choices = row.getAllByRole('radio')
      expect(choices.map((choice) => choice.textContent)).toEqual(['Managed', 'Your own', 'Off'])
      expect(choices.map((choice) => choice.tagName)).toEqual(['BUTTON', 'BUTTON', 'BUTTON'])
      expect(choices.map((choice) => choice.className.replace(/ on$/, ''))).toEqual(['ca-opt', 'ca-opt', 'ca-opt'])
      for (const choice of choices) {
        fireEvent.click(choice)
        expect(choice).toHaveClass('on')
        expect(choice).toHaveAttribute('aria-checked', 'true')
      }
    }
  })

  it('saves only the BYO credential name, shows busy state and the API error', async () => {
    let finish
    const pending = new Promise((resolve) => { finish = resolve })
    const fetch = mockFetch({ put: () => pending })
    const save = vi.spyOn(addonsApi, 'setAddonMode')
    panel()
    const row = within(await loadedRow())
    expect(row.queryByRole('textbox')).not.toBeInTheDocument()
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    const input = row.getByRole('textbox', { name: 'Credential name for Hosted execution' })
    expect(input).toHaveAttribute('placeholder', 'credential name, for example my-e2b-key')
    expect(row.getByText(/value is stored in your tenant's credential store, not here/)).toBeInTheDocument()
    expect(row.getByRole('button', { name: 'Save' })).toBeDisabled()
    fireEvent.change(input, { target: { value: 'my-e2b-key' } })
    fireEvent.click(row.getByRole('button', { name: 'Save' }))
    expect(save).toHaveBeenCalledWith('org-1', 'e2b_exec', 'byo', 'my-e2b-key')
    expect(row.getByRole('button', { name: 'Saving...' })).toBeDisabled()
    const mutation = fetch.mock.calls.find(([, options]) => options.method === 'PUT')
    expect(JSON.parse(mutation[1].body)).toEqual({ mode: 'byo', byo_ref: 'my-e2b-key' })
    finish(response({ error: { message: 'Credential name was not found.' } }, 400))
    expect(await row.findByRole('alert')).toHaveTextContent('Credential name was not found.')
    expect(input).toBeEnabled()
    expect(input).toHaveValue('my-e2b-key')
  })

  it('shows a failed save detail string in the row alert', async () => {
    mockFetch({ put: () => response({ detail: 'byo_ref must be a credential name', error: { message: 'Generic error' } }, 422) })
    panel()
    const row = within(await loadedRow())
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    fireEvent.change(row.getByRole('textbox'), { target: { value: 'my key' } })
    fireEvent.click(row.getByRole('button', { name: 'Save' }))
    expect(await row.findByRole('alert')).toHaveTextContent('byo_ref must be a credential name')
  })

  it('joins failed save validation details in the row alert', async () => {
    mockFetch({ put: () => response({ detail: [{ msg: 'a' }, { msg: 'b' }], message: 'Generic error' }, 422) })
    panel()
    const row = within(await loadedRow())
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    fireEvent.change(row.getByRole('textbox'), { target: { value: 'my key' } })
    fireEvent.click(row.getByRole('button', { name: 'Save' }))
    expect(await row.findByRole('alert')).toHaveTextContent('a; b')
  })

  it('humanizes a failed save with a non-JSON body', async () => {
    mockFetch({ put: () => new Response('Internal Server Error', { status: 500 }) })
    panel()
    const row = within(await loadedRow())
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    fireEvent.change(row.getByRole('textbox'), { target: { value: 'my-e2b-key' } })
    fireEvent.click(row.getByRole('button', { name: 'Save' }))
    expect(await row.findByRole('alert')).toHaveTextContent('The service had a temporary problem. Try again.')
  })

  it('renders managed statement lines, decimal money, totals and placeholder pricing', async () => {
    mockFetch()
    panel()
    const month = within(await screen.findByRole('region', { name: 'This month' }))
    expect(month.getByText('Prices are placeholders while pricing is set')).toBeInTheDocument()
    expect(month.getByRole('columnheader', { name: 'Management fee' })).toBeInTheDocument()
    const execution = within(month.getByRole('row', { name: /Hosted execution/ }))
    for (const money of ['$12.34', '$1.23', '$13.57']) expect(execution.getByText(money)).toBeInTheDocument()
    const totals = within(month.getByRole('row', { name: /Totals/ }))
    for (const money of ['$14.34', '$1.43', '$15.77']) expect(totals.getByText(money)).toBeInTheDocument()
    expect(month.queryByText('Repository hosting')).not.toBeInTheDocument()
  })

  it('loads existing modes and names and confirms successful saves', async () => {
    mockFetch({ settings: { e2b_exec: { mode: 'byo', byo_ref: 'existing-name' }, storage: { mode: 'managed' } } })
    panel()
    const row = within(await loadedRow())
    expect(row.getByRole('radio', { name: 'Your own' })).toHaveAttribute('aria-checked', 'true')
    expect(row.getByRole('textbox')).toHaveValue('existing-name')
    expect(within(screen.getByRole('region', { name: 'File storage' })).getByRole('radio', { name: 'Managed' })).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(row.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(row.getByRole('status')).toHaveTextContent('Saved'))
    expect(row.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('shows the empty state when there are only BYO lines', async () => {
    mockFetch({ usage: { ...statement, placeholder: false, lines: [statement.lines[2]] } })
    panel()
    expect(await screen.findByText('No managed usage this month.')).toBeInTheDocument()
    expect(screen.queryByText('Prices are placeholders while pricing is set')).not.toBeInTheDocument()
  })

  it('shows loading and an API failure, then retries', async () => {
    const fetch = vi.fn().mockResolvedValue(response({ error: { message: 'Add-ons unavailable.' } }, 503))
    vi.stubGlobal('fetch', fetch)
    panel()
    expect(screen.getByRole('status')).toHaveTextContent('Loading add-ons...')
    expect(await screen.findByRole('alert')).toHaveTextContent('Add-ons unavailable.')
    mockFetch()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await loadedRow()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps the settings usable when the statement is refused at the event limit', async () => {
    const fetch = vi.fn(async (url, options) => {
      if (options?.method === 'PUT') return response({ addon: { mode: 'byo', byo_ref: 'my-e2b-key' } })
      if (url.includes('/billing/statement?')) return response({ detail: 'statement window exceeds the event limit' }, 409)
      return response({ addons: {} })
    })
    vi.stubGlobal('fetch', fetch)
    panel()
    await loadedRow()
    for (const label of LABELS) expect(screen.getByRole('region', { name: label })).toBeInTheDocument()
    const month = within(screen.getByRole('region', { name: 'This month' }))
    expect(await month.findByRole('alert')).toHaveTextContent('statement window exceeds the event limit')
    const row = within(screen.getByRole('region', { name: 'Hosted execution' }))
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    fireEvent.change(row.getByRole('textbox'), { target: { value: 'my-e2b-key' } })
    fireEvent.click(row.getByRole('button', { name: 'Save' }))
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/addons/e2b_exec'), expect.objectContaining({ method: 'PUT' }))
    expect(await row.findByRole('status')).toHaveTextContent('Saved')
  })

  it('retries only the statement from This month', async () => {
    const fetch = vi.fn(async (url) => {
      if (url.includes('/billing/statement?')) return response({ detail: 'statement window exceeds the event limit' }, 409)
      return response({ addons: {} })
    })
    vi.stubGlobal('fetch', fetch)
    panel()
    const month = within(await screen.findByRole('region', { name: 'This month' }))
    expect(await month.findByRole('alert')).toHaveTextContent('statement window exceeds the event limit')
    const retryFetch = mockFetch()
    fireEvent.click(month.getByRole('button', { name: 'Retry' }))
    expect(await month.findByRole('rowheader', { name: 'Hosted execution' })).toBeInTheDocument()
    expect(month.getByText('$15.77')).toBeInTheDocument()
    const addonsGets = [...fetch.mock.calls, ...retryFetch.mock.calls]
      .filter(([url, options]) => url.endsWith('/addons') && (!options?.method || options.method === 'GET'))
    expect(addonsGets).toHaveLength(1)
    expect(retryFetch).toHaveBeenCalledWith(expect.stringContaining('/billing/statement?'), expect.any(Object))
  })

  it('offers only text inputs named for credentials across all BYO rows', async () => {
    mockFetch()
    const { container } = panel()
    await loadedRow()
    for (const button of screen.getAllByRole('radio', { name: 'Your own' })) fireEvent.click(button)
    expect(screen.getAllByRole('textbox')).toHaveLength(11)
    for (const input of container.querySelectorAll('input')) expect(input.type).toBe('text')
    for (const label of container.querySelectorAll('label')) expect(label.textContent).not.toMatch(/secret value|API key value/i)
  })

  it('uses the standard trigger, Escape owner and outside close', () => {
    mockFetch()
    const onToggle = vi.fn()
    panel({ onToggle })
    fireEvent.click(screen.getByRole('button', { name: 'Add-ons' }))
    expect(onToggle).toHaveBeenLastCalledWith(false)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onToggle).toHaveBeenCalledTimes(2)
    fireEvent.mouseDown(document.body)
    expect(onToggle).toHaveBeenCalledTimes(3)
  })

  it('does not fetch when closed or in mock mode', () => {
    const fetch = mockFetch()
    const { rerender } = panel({ open: false })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    rerender(<AddonsPanel mock orgId="org-1" open onToggle={vi.fn()} />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })
})

describe('add-on API helpers', () => {
  it('uses the API auth and org scope, encodes paths and leaves BYO references out of managed/off writes', async () => {
    localStorage.setItem('leaf.jwt', 'test-token')
    const fetch = mockFetch()
    await addonsApi.getAddons('org/1')
    await addonsApi.getStatement('org/1', '2026-10')
    for (const mode of ['managed', 'off']) await addonsApi.setAddonMode('org/1', 'e2b_exec', mode, 'old-name')
    expect(fetch.mock.calls[0][0]).toContain('/api/orgs/org%2F1/addons')
    expect(fetch.mock.calls[1][0]).toContain('/billing/statement?period=2026-10')
    for (const [, options] of fetch.mock.calls) {
      expect(options.headers).toMatchObject({ Authorization: `Bearer ${'test-token'}`, 'X-Org-Id': 'org/1' })
    }
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ mode: 'managed' })
    expect(JSON.parse(fetch.mock.calls[3][1].body)).toEqual({ mode: 'off' })
  })

  it('preserves failure status and body and rejects a missing workspace without fetching', async () => {
    const fetch = vi.fn(async () => response({ error: { message: 'Owner access required.' } }, 403))
    vi.stubGlobal('fetch', fetch)
    await expect(addonsApi.getAddons('org-1')).rejects.toMatchObject({ status: 403, message: 'Owner access required.', body: { error: { message: 'Owner access required.' } } })
    await expect(addonsApi.getAddons(null)).rejects.toThrow('Choose a workspace')
    expect(fetch).toHaveBeenCalledTimes(1)
  })
})
