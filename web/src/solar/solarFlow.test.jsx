import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { solarRailReason } from '../lib/ribbonClusters.js'
import SolarFlowRail from './SolarFlowRail.jsx'
import SolarStepEditor from './SolarStepEditor.jsx'
import conductorDeclaration from '../../../server/solar_tools/solar_string_conductors.json'
import { SOLAR_FLOW_MATURITY_NOTES, SOLAR_FLOW_UNAVAILABLE_REASONS, solarFlowStepId } from './solarFlowModel.js'

afterEach(cleanup)

const W1 = [
  ['solar-settings', 10, 'Solar settings'], ['solar-size-strings', 20, 'Size strings'],
  ['solar-panel-groups', 30, 'Panel groups'], ['solar-solve-proposal', 40, 'Solve proposal'],
  ['solar-commit-solve', 50, 'Commit solve'], ['solar-correct-string', 60, 'Correct string'],
  ['solar-assign-equipment', 70, 'Assign equipment'], ['solar-homeruns', 80, 'Homeruns'],
  ['solar-schedule', 90, 'Schedule'],
]
const READY = Object.freeze({ entitled: true, implemented: true, engine_ready: true, input_ready: true, refusal_reasons: [] })

function blocked(...codes) {
  return { entitled: true, implemented: true, engine_ready: true, input_ready: false, refusal_reasons: codes }
}

function row(name, order, label, availability) {
  return {
    name, label, availability, capabilities: ['drawing.write'],
    solar: {
      schema: 'leaf.solar-tool-view.v1', name, family: 'stringing', wave: 1, order,
      entitlement: 'run_write', interaction: { mode: 'form' },
    },
    params: { type: 'object', properties: {
      drawing_id: { type: 'string', default: 'must-not-show' },
      expected_rev: { type: 'integer', default: 0 },
      note: { type: 'string', default: 'a' },
    } },
  }
}

function families(readyNames, blockedAvailability = blocked('valid_settings_required')) {
  return [{
    id: 'stringing',
    capabilities: W1.map(([name, order, label]) => row(name, order, label,
      readyNames.includes(name) ? READY : blockedAvailability)),
  }]
}

function items() {
  return [...screen.getByTestId('solar-flow-rail').querySelectorAll('ol > li')]
}

function button(name) {
  return screen.getByRole('button', { name: new RegExp(`^${name}`) })
}


const LIVE_TABLE = [
  ['stringing', [
    ['solar-settings', 1, 10, 'run_write'], ['solar-size-strings', 1, 20, 'run_write'],
    ['solar-panel-groups', 1, 30, 'run_write'], ['solar-commit-solve', 1, 50, 'run_write'],
    ['solar-correct-string', 1, 60, 'run_write'], ['solar-assign-equipment', 1, 70, 'run_write'],
    ['solar-homeruns', 1, 80, 'run_write'], ['solar-schedule', 1, 90, 'run_write'],
    ['solar-string-add', 2, 70, 'run_write'], ['solar-string-flip', 2, 72, 'run_write'],
    ['solar-string-swap', 2, 74, 'run_write'], ['solar-string-rebuild', 2, 76, 'run_read'],
    ['solar-string-data', 2, 78, 'run_read'], ['solar-string-delete', 2, 80, 'run_write'],
    ['solar-string-conductors', 2, 85, 'run_write'], ['solar-solve-proposal', 1, 40, 'solve'],
  ]],
  ['placement', [
    ['solar-panels-from-drawing', 1, 15, 'run_write'], ['solar-select-by-zone', 2, 10, 'run_read'],
    ['solar-electrical-zones', 2, 20, 'run_write'], ['solar-panel-add', 2, 30, 'run_write'],
    ['solar-panel-remove', 2, 40, 'run_write'], ['solar-panel-group-delete', 2, 50, 'run_write'],
    ['solar-autofill-plan', 2, 58, 'run_read'], ['solar-autofill', 2, 60, 'run_write'],
  ]],
  ['settings', [['solar-unit-sync', 2, 10, 'run_write']]],
  ['equipment', [
    ['solar-nec-ampacity-correction', 3, 10, 'run_read'], ['solar-nec-ac-voltage-drop', 3, 20, 'run_read'],
    ['solar-nec-conduit-fill', 3, 30, 'run_read'], ['solar-nec-feeder-ocpd', 3, 40, 'run_read'],
  ]],
]
function liveRow(name, family, wave, order, entitlement) {
  return {
    name, availability: READY, params: { type: 'object', properties: {} },
    solar: { schema: 'leaf.solar-tool-view.v1', name, family, wave, order, entitlement, interaction: { mode: 'form' } },
  }
}
const LIVE = LIVE_TABLE.map(([family_id, rows]) => ({
  family_id, capabilities: rows.map(([name, wave, order, entitlement]) => liveRow(name, family_id, wave, order, entitlement)),
}))
const ROOFTOP_LIVE = [
  'solar-settings', 'solar-panels-from-drawing', 'solar-size-strings', 'solar-panel-groups',
  'solar-solve-proposal', 'solar-commit-solve', 'solar-correct-string', 'solar-assign-equipment',
  'solar-string-conductors', 'solar-homeruns', 'solar-schedule',
]

