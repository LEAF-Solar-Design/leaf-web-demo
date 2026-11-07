import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import AddonsPanel from './AddonsPanel.jsx'
import * as addonsApi from '../addons.js'

const LABELS = ['Autodesk processing', 'Hosted execution', 'Long-running tasks', 'Repository hosting',
  'Cloud resources', 'File storage', 'Drawing solves', 'Tool checks', 'GPU inference', 'iOS builds', 'App sign-in', 'Kashbot runner']
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

function mockFetch({ usage = statement, settings = {}, catalog, put } = {}) {
  const fetch = vi.fn(async (url, options) => {
    if (options?.method === 'PUT') return put ? put(url, options) : response({ addon: { mode: 'byo', byo_ref: 'my-e2b-key' } })
    if (url.includes('/billing/statement?')) return response({ statement: usage })
    return response(catalog ?? { addons: settings })
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
  it('test_panel_superset', async () => {
    const fetch = mockFetch({ catalog: {
      schema: 'leaf.addons.v1', reader_version: 2, extra: true,
      addons: JSON.parse(`{
        "e2b_exec": {"mode":"managed","min_reader_version":1,"label":"Untrusted label"},
        "storage": {"mode":"managed","min_reader_version":2},
        "future_addon": {"mode":"byo","byo_ref":"future-name"},
        "__proto__": {"mode":"managed"},
        "constructor": {"mode":"managed"},
        "prototype": {"mode":"managed"}
      }`),
    } })
    panel()
    const row = within(await loadedRow())
    expect(row.getByRole('radio', { name: 'Managed' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.queryByRole('region', { name: 'File storage' })).not.toBeInTheDocument()
    expect(screen.queryByText('Untrusted label')).not.toBeInTheDocument()
    expect(screen.getAllByRole('radiogroup')).toHaveLength(LABELS.length - 1)
    expect(fetch.mock.calls.filter(([, options]) => options.method === 'PUT')).toHaveLength(0)
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    fireEvent.change(row.getByRole('textbox'), { target: { value: 'my-e2b-key' } })
    for (const save of screen.getAllByRole('button', { name: 'Save' })) fireEvent.click(save)
    await waitFor(() => expect(screen.getAllByText('Saved')).toHaveLength(LABELS.length - 1))
    const writes = fetch.mock.calls.filter(([, options]) => options.method === 'PUT')
    expect(writes.map(([url]) => url.split('/').at(-1)).sort()).toEqual(
      Object.keys(addonsApi.ADDON_LABELS).filter((addon) => addon !== 'storage').sort())
    expect(JSON.parse(writes.find(([url]) => url.endsWith('/e2b_exec'))[1].body))
      .toEqual({ mode: 'byo', byo_ref: 'my-e2b-key' })
    for (const addon of ['future_addon', '__proto__', 'constructor', 'prototype']) {
      await expect(addonsApi.setAddonMode('org-1', addon, 'managed')).rejects.toThrow('Choose a valid add-on and mode.')
    }
    expect(fetch.mock.calls.filter(([, options]) => options.method === 'PUT')).toHaveLength(writes.length)
  })

  it('asks for a workspace without fetching or offering Retry when none is selected', () => {
    const fetch = mockFetch()
    panel({ orgId: null })
    expect(screen.getByText('Choose a workspace to manage add-ons.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('renders all twelve labels with equally styled mode buttons', async () => {
    mockFetch()
    panel()
    await loadedRow()
    expect(Object.keys(addonsApi.ADDON_LABELS)).toHaveLength(12)
    for (const label of LABELS) {
      const row = within(screen.getByRole('region', { name: label }))
      const choices = row.getAllByRole('radio')
      expect(choices.map((choice) => choice.textContent)).toEqual(
        label === 'Kashbot runner' ? ['Your own', 'Off'] : ['Managed', 'Your own', 'Off'])
      expect(choices.every((choice) => choice.tagName === 'BUTTON')).toBe(true)
      expect(choices.every((choice) => choice.className.replace(/ on$/, '') === 'ca-opt')).toBe(true)
      for (const choice of choices) {
        if (choice.disabled) continue
        fireEvent.click(choice)
        expect(choice).toHaveClass('on')
        expect(choice).toHaveAttribute('aria-checked', 'true')
      }
    }
  })

  it('test_runner_panel', async () => {
    const ref = 'kashbot-runner:7b51687d-2d39-47ac-b598-54f3d1fd9e22'
    const fetch = mockFetch({ catalog: {
      schema: 'leaf.addons.v1', reader_version: 2, extra: { ignored: true },
      addons: {
        kashbot_runner: { mode: 'byo', byo_ref: ref, min_reader_version: 1, extra: true },
        future_addon: { mode: 'byo', byo_ref: 'ignored' },
      },
    }, put: (_url, options) => response(JSON.parse(options.body)) })
    panel()
    const row = within(await loadedRow('Kashbot runner'))
    expect(Object.keys(addonsApi.ADDON_LABELS)).toEqual([
      'aps_solve', 'e2b_exec', 'marathon', 'forge_hosting', 'aws_resources', 'storage',
      'canonical_solve', 'tool_ci', 'gpu_inference', 'ios_build', 'app_auth', 'kashbot_runner',
    ])
    expect(screen.getAllByRole('radiogroup')).toHaveLength(12)
    expect(row.getAllByRole('radio').map((choice) => choice.textContent)).toEqual(['Your own', 'Off'])
    expect(row.getByRole('radio', { name: 'Your own' })).toHaveAttribute('aria-checked', 'true')
    expect(row.getByRole('textbox', { name: 'Runner reference' })).toHaveValue(ref)
    expect(row.queryByText(/credential/i)).not.toBeInTheDocument()
    expect(row.getByRole('button', { name: 'Save' })).toBeEnabled()
    const calls = fetch.mock.calls.length
    await expect(addonsApi.setAddonMode('org-1', 'kashbot_runner', 'managed'))
      .rejects.toThrow('Choose a valid add-on and mode.')
    expect(fetch).toHaveBeenCalledTimes(calls)
    fireEvent.click(row.getByRole('radio', { name: 'Off' }))
    fireEvent.click(row.getByRole('button', { name: 'Save' }))
    expect(await row.findByRole('status')).toHaveTextContent('Saved')
    const writes = fetch.mock.calls.filter(([, options]) => options.method === 'PUT')
    expect(writes).toHaveLength(1)
    expect(writes[0][0]).toContain('/addons/kashbot_runner')
    expect(JSON.parse(writes[0][1].body)).toEqual({ mode: 'off' })
    expect(row.queryByText(ref)).not.toBeInTheDocument()
    expect(row.getByRole('radio', { name: 'Off' })).toHaveAttribute('aria-checked', 'true')
    expect(row.getByRole('radio', { name: 'Your own' })).toBeEnabled()
    expect(row.queryByRole('textbox')).not.toBeInTheDocument()
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    expect(row.getByRole('textbox', { name: 'Runner reference' })).toHaveValue('')
    expect(row.getByRole('button', { name: 'Save' })).toBeDisabled()
    // The link flow can still save a validated reference through the existing helper.
    await addonsApi.setAddonMode('org-1', 'kashbot_runner', 'byo', ref)
    expect(JSON.parse(fetch.mock.calls.at(-1)[1].body)).toEqual({ mode: 'byo', byo_ref: ref })
    const future = addonsApi.readAddonCatalog({ addons: {
      kashbot_runner: { mode: 'byo', byo_ref: ref, min_reader_version: 2 },
      future_addon: { anything: [] },
    } })
    expect(Object.hasOwn(future, 'kashbot_runner')).toBe(false)
    expect(Object.hasOwn(future, 'future_addon')).toBe(false)
    expect(addonsApi.readAddonCatalog({ addons: { kashbot_runner: { mode: 'managed' } } }).kashbot_runner)
      .toEqual({ mode: 'off', byo_ref: null })
  })

  it.each(['', '  '])('runner reference save with surrounding whitespace %j', async (padding) => {
    const ref = 'kashbot-runner:7b51687d-2d39-47ac-b598-54f3d1fd9e22'
    let finish
    const pending = new Promise((resolve) => { finish = resolve })
    mockFetch({ put: () => pending })
    const save = vi.spyOn(addonsApi, 'setAddonMode')
    panel({ orgId: 'org' })
    const row = within(await loadedRow('Kashbot runner'))
    expect(row.getAllByRole('radio').map((choice) => choice.textContent)).toEqual(['Your own', 'Off'])
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    const input = row.getByRole('textbox', { name: 'Runner reference' })
    expect(input).toHaveAttribute('placeholder', 'kashbot-runner:<runner id>')
    expect(input).toHaveAttribute('autocomplete', 'off')
    expect(input).toHaveAttribute('spellcheck', 'false')
    expect(input).toHaveAttribute('maxlength', '128')
    expect(row.queryByText(/credential/i)).not.toBeInTheDocument()
    fireEvent.change(input, { target: { value: `${padding}${ref}${padding}` } })
    fireEvent.click(row.getByRole('button', { name: 'Save' }))
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledWith('org', 'kashbot_runner', 'byo', ref)
    expect(input).toBeDisabled()
    expect(row.getByRole('button', { name: 'Saving...' })).toBeDisabled()
    finish(response({ addon: { mode: 'byo', byo_ref: ref } }))
    expect(await row.findByRole('status')).toHaveTextContent('Saved')
    expect(input).toBeEnabled()
  })

  it('requires a nonblank runner reference before saving Your own', async () => {
    mockFetch()
    panel()
    const row = within(await loadedRow('Kashbot runner'))
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    const input = row.getByRole('textbox', { name: 'Runner reference' })
    const save = row.getByRole('button', { name: 'Save' })
    expect(input).toHaveValue('')
    expect(save).toBeDisabled()
    fireEvent.change(input, { target: { value: '   ' } })
    expect(save).toBeDisabled()
    fireEvent.change(input, { target: { value: 'runner' } })
    expect(save).toBeEnabled()
    fireEvent.change(input, { target: { value: '' } })
    expect(save).toBeDisabled()
  })

  it.each([
    'invalid-runner', 'kashbot-runner:7B51687D-2D39-47AC-B598-54F3D1FD9E22',
  ])('rejects malformed runner reference %s without a request and keeps the typed value', async (value) => {
    const fetch = mockFetch()
    const save = vi.spyOn(addonsApi, 'setAddonMode')
    panel()
    const row = within(await loadedRow('Kashbot runner'))
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    const input = row.getByRole('textbox', { name: 'Runner reference' })
    fireEvent.change(input, { target: { value } })
    fireEvent.click(row.getByRole('button', { name: 'Save' }))
    expect((await row.findByRole('alert')).textContent)
      .toBe('Runner reference must be kashbot-runner: followed by the runner id.')
    expect(save).not.toHaveBeenCalled()
    expect(fetch.mock.calls.filter(([, options]) => options.method === 'PUT')).toHaveLength(0)
    expect(input).toHaveValue(value)
    expect(input).toBeEnabled()
    expect(row.getByRole('button', { name: 'Save' })).toBeEnabled()
    expect(row.queryByRole('status')).not.toBeInTheDocument()
  })

  it('humanizes a runner reference 422 and keeps the well-formed typed value', async () => {
    const ref = 'kashbot-runner:7b51687d-2d39-47ac-b598-54f3d1fd9e22'
    const detail = 'byo_ref must be kashbot-runner:<runner_uuid>'
    const fetch = mockFetch({ put: () => response({ detail, error: { message: 'Generic error' } }, 422) })
    panel()
    const row = within(await loadedRow('Kashbot runner'))
    fireEvent.click(row.getByRole('radio', { name: 'Your own' }))
    const input = row.getByRole('textbox', { name: 'Runner reference' })
    fireEvent.change(input, { target: { value: ref } })
    fireEvent.click(row.getByRole('button', { name: 'Save' }))
    expect(await row.findByRole('alert')).toHaveTextContent('Something went wrong. Try again.')
    const writes = fetch.mock.calls.filter(([, options]) => options.method === 'PUT')
    expect(writes).toHaveLength(1)
    expect(JSON.parse(writes[0][1].body)).toEqual({ mode: 'byo', byo_ref: ref })
    expect(input).toHaveValue(ref)
    expect(input).toBeEnabled()
    expect(row.getByRole('button', { name: 'Save' })).toBeEnabled()
    expect(row.queryByRole('status')).not.toBeInTheDocument()
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
    expect(month.getByText('Prices are not final yet.')).toBeInTheDocument()
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
    expect(screen.queryByText('Prices are not final yet.')).not.toBeInTheDocument()
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

  it('offers text inputs named for credentials or a runner reference across all BYO rows', async () => {
    mockFetch()
    const { container } = panel()
    await loadedRow()
    for (const button of screen.getAllByRole('radio', { name: 'Your own' })) fireEvent.click(button)
    expect(screen.getAllByRole('textbox', { name: /^Credential name for / })).toHaveLength(11)
    expect(screen.getByRole('textbox', { name: 'Runner reference' })).toBeInTheDocument()
    expect(screen.getAllByRole('textbox')).toHaveLength(12)
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
  it('test_catalog_superset', () => {
    const payload = JSON.parse(`{
      "extra": [null, {"anything":true}],
      "__proto__": {"polluted":true}, "constructor": {}, "prototype": {},
      "addons": {
        "e2b_exec": {"mode":"byo","byo_ref":"existing-name","extra":[],
          "__proto__":{"polluted":true},"constructor":{},"prototype":{}},
        "storage": {"mode":"managed","byo_ref":"ignored-name"},
        "aps_solve": {"mode":"off","byo_ref":{"ignored":true}},
        "marathon": {"mode":"invalid"},
        "future_object": {"min_reader_version":"anything","mode":[]},
        "future_array": [null, false, {"nested":true}],
        "future_null": null, "future_string": "anything", "future_number": 42,
        "__proto__": {"polluted":true}, "constructor": {}, "prototype": {}
      }
    }`)
    const before = JSON.stringify(payload)
    const freeze = (value) => {
      if (value !== null && typeof value === 'object') {
        Object.values(value).forEach(freeze)
        Object.freeze(value)
      }
    }
    freeze(payload)
    const settings = addonsApi.readAddonCatalog(payload)
    expect(Object.keys(settings)).toEqual(Object.keys(addonsApi.ADDON_LABELS))
    expect(settings.e2b_exec).toEqual({ mode: 'byo', byo_ref: 'existing-name' })
    expect(settings.storage).toEqual({ mode: 'managed', byo_ref: null })
    for (const addon of Object.keys(addonsApi.ADDON_LABELS).filter((id) => !['e2b_exec', 'storage'].includes(id))) {
      expect(settings[addon]).toEqual({ mode: 'off', byo_ref: null })
    }
    for (const key of ['__proto__', 'constructor', 'prototype']) expect(Object.hasOwn(settings, key)).toBe(false)
    expect(Object.getPrototypeOf(settings)).toBe(Object.prototype)
    expect(Object.hasOwn(Object.prototype, 'polluted')).toBe(false)
    const again = addonsApi.readAddonCatalog(payload)
    expect(again).toEqual(settings)
    expect(again).not.toBe(settings)
    for (const addon of Object.keys(settings)) expect(again[addon]).not.toBe(settings[addon])
    settings.e2b_exec.byo_ref = 'changed'
    settings.tool_ci.mode = 'managed'
    expect(again.e2b_exec.byo_ref).toBe('existing-name')
    expect(settings.app_auth.mode).toBe('off')
    expect(JSON.stringify(payload)).toBe(before)
  })

  it('test_catalog_versions', () => {
    const read = addonsApi.readAddonCatalog
    const invalid = 'Invalid add-on catalog.'
    expect(addonsApi.ADDON_READER_VERSION).toBe(1)
    for (const payload of [null, undefined, [], true, 1, 'catalog', {}, { addons: null }, { addons: [] }, { addons: 'bad' }]) {
      expect(() => read(payload)).toThrow(new Error(invalid))
    }
    for (const schema of ['leaf.addons.v2', '', null, 1, {}, []]) {
      expect(() => read({ schema, addons: {} })).toThrow(new Error('Unsupported add-on catalog schema.'))
    }
    const badVersions = [null, undefined, 0, -1, 1.5, '1', true, {}, [], NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]
    for (const version of badVersions) {
      expect(() => read({ reader_version: version, addons: {} })).toThrow(new Error(invalid))
      if (version !== undefined) expect(() => read({ addons: {} }, version)).toThrow(new Error(invalid))
      const settings = read({ addons: {
        e2b_exec: { mode: 'managed', min_reader_version: version }, storage: { mode: 'managed' },
      } })
      expect(settings.e2b_exec).toEqual({ mode: 'off', byo_ref: null })
      expect(settings.storage).toEqual({ mode: 'managed', byo_ref: null })
    }
    const catalog = { schema: 'leaf.addons.v1', reader_version: Number.MAX_SAFE_INTEGER, addons: {
      e2b_exec: { mode: 'byo', byo_ref: 'existing-name', min_reader_version: 2 },
      storage: { mode: 'managed', min_reader_version: 1 },
      marathon: { mode: 'invalid', min_reader_version: 2 },
    } }
    const older = read(catalog)
    expect(Object.hasOwn(older, 'e2b_exec')).toBe(false)
    expect(Object.hasOwn(older, 'marathon')).toBe(false)
    expect(older.storage).toEqual({ mode: 'managed', byo_ref: null })
    expect(read(catalog, 2).e2b_exec).toEqual({ mode: 'byo', byo_ref: 'existing-name' })
    expect(read(catalog, 2).marathon).toEqual({ mode: 'off', byo_ref: null })
    expect(read(catalog, Number.MAX_SAFE_INTEGER).e2b_exec.mode).toBe('byo')
    for (const entry of [null, [], 42, 'managed', true, {}, { mode: 'MANAGED' }, { mode: 'future' },
      { mode: 'byo' }, ...[null, '', 1, {}, [], false].map((byo_ref) => ({ mode: 'byo', byo_ref }))]) {
      const settings = read({ addons: { e2b_exec: entry, storage: { mode: 'managed' } } })
      expect(settings.e2b_exec).toEqual({ mode: 'off', byo_ref: null })
      expect(settings.storage).toEqual({ mode: 'managed', byo_ref: null })
    }
    expect(read({ addons: {} })).toEqual(read({ schema: 'leaf.addons.v1', reader_version: 1, addons: {} }))
  })

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
