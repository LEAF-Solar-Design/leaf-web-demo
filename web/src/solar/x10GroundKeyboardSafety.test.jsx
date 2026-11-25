import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as publicationClients from './solarProjectMaterializationClient.js'
import generationIntake from '../../../docs/parity/evidence/ground/generate/intake.json'
import SolarWorkspaceTools from './SolarWorkspaceTools.jsx'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

// Minimal supplied/initialFixture data from SolarWorkspaceTools.test.jsx.
// The real workspace, form, capture and onClick publication path stay mounted.
const id = n => `00000000-0000-0000-0000-${String(n).padStart(12, '0')}`
function initialFixture() {
  const scope = { organization_id: id(1), project_id: id(2), drawing_id: null, input_version_id: null }
  const api = {
    read: vi.fn(async () => ({ ok: true, value: { schema: 'leaf.project-physical-layout.v1',
      organization_id: id(1), project_id: id(2), drawing_id: null, parent_version_id: null,
      mode: 'initial', writer_available: true } })),
    publish: vi.fn(async captured => ({ ok: true, value: {
      organization_id: id(1), project_id: id(2), drawing_id: id(3), parent_version_id: null,
      request_id: captured.request_id, source_sha256: 'c'.repeat(64), intake_sha256: 'd'.repeat(64),
      mode: 'initial', generation_source: { parameters_sha256: 'e'.repeat(64), physical_content_sha256: 'f'.repeat(64) },
      drawing_version: { org_id: id(1), project_id: id(2), drawing_id: id(3), version_id: id(6) },
    } })),
  }
  vi.spyOn(publicationClients, 'createSolarProjectMaterializationClient').mockReturnValue(api)
  const props = {
    drawingId: null, projectId: id(2), projectScope: scope, drawingVersion: 1, flow: 'ground-physical',
    checkoutHeld: false, busy: false, catalogDigestOf: name => `sha256:${name}`,
    getCheckoutCapability: vi.fn(), onPhysicalHeadChanged: vi.fn(), onDrawingVersionChanged: vi.fn(),
    onProjectDrawingPublished: vi.fn(async () => true),
    transport: { fetchImpl: vi.fn(async () => { throw new Error('Unexpected transport request') }),
      headers: vi.fn(() => ({ 'X-Tenant-Id': 'workspace-test' })), onResponse: vi.fn(),
      publicationCheckout: { acquireCheckout: vi.fn(), releaseCheckout: vi.fn() } },
  }
  return { api, props }
}
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const generate = () => screen.getByRole('button', { name: 'Generate editable project drawing' })
async function ready() {
  fireEvent.click(screen.getByRole('button', { name: 'Read current layout for publication' }))
  await waitFor(() => expect(generate().disabled).toBe(false))
}
function keys(node, key) {
  node.focus()
  expect(document.activeElement).toBe(node)
  fireEvent.keyDown(node, { key, code: key === ' ' ? 'Space' : key })
  fireEvent.keyUp(node, { key, code: key === ' ' ? 'Space' : key })
}

describe('ground keyboard safety through the real workspace', () => {
  it.each(['structured', 'advanced'])('Enter in %s text inputs has no implicit publication route', async mode => {
    const f = initialFixture()
    const view = render(<SolarWorkspaceTools {...f.props} />)
    change('Site width (drawing units)', '10'); change('Site height (drawing units)', '20')
    if (mode === 'advanced') {
      fireEvent.click(screen.getByRole('button', { name: 'Advanced JSON' }))
      change('Site boundary (JSON)', JSON.stringify(generationIntake.boundary))
      change('Complete preset (JSON)', JSON.stringify(generationIntake.active_preset))
    }
    await ready()
    const labels = mode === 'structured'
      ? ['Site origin X (drawing units)', 'Site origin Y (drawing units)', 'Site width (drawing units)', 'Site height (drawing units)',
        'Preset name', 'Module length (metres)', 'Module width (metres)', 'Module thickness (metres)', 'Module power (watts)',
        'Horizontal module gap (metres)', 'Vertical module gap (metres)', 'Module rows', 'Module columns', 'Tilt (degrees)', 'Axis azimuth (degrees)']
      : ['Site boundary (JSON)', 'Complete preset (JSON)']
    for (const label of labels) {
      const input = screen.getByLabelText(label)
      expect(input.form).toBeNull()
      keys(input, 'Enter')
    }
    expect(view.container.querySelector('form, [onsubmit]')).toBeNull()
    for (const button of view.container.querySelectorAll('button')) expect(button.getAttribute('type')).toBe('button')
    expect(f.api.publish).not.toHaveBeenCalled()
    expect(f.props.onProjectDrawingPublished).not.toHaveBeenCalled()
  })

  it.each(['Enter', ' '])('explicit focused Generate %s activation dispatches the normal captured payload and callback', async key => {
    const f = initialFixture()
    render(<SolarWorkspaceTools {...f.props} />)
    change('Site width (drawing units)', '10'); change('Site height (drawing units)', '20')
    change('Preset name', 'Keyboard 🌞')
    const expected = JSON.parse(screen.getByLabelText('Generated source input parameters JSON').textContent)
    await ready()
    const button = generate()
    expect(button.tagName).toBe('BUTTON')
    expect(button.getAttribute('type')).toBe('button')
    expect(button.disabled).toBe(false)
    keys(button, key)
    expect(f.api.publish).not.toHaveBeenCalled()
    // jsdom does not synthesize all browser default keyboard actions. Model the
    // native button activation click explicitly; key events alone prove no activation.
    fireEvent.click(button)
    await waitFor(() => expect(f.props.onProjectDrawingPublished).toHaveBeenCalledOnce())
    expect(f.api.publish).toHaveBeenCalledOnce()
    const captured = f.api.publish.mock.calls[0][0]
    expect(captured).toMatchObject({ ...f.props.projectScope, mode: 'initial' })
    expect(JSON.parse(captured.parameters_json)).toEqual(expected)
    expect(f.props.transport.publicationCheckout.acquireCheckout).not.toHaveBeenCalled()
    const [receipt, callbackCapture, current] = f.props.onProjectDrawingPublished.mock.calls[0]
    expect(callbackCapture).toEqual(captured)
    expect(receipt.request_id).toBe(captured.request_id)
    expect(current()).toBe(true)
  })
})
