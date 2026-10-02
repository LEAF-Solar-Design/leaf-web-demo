import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api.js'
import SolarWorkspaceTools from './SolarWorkspaceTools.jsx'
import { COMBINER_INTAKE_REASONS } from './solarCombinerIntakeClient.js'

// The route capture also used by SolarLandxmlUpload.test.jsx; the real client validates it.
const FIRST_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","media_type":"application/json","filename":"physical-state.json","byte_length":18283,"content_sha256":"0de2afeb7ae692538ce33fd011b043438cd65cc426fd1cead1933eb4046d22f0","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8"}},"error":null,"degraded_mode":false}`
const UNAUTH_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"UNAUTHENTICATED","message":"missing bearer token (Authorization header)","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Sign in, then repeat the request."},"degraded_mode":false}`
const CREATED = 'Terrain imported for this drawing.'
const EXISTING = 'This terrain was already imported, so nothing changed.'

const COMBINER_VALUE = {
  schema_version: 'leaf.solar-combiner-intake-import.v1', drawing_id: 'solar',
  project_id: 'leaf:project:00000000-0000-4000-8000-000000000001',
  created: true, version: 2, parent_version: 1, graph_rev: 7,
  graph_sha256: 'a'.repeat(64), bound: { l2_inverters: 2, strings: 4 },
  panel_groups: 1, outline_vertices: 4, intake_sha256: 'b'.repeat(64),
  combiner_intake_sha256: 'c'.repeat(64), panel_groups_sha256: 'd'.repeat(64),
}
function combinerResponse(overrides = {}) {
  return new Response(JSON.stringify({ ...COMBINER_VALUE, ...overrides }), {
    status: 200, headers: { 'content-type': 'application/json' },
  })
}
function combinerProps(overrides = {}) {
  const props = supplied({ flow: 'rooftop', onRunPlacement: vi.fn(() => true), ...overrides })
  props.transport?.fetchImpl.mockImplementation(async () => combinerResponse())
  return props
}
const combinerTrigger = () => screen.getByRole('button', { name: 'Import combiner intake' })
const combinerFile = () => screen.getByLabelText('Combiner intake file')
const combinerImport = () => screen.getByRole('button', { name: 'Import' })
const combinerRun = () => screen.getByRole('button', { name: 'Run placement' })
const combinerReason = () => screen.queryByTestId('solar-combiner-reason')
function chooseCombiner() {
  const file = new File(['{}'], 'intake.json', { type: 'application/json' })
  fireEvent.change(combinerFile(), { target: { files: [file] } })
  return file
}
function combinerHardware() {
  for (const [label, value] of [['Model', 'Combiner'], ['DC voltage', '480'], ['AC power', '10']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
}
function openCombiner() { fireEvent.click(combinerTrigger()) }
async function importCombiner() {
  fireEvent.click(combinerImport())
  await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Combiner intake imported for this drawing.'))
}
function workspaceDeferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

function value(created = true) {
  const result = JSON.parse(FIRST_TEXT)
  result.created = created
  delete result.error
  delete result.degraded_mode
  return result
}
function response(created = true) {
  return new Response(JSON.stringify(value(created)), { status: 200, headers: { 'content-type': 'application/json' } })
}
function supplied(overrides = {}) {
  return {
    drawingId: 'solar', projectId: null, drawingVersion: 1, flow: 'ground-physical',
    checkoutHeld: true, busy: false, getCheckoutCapability: vi.fn(),
    onPhysicalHeadChanged: vi.fn(), onDrawingVersionChanged: vi.fn(),
    transport: {
      fetchImpl: vi.fn(async () => response()),
      headers: vi.fn(() => ({ 'X-Tenant-Id': 'workspace-test' })),
      onResponse: vi.fn(),
    },
    ...overrides,
  }
}
const trigger = () => screen.getByRole('button', { name: 'Import LandXML terrain' })
const control = () => screen.queryByTestId('solar-landxml-upload')
const announcement = () => document.querySelector('.solar-workspace-announce')?.textContent ?? ''
function open() { fireEvent.click(trigger()) }
function submit() {
  fireEvent.change(screen.getByLabelText('Drawing units'), { target: { value: 'm' } })
  fireEvent.change(screen.getByLabelText('LandXML file'), {
    target: { files: [new File([new Uint8Array(45305)], 'site.xml', { type: 'application/xml' })] },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Import terrain' }))
}
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.removeItem('leaf.jwt')
})

describe('combiner workspace integration', () => {
  it('W20-07b B1 exposes the local combiner and terrain panels only on their flows', () => {
    const props = combinerProps()
    const view = render(<SolarWorkspaceTools {...props} />)
    for (const flow of ['rooftop', 'ground-physical', 'ground-electrical', 'solaredge-import', 'pvcase-tutorial', 'rooftop']) {
      view.rerender(<SolarWorkspaceTools {...props} flow={flow} />)
      expect(!!screen.queryByRole('button', { name: 'Import combiner intake' })).toBe(flow === 'rooftop')
      expect(!!screen.queryByRole('button', { name: 'Import LandXML terrain' })).toBe(flow === 'ground-physical')
      expect(document.querySelectorAll('section[aria-label="Solar workspace tools"]').length)
        .toBe(['rooftop', 'ground-physical'].includes(flow) ? 1 : 0)
    }
    view.rerender(<SolarWorkspaceTools {...props} projectId="project" />)
    expect(screen.queryByRole('region', { name: 'Solar workspace tools' })).toBeNull()
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
  })

  it('W20-07b B2 focuses the file and closes with trigger focus or a flow change', () => {
    const props = combinerProps()
    const view = render(<SolarWorkspaceTools {...props} />)
    openCombiner()
    expect(document.activeElement).toBe(combinerFile())
    expect(combinerTrigger().getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.queryByLabelText('Combiner intake file')).toBeNull()
    expect(document.activeElement).toBe(combinerTrigger())
    expect(combinerTrigger().getAttribute('aria-expanded')).toBe('false')
    openCombiner()
    view.rerender(<SolarWorkspaceTools {...props} flow="ground-physical" />)
    view.rerender(<SolarWorkspaceTools {...props} />)
    expect(screen.queryByLabelText('Combiner intake file')).toBeNull()
    expect(combinerTrigger().getAttribute('aria-expanded')).toBe('false')
  })

  it('W20-07b B3 reads default authentication and the latest capability for every request', async () => {
    const fetchImpl = vi.fn(async () => combinerResponse())
    const props = combinerProps({ drawingVersion: 2, transport: undefined, getCheckoutCapability: () => 'cap-a' })
    const transport = { fetchImpl }
    const view = render(<SolarWorkspaceTools {...props} transport={transport} />)
    openCombiner()
    chooseCombiner()
    localStorage.setItem('leaf.jwt', 'jwt-a')
    await importCombiner()
    localStorage.setItem('leaf.jwt', 'jwt-b')
    // Preserve transport identity while changing the getter; the memoized client must see it.
    view.rerender(<SolarWorkspaceTools {...props} transport={transport} getCheckoutCapability={() => 'cap-b'} />)
    await importCombiner()
    for (const [index, cap, bearer] of [[0, 'cap-a', 'jwt-a'], [1, 'cap-b', 'jwt-b']]) {
      const [url, init] = fetchImpl.mock.calls[index]
      expect(url).toBe(api.config.apiBase.replace(/\/$/, '') + '/api/drawings/solar/imports/combiner-intake')
      expect(init.headers).toMatchObject({ 'Content-Type': 'application/json', 'X-Tenant-Id': api.config.tenant,
        Authorization: `Bearer ${bearer}`, 'X-Checkout-Capability': cap })
    }
    for (const cap of ['', null, 42]) {
      view.rerender(<SolarWorkspaceTools {...props} transport={transport} getCheckoutCapability={() => cap} />)
      const count = fetchImpl.mock.calls.length
      fireEvent.click(combinerImport())
      await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(count + 1))
      await waitFor(() => expect(combinerImport().disabled).toBe(false))
      expect(fetchImpl.mock.calls[count][1].headers).not.toHaveProperty('X-Checkout-Capability')
    }
  })

  it('W20-07b B4 reports unauthorized with the sent bearer and never stores or stages', async () => {
    const refused = new Response(UNAUTH_TEXT, { status: 401, headers: { 'content-type': 'application/json' } })
    const fetchImpl = vi.fn(async () => refused)
    const note = vi.spyOn(api, 'noteUnauthorized')
    const props = combinerProps({ transport: undefined })
    render(<SolarWorkspaceTools {...props} transport={{ fetchImpl }} />)
    localStorage.setItem('leaf.jwt', 'combiner-old')
    openCombiner()
    chooseCombiner()
    fireEvent.click(combinerImport())
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe(COMBINER_INTAKE_REASONS.UNAUTHENTICATED))
    expect(note).toHaveBeenCalledWith(refused, fetchImpl.mock.calls[0][0], 'Bearer combiner-old')
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    expect(props.onRunPlacement).not.toHaveBeenCalled()
  })

  it('W20-07b B5 checkout and busy disable import with busy taking precedence', () => {
    const props = combinerProps()
    const view = render(<SolarWorkspaceTools {...props} />)
    openCombiner()
    chooseCombiner()
    view.rerender(<SolarWorkspaceTools {...props} checkoutHeld={false} />)
    expect(combinerReason().textContent).toBe('Take the drawing checkout before importing the combiner intake')
    expect(combinerFile().disabled).toBe(true)
    expect(document.querySelector('fieldset').disabled).toBe(true)
    fireEvent.click(combinerImport())
    view.rerender(<SolarWorkspaceTools {...props} checkoutHeld={false} busy={true} />)
    expect(combinerReason().textContent).toBe('A run is in progress, so wait for it to finish')
    fireEvent.click(combinerImport())
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
    view.rerender(<SolarWorkspaceTools {...props} />)
    expect(combinerReason()).toBeNull()
    expect(combinerImport().disabled).toBe(false)
    expect(document.querySelector('fieldset').disabled).toBe(false)
  })

  it('W20-07b B6 waits for the version prop even after the refresh promise settles', async () => {
    const pending = workspaceDeferred()
    const props = combinerProps({ onDrawingVersionChanged: vi.fn(() => pending.promise) })
    const view = render(<SolarWorkspaceTools {...props} />)
    openCombiner(); chooseCombiner(); combinerHardware()
    await importCombiner()
    expect(props.onDrawingVersionChanged).toHaveBeenCalledTimes(1)
    expect(props.onDrawingVersionChanged).toHaveBeenCalledWith({ drawing_id: 'solar', version: 2, parent: 1 },
      undefined, { isCurrent: expect.any(Function), announce: true })
    expect(props.onDrawingVersionChanged.mock.calls[0][2].isCurrent()).toBe(true)
    expect(combinerReason().textContent).toBe('The drawing is refreshing to the imported version')
    expect(combinerRun().disabled).toBe(true)
    await act(async () => { pending.resolve(true) })
    expect(combinerRun().disabled).toBe(true)
    expect(combinerReason().textContent).toBe('The drawing is refreshing to the imported version')
    view.rerender(<SolarWorkspaceTools {...props} drawingVersion={2} />)
    expect(combinerReason()).toBeNull()
    expect(combinerRun().disabled).toBe(false)
    expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
  })

  it('W20-07b B7 existing intake seats only a behind browser and suppresses the creation announcement', async () => {
    const props = combinerProps({ drawingVersion: 2 })
    props.transport.fetchImpl.mockImplementation(async () => combinerResponse({ created: false }))
    const view = render(<SolarWorkspaceTools {...props} />)
    openCombiner(); chooseCombiner(); combinerHardware()
    await importCombiner()
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    expect(combinerRun().disabled).toBe(false)
    view.unmount()
    render(<SolarWorkspaceTools {...props} drawingVersion={1} />)
    openCombiner(); chooseCombiner()
    await importCombiner()
    expect(props.onDrawingVersionChanged).toHaveBeenCalledTimes(1)
    expect(props.onDrawingVersionChanged).toHaveBeenCalledWith({ drawing_id: 'solar', version: 2, parent: 1 },
      undefined, { isCurrent: expect.any(Function), announce: false })
  })

  it('W20-07b B8 forwards imported graph revision and numeric hardware only when valid', async () => {
    const props = combinerProps({ drawingVersion: 2 })
    render(<SolarWorkspaceTools {...props} />)
    openCombiner(); chooseCombiner(); combinerHardware()
    await importCombiner()
    fireEvent.click(combinerRun())
    expect(props.onRunPlacement).toHaveBeenCalledTimes(1)
    expect(props.onRunPlacement).toHaveBeenCalledWith({ expected_rev: 7,
      hardware: { model: 'Combiner', max_dc_voltage: 480, max_ac_power_kw: 10 } })
    fireEvent.change(screen.getByLabelText('DC voltage'), { target: { value: '0' } })
    fireEvent.click(combinerRun())
    expect(props.onRunPlacement).toHaveBeenCalledTimes(1)
    combinerHardware()
    props.onRunPlacement.mockReturnValueOnce(false)
    fireEvent.click(combinerRun())
    expect(combinerReason().textContent).toBe('Combiner placement was not staged')
    expect(combinerRun().disabled).toBe(false)
    fireEvent.click(combinerRun())
    expect(combinerReason()).toBeNull()
    props.onRunPlacement.mockReturnValueOnce(false)
    fireEvent.click(combinerRun())
    expect(combinerReason().textContent).toBe('Combiner placement was not staged')
    props.transport.fetchImpl.mockImplementationOnce(async () => new Response(UNAUTH_TEXT, { status: 401 }))
    fireEvent.click(combinerImport())
    expect(combinerReason()).toBeNull()
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe(COMBINER_INTAKE_REASONS.UNAUTHENTICATED))
  })

  it('W20-07b B9 retains file and hardware through a retryable refusal then imports', async () => {
    const props = combinerProps()
    props.transport.fetchImpl.mockImplementationOnce(async () => new Response(JSON.stringify({
      error: { error_code: 'INTERNAL', reason_code: 'COMBINER_IMPORT_STORE_UNAVAILABLE', retryable: true },
    }), { status: 503, headers: { 'content-type': 'application/json' } }))
    render(<SolarWorkspaceTools {...props} />)
    openCombiner()
    const file = chooseCombiner()
    combinerHardware()
    fireEvent.click(combinerImport())
    await waitFor(() => expect(screen.getByRole('status').textContent)
      .toBe(COMBINER_INTAKE_REASONS.COMBINER_IMPORT_STORE_UNAVAILABLE + '. This import can be retried.'))
    expect(combinerFile().files[0]).toBe(file)
    expect(screen.getByLabelText('Model').value).toBe('Combiner')
    expect(screen.getByLabelText('DC voltage').value).toBe('480')
    expect(screen.getByLabelText('AC power').value).toBe('10')
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    await importCombiner()
    expect(props.transport.fetchImpl).toHaveBeenCalledTimes(2)
    expect(props.onDrawingVersionChanged).toHaveBeenCalledTimes(1)
    expect(combinerFile().files[0]).toBe(file)
    expect(screen.getByLabelText('Model').value).toBe('Combiner')
  })

  it('W20-07b B10 aborts and drops old scopes including A to B to A and project changes', async () => {
    for (const next of [{ drawingId: 'other' }, { projectId: 'project' }]) {
      const pending = workspaceDeferred()
      const props = combinerProps()
      const view = render(<SolarWorkspaceTools {...props} />)
      openCombiner(); chooseCombiner()
      await importCombiner()
      const isCurrent = props.onDrawingVersionChanged.mock.calls[0][2].isCurrent
      view.rerender(<SolarWorkspaceTools {...props} {...next} />)
      expect(isCurrent()).toBe(false)
      view.rerender(<SolarWorkspaceTools {...props} />)
      props.onDrawingVersionChanged.mockClear()
      props.transport.fetchImpl.mockImplementationOnce(() => pending.promise)
      openCombiner(); chooseCombiner(); combinerHardware()
      fireEvent.click(combinerImport())
      await waitFor(() => expect(props.transport.fetchImpl).toHaveBeenCalledTimes(2))
      const signal = props.transport.fetchImpl.mock.calls[1][1].signal
      view.rerender(<SolarWorkspaceTools {...props} {...next} />)
      expect(signal.aborted).toBe(true)
      view.rerender(<SolarWorkspaceTools {...props} />)
      await act(async () => { pending.resolve(combinerResponse()) })
      expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
      openCombiner()
      expect(screen.getByText('Import the combiner intake for this drawing first')).toBeTruthy()
      expect(combinerRun().disabled).toBe(true)
      fireEvent.click(combinerRun())
      expect(props.onRunPlacement).not.toHaveBeenCalled()
      view.unmount()
    }
  })

  it('W20-07b B11 fails on false or rejection and retries the same version with fresh guards', async () => {
    const props = combinerProps({ onDrawingVersionChanged: vi.fn()
      .mockResolvedValueOnce(false).mockRejectedValueOnce(new Error('read failed')).mockResolvedValueOnce(true) })
    const view = render(<SolarWorkspaceTools {...props} />)
    openCombiner(); chooseCombiner(); combinerHardware()
    await importCombiner()
    await waitFor(() => expect(combinerReason().textContent).toBe('The drawing did not refresh to the imported version'))
    expect(combinerRun().disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh drawing' }))
    expect(combinerReason().textContent).toBe('The drawing is refreshing to the imported version')
    await waitFor(() => expect(combinerReason().textContent).toBe('The drawing did not refresh to the imported version'))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh drawing' }))
    expect(combinerReason().textContent).toBe('The drawing is refreshing to the imported version')
    expect(props.onDrawingVersionChanged).toHaveBeenCalledTimes(3)
    const calls = props.onDrawingVersionChanged.mock.calls
    for (const call of calls) {
      expect(call[0]).toEqual({ drawing_id: 'solar', version: 2, parent: 1 })
      expect(call[1]).toBeUndefined()
      expect(call[2].announce).toBe(true)
    }
    expect(calls[0][2].isCurrent).not.toBe(calls[1][2].isCurrent)
    view.rerender(<SolarWorkspaceTools {...props} drawingVersion={2} />)
    expect(combinerReason()).toBeNull()
    expect(combinerRun().disabled).toBe(false)
  })

  it('W20-07b B12 remounts intake after idle movement or a refreshing version is overtaken', async () => {
    for (const drawingVersion of [2, 1]) {
      const props = combinerProps({ drawingVersion })
      const view = render(<SolarWorkspaceTools {...props} />)
      openCombiner(); chooseCombiner(); combinerHardware()
      await importCombiner()
      view.rerender(<SolarWorkspaceTools {...props} drawingVersion={3} />)
      expect(screen.getByText('Import the combiner intake for this drawing first')).toBeTruthy()
      expect(combinerRun().disabled).toBe(true)
      expect(combinerReason()).toBeNull()
      expect(screen.getByLabelText('Model').value).toBe('')
      fireEvent.click(combinerRun())
      expect(props.onRunPlacement).not.toHaveBeenCalled()
      view.unmount()
    }
  })
})

describe('SolarWorkspaceTools', () => {
  it('W1 ground physical opens the terrain control and focuses its file input', () => {
    render(<SolarWorkspaceTools {...supplied()} />)
    expect(control()).toBeNull()
    open()
    expect(control()).not.toBeNull()
    expect(document.activeElement).toBe(screen.getByLabelText('LandXML file'))
    expect(trigger().getAttribute('aria-expanded')).toBe('true')
  })

  it('W2 other flows expose no LandXML action and never fetch', () => {
    const props = supplied()
    const view = render(<SolarWorkspaceTools {...props} flow="rooftop" />)
    for (const flow of ['rooftop', 'ground-electrical', 'solaredge-import', 'pvcase-tutorial']) {
      view.rerender(<SolarWorkspaceTools {...props} flow={flow} />)
      expect(screen.queryByRole('button', { name: 'Import LandXML terrain' })).toBeNull()
      expect(control()).toBeNull()
    }
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
  })

  it('W3 without a drawing there is no actionable control or request', () => {
    const props = supplied({ drawingId: null })
    render(<SolarWorkspaceTools {...props} />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(control()).toBeNull()
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
  })

  it('W4 default transport reads tenant and bearer headers afresh for each upload', async () => {
    const props = supplied({ transport: undefined })
    render(<SolarWorkspaceTools {...props} />)
    // Install fetch after mounting: the default transport must read it at request time.
    const fetchImpl = vi.fn(async () => response())
    vi.stubGlobal('fetch', fetchImpl)
    localStorage.setItem('leaf.jwt', 'jwt-a')
    open()
    submit()
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url.startsWith(api.config.apiBase.replace(/\/$/, '') + '/api/drawings/solar/imports/landxml?')).toBe(true)
    expect(init.method).toBe('POST')
    expect(init.headers['X-Tenant-Id']).toBe(api.config.tenant)
    expect(init.headers.Authorization).toBe('Bearer jwt-a')
    expect(init.headers['Content-Type']).toBe('application/xml')
    expect(Object.keys(init.headers).some((name) => /checkout|guest/i.test(name))).toBe(false)

    localStorage.setItem('leaf.jwt', 'jwt-b')
    fireEvent.click(screen.getByRole('button', { name: 'Import terrain' }))
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(2))
    expect(fetchImpl.mock.calls[1][1].headers.Authorization).toBe('Bearer jwt-b')
    expect(fetchImpl.mock.calls[1][1].headers['X-Tenant-Id']).toBe(api.config.tenant)
  })

  it('W5 default transport reports a 401 with the bearer actually sent', async () => {
    const refused = new Response(UNAUTH_TEXT, { status: 401, headers: { 'content-type': 'application/json' } })
    const fetchImpl = vi.fn(async () => refused)
    vi.stubGlobal('fetch', fetchImpl)
    const note = vi.spyOn(api, 'noteUnauthorized')
    localStorage.setItem('leaf.jwt', 'jwt-old')
    const props = supplied({ transport: undefined })
    render(<SolarWorkspaceTools {...props} />)
    open()
    submit()
    await waitFor(() => expect(note).toHaveBeenCalledTimes(1))
    expect(note).toHaveBeenCalledWith(refused, fetchImpl.mock.calls[0][0], 'Bearer jwt-old')
    await waitFor(() => expect(screen.getByTestId('solar-landxml-refusal').textContent).toBe('Sign in again to import a LandXML file'))
    expect(localStorage.getItem('leaf.jwt')).toBeNull()
    expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
  })

  it('W6 a created terrain stores its head, announces it and calls only the physical callback once', async () => {
    const props = supplied()
    render(<SolarWorkspaceTools {...props} />)
    open()
    submit()
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    expect(props.onPhysicalHeadChanged).toHaveBeenCalledWith(value())
    expect(announcement()).toBe(CREATED)
    expect(document.querySelector('.solar-workspace-announce').getAttribute('aria-live')).toBe('polite')
    expect(screen.getByRole('region', { name: 'Solar workspace tools' }).getAttribute('data-physical-head-index')).toBe('0')
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
    expect(props.getCheckoutCapability).not.toHaveBeenCalled()
  })

  it('W7 an existing terrain announces nothing changed and still calls the physical callback once', async () => {
    const props = supplied()
    props.transport.fetchImpl.mockImplementation(async () => response(false))
    render(<SolarWorkspaceTools {...props} />)
    open()
    submit()
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    expect(props.onPhysicalHeadChanged).toHaveBeenCalledWith(value(false))
    expect(announcement()).toBe(EXISTING)
    expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
  })

  it('W8 scope changes close the panel, clear terrain state and drop a late result even after returning', async () => {
    for (const nextScope of [{ drawingId: 'other' }, { projectId: 'other-project' }]) {
      let resolve
      const pending = new Promise((done) => { resolve = done })
      const props = supplied()
      props.transport.fetchImpl.mockImplementation(() => pending)
      const view = render(<SolarWorkspaceTools {...props} />)
      open()
      submit()
      await waitFor(() => expect(props.transport.fetchImpl).toHaveBeenCalledTimes(1))
      const signal = props.transport.fetchImpl.mock.calls[0][1].signal
      view.rerender(<SolarWorkspaceTools {...props} {...nextScope} />)
      expect(control()).toBeNull()
      expect(announcement()).toBe('')
      expect(signal.aborted).toBe(true)
      view.rerender(<SolarWorkspaceTools {...props} />)
      await act(async () => { resolve(response()) })
      expect(props.onPhysicalHeadChanged).not.toHaveBeenCalled()
      expect(props.onDrawingVersionChanged).not.toHaveBeenCalled()
      expect(announcement()).toBe('')
      expect(control()).toBeNull()

      // A completed import's stored head and announcement also reset on either scope change.
      props.transport.fetchImpl.mockImplementation(async () => response())
      open()
      submit()
      await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
      expect(announcement()).toBe(CREATED)
      view.rerender(<SolarWorkspaceTools {...props} {...nextScope} />)
      expect(announcement()).toBe('')
      expect(control()).toBeNull()
      expect(screen.getByRole('region', { name: 'Solar workspace tools' }).hasAttribute('data-physical-head-index')).toBe(false)
      view.unmount()
    }
  })

  it('W9 Close removes the control and returns focus to the trigger', () => {
    render(<SolarWorkspaceTools {...supplied()} />)
    open()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(control()).toBeNull()
    expect(document.activeElement).toBe(trigger())
    expect(trigger().getAttribute('aria-expanded')).toBe('false')
  })

  it('W10 the upload control supplies checkout and busy disabled reasons', () => {
    const props = supplied({ checkoutHeld: false })
    const view = render(<SolarWorkspaceTools {...props} />)
    open()
    expect(screen.getByTestId('solar-landxml-reason').textContent).toBe('Take the drawing checkout before importing terrain')
    expect(screen.getByRole('button', { name: 'Import terrain' }).disabled).toBe(true)
    view.rerender(<SolarWorkspaceTools {...props} checkoutHeld={true} busy={true} />)
    expect(screen.getByTestId('solar-landxml-reason').textContent).toBe('A run is in progress, so wait for it to finish')
    expect(screen.getByRole('button', { name: 'Import terrain' }).disabled).toBe(true)
    expect(props.transport.fetchImpl).not.toHaveBeenCalled()
  })
})