const FLOW_OPTION_LABELS = [
  'Rooftop', 'Ground Mount Electrical (unavailable)', 'Ground Mount Physical (preview, unavailable)',
  'SolarEdge PDF Import (unavailable)', 'PVcase Parity (tutorial, unavailable)',
]
function changeFlow(id) {
  fireEvent.change(screen.getByRole('combobox', { name: 'Solar flow' }), { target: { value: id } })
}

describe('Solar flow picker', () => {
  it('FL11 the selector retains default Rooftop labels and statuses', () => {
    render(<SolarFlowRail families={families(['solar-settings', 'solar-size-strings'])} drawingId="d1" onOpenStep={vi.fn()} />)
    const select = screen.getByRole('combobox', { name: 'Solar flow' })
    expect(select.value).toBe('rooftop')
    expect([...select.options].map((option) => option.textContent)).toEqual(FLOW_OPTION_LABELS)
    expect(screen.getByTestId('solar-flow-rail').getAttribute('data-flow')).toBe('rooftop')
    expect(items()).toHaveLength(9)
    expect(items().map((item) => item.querySelector('.solar-flow-label').textContent)).toEqual(W1.map(([, , label]) => label))
    expect(items().map((item) => item.getAttribute('data-status')))
      .toEqual(['ready', 'ready', 'blocked', 'blocked', 'blocked', 'blocked', 'blocked', 'blocked', 'blocked'])
    expect(screen.queryByTestId('solar-flow-unavailable')).toBeNull()
    expect(screen.queryByTestId('solar-flow-maturity')).toBeNull()
    expect(screen.getAllByRole('status')).toHaveLength(1)
  })

  it('FL12 unavailable flows explain missing stages without step controls', () => {
    const onFlowChange = vi.fn()
    render(<SolarFlowRail families={LIVE} drawingId="d1" onFlowChange={onFlowChange} />)
    changeFlow('ground-electrical')
    const rail = screen.getByTestId('solar-flow-rail')
    expect(items()).toEqual([])
    expect(rail.querySelector('ol')).toBeNull()
    expect(rail.querySelector('[id^="solar-step-"]')).toBeNull()
    expect(rail.querySelector('button')).toBeNull()
    expect(screen.getByTestId('solar-flow-unavailable')).toBeTruthy()
    expect(document.getElementById('solar-flow-unavailable-reason').textContent).toBe(SOLAR_FLOW_UNAVAILABLE_REASONS.stages_missing)
    expect(screen.getByRole('combobox', { name: 'Solar flow' }).getAttribute('aria-describedby')).toBe('solar-flow-unavailable-reason')
    const list = screen.getByRole('list', { name: 'Stages not in this catalog' })
    expect([...list.children].map((item) => item.textContent))
      // W20-03: matches FL4's LIVE catalog missing stages.
      .toEqual(['Tracker conversion', 'Feeders and routes'])
    expect(rail.getAttribute('data-flow')).toBe('ground-electrical')
    expect(onFlowChange).toHaveBeenCalledTimes(1)
    expect(onFlowChange).toHaveBeenCalledWith('ground-electrical')
    expect(screen.getAllByRole('status')).toHaveLength(1)
    expect(screen.getByRole('status').textContent).toBe('')
    changeFlow('ground-electrical')
    expect(onFlowChange).toHaveBeenCalledTimes(1)
  })

  it('FL12 a run settling in another flow cannot restore an old success announcement', () => {
    const shown = families(['solar-settings'])
    const { rerender } = render(<SolarFlowRail families={shown} drawingId="d1" runs={{}} />)
    const success = { 'solar-settings': { ok: true, code: null } }
    rerender(<SolarFlowRail families={shown} drawingId="d1" runs={success} />)
    expect(screen.getByRole('status').textContent).toBe('Solar settings finished')
    rerender(<SolarFlowRail families={shown} drawingId="d1" pendingTool="solar-settings" runs={success} />)
    expect(items()[0].getAttribute('data-status')).toBe('pending')
    changeFlow('ground-electrical')
    expect(screen.getByRole('status').textContent).toBe('')
    const failure = { 'solar-settings': { ok: false, code: 'STALE_GRAPH_REVISION' } }
    rerender(<SolarFlowRail families={shown} drawingId="d1" runs={failure} />)
    changeFlow('rooftop')
    expect(items()[0].getAttribute('data-status')).toBe('failed')
    expect(screen.getByRole('status').textContent).not.toContain('Solar settings finished')
  })

  it('FL13 the live catalog renders all eleven Rooftop steps in order', () => {
    render(<SolarFlowRail families={LIVE} drawingId="d1" />)
    expect(items().map((item) => item.querySelector('button').id)).toEqual(ROOFTOP_LIVE.map(solarFlowStepId))
    expect(items()[8].querySelector('button').id).toBe('solar-step-solar-string-conductors')
  })

  it('FL14 preview and tutorial notes and readiness memory survive a flow round trip', () => {
    const all = W1.map(([name]) => name)
    const { rerender } = render(<SolarFlowRail families={families(all)} familiesDrawingId="d1" drawingId="d1" />)
    changeFlow('ground-physical')
    expect(screen.getByTestId('solar-flow-maturity').textContent).toBe(SOLAR_FLOW_MATURITY_NOTES.preview)
    changeFlow('pvcase-tutorial')
    expect(screen.getByTestId('solar-flow-maturity').textContent).toBe(SOLAR_FLOW_MATURITY_NOTES.tutorial)
    changeFlow('solaredge-import')
    expect(screen.queryByTestId('solar-flow-maturity')).toBeNull()
    changeFlow('rooftop')
    expect(screen.queryByTestId('solar-flow-maturity')).toBeNull()
    expect(items()).toHaveLength(9)
    changeFlow('ground-electrical')
    rerender(<SolarFlowRail families={families(['solar-settings'], blocked('valid_strings_required'))} familiesDrawingId="d1" drawingId="d1" />)
    changeFlow('rooftop')
    expect(items()).toHaveLength(9)
    expect(items()[6].getAttribute('data-invalidated')).toBe('true')
    expect(items()[6].textContent).toContain('Needs rerun: Solve valid strings first')
  })
})


