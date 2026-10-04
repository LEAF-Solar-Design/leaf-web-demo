import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { solarRailReason } from '../lib/ribbonClusters.js'
import SolarFlowRail from './SolarFlowRail.jsx'
import SolarStepEditor from './SolarStepEditor.jsx'
import conductorDeclaration from '../../../server/solar_tools/solar_string_conductors.json'
import ground from './__fixtures__/stringComposerGround.json'
import rooftop from './__fixtures__/stringComposerRooftop.json'
import { STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL, SOLAR_STRING_COMPOSER_REASONS,
  composeStringRequest, stringComposerView } from './solarStringComposerModel.js'
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
  'SolarEdge PDF Import (preview, unavailable)', 'PVcase Parity (tutorial, unavailable)',
]
function changeFlow(id) {
  fireEvent.change(screen.getByRole('combobox', { name: 'Solar flow' }), { target: { value: id } })
}

describe('F2 rail panel admission', () => {
  const accept = liveRow('solar-solaredge-accept', 'imports', 4, 30, 'run_write')
  const review = liveRow('solar-solaredge-tracking-read', 'imports', 4, 31, 'run_read')
  const catalog = [{ family_id: 'imports', capabilities: [accept, review] }]
  const host = { 'solaredge-import': ['solaredge-import'] }

  it('F2 20 rail recomputes selection and options when host changes', () => {
    const { rerender } = render(<SolarFlowRail families={catalog} drawingId="d1" />)
    changeFlow('solaredge-import')
    const option = () => screen.getByRole('combobox', { name: 'Solar flow' }).selectedOptions[0].textContent
    expect(screen.getByTestId('solar-flow-unavailable')).toBeTruthy()
    expect(items()).toEqual([])
    expect(option()).toBe('SolarEdge PDF Import (preview, unavailable)')
    rerender(<SolarFlowRail families={catalog} drawingId="d1" workspacePanelsByFlow={host} />)
    expect(screen.queryByTestId('solar-flow-unavailable')).toBeNull()
    expect(option()).toBe('SolarEdge PDF Import (preview)')
    expect(items().map((item) => item.querySelector('button').id)).toEqual([accept.name, review.name].map(solarFlowStepId))
    expect(screen.getAllByRole('button')).toHaveLength(2)
    rerender(<SolarFlowRail families={catalog} drawingId="d1" />)
    expect(screen.getByTestId('solar-flow-unavailable')).toBeTruthy()
    expect(items()).toEqual([])
    expect(screen.queryAllByRole('button')).toEqual([])
    expect(option()).toBe('SolarEdge PDF Import (preview, unavailable)')
  })

  it('F2 21 rail presents exact boundary copy without requests', () => {
    const fetchImpl = vi.fn()
    vi.stubGlobal('fetch', fetchImpl)
    try {
      render(<SolarFlowRail families={LIVE} drawingId="d1" />)
      for (const [id, labels, note] of [
        ['ground-physical', ['Terrain', 'Tracker layout', 'Civil and piles', 'Shade and terrain analysis', 'Exports'],
          'Preview flow: its results are not production Solar design yet.'],
        ['solaredge-import', ['Upload the SolarEdge PDF', 'Inspect counts and matching', 'Accept tracking', 'Review accepted tracking'],
          'Preview flow: its results are not production Solar design yet.'],
        ['pvcase-tutorial', ['Geometry conversion', 'Solve on the shared model', 'Exports'],
          'Tutorial flow: conversion and solve are not available in this workspace yet.'],
      ]) {
        changeFlow(id)
        expect(document.getElementById('solar-flow-unavailable-reason').textContent).toBe('Some stages in this flow are not available yet.')
        expect([...screen.getByRole('list', { name: 'Unavailable stages' }).children].map((item) => item.textContent)).toEqual(labels)
        expect(screen.getByTestId('solar-flow-maturity').textContent).toBe(note)
        expect(items()).toEqual([])
        expect(screen.queryAllByRole('button')).toEqual([])
      }
      expect(fetchImpl).not.toHaveBeenCalled()
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

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
    const list = screen.getByRole('list', { name: 'Unavailable stages' })
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

  it('G2c FL16 a catalog with conversion and feeders lists the Ground Mount Electrical steps in stage order', () => {
    const complete = [...LIVE, { family_id: 'routing', capabilities: [
      liveRow('solar-trackers-to-panel-groups', 'stringing', 3, 5, 'run_write'),
      liveRow('solar-feeders', 'routing', 3, 60, 'run_write'),
    ] }]
    render(<SolarFlowRail families={complete} drawingId="d1" />)
    const select = screen.getByRole('combobox', { name: 'Solar flow' })
    expect([...select.options].map((option) => option.textContent))
      .toEqual(['Rooftop', 'Ground Mount Electrical', ...FLOW_OPTION_LABELS.slice(2)])
    changeFlow('ground-electrical')
    expect(screen.queryByTestId('solar-flow-unavailable')).toBeNull()
    expect(items().map((item) => item.querySelector('button').id)).toEqual([
      'solar-trackers-to-panel-groups',
      'solar-settings', 'solar-size-strings', 'solar-string-add', 'solar-string-flip', 'solar-string-swap',
      'solar-string-delete', 'solar-string-rebuild', 'solar-correct-string',
      'solar-assign-equipment', 'solar-string-conductors', 'solar-feeders',
      'solar-nec-ampacity-correction', 'solar-nec-ac-voltage-drop', 'solar-nec-conduit-fill', 'solar-nec-feeder-ocpd',
      'solar-string-data',
    ].map(solarFlowStepId))
    changeFlow('rooftop')
    expect(items().map((item) => item.querySelector('button').id)).toEqual(ROOFTOP_LIVE.map(solarFlowStepId))
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
    expect(screen.getByTestId('solar-flow-maturity').textContent).toBe(SOLAR_FLOW_MATURITY_NOTES.preview)
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
  const stringEnvelope = (graph = ground) => ({ intake: { solar_design_graph: structuredClone(graph) },
    version: 7, head: 7, latest: 7 })
  const stringTools = [STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL]
  const stringButton = (name) => screen.getByRole('button', { name })
  const stringClick = (name) => fireEvent.click(stringButton(name))
  const stringChange = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
  const stringQueue = () => within(screen.getByRole('region', { name: 'Selection queue' }))
    .queryAllByRole('listitem').map((item) => item.querySelector('span').textContent)
  const stringRange = () => ({ kind: 'range', frameIndex: 0, frameId: ground.frames[0].id, fromSlot: 1, toSlot: 3 })
  const fillString = () => {
    stringClick('Select row 1 Group 1')
    stringChange('First slot', '1'); stringChange('Last slot', '3'); stringClick('Add range')
  }
  async function mountString(toolName = STRING_ADD_TOOL, graph = ground, overrides = {}) {
    const props = { row: row(toolName, 25, 'Compose strings', READY), drawingId: 'd1', drawingVersion: 7,
      projectId: null, readIntake: vi.fn(async () => stringEnvelope(graph)), onSubmit: vi.fn(), onClose: vi.fn(),
      ...overrides }
    const result = render(<SolarStepEditor {...props} />)
    await act(async () => {})
    return { ...result, props }
  }
  const expectComposer = (graph) => {
    expect(screen.getAllByRole('button', { name: graph === ground ? /^Select row 1 / : /^Add panel / }).length).toBeGreaterThan(0)
    expect(screen.getByRole('region', { name: 'Selection queue' })).toBeTruthy()
    expect(screen.getByRole('region', { name: 'Composed strings' })).toBeTruthy()
    for (const label of ['Ordered panel refs', 'Expected rev']) expect(screen.queryByLabelText(label)).toBeNull()
    expect(document.querySelector('textarea')).toBeNull()
  }

  it('H1 single add mounts the composer for Ground and Rooftop', async () => {
    for (const graph of [ground, rooftop]) {
      await mountString(STRING_ADD_TOOL, graph)
      expectComposer(graph)
      expect(screen.queryByLabelText('String length')).toBeNull()
      cleanup()
    }
  })

  it('H2 multi add mounts the composer with String length for both designs', async () => {
    for (const graph of [ground, rooftop]) {
      await mountString(STRING_MULTI_ADD_TOOL, graph)
      expectComposer(graph)
      expect(screen.getByLabelText('String length')).toBeTruthy()
      cleanup()
    }
  })

  it('H3 sizing conductors and Homeruns keep their hosts', async () => {
    for (const [step, label, action] of [[row('solar-size-strings', 20, 'Size strings', READY), 'Scope', 'Review & run'],
      [{ ...conductorDeclaration, label: 'String conductors' }, 'Conductor', 'Apply to selected strings'],
      [homeruns(), 'Note', 'Review & run']]) {
      render(<SolarStepEditor row={step} drawingId="d1" drawingVersion={7}
        readIntake={vi.fn(async () => stringEnvelope())} onSubmit={vi.fn()} onClose={vi.fn()} />)
      await act(async () => {})
      expect(screen.getByLabelText(label)).toBeTruthy()
      expect(screen.getByRole('button', { name: action })).toBeTruthy()
      expect(screen.queryByRole('region', { name: 'Selection queue' })).toBeNull()
      expect(screen.queryByLabelText('Search rows')).toBeNull()
      expect(screen.queryByLabelText('Search panels')).toBeNull()
      cleanup()
    }
  })

  it('H4 composed single and multi requests hand off once and stay mounted', async () => {
    for (const toolName of stringTools) {
      let finishHead
      const readIntake = vi.fn().mockResolvedValueOnce(stringEnvelope())
        .mockImplementationOnce(() => new Promise((resolve) => { finishHead = resolve }))
      const { props } = await mountString(toolName, ground, { readIntake })
      fillString()
      if (toolName === STRING_MULTI_ADD_TOOL) stringChange('String length', '3')
      stringClick('Review & run')
      await act(async () => {})
      expect(readIntake.mock.calls).toEqual([['d1', 7], ['d1', 'head']])
      expect(props.onSubmit).not.toHaveBeenCalled()
      await act(async () => { finishHead(stringEnvelope()) })
      const expected = composeStringRequest({ view: stringComposerView({ envelope: stringEnvelope(),
        drawingId: 'd1', drawingVersion: 7, projectId: null }), toolName, queue: [stringRange()], stringLength: 3 })
      expect(expected.ok).toBe(true)
      expect(props.onSubmit).toHaveBeenCalledExactlyOnceWith(props.row, expected.params)
      expect(props.onSubmit.mock.calls[0][0]).toBe(props.row)
      expect(props.onClose).not.toHaveBeenCalled()
      expectComposer(ground)
      expect(stringQueue()).toEqual(['Group 1 slots 1 to 3'])
      cleanup()
    }
  })

  it('H5 run status reaches the composer and retains then clears the queue', async () => {
    const { props, rerender, container } = await mountString(STRING_MULTI_ADD_TOOL)
    fillString(); stringChange('String length', '3')
    rerender(<SolarStepEditor {...props} status="pending" />)
    for (const label of ['Search rows', 'First slot', 'Last slot', 'String length']) {
      expect(screen.getByLabelText(label).disabled).toBe(true)
    }
    for (const name of ['Select row 1 Group 1', 'Add range', 'Remove selection 1', 'Review & run']) {
      expect(stringButton(name).disabled).toBe(true)
    }
    expect(screen.getAllByText(SOLAR_STRING_COMPOSER_REASONS.pending)).toHaveLength(1)
    expect(container.querySelector('#solar-step-editor > .solar-step-note')).toBeNull()
    rerender(<SolarStepEditor {...props} status="failed" failureCode="STRING_TOO_LONG" />)
    expect(screen.getByRole('alert').textContent).toBe(SOLAR_STRING_COMPOSER_REASONS.tooLong)
    expect(stringQueue()).toEqual(['Group 1 slots 1 to 3'])
    expect(container.textContent).not.toContain('Your inputs are kept.')
    rerender(<SolarStepEditor {...props} status="finished" />)
    await act(async () => {})
    expect(stringQueue()).toEqual([])
    expect(props.readIntake.mock.calls).toEqual([['d1', 7], ['d1', 7]])
  })

  it('H6 string scope refusals have one Cancel and perform no reads or submissions', async () => {
    for (const toolName of stringTools) {
      for (const scope of [{ projectId: 'p1' }, { drawingId: '' }, { drawingVersion: null }]) {
        const { props, container } = await mountString(toolName, ground, scope)
        expect(screen.getByText(SOLAR_STRING_COMPOSER_REASONS.scope)).toBeTruthy()
        expect(screen.getAllByRole('button', { name: 'Cancel' })).toHaveLength(1)
        expect(container.querySelector('input')).toBeNull()
        expect(props.readIntake).not.toHaveBeenCalled()
        expect(props.onSubmit).not.toHaveBeenCalled()
        cleanup()
      }
    }
  })

  it('H7 composer Cancel closes once and focuses its rail button', async () => {
    for (const toolName of stringTools) {
      render(<button id={solarFlowStepId(toolName)}>String step</button>)
      const rail = screen.getByRole('button', { name: 'String step' })
      const { props } = await mountString(toolName)
      expect(screen.getAllByRole('button', { name: 'Cancel' })).toHaveLength(1)
      stringClick('Cancel')
      expect(props.onClose).toHaveBeenCalledTimes(1)
      expect(props.onSubmit).not.toHaveBeenCalled()
      expect(document.activeElement).toBe(rail)
      cleanup()
    }
  })

  it('H8 Escape from the row search closes focuses the rail and stops propagation', async () => {
    const parentKey = vi.fn()
    render(<button id={solarFlowStepId(STRING_ADD_TOOL)}>String step</button>)
    const rail = screen.getByRole('button', { name: 'String step' })
    const { props, container } = await mountString()
    const parent = container.parentElement
    parent.addEventListener('keydown', parentKey)
    const search = screen.getByLabelText('Search rows')
    search.focus()
    fireEvent.keyDown(search, { key: 'Escape' })
    expect(props.onClose).toHaveBeenCalledTimes(1)
    expect(props.onSubmit).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(rail)
    expect(parentKey).not.toHaveBeenCalled()
    parent.removeEventListener('keydown', parentKey)
  })

  it('H9 each string tool owns its only numeric read and Homeruns still prefills', async () => {
    for (const toolName of stringTools) {
      const { props } = await mountString(toolName)
      expect(props.readIntake).toHaveBeenCalledExactlyOnceWith('d1', 7)
      cleanup()
    }
    const readIntake = vi.fn(async () => stringEnvelope())
    render(<SolarStepEditor row={homeruns()} drawingId="d1" drawingVersion={7} readIntake={readIntake}
      onSubmit={vi.fn()} onClose={vi.fn()} />)
    await act(async () => {})
    expect(readIntake).toHaveBeenCalledExactlyOnceWith('d1', 7)
    expect(screen.getByLabelText('Expected rev').value).toBe(String(ground.rev))
  })

  it('H10 same tick review clicks read the head and submit once without closing', async () => {
    let finishHead
    const readIntake = vi.fn().mockResolvedValueOnce(stringEnvelope())
      .mockImplementationOnce(() => new Promise((resolve) => { finishHead = resolve }))
    const { props } = await mountString(STRING_ADD_TOOL, ground, { readIntake })
    fillString()
    const review = stringButton('Review & run')
    act(() => { fireEvent.click(review); fireEvent.click(review) })
    await act(async () => {})
    expect(readIntake.mock.calls).toEqual([['d1', 7], ['d1', 'head']])
    await act(async () => { finishHead(stringEnvelope()) })
    expect(props.onSubmit).toHaveBeenCalledTimes(1)
    expect(props.onClose).not.toHaveBeenCalled()
    expectComposer(ground)
  })

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
