import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import ProjectReconciliationPanel from './ProjectReconciliationPanel.jsx'

afterEach(() => { cleanup(); vi.useRealTimers() })

function revision(rev, count) {
  const binding = {
    kind: 'project',
    organization_id: '11111111-1111-4111-8111-111111111111',
    project_id: '22222222-2222-4222-8222-222222222222',
    drawing_id: '33333333-3333-4333-8333-333333333333',
    input_version_id: '44444444-4444-4444-8444-' + String(rev).padStart(12, '0'),
  }
  const id = (kind, index) => 'leaf:' + kind + ':00000000-0000-4000-8000-' + String(index + 1).padStart(12, '0')
  const panels = Array.from({ length: count }, (_, i) => ({ id: id('panel', i) }))
  const graph = {
    rev, panels,
    frames: [{ id: id('frame', 0), panel_refs: panels.map(panel => panel.id), module_power_watts: 500 }],
    strings: panels.map((panel, i) => ({ id: id('string', i), module_count: 1, ordered_panel_refs: [panel.id] })),
    inverters: Array.from({ length: count - 1 }, (_, i) => ({ id: id('inverter', i), equipment_type: 'central_inverter' })),
  }
  return { binding, graph }
}

it.each([
  ['Edit my report on the third visit before its result arrives, then ignore both early visits.', [8, 8], [2, 1, 0]],
  ['Keep my pending report edits when an older result arrives on either side of the current one.', [8, 9], [1, 2, 0]],
  ['Use the edits I made during the last read even when both earlier reads finish first.', [9, 8], [0, 1, 2]],
])('%s', async (_ask, detours, order) => {
  const visits = [revision(7, 1), revision(7, 3), revision(7, 5)]
  for (const visit of visits) visit.binding = visits[0].binding
  const stops = detours.map(rev => revision(rev, 2))
  const versions = [...visits, ...stops]
  const originals = structuredClone(versions)
  let selected = visits[0]
  const requests = []
  const isCurrent = () => true
  const client = {
    getSnapshot: () => ({ binding: selected.binding, authorityKey: 'review' }),
    subscribe: () => () => {},
    readContext: () => {
      // Capture the revision at request time. This transport still replies after cancellation.
      const { binding, graph } = selected
      return new Promise((resolve, reject) => requests.push({ fail: reject, succeed: () => resolve({ ok: true, data: {
        context: binding, intake: { solar_design_graph: graph, solar_design_graph_sha256: String(graph.rev).repeat(64) },
      } }) }))
    },
  }
  const panel = () => <ProjectReconciliationPanel solarBinding={selected.binding} authorityKey="review"
    projectClient={client} isCurrent={isCurrent} />
  const view = render(panel())
  const read = () => fireEvent.click(screen.getByRole('button', { name: 'Read saved design' }))
  const metrics = ['Panels', 'Optimizers', 'Inverters', 'Strings', 'kW DC']
  let report
  for (const [i, visit] of visits.entries()) {
    if (i) {
      selected = stops[i - 1]
      view.rerender(panel())
      read()
      await act(async () => { requests.pop().succeed() })
      expect(screen.getByText('Saved design revision ' + selected.graph.rev + '.')).toBeTruthy()
    }
    selected = visit
    view.rerender(panel())
    report = metrics.map((_, column) => String(10 * (i + 1) + column))
    for (const [column, metric] of metrics.entries()) {
      const input = screen.getByRole('textbox', { name: metric + ' engineering report/BOM' })
      expect(input.value).toBe('')
      fireEvent.change(input, { target: { value: report[column] } })
    }
    read()
    expect(requests).toHaveLength(i + 1)
  }
  report = ['5', '9', '2', '7', '1.75']
  expect(screen.getByText('Reading saved design.')).toBeTruthy()
  const inputs = metrics.map(metric => screen.getByRole('textbox', { name: metric + ' engineering report/BOM' }))
  expect.soft(inputs.filter(input => input.disabled).map(input => input.getAttribute('aria-label')),
    'report inputs must accept user edits during the selected read').toEqual([])
  for (const [i, input] of inputs.entries()) {
    if (!input.disabled) fireEvent.change(input, { target: { value: report[i] } })
  }
  const current = originals[2].graph
  const counts = [current.panels.length, null, current.inverters.length, current.strings.length, current.panels.length / 2]
  const checkSelected = () => {
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByText('Saved design revision ' + current.rev + '.')).toBeTruthy()
    const table = screen.getByRole('table', { name: 'Saved Studio vs engineering report/BOM totals' })
    const rows = within(table).getAllByRole('rowheader').map(label => label.closest('tr'))
    expect(rows.map(row => row.cells[0].textContent)).toEqual(metrics)
    for (const [i, row] of rows.entries()) {
      const quantity = counts[i]
      const difference = quantity === null ? null : quantity - Number(report[i])
      const verdict = quantity === null ? 'Unavailable: The saved graph has no explicit measured optimizer count.'
        : difference === 0 ? 'PASS' : 'FAIL'
      expect(row.cells[1].textContent).toBe(quantity === null ? 'Unavailable' : String(quantity))
      expect.soft(within(row).getByRole('textbox').value).toBe(report[i])
      expect.soft(row.cells[3].textContent).toBe(difference === null ? 'Unavailable' : String(difference))
      expect.soft(row.cells[4].textContent).toBe(verdict)
    }
    expect(versions).toEqual(originals)
  }
  let ready = false
  for (const index of order) {
    await act(async () => { requests[index].succeed() })
    if (index === 2) ready = true
    if (ready) checkSelected()
    else expect(screen.getByText('Reading saved design.')).toBeTruthy()
    expect(versions).toEqual(originals)
  }
})
