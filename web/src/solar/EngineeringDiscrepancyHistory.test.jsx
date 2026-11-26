import { createHash, webcrypto } from 'node:crypto'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ProjectReconciliationPanel from './ProjectReconciliationPanel.jsx'
import { createEngineeringTotalsRecord, engineeringTotalsPath } from './projectReconciliation.js'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const uuid = n => '00000000-0000-4000-8000-' + String(n).padStart(12, '0')
const sha = value => createHash('sha256').update(value).digest('hex')
const base = { kind: 'project', organization_id: uuid(1), project_id: uuid(2), drawing_id: uuid(3) }
const versionBinding = seq => ({ ...base, input_version_id: uuid(100 + seq) })
const version = (seq, count) => {
  const binding = versionBinding(seq)
  const graph = { rev: seq, panels: [{ id: 'leaf:panel:' + uuid(5) }], frames: [], strings: [], inverters: [] }
  const hash = sha(JSON.stringify(graph))
  const path = engineeringTotalsPath(binding)
  const record = count === null ? null : createEngineeringTotalsRecord(binding, hash, [count, '0', '0', '0', ''])
  const content = record && JSON.stringify(record)
  const file = content && { file_id: uuid(200 + seq), path, media_type: 'application/json',
    revision: 1, content, content_sha256: sha(content) }
  return { seq, binding, graph, hash, path, file,
    row: { seq, version_id: binding.input_version_id, drawing_id: binding.drawing_id,
      org_id: binding.organization_id, project_id: binding.project_id } }
}

function fixture(versions, selected = versions.at(-1), { beforeWorkspace } = {}) {
  const workspace = { project: { org_id: base.organization_id, project_id: base.project_id },
    drawing_versions: [{ ...versions[0].row, drawing_id: uuid(4) }, ...versions.map(item => item.row)].reverse() }
  vi.stubGlobal('crypto', webcrypto)
  const calls = []
  vi.stubGlobal('fetch', async (target, init) => {
    const url = new URL(target, 'http://localhost')
    calls.push({ url, method: init.method ?? 'GET' })
    if (url.pathname === `/api/projects/${base.project_id}`) {
      await beforeWorkspace?.()
      return Response.json(workspace)
    }
    const design = versions.find(item => url.pathname.endsWith('/' + item.binding.input_version_id + '/context'))
    if (design) return Response.json({ context: { ...design.binding, is_head: design === versions.at(-1),
      head_version_id: versions.at(-1).binding.input_version_id },
    intake: { solar_design_graph: design.graph, solar_design_graph_sha256: design.hash } })
    const report = versions.find(item => url.searchParams.get('path') === item.path)
    if (!report) return new Response(null, { status: 404 })
    return Response.json({ organization_id: base.organization_id, project_id: base.project_id,
      can_write: false, file: report.file })
  })
  const renderPanel = (current, authorityKey) => <ProjectReconciliationPanel solarBinding={current.binding}
    authorityKey={authorityKey} isCurrent={() => true}
    projectClient={{ getSnapshot: () => ({ binding: current.binding, authorityKey }), readContext: () => {},
      subscribe: () => () => {} }} />
  const view = render(renderPanel(selected, 'user-a'))
  const read = () => fireEvent.click(screen.getByRole('button', { name: 'Read saved discrepancy history' }))
  return { read, calls, retarget: (current, authorityKey) => view.rerender(renderPanel(current, authorityKey)) }
}
const history = () => screen.findByRole('table', { name: 'Saved engineering discrepancies by drawing revision' })
const revision = (table, seq) => within(table).getByRole('rowheader', { name: new RegExp(`^Revision ${seq}( |$)`) }).closest('tr')

it('reads sorted versions and reports a consecutive discrepancy onset and resolution', async () => {
  const h = fixture([version(1, '1'), version(2, '2'), version(3, '1')])
  h.read()
  const table = await history()
  expect(revision(table, 1)).toHaveTextContent('Matches')
  expect(revision(table, 2)).toHaveTextContent('Discrepancy appeared (-1)')
  expect(revision(table, 3)).toHaveTextContent('Discrepancy resolved')
  expect(h.calls).toHaveLength(7)
  expect(h.calls.every(call => call.method === 'GET')).toBe(true)
})

it('shows unavailable totals and skips transition claims across missing reports and revisions', async () => {
  const h = fixture([version(1, '2'), version(2, null), version(3, '1'), version(5, '2')])
  h.read()
  const table = await history()
  expect(revision(table, 2)).toHaveTextContent('No saved engineering totals')
  expect(revision(table, 3)).toHaveTextContent('Matches')
  expect(revision(table, 5)).toHaveTextContent('Earlier revision evidence missing')
  expect(revision(table, 5)).toHaveTextContent('Discrepancy (-1)')
  expect(table).not.toHaveTextContent(/appeared|resolved/)
})

it('limits reads to the latest 12 versions through the selected revision', async () => {
  const versions = Array.from({ length: 15 }, (_, index) => version(index + 1, '1'))
  const h = fixture(versions, versions[13])
  h.read()
  const table = await history()
  expect(screen.getByText(/Showing the latest 12/)).toBeVisible()
  expect(within(table).getAllByRole('rowheader')).toHaveLength(12)
  expect(revision(table, 3)).toBeVisible()
  expect(revision(table, 14)).toBeVisible()
  expect(h.calls).toHaveLength(25)
})

it('does not read or show an old workspace response after an authority change', async () => {
  let release
  const deferred = new Promise(resolve => { release = resolve })
  const selected = version(1, '1')
  const h = fixture([selected], selected, { beforeWorkspace: () => deferred })
  h.read()
  h.retarget(selected, 'user-b')
  await act(async () => release())
  expect(h.calls).toHaveLength(1)
  expect(screen.queryByText(/Reading saved discrepancy history/)).toBeNull()
  expect(screen.queryByRole('table', { name: /engineering discrepancies/ })).toBeNull()
})
