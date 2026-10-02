import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api.js'
import SolarWorkspaceTools from './SolarWorkspaceTools.jsx'

// The route capture also used by SolarLandxmlUpload.test.jsx; the real client validates it.
const FIRST_TEXT = `{"schema":"leaf.solar-landxml-import.v1","created":true,"drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","source":{"artifact_id":"d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce","media_type":"application/xml","filename":"landxml-source.xml","byte_length":45305,"content_sha256":"9846f488c6780af2185fdf62741865fbc7a75077488449f6741f0aac77f370b5","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/d6923bdcbbc5c7e02b2cfc486ff981d716adc4b7d05e344c04939bb4eef33cce"},"interpretation":{"point_order":"northing-easting-elevation","drawing_x":"easting","drawing_y":"northing","linear_unit":"meter","meters_per_source_unit":1.0,"drawing_units":"m","meters_per_drawing_unit":1.0,"horizontal_scale":1.0,"elevation_scale":1.0,"crs":"none","crs_source":"declared","elevation_datum":"unrecorded"},"points":{"declared":441,"accepted":441,"skipped":0},"grid":{"rows":30,"cols":30,"target_cells":30,"x_min":-100.0,"x_max":100.0,"y_min":-100.0,"y_max":100.0},"head":{"schema":"leaf.solar-physical-head.v1","drawing_id":"solar","project_id":"leaf:project:00000000-0000-4000-8000-000000000001","index":0,"parent":null,"state":{"artifact_id":"56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8","media_type":"application/json","filename":"physical-state.json","byte_length":18283,"content_sha256":"0de2afeb7ae692538ce33fd011b043438cd65cc426fd1cead1933eb4046d22f0","source_version":1,"schema":"leaf.solar-artifact-ref.v1","download":"/api/drawings/solar/artifacts/56f3892ae8228c968f19b0a7f1a3fb65ae49be60d279382fb69e21ed4a4bcbc8"}},"error":null,"degraded_mode":false}`
const UNAUTH_TEXT = `{"ok":false,"tool":null,"version":null,"result":null,"overlay":null,"timing_ms":0,"cost":null,"error":{"error_code":"UNAUTHENTICATED","message":"missing bearer token (Authorization header)","retryable":false,"retry_class":"after_action","actor":"user","next_action":"Sign in, then repeat the request."},"degraded_mode":false}`
const CREATED = 'Terrain imported for this drawing.'
const EXISTING = 'This terrain was already imported, so nothing changed.'

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
    localStorage.setItem('leaf.jwt', 'first-token')
    open()
    submit()
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(1))
    const [url, init] = fetchImpl.mock.calls[0]
    expect(url.startsWith(api.config.apiBase.replace(/\/$/, '') + '/api/drawings/solar/imports/landxml?')).toBe(true)
    expect(init.method).toBe('POST')
    expect(init.headers['X-Tenant-Id']).toBe(api.config.tenant)
    expect(init.headers.Authorization).toBe('Bearer first-token')
    expect(init.headers['Content-Type']).toBe('application/xml')
    expect(Object.keys(init.headers).some((name) => /checkout|guest/i.test(name))).toBe(false)

    localStorage.setItem('leaf.jwt', 'second-token')
    fireEvent.click(screen.getByRole('button', { name: 'Import terrain' }))
    await waitFor(() => expect(props.onPhysicalHeadChanged).toHaveBeenCalledTimes(2))
    expect(fetchImpl.mock.calls[1][1].headers.Authorization).toBe('Bearer second-token')
    expect(fetchImpl.mock.calls[1][1].headers['X-Tenant-Id']).toBe(api.config.tenant)
  })

  it('W5 default transport reports a 401 with the bearer actually sent', async () => {
    const refused = new Response(UNAUTH_TEXT, { status: 401, headers: { 'content-type': 'application/json' } })
    const fetchImpl = vi.fn(async () => refused)
    vi.stubGlobal('fetch', fetchImpl)
    const note = vi.spyOn(api, 'noteUnauthorized')
    localStorage.setItem('leaf.jwt', 'expired-token')
    const props = supplied({ transport: undefined })
    render(<SolarWorkspaceTools {...props} />)
    open()
    submit()
    await waitFor(() => expect(note).toHaveBeenCalledTimes(1))
    expect(note).toHaveBeenCalledWith(refused, fetchImpl.mock.calls[0][0], 'Bearer expired-token')
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

