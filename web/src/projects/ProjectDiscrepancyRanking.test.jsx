import { createHash, webcrypto } from 'node:crypto'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectSwitcher from '../components/ProjectSwitcher.jsx'
import { createWorkspaceController } from '../controllers/workspace/createWorkspaceController.js'
import { openProject } from '../api.js'
import { createEngineeringTotalsRecord, engineeringTotalsPath } from '../solar/projectReconciliation.js'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const uuid = n => '00000000-0000-4000-8000-' + String(n).padStart(12, '0')
const org = uuid(1)
const sha = s => createHash('sha256').update(s).digest('hex')
const cases = [['North Yard', ['5', '1', '0', '0', '0.5']],
  ['East Roof', ['2', '1', '0', '0', '2.3']], ['West Annex', null]].map(([name, values], i) => {
  const project = { project_id: uuid(10 + i), org_id: org, name, status: 'active' }
  const binding = { kind: 'project', organization_id: org, project_id: project.project_id,
    drawing_id: uuid(20 + i), input_version_id: uuid(30 + i) }
  const row = { seq: i + 1, version_id: binding.input_version_id, drawing_id: binding.drawing_id,
    org_id: org, project_id: binding.project_id }
  const panel = 'leaf:panel:' + uuid(40 + i)
  const graph = { rev: i + 1, panels: [{ id: panel }],
    frames: [{ id: 'leaf:frame:' + uuid(50 + i), panel_refs: [panel], module_power_watts: 400 }],
    strings: [], inverters: [] }
  const hash = sha(JSON.stringify(graph))
  const path = engineeringTotalsPath(binding)
  const content = values && JSON.stringify(createEngineeringTotalsRecord(binding, hash, values))
  const file = content && { file_id: uuid(60 + i), path, media_type: 'application/json',
    revision: 1, content, content_sha256: sha(content) }
  return { project, binding, row, graph, hash, path, file }
})


function fixtureTransport({ invalidContext, staleReport, omitNorthOnOpen } = {}) {
  const calls = []
  const opened = new Map()
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('fetch', async (target, init = {}) => {
    const url = new URL(target, 'http://localhost')
    calls.push({ url, method: init.method || 'GET', headers: new Headers(init.headers) })
    const current = cases.find(c => url.pathname.startsWith('/api/projects/' + c.project.project_id))
    if (!current) return new Response(null, { status: 404 })
    const { project, binding, row, graph, hash, path, file } = current
    if (url.pathname === '/api/projects/' + project.project_id) {
      const count = (opened.get(project.project_id) || 0) + 1
      opened.set(project.project_id, count)
      return Response.json({ project, drawing_versions: omitNorthOnOpen && current === cases[0] && count > 1
        ? [{ ...row, version_id: uuid(99) }] : [row] })
    }
    if (url.pathname.endsWith('/' + binding.input_version_id + '/context')) return Response.json({
      context: { ...binding, is_head: true, head_version_id: binding.input_version_id,
        ...(invalidContext && current === cases[0] ? { project_id: uuid(98) } : {}) },
      intake: { solar_design_graph: graph, solar_design_graph_sha256: hash },
    })
    if (url.searchParams.get('path') === path) return Response.json({
      organization_id: org, project_id: project.project_id, can_write: false,
      file: staleReport && current === cases[1] ? { ...file, content_sha256: sha('different report') } : file,
    })
    return new Response(null, { status: 404 })
  })
  return calls
}

function launch() {
  const storage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }
  const controller = createWorkspaceController({ storage, services: { openProject } })
  controller.adoptOrgId(org)
  render(<ProjectSwitcher orgId={org} principalId="fixture-member" storage={storage}
    projects={cases.map(entry => entry.project)} onOpenProject={controller.openProject} />)
  fireEvent.click(screen.getByRole('button', { name: /Projects: change project/ }))
  fireEvent.click(screen.getByRole('menuitem', { name: 'Rank engineering discrepancies' }))
  return controller
}

function metric(name) {
  return within(screen.getByText(name, { selector: 'dt' }).parentElement)
}

it('ranks each metric independently and selects the exact scoped saved revision', async () => {
  const requests = fixtureTransport()
  const controller = launch()
  await screen.findByText(/3 of 3 projects checked/)
  expect(metric('Panels').getAllByRole('menuitem').map(el => el.textContent.trim()))
    .toEqual(['North Yard, revision 1', 'East Roof, revision 2'])
  expect(metric('kW DC').getAllByRole('menuitem').map(el => el.textContent.trim()))
    .toEqual(['East Roof, revision 2', 'North Yard, revision 1'])
  expect(screen.getByRole('status').textContent).toMatch(/lack complete comparison evidence/)
  expect(metric('Strings').getByText('No verified discrepancy found')).toBeTruthy()
  expect(requests.every(request => request.method === 'GET' && request.headers.get('X-Org-Id') === org)).toBe(true)
  fireEvent.click(metric('kW DC').getByRole('menuitem', { name: 'East Roof, revision 2' }))
  await vi.waitFor(() => {
    expect(controller.getSnapshot().openProjectId).toBe(cases[1].project.project_id)
    expect(controller.getSnapshot().canonicalVersionId).toBe(cases[1].row.version_id)
  })
})

it('excludes mismatched context and an invalid saved report from the rankings', async () => {
  const requests = fixtureTransport({ invalidContext: true, staleReport: true })
  launch()
  await screen.findByText(/3 of 3 projects checked/)
  expect(screen.getByRole('status').textContent).toMatch(/3 revisions lack complete comparison evidence/)
  expect(screen.queryByRole('menuitem', { name: /revision [123]/ })).toBeNull()
  expect(requests.some(request => request.url.searchParams.get('path') === cases[0].path)).toBe(false)
})

it('reports an unavailable exact revision without selecting a replacement', async () => {
  fixtureTransport({ omitNorthOnOpen: true })
  const controller = launch()
  await screen.findByText(/3 of 3 projects checked/)
  fireEvent.click(metric('Panels').getByRole('menuitem', { name: 'North Yard, revision 1' }))
  expect((await screen.findByRole('alert')).textContent).toBe('This saved revision could not be opened.')
  expect(controller.getSnapshot().canonicalVersionId).toBeNull()
})

it('drops a slow exact revision open when a newer workspace open wins', async () => {
  const storage = { getItem: () => null, setItem: () => {}, removeItem: () => {} }
  let finish
  const controller = createWorkspaceController({ storage, services: {
    openProject: id => id === cases[0].project.project_id ? new Promise(resolve => { finish = resolve })
      : Promise.resolve({ project: cases[1].project, drawing_versions: [cases[1].row] }),
  } })
  controller.adoptOrgId(org)
  const slow = controller.openProject(cases[0].project.project_id, cases[0].binding)
  await controller.openProject(cases[1].project.project_id, cases[1].binding)
  finish({ project: cases[0].project, drawing_versions: [cases[0].row] })
  expect(await slow).toBeNull()
  expect(controller.getSnapshot().openProjectId).toBe(cases[1].project.project_id)
  expect(controller.getSnapshot().canonicalVersionId).toBe(cases[1].row.version_id)
})