describe('SolarFlowRail', () => {
  it('CF12 conductors stay enabled when homeruns are blocked and open the table', async () => {
    const conductor = {
      ...row('solar-string-conductors', 85, 'String conductors', READY),
      params: conductorDeclaration.record.params,
    }
    conductor.solar.wave = 2
    const shown = families(W1.map(([name]) => name).filter((name) => name !== 'solar-homeruns'), blocked('routing_topology_required'))
    shown[0].capabilities.push(conductor)
    const readIntake = vi.fn(async () => ({ version: 3, intake: { solar_design_graph: { rev: 7, strings: [
      { id: 'T1', circuit_tag: 'A1', wire_gauge: '' },
    ] } } }))
    function Host() {
      const [step, setStep] = React.useState(null)
      return <>
        <SolarFlowRail families={shown} drawingId="d1" familiesDrawingId="d1" onOpenStep={setStep} />
        {step && <SolarStepEditor row={step} drawingId="d1" drawingVersion={3} readIntake={readIntake}
          onSubmit={vi.fn()} onClose={() => setStep(null)} />}
      </>
    }
    render(<Host />)
    expect(items()).toHaveLength(10)
    expect(button('String conductors').disabled).toBe(false)
    expect(button('Homeruns').disabled).toBe(true)
    fireEvent.click(button('String conductors'))
    expect(screen.getByRole('table', { name: 'String conductors' })).toBeTruthy()
    await screen.findByLabelText('Select A1')
    expect(readIntake).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(screen.getByLabelText('Select A1'), { key: 'Escape' })
    expect(screen.queryByRole('table')).toBeNull()
    expect(document.activeElement).toBe(button('String conductors'))
  })

  it('R1 renders the nine steps in order with the resume step marked', () => {
    render(<SolarFlowRail families={families(['solar-settings', 'solar-size-strings'])} drawingId="d1" onOpenStep={vi.fn()} />)
    const nav = screen.getByRole('navigation', { name: 'Solar design steps' })
    expect(nav.getAttribute('data-testid')).toBe('solar-flow-rail')
    const listed = items()
    expect(listed).toHaveLength(9)
    expect(listed.map((item) => item.querySelector('.solar-flow-label').textContent)).toEqual(W1.map(([, , label]) => label))
    expect(listed.map((item) => item.getAttribute('data-status')))
      .toEqual(['ready', 'ready', 'blocked', 'blocked', 'blocked', 'blocked', 'blocked', 'blocked', 'blocked'])
    const current = nav.querySelectorAll('[aria-current="step"]')
    expect(current).toHaveLength(1)
    expect(current[0]).toBe(button('Size strings'))
  })

  it('R2 a blocked step is disabled and described by the ribbon reason', () => {
    const shown = families(['solar-settings'])
    render(<SolarFlowRail families={shown} drawingId="d1" onOpenStep={vi.fn()} />)
    const sizing = button('Size strings')
    expect(sizing.disabled).toBe(true)
    const describedBy = sizing.getAttribute('aria-describedby')
    expect(describedBy).toBeTruthy()
    const text = describedBy.split(' ').map((id) => document.getElementById(id).textContent).join('')
    expect(text).toBe(solarRailReason(shown[0].capabilities[1].availability))
    expect(text).toBe('Complete valid Solar settings first')
    expect(button('Solar settings').getAttribute('aria-describedby')).toBeNull()
  })

  it('R3 a step that was ready on this drawing and is blocked by the graph now needs a rerun', () => {
    const all = W1.map(([name]) => name)
    const { rerender } = render(<SolarFlowRail families={families(all)} familiesDrawingId="d1" drawingId="d1" onOpenStep={vi.fn()} />)
    const after = families(['solar-settings'], blocked('valid_strings_required'))
    rerender(<SolarFlowRail families={after} familiesDrawingId="d1" drawingId="d1" onOpenStep={vi.fn()} />)
    const equipment = items()[6]
    expect(equipment.getAttribute('data-invalidated')).toBe('true')
    expect(equipment.textContent).toContain(`Needs rerun: ${solarRailReason(after[0].capabilities[6].availability)}`)
    expect(equipment.textContent).toContain('Needs rerun: Solve valid strings first')
    rerender(<SolarFlowRail families={families(['solar-settings'], blocked('entitlement_required'))} familiesDrawingId="d1" drawingId="d1" onOpenStep={vi.fn()} />)
    expect(items()[6].textContent).not.toContain('Needs rerun')
    rerender(<SolarFlowRail families={after} familiesDrawingId="d2" drawingId="d2" onOpenStep={vi.fn()} />)
    expect(items()[6].textContent).not.toContain('Needs rerun')
  })

  it('CORR1 a drawing switch never records the old drawing readiness under the new drawing', () => {
    const all = W1.map(([name]) => name)
    const old = families(all)
    const { rerender } = render(<SolarFlowRail families={old} familiesDrawingId="d1" drawingId="d1" onOpenStep={vi.fn()} />)
    // The realistic ordering: the drawing id moves first while the families (and their drawing id) are still d1's.
    rerender(<SolarFlowRail families={old} familiesDrawingId="d1" drawingId="d2" onOpenStep={vi.fn()} />)
    rerender(<SolarFlowRail families={families(['solar-settings'], blocked('valid_strings_required'))} familiesDrawingId="d2" drawingId="d2" onOpenStep={vi.fn()} />)
    expect(items().some((item) => item.textContent.includes('Needs rerun'))).toBe(false)
    expect(items().some((item) => item.getAttribute('data-invalidated') === 'true')).toBe(false)
  })

  it('CORR1 on a graphless drawing the opener enables the Solar settings step', () => {
    const seed = { entitled: true, implemented: true, engine_ready: true, input_ready: false, refusal_reasons: ['graph_seed_required'] }
    const shown = [{
      id: 'stringing',
      capabilities: W1.map(([name, order, label]) => row(name, order, label,
        name === 'solar-settings' ? seed : blocked('valid_settings_required'))),
    }]
    const onOpenStep = vi.fn()
    const opener = vi.fn((name, availability) => name === 'solar-settings' && availability?.refusal_reasons?.[0] === 'graph_seed_required')
    const { rerender } = render(<SolarFlowRail families={shown} familiesDrawingId="d1" drawingId="d1" openSettingsForm={opener} onOpenStep={onOpenStep} />)
    const settings = button('Solar settings')
    expect(settings.disabled).toBe(false)
    expect(settings.getAttribute('aria-current')).toBe('step')
    expect(button('Size strings').disabled).toBe(true)
    fireEvent.click(settings)
    expect(onOpenStep).toHaveBeenCalledTimes(1)
    expect(onOpenStep).toHaveBeenCalledWith(shown[0].capabilities[0])
    rerender(<SolarFlowRail families={shown} familiesDrawingId="d1" drawingId="d1" onOpenStep={onOpenStep} />)
    expect(button('Solar settings').disabled).toBe(true)
  })

  it('R4 a settled run is announced in the status region', () => {
    const shown = families(['solar-settings'])
    const { rerender } = render(<SolarFlowRail families={shown} drawingId="d1" runs={{}} onOpenStep={vi.fn()} />)
    expect(screen.getByRole('status').textContent).toBe('')
    rerender(<SolarFlowRail families={shown} drawingId="d1" runs={{ 'solar-settings': { ok: false, code: 'INVALID_SEED_PARENT' } }} onOpenStep={vi.fn()} />)
    expect(screen.getByRole('status').textContent).toBe('Solar settings failed: INVALID_SEED_PARENT')
    expect(items()[0].getAttribute('data-status')).toBe('failed')
    rerender(<SolarFlowRail families={shown} drawingId="d1" runs={{ 'solar-settings': { ok: true, code: null } }} onOpenStep={vi.fn()} />)
    expect(screen.getByRole('status').textContent).toBe('Solar settings finished')
    rerender(<SolarFlowRail families={shown} drawingId="d1" pendingTool="solar-settings" runs={{ 'solar-settings': { ok: true, code: null } }} onOpenStep={vi.fn()} />)
    expect(items()[0].getAttribute('data-status')).toBe('pending')
  })

  it('R5 a ready step opens once and a disabled step never opens', () => {
    const onOpenStep = vi.fn()
    const shown = families(['solar-settings'])
    render(<SolarFlowRail families={shown} drawingId="d1" onOpenStep={onOpenStep} />)
    fireEvent.click(button('Solar settings'))
    expect(onOpenStep).toHaveBeenCalledTimes(1)
    expect(onOpenStep).toHaveBeenCalledWith(shown[0].capabilities[0])
    fireEvent.click(button('Size strings'))
    expect(onOpenStep).toHaveBeenCalledTimes(1)
  })
})

