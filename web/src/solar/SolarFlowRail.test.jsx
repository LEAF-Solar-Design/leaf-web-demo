import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarFlowRail from './SolarFlowRail.jsx'

afterEach(cleanup)
it('opens canonical SolarEdge import and tracking review from the selected project rail', () => {
  const binding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
    project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
    input_version_id: '44444444-4444-4444-4444-444444444444' }
  const rows = ['solar-solaredge-accept', 'solar-solaredge-tracking-read'].map((name) => ({ name,
    catalog_digest: 'sha256:' + 'a'.repeat(64), params: { type: 'object', properties: {} },
    solar: { schema: 'leaf.solar-tool-view.v1', name, family: 'imports', wave: 4, order: 1,
      entitlement: name === 'solar-solaredge-accept' ? 'run_write' : 'run_read', interaction: { mode: 'form' } },
    availability: { entitled: true, implemented: true, engine_ready: true, input_ready: false, runnable: false,
      admission_checked: false, admission: null, refusal_reasons: ['project_request_required'] } }))
  const client = { getSnapshot: () => ({ binding, authorityKey: 'session-1' }), subscribe: () => () => {} }
  const props = { solarBinding: binding, authorityKey: 'session-1', projectClient: client,
    projectCatalog: { binding, authorityKey: 'session-1', client, families: [{ capabilities: rows }], isCurrent: () => true },
    onOpenStep: vi.fn() }
  const view = render(<SolarFlowRail {...props} />)
  fireEvent.change(screen.getByLabelText('Solar flow'), { target: { value: 'solaredge-import' } })
  const buttons = screen.getAllByRole('button')
  expect(buttons).toHaveLength(2)
  fireEvent.click(buttons[0]); fireEvent.click(buttons[1])
  expect(props.onOpenStep.mock.calls.map(([row]) => row)).toEqual(rows)
  view.rerender(<SolarFlowRail {...props} projectCatalog={{ ...props.projectCatalog, isCurrent: () => false }} />)
  expect(screen.getAllByRole('button').every((button) => button.disabled)).toBe(true)
})
