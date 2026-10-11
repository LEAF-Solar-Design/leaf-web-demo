import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarReadResult from './SolarReadResult.jsx'
import ResultPanel from '../components/ResultPanel.jsx'
import { ARTIFACT_UNVERIFIED, DOWNLOAD_REASONS, READ_RESULT_UNREADABLE, downloadReason } from './solarReadResultModel.js'
import { PROJECT_READ_REASONS, projectReadReason } from './solarReadResultModel.js'
import { createSolarProjectClient } from './solarProjectClient.js'
import * as artifactModule from './solarProjectArtifactClient.js'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'

const ampacity = {
  "adapter": "local-graph-read",
  "drawing_changed": false,
  "drawing_id": "solar",
  "graph_sha256": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
  "job_id": "job-g6r-1",
  "output": {
    "article": "NEC 310.16",
    "formula": "I_corrected = I_base × temp_factor × conduit_factor = 100 × 0.82 × 0.7",
    "inputs": {
      "I_base": 100,
      "conduit_factor": 0.7,
      "temp_factor": 0.82
    },
    "one_liner": "NEC 310.16 — Conductor ampacity with temperature and conduit-fill correction: I_corrected = I_base × temp_factor × conduit_factor = 100 × 0.82 × 0.7 = 57.4 A",
    "rejected_alternatives": [
      {
        "description": "I_base with no temperature correction",
        "why_rejected": "Required when ambient exceeds 30°C.",
        "would_have_resulted_in": 70
      }
    ],
    "result": 57.4,
    "short_description": "Conductor ampacity with temperature and conduit-fill correction",
    "source_url": "https://www.nfpa.org/codes-and-standards/nfpa-70",
    "units": "A"
  },
  "output_bytes": 743,
  "output_sha256": "8e74a3c4458786952c3d1ef54af45f7716abc16a31708fa0f5476ed518a0a9b2",
  "project_id": "leaf:project:00000000-0000-4000-8000-000000000001",
  "representation": "intake",
  "request_sha256": "ea27a372187309ff800cc8841bf16b4524c4f0b118da101d5f703c1d92897d6e",
  "schema_version": "leaf.solar-graph-read.v1",
  "source_version": 1,
  "tenant_id": "fixture-tenant",
  "tool": "solar-nec-ampacity-correction"
}

const acDrop = {
  "adapter": "local-graph-read",
  "drawing_changed": false,
  "drawing_id": "solar",
  "graph_sha256": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
  "job_id": "job-g6r-2",
  "output": {
    "article": "NEC 210.19(A)(4) Informational Note 4 (AC)",
    "formula": "VD% = (2 × I × (R·cosφ + X·sinφ) × L / 1000) / V × 100 = (2 × 10 × (0.5·1 + 0.1·0.000) × 100 / 1000) / 240 × 100",
    "inputs": {
      "I": 10,
      "L_ft": 100,
      "R_ohm_per_1000ft": 0.5,
      "V_source": 240,
      "X_ohm_per_1000ft": 0.1,
      "phase": 1,
      "powerFactor": 1
    },
    "one_liner": "NEC 210.19(A)(4) Informational Note 4 (AC) — AC voltage drop (1-phase, recommended <= 3% for inverter output): VD% = (2 × I × (R·cosφ + X·sinφ) × L / 1000) / V × 100 = (2 × 10 × (0.5·1 + 0.1·0.000) × 100 / 1000) / 240 × 100 = 0.416667 % (3% recommended max)",
    "rejected_alternatives": [],
    "result": 0.4166666666666667,
    "short_description": "AC voltage drop (1-phase, recommended <= 3% for inverter output)",
    "source_url": "https://www.nfpa.org/codes-and-standards/nfpa-70",
    "units": "% (3% recommended max)"
  },
  "output_bytes": 743,
  "output_sha256": "8e74a3c4458786952c3d1ef54af45f7716abc16a31708fa0f5476ed518a0a9b2",
  "project_id": "leaf:project:00000000-0000-4000-8000-000000000001",
  "representation": "intake",
  "request_sha256": "ea27a372187309ff800cc8841bf16b4524c4f0b118da101d5f703c1d92897d6e",
  "schema_version": "leaf.solar-graph-read.v1",
  "source_version": 1,
  "tenant_id": "fixture-tenant",
  "tool": "solar-nec-ac-voltage-drop"
}

