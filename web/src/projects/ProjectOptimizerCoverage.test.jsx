import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectLifecyclePanel from './ProjectLifecyclePanel.jsx'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const uuid = n => '00000000-0000-4000-8000-' + String(n).padStart(12, '0')
const orgId = uuid(1), projectId = uuid(2)
const scope = { org_id: orgId, project_id: projectId }
const view = id => <ProjectLifecyclePanel enabled projectId={id} />
const check = () => fireEvent.click(screen.getByRole('button', { name: 'Check optimizer coverage' }))
const table = () => screen.findByRole('table', { name: 'Optimizer evidence on current saved revisions' })
const row = name => within(screen.getByRole('table')).getByRole('rowheader', { name }).closest('tr')

function design(n, name, status, seq = 1) {
  const drawingId = uuid(10 + n), versionId = uuid(1000 + n * 10 + seq)
  const graph = { rev: seq, panels: [], frames: [], strings: [], inverters: [] }
  if (['positive', 'zero', 'stale'].includes(status)) {
    const panel = { id: 'leaf:panel:' + uuid(900), provenance: { source_handle: 'A1' } }
    const frame = { id: 'leaf:frame:' + uuid(901), panel_refs: [panel.id], module_power_watts: 400 }
    const positive = status !== 'zero'
    if (positive) { graph.panels.push(panel); graph.frames.push(frame) }
    graph.extra = { solaredge_import: { schema: 'leaf.solar-project-solaredge-import.v1',
      source: { artifact_id: 'a'.repeat(64), content_sha256: 'b'.repeat(64) },
      report: { artifact_id: 'c'.repeat(64), content_sha256: 'd'.repeat(64), graph_sha256: 'e'.repeat(64) },
      frames: positive ? [{ frame_ref: frame.id, pdf_matrix: 0, pdf_grid: 0 }] : [],
      strings: positive ? [{ index: 0, source: 'group', frame_ref: frame.id, pdf_matrix: 0,
        partial: false, panel_refs: [panel.id] }] : [],
      unassigned_panel_refs: [],
      optimizer_measurement: { schema: 'leaf.solaredge-optimizer-measurement.v1',
        graph_rev: status === 'stale' ? seq + 1 : seq, complete: true,
        total_count: Number(positive), string_counts: positive ? [1] : [],
        optimizers: positive ? [{ id: 'pdf-optimizer-0', vendor_label: '42', pdf_matrix: 0,
          panel_refs: [panel.id], panel_handles: ['A1'], string_index: 0 }] : [] } } }
  }
  return { status, graph,
    drawing: { ...scope, drawing_id: drawingId, name },
    version: { ...scope, drawing_id: drawingId, version_id: versionId, seq } }
}

function fixture(designs, beforeWorkspace) {
  const calls = []
  vi.stubGlobal('fetch', async (target, init) => {
    const url = new URL(target, 'http://localhost')
    const headers = new Headers(init?.headers)
    calls.push({ url, method: init?.method ?? 'GET', org: headers.get('x-org-id') })
    const requestedProject = url.pathname.split('/')[3]
    const project = { ...scope, project_id: requestedProject, name: 'Project ' + requestedProject, status: 'active' }
    if (url.pathname.endsWith('/lifecycle')) {
      return Response.json({ project, viewer: { role: 'read_only', binding_id: uuid(3),
        membership_id: uuid(4) }, members: [], files: [], receipts: [] })
    }
    if (url.pathname.endsWith('/' + requestedProject)) {
      await beforeWorkspace?.(requestedProject)
      const rows = requestedProject === projectId ? designs : []
      return Response.json({ project, drawing_versions: rows.map(item => item.version),
        drawing_artifacts: [...new Map(rows.map(item => [item.drawing.drawing_id, item.drawing])).values()] })
    }
    const item = designs.find(({ version }) => url.pathname.endsWith('/' + version.version_id + '/context'))
    if (!item || item.status === 'unreadable') return new Response(null, { status: 503 })
    const { version } = item
    const actualDrawing = item.status === 'wrong-scope' ? uuid(777) : version.drawing_id
    const head = item.status === 'changed' ? uuid(888) : version.version_id
    return Response.json({ context: { organization_id: orgId, project_id: projectId,
      drawing_id: actualDrawing, input_version_id: version.version_id, head_version_id: head,
      is_head: item.status !== 'changed' },
    intake: { solar_design_graph: item.graph, solar_design_graph_sha256: 'f'.repeat(64) } })
  })
  return { calls, ...render(view(projectId)) }
}

