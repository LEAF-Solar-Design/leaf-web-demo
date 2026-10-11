import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ResultPanel from './ResultPanel.jsx'
import { SINGLE_KEY_SHORTCUTS_KEY, writeSingleKeyShortcuts } from '../lib/singleKeyPreference.js'
import { act, waitFor } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createHash } from 'node:crypto'
import SolarReadResult from '../solar/SolarReadResult.jsx'
import { isSolarProjectReadResult, isSolarReadResult, projectReadReason } from '../solar/solarReadResultModel.js'

vi.mock('../solar/SolarReadResult.jsx', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, default: vi.fn((props) => <actual.default {...props} />) }
})


afterEach(() => {
  cleanup()
  localStorage.removeItem(SINGLE_KEY_SHORTCUTS_KEY)
})

describe('ResultPanel R retry and the single-key switch (S25)', () => {
  const retryable = { ok: false, error: { error_code: 'UPSTREAM_TIMEOUT', message: 'The run timed out.', retryable: true } }

  it('R calls onRetry with single-key shortcuts on (the default)', () => {
    const onRetry = vi.fn()
    render(<ResultPanel running={false} result={retryable} onRetry={onRetry} />)
    fireEvent.keyDown(window, { key: 'r' })
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('R does not call onRetry with the switch off, and does again once it is back on', () => {
    const onRetry = vi.fn()
    render(<ResultPanel running={false} result={retryable} onRetry={onRetry} />)
    expect(writeSingleKeyShortcuts(false)).toBe(true)
    fireEvent.keyDown(window, { key: 'r' })
    fireEvent.keyDown(window, { key: 'R' })
    expect(onRetry).not.toHaveBeenCalled()
    // Read at keystroke time: flipping back on needs no remount.
    expect(writeSingleKeyShortcuts(true)).toBe(true)
    fireEvent.keyDown(window, { key: 'r' })
    expect(onRetry).toHaveBeenCalledTimes(1)
  })
})

describe('ResultPanel outcome fallbacks', () => {
  it('does not call highlight overlay output empty', () => {
    render(<ResultPanel running={false} result={{ ok: true, result: {}, overlay: { highlight_handles: ['A1'] } }} />)
    expect(screen.queryByText('Completed with no output.')).toBeNull()
    expect(screen.getByText('1 panel highlighted in the viewer')).toBeInTheDocument()
  })

  it('does not call files-only output empty', () => {
    render(<ResultPanel running={false} result={{ ok: true, result: { files: [{ name: 'a.dxf' }] } }} />)
    expect(screen.queryByText('Completed with no output.')).toBeNull()
  })

  it('reports a successful run with no result', () => {
    render(<ResultPanel running={false} result={{ ok: true }} />)
    expect(screen.getByText('Completed with no output.')).toBeInTheDocument()
  })

  it('reports a nested-only result without an empty key/value table', () => {
    const { container } = render(<ResultPanel running={false} result={{ ok: true, result: { nested: { a: 1 } } }} />)
    expect(screen.getByText('Completed with no output.')).toBeInTheDocument()
    expect(container.querySelector('table.kv')).toBeNull()
  })

  it('reports a failed run without a reason', () => {
    render(<ResultPanel running={false} result={{ ok: false }} />)
    expect(screen.getByText('The run failed without reporting a reason.')).toBeInTheDocument()
    expect(screen.queryByText('Completed with no output.')).toBeNull()
  })

  it('keeps the counts table for counts output', () => {
    render(<ResultPanel running={false} result={{ ok: true, result: { counts: { Panels: 4 }, total: 4 } }} />)
    expect(screen.getByRole('table')).toHaveClass('counts')
    expect(screen.getByRole('cell', { name: 'Panels' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '4' })).toBeInTheDocument()
    expect(screen.queryByText('Completed with no output.')).toBeNull()
  })
})


describe('ResultPanel actionable error guidance', () => {
  it('shows the next action and responsible actor from the shared envelope', () => {
    render(
      <ResultPanel
        running={false}
        tool={{ name: 'drape-onto-spheres' }}
        result={{
          ok: false,
          tool: 'drape-onto-spheres',
          version: '1.0.1',
          error: {
            error_code: 'ENTITLEMENT_REQUIRED',
            message: 'This workspace cannot run drawing-write tools.',
            retryable: false,
            retry_class: 'after_action',
            actor: 'workspace_admin',
            next_action: 'Enable this capability for the workspace, then retry.',
          },
          timing_ms: 0,
          cost: null,
          entitlement_required: 'drawing.write',
        }}
      />,
    )

    expect(screen.getByText(
      'Next: Enable this capability for the workspace, then retry.',
    )).toBeInTheDocument()
    expect(screen.getByText('Workspace admin')).toBeInTheDocument()
  })
})

const B = {
  kind: 'project',
  organization_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222',
  drawing_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444',
}
const authorityKey = 'epoch-1'
const D = 'sha256:' + 'c'.repeat(64)
const intakeHash = 'a'.repeat(64)
const graphHash = 'b'.repeat(64)
const artifactId = 'd'.repeat(64)
const bodyHash = '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'
const ref = {
  organization_id: B.organization_id, project_id: B.project_id, drawing_id: B.drawing_id,
  source_version_id: B.input_version_id, source_intake_sha256: intakeHash, graph_sha256: graphHash,
  artifact_id: artifactId, media_type: 'application/json', filename: 'ElectricalSchedules.json',
  byte_length: 2, content_sha256: bodyHash, schema: 'leaf.solar-project-artifact-ref.v1',
  download: `/api/projects/${B.project_id}/drawings/${B.drawing_id}/solar-artifacts/${artifactId}`,
}
const output = { result: 57.4, units: 'A', artifact: ref }
function resultFor(tool = 'solar-electrical-schedules') {
  return {
    schema: 'leaf.solar-project-read.v1', adapter: 'project-local-graph-read',
    organization_id: B.organization_id, project_id: B.project_id, drawing_id: B.drawing_id,
    input_version_id: B.input_version_id, input_intake_sha256: intakeHash, graph_sha256: graphHash,
    tool, tool_manifest_sha256: D, graph_project_id: 'private-graph-project',
    head_version_id: B.input_version_id, is_head: true, current_required: true,
    request_sha256: 'e'.repeat(64), output_sha256: 'f'.repeat(64),
    output_bytes: Buffer.byteLength(JSON.stringify(output)), drawing_changed: false, output,
  }
}
const pendingFile = () => {
  let resolveFile
  const promise = new Promise((resolvePending) => { resolveFile = resolvePending })
  return { promise, resolve: resolveFile }
}
const rawFile = () => ({ ok: true, status: 200, data: {
  notModified: false, bytes: new Uint8Array([123, 125]), headers: {
    ETag: '"' + bodyHash + '"', 'X-Leaf-Artifact-Id': artifactId, 'Content-Type': 'application/json',
  },
} })
function resultHarness(transport = async () => rawFile()) {
  let revision = 0
  let snapshot = { binding: B, authorityKey }
  const listeners = new Set()
  const projectClient = {
    downloadArtifact: vi.fn(transport), getSnapshot: () => snapshot,
    captureCatalogScope: () => { const captured = revision; return () => revision === captured },
    subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener) },
  }
  const save = vi.fn()
  const props = { running: false, result: { ok: true, result: resultFor() }, solarBinding: B,
    authorityKey, projectClient, catalogDigest: D, projectCurrent: true, save }
  return { props, projectClient, save, notify: (next = snapshot) => {
    snapshot = next; revision += 1; for (const listener of listeners) listener()
  } }
}
afterEach(() => {
  SolarReadResult.mockClear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('ResultPanel canonical project Solar dispatch', () => {
  it('R13E-11 recognizes a canonical project read result', () => {
    const h = resultHarness()
    expect(isSolarProjectReadResult(h.props.result.result)).toBe(true)
    expect(isSolarReadResult(h.props.result.result)).toBe(false)
    const view = render(<ResultPanel {...h.props} />)
    expect(screen.getByTestId('solar-read-result')).toBeInTheDocument()
    expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A')
    expect(view.container.querySelector('.result-card > table.kv')).toBeNull()
    for (const metadata of ['private-graph-project', B.project_id, 'leaf.solar-project-read.v1']) {
      expect(view.container.textContent).not.toContain(metadata)
    }
  })

  it('R13E-46 every canonical project read tool takes the canonical branch', () => {
    for (const tool of ['solar-string-data', 'solar-electrical-schedules', 'solar-cable-export']) {
      const h = resultHarness()
      const result = resultFor(tool)
      expect(isSolarProjectReadResult(result)).toBe(true)
      SolarReadResult.mockClear()
      const view = render(<ResultPanel {...h.props} result={{ ok: true, result }} />)
      expect(SolarReadResult.mock.calls.at(-1)[0].data).toBe(result)
      expect(SolarReadResult.mock.calls.at(-1)[0].projectClient).toBe(h.projectClient)
      expect(screen.getByTestId('solar-read-result')).toBeInTheDocument()
      expect(view.container.querySelector('.result-card > table.kv')).toBeNull()
      for (const metadata of ['private-graph-project', B.project_id, 'leaf.solar-project-read.v1']) {
        expect(view.container.textContent).not.toContain(metadata)
      }
      cleanup()
    }
  })

  it('R13E-12 forwards every R12 project prop without changing identity', () => {
    const h = resultHarness()
    const data = { ...resultFor(), is_head: false, head_version_id: B.organization_id, current_required: false }
    render(<ResultPanel {...h.props} result={{ ok: true, result: data }} projectCurrent={false} />)
    const supplied = SolarReadResult.mock.calls.at(-1)[0]
    expect(supplied.data).toBe(data)
    expect(supplied.solarBinding).toBe(B)
    expect(supplied.projectClient).toBe(h.projectClient)
    expect(supplied.save).toBe(h.save)
    expect(supplied.authorityKey).toBe(authorityKey)
    expect(supplied.catalogDigest).toBe(D)
    expect(supplied.projectCurrent).toBe(false)
    expect(screen.getByText('Historical version.')).toBeInTheDocument()
    expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A')
  })

  it('R13E-13 missing canonical dependencies refuse without legacy download', () => {
    const legacyFetch = vi.fn()
    vi.stubGlobal('fetch', legacyFetch)
    const cases = [
      ['solarBinding', 'SIP_R12_BINDING_INVALID'], ['authorityKey', 'SIP_R12_CLIENT_UNAVAILABLE'],
      ['projectClient', 'SIP_R12_CLIENT_UNAVAILABLE'], ['catalogDigest', 'SIP_R12_CATALOG_DIGEST_REQUIRED'],
      ['downloadArtifact', 'SIP_R12_CLIENT_UNAVAILABLE'], ['getSnapshot', 'SIP_R12_CLIENT_UNAVAILABLE'],
      ['subscribe', 'SIP_R12_CLIENT_UNAVAILABLE'], ['captureCatalogScope', 'SIP_R12_CLIENT_UNAVAILABLE'],
    ]
    for (const [key, reason] of cases) {
      const h = resultHarness()
      const props = { ...h.props }
      if (Object.hasOwn(props, key)) delete props[key]
      else props.projectClient = { ...h.projectClient, [key]: undefined }
      const view = render(<ResultPanel {...props} />)
      expect(screen.getByText(projectReadReason(reason))).toBeInTheDocument()
      expect(screen.queryByTestId('solar-read-download')).toBeNull()
      expect(view.container.innerHTML).not.toContain('/api/drawings/')
      expect(h.projectClient.downloadArtifact).not.toHaveBeenCalled()
      expect(h.save).not.toHaveBeenCalled()
      cleanup()
    }
    const h = resultHarness()
    render(<ResultPanel {...h.props} projectCurrent={undefined} save={undefined} />)
    expect(screen.getByTestId('solar-read-download')).toBeInTheDocument()
    expect(legacyFetch).not.toHaveBeenCalled()
  })

  it('R13E-14 a foreign project artifact cannot save through ResultPanel', () => {
    const h = resultHarness()
    const data = { ...resultFor(), output: { ...output, artifact: { ...ref, project_id: B.organization_id } } }
    render(<ResultPanel {...h.props} result={{ ok: true, result: data }} />)
    expect(screen.getByTestId('solar-read-headline').textContent).toBe('57.4 A')
    expect(screen.getByText(projectReadReason('SIP_R12_ARTIFACT_REF_INVALID'))).toBeInTheDocument()
    expect(screen.queryByTestId('solar-read-download')).toBeNull()
    expect(h.projectClient.downloadArtifact).not.toHaveBeenCalled()
    expect(h.save).not.toHaveBeenCalled()
  })

  it('R13E-15 a stale current artifact cannot save through ResultPanel', () => {
    const h = resultHarness()
    const data = { ...resultFor(), is_head: false, head_version_id: B.organization_id }
    render(<ResultPanel {...h.props} result={{ ok: true, result: data }} />)
    expect(screen.getByText(projectReadReason('SIP_R12_STALE_CURRENT'))).toBeInTheDocument()
    expect(screen.queryByTestId('solar-read-download')).toBeNull()
    expect(h.projectClient.downloadArtifact).not.toHaveBeenCalled()
    expect(h.save).not.toHaveBeenCalled()
  })

  it('R13E-16 changing scope during ResultPanel download prevents save', async () => {
    vi.stubGlobal('crypto', { subtle: { digest: async (_algorithm, bytes) =>
      new Uint8Array(createHash('sha256').update(bytes).digest()).buffer } })
    for (const change of ['binding', 'authority', 'notification']) {
      const pending = pendingFile()
      const h = resultHarness(() => pending.promise)
      const view = render(<ResultPanel {...h.props} />)
      fireEvent.click(screen.getByTestId('solar-read-download'))
      await waitFor(() => expect(h.projectClient.downloadArtifact).toHaveBeenCalledTimes(1))
      const signal = h.projectClient.downloadArtifact.mock.calls[0][1].signal
      if (change === 'binding') view.rerender(<ResultPanel {...h.props}
        solarBinding={{ ...B, input_version_id: B.organization_id }} />)
      if (change === 'authority') view.rerender(<ResultPanel {...h.props} authorityKey="epoch-2" />)
      if (change === 'notification') act(() => h.notify())
      expect(signal.aborted).toBe(true)
      await act(async () => { pending.resolve(rawFile()); await pending.promise })
      expect(screen.queryByText('Downloaded ElectricalSchedules.json.')).toBeNull()
      expect(h.save).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('R13E-17 standalone Solar dispatch preserves frozen rendered bytes', () => {
    const raw = readFileSync(resolve(process.cwd(), 'src/solar/__fixtures__/solarReadResultStandalone.main-a021056e.json'), 'utf8')
    expect(createHash('sha256').update(raw).digest('hex')).toBe('e3c8dafa5a78cd21c7ef21f07e3a17dab34fb8ae852b972a6992302ae5ca3ae9')
    const oracle = JSON.parse(raw)
    for (const entry of Object.values(oracle.cases)) {
      for (const extras of [{}, { solarBinding: B, authorityKey, projectClient: resultHarness().projectClient,
        catalogDigest: D, projectCurrent: false, save: vi.fn() }]) {
        const view = render(<ResultPanel running={false} result={{ ok: true, result: entry.input }} {...extras} />)
        const html = view.container.querySelector('[data-testid="solar-read-result"]').outerHTML
        expect(html).toBe(entry.html)
        expect(Buffer.byteLength(html, 'utf8')).toBe(entry.htmlBytes)
        expect(createHash('sha256').update(html).digest('hex')).toBe(entry.htmlSha256)
        cleanup()
      }
    }
  })
})
