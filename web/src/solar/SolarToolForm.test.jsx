import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarToolForm from './SolarToolForm.jsx'
import declaration from '../../../server/solar_tools/solar_design_presets.json'
import listDeclaration from '../../../server/solar_tools/solar_design_presets_list.json'
import conversionDeclaration from '../../../server/solar_tools/solar_trackers_to_panel_groups.json'
import snapshot from '../../../docs/parity/evidence/ground/generate/profile-settings.json'

afterEach(cleanup)

const presetTool = {
  name: 'solar-design-presets', label: 'Design presets', capabilities: ['drawing.write'],
  solar: { schema: 'leaf.solar-tool-view.v1', name: 'solar-design-presets', family: 'settings',
    wave: 2, order: 12, entitlement: 'run_write', interaction: { mode: 'form' } },
  params: declaration.record.params,
}
const SNAP = Object.fromEntries(Object.keys(declaration.record.params.properties.current_settings.properties).map((key) => [key, snapshot[key]]))
const row = (n, name, value) => ({ id: { entity_id: n }, type: 'report', quantity: 1, unit: 'each', name, value })
const LIST_TWO = { schema: 'leaf.solar-design-presets.v1', active_prefix: 'B', current_settings: { ...SNAP }, rows: [],
  list_rows: [row('report-profile-1', 'profile-1', 'A Alpha'), row('report-profile-2', 'profile-2', 'B Beta'), row('report-profiles', 'profiles', 2)] }


const tool = {
  name: 'solar-extra', label: 'Solar inputs', capabilities: ['drawing.write'],
  solar: {
    schema: 'leaf.solar-tool-view.v1', name: 'solar-extra', family: 'stringing',
    wave: 1, order: 10, entitlement: 'run_write', interaction: { mode: 'form' },
  },
  params: { type: 'object', properties: {
    drawing_id: { type: 'string', default: 'must-not-submit' },
    expected_rev: { type: 'integer', default: 1 },
    cancel: { type: 'boolean', default: false },
    changes: { type: 'object', default: {} },
    initialize: { type: ['object', 'null'], default: null },
  } },
}

it('SolarToolForm renders only the visible keys', () => {
  const { container } = render(<SolarToolForm tool={tool} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByRole('region', { name: 'Solar inputs parameters' }).id).toBe('solar-tool-form')
  expect(screen.getByRole('heading', { name: 'Solar inputs' })).toBeTruthy()
  for (const label of ['Expected rev', 'Cancel', 'Changes', 'Initialize']) {
    expect(screen.getByLabelText(label)).toBeTruthy()
  }
  expect(container.querySelectorAll('input')).toHaveLength(4)
  expect(screen.queryByLabelText('Drawing id')).toBeNull()
  expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(false)
})

it('SolarToolForm submits defaults merged with edits once and closes', () => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  render(<SolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose} />)
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onSubmit).toHaveBeenCalledTimes(1)
  expect(onSubmit).toHaveBeenCalledWith(tool, { expected_rev: 3, cancel: false, changes: {}, initialize: null })
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit.mock.invocationCallOrder[0]).toBeLessThan(onClose.mock.invocationCallOrder[0])
})

it('SolarToolForm Escape and Cancel close without a run', () => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  const onParentKeyDown = vi.fn()
  render(<div onKeyDown={onParentKeyDown}><SolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose} /></div>)
  fireEvent.keyDown(screen.getByLabelText('Expected rev'), { key: 'Escape' })
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
  expect(onParentKeyDown).not.toHaveBeenCalled()
  onClose.mockClear()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
})

it('ST4 routes design presets to the typed form', () => {
  const { container } = render(<SolarToolForm tool={presetTool} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByTestId('solar-preset-form')).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Design presets parameters' }).id).toBe('solar-tool-form')
  expect([...container.querySelectorAll('input')].some((input) => input.value === '{}')).toBe(false)
})

it('ST5 forwards the optional listing and revision', () => {
  render(<SolarToolForm tool={presetTool} presetListing={LIST_TWO} presetRevision={2} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.getByText('B Beta (active)')).toBeTruthy()
  expect(screen.getByLabelText('Graph revision').value).toBe('2')
})

it('ST6 keeps the List tool on the generic form', () => {
  const listTool = { ...presetTool, name: 'solar-design-presets-list', capabilities: ['drawing.read'],
    params: listDeclaration.record.params,
    solar: { ...presetTool.solar, name: 'solar-design-presets-list', entitlement: 'run_read', order: 14 } }
  render(<SolarToolForm tool={listTool} onSubmit={vi.fn()} onClose={vi.fn()} />)
  expect(screen.queryByTestId('solar-preset-form')).toBeNull()
  expect(screen.getByRole('button', { name: 'Review & run' })).toBeTruthy()
})

