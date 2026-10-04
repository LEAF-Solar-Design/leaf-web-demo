import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarReadResult from './SolarReadResult.jsx'
import ResultPanel from '../components/ResultPanel.jsx'
import { ARTIFACT_UNVERIFIED, DOWNLOAD_REASONS, READ_RESULT_UNREADABLE, downloadReason } from './solarReadResultModel.js'

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
