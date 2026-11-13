import React from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
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


describe('ResultPanel row arrays', () => {
  it('renders every layer and bound in supplied order alongside the scalar summary', () => {
    const layers = [
      { layer: 'West roof', min_x: -1234.56789123, max_x: '2,345.6000 mm', min_y: 0, max_y: 9.87654321, geometry_count: 17 },
      { layer: 'East roof', min_x: -4, max_x: 5, min_y: -6, max_y: 7, geometry_count: 8 },
      { layer: 'Annex', min_x: 1, max_x: 2, min_y: 3, max_y: 4, geometry_count: 9 },
    ]
    render(<ResultPanel result={{ ok: true, result: { layers, layer_count: layers.length } }} density="comfortable" />)
    const table = screen.getByRole('table', { name: 'layers' })
    expect(table).toHaveAttribute('data-density', 'comfortable')
    expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent))
      .toEqual(['layer', 'min x', 'max x', 'min y', 'max y', 'geometry count'])
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((row) => within(row).getAllByRole('cell').map((cell) => cell.textContent)))
      .toEqual(layers.map((layer) => Object.values(layer).map(String)))
    const summary = screen.getByRole('rowheader', { name: 'layer count' }).closest('tr')
    expect(within(summary).getByRole('cell')).toHaveTextContent('3')
    expect(screen.queryByText('Completed with no output.')).toBeNull()
  })

  it('shows nested report arrays and nested bounds, including units and formatted values', () => {
    render(<ResultPanel result={{ ok: true, result: { report: { regions: [
      { name: 'Upper deck', bounds: { min_x: '-12.3400', max_x: 45.67891234, units: 'mm', origin: [1.25, -2.5] }, area_sqft: '1,200.00' },
      { name: 'Lower deck', bounds: { min_x: 0, max_x: 3, units: 'mm' }, note: 'Survey pending' },
    ] } } }} />)
    const table = screen.getByRole('table', { name: 'report / regions' })
    expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent))
      .toEqual(['name', 'bounds / min x', 'bounds / max x', 'bounds / units', 'bounds / origin', 'area (sqft)', 'note'])
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((row) => within(row).getAllByRole('cell').map((cell) => cell.textContent))).toEqual([
      ['Upper deck', '-12.3400', '45.67891234', 'mm', '[1.25,-2.5]', '1,200.00', ''],
      ['Lower deck', '0', '3', 'mm', '', '', 'Survey pending'],
    ])
  })

  it('discovers separately labelled layer tables inside outer row arrays', () => {
    render(<ResultPanel result={{ ok: true, result: { report: { groups: [
      { name: 'North survey', layers: [
        { name: 'Upper roof', bounds: { min_x: '-12.3400 mm', max_x: 45.67891234 } },
        { name: 'Lower roof', bounds: { min_x: 0, max_x: 3 } },
      ] },
      { name: 'South survey', layers: [{ name: 'Annex roof', bounds: { min_x: -1, max_x: 2 } }] },
    ] } } }} />)
    expect(screen.getAllByRole('table')).toHaveLength(3)
    const groups = screen.getByRole('table', { name: 'report / groups' })
    expect(within(groups).getAllByRole('row').slice(1).map((row) => row.textContent))
      .toEqual(['North survey2 rows', 'South survey1 row'])
    const north = screen.getByRole('table', { name: 'report / groups / 0 / layers' })
    expect(within(north).getAllByRole('row').slice(1).map((row) =>
      within(row).getAllByRole('cell').map((cell) => cell.textContent)))
      .toEqual([['Upper roof', '-12.3400 mm', '45.67891234'], ['Lower roof', '0', '3']])
    const south = screen.getByRole('table', { name: 'report / groups / 1 / layers' })
    expect(within(south).getByRole('cell', { name: 'Annex roof' })).toBeInTheDocument()
  })

  it('mounts child tables only for the current parent page while keeping every child reachable', () => {
    const groups = Array.from({ length: 1000 }, (_, index) => ({
      name: `Survey ${index}`, layers: [{ name: `Roof ${index}` }],
    }))
    const { container } = render(<ResultPanel result={{ ok: true, result: { report: { groups } } }} />)
    const table = screen.getByRole('table', { name: 'report / groups' })
    expect(screen.queryByRole('table', { name: 'report / groups / 100 / layers' })).toBeNull()
    const visited = []
    for (let page = 0; page < 10; page++) {
      const tables = Array.from(container.querySelectorAll('table'))
      expect(tables).toHaveLength(101)
      expect(table.querySelectorAll('tbody tr')).toHaveLength(100)
      visited.push(...tables.filter((child) => child !== table).map((child) => child.querySelector('tbody td').textContent))
      if (page < 9) fireEvent.click(screen.getByRole('button', { name: 'Next report / groups page' }))
    }
    expect(visited).toEqual(groups.map((group) => group.layers[0].name))
    expect(screen.queryByRole('table', { name: 'report / groups / 0 / layers' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Next report / groups page' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Previous report / groups page' }))
    expect(screen.getByRole('table', { name: 'report / groups / 800 / layers' })).toHaveTextContent('Roof 800')
  })

  it('caps wide tables at 50 columns with all fields available per row while keeping pagination', () => {
    const layers = Array.from({ length: 105 }, (_, row) => Object.fromEntries([
      ['name', `Layer ${row}`],
      ...Array.from({ length: 55 }, (_, column) => [`field_${column}`, `${row}:${column}`]),
    ]))
    layers[104].last_field = 'Only on the last row'
    render(<ResultPanel result={{ ok: true, result: { layers } }} />)
    const table = screen.getByRole('table', { name: 'layers' })
    const headers = within(table).getAllByRole('columnheader').map((cell) => cell.textContent)
    expect(headers).toEqual(['name', ...Array.from({ length: 49 }, (_, index) => `field ${index}`)])
    expect(within(table).getAllByRole('cell')).toHaveLength(5000)
    expect(screen.getByText("7 more fields available in each row's All fields.")).toBeInTheDocument()
    expect(within(table).queryByText('0:54')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'All fields for layers row 1' }))
    expect(within(table).getByText('field 54')).toBeInTheDocument()
    expect(within(table).getByText('0:54')).toBeInTheDocument()
    expect(within(table).queryByText('1:54')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Hide all fields for layers row 1' }))
    expect(within(table).queryByText('0:54')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Next layers page' }))
    expect(within(table).getAllByRole('cell')).toHaveLength(250)
    expect(within(table).getByText('Layer 104')).toBeInTheDocument()
    expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual(headers)
    expect(screen.getByText("7 more fields available in each row's All fields.")).toBeInTheDocument()
    expect(screen.getByText('Rows 101 to 105 of 105')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next layers page' })).toBeDisabled()
    expect(within(table).queryByText('Only on the last row')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'All fields for layers row 105' }))
    expect(within(table).getByText('last field')).toBeInTheDocument()
    expect(within(table).getByText('Only on the last row')).toBeInTheDocument()
    expect(within(table).getByText('104:54')).toBeInTheDocument()
  })

  it('keeps arrays at the discovery depth boundary as paged tables alongside earlier arrays', () => {
    const layers = Array.from({ length: 123 }, (_, index) => ({ name: `Boundary roof ${index}`, min_x: `${index}.0000 mm` }))
    let report = { layers }
    for (let index = 0; index < 18; index++) report = { nested: report }
    const label = ['report', ...Array(18).fill('nested'), 'layers'].join(' / ')
    render(<ResultPanel result={{ ok: true, result: { layers: [{ name: 'Shallow roof' }], report } }} />)
    expect(screen.getByRole('table', { name: 'layers' })).toHaveTextContent('Shallow roof')
    const table = screen.getByRole('table', { name: label })
    expect(screen.getAllByRole('table')).toHaveLength(2)
    expect(table.querySelectorAll('tbody tr')).toHaveLength(100)
    expect(within(table).getByRole('cell', { name: 'Boundary roof 0' })).toBeInTheDocument()
    expect(screen.queryByText('Boundary roof 122')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: `Next ${label} page` }))
    expect(table.querySelectorAll('tbody tr')).toHaveLength(23)
    expect(within(table).getByRole('cell', { name: 'Boundary roof 122' })).toBeInTheDocument()
    expect(within(table).getByRole('cell', { name: '122.0000 mm' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: `Next ${label} page` })).toBeDisabled()
  })

  it('bounds discovery, flattened fields and JSON cell serialization for very deep results', () => {
    let deep = { layers: [{ name: 'Beyond display depth' }] }
    for (let index = 0; index < 12000; index++) deep = { nested: deep }
    const { container } = render(<ResultPanel result={{ ok: true, result: {
      report: deep,
      layers: [{ name: 'Deep bounds', bounds: deep, coordinates: [0, deep] }],
    } }} />)
    const table = screen.getByRole('table', { name: 'layers' })
    expect(within(table).getByRole('cell', { name: 'Deep bounds' })).toBeInTheDocument()
    expect(within(table).getAllByRole('columnheader').every((cell) => cell.textContent.split(' / ').length <= 20)).toBe(true)
    expect(table.querySelector('tbody')).toHaveTextContent('[Truncated]')
    expect(screen.getByText(/^report \/ nested.*\[Truncated\]/)).toBeInTheDocument()
    expect(container.textContent.length).toBeLessThan(10000)
    expect(screen.queryByText('Beyond display depth')).toBeNull()
    expect(screen.queryByText('Completed with no output.')).toBeNull()
  })

  it('renders cyclic cell values as text without recursive discovery failing', () => {
    const cycle = { name: 'Cycle point' }
    cycle.self = cycle
    render(<ResultPanel result={{ ok: true, result: { layers: [{ name: 'Cyclic bounds', coordinates: [0, cycle] }] } }} />)
    const table = screen.getByRole('table', { name: 'layers' })
    expect(within(table).getByRole('cell', { name: 'Cyclic bounds' })).toBeInTheDocument()
    expect(within(table).getByRole('cell', { name: /\[Circular\]/ })).toHaveTextContent('Cycle point')
  })

  it('reads only own properties for fields missing on other rows, including constructor paths', () => {
    const own = JSON.parse('{"name":"Own fields","constructor":{"name":"Reported constructor"},"bounds":{"constructor":{"name":"Reported bound constructor"}},"__proto__":{"name":"Reported prototype"}}')
    const inherited = Object.assign(Object.create({
      constructor: { name: 'Inherited constructor' },
      bounds: { constructor: { name: 'Inherited bounds' } },
      hidden: 'Inherited field',
    }), { name: 'Inherited fields' })
    render(<ResultPanel result={{ ok: true, result: { layers: [own, { name: 'Missing fields' }, inherited] } }} />)
    const table = screen.getByRole('table', { name: 'layers' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((row) => within(row).getAllByRole('cell').map((cell) => cell.textContent))).toEqual([
      ['Own fields', 'Reported constructor', 'Reported bound constructor', 'Reported prototype'],
      ['Missing fields', '', '', ''],
      ['Inherited fields', '', '', ''],
    ])
    expect(within(table).queryByRole('columnheader', { name: 'hidden' })).toBeNull()
  })

  it('keeps scalar summaries alongside both counts and layer rows', () => {
    render(<ResultPanel result={{ ok: true, result: {
      counts: { Inspected: 4 }, total: 4, layer_count: 2, status: 'Survey complete',
      layers: [{ name: 'North roof' }, { name: 'South roof' }],
    } }} />)
    expect(screen.getByRole('cell', { name: 'Inspected' })).toBeInTheDocument()
    expect(screen.getByText('Total:')).toHaveTextContent('Total: 4')
    expect(screen.queryByRole('rowheader', { name: 'total' })).toBeNull()
    expect(screen.getByRole('rowheader', { name: 'layer count' }).closest('tr')).toHaveTextContent('2')
    expect(screen.getByRole('rowheader', { name: 'status' }).closest('tr')).toHaveTextContent('Survey complete')
    const table = screen.getByRole('table', { name: 'layers' })
    expect(within(table).getAllByRole('cell').map((cell) => cell.textContent)).toEqual(['North roof', 'South roof'])
  })

  it('bounds a large array in the DOM while allowing every row to be reached in order', () => {
    const layers = Array.from({ length: 237 }, (_, index) => ({ name: `Layer ${index}`, min_x: index + 0.123456789 }))
    layers[236].note = 'Last surveyed layer'
    const { rerender } = render(<ResultPanel result={{ ok: true, result: { layers } }} />)
    const table = screen.getByRole('table', { name: 'layers' })
    const namesOnPage = () => within(table).getAllByRole('row').slice(1)
      .map((row) => within(row).getAllByRole('cell')[0].textContent)
    expect(within(table).getByRole('columnheader', { name: 'note' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Previous layers page' })).toBeDisabled()
    const visited = []
    for (const count of [100, 100, 37]) {
      expect(table.querySelectorAll('tbody tr')).toHaveLength(count)
      visited.push(...namesOnPage())
      if (visited.length < layers.length) fireEvent.click(screen.getByRole('button', { name: 'Next layers page' }))
    }
    expect(visited).toEqual(layers.map((row) => row.name))
    expect(screen.getByText('Rows 201 to 237 of 237')).toBeInTheDocument()
    expect(within(table).getByRole('cell', { name: 'Last surveyed layer' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next layers page' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Previous layers page' }))
    expect(namesOnPage()).toEqual(layers.slice(100, 200).map((row) => row.name))
    rerender(<ResultPanel result={{ ok: true, result: { layers: layers.slice(0, 150) } }} />)
    expect(screen.getByText('Rows 1 to 100 of 150')).toBeInTheDocument()
    expect(namesOnPage()).toEqual(layers.slice(0, 100).map((row) => row.name))
  })

  it.each([{}, { layer_count: 0 }])('labels an empty array without claiming no output (%j)', (summary) => {
    render(<ResultPanel result={{ ok: true, result: { layers: [], ...summary } }} />)
    expect(screen.getByText('layers: No rows.')).toBeInTheDocument()
    expect(screen.queryByRole('table', { name: 'layers' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Next layers page' })).toBeNull()
    expect(screen.queryByText('Completed with no output.')).toBeNull()
    if ('layer_count' in summary) {
      expect(screen.getByRole('rowheader', { name: 'layer count' }).closest('tr')).toHaveTextContent('0')
    }
  })

  it.each([
    { counts: { Inspected: 2 }, total: 2 },
    { table: { columns: ['Inspection'], rows: [['Complete']] }, layer_count: 1 },
  ])('keeps row arrays visible beside existing specialized tables (%j)', (data) => {
    render(<ResultPanel result={{ ok: true, result: { ...data, layers: [{ name: 'Measured roof', width_ft: '12.5000' }] } }} />)
    const table = screen.getByRole('table', { name: 'layers' })
    expect(within(table).getByRole('cell', { name: 'Measured roof' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: 'width (ft)' })).toBeInTheDocument()
    expect(screen.getAllByRole('table').length).toBeGreaterThan(1)
  })

  it('renders row labels, field names and values as text', () => {
    const markup = '<img src=x onerror="alert(1)">'
    const { container } = render(<ResultPanel result={{ ok: true, result: {
      [markup]: [{ name: markup, bounds: { [markup]: '<svg onload="alert(2)">' } }],
    } }} />)
    const table = screen.getByRole('table', { name: markup })
    expect(within(table).getByRole('cell', { name: markup })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: `bounds / ${markup}` })).toBeInTheDocument()
    expect(within(table).getByRole('cell', { name: '<svg onload="alert(2)">' })).toBeInTheDocument()
    expect(container.querySelector('img, svg, script')).toBeNull()
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

describe('ResultPanel version label', () => {
  // The shape of a synchronous project write: version is the new canonical drawing version.
  const projectWrite = { ok: true, tool: 'delete-marked-panel',
    version: { version_id: '66666666-6666-4666-8666-666666666666', drawing_id: '33333333-3333-4333-8333-333333333333',
      project_id: '22222222-2222-4222-8222-222222222222', org_id: '11111111-1111-4111-8111-111111111111', seq: 2 },
    result: { deleted_handle: 'ABCD', added_marker_handle: 'ABCE' }, overlay: null,
    project_context: { input_version_id: '44444444-4444-4444-8444-444444444444' } }

  it('renders a project write whose version is an object, naming the drawing version', () => {
    render(<ResultPanel running={false} result={projectWrite} />)
    expect(screen.getByText('Passed')).toBeTruthy()
    expect(screen.getByText('drawing version 2 (66666666)')).toBeTruthy()
  })

  it('keeps a primitive tool version exactly as before', () => {
    render(<ResultPanel running={false} result={{ ok: true, tool: 'count-by-layer', version: '1.0.0', result: { total: 1 } }} />)
    expect(screen.getByText('v1.0.0')).toBeTruthy()
  })

  it('does not invent a version from a malformed object', () => {
    render(<ResultPanel running={false} result={{ ok: true, tool: 'delete-marked-panel', version: { seq: 'two' }, result: {} }} />)
    expect(screen.getByText('drawing version not reported')).toBeTruthy()
  })
})