// sf-w3-conversion-graph-surface: the ribbon's generic form starts expected_rev at the drawing's own graph
// revision, read through the same intake loader and rule the Solar step editor uses (solarFlowPrefill).
const conversionTool = {
  name: conversionDeclaration.name, capabilities: conversionDeclaration.record.capabilities,
  params: conversionDeclaration.record.params,
  solar: { schema: 'leaf.solar-tool-view.v1', name: conversionDeclaration.name, family: conversionDeclaration.family,
    wave: conversionDeclaration.wave, order: conversionDeclaration.order, entitlement: conversionDeclaration.entitlement,
    interaction: conversionDeclaration.interaction },
}
const intakeAt = (version, rev) => ({ version, intake: { solar_design_graph: { rev } } })
const deferred = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

it('TF1 the conversion form starts at the drawing graph revision and submits it', async () => {
  const readIntake = vi.fn(async () => intakeAt(3, 7))
  const onSubmit = vi.fn()
  render(<SolarToolForm tool={conversionTool} readIntake={readIntake} drawingId="d1" drawingVersion={3}
    onSubmit={onSubmit} onClose={vi.fn()} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
  expect(readIntake).toHaveBeenCalledTimes(1)
  expect(readIntake).toHaveBeenCalledWith('d1', 3)
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 7 })
})

it('TF2 a revision the drafter typed is never overwritten by a late intake', async () => {
  const pending = deferred()
  const onSubmit = vi.fn()
  render(<SolarToolForm tool={conversionTool} readIntake={() => pending.promise} drawingId="d1" drawingVersion={3}
    onSubmit={onSubmit} onClose={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('5')
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 5 })
})

it('TF3 an intake of another version, a malformed intake or a failed read prefills nothing', async () => {
  const answers = [
    () => intakeAt(2, 7),
    () => null,
    () => [intakeAt(3, 7)],
    () => Object.assign(Object.create({ version: 3 }), { intake: { solar_design_graph: { rev: 7 } } }),
    () => intakeAt(3, -1),
    () => intakeAt(3, 1.5),
    () => intakeAt(3, '7'),
    () => intakeAt(3, 2 ** 53),
    () => ({ version: 3, intake: null }),
    () => Promise.reject(new Error('offline')),
    () => { throw new Error('sync failure') },
  ]
  for (const [index, answer] of answers.entries()) {
    const readIntake = vi.fn(answer)
    const onSubmit = vi.fn()
    const view = render(<SolarToolForm tool={conversionTool} readIntake={readIntake} drawingId="d1" drawingVersion={3}
      onSubmit={onSubmit} onClose={vi.fn()} />)
    await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(1))
    await act(async () => { await new Promise((done) => setTimeout(done, 0)) })
    // SchemaForm draws an absent integer as 0 but submits nothing for it (the base behaviour).
    expect(screen.getByLabelText('Expected rev').value, `answer ${index}`).toBe('0')
    fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
    expect(onSubmit, `answer ${index}`).toHaveBeenCalledWith(conversionTool, {})
    view.unmount()
  }
})

it('TF4 a form without an integer expected_rev never reads the intake', async () => {
  const readIntake = vi.fn(async () => intakeAt(3, 7))
  const stringRev = { ...tool, params: { type: 'object', properties: { expected_rev: { type: 'string' } } } }
  const noRev = { ...tool, params: { type: 'object', properties: { label: { type: 'string' } } } }
  // A pick step that binds expected_rev takes it off the form, so the form must not supply one either.
  const pickedRev = { ...conversionTool, solar: { ...conversionTool.solar,
    interaction: { mode: 'form', pick: [{ key: 'expected_rev' }] } } }
  for (const row of [stringRev, noRev, pickedRev]) {
    const view = render(<SolarToolForm tool={row} readIntake={readIntake} drawingId="d1" drawingVersion={3}
      onSubmit={vi.fn()} onClose={vi.fn()} />)
    await act(async () => { await new Promise((done) => setTimeout(done, 0)) })
    view.unmount()
  }
  expect(readIntake).not.toHaveBeenCalled()
})

