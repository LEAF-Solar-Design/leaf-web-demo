import React from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createHash } from 'node:crypto'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { solarRailReason } from '../lib/ribbonClusters.js'
import SolarFlowRail from './SolarFlowRail.jsx'
import SolarStepEditor from './SolarStepEditor.jsx'
import conductorDeclaration from '../../../server/solar_tools/solar_string_conductors.json'
import ground from './__fixtures__/stringComposerGround.json'
import rooftop from './__fixtures__/stringComposerRooftop.json'
import { STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL, SOLAR_STRING_COMPOSER_REASONS, PROJECT_STRING_COMPOSER_REASONS,
  composeStringRequest, stringComposerView } from './solarStringComposerModel.js'
import { SOLAR_FLOW_MATURITY_NOTES, SOLAR_FLOW_UNAVAILABLE_REASONS, solarFlowStepId,
  PROJECT_SOLAR_ROOFTOP_STAGES, PROJECT_SOLAR_REVIEW_TOOLS, PROJECT_SOLAR_RAIL_REASONS,
  projectSolarRailReason, SOLAR_FLOWS } from './solarFlowModel.js'
import { recordProjectSolarFlowRun, projectSolarFlowRunsFor, projectSolarFlowScope, projectSolarFlowReason } from './solarFlowModel.js'
import { createSolarProjectClient } from './solarProjectClient.js'

afterEach(cleanup)

const projectRailClients = []
afterEach(() => { cleanup(); for (const client of projectRailClients.splice(0)) client.dispose() })
const projectRailBinding = { kind: 'project', organization_id: '11111111-1111-1111-1111-111111111111',
  project_id: '22222222-2222-2222-2222-222222222222', drawing_id: '33333333-3333-3333-3333-333333333333',
  input_version_id: '44444444-4444-4444-4444-444444444444' }
const projectRailNext = { ...projectRailBinding, input_version_id: '55555555-5555-5555-5555-555555555555' }
const projectRailDigest = 'sha256:' + 'd'.repeat(64)
const projectRailToolOrder = [
  'solar-settings', 'solar-panels-from-drawing', 'solar-size-strings',
  'solar-string-add', 'solar-string-multi-add',
  'solar-solve-proposal', 'solar-commit-solve', 'solar-correct-string',
  'solar-string-conductors', 'solar-assign-equipment', 'solar-homeruns', 'solar-feeders',
  'solar-schedule', 'solar-string-data', 'solar-electrical-schedules', 'solar-cable-export',
]
function projectRailFamilies(availability = {}) {
  return [{ capabilities: [{ ...row('solar-assign-equipment', 70, 'Assign equipment',
    { ...READY, runnable: true, admission_checked: true, admission: { status_code: 200, error: null }, ...availability }),
    catalog_digest: projectRailDigest }] }]
}
async function projectRailHarness() {
  const h = { families: projectRailFamilies(), unsubscribes: [] }
  h.fetchImpl = vi.fn(async (url) => {
    let data = { families: h.families }
    if (url.endsWith('/checkout')) {
      const binding = h.real.getSnapshot().binding
      data = { organization_id: binding.organization_id, project_id: binding.project_id, drawing_id: binding.drawing_id,
        acquired: true, checkout: { holder: 'synthetic editor', acquired: '2026-01-01T00:00:00Z',
          expires: '2099-01-01T00:00:00Z', fence: '1' }, checkout_capability: 'lco1.' + 'c'.repeat(64) }
    }
    return { ok: true, status: 200, json: async () => data }
  })
  h.real = createSolarProjectClient({ fetchImpl: h.fetchImpl })
  h.real.setScope(projectRailBinding, 'epoch-1')
  projectRailClients.push(h.real)
  h.client = { ...h.real, subscribe: vi.fn((listener) => {
    const unsubscribe = vi.fn(h.real.subscribe(listener))
    h.unsubscribes.push(unsubscribe)
    return unsubscribe
  }) }
  h.catalog = async () => {
    const snapshot = h.real.getSnapshot()
    const isCurrent = h.real.captureCatalogScope()
    const outcome = await h.real.catalog()
    expect(outcome.ok).toBe(true)
    return { binding: snapshot.binding, authorityKey: snapshot.authorityKey,
      client: h.client, families: outcome.data.families, isCurrent }
  }
  h.props = { solarBinding: projectRailBinding, authorityKey: 'epoch-1', projectClient: h.client,
    projectCatalog: await h.catalog(), onOpenStep: vi.fn() }
  return h
}

it('R11-04 artifact history and announcements cannot cross project artifact or authority boundaries', async () => {
  const h = await projectRailHarness()
  const run = { binding: projectRailBinding, authorityKey: 'epoch-1', tool: 'solar-assign-equipment' }
  const memory = recordProjectSolarFlowRun(null, run, { terminal: true, ok: true })
  const view = render(<SolarFlowRail {...h.props} projectRunMemory={memory} />)
  expect(screen.getByTestId('solar-flow-announce').textContent).toBe('')
  h.real.setScope(projectRailNext, 'epoch-1')
  h.props = { ...h.props, solarBinding: projectRailNext, projectCatalog: await h.catalog(), projectRunMemory: memory }
  view.rerender(<SolarFlowRail {...h.props} />)
  expect(projectSolarFlowRunsFor(memory, projectRailNext, 'epoch-1')).toBe(memory.runs)
  expect(screen.getByTestId('solar-flow-announce').textContent).toBe('')
  const failed = recordProjectSolarFlowRun(memory, { ...run, binding: projectRailNext }, { terminal: true, ok: false, reason_code: 'STALE_GRAPH_REVISION' })
  view.rerender(<SolarFlowRail {...h.props} projectRunMemory={failed} />)
  await waitFor(() => expect(screen.getByTestId('solar-flow-announce').textContent).toBe('Assign equipment failed: STALE_GRAPH_REVISION'))
  for (const [binding, authorityKey] of [[projectRailNext, 'epoch-2'],
    [{ ...projectRailNext, project_id: '66666666-6666-6666-6666-666666666666' }, 'epoch-1'],
    [{ ...projectRailNext, drawing_id: '66666666-6666-6666-6666-666666666666' }, 'epoch-1'],
    [{ ...projectRailNext, organization_id: '66666666-6666-6666-6666-666666666666' }, 'epoch-1']]) {
    h.real.setScope(binding, authorityKey)
    view.rerender(<SolarFlowRail {...h.props} solarBinding={binding} authorityKey={authorityKey}
      projectCatalog={await h.catalog()} projectRunMemory={failed} />)
    expect(projectSolarFlowRunsFor(failed, binding, authorityKey)).toEqual({})
    expect(Object.isFrozen(projectSolarFlowRunsFor(failed, binding, authorityKey))).toBe(true)
    expect(screen.getByTestId('solar-flow-announce').textContent).toBe('')
  }
  for (const authority of [null, '', ' ', 'x'.repeat(257)]) expect(projectSolarFlowScope(projectRailBinding, authority)).toBeNull()
  expect(projectSolarFlowScope(null, 'epoch-1')).toBeNull()
  view.unmount()
  expect(h.unsubscribes.every((unsubscribe) => unsubscribe.mock.calls.length === 1)).toBe(true)
})