const conduit = {
  "adapter": "local-graph-read",
  "drawing_changed": false,
  "drawing_id": "solar",
  "graph_sha256": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
  "job_id": "job-g6r-3",
  "output": {
    "conductor_area_sq_in": 0.0528,
    "conductors": [
      {
        "area_sq_in": 0.0211,
        "count": 2,
        "gauge": "10",
        "insulation": "THWN2",
        "role": "current-carrying",
        "unit": "AWG"
      },
      {
        "area_sq_in": 0.0106,
        "count": 1,
        "gauge": "10",
        "insulation": "Bare",
        "role": "egc",
        "unit": "AWG"
      }
    ],
    "conduit_area_sq_in": 0.285,
    "conduit_table": [
      {
        "area_sq_in": 0.285,
        "trade_size": "1/2"
      },
      {
        "area_sq_in": 0.508,
        "trade_size": "3/4"
      },
      {
        "area_sq_in": 0.832,
        "trade_size": "1"
      },
      {
        "area_sq_in": 1.453,
        "trade_size": "1-1/4"
      },
      {
        "area_sq_in": 1.986,
        "trade_size": "1-1/2"
      },
      {
        "area_sq_in": 3.291,
        "trade_size": "2"
      },
      {
        "area_sq_in": 4.695,
        "trade_size": "2-1/2"
      },
      {
        "area_sq_in": 7.268,
        "trade_size": "3"
      },
      {
        "area_sq_in": 9.737,
        "trade_size": "3-1/2"
      },
      {
        "area_sq_in": 12.554,
        "trade_size": "4"
      }
    ],
    "conduit_type": "PvcSch40",
    "conduit_type_label": "PVC Sch 40",
    "failure_reason": null,
    "fill_pct": 18.526315789473685,
    "max_fill_fraction": 0.4,
    "max_fill_pct": 40,
    "note": "NEC Ch9 T1/T4 - 3 conductors in 1/2\" PVC Sch 40: fill 18.5% ≤ 40% max",
    "success": true,
    "total_conductors": 3,
    "trade_size": "1/2"
  },
  "output_bytes": 743,
  "output_sha256": "8e74a3c4458786952c3d1ef54af45f7716abc16a31708fa0f5476ed518a0a9b2",
  "project_id": "leaf:project:00000000-0000-4000-8000-000000000001",
  "representation": "intake",
  "request_sha256": "ea27a372187309ff800cc8841bf16b4524c4f0b118da101d5f703c1d92897d6e",
  "schema_version": "leaf.solar-graph-read.v1",
  "source_version": 1,
  "tenant_id": "fixture-tenant",
  "tool": "solar-nec-conduit-fill"
}