it('TF5 a new drawing version reads its own revision while the field is untouched', async () => {
  const readIntake = vi.fn(async (id, version) => intakeAt(version, version === 3 ? 7 : 9))
  const props = { tool: conversionTool, readIntake, drawingId: 'd1', onSubmit: vi.fn(), onClose: vi.fn() }
  const view = render(<SolarToolForm {...props} drawingVersion={3} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
  view.rerender(<SolarToolForm {...props} drawingVersion={4} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('9'))
  expect(readIntake.mock.calls).toEqual([['d1', 3], ['d1', 4]])
})

it('TF6 a late answer for the version the form left behind never lands', async () => {
  const late = deferred()
  const readIntake = vi.fn((id, version) => (version === 3 ? late.promise : Promise.resolve(intakeAt(4, 9))))
  const props = { tool: conversionTool, readIntake, drawingId: 'd1', onSubmit: vi.fn(), onClose: vi.fn() }
  const view = render(<SolarToolForm {...props} drawingVersion={3} />)
  view.rerender(<SolarToolForm {...props} drawingVersion={4} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('9'))
  await act(async () => { late.resolve(intakeAt(3, 7)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('9')
})

it('TF7 a new version clears the automatic revision and waits for its own read', async () => {
  const pending = deferred()
  const readIntake = vi.fn((id, version) => version === 3 ? Promise.resolve(intakeAt(3, 7)) : pending.promise)
  const props = { tool: conversionTool, readIntake, drawingId: 'd1', onSubmit: vi.fn(), onClose: vi.fn() }
  const view = render(<SolarToolForm {...props} drawingVersion={3} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
  view.rerender(<SolarToolForm {...props} drawingVersion={4} />)
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run.disabled).toBe(true)
  expect(run.title).toBe("Reading this drawing's design revision.")
  expect(screen.getByLabelText('Expected rev').value).toBe('0')
  fireEvent.click(run)
  expect(props.onSubmit).not.toHaveBeenCalled()
  await act(async () => { pending.resolve(intakeAt(4, 8)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('8')
  expect(run.disabled).toBe(false)
  expect(run.title).toBe('')
  fireEvent.click(run)
  expect(props.onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 8 })
})

it('TF8 a failed or graphless new read leaves the schema default instead of the old automatic revision', async () => {
  for (const answer of [() => Promise.reject(new Error('offline')), () => ({ version: 4, intake: null }),
    () => intakeAt(3, 8)]) {
    const readIntake = vi.fn((id, version) => version === 3 ? intakeAt(3, 7) : answer())
    const props = { tool: conversionTool, readIntake, drawingId: 'd1', onSubmit: vi.fn(), onClose: vi.fn() }
    const view = render(<SolarToolForm {...props} drawingVersion={3} />)
    await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
    view.rerender(<SolarToolForm {...props} drawingVersion={4} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(false))
    expect(screen.getByLabelText('Expected rev').value).toBe('0')
    fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
    expect(props.onSubmit).toHaveBeenCalledWith(conversionTool, {})
    view.unmount()
  }
})

it('TF9 a typed revision survives a drawing and version change while Run waits', async () => {
  const pending = deferred()
  const readIntake = vi.fn((id) => id === 'd1' ? intakeAt(3, 7) : pending.promise)
  const props = { tool: conversionTool, readIntake, onSubmit: vi.fn(), onClose: vi.fn() }
  const view = render(<SolarToolForm {...props} drawingId="d1" drawingVersion={3} />)
  await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
  view.rerender(<SolarToolForm {...props} drawingId="d2" drawingVersion={4} />)
  expect(screen.getByLabelText('Expected rev').value).toBe('5')
  expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(true)
  await act(async () => { pending.resolve(intakeAt(4, 8)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('5')
  expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(props.onSubmit).toHaveBeenCalledWith(conversionTool, { expected_rev: 5 })
})

it('TF10 without a loader there is no pending read and the form keeps its default', () => {
  render(<SolarToolForm tool={tool} drawingId="d1" drawingVersion={3} onSubmit={vi.fn()} onClose={vi.fn()} />)
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run.disabled).toBe(false)
  expect(run.title).toBe('')
  expect(screen.getByLabelText('Expected rev').value).toBe('1')
})

it('TF11 a changed drawing or tool clears the automatic revision to the current schema default', async () => {
  for (const changeTool of [false, true]) {
    const pending = deferred()
    const readIntake = vi.fn().mockResolvedValueOnce(intakeAt(3, 7)).mockImplementation(() => pending.promise)
    const nextTool = { ...conversionTool, params: { ...conversionTool.params,
      properties: { ...conversionTool.params.properties, expected_rev: { type: 'integer', default: 2 } } } }
    const props = { readIntake, drawingVersion: 3, onSubmit: vi.fn(), onClose: vi.fn() }
    const view = render(<SolarToolForm {...props} tool={conversionTool} drawingId="d1" />)
    await waitFor(() => expect(screen.getByLabelText('Expected rev').value).toBe('7'))
    view.rerender(<SolarToolForm {...props} tool={changeTool ? nextTool : conversionTool} drawingId={changeTool ? 'd1' : 'd2'} />)
    expect(screen.getByLabelText('Expected rev').value).toBe(changeTool ? '2' : '0')
    expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(true)
    await act(async () => { pending.resolve(null) })
    expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(false)
    expect(screen.getByLabelText('Expected rev').value).toBe(changeTool ? '2' : '0')
    view.unmount()
  }
})