it('checks exact saved heads and distinguishes verified positive and zero from missing counts', async () => {
  const old = design(1, 'North', 'positive')
  const north = design(1, 'North', 'missing', 2)
  const { calls } = fixture([north, design(2, 'South', 'positive'), design(3, 'West', 'zero'), old])
  await screen.findByRole('heading', { name: 'Optimizer measurement coverage' })
  expect(calls).toHaveLength(1)
  check()
  await table()
  expect(row('North')).toHaveTextContent('2Missing measured optimizer count')
  expect(row('South')).toHaveTextContent('1 measured')
  expect(row('West')).toHaveTextContent('0 measured')
  expect(calls.some(({ url }) => url.pathname.includes(old.version.version_id))).toBe(false)
  expect(calls.slice(1).every(({ method, org }) => method === 'GET' && org === orgId)).toBe(true)
  expect(calls.find(({ url }) => url.pathname.includes(north.version.version_id)).url.searchParams.get('drawing_id'))
    .toBe(north.drawing.drawing_id)
})

it('keeps invalid evidence, changed heads, scope mismatch and failed reads distinct from measured rows', async () => {
  fixture([design(1, 'Stale', 'stale'), design(2, 'Changed', 'changed'), design(3, 'Unreadable', 'unreadable'),
    design(4, 'Wrong scope', 'wrong-scope'), design(5, 'Verified', 'positive')])
  await screen.findByRole('heading', { name: 'Optimizer measurement coverage' })
  check()
  await table()
  expect(row('Stale')).toHaveTextContent('Missing verified optimizer count')
  expect(row('Changed')).toHaveTextContent('Saved revision changed')
  expect(row('Unreadable')).toHaveTextContent('Saved design unavailable')
  expect(row('Wrong scope')).toHaveTextContent('Saved design unavailable')
  expect(row('Verified')).toHaveTextContent('1 measured')
})

it('drops an older project response on project switch and reports an empty project', async () => {
  let release
  const delayed = new Promise(resolve => { release = resolve })
  const { calls, rerender } = fixture([design(1, 'North', 'positive')], id => id === projectId ? delayed : undefined)
  await screen.findByRole('heading', { name: 'Optimizer measurement coverage' })
  check()
  rerender(view(uuid(5)))
  await screen.findByRole('button', { name: 'Check optimizer coverage' })
  check()
  await screen.findByText('No saved drawing revisions.')
  await act(async () => release())
  expect(calls.some(({ url }) => url.pathname.endsWith('/context'))).toBe(false)
  expect(screen.queryByText('North')).toBeNull()
})

it('allows all saved drawings to be checked in bounded batches', async () => {
  const { calls } = fixture(Array.from({ length: 21 }, (_, index) =>
    design(index + 1, 'Drawing ' + String(index + 1).padStart(2, '0'), 'missing')))
  await screen.findByRole('heading', { name: 'Optimizer measurement coverage' })
  check()
  const coverage = await table()
  expect(within(coverage).getAllByRole('rowheader')).toHaveLength(20)
  expect(screen.getByText(/Checked 20 of 21 saved drawings/)).toBeVisible()
  expect(calls.filter(({ url }) => url.pathname.endsWith('/context'))).toHaveLength(20)
  fireEvent.click(screen.getByRole('button', { name: 'Check more drawings' }))
  expect(await screen.findByRole('rowheader', { name: 'Drawing 21' })).toBeVisible()
  expect(within(coverage).getAllByRole('rowheader')).toHaveLength(21)
  expect(calls.filter(({ url }) => url.pathname.endsWith('/context'))).toHaveLength(21)
})
