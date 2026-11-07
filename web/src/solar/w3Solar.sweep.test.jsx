import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import SolarStepEditor from './SolarStepEditor.jsx'
import SolarSizingForm from './SolarSizingForm.jsx'
import SolarEdgeImportPanel from './SolarEdgeImportPanel.jsx'
import { createSolarImportClient, solarEdgeReason } from './solarImportClient.js'
import { createSolarProjectClient } from './solarProjectClient.js'
import { terrainSummary, TERRAIN_CAPABILITIES } from './solarTerrainModel.js'

afterEach(cleanup)

it('S1-113 keeps a refused step readable and allows retry with retained inputs', () => {
  const onSubmit = vi.fn()
  render(<SolarStepEditor row={{ name: 'solar-test', label: 'Solar step', params: { type: 'object', properties: {} } }}
    status="failed" failureCode="HTTP_ERROR" retained={{}} onSubmit={onSubmit} onClose={vi.fn()} />)
  expect(screen.getByRole('alert').textContent).not.toContain('HTTP_ERROR')
  expect(screen.getByRole('alert').textContent).toContain('Your inputs are kept')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(onSubmit).toHaveBeenCalledTimes(1)
})

it('S1-118 names module parameters in designer language', async () => {
  render(<SolarSizingForm row={{ name: 'solar-size-strings' }} drawingId="solar" drawingVersion={1}
    readIntake={async () => null} onSubmit={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('Scope'), { target: { value: 'global' } })
  fireEvent.click(screen.getByLabelText('Use module parameters'))
  await waitFor(() => expect(screen.getByLabelText('Cells in series')).toBeTruthy())
  expect(screen.getByLabelText('Open circuit voltage at reference conditions (V)')).toBeTruthy()
  expect(screen.getByLabelText('Power temperature coefficient (%/°C)')).toBeTruthy()
  expect(screen.queryByLabelText('V_oc_ref')).toBeNull()
})

it('S1-113 keeps the failure notice for a run without a reason code', () => {
  render(<SolarStepEditor row={{ name: 'solar-test', label: 'Solar step', params: { type: 'object', properties: {} } }}
    status="failed" onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByRole('alert').textContent).toBe('This run failed. Your inputs are kept.')
})

it('S1-113 separates a known refusal sentence from the retained inputs notice', () => {
  render(<SolarStepEditor row={{ name: 'solar-test', label: 'Solar step', params: { type: 'object', properties: {} } }}
    status="failed" failureCode="pvg_no_panel_groups" onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByRole('alert').textContent).toBe('Choose a G33 capture that contains panel groups. Your inputs are kept.')
})

it('S1-113 explains a changed drawing without displaying the refusal code', () => {
  render(<SolarStepEditor row={{ name: 'solar-test', label: 'Solar step', params: { type: 'object', properties: {} } }}
    status="failed" failureCode="stale_rev" onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByRole('alert').textContent).toBe('The drawing changed. Review it before trying again. Your inputs are kept.')
})

it('S1-124 gives one retry instruction for a busy import and preserves focus', async () => {
  const client = { uploadPdf: vi.fn(async () => ({ ok: false, code: 'REPORT_BUSY', retryable: true })) }
  render(<SolarEdgeImportPanel drawingId="solar" client={client} />)
  fireEvent.change(screen.getByLabelText('SolarEdge layout PDF'), {
    target: { files: [new File(['%PDF-1.7'], 'layout.pdf', { type: 'application/pdf' })] },
  })
  const button = screen.getByRole('button', { name: 'Upload' })
  fireEvent.click(button)
  await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Other PDFs'))
  const text = screen.getByRole('status').textContent
  expect(text.match(/try again/gi)).toHaveLength(1)
  expect(text).not.toContain('retryable')
  expect(button.disabled).toBe(false)
  expect(document.activeElement).toBe(button)
})

it('S1-124 hides unknown import reason codes', () => {
  expect(solarEdgeReason('REPORT_FUTURE_REFUSAL')).toBe('The SolarEdge import stopped')
})

it('S1-128 asks users to check for saved work after an import timeout', () => {
  const text = solarEdgeReason('SOLAREDGE_CLIENT_TIMEOUT')
  expect(text).toContain('check for the saved PDF or report before trying again')
  expect(text).not.toContain('so try again')
})

it('S1-128 identifies a gateway failure as a service problem', async () => {
  const client = createSolarImportClient({ headers: () => ({}), fetchImpl: async () => ({
    status: 502, headers: { get: () => null }, body: null, text: async () => 'Bad gateway',
  }) })
  const result = await client.requestReport({ drawingId: 'solar', sourceArtifactId: 'a'.repeat(64), alignmentTolerance: 1 })
  expect(result).toMatchObject({ ok: false, code: 'SOLAREDGE_CLIENT_SERVICE_UNAVAILABLE' })
  expect(solarEdgeReason(result.code)).toContain('service had a problem')
  expect(solarEdgeReason(result.code)).not.toContain('could not be read')
})

it('S1-121 clears a failed checkout mutation when Retry reads an available checkout', async () => {
  const identity = { organization_id: '11111111-1111-1111-1111-111111111111',
    project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333' }
  const fetchImpl = vi.fn()
    .mockResolvedValueOnce({ ok: false, status: 409, json: async () => ({ error: { reason_code: 'CHECKOUT_DENIED' } }) })
    .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ ...identity, checkout: null }) })
  const client = createSolarProjectClient({ fetchImpl, authHeaders: () => ({}) })
  try {
    client.setScope({ kind: 'project', ...identity, input_version_id: '44444444-4444-4444-4444-444444444444' }, 'session-1')
    expect((await client.acquireCheckout()).ok).toBe(false)
    expect(client.getSnapshot().error).toBeTruthy()
    expect((await client.readCheckout()).ok).toBe(true)
    expect(client.getSnapshot().error).toBeNull()
  } finally { client.dispose() }
})

it('S1-126 describes the saved terrain revision without displaying its file identifier', () => {
  const artifactId = 'a'.repeat(64)
  const lines = terrainSummary({ stored: true, head: { index: 2, state: { artifact_id: artifactId } },
    terrain: { frame: { drawing_units: 'm', crs: 'none', elevation_datum: 'unrecorded' }, grid: null,
      previews: Object.fromEntries(TERRAIN_CAPABILITIES.map(key => [key, { state: 'absent', record: null }])) } })
  expect(lines).not.toBeNull()
  expect(lines.find(line => line.key === 'change')).toMatchObject({ label: 'Terrain revision', text: '3' })
  expect(JSON.stringify(lines)).not.toContain(artifactId)
  expect(JSON.stringify(lines)).not.toContain('of this drawing')
})