it('R9-42 project rail opens each specialized form for canonical context and same-intent handoff', async () => {
  for (const name of ['solar-size-strings', 'solar-string-conductors']) {
    const h = await projectRailHarness()
    const review = { ...READY, input_ready: false, runnable: false, admission_checked: false,
      admission: null, refusal_reasons: ['project_request_required'] }
    const label = name === 'solar-size-strings' ? 'Size strings' : 'String conductors'
    const step = { ...row(name, 20, label, review), catalog_digest: projectRailDigest }
    if (name === 'solar-string-conductors') step.params = conductorDeclaration.record.params
    h.families = [{ capabilities: [step] }]
    const graph = { rev: 7, settings: { id: 'S', panels_in_sequence: 3, extra: {} },
      project: { zip_code: '' }, panels: [{ id: 'P1' }], electrical_zones: [],
      strings: [{ id: 'T1', circuit_tag: 'A1', wire_gauge: '' }] }
    const context = { context: { ...projectRailBinding, head_version_id: projectRailBinding.input_version_id,
      is_head: true, intake_sha256: 'a'.repeat(64) },
    intake: { solar_design_graph: graph, solar_design_graph_sha256: 'b'.repeat(64) } }
    h.fetchImpl.mockImplementation(async (url) => {
      const data = url.includes('/context?') ? context : { families: h.families }
      return { ok: true, status: 200, json: async () => data }
    })
    h.client.readContext = vi.fn(h.real.readContext)
    h.client.catalog = vi.fn(h.real.catalog)
    h.props = { ...h.props, projectCatalog: await h.catalog(), catalogDigest: projectRailDigest,
      newProjectIntentKey: vi.fn(() => 'r9-rail-key'), onProjectSubmit: vi.fn(async () => ({
        ok: true, status: 202, data: { status: 'submitted', job_id: '77777777-7777-7777-7777-777777777777' },
      })), readIntake: vi.fn(), onSubmit: vi.fn() }
    function Host() {
      const [opened, setOpened] = React.useState(null)
      return <>
        <SolarFlowRail {...h.props} onOpenStep={(next) => { h.props.onOpenStep(next); setOpened(next) }} />
        {opened && <SolarStepEditor {...h.props} row={opened} onClose={() => setOpened(null)} />}
      </>
    }
    render(<Host />)
    const open = screen.getByRole('button', { name: new RegExp('^' + label) })
    expect(open.disabled).toBe(false)
    expect(open.closest('li').getAttribute('data-status')).toBe('review')
    const admitted = { ...step, availability: { ...READY, runnable: true, admission_checked: true,
      admission: { status_code: 200, error: null } } }
    h.families = [{ capabilities: [admitted] }]
    fireEvent.click(open)
    if (name === 'solar-string-conductors') {
      await waitFor(() => expect(screen.getByLabelText('Select A1')).toBeTruthy())
      fireEvent.click(screen.getByRole('button', { name: 'Select all' }))
      fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
    }
    const action = name === 'solar-size-strings' ? 'Review & run' : 'Apply to selected strings'
    await waitFor(() => expect(screen.getByRole('button', { name: action }).disabled).toBe(false))
    const intent = h.client.catalog.mock.calls[0][0].intents[0]
    fireEvent.click(screen.getByRole('button', { name: action }))
    await waitFor(() => expect(screen.getByText('Request submitted.')).toBeTruthy())
    expect(h.props.onOpenStep).toHaveBeenCalledExactlyOnceWith(step)
    expect(h.client.readContext).toHaveBeenCalledTimes(2)
    expect(h.props.onProjectSubmit.mock.calls[0][0]).toBe(intent)
    expect(h.props.readIntake).not.toHaveBeenCalled()
    expect(h.props.onSubmit).not.toHaveBeenCalled()
    expect(open.closest('li').getAttribute('data-status')).toBe('review')
    expect(screen.getByTestId('solar-flow-announce').textContent).toBe('')
    cleanup()
  }
})

it('R11-15 stale catalog provenance withdraws project actions resume and readiness at render and click time', async () => {
  const h = await projectRailHarness()
  const catalogV = h.props.projectCatalog
  const view = render(<SolarFlowRail {...h.props} />)
  const open = screen.getByRole('button', { name: new RegExp('^Assign equipment') })
  expect(open.getAttribute('aria-current')).toBe('step')
  let live = true
  view.rerender(<SolarFlowRail {...h.props} projectCatalog={{ ...catalogV, isCurrent: () => live && catalogV.isCurrent() }} />)
  live = false
  fireEvent.click(screen.getByRole('button', { name: new RegExp('^Assign equipment') }))
  expect(h.props.onOpenStep).not.toHaveBeenCalled()
  view.rerender(<SolarFlowRail {...h.props} solarBinding={projectRailNext} />)
  expect(screen.getByTestId('project-solar-flow-reason').textContent).toBe(projectSolarFlowReason('catalog_pending'))
  expect(screen.queryAllByRole('button').map((button) => button.id)).toEqual(projectRailToolOrder.map(solarFlowStepId))
  expect(screen.queryAllByRole('button').every((button) => button.disabled)).toBe(true)
  expect(screen.queryByText('Resume here')).toBeNull()
  h.real.setScope(projectRailNext, 'epoch-1')
  h.families = projectRailFamilies({ runnable: false, refusal_reasons: ['GRAPH_NOT_EMBEDDED'],
    admission: { status_code: 409, error: { reason_code: 'GRAPH_NOT_EMBEDDED' } } })
  h.props = { ...h.props, solarBinding: projectRailNext, projectCatalog: await h.catalog() }
  view.rerender(<SolarFlowRail {...h.props} />)
  // The old V catalog was never recorded under W; actual artifact readiness recorded on V still survives.
  expect(document.getElementById(solarFlowStepId('solar-assign-equipment')).closest('li').getAttribute('data-invalidated')).toBe('true')
  h.families = projectRailFamilies({ runnable: false, input_ready: false, admission_checked: false,
    admission: null, refusal_reasons: ['project_request_required'] })
  h.props = { ...h.props, projectCatalog: await h.catalog() }
  view.rerender(<SolarFlowRail {...h.props} />)
  expect(document.getElementById(solarFlowStepId('solar-assign-equipment')).closest('li').getAttribute('data-invalidated')).toBeNull()
  expect(screen.getByText('Review inputs')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: new RegExp('^Assign equipment') }))
  expect(h.props.onOpenStep).toHaveBeenCalledTimes(1)
  expect(h.props.onOpenStep.mock.calls[0][0]).toBe(h.props.projectCatalog.families[0].capabilities[0])
  for (const projectCatalog of [undefined, { ...h.props.projectCatalog, authorityKey: 'epoch-old' },
    { ...h.props.projectCatalog, binding: projectRailBinding }, { ...h.props.projectCatalog, client: {} },
    { ...h.props.projectCatalog, isCurrent: () => false }]) {
    view.rerender(<SolarFlowRail {...h.props} projectCatalog={projectCatalog} />)
    expect(screen.queryAllByRole('button').map((button) => button.id)).toEqual(projectRailToolOrder.map(solarFlowStepId))
    expect(screen.queryAllByRole('button').every((button) => button.disabled)).toBe(true)
    expect(screen.queryByText('Resume here')).toBeNull()
    expect(screen.getByTestId('project-solar-flow-reason').textContent).toBe(projectSolarFlowReason('catalog_pending'))
  }
  view.rerender(<SolarFlowRail {...h.props} />)
  await act(async () => { await h.real.acquireCheckout() })
  expect(screen.queryAllByRole('button').map((button) => button.id)).toEqual(projectRailToolOrder.map(solarFlowStepId))
  expect(screen.queryAllByRole('button').every((button) => button.disabled)).toBe(true)
  expect(screen.getByTestId('project-solar-flow-reason').textContent).toBe(projectSolarFlowReason('catalog_pending'))
  expect(h.props.onOpenStep).toHaveBeenCalledTimes(1)
})

