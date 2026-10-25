import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { parse } from '@babel/parser'
import SolarCivilPanel from './SolarCivilPanel.jsx'
import SolarPhysicalReadPanel from './SolarPhysicalReadPanel.jsx'
import SolarTrackerRowsPanel from './SolarTrackerRowsPanel.jsx'
import { civilReason } from './solarCivilClient.js'
import SolarWorkspaceTools from './SolarWorkspaceTools.jsx'
import * as importClients from './solarImportClient.js'

afterEach(() => { cleanup(); vi.restoreAllMocks() })
const civilClient = () => ({
  getCivil: vi.fn().mockResolvedValue({ ok: true, value: {
    schema: 'leaf.solar-civil-view-response.v1', stored: false, head: null,
    preview: null, standing: null, grade_pads: 0,
  } }),
  runCivilOperation: vi.fn(),
})

it('S1-112 civil explains a missing checkout', async () => {
  render(<SolarCivilPanel drawingId="solar" client={civilClient()} disabled
    disabledReason="Take the drawing checkout before running civil operations." />)
  expect(screen.getByText('Take the drawing checkout before running civil operations.')).toBeInTheDocument()
  expect(screen.queryByText('A run is in progress, so wait for it to finish.')).toBeNull()
  await waitFor(() => expect(screen.getByRole('region').dataset.state).toBe('ready'))
})

it('S1-112 physical reads explains a failed drawing refresh', async () => {
  render(<SolarPhysicalReadPanel drawingId="solar" disabled
    disabledReason="The drawing could not be refreshed. Refresh the drawing before continuing." />)
  expect(screen.getByText('The drawing could not be refreshed. Refresh the drawing before continuing.')).toBeInTheDocument()
  await screen.findByRole('alert')
})

