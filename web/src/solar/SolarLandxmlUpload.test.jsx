// sf-w4-landxml-upload-control: the LandXML terrain upload control, unmounted. The end to end rows
// drive the real upload client (solarLandxmlClient.js) with an injected fetch that serves bodies
// the real route answered (planner measurement on Forge main; FIRST to UNAUTH are the client
// test's own fixtures, TOO_FEW was captured on 3293e8cd).
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LANDXML_IMPORT_REASONS, LANDXML_MAX_BYTES, createSolarLandxmlClient } from './solarLandxmlClient.js'
import SolarLandxmlUpload from './SolarLandxmlUpload.jsx'

const FIRST_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","media_type":"application/json","filename":"physical-state.json","byte_length":18283,"content_sha256":"0de2afeb7ae692538ce33fd011b043438cd65cc426fd1cead1933eb4046d22f0","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8"}},"error":null,"degraded_mode":false}`
const WITH_PROJECT_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":2,"parent":"3a16bacbf6d90a1f6d01345174a40f86f2a07165d85a98c61f53a791334e044f","state":{"artifact_id":"0e63a1bceb0d338bd7a550bda337670d5af1d5dbec9f7242009249b425bc2eb4","media_type":"application/json","filename":"physical-state.json","byte_length":18345,"content_sha256":"1ca5790746b6b63f5eb5399fd75f37507361a1eb0e3479c3666ac526400bcaab","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/0e63a1bceb0d338bd7a550bda337670d5af1d5dbec9f7242009249b425bc2eb4"}},"error":null,"degraded_mode":false}`
const UNSAFE_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"LANDXML_UNSAFE","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"LANDXML_UNSAFE"},"degraded_mode":false}`
const DRAINED_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"INTERNAL","message":"LANDXML_WRITES_DRAINED","retryable":true,"retry_class":"backoff","actor":"service","next_action":"Wait a short time, then retry the request.","reason_code":"LANDXML_WRITES_DRAINED"},"degraded_mode":false}`
const CONFLICT_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"PHYSICAL_HEAD_CONFLICT","retryable":true,"retry_class":"backoff","actor":"service","next_action":"Wait a short time, then retry the request.","reason_code":"PHYSICAL_HEAD_CONFLICT"},"degraded_mode":false}`
const UNITS_MISMATCH_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"LANDXML_UNITS_MISMATCH","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"LANDXML_UNITS_MISMATCH"},"degraded_mode":false}`
const GUEST_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"FORBIDDEN","message":"guest sessions are upload-only: upload, upload-status, intake and versions reads; create an account for everything else","retryable":false,"retry_class":"after_action","actor":"workspace_admin","next_action":"Ask a workspace admin to grant the required access."},"degraded_mode":false}`
const ENTITLEMENT_DENIED_TEXT = `{"entitlement_required":true,"required":"upload","tier":"demo","error":{"error_code":"ENTITLEMENT_REQUIRED","message":"the 'demo' plan does not include uploading drawings; upgrade the workspace plan to enable uploads.","retryable":false,"retry_class":"after_action","actor":"workspace_admin","next_action":"Enable this capability for the workspace, then retry."},"degraded_mode":false}`
const UNAUTH_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"UNAUTHENTICATED","message":"missing bearer token (Authorization header)","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Sign in, then repeat the request."},"degraded_mode":false}`
const TOO_FEW_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"BAD_PARAMS","message":"LANDXML_TOO_FEW_POINTS","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Review the inputs, correct them, and submit again.","reason_code":"LANDXML_TOO_FEW_POINTS"},"degraded_mode":false}`

const PROJECT = 'leaf:project:00000000-0000-4000-8000-000000000001'
const CAPTURE_BYTES = 45_305
const FIRST_LINES = [
  ['Terrain', 'Stored as terrain change 1 of this drawing'], ['Survey points', '441 read'],
  ['Terrain grid', '30 by 30 nodes'], ['Extent', 'X -100 to 100, Y -100 to 100 meters'],
  ['File units', 'Meters'], ['Coordinate system', 'None'], ['Elevation datum', 'Not recorded'],
]
const UNITS_REASON = 'Choose meters or feet as the drawing units before importing'
const FILE_REASON = 'Choose a LandXML file to import'
const CANCELLED = 'The import was cancelled here, but the drawing may already hold this terrain'
const INVALID = 'The server answer could not be read, so the step stopped'

const xmlFile = (bytes = CAPTURE_BYTES) => new File([new Uint8Array(bytes)], 'site.xml', { type: 'application/xml' })
const withoutEnvelope = (text) => {
  const copy = JSON.parse(text)
  delete copy.error
  delete copy.degraded_mode
  return copy
}
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
// The real client, its fetch answering one measured body.
function realUpload(text, status = 200) {
  const fetchImpl = vi.fn(async () => new Response(text, { status, headers: { 'content-type': 'application/json' } }))
  const client = createSolarLandxmlClient({ fetchImpl, apiBase: 'https://studio.test', headers: () => ({ Authorization: 'Bearer t' }) })
  return { upload: vi.fn((options) => client.uploadLandxml(options)), fetchImpl }
}
function props(overrides = {}) {
  return {
    drawingId: 'solar', projectId: null, upload: vi.fn(() => new Promise(() => {})), checkoutHeld: true, busy: false,
    onImported: vi.fn(), onClose: vi.fn(), ...overrides,
  }
}
const control = () => screen.getByTestId('solar-landxml-upload')
const reasonText = () => screen.queryByTestId('solar-landxml-reason')?.textContent ?? null
const refusalText = () => screen.queryByTestId('solar-landxml-refusal')?.textContent ?? null
const importButton = () => screen.getByRole('button', { name: 'Import terrain' })
const chooseFile = (file) => fireEvent.change(screen.getByLabelText('LandXML file'), { target: { files: file ? [file] : [] } })
const chooseUnits = (value) => fireEvent.change(screen.getByLabelText('Drawing units'), { target: { value } })
const typeCrs = (value) => fireEvent.change(screen.getByLabelText('Coordinate system'), { target: { value } })
const typeCells = (value) => fireEvent.change(screen.getByLabelText('Grid size'), { target: { value } })
function summaryRows() {
  const list = screen.queryByTestId('solar-landxml-summary')
  if (!list) return null
  return [...list.querySelectorAll('div')].map((row) => [row.querySelector('dt').textContent, row.querySelector('dd').textContent])
}
function ready(file = xmlFile()) {
  chooseUnits('m')
  chooseFile(file)
}

afterEach(cleanup)

describe('SolarLandxmlUpload', () => {
  it('UC1 opens with every field, the hints and Import off until units are chosen', () => {
    const supplied = props()
    render(<SolarLandxmlUpload {...supplied} />)
    expect(control().getAttribute('aria-label')).toBe('LandXML terrain import')
    expect(control().getAttribute('data-phase')).toBe('idle')
    const file = screen.getByLabelText('LandXML file')
    expect(file.getAttribute('type')).toBe('file')
    expect(file.getAttribute('accept')).toBe('.xml,application/xml,text/xml')
    const units = screen.getByLabelText('Drawing units')
    expect([...units.options].map((option) => [option.value, option.textContent])).toEqual([['', 'Choose'], ['m', 'Meters'], ['ft', 'Feet']])
    expect(units.value).toBe('')
    expect(screen.getByLabelText('Coordinate system').value).toBe('')
    expect(screen.getByLabelText('Grid size').value).toBe('30')
    expect(screen.getByText('Leave blank for none, or enter an EPSG code such as EPSG:2229')).toBeTruthy()
    expect(screen.getByText('Nodes along the longer side, 2 to 200')).toBeTruthy()
    expect(reasonText()).toBe(UNITS_REASON)
    expect(importButton().disabled).toBe(true)
    expect(refusalText()).toBeNull()
    expect(summaryRows()).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel import' })).toBeNull()
    expect(screen.getByLabelText('Coordinate system').getAttribute('aria-invalid')).toBeNull()
    expect(screen.getByLabelText('Grid size').getAttribute('aria-invalid')).toBeNull()
  })

  it('UC2 units then a file turn Import on', () => {
    render(<SolarLandxmlUpload {...props()} />)
    chooseUnits('m')
    expect(reasonText()).toBe(FILE_REASON)
    expect(importButton().disabled).toBe(true)
    chooseFile(xmlFile())
    expect(reasonText()).toBeNull()
    expect(importButton().disabled).toBe(false)
    chooseFile(null)
    expect(reasonText()).toBe(FILE_REASON)
  })

  it('UC3 Import sends the request once and locks the control while it runs', async () => {
    const supplied = props()
    render(<SolarLandxmlUpload {...supplied} />)
    const file = xmlFile(397)
    chooseUnits('ft')
    chooseFile(file)
    typeCrs(' EPSG:2229 ')
    typeCells('7')
    fireEvent.click(importButton())
    fireEvent.click(importButton())
    fireEvent.submit(importButton().closest('form'))
    await waitFor(() => expect(supplied.upload).toHaveBeenCalledTimes(1))
    const [options] = supplied.upload.mock.calls[0]
    expect(Object.keys(options)).toEqual(['drawingId', 'file', 'drawingUnits', 'crs', 'targetCells', 'projectId', 'signal'])
    expect(options.file).toBe(file)
    expect({ ...options, file: null, signal: null }).toEqual({
      drawingId: 'solar', file: null, drawingUnits: 'ft', crs: 'EPSG:2229', targetCells: 7, projectId: null, signal: null,
    })
    expect(options.signal.aborted).toBe(false)
    expect(control().getAttribute('data-phase')).toBe('uploading')
    expect(reasonText()).toBe('The LandXML file is being imported')
    expect(importButton().disabled).toBe(true)
    for (const label of ['LandXML file', 'Drawing units', 'Coordinate system', 'Grid size']) {
      expect(screen.getByLabelText(label).disabled).toBe(true)
    }
    expect(screen.getByRole('button', { name: 'Cancel import' })).toBeTruthy()
    fireEvent.submit(importButton().closest('form'))
    await Promise.resolve()
    expect(supplied.upload).toHaveBeenCalledTimes(1)
  })

  it('UC4 a stored terrain shows its summary from the real client and the measured answer', async () => {
    const { upload, fetchImpl } = realUpload(FIRST_TEXT)
    const supplied = props({ upload })
    render(<SolarLandxmlUpload {...supplied} />)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(summaryRows()).toEqual(FIRST_LINES))
    expect(screen.getByTestId('solar-landxml-summary').getAttribute('aria-label')).toBe('Imported terrain')
    expect(control().getAttribute('data-phase')).toBe('imported')
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toBe('https://studio.test/api/drawings/solar/imports/landxml?drawing_units=m&crs=none&target_cells=30')
    expect(supplied.onImported).toHaveBeenCalledTimes(1)
    expect(supplied.onImported.mock.calls[0][0]).toEqual(withoutEnvelope(FIRST_TEXT))
    expect(refusalText()).toBeNull()
    expect(reasonText()).toBeNull()
    expect(importButton().disabled).toBe(false)
    expect(screen.queryByRole('button', { name: 'Cancel import' })).toBeNull()
  })

  it('UC5 the project prop reaches the request and the stored terrain names its change', async () => {
    const { upload, fetchImpl } = realUpload(WITH_PROJECT_TEXT)
    render(<SolarLandxmlUpload {...props({ upload, projectId: PROJECT })} />)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(summaryRows()?.[0]).toEqual(['Terrain', 'Stored as terrain change 3 of this drawing']))
    expect(fetchImpl.mock.calls[0][0]).toBe(
      `https://studio.test/api/drawings/solar/imports/landxml?drawing_units=m&crs=none&target_cells=30&project_id=${encodeURIComponent(PROJECT)}`)
  })

  it.each([
    ['UNSAFE', UNSAFE_TEXT, 400, 'LANDXML_UNSAFE'],
    ['DRAINED', DRAINED_TEXT, 503, 'LANDXML_WRITES_DRAINED'],
    ['CONFLICT', CONFLICT_TEXT, 409, 'PHYSICAL_HEAD_CONFLICT'],
    ['UNITS_MISMATCH', UNITS_MISMATCH_TEXT, 409, 'LANDXML_UNITS_MISMATCH'],
    ['TOO_FEW', TOO_FEW_TEXT, 400, 'LANDXML_TOO_FEW_POINTS'],
    ['GUEST', GUEST_TEXT, 403, 'FORBIDDEN'],
    ['ENTITLEMENT_DENIED', ENTITLEMENT_DENIED_TEXT, 403, 'ENTITLEMENT_REQUIRED'],
    ['UNAUTH', UNAUTH_TEXT, 401, 'UNAUTHENTICATED'],
  ])('UC6 the measured %s refusal shows its sentence and Import stays available', async (name, text, status, code) => {
    const { upload, fetchImpl } = realUpload(text, status)
    const supplied = props({ upload })
    render(<SolarLandxmlUpload {...supplied} />)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(refusalText()).toBe(LANDXML_IMPORT_REASONS[code]))
    expect(screen.getByTestId('solar-landxml-refusal').getAttribute('role')).toBe('alert')
    expect(control().getAttribute('data-phase')).toBe('refused')
    expect(summaryRows()).toBeNull()
    expect(supplied.onImported).not.toHaveBeenCalled()
    expect(importButton().disabled).toBe(false)
    fireEvent.click(importButton())
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(2))
  })

  it.each([
    ['an empty file', () => chooseFile(xmlFile(0)), 'The chosen file is empty, so choose the LandXML file again', null],
    ['a file over 16 MiB', () => chooseFile(xmlFile(LANDXML_MAX_BYTES + 1)), 'The file is larger than 16 MiB, more than an import accepts', null],
    ['a lower case EPSG code', () => typeCrs('epsg:4326'), 'Choose no coordinate system or an EPSG code such as EPSG:2229', 'Coordinate system'],
    ['a grid of 201', () => typeCells('201'), 'Choose a grid size from 2 to 200 cells', 'Grid size'],
    ['a grid of 1', () => typeCells('1'), 'Choose a grid size from 2 to 200 cells', 'Grid size'],
  ])('UC7 %s is refused before anything is sent', async (name, act1, sentence, invalidLabel) => {
    const supplied = props()
    render(<SolarLandxmlUpload {...supplied} />)
    ready()
    act1()
    expect(reasonText()).toBe(sentence)
    expect(importButton().disabled).toBe(true)
    if (invalidLabel) expect(screen.getByLabelText(invalidLabel).getAttribute('aria-invalid')).toBe('true')
    fireEvent.submit(importButton().closest('form'))
    await Promise.resolve()
    expect(supplied.upload).not.toHaveBeenCalled()
  })

  it('UC8 a corrected draft turns Import back on', () => {
    render(<SolarLandxmlUpload {...props()} />)
    ready()
    typeCrs('epsg:4326')
    typeCrs('EPSG:4326')
    expect(screen.getByLabelText('Coordinate system').getAttribute('aria-invalid')).toBeNull()
    typeCells('abc')
    typeCells('200')
    expect(screen.getByLabelText('Grid size').getAttribute('aria-invalid')).toBeNull()
    expect(reasonText()).toBeNull()
    expect(importButton().disabled).toBe(false)
  })

  it.each([[false], [undefined], ['true'], [1]])('UC9 a checkout state of %j keeps Import off', async (held) => {
    const supplied = props({ checkoutHeld: held })
    const { rerender } = render(<SolarLandxmlUpload {...supplied} />)
    ready()
    expect(reasonText()).toBe('Take the drawing checkout before importing terrain')
    expect(importButton().disabled).toBe(true)
    fireEvent.submit(importButton().closest('form'))
    await Promise.resolve()
    expect(supplied.upload).not.toHaveBeenCalled()
    rerender(<SolarLandxmlUpload {...supplied} checkoutHeld />)
    expect(reasonText()).toBeNull()
    expect(importButton().disabled).toBe(false)
  })

  it('UC10 a run in progress comes before the checkout and keeps Import off', async () => {
    const supplied = props({ busy: true, checkoutHeld: false })
    const { rerender } = render(<SolarLandxmlUpload {...supplied} />)
    ready()
    expect(reasonText()).toBe('A run is in progress, so wait for it to finish')
    fireEvent.submit(importButton().closest('form'))
    rerender(<SolarLandxmlUpload {...supplied} checkoutHeld />)
    expect(reasonText()).toBe('A run is in progress, so wait for it to finish')
    expect(importButton().disabled).toBe(true)
    await Promise.resolve()
    expect(supplied.upload).not.toHaveBeenCalled()
  })

  it.each([[undefined], [null], ['upload'], [{}]])('UC11 an upload prop of %j keeps Import off', (upload) => {
    render(<SolarLandxmlUpload {...props({ upload })} />)
    ready()
    expect(reasonText()).toBe('This LandXML step was given input it cannot send')
    expect(importButton().disabled).toBe(true)
  })

  it.each([
    ['a success with no readable result', () => Promise.resolve({ ok: true, status: 200, value: {} })],
    ['a rejected upload', () => Promise.reject(new Error('boom'))],
    ['an upload that throws', () => { throw new Error('boom') }],
    ['an uncoded refusal', () => Promise.resolve({ ok: false, status: 400 })],
  ])('UC12 %s shows RESPONSE_INVALID, never a summary', async (name, upload) => {
    const supplied = props({ upload: vi.fn(upload) })
    render(<SolarLandxmlUpload {...supplied} />)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(refusalText()).toBe(INVALID))
    expect(summaryRows()).toBeNull()
    expect(supplied.onImported).not.toHaveBeenCalled()
    expect(importButton().disabled).toBe(false)
  })

  it('UC13 Cancel import settles the control at once and a late answer is ignored', async () => {
    const late = deferred()
    const supplied = props({ upload: vi.fn(() => late.promise) })
    render(<SolarLandxmlUpload {...supplied} />)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(supplied.upload).toHaveBeenCalledTimes(1))
    const { signal } = supplied.upload.mock.calls[0][0]
    fireEvent.click(screen.getByRole('button', { name: 'Cancel import' }))
    expect(signal.aborted).toBe(true)
    expect(refusalText()).toBe(CANCELLED)
    expect(control().getAttribute('data-phase')).toBe('refused')
    expect(importButton().disabled).toBe(false)
    await act(async () => {
      late.resolve({ ok: true, status: 200, value: withoutEnvelope(FIRST_TEXT) })
      await late.promise
    })
    expect(summaryRows()).toBeNull()
    expect(refusalText()).toBe(CANCELLED)
    expect(supplied.onImported).not.toHaveBeenCalled()
  })

  it('UC14 the real client answers a cancel as cancelled, not as a stored terrain', async () => {
    const fetchImpl = vi.fn((url, init) => new Promise((resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
    }))
    const client = createSolarLandxmlClient({ fetchImpl, headers: () => ({}) })
    const supplied = props({ upload: vi.fn((options) => client.uploadLandxml(options)) })
    render(<SolarLandxmlUpload {...supplied} />)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(fetchImpl).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel import' }))
    expect(refusalText()).toBe(CANCELLED)
    await expect(supplied.upload.mock.results[0].value).resolves.toEqual(
      { ok: false, status: null, code: 'LANDXML_CLIENT_ABORTED', retryable: false })
    expect(refusalText()).toBe(CANCELLED)
  })

  it('UC15 unmounting during an upload aborts it and a late answer calls nothing', async () => {
    const late = deferred()
    const supplied = props({ upload: vi.fn(() => late.promise) })
    const { unmount } = render(<SolarLandxmlUpload {...supplied} />)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(supplied.upload).toHaveBeenCalledTimes(1))
    const { signal } = supplied.upload.mock.calls[0][0]
    unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => {
      late.resolve({ ok: true, status: 200, value: withoutEnvelope(FIRST_TEXT) })
      await late.promise
    })
    expect(supplied.onImported).not.toHaveBeenCalled()
    expect(supplied.onClose).not.toHaveBeenCalled()
  })

  it('UC16 another drawing starts a fresh control and ends the upload of the last one', async () => {
    const late = deferred()
    const supplied = props({ upload: vi.fn(() => late.promise) })
    const { rerender } = render(<SolarLandxmlUpload {...supplied} />)
    ready()
    typeCells('12')
    fireEvent.click(importButton())
    await waitFor(() => expect(supplied.upload).toHaveBeenCalledTimes(1))
    const { signal } = supplied.upload.mock.calls[0][0]
    rerender(<SolarLandxmlUpload {...supplied} drawingId="other" />)
    expect(signal.aborted).toBe(true)
    expect(control().getAttribute('data-phase')).toBe('idle')
    expect(screen.getByLabelText('Drawing units').value).toBe('')
    expect(screen.getByLabelText('Grid size').value).toBe('30')
    expect(reasonText()).toBe(UNITS_REASON)
    await act(async () => {
      late.resolve({ ok: true, status: 200, value: withoutEnvelope(FIRST_TEXT) })
      await late.promise
    })
    expect(summaryRows()).toBeNull()
    expect(supplied.onImported).not.toHaveBeenCalled()
  })

  it('UC17 another project starts a fresh control too', async () => {
    const { upload } = realUpload(FIRST_TEXT)
    const supplied = props({ upload })
    const { rerender } = render(<SolarLandxmlUpload {...supplied} />)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(summaryRows()).toEqual(FIRST_LINES))
    rerender(<SolarLandxmlUpload {...supplied} projectId={PROJECT} />)
    expect(summaryRows()).toBeNull()
    expect(reasonText()).toBe(UNITS_REASON)
  })

  it('UC18 an edit after an answer clears the answer it no longer describes', async () => {
    const { upload } = realUpload(FIRST_TEXT)
    render(<SolarLandxmlUpload {...props({ upload })} />)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(summaryRows()).toEqual(FIRST_LINES))
    typeCrs('EPSG:2229')
    expect(summaryRows()).toBeNull()
    expect(control().getAttribute('data-phase')).toBe('idle')
    cleanup()
    const refusing = realUpload(UNSAFE_TEXT, 400)
    render(<SolarLandxmlUpload {...props({ upload: refusing.upload })} />)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(refusalText()).toBe(LANDXML_IMPORT_REASONS.LANDXML_UNSAFE))
    typeCells('10')
    expect(refusalText()).toBeNull()
    fireEvent.click(importButton())
    await waitFor(() => expect(refusalText()).toBe(LANDXML_IMPORT_REASONS.LANDXML_UNSAFE))
    chooseUnits('ft')
    expect(refusalText()).toBeNull()
    fireEvent.click(importButton())
    await waitFor(() => expect(refusalText()).toBe(LANDXML_IMPORT_REASONS.LANDXML_UNSAFE))
    chooseFile(xmlFile(10))
    expect(refusalText()).toBeNull()
  })

  it('UC19 a mount whose onImported throws still sees the summary and leaves no unhandled rejection', async () => {
    const unhandled = []
    const record = (reason) => { unhandled.push(reason) }
    process.on('unhandledRejection', record)
    try {
      const { upload } = realUpload(FIRST_TEXT)
      const onImported = vi.fn(() => { throw new Error('mount') })
      render(<SolarLandxmlUpload {...props({ upload, onImported })} />)
      ready()
      fireEvent.click(importButton())
      await waitFor(() => expect(summaryRows()).toEqual(FIRST_LINES))
      expect(onImported).toHaveBeenCalledTimes(1)
      expect(importButton().disabled).toBe(false)
      await new Promise((resolve) => { setTimeout(resolve, 50) })
      expect(unhandled).toEqual([])
    } finally {
      process.off('unhandledRejection', record)
    }
  })

  it('UC20 Close ends an upload in flight, then closes', async () => {
    const supplied = props()
    render(<SolarLandxmlUpload {...supplied} />)
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(supplied.onClose).toHaveBeenCalledTimes(1)
    ready()
    fireEvent.click(importButton())
    await waitFor(() => expect(supplied.upload).toHaveBeenCalledTimes(1))
    const { signal } = supplied.upload.mock.calls[0][0]
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(signal.aborted).toBe(true)
    expect(supplied.onClose).toHaveBeenCalledTimes(2)
    cleanup()
    render(<SolarLandxmlUpload {...props({ onClose: undefined })} />)
    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull()
  })
})