describe('SolarStepEditor', () => {
  it('SZ22 sizing mounts its Scope control and owns the only intake read', async () => {
    const step = row('solar-size-strings', 20, 'Size strings', READY)
    const readIntake = vi.fn(async () => ({ version: 3, intake: { solar_design_graph: {
      rev: 7, settings: { id: 'S' }, project: { zip_code: '44224' }, panels: [{ id: 'P1' }], electrical_zones: [],
    } } }))
    render(<SolarStepEditor row={step} drawingId="d1" drawingVersion={3} readIntake={readIntake}
      onSubmit={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getByLabelText('Scope')).toBeTruthy()
    expect(screen.queryByLabelText('Expected rev')).toBeNull()
    expect(screen.queryByLabelText('Note')).toBeNull()
    await screen.findByText('Saved project ZIP: 44224')
    expect(readIntake).toHaveBeenCalledExactlyOnceWith('d1', 3)
  })

  const homeruns = () => families(W1.map(([name]) => name))[0].capabilities[7]

  it('CF13 prefills only a matching plain envelope with a safe nonnegative revision', async () => {
    const samples = [
      [{ version: 3, intake: { solar_design_graph: { rev: 7 } } }, '7'],
      [{ solar_design_graph: { rev: 7 } }, '0'],
      [{ version: 4, intake: { solar_design_graph: { rev: 7 } } }, '0'],
      [{ version: 3, intake: { solar_design_graph: { rev: -1 } } }, '0'],
      [{ version: 3, intake: { solar_design_graph: { rev: 1.5 } } }, '0'],
      [Object.assign(new Date(), { version: 3, intake: { solar_design_graph: { rev: 7 } } }), '0'],
    ]
    for (const [value, expected] of samples) {
      const readIntake = vi.fn(async () => value)
      render(<SolarStepEditor row={homeruns()} drawingId="d1" drawingVersion={3} readIntake={readIntake}
        onSubmit={vi.fn()} onClose={vi.fn()} />)
      await act(async () => {})
      expect(screen.getByLabelText('Expected rev').value).toBe(expected)
      cleanup()
    }
  })

  it('E1 prefills the graph revision, submits without closing, and retries the same values', async () => {
    const step = homeruns()
    const readIntake = vi.fn(async () => ({ version: 3, intake: { solar_design_graph: { rev: 7 } } }))
    const onSubmit = vi.fn()
    const onClose = vi.fn()
    const props = { row: step, drawingId: 'd1', drawingVersion: 3, readIntake, onSubmit, onClose }
    const { rerender } = render(<SolarStepEditor {...props} />)
    expect(screen.queryByLabelText('Drawing id')).toBeNull()
    await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
    expect(readIntake).toHaveBeenCalledTimes(1)
    expect(readIntake).toHaveBeenCalledWith('d1', 3)
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'b' } })
    fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit).toHaveBeenCalledWith(step, { expected_rev: 7, note: 'b' })
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('region', { name: 'Homeruns parameters' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()

    rerender(<SolarStepEditor {...props} status="pending" />)
    expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(true)

    rerender(<SolarStepEditor {...props} status="failed" failureCode="stale_rev" />)
    expect(screen.getByRole('alert').textContent).toContain('stale_rev')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onSubmit).toHaveBeenCalledTimes(2)
    expect(onSubmit.mock.calls[1][0]).toBe(step)
    expect(onSubmit.mock.calls[1][1]).toEqual(onSubmit.mock.calls[0][1])
    expect(readIntake).toHaveBeenCalledTimes(1)
  })

  it('E1 retained inputs win over the prefill and a touched revision is never overwritten', async () => {
    const readIntake = vi.fn(async () => ({ version: 3, intake: { solar_design_graph: { rev: 7 } } }))
    render(<SolarStepEditor row={homeruns()} drawingId="d1" drawingVersion={3} readIntake={readIntake}
      retained={{ expected_rev: 4, note: 'kept' }} onSubmit={vi.fn()} onClose={vi.fn()} />)
    await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(1))
    await act(async () => {})
    expect(screen.getByLabelText('Expected rev').value).toBe('4')
    expect(screen.getByLabelText('Note').value).toBe('kept')
    cleanup()

    let resolve
    const slow = vi.fn(() => new Promise((done) => { resolve = done }))
    render(<SolarStepEditor row={homeruns()} drawingId="d1" drawingVersion={3} readIntake={slow} onSubmit={vi.fn()} onClose={vi.fn()} />)
    await waitFor(() => expect(slow).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
    await act(async () => { resolve({ version: 3, intake: { solar_design_graph: { rev: 9 } } }) })
    expect(screen.getByLabelText('Expected rev').value).toBe('5')
  })

  it('E1 a step without expected_rev never reads the drawing', () => {
    const step = homeruns()
    const bare = { ...step, params: { type: 'object', properties: { note: { type: 'string', default: 'a' } } } }
    const readIntake = vi.fn()
    render(<SolarStepEditor row={bare} drawingId="d1" drawingVersion={3} readIntake={readIntake} onSubmit={vi.fn()} onClose={vi.fn()} />)
    expect(readIntake).not.toHaveBeenCalled()
  })

  it('E1 Cancel and Escape close and return focus to the step rail button', () => {
    const shown = families(W1.map(([name]) => name))
    const onClose = vi.fn()
    const onSubmit = vi.fn()
    render(
      <>
        <SolarFlowRail families={shown} drawingId="d1" onOpenStep={vi.fn()} />
        <SolarStepEditor row={shown[0].capabilities[7]} drawingId="d1" drawingVersion={3} onSubmit={onSubmit} onClose={onClose} />
      </>,
    )
    screen.getByLabelText('Note').focus()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onSubmit).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(button('Homeruns'))
    expect(document.activeElement.id).toBe('solar-step-solar-homeruns')
    fireEvent.keyDown(screen.getByLabelText('Note'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(2)
    expect(document.activeElement).toBe(button('Homeruns'))
  })
})