it('R11-29 a rail given only a project id is project mode and refuses without a binding', () => {
  const legacyReady = { ...row('solar-assign-equipment', 70, 'Assign equipment',
    { ...READY, runnable: false, admission_checked: false, admission: null }), catalog_digest: projectRailDigest }
  const onOpenStep = vi.fn()
  const legacy = { families: [{ capabilities: [legacyReady] }], familiesDrawingId: 'dwg-1', drawingId: 'dwg-1', onOpenStep }
  const view = render(<SolarFlowRail {...legacy} projectId={projectRailBinding.project_id} />)
  expect(screen.getByTestId('project-solar-flow-reason').textContent).toBe(projectSolarRailReason('binding_invalid'))
  expect(screen.queryAllByRole('button').map((button) => button.id)).toEqual(projectRailToolOrder.map(solarFlowStepId))
  expect(screen.queryAllByRole('button').every((button) => button.disabled)).toBe(true)
  expect(screen.queryByText('Resume here')).toBeNull()
  expect(onOpenStep).not.toHaveBeenCalled()
  // The same props with a null project id stay standalone, where the four legacy flags arm the step.
  view.rerender(<SolarFlowRail {...legacy} projectId={null} />)
  expect(screen.queryByTestId('project-solar-flow-reason')).toBeNull()
  const open = screen.getByRole('button', { name: new RegExp('^Assign equipment') })
  expect(open.disabled).toBe(false)
  fireEvent.click(open)
  expect(onOpenStep).toHaveBeenCalledTimes(1)
  view.rerender(<SolarFlowRail {...legacy} projectId={projectRailBinding.project_id} />)
  expect(screen.getByTestId('project-solar-flow-reason').textContent).toBe(projectSolarRailReason('binding_invalid'))
  expect(screen.queryAllByRole('button').map((button) => button.id)).toEqual(projectRailToolOrder.map(solarFlowStepId))
  expect(screen.queryAllByRole('button').every((button) => button.disabled)).toBe(true)
  expect(onOpenStep).toHaveBeenCalledTimes(1)
})

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
        ['ground-physical', ['Terrain', 'Native frame layout', 'Grade pads and native piles', 'Terrain and frame shade', 'Terrain and shade CSV'],
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
  it('FORM33 a nullable boolean retained value permits review and Retry', () => {
    const step = { ...row('solar-homeruns', 80, 'Homeruns', READY),
      params: { properties: { x: { type: ['boolean', 'null'], default: null } } } }
    const onSubmit = vi.fn(), onClose = vi.fn()
    render(<SolarStepEditor row={step} retained={{ x: null }} status="failed" onSubmit={onSubmit} onClose={onClose} />)
    const input = screen.getByLabelText('X')
    expect(input.getAttribute('aria-invalid')).toBe('false')
    expect(input.validationMessage).toBe('')
    expect(screen.queryByText('X has a value this tool no longer accepts. Choose or enter a new one.')).toBeNull()
    expect(screen.getByRole('alert').textContent).toBe('This run failed. Your inputs are kept.')
    for (const name of ['Review & run', 'Retry']) {
      const button = screen.getByRole('button', { name })
      expect(button.disabled).toBe(false)
      fireEvent.click(button)
    }
    expect(onSubmit.mock.calls).toEqual([[step, { x: null }], [step, { x: null }]])
    expect(onClose).not.toHaveBeenCalled()
  })

  it('FORM44 a nullable sibling keeps step revision intake automatic after another field edit', async () => {
    const original = row('solar-homeruns', 80, 'Homeruns', READY)
    const step = { ...original, params: { ...original.params, properties: { ...original.params.properties,
      x: { type: ['boolean', 'null'], default: null } } } }
    let resolve
    const readIntake = vi.fn(() => new Promise((done) => { resolve = done }))
    const onSubmit = vi.fn()
    render(<SolarStepEditor row={step} drawingId="d1" drawingVersion={3} readIntake={readIntake}
      onSubmit={onSubmit} onClose={vi.fn()} />)
    await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'b' } })
    await act(async () => { resolve({ version: 3, intake: { solar_design_graph: { rev: 7 } } }) })
    expect(screen.getByLabelText('Expected rev').value).toBe('7')
    const input = screen.getByLabelText('X')
    expect(input.getAttribute('aria-invalid')).toBe('false')
    expect(input.validationMessage).toBe('')
    expect(screen.queryByText('X has a value this tool no longer accepts. Choose or enter a new one.')).toBeNull()
    const run = screen.getByRole('button', { name: 'Review & run' })
    expect(run.disabled).toBe(false)
    fireEvent.click(run)
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith(step, { expected_rev: 7, note: 'b', x: null })
  })

  it('FORM22 invalid step JSON blocks both review and Retry', () => {
    const step = { ...row('solar-homeruns', 80, 'Homeruns', READY),
      params: { properties: { changes: { type: 'object', default: {} } } } }
    const onSubmit = vi.fn()
    render(<SolarStepEditor row={step} status="failed" onSubmit={onSubmit} onClose={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{' } })
    for (const name of ['Review & run', 'Retry']) {
      const button = screen.getByRole('button', { name })
      expect(button.disabled).toBe(true); fireEvent.click(button)
    }
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('FORM23b clearing a step revision counts as a touch before intake', async () => {
    const step = row('solar-homeruns', 80, 'Homeruns', READY)
    let resolve
    const readIntake = vi.fn(() => new Promise((done) => { resolve = done }))
    const onSubmit = vi.fn()
    render(<SolarStepEditor row={step} drawingId="d1" drawingVersion={3} readIntake={readIntake}
      onSubmit={onSubmit} onClose={vi.fn()} />)
    await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
    fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '' } })
    await act(async () => { resolve({ version: 3, intake: { solar_design_graph: { rev: 7 } } }) })
    expect(screen.getByLabelText('Expected rev').value).toBe('')
    fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
    expect(onSubmit).toHaveBeenCalledWith(step, { note: 'a' })
  })

  it('FORM30 a change to another step field leaves the revision automatic', async () => {
    const step = row('solar-homeruns', 80, 'Homeruns', READY)
    let resolve
    const readIntake = vi.fn(() => new Promise((done) => { resolve = done }))
    const onSubmit = vi.fn()
    render(<SolarStepEditor row={step} drawingId="d1" drawingVersion={3} readIntake={readIntake}
      onSubmit={onSubmit} onClose={vi.fn()} />)
    await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'b' } })
    await act(async () => { resolve({ version: 3, intake: { solar_design_graph: { rev: 7 } } }) })
    expect(screen.getByLabelText('Expected rev').value).toBe('7')
    fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
    expect(onSubmit).toHaveBeenCalledWith(step, { expected_rev: 7, note: 'b' })
  })

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
      // A project id without its canonical binding reaches R10's project composer, which refuses
      // with the project sentence; the two standalone scopes keep the standalone sentence.
      for (const [scope, reason] of [[{ projectId: 'p1' }, PROJECT_STRING_COMPOSER_REASONS.binding_invalid],
        [{ drawingId: '' }, SOLAR_STRING_COMPOSER_REASONS.scope],
        [{ drawingVersion: null }, SOLAR_STRING_COMPOSER_REASONS.scope]]) {
        const { props, container } = await mountString(toolName, ground, scope)
        expect(screen.getByText(reason)).toBeTruthy()
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
    expect(screen.getByRole('alert').textContent).toContain('The drawing changed. Review it before trying again. Your inputs are kept.')
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

const R13D_RAIL_NAMES = PROJECT_SOLAR_ROOFTOP_STAGES.flatMap((stage) => stage.capabilities)
const R13D_CORE_LABELS = ['Settings', 'Panel import', 'Manual sizing', 'Single strings', 'Multiple strings',
  'Solve', 'Commit proposal', 'Correct strings', 'Conductors', 'Equipment']
function r13dRailRow(name, ready = false) {
  const index = R13D_RAIL_NAMES.indexOf(name)
  const candidate = { ...row(name, 999 - Math.max(index, 0), R13D_CORE_LABELS[index] || name,
    { ...READY, runnable: ready, input_ready: ready, admission_checked: ready,
      admission: ready ? { status_code: 200, error: null } : null,
      refusal_reasons: ready ? [] : ['project_request_required'] }), catalog_digest: projectRailDigest }
  candidate.solar.wave = index === 3 || index === 4 ? 2 : 5
  if (name === 'solar-string-conductors') candidate.params = conductorDeclaration.record.params
  if (name === 'solar-assign-equipment') candidate.params = { type: 'object', properties: {
    equipment: { type: 'array', items: { type: 'object' } },
    assignments: { type: 'array', items: { type: 'object' } },
    cancel: { type: 'boolean', default: false }, preview: { type: 'boolean', default: false },
  } }
  return candidate
}
function r13dRailContext() {
  const graph = structuredClone(ground)
  graph.strings = [{ id: 'T1', circuit_tag: 'A1', wire_gauge: '', ordered_panel_refs: [] }]
  return { context: { ...projectRailBinding, head_version_id: projectRailBinding.input_version_id,
    is_head: true, intake_sha256: 'a'.repeat(64) },
  intake: { solar_design_graph: graph, solar_design_graph_sha256: 'b'.repeat(64) } }
}
async function r13dRailHarness(names = R13D_RAIL_NAMES) {
  const h = await projectRailHarness()
  h.rows = names.map((name) => r13dRailRow(name))
  h.families = [{ capabilities: [...h.rows].reverse() }]
  h.props = { ...h.props, projectCatalog: await h.catalog(), catalogDigest: projectRailDigest,
    newProjectIntentKey: vi.fn(() => 'r13d-intent'), onProjectSubmit: vi.fn(async () => ({
      ok: true, status: 202, data: { status: 'submitted', job_id: '77777777-7777-7777-7777-777777777777' },
    })), readIntake: vi.fn(), onSubmit: vi.fn(), onClose: vi.fn() }
  h.client.readContext = vi.fn(h.real.readContext)
  h.client.catalog = vi.fn(h.real.catalog)
  h.context = r13dRailContext()
  h.fetchImpl.mockImplementation(async (url) => {
    if (url.endsWith('/checkout')) return { ok: true, status: 200, json: async () => ({
      organization_id: projectRailBinding.organization_id, project_id: projectRailBinding.project_id,
      drawing_id: projectRailBinding.drawing_id, acquired: true,
      checkout: { holder: 'synthetic editor', acquired: '2026-01-01T00:00:00Z',
        expires: '2099-01-01T00:00:00Z', fence: '1' }, checkout_capability: 'lco1.' + 'c'.repeat(64),
    }) }
    const data = url.includes('/context?') ? h.context : {
      families: [{ capabilities: h.rows.map((entry) => ({ ...entry, availability: {
        ...READY, runnable: true, admission_checked: true, admission: { status_code: 200, error: null },
      } })) }],
    }
    return { ok: true, status: 200, json: async () => data }
  })
  return h
}
function r13dRailButton(name) { return document.getElementById(solarFlowStepId(name)) }
function R13DOpenHost({ h }) {
  const [opened, setOpened] = React.useState(null)
  return <>
    <SolarFlowRail {...h.props} onOpenStep={(entry) => { h.props.onOpenStep(entry); setOpened(entry) }} />
    {opened && <SolarStepEditor {...h.props} row={opened} onClose={() => setOpened(null)} />}
  </>
}
function r13dAssertHandoff(h, name) {
  expect(h.props.onOpenStep).toHaveBeenCalledExactlyOnceWith(h.rows.find((entry) => entry.name === name))
  expect(h.props.readIntake).not.toHaveBeenCalled()
  expect(h.props.onSubmit).not.toHaveBeenCalled()
  expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
}

it('W1C5 the project Strings stage opens solve, commit and correction in registry order', async () => {
  const h = await r13dRailHarness()
  render(<SolarFlowRail {...h.props} />)
  const names = ['solar-solve-proposal', 'solar-commit-solve', 'solar-correct-string']
  expect(items().slice(5, 8).map(item => item.querySelector('button').id)).toEqual(names.map(solarFlowStepId))
  for (const name of names) {
    expect(r13dRailButton(name).disabled).toBe(false)
    fireEvent.click(r13dRailButton(name))
    expect(h.props.onOpenStep).toHaveBeenLastCalledWith(h.rows.find(row => row.name === name))
  }
  expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
})

it('R13D-17 settings is the first reachable project step', async () => {
  const h = await r13dRailHarness()
  render(<SolarFlowRail {...h.props} />)
  expect(items()[0].querySelector('button')).toBe(r13dRailButton('solar-settings'))
  expect(r13dRailButton('solar-settings').disabled).toBe(false)
  fireEvent.click(r13dRailButton('solar-settings'))
  r13dAssertHandoff(h, 'solar-settings')
})

it('R13D-18 panel import is reachable before sizing', async () => {
  const h = await r13dRailHarness()
  render(<SolarFlowRail {...h.props} />)
  expect(items()[1].querySelector('button')).toBe(r13dRailButton('solar-panels-from-drawing'))
  expect(items()[2].querySelector('button')).toBe(r13dRailButton('solar-size-strings'))
  fireEvent.click(r13dRailButton('solar-panels-from-drawing'))
  r13dAssertHandoff(h, 'solar-panels-from-drawing')
})

it('R13D-19 manual sizing is reachable after panel import', async () => {
  const h = await r13dRailHarness()
  render(<R13DOpenHost h={h} />)
  expect(items()[2].querySelector('button')).toBe(r13dRailButton('solar-size-strings'))
  fireEvent.click(r13dRailButton('solar-size-strings'))
  await waitFor(() => expect(screen.getByTestId('project-solar-sizing-form').getAttribute('data-mode')).toBe('edit'))
  expect(screen.getByLabelText('Scope').value).toBe('manual-global')
  r13dAssertHandoff(h, 'solar-size-strings')
})

it('R13D-20 single string composition is reachable', async () => {
  const h = await r13dRailHarness()
  render(<R13DOpenHost h={h} />)
  expect(items()[3].querySelector('button')).toBe(r13dRailButton(STRING_ADD_TOOL))
  fireEvent.click(r13dRailButton(STRING_ADD_TOOL))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Select row 1 Group 1' })).toBeTruthy())
  expect(screen.getByRole('region', { name: 'Selection queue' })).toBeTruthy()
  expect(screen.queryByLabelText('String length')).toBeNull()
  r13dAssertHandoff(h, STRING_ADD_TOOL)
})

it('R13D-21 multiple string composition is reachable', async () => {
  const h = await r13dRailHarness()
  render(<R13DOpenHost h={h} />)
  expect(items()[4].querySelector('button')).toBe(r13dRailButton(STRING_MULTI_ADD_TOOL))
  fireEvent.click(r13dRailButton(STRING_MULTI_ADD_TOOL))
  await waitFor(() => expect(screen.getByLabelText('String length')).toBeTruthy())
  expect(screen.getByRole('region', { name: 'Selection queue' })).toBeTruthy()
  r13dAssertHandoff(h, STRING_MULTI_ADD_TOOL)
})

it('R13D-22 conductors follow both string composers', async () => {
  const h = await r13dRailHarness()
  render(<R13DOpenHost h={h} />)
  expect(items().slice(4, 10).map((item) => item.querySelector('button').id)).toEqual([
    STRING_MULTI_ADD_TOOL, 'solar-solve-proposal', 'solar-commit-solve', 'solar-correct-string',
    'solar-string-conductors', 'solar-assign-equipment',
  ].map(solarFlowStepId))
  fireEvent.click(r13dRailButton('solar-string-conductors'))
  await waitFor(() => expect(screen.getByLabelText('Select A1')).toBeTruthy())
  expect(screen.getByLabelText('Conductor')).toBeTruthy()
  r13dAssertHandoff(h, 'solar-string-conductors')
})

it('R13D-23 equipment remains reachable after conductors', async () => {
  const h = await r13dRailHarness()
  render(<R13DOpenHost h={h} />)
  expect(items()[9].querySelector('button')).toBe(r13dRailButton('solar-assign-equipment'))
  fireEvent.click(r13dRailButton('solar-assign-equipment'))
  await waitFor(() => expect(screen.getByTestId('project-solar-equipment-form').getAttribute('data-mode')).toBe('edit'))
  expect(screen.getByLabelText('Equipment')).toBeTruthy()
  r13dAssertHandoff(h, 'solar-assign-equipment')
})

it('R13D-24 a refused stage shows its server reason', async () => {
  const h = await r13dRailHarness()
  const refused = r13dRailRow('solar-homeruns', true)
  refused.availability = { ...refused.availability, runnable: false, refusal_reasons: ['capability_not_ready', 'capability_not_ready'],
    admission: { status_code: 409, error: { reason_code: 'EQUIPMENT_ASSIGNMENT_REQUIRED', message: 'private envelope' } } }
  h.props.projectCatalog = { ...h.props.projectCatalog, families: [{ capabilities: [refused] }] }
  const view = render(<SolarFlowRail {...h.props} />)
  const open = r13dRailButton('solar-homeruns')
  expect(open.disabled).toBe(true)
  expect(document.getElementById(open.getAttribute('aria-describedby')).textContent).toBe(
    PROJECT_SOLAR_RAIL_REASONS.stage_refused + ' (capability_not_ready, EQUIPMENT_ASSIGNMENT_REQUIRED)')
  expect(view.container.textContent).not.toContain('private envelope')
  fireEvent.click(open)
  expect(h.props.onOpenStep).not.toHaveBeenCalled()
  expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
  expect(h.client.readContext).not.toHaveBeenCalled()
})

it('R13D-25 a missing stage stays visible with a sentence', async () => {
  const h = await r13dRailHarness(R13D_RAIL_NAMES.filter((name) => name !== 'solar-panels-from-drawing'))
  render(<SolarFlowRail {...h.props} />)
  const missing = r13dRailButton('solar-panels-from-drawing')
  expect(items()).toHaveLength(16)
  expect(missing.disabled).toBe(true)
  expect(document.getElementById(missing.getAttribute('aria-describedby')).textContent).toBe(PROJECT_SOLAR_RAIL_REASONS.stage_missing)
  expect(r13dRailButton('solar-settings').disabled).toBe(false)
  expect(r13dRailButton('solar-size-strings').disabled).toBe(false)
  fireEvent.click(missing)
  expect(h.props.onOpenStep).not.toHaveBeenCalled()
})

it('R13D-26 invalid or stale project scope retains disabled stages', async () => {
  const h = await r13dRailHarness()
  const view = render(<SolarFlowRail {...h.props} />)
  for (const [changes, code] of [
    [{ solarBinding: undefined, projectId: projectRailBinding.project_id }, 'binding_invalid'],
    [{ solarBinding: undefined }, 'binding_invalid'],
    [{ solarBinding: projectRailNext }, 'catalog_pending'],
    [{ authorityKey: 'epoch-other' }, 'catalog_pending'],
    [{ projectClient: {} }, 'catalog_pending'],
    [{ projectCatalog: { ...h.props.projectCatalog, client: {} } }, 'catalog_pending'],
    [{ projectCatalog: { ...h.props.projectCatalog, isCurrent: () => false } }, 'catalog_pending'],
    [{ projectCatalog: { ...h.props.projectCatalog, isCurrent: () => { throw new Error('unreadable') } } }, 'catalog_pending'],
  ]) {
    view.rerender(<SolarFlowRail {...h.props} {...changes} />)
    expect(items()).toHaveLength(16)
    for (const item of items()) {
      const open = item.querySelector('button')
      expect(open.disabled).toBe(true)
      expect(open.getAttribute('aria-current')).toBeNull()
      expect(document.getElementById(open.getAttribute('aria-describedby')).textContent).toBe(projectSolarRailReason(code))
      fireEvent.click(open)
    }
    expect(screen.queryByText('Resume here')).toBeNull()
  }
  const projectOnly = { ...h.props, projectId: projectRailBinding.project_id }
  delete projectOnly.solarBinding
  view.rerender(<SolarFlowRail {...projectOnly} />)
  expect(items().every((item) => item.querySelector('button').disabled)).toBe(true)
  view.rerender(<SolarFlowRail {...h.props} onOpenStep={undefined} />)
  expect(items().every((item) => item.querySelector('button').disabled)).toBe(true)
  expect(document.getElementById(r13dRailButton('solar-settings').getAttribute('aria-describedby')).textContent).toBe(PROJECT_SOLAR_RAIL_REASONS.form_unavailable)
  view.rerender(<SolarFlowRail {...h.props} />)
  await act(async () => { await h.real.acquireCheckout() })
  expect(items()).toHaveLength(16)
  expect(items().every((item) => item.querySelector('button').disabled)).toBe(true)
  expect(h.props.onOpenStep).not.toHaveBeenCalled()
})

it('R13D-27 busy reaches every specialized project editor and clears', async () => {
  for (const name of ['solar-size-strings', 'solar-string-conductors', STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL, 'solar-assign-equipment']) {
    const h = await r13dRailHarness()
    const candidate = h.rows.find((entry) => entry.name === name)
    const withdrawsSubmit = ['solar-size-strings', 'solar-string-conductors'].includes(name)
    for (const busy of [false, true, false]) {
      const boundary = SolarStepEditor({ ...h.props, row: candidate, busy })
      const child = React.Children.toArray(boundary.props.children).at(-1)
      const forwarded = child.props.projectProps ?? child.props
      expect(boundary.props.disabled).toBe(busy)
      expect(forwarded.busy).toBe(busy)
      expect(forwarded.onProjectSubmit).toBe(busy && withdrawsSubmit ? undefined : h.props.onProjectSubmit)
    }
    function Host() {
      const [busy, setBusy] = React.useState(false)
      return <>
        <button onClick={() => setBusy((value) => !value)}>Toggle busy</button>
        <SolarFlowRail {...h.props} busy={busy} />
        <SolarStepEditor {...h.props} row={candidate} busy={busy} />
      </>
    }
    render(<Host />)
    let draftLabel = null, draftValue = null
    if ([STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL].includes(name)) {
      await waitFor(() => expect(screen.getByLabelText('Search rows')).toBeTruthy())
      fireEvent.click(screen.getByRole('button', { name: 'Select row 1 Group 1' }))
      fireEvent.change(screen.getByLabelText('First slot'), { target: { value: '1' } })
      fireEvent.change(screen.getByLabelText('Last slot'), { target: { value: '3' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add range' }))
      if (name === STRING_MULTI_ADD_TOOL) fireEvent.change(screen.getByLabelText('String length'), { target: { value: '3' } })
      draftLabel = 'Search rows'; draftValue = 'Group 1'
    } else if (name === 'solar-string-conductors') {
      await waitFor(() => expect(screen.getByLabelText('Select A1')).toBeTruthy())
      fireEvent.click(screen.getByRole('button', { name: 'Select all' }))
      draftLabel = 'Conductor'; draftValue = '10 AWG'
    } else if (name === 'solar-assign-equipment') {
      await waitFor(() => expect(screen.getByLabelText('Equipment')).toBeTruthy())
      draftLabel = 'Equipment'; draftValue = '[{"id":"kept"}]'
    } else await waitFor(() => expect(screen.getByLabelText('Scope')).toBeTruthy())
    if (draftLabel) fireEvent.change(screen.getByLabelText(draftLabel), { target: { value: draftValue } })
    const boundary = screen.getByTestId('project-solar-step-boundary')
    const mountedEditor = boundary.querySelector('#solar-step-editor')
    const draft = screen.getByLabelText(draftLabel ?? 'Scope')
    const action = name === 'solar-string-conductors' ? 'Apply to selected strings' : 'Review & run'
    await waitFor(() => expect(screen.getByRole('button', { name: action }).disabled).toBe(false))
    expect(boundary.disabled).toBe(false)
    if (name === 'solar-string-conductors') {
      const checkbox = within(screen.getByLabelText('Select A1')).getByRole('checkbox', { name: 'Include A1' })
      expect(checkbox).toBeChecked()
      expect(checkbox).toBeEnabled()
    }
    fireEvent.click(screen.getByRole('button', { name: 'Toggle busy' }))
    expect(screen.getByTestId('project-solar-step-boundary')).toBe(boundary)
    expect(boundary.disabled).toBe(true)
    expect(document.getElementById(boundary.getAttribute('aria-describedby')).textContent).toBe(PROJECT_SOLAR_RAIL_REASONS.run_in_progress)
    expect(items().every((item) => item.querySelector('button').disabled)).toBe(true)
    expect(screen.queryByText('Resume here')).toBeNull()
    expect(boundary.querySelector('#solar-step-editor')).toBe(mountedEditor)
    if (name === 'solar-string-conductors') {
      // Busy project forms withdraw the rows until editing resumes.
      expect(screen.queryByLabelText('Select A1')).toBeNull()
      expect(within(screen.getByRole('table', { name: 'String conductors' })).queryByRole('checkbox')).toBeNull()
      expect(draft).toBeDisabled()
    }
    if (draftLabel) {
      expect(screen.getByLabelText(draftLabel)).toBe(draft)
      expect(draft.value).toBe(draftValue)
    }
    if ([STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL].includes(name)) {
      expect(screen.getByText('1 selections')).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Remove selection 1' })).toBeTruthy()
      if (name === STRING_MULTI_ADD_TOOL) expect(screen.getByLabelText('String length').value).toBe('3')
    }
    fireEvent.click(screen.getByRole('button', { name: action }))
    await act(async () => {})
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Toggle busy' }))
    expect(boundary.disabled).toBe(false)
    if (draftLabel) await waitFor(() => expect(screen.getByLabelText(draftLabel).value).toBe(draftValue))
    else await waitFor(() => expect(screen.getByLabelText('Scope').value).toBe('manual-global'))
    expect(boundary.querySelector('#solar-step-editor')).toBe(mountedEditor)
    if (draftLabel) expect(screen.getByLabelText(draftLabel)).toBe(draft)
    if (name === 'solar-string-conductors') {
      await waitFor(() => expect(within(screen.getByLabelText('Select A1')).getByRole('checkbox', { name: 'Include A1' })).toBeChecked())
      expect(within(screen.getByLabelText('Select A1')).getByRole('checkbox', { name: 'Include A1' })).toBeEnabled()
      expect(draft).toBeEnabled()
    }
    if ([STRING_ADD_TOOL, STRING_MULTI_ADD_TOOL].includes(name)) {
      expect(screen.getByText('1 selections')).toBeTruthy()
      if (name === STRING_MULTI_ADD_TOOL) expect(screen.getByLabelText('String length').value).toBe('3')
    }
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    expect(r13dRailButton(name).disabled).toBe(false)
    expect(h.props.readIntake).not.toHaveBeenCalled()
    expect(h.props.onSubmit).not.toHaveBeenCalled()
    cleanup()
  }
}, 15000)

it('R13D-28 pending status is scoped independently of admission', async () => {
  const h = await r13dRailHarness()
  const pending = { tool: 'solar-size-strings', binding: projectRailBinding, authorityKey: 'epoch-1' }
  const view = render(<SolarFlowRail {...h.props} projectPending={pending} busy />)
  const sizing = () => r13dRailButton(pending.tool)
  expect(sizing().closest('li').getAttribute('data-status')).toBe('pending')
  expect(within(sizing()).getByText('Running')).toBeTruthy()
  expect(sizing().disabled).toBe(true)
  expect(sizing().getAttribute('aria-current')).toBeNull()
  expect(screen.getByTestId('solar-flow-announce').textContent).toBe('')
  for (const foreign of [{ ...pending, binding: projectRailNext }, { ...pending, authorityKey: 'epoch-other' }]) {
    view.rerender(<SolarFlowRail {...h.props} projectPending={foreign} busy />)
    expect(sizing().closest('li').getAttribute('data-status')).toBe('review')
    expect(sizing().disabled).toBe(true)
  }
  view.rerender(<SolarFlowRail {...h.props} projectPending={pending} busy={false} />)
  expect(sizing().closest('li').getAttribute('data-status')).toBe('pending')
  expect(sizing().disabled).toBe(false)
  view.rerender(<SolarFlowRail {...h.props} busy={false} />)
  expect(sizing().closest('li').getAttribute('data-status')).toBe('review')
  expect(sizing().disabled).toBe(false)
  expect(h.rows.find((entry) => entry.name === pending.tool).availability.runnable).toBe(false)
})

it('R13D-29 standalone rendering remains byte-identical', () => {
  for (const [file, anchor, bytes, digest] of [
    ['SolarFlowRail.jsx', 'function StandaloneSolarFlowRail(', 6264, '7c3da7e370022baaa307037174bb8f2ce507ad0b8a7fb6c54bedaf24c819805e'],
    ['SolarStepEditor.jsx', 'function StandaloneSolarStepEditor(', 4601, '9d7dd3ef5cb3af31d33113f60cb7a24543c8a6404fdb772e48c8653e88d9c5fe'],
  ]) {
    const source = readFileSync(resolve(process.cwd(), 'src/solar', file))
    const suffix = source.subarray(source.indexOf(Buffer.from(anchor)))
    expect(suffix.length).toBe(bytes)
    expect(createHash('sha256').update(suffix).digest('hex')).toBe(digest)
  }
  for (const catalog of [families([]), families(W1.map(([name]) => name)), LIVE, []]) {
    const view = render(<SolarFlowRail families={catalog} drawingId="d1" onOpenStep={vi.fn()} busy={false} />)
    const clear = view.container.innerHTML
    view.rerender(<SolarFlowRail families={catalog} drawingId="d1" onOpenStep={vi.fn()} busy />)
    expect(view.container.innerHTML).toBe(clear)
    cleanup()
  }
  for (const status of [null, 'pending', 'failed']) {
    const props = { row: row('solar-homeruns', 80, 'Homeruns', READY), drawingId: 'd1', status,
      onSubmit: vi.fn(), onClose: vi.fn() }
    const view = render(<SolarStepEditor {...props} busy={false} />)
    const clear = view.container.innerHTML
    view.rerender(<SolarStepEditor {...props} busy />)
    expect(view.container.innerHTML).toBe(clear)
    expect(screen.queryByTestId('project-solar-step-boundary')).toBeNull()
    cleanup()
  }
})

it('R13D-30 standalone interaction contracts remain unchanged', async () => {
  const shown = families(['solar-settings', 'solar-size-strings'])
  const onOpenStep = vi.fn(), onFlowChange = vi.fn(), onClose = vi.fn(), onSubmit = vi.fn()
  const rail = render(<SolarFlowRail families={shown} drawingId="d1" familiesDrawingId="d1"
    onOpenStep={onOpenStep} onFlowChange={onFlowChange} busy />)
  expect(button('Size strings').getAttribute('aria-current')).toBe('step')
  fireEvent.click(button('Size strings'))
  expect(onOpenStep).toHaveBeenCalledExactlyOnceWith(shown[0].capabilities[1])
  fireEvent.click(button('Homeruns'))
  expect(onOpenStep).toHaveBeenCalledTimes(1)
  changeFlow('solaredge-import')
  expect(onFlowChange).toHaveBeenCalledExactlyOnceWith('solaredge-import')
  expect(screen.getByTestId('solar-flow-unavailable')).toBeTruthy()
  changeFlow('rooftop')
  rail.rerender(<SolarFlowRail families={shown} drawingId="d1" familiesDrawingId="d1" onOpenStep={onOpenStep}
    runs={{ 'solar-homeruns': { ok: false, code: 'STALE_GRAPH_REVISION' } }} />)
  await waitFor(() => expect(screen.getByTestId('solar-flow-announce').textContent).toBe('Homeruns failed: STALE_GRAPH_REVISION'))
  // The focus contract needs a focusable step button: re-render the rail with Homeruns runnable.
  rail.rerender(<SolarFlowRail families={families(['solar-settings', 'solar-size-strings', 'solar-homeruns'])} drawingId="d1"
    familiesDrawingId="d1" onOpenStep={onOpenStep} runs={{ 'solar-homeruns': { ok: false, code: 'STALE_GRAPH_REVISION' } }} />)
  expect(button('Homeruns').disabled).toBe(false)
  const editorProps = { row: shown[0].capabilities[7], drawingId: 'd1', status: 'failed',
    failureCode: 'STALE_GRAPH_REVISION', onSubmit, onClose }
  const editor = render(<SolarStepEditor {...editorProps} busy={false} />)
  const clearMarkup = editor.container.innerHTML
  editor.rerender(<SolarStepEditor {...editorProps} busy />)
  expect(editor.container.innerHTML).toBe(clearMarkup)
  fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'kept' } })
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  expect(onSubmit).toHaveBeenCalledExactlyOnceWith(shown[0].capabilities[7], { expected_rev: 0, note: 'kept' })
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(document.activeElement).toBe(button('Homeruns'))
  fireEvent.keyDown(screen.getByLabelText('Note'), { key: 'Escape' })
  expect(onClose).toHaveBeenCalledTimes(2)
  expect(document.activeElement).toBe(button('Homeruns'))
  cleanup()
  render(<SolarFlowRail families={LIVE} drawingId="d1" onOpenStep={onOpenStep}
    workspacePanelsByFlow={{ 'solaredge-import': ['solaredge-import'] }} />)
  expect(items().map((item) => item.querySelector('button').id)).toEqual(ROOFTOP_LIVE.map(solarFlowStepId))
})

it('R13D-31 other project flows retain visible unavailable stages', async () => {
  const h = await r13dRailHarness()
  render(<SolarFlowRail {...h.props} workspacePanelsByFlow={{
    'ground-physical': ['landxml', 'terrain', 'civil', 'physical-read'], 'solaredge-import': ['solaredge-import'],
  }} />)
  changeFlow('ground-electrical')
  const expectedNames = SOLAR_FLOWS.find((flow) => flow.id === 'ground-electrical').stages.flatMap((stage) => stage.capabilities)
  expect(items().map((item) => item.querySelector('button').id)).toEqual(expectedNames.map(solarFlowStepId))
  // Feeders is present but unchecked in the complete Rooftop catalog; removing it leaves all neighbors visible.
  cleanup()
  const noFeeders = { ...h.props, projectCatalog: { ...h.props.projectCatalog,
    families: [{ capabilities: h.rows.filter((entry) => entry.name !== 'solar-feeders') }] } }
  render(<SolarFlowRail {...noFeeders} workspacePanelsByFlow={{
    'ground-physical': ['landxml', 'terrain', 'civil', 'physical-read'], 'solaredge-import': ['solaredge-import'],
  }} />)
  changeFlow('ground-electrical')
  // The live button is read again after the re-render: an element saved earlier is the detached one.
  const missingFeeder = r13dRailButton('solar-feeders')
  expect(missingFeeder.isConnected).toBe(true)
  expect(missingFeeder.disabled).toBe(true)
  fireEvent.click(missingFeeder)
  expect(items().map((item) => item.querySelector('button').id)).toEqual(expectedNames.map(solarFlowStepId))
  expect(document.getElementById(r13dRailButton('solar-feeders').getAttribute('aria-describedby')).textContent).toBe(PROJECT_SOLAR_RAIL_REASONS.stage_missing)
  for (const id of ['ground-physical', 'solaredge-import', 'pvcase-tutorial']) {
    changeFlow(id)
    const stages = SOLAR_FLOWS.find((flow) => flow.id === id).stages
    expect(items().map((item) => item.querySelector('.solar-flow-label').textContent)).toEqual(stages.map((stage) => stage.label))
    expect(items().every((item) => item.querySelector('button').disabled)).toBe(true)
    for (const item of items()) {
      const open = item.querySelector('button')
      const description = document.getElementById(open.getAttribute('aria-describedby')).textContent
      expect([PROJECT_SOLAR_RAIL_REASONS.form_unavailable, PROJECT_SOLAR_RAIL_REASONS.stage_missing]).toContain(description)
      fireEvent.click(open)
    }
    expect(screen.getByTestId('solar-flow-maturity').textContent).toBe(id === 'pvcase-tutorial'
      ? SOLAR_FLOW_MATURITY_NOTES.tutorial : SOLAR_FLOW_MATURITY_NOTES.preview)
  }
  expect(h.props.onOpenStep).not.toHaveBeenCalled()
  expect(h.client.readContext).not.toHaveBeenCalled()
})

it('R13D-32 busy arriving during context recheck prevents handoff', async () => {
  for (const name of ['solar-size-strings', 'solar-string-conductors']) {
    const h = await r13dRailHarness()
    const candidate = h.rows.find((entry) => entry.name === name)
    const view = render(<SolarStepEditor {...h.props} row={candidate} busy={false} />)
    if (name === 'solar-string-conductors') {
      await waitFor(() => expect(screen.getByLabelText('Select A1')).toBeTruthy())
      fireEvent.click(screen.getByRole('button', { name: 'Select all' }))
      fireEvent.change(screen.getByLabelText('Conductor'), { target: { value: '10 AWG' } })
    }
    const action = name === 'solar-size-strings' ? 'Review & run' : 'Apply to selected strings'
    await waitFor(() => expect(screen.getByRole('button', { name: action }).disabled).toBe(false))
    let resolveContext
    const contextRead = new Promise((done) => { resolveContext = done })
    h.client.readContext.mockReturnValueOnce(contextRead)
    const reads = h.client.readContext.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: action }))
    expect(h.client.readContext).toHaveBeenCalledTimes(reads + 1)
    view.rerender(<SolarStepEditor {...h.props} row={candidate} busy />)
    await act(async () => { resolveContext({ ok: true, status: 200, data: h.context }) })
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    view.rerender(<SolarStepEditor {...h.props} row={candidate} busy={false} />)
    await waitFor(() => expect(screen.getByRole('button', { name: action }).disabled).toBe(false))
    expect(h.props.onProjectSubmit).not.toHaveBeenCalled()
    const freshIntent = h.client.catalog.mock.calls.at(-1)[0].intents[0]
    fireEvent.click(screen.getByRole('button', { name: action }))
    await waitFor(() => expect(h.props.onProjectSubmit).toHaveBeenCalledExactlyOnceWith(freshIntent, expect.objectContaining({
      binding: projectRailBinding, authorityKey: 'epoch-1',
    })))
    expect(h.props.onSubmit).not.toHaveBeenCalled()
    expect(h.props.readIntake).not.toHaveBeenCalled()
    cleanup()
  }
}, 10000)