it('S1-119 read failures name the read instead of publishing', async () => {
  render(<SolarPhysicalReadPanel drawingId="solar" terrainClient={{ getTerrain: vi.fn().mockResolvedValue({ ok: false }) }} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('The current physical state could not be read. Refresh it before running this read.')
})

it('S1-125 no fitting frames asks for geometry changes', () => {
  expect(civilReason('FRAMES_PILES_NO_FRAMES_FIT')).toContain('Enlarge the boundary')
  expect(civilReason('FRAMES_PILES_NO_FRAMES_FIT')).not.toContain('refresh')
})

it('S1-125 structured fields include accessible examples', async () => {
  render(<SolarCivilPanel drawingId="solar" client={civilClient()} />)
  expect(screen.getByLabelText('Boundary')).toHaveAccessibleDescription(/JSON example:.*100/)
  expect(screen.getByLabelText('Preset')).toHaveAccessibleDescription(/JSON example:.*Name/)
  fireEvent.change(screen.getByLabelText('Operation'), { target: { value: 'piling-generate' } })
  expect(screen.getByLabelText('Pile template')).toHaveAccessibleDescription(/JSON example:.*Stations/)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Run' })).not.toBeDisabled())
})

it('S1-125 JSON syntax failures differ from invalid geometry', async () => {
  render(<SolarCivilPanel drawingId="solar" client={civilClient()} />)
  const run = screen.getByRole('button', { name: 'Run' })
  await waitFor(() => expect(run).not.toBeDisabled())
  fireEvent.change(screen.getByLabelText('Boundary'), { target: { value: '[' } })
  fireEvent.click(run)
  expect(screen.getByRole('alert')).toHaveTextContent('Enter valid JSON for boundary.')
  fireEvent.change(screen.getByLabelText('Boundary'), { target: { value: '[]' } })
  fireEvent.change(screen.getByLabelText('Preset'), { target: { value: '{"Name":"Full"}' } })
  fireEvent.click(run)
  expect(screen.getByRole('alert')).toHaveTextContent('at least three coordinate pairs')
})

it('S1-127 civil read preserves expired session guidance', async () => {
  const client = civilClient()
  client.getCivil.mockResolvedValue({ ok: false, code: 'UNAUTHENTICATED' })
  render(<SolarCivilPanel drawingId="solar" client={client} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Sign in again')
  expect(screen.getByRole('button', { name: 'Refresh physical state' })).not.toBeDisabled()
})

it('S1-127 tracker read preserves plan guidance', async () => {
  render(<SolarTrackerRowsPanel drawingId="solar" checkoutHeld
    client={{ createTrackerRows: vi.fn() }} readIntake={vi.fn()}
    terrainClient={{ getTerrain: vi.fn().mockResolvedValue({ ok: false, code: 'ENTITLEMENT_REQUIRED' }) }} />)
  await waitFor(() => expect(screen.getByTestId('solar-tracker-rows-status')).toHaveTextContent('workspace plan does not include'))
})

it('S1-127 civil read gives timeout recovery guidance', async () => {
  const client = civilClient()
  client.getCivil.mockResolvedValue({ ok: false, code: 'TERRAIN_CLIENT_TIMEOUT' })
  render(<SolarCivilPanel drawingId="solar" client={client} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('did not answer in time')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh physical state' }))
  await waitFor(() => expect(client.getCivil).toHaveBeenCalledTimes(2))
})

// Exercise App's actual callback and mount binding without booting the full Studio.
const appSource = readFileSync(`${process.cwd()}/src/App.jsx`, 'utf8')
const appTree = parse(appSource, { sourceType: 'module', plugins: ['jsx'] })
function appNode(predicate, node = appTree) {
  if (!node || typeof node !== 'object') return null
  if (predicate(node)) return node
  for (const value of Object.values(node)) {
    for (const child of Array.isArray(value) ? value : [value]) {
      if (child && typeof child.type === 'string') {
        const found = appNode(predicate, child)
        if (found) return found
      }
    }
  }
  return null
}
function hostAcceptance(overrides = {}) {
  const row = { name: 'solar-solaredge-accept', is_write: true }
  const scope = {
    tools: [row], refuseProjectSolar: vi.fn(() => false), setRunErr: vi.fn(),
    onRequestCatalogRun: vi.fn(() => ({ runIntent: { intentId: 'confirm-labels' } })),
    RIBBON_RATIONALE: 'Confirm before running.', ...overrides,
  }
  const mount = appNode(node => node.type === 'JSXOpeningElement' && node.name.name === 'SolarWorkspaceTools')
  const binding = mount.attributes.find(attr => attr.name?.name === 'onStageSolarEdgeAccept')
  expect(binding?.value?.expression?.type).toBe('Identifier')
  const declaration = appNode(node => node.type === 'VariableDeclarator'
    && node.id.name === binding.value.expression.name)
  const callback = declaration.init.arguments[0]
  const onStageSolarEdgeAccept = Function(...Object.keys(scope),
    `return (${appSource.slice(callback.start, callback.end)})`)(...Object.values(scope))
  return { ...scope, row, onStageSolarEdgeAccept }
}

it('S1-114 host stages the exact tracking-label request for confirmation', () => {
  const host = hostAcceptance()
  const params = { expected_rev: 12, report_artifact_id: 'b'.repeat(64) }
  expect(host.onStageSolarEdgeAccept(params)).toBe(true)
  expect(host.refuseProjectSolar).toHaveBeenCalledExactlyOnceWith('solar-solaredge-accept')
  expect(host.onRequestCatalogRun).toHaveBeenCalledExactlyOnceWith(
    host.row, params, host.RIBBON_RATIONALE, 'ribbon', { complete: true })
})

it('S1-114 host refuses project Solar before staging', () => {
  const host = hostAcceptance({ refuseProjectSolar: vi.fn(() => true) })
  expect(host.onStageSolarEdgeAccept({})).toBe(false)
  expect(host.onRequestCatalogRun).not.toHaveBeenCalled()
  expect(host.setRunErr).not.toHaveBeenCalled()
})

it('S1-114 host reports a missing acceptance tool without staging', () => {
  const host = hostAcceptance({ tools: [] })
  expect(host.onStageSolarEdgeAccept({})).toBe(false)
  expect(host.onRequestCatalogRun).not.toHaveBeenCalled()
  expect(host.setRunErr).toHaveBeenCalledWith('Tracking label acceptance is not in this catalog.')
})

it.each([undefined, {}, { runIntent: {} }])('S1-114 host returns false when no confirmation intent is armed (%j)', (answer) => {
  const host = hostAcceptance({ onRequestCatalogRun: vi.fn(() => answer) })
  expect(host.onStageSolarEdgeAccept({})).toBe(false)
})

it('S1-114 host binding enables acceptance through the real workspace and import panel', async () => {
  const host = hostAcceptance()
  const sourceId = 'a'.repeat(64)
  const reportId = 'b'.repeat(64)
  const artifact = (id, pdf = false) => ({
    schema: 'leaf.solar-artifact-ref.v1', artifact_id: id,
    media_type: pdf ? 'application/pdf' : 'application/json',
    filename: pdf ? 'solaredge-source.pdf' : 'solaredge-report.json', byte_length: 100,
    content_sha256: 'c'.repeat(64), source_version: 3,
    download: `/api/drawings/solar/artifacts/${id}`,
  })
  const common = { drawing_id: 'solar', project_id: 'p1', source_version: 3, graph_sha256: 'd'.repeat(64) }
  const client = {
    uploadPdf: vi.fn().mockResolvedValue({ ok: true, value: {
      ...common, schema: 'leaf.solar-import-source.v1', kind: 'solaredge-pdf',
      page_count: 2, source: artifact(sourceId, true),
    } }),
    requestReport: vi.fn().mockResolvedValue({ ok: true, value: {
      ...common, schema: 'leaf.solar-solaredge-report-result.v1', kind: 'solaredge-report',
      source_artifact_id: sourceId, report: artifact(reportId), counts: {
        pdf_matrices: 1, pdf_panels: 20, matchable_grids: 2, bridge_grids: 1, frames: 3,
        matched_frames: 2, group_strings: 4, bridge_strings: 1, strings: 5,
        assigned_panels: 18, unassigned_panels: 2, partial_strings: 1,
      },
    } }),
  }
  vi.spyOn(importClients, 'createSolarImportClient').mockReturnValue(client)
  render(<SolarWorkspaceTools drawingId="solar" projectId="p1" drawingVersion={3}
    flow="solaredge-import" checkoutHeld busy={false}
    onStageSolarEdgeAccept={host.onStageSolarEdgeAccept}
    transport={{ readIntake: vi.fn().mockResolvedValue({ version: 3, intake: { solar_design_graph: { rev: 12 } } }) }} />)
  fireEvent.click(screen.getByRole('button', { name: 'Import SolarEdge PDF' }))
  fireEvent.change(screen.getByLabelText('SolarEdge layout PDF'), {
    target: { files: [new File(['%PDF-1.7'], 'layout.pdf', { type: 'application/pdf' })] },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Upload', exact: true }))
  fireEvent.change(await screen.findByLabelText('Alignment tolerance'), { target: { value: '0.5' } })
  fireEvent.click(screen.getByRole('button', { name: 'Build report' }))
  await screen.findByRole('region', { name: 'Report review' })
  const accept = screen.getByRole('button', { name: 'Accept tracking labels' })
  await waitFor(() => expect(accept).not.toBeDisabled())
  expect(host.onRequestCatalogRun).not.toHaveBeenCalled()
  fireEvent.click(accept)
  expect(host.onRequestCatalogRun).toHaveBeenCalledExactlyOnceWith(host.row,
    { expected_rev: 12, report_artifact_id: reportId }, host.RIBBON_RATIONALE, 'ribbon', { complete: true })
})