const cable = {
  "adapter": "local-graph-read",
  "drawing_changed": false,
  "drawing_id": "solar",
  "graph_sha256": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3",
  "job_id": "dfe84971-84b5-45de-804e-d4da02c4c787",
  "output": {
    "artifact": {
      "artifact_id": "e3c082e905ff821f63b27ce25902f93396ae1bed958f21257cfd35e06c079e06",
      "byte_length": 4421,
      "content_sha256": "475c7c55ac1ee7eff28cc8d7a0bbf9d63b9d988d94624231f6077e39012be6e2",
      "download": "/api/drawings/solar/artifacts/e3c082e905ff821f63b27ce25902f93396ae1bed958f21257cfd35e06c079e06",
      "filename": "CableExport.xlsx",
      "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "schema": "leaf.solar-artifact-ref.v1",
      "source_version": 1
    },
    "summary": {
      "circuit_source": "topology",
      "feeders": 0,
      "inverter_record": "caller",
      "module_catalog": "unresolved",
      "modules": 3,
      "rows": [
        4,
        17,
        7,
        8
      ],
      "sheets": [
        "Homeruns",
        "Equipment Schedule",
        "Inverter Schedule",
        "String Schedule"
      ],
      "sizing": "absent",
      "status": "written",
      "strings": 2
    }
  },
  "output_bytes": 743,
  "output_sha256": "8e74a3c4458786952c3d1ef54af45f7716abc16a31708fa0f5476ed518a0a9b2",
  "project_id": "leaf:project:00000000-0000-4000-8000-000000000001",
  "representation": "intake",
  "request_sha256": "ea27a372187309ff800cc8841bf16b4524c4f0b118da101d5f703c1d92897d6e",
  "schema_version": "leaf.solar-graph-read.v1",
  "source_version": 1,
  "tenant_id": "fixture-tenant",
  "tool": "solar-cable-export"
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const button = () => screen.getByRole('button', { name: 'Download CableExport.xlsx (4.3 KB)' })
function deferred() {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}
const bytes = new Uint8Array([1, 2, 3])
const mediaType = cable.output.artifact.media_type
const success = {
  ok: true, status: 200,
  value: { artifactId: cable.output.artifact.artifact_id, mediaType, filename: 'CableExport.xlsx', byteLength: 4421, bytes },
}
function expectRow(label, text) {
  const header = screen.getByRole('rowheader', { name: label })
  expect(within(header.closest('tr')).getByRole('cell')).toHaveTextContent(text)
}

describe('Solar read result component', () => {
  it('srv01_ampacity_renders', () => {
    const { container } = render(<SolarReadResult data={ampacity} />)
    expect(screen.getByTestId('solar-read-headline')).toHaveTextContent('57.4 A')
    expectRow('Article', 'NEC 310.16')
    const inputs = screen.getByRole('region', { name: 'Inputs' })
    expect(within(inputs).getByRole('rowheader', { name: 'Temp factor' })).toBeInTheDocument()
    expect(within(inputs).getByRole('cell', { name: '0.82' })).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Rejected alternatives' })).getByRole('cell', { name: '70' })).toBeInTheDocument()
    expect(screen.getByText('Read details', { selector: 'summary' })).toBeInTheDocument()
    expect(container.textContent).not.toContain('fixture-tenant')
    expect(container.textContent).not.toContain(cable.project_id)
    expect(screen.getByTestId('solar-read-result')).toHaveAttribute('data-tool', ampacity.tool)
  })

  it('srv02_conduit_fill_renders', () => {
    render(<SolarReadResult data={conduit} />)
    expect(screen.getByRole('cell', { name: 'Bare' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '0.0106' })).toBeInTheDocument()
    expectRow('Fill pct', '18.5263')
  })

  it('srv03_download_success', async () => {
    const download = vi.fn().mockResolvedValue(success)
    const save = vi.fn()
    render(<SolarReadResult data={cable} download={download} save={save} />)
    fireEvent.click(button())
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Downloaded CableExport.xlsx.'))
    expect(download).toHaveBeenCalledTimes(1)
    expect(download).toHaveBeenCalledWith({ drawingId: 'solar', ref: cable.output.artifact, signal: expect.any(AbortSignal) })
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledWith(bytes, mediaType, 'CableExport.xlsx')
  })

  it('srv04_single_flight', async () => {
    const pending = deferred()
    const download = vi.fn().mockReturnValue(pending.promise)
    const save = vi.fn()
    render(<SolarReadResult data={cable} download={download} save={save} />)
    const target = button()
    act(() => { target.click(); target.click() })
    expect(download).toHaveBeenCalledTimes(1)
    expect(target).toHaveTextContent('Downloading CableExport.xlsx')
    expect(target).toBeDisabled()
    await act(async () => { pending.resolve({ ok: false, code: 'SOLAREDGE_CLIENT_ABORTED' }) })
    expect(save).not.toHaveBeenCalled()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(button()).not.toBeDisabled()
  })

  it('srv05_refusal_sentences', async () => {
    for (const [code, status, sentence] of [
      ['ARTIFACT_NOT_FOUND', 404, 'This file is no longer stored, so run the tool again to make a new one.'],
      ['ARTIFACT_STALE', 409, 'The drawing changed after this file was made, so run the tool again.'],
      ['SOLAREDGE_CLIENT_ARTIFACT_MISMATCH', 200, 'The downloaded file did not match its record, so it was discarded.'],
      ['FORBIDDEN', 403, 'You do not have access to this file.'],
      ['UNAUTHENTICATED', 401, 'Sign in again to download this file.'],
      ['SOMETHING_ELSE', 500, 'The download failed, so try again.'],
      ['WHATEVER', 403, 'You do not have access to this file.'],
      ['WHATEVER', 404, 'This file is no longer stored, so run the tool again to make a new one.'],
      ['toString', 500, 'The download failed, so try again.'],
    ]) {
      const save = vi.fn()
      const view = render(<SolarReadResult data={cable} download={vi.fn().mockResolvedValue({ ok: false, code, status })} save={save} />)
      fireEvent.click(button())
      await waitFor(() => expect(screen.getByRole('status').textContent).toBe(sentence))
      expect(downloadReason(code, status)).toBe(sentence)
      expect(save).not.toHaveBeenCalled()
      view.unmount()
    }
  })

  it('srv06_failures', async () => {
    const save = vi.fn()
    const rejected = render(<SolarReadResult data={cable} download={vi.fn().mockRejectedValue(new Error('network'))} save={save} />)
    fireEvent.click(button())
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(DOWNLOAD_REASONS.FALLBACK))
    expect(save).not.toHaveBeenCalled()
    rejected.unmount()
    const throwingSave = vi.fn(() => { throw new Error('save failed') })
    render(<SolarReadResult data={cable} download={vi.fn().mockResolvedValue(success)} save={throwingSave} />)
    fireEvent.click(button())
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(DOWNLOAD_REASONS.SAVE_FAILED))
    expect(throwingSave).toHaveBeenCalledTimes(1)
  })

  it('srv07_unmount_aborts', async () => {
    const pending = deferred()
    const download = vi.fn().mockReturnValue(pending.promise)
    const save = vi.fn()
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
    const view = render(<SolarReadResult data={cable} download={download} save={save} />)
    fireEvent.click(button())
    const signal = download.mock.calls[0][0].signal
    expect(signal.aborted).toBe(false)
    view.unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => { pending.resolve(success) })
    expect(save).not.toHaveBeenCalled()
    expect(errors).not.toHaveBeenCalled()
  })

  it('srv08_unverified_ref', () => {
    const artifact = { ...cable.output.artifact, download: cable.output.artifact.download.replace('/solar/', '/other/') }
    render(<SolarReadResult data={{ ...cable, output: { ...cable.output, artifact } }} />)
    expect(screen.getByTestId('solar-read-download-status')).toHaveTextContent(ARTIFACT_UNVERIFIED)
    expect(screen.queryByTestId('solar-read-download')).not.toBeInTheDocument()
  })

  it('srv09_unreadable', () => {
    render(<SolarReadResult data={{ ...ampacity, output: null }} />)
    expect(screen.getByText(READ_RESULT_UNREADABLE)).toBeInTheDocument()
    expect(screen.queryByTestId('solar-read-headline')).not.toBeInTheDocument()
    expect(screen.getByText('Read details', { selector: 'summary' })).toBeInTheDocument()
  })

  it('srv10_result_panel_dispatch', () => {
    const view = render(<ResultPanel running={false} result={{ ok: true, result: ampacity }} />)
    expect(screen.getByTestId('solar-read-headline')).toHaveTextContent('57.4 A')
    expect(screen.queryByText('request_sha256')).not.toBeInTheDocument()
    view.rerender(<ResultPanel running={false} result={{ ok: true, result: { ...ampacity, schema_version: 'leaf.solar-graph-commit.v1' } }} />)
    expect(screen.queryByTestId('solar-read-result')).not.toBeInTheDocument()
    const toolCell = screen.getByRole('cell', { name: ampacity.tool })
    expect(toolCell.closest('tr').children[0]).toHaveTextContent(new RegExp('tool', 'i'))
    view.rerender(<ResultPanel running={false} result={{ ok: true, result: { counts: { Panels: 4 }, total: 4 } }} />)
    expect(view.container.querySelector('table.counts')).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: 'Panels' })).toBeInTheDocument()
  })

  it('srv11_default_transport', async () => {
    const previousToken = localStorage.getItem('leaf.jwt')
    const fetchStub = vi.fn().mockImplementation(async () => new Response(
      '{"ok":false,"error":{"reason_code":"ARTIFACT_NOT_FOUND","retryable":false}}',
      { status: 404, headers: { 'content-type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchStub)
    localStorage.setItem('leaf.jwt', 'tok-g6r')
    try {
      render(<SolarReadResult data={cable} />)
      fireEvent.click(button())
      await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(DOWNLOAD_REASONS.ARTIFACT_NOT_FOUND))
      expect(fetchStub).toHaveBeenCalledTimes(1)
      const [url, init] = fetchStub.mock.calls[0]
      expect(url.endsWith('/api/drawings/solar/artifacts/' + cable.output.artifact.artifact_id)).toBe(true)
      expect(init.headers['X-Tenant-Id']).toBeTruthy()
      expect(init.headers.Authorization).toBe('Bearer tok-g6r')
      expect(url).not.toContain('tok-g6r')
    } finally {
      cleanup()
      if (previousToken === null) localStorage.removeItem('leaf.jwt')
      else localStorage.setItem('leaf.jwt', previousToken)
      vi.unstubAllGlobals()
    }
  })

  it('srv12_summary_renders', () => {
    render(<SolarReadResult data={ampacity} />)
    const summary = screen.getByText(ampacity.output.one_liner)
    expect(summary.tagName).toBe('P')
    expect(summary).toHaveClass('solar-read-summary')
  })

  it('srv13_private_keys_not_rendered', () => {
    const id = cable.project_id
    const { container } = render(<SolarReadResult data={{ ...ampacity, output: { head: { project_id: id, index: 3 }, rows: [{ project_id: id, n: 1 }], tenant_id: 'fixture-tenant' } }} />)
    for (const hidden of [id, 'fixture-tenant', 'Project id', 'Tenant id']) expect(container.textContent).not.toContain(hidden)
    expect(within(screen.getByRole('region', { name: 'Head' })).getByRole('rowheader', { name: 'Index' })).toBeInTheDocument()
  })
})

const projectBinding = Object.freeze({
  kind: 'project',
  organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222',
  drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444',
})
const bodyHash = '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
const intakeHash = 'a'.repeat(64)
const graphHash = 'b'.repeat(64)
const catalogDigest = 'sha256:' + 'c'.repeat(64)
function projectRef(overrides = {}) {
  const artifactId = 'd'.repeat(64)
  return { organization_id: projectBinding.organization_id, project_id: projectBinding.project_id,
    drawing_id: projectBinding.drawing_id, source_version_id: projectBinding.input_version_id,
    source_intake_sha256: intakeHash, graph_sha256: graphHash, artifact_id: artifactId,
    media_type: 'application/json', filename: 'ElectricalSchedules.json', byte_length: 2,
    content_sha256: bodyHash, schema: 'leaf.solar-project-artifact-ref.v1',
    download: `/api/projects/${projectBinding.project_id}/drawings/${projectBinding.drawing_id}/solar-artifacts/${artifactId}`,
    ...overrides }
}
function projectResult(overrides = {}) {
  return { schema: 'leaf.solar-project-read.v1', adapter: 'project-local-graph-read',
    organization_id: projectBinding.organization_id, project_id: projectBinding.project_id,
    drawing_id: projectBinding.drawing_id, input_version_id: projectBinding.input_version_id,
    input_intake_sha256: intakeHash, graph_sha256: graphHash, tool: 'solar-electrical-schedules',
    tool_manifest_sha256: catalogDigest, graph_project_id: 'private-graph-project',
    head_version_id: projectBinding.input_version_id, is_head: true, current_required: true,
    request_sha256: 'e'.repeat(64), output_sha256: 'f'.repeat(64), output_bytes: 200,
    drawing_changed: false, output: { result: 57.4, units: 'A', artifact: projectRef() },
    ...overrides }
}

function rawProjectFile() {
  return { ok: true, status: 200, data: { notModified: false, bytes: new Uint8Array([123, 125]),
    headers: { ETag: '"' + bodyHash + '"', 'X-Leaf-Artifact-Id': 'd'.repeat(64), 'Content-Type': 'application/json' } } }
}
function hashBuffer(bytes) {
  return new Uint8Array(createHash('sha256').update(bytes).digest()).buffer
}
function installDigest(implementation = async (_algorithm, value) => hashBuffer(value)) {
  const digest = vi.fn(implementation)
  vi.stubGlobal('crypto', { subtle: { digest } })
  return digest
}
function projectHarness(transport = async () => rawProjectFile()) {
  let revision = 0
  let snapshot = { binding: projectBinding, authorityKey: 'epoch-1' }
  const listeners = new Set()
  const projectClient = {
    downloadArtifact: vi.fn(transport),
    getSnapshot: () => snapshot,
    captureCatalogScope: () => { const captured = revision; return () => captured === revision },
    subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener) },
    prepareRun: vi.fn(), submitRun: vi.fn(), setScope: vi.fn(), dispose: vi.fn(),
  }
  const save = vi.fn()
  const data = projectResult()
  const props = { data, solarBinding: projectBinding, authorityKey: 'epoch-1', projectClient, catalogDigest, save }
  const change = (next = snapshot) => { snapshot = next; revision += 1; for (const listener of listeners) listener() }
  return { props, projectClient, save, change }
}
const projectButton = () => screen.getByRole('button', { name: 'Download ElectricalSchedules.json (2 bytes)' })
const clickProject = () => fireEvent.click(projectButton())
const flushProject = async () => { for (let index = 0; index < 12; index += 1) await Promise.resolve() }

describe('project result downloads', () => {
  it('R12-04 saves exactly one verified file through the R7 project client', async () => {
    installDigest()
    const fetchImpl = vi.fn(async () => new Response('{}', { status: 200, headers: {
      'Content-Type': 'application/json', 'Content-Length': '2', ETag: '"' + bodyHash + '"',
      'X-Leaf-Artifact-Id': 'd'.repeat(64) } }))
    const projectClient = createSolarProjectClient({ fetchImpl })
    projectClient.setScope(projectBinding, 'epoch-1')
    const save = vi.fn()
    const legacy = vi.fn()
    render(<SolarReadResult data={projectResult()} solarBinding={projectBinding} authorityKey="epoch-1"
      projectClient={projectClient} catalogDigest={catalogDigest} download={legacy} save={save} />)
    expect(screen.getByTestId('solar-read-headline')).toHaveTextContent('57.4 A')
    clickProject()
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Downloaded ElectricalSchedules.json.'))
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    expect(fetchImpl.mock.calls[0][0]).toBe(projectRef().download + '?current=true')
    expect(save).toHaveBeenCalledTimes(1)
    expect(save.mock.calls[0]).toEqual([new Uint8Array([123, 125]), 'application/json', 'ElectricalSchedules.json'])
    expect(legacy).not.toHaveBeenCalled()
    projectClient.dispose()
  })

  it('R12-12 discards a pending transport when the drawing or principal scope changes', async () => {
    installDigest()
    for (const change of [
      { binding: { ...projectBinding, input_version_id: projectBinding.organization_id }, authorityKey: 'epoch-1' },
      { binding: projectBinding, authorityKey: 'epoch-2' },
    ]) {
      const pending = deferred()
      const h = projectHarness(() => pending.promise)
      const view = render(<SolarReadResult {...h.props} />)
      clickProject()
      await act(flushProject)
      const signal = h.projectClient.downloadArtifact.mock.calls[0][1].signal
      await act(async () => { h.change(change); await flushProject() })
      expect(signal.aborted).toBe(true)
      expect(screen.getByRole('status')).toHaveTextContent(PROJECT_READ_REASONS.scope_stale)
      await act(async () => { pending.resolve(rawProjectFile()); await flushProject() })
      expect(h.save).not.toHaveBeenCalled()
      expect(projectButton()).not.toBeDisabled()
      view.unmount()
    }
  })

  it('R12-13 guards transport digest failure and the component continuation against stale scope', async () => {
    for (const rejected of [false, true]) {
      const pending = deferred()
      let rejectDigest
      const digest = installDigest(() => rejected ? new Promise((_resolve, reject) => { rejectDigest = reject }) : pending.promise)
      const h = projectHarness()
      const view = render(<SolarReadResult {...h.props} />)
      clickProject()
      await waitFor(() => expect(digest).toHaveBeenCalledTimes(1))
      await act(async () => {
        h.change()
        if (rejected) rejectDigest(new Error('digest failed'))
        else pending.resolve(hashBuffer(new Uint8Array([123, 125])))
        await flushProject()
      })
      expect(h.save).not.toHaveBeenCalled()
      expect(screen.getByRole('status')).toHaveTextContent(PROJECT_READ_REASONS.scope_stale)
      view.unmount()
    }
    const pending = deferred()
    const h = projectHarness()
    // Isolate the final continuation: a success-shaped wrapper outcome still needs a live R7 scope.
    vi.spyOn(artifactModule, 'createSolarProjectArtifactClient').mockReturnValue({ downloadArtifact: () => pending.promise })
    render(<SolarReadResult {...h.props} />)
    clickProject()
    await act(async () => {
      pending.resolve({ ok: true, status: 200, data: { bytes: new Uint8Array([123, 125]),
        mediaType: 'application/json', filename: 'ElectricalSchedules.json' } })
      h.change()
      await flushProject()
    })
    expect(h.save).not.toHaveBeenCalled()
  })

  it('R12-14 generations protect A to B to A and every result client catalog or policy replacement', async () => {
    installDigest()
    for (const kind of ['binding', 'result', 'client', 'catalog', 'policy']) {
      const oldPending = deferred()
      const newPending = deferred()
      const h = projectHarness(vi.fn().mockReturnValueOnce(oldPending.promise).mockReturnValue(newPending.promise))
      const view = render(<SolarReadResult {...h.props} />)
      clickProject()
      await act(flushProject)
      const changed = { ...h.props }
      if (kind === 'binding') changed.solarBinding = { ...projectBinding, input_version_id: projectBinding.organization_id }
      if (kind === 'result') changed.data = projectResult({ output: { count: 4, artifact: projectRef() } })
      if (kind === 'client') changed.projectClient = projectHarness().projectClient
      if (kind === 'catalog') changed.catalogDigest = 'sha256:' + '0'.repeat(64)
      if (kind === 'policy') changed.projectCurrent = false
      view.rerender(<SolarReadResult {...changed} />)
      view.rerender(<SolarReadResult {...h.props} />)
      clickProject()
      await act(flushProject)
      expect(h.projectClient.downloadArtifact).toHaveBeenCalledTimes(2)
      await act(async () => { oldPending.resolve(rawProjectFile()); await flushProject() })
      expect(h.save).not.toHaveBeenCalled()
      expect(screen.getByTestId('solar-read-download')).toBeDisabled()
      fireEvent.click(screen.getByTestId('solar-read-download'))
      expect(h.projectClient.downloadArtifact).toHaveBeenCalledTimes(2)
      await act(async () => { newPending.resolve(rawProjectFile()); await flushProject() })
      expect(h.save).toHaveBeenCalledTimes(1)
      view.unmount()
    }
  })

  it('R12-15 invalidates a pending file on captured R7 lease rotation', async () => {
    installDigest()
    const pending = deferred()
    const h = projectHarness(() => pending.promise)
    render(<SolarReadResult {...h.props} />)
    clickProject()
    await act(flushProject)
    await act(async () => { h.change(); await flushProject() })
    expect(screen.getByRole('status')).toHaveTextContent(PROJECT_READ_REASONS.scope_stale)
    await act(async () => { pending.resolve(rawProjectFile()); await flushProject() })
    expect(h.save).not.toHaveBeenCalled()
    expect(h.projectClient.setScope).not.toHaveBeenCalled()
    expect(h.projectClient.dispose).not.toHaveBeenCalled()
  })

  it('R12-16 locks synchronously for two clicks before a render', async () => {
    installDigest()
    const pending = deferred()
    const h = projectHarness(() => pending.promise)
    render(<SolarReadResult {...h.props} />)
    const target = projectButton()
    act(() => { target.click(); target.click() })
    await act(flushProject)
    expect(h.projectClient.downloadArtifact).toHaveBeenCalledTimes(1)
    expect(target).toBeDisabled()
    await act(async () => { pending.resolve(rawProjectFile()); await flushProject() })
    expect(h.save).toHaveBeenCalledTimes(1)
  })

  it('R12-17 retains the pending lock across an unrelated rerender and aborts on unmount', async () => {
    installDigest()
    const pending = deferred()
    const h = projectHarness(() => pending.promise)
    const view = render(<SolarReadResult {...h.props} />)
    clickProject()
    await act(flushProject)
    view.rerender(<SolarReadResult {...h.props} title="unrelated" />)
    expect(screen.getByTestId('solar-read-download')).toBeDisabled()
    fireEvent.click(screen.getByTestId('solar-read-download'))
    expect(h.projectClient.downloadArtifact).toHaveBeenCalledTimes(1)
    const signal = h.projectClient.downloadArtifact.mock.calls[0][1].signal
    view.unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => { pending.resolve(rawProjectFile()); await flushProject() })
    expect(h.save).not.toHaveBeenCalled()
    vi.useFakeTimers()
    const digest = installDigest(() => new Promise(() => {}))
    const timed = projectHarness()
    const timedView = render(<SolarReadResult {...timed.props} />)
    clickProject()
    await act(flushProject)
    expect(digest).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.advanceTimersByTimeAsync(60001); await flushProject() })
    expect(screen.getByRole('status')).toHaveTextContent(PROJECT_READ_REASONS.timeout)
    expect(projectButton()).not.toBeDisabled()
    expect(timed.save).not.toHaveBeenCalled()
    timedView.unmount()
  })

  it('R12-19 reports a thrown save without claiming a completed download', async () => {
    installDigest()
    const h = projectHarness()
    h.props.save = vi.fn(() => { throw new Error('save failed') })
    render(<SolarReadResult {...h.props} />)
    clickProject()
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(PROJECT_READ_REASONS.save_failed))
    expect(h.props.save).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Downloaded ElectricalSchedules.json.')).not.toBeInTheDocument()
  })

  it('R12-22 verifies and saves an explicitly historical result with current false', async () => {
    installDigest()
    const fetchImpl = vi.fn(async () => new Response('{}', { headers: {
      'Content-Type': 'application/json', ETag: '"' + bodyHash + '"', 'X-Leaf-Artifact-Id': 'd'.repeat(64) } }))
    const projectClient = createSolarProjectClient({ fetchImpl })
    projectClient.setScope(projectBinding, 'epoch-1')
    const data = projectResult({ head_version_id: projectBinding.organization_id, is_head: false, current_required: false })
    const save = vi.fn()
    render(<SolarReadResult data={data} solarBinding={projectBinding} authorityKey="epoch-1"
      projectClient={projectClient} catalogDigest={catalogDigest} projectCurrent={false} save={save} />)
    expect(screen.getByText('Historical version.')).toBeInTheDocument()
    clickProject()
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect(fetchImpl.mock.calls[0][0]).toBe(projectRef().download + '?current=false')
    expect(fetchImpl.mock.calls[0][1].headers).not.toHaveProperty('If-None-Match')
    projectClient.dispose()
  })

  it('R12-28 refuses missing project dependencies without any legacy transport fallback', () => {
    const legacy = vi.fn()
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    for (const [key, reason] of [['solarBinding', 'SIP_R12_BINDING_INVALID'], ['authorityKey', 'SIP_R12_CLIENT_UNAVAILABLE'],
      ['projectClient', 'SIP_R12_CLIENT_UNAVAILABLE'], ['catalogDigest', 'SIP_R12_CATALOG_DIGEST_REQUIRED']]) {
      const h = projectHarness()
      const props = { ...h.props, [key]: undefined, download: legacy }
      const view = render(<SolarReadResult {...props} />)
      expect(screen.getByText(projectReadReason(reason))).toBeInTheDocument()
      expect(screen.queryByTestId('solar-read-download')).not.toBeInTheDocument()
      expect(h.projectClient.downloadArtifact).not.toHaveBeenCalled()
      view.unmount()
    }
    expect(legacy).not.toHaveBeenCalled()
    expect(fetchImpl).not.toHaveBeenCalled()
  })

  it('R12-29 consumes completed reads without admitting or submitting a graph job preview', async () => {
    installDigest()
    const refusal = { ok: false, status: 409, reason: 'project_adapter_unavailable',
      envelope: { error: { reason_code: 'project_adapter_unavailable', retryable: false } } }
    const h = projectHarness(async () => refusal)
    render(<SolarReadResult {...h.props} />)
    expect(screen.queryByRole('button', { name: /run|refresh/i })).not.toBeInTheDocument()
    clickProject()
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(PROJECT_READ_REASONS.unsupported))
    expect(h.projectClient.prepareRun).not.toHaveBeenCalled()
    expect(h.projectClient.submitRun).not.toHaveBeenCalled()
    expect(h.save).not.toHaveBeenCalled()
    const source = readFileSync(process.cwd() + '/src/solar/SolarReadResult.jsx', 'utf8')
    for (const forbidden of ['/api/run', 'prepareRun(', 'submitRun(', 'newProjectIntentKey(']) expect(source).not.toContain(forbidden)
  })

  it('R12-34 never renders the private graph project or the scope ids', () => {
    const h = projectHarness()
    const { container } = render(<SolarReadResult {...h.props} />)
    expect(container.textContent).toContain('57.4')
    for (const hidden of ['private-graph-project', projectBinding.organization_id, projectBinding.project_id]) {
      expect(container.innerHTML).not.toContain(hidden)
    }
  })

  it('R12-31 matches all frozen standalone rendered HTML bytes and hashes', () => {
    const oracle = JSON.parse(readFileSync(process.cwd() + '/src/solar/__fixtures__/solarReadResultStandalone.main-a021056e.json', 'utf8'))
    for (const entry of Object.values(oracle.cases)) {
      const view = render(<SolarReadResult data={entry.input} />)
      expect(view.container.innerHTML).toBe(entry.html)
      expect(Buffer.byteLength(view.container.innerHTML, 'utf8')).toBe(entry.htmlBytes)
      expect(createHash('sha256').update(view.container.innerHTML).digest('hex')).toBe(entry.htmlSha256)
      cleanup()
    }
  })
})
