import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SolarToolForm from './SolarToolForm.jsx'
import SolarPresetForm from './SolarPresetForm.jsx'
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
  expect(container.querySelectorAll('input')).toHaveLength(3)
  expect(container.querySelectorAll('select')).toHaveLength(1)
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

const focusCases = [tool, presetTool].flatMap((formTool) =>
  ['Cancel', 'Escape'].map((action) => [formTool.name, action, formTool]))

it.each(focusCases)('%s %s returns focus to its visible ribbon button without submitting', (name, action, formTool) => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  render(<>
    <button data-tool={name}>Open tool</button>
    <SolarToolForm tool={formTool} onSubmit={onSubmit} onClose={onClose} />
  </>)
  const cancel = screen.getByRole('button', { name: 'Cancel', exact: true })
  cancel.focus()
  if (action === 'Cancel') fireEvent.click(cancel)
  else fireEvent.keyDown(cancel, { key: 'Escape' })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open tool' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
})

it.each(focusCases)('%s %s returns focus to More panels when its ribbon group is hidden', (name, action, formTool) => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  render(<>
    <div id="drafting-ribbon-panels"><div hidden><button data-tool={name}>Open tool</button></div></div>
    <button aria-controls="drafting-ribbon-panels" aria-expanded="false">More panels</button>
    <SolarToolForm tool={formTool} onSubmit={onSubmit} onClose={onClose} />
  </>)
  const cancel = screen.getByRole('button', { name: 'Cancel', exact: true })
  cancel.focus()
  if (action === 'Cancel') fireEvent.click(cancel)
  else fireEvent.keyDown(cancel, { key: 'Escape' })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'More panels' }))
  expect(onClose).toHaveBeenCalledTimes(1)
  expect(onSubmit).not.toHaveBeenCalled()
})

it.each(focusCases)('%s %s leaves focus alone when no return control exists', (name, action, formTool) => {
  const onSubmit = vi.fn()
  const onClose = vi.fn()
  render(<SolarToolForm tool={formTool} onSubmit={onSubmit} onClose={onClose} />)
  const cancel = screen.getByRole('button', { name: 'Cancel', exact: true })
  cancel.focus()
  expect(() => {
    if (action === 'Cancel') fireEvent.click(cancel)
    else fireEvent.keyDown(cancel, { key: 'Escape' })
  }).not.toThrow()
  expect(document.activeElement).toBe(cancel)
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
    // An absent revision is shown as empty and submitted without a key.
    expect(screen.getByLabelText('Expected rev').value, `answer ${index}`).toBe('')
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
  expect(screen.getByLabelText('Expected rev').value).toBe('')
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
    expect(screen.getByLabelText('Expected rev').value).toBe('')
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
    expect(screen.getByLabelText('Expected rev').value).toBe(changeTool ? '2' : '')
    expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(true)
    await act(async () => { pending.resolve(null) })
    expect(screen.getByRole('button', { name: 'Review & run' }).disabled).toBe(false)
    expect(screen.getByLabelText('Expected rev').value).toBe(changeTool ? '2' : '')
    view.unmount()
  }
})

const presetRevisionField = () => screen.getByLabelText('Graph revision')

it('FORM21 invalid Solar JSON blocks submit and close until corrected', () => {
  const onSubmit = vi.fn(), onClose = vi.fn()
  render(<SolarToolForm tool={tool} onSubmit={onSubmit} onClose={onClose} />)
  fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{' } })
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run.disabled).toBe(true); fireEvent.click(run)
  expect(onSubmit).not.toHaveBeenCalled(); expect(onClose).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{"tilt":20}' } })
  expect(run.disabled).toBe(false); fireEvent.click(run)
  expect(onSubmit).toHaveBeenCalledExactlyOnceWith(tool, { expected_rev: 1, cancel: false, changes: { tilt: 20 }, initialize: null })
  expect(onClose).toHaveBeenCalledTimes(1)
})

it('FORM23a clearing the Solar tool revision counts as a touch before intake', async () => {
  const pending = deferred(), onSubmit = vi.fn()
  const readIntake = vi.fn(() => pending.promise)
  render(<SolarToolForm tool={conversionTool} readIntake={readIntake} drawingId="d1" drawingVersion={3}
    onSubmit={onSubmit} onClose={vi.fn()} />)
  await waitFor(() => expect(readIntake).toHaveBeenCalledTimes(1))
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '5' } })
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '' } })
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('')
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onSubmit).toHaveBeenCalledWith(conversionTool, {})
})
it('FORM29 a change to another Solar field leaves the revision automatic', async () => {
  const pending = deferred(), onSubmit = vi.fn()
  const noteTool = { ...conversionTool, params: { ...conversionTool.params,
    properties: { ...conversionTool.params.properties, note: { type: 'string' } } } }
  render(<SolarToolForm tool={noteTool} readIntake={vi.fn(() => pending.promise)} drawingId="d1" drawingVersion={3}
    onSubmit={onSubmit} onClose={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'east' } })
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('7')
  fireEvent.click(screen.getByRole('button', { name: 'Review & run' }))
  expect(onSubmit).toHaveBeenCalledWith(noteTool, { expected_rev: 7, note: 'east' })
})
const presetRun = () => screen.getByRole('button', { name: 'Review & run' })
const STALE = (label) => `${label} has a value this tool no longer accepts. Choose or enter a new one.`

it('FORM32 a nullable boolean default submits and closes the Solar form', () => {
  const nullableTool = { ...tool, params: { properties: { x: { type: ['boolean', 'null'], default: null } } } }
  const onSubmit = vi.fn(), onClose = vi.fn()
  render(<SolarToolForm tool={nullableTool} onSubmit={onSubmit} onClose={onClose} />)
  const input = screen.getByLabelText('X')
  expect(input.getAttribute('aria-invalid')).toBe('false')
  expect(input.validationMessage).toBe('')
  expect(screen.queryByText(STALE('X'))).toBeNull()
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run.disabled).toBe(false)
  fireEvent.click(run)
  expect(onSubmit).toHaveBeenCalledExactlyOnceWith(nullableTool, { x: null })
  expect(onClose).toHaveBeenCalledTimes(1)
})

it('FORM43 a nullable sibling keeps revision intake automatic after another field edit', async () => {
  const pending = deferred(), onSubmit = vi.fn()
  const noteTool = { ...conversionTool, params: { ...conversionTool.params,
    properties: { ...conversionTool.params.properties, note: { type: 'string' },
      x: { type: ['boolean', 'null'], default: null } } } }
  render(<SolarToolForm tool={noteTool} readIntake={vi.fn(() => pending.promise)} drawingId="d1" drawingVersion={3}
    onSubmit={onSubmit} onClose={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('Note'), { target: { value: 'east' } })
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(screen.getByLabelText('Expected rev').value).toBe('7')
  const input = screen.getByLabelText('X')
  expect(input.getAttribute('aria-invalid')).toBe('false')
  expect(input.validationMessage).toBe('')
  expect(screen.queryByText(STALE('X'))).toBeNull()
  const run = screen.getByRole('button', { name: 'Review & run' })
  expect(run.disabled).toBe(false)
  fireEvent.click(run)
  expect(onSubmit).toHaveBeenCalledExactlyOnceWith(noteTool, { expected_rev: 7, note: 'east', x: null })
})

function openPresetRead(props = {}) {
  const bound = { tool: presetTool, drawingId: 'd1', drawingVersion: 3, onSubmit: vi.fn(), onClose: vi.fn(), ...props }
  return { ...render(<SolarToolForm {...bound} />), props: bound }
}
const presetName = () => fireEvent.change(screen.getByLabelText('Preset name'), { target: { value: 'Alpha' } })

it('G1C-1 reads the bound drawing revision and submits it', async () => {
  const pending = deferred()
  const readIntake = vi.fn(() => pending.promise)
  const { props } = openPresetRead({ readIntake })
  presetName()
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(readIntake).toHaveBeenCalledTimes(1)
  expect(readIntake).toHaveBeenCalledWith('d1', 3)
  expect(presetRevisionField().value).toBe('7')
  fireEvent.click(presetRun())
  expect(props.onSubmit).toHaveBeenCalledWith(presetTool, { expected_rev: 7, subcommand: 'Create', name: 'Alpha' })
})

it('G1C-2 prefills revision zero', async () => {
  const pending = deferred()
  openPresetRead({ readIntake: () => pending.promise })
  await act(async () => { pending.resolve(intakeAt(3, 0)) })
  expect(presetRevisionField().value).toBe('0')
})

it('G1C-3 without a reader keeps an empty revision and no reading lock', () => {
  openPresetRead()
  expect(presetRevisionField().value).toBe('')
  expect(presetRun().title).toBe('')
  presetName()
  fireEvent.change(presetRevisionField(), { target: { value: '0' } })
  expect(presetRun().disabled).toBe(false)
})

it('G1C-4 invalid or failed intakes leave an empty revision and settle the read', async () => {
  const answers = [() => intakeAt(2, 7), () => ({ version: 3, intake: {} }), () => null,
    () => Promise.reject(new Error('offline')), () => { throw new Error('sync') },
    () => [intakeAt(3, 7)], () => Object.assign(Object.create({ version: 3 }), { intake: { solar_design_graph: { rev: 7 } } })]
  for (const answer of answers) {
    const readIntake = vi.fn(answer)
    const view = openPresetRead({ readIntake })
    await act(async () => {})
    expect(readIntake).toHaveBeenCalledTimes(1)
    expect(presetRevisionField().value).toBe('')
    expect(presetRun().title).toBe('')
    presetName()
    fireEvent.change(presetRevisionField(), { target: { value: '0' } })
    expect(presetRun().disabled).toBe(false)
    view.unmount()
  }
})

it('G1C-5 every revision change event protects the draft from a late read', async () => {
  for (const value of ['5', '', '7']) {
    const pending = deferred()
    const first = deferred()
    const readIntake = vi.fn((id, version) => value === '7' && version === 3 ? first.promise : pending.promise)
    const view = openPresetRead({ readIntake })
    // Dispatch even when the value shown is unchanged: any change event is a touch.
    if (value === '7') {
      await act(async () => { first.resolve(intakeAt(3, 7)) })
      // React tracks DOM values; reset its event tracker to dispatch the same-value change.
      presetRevisionField()._valueTracker.setValue('')
      fireEvent.change(presetRevisionField(), { target: { value: '7' } })
      view.rerender(<SolarToolForm {...view.props} drawingVersion={4} />)
    } else if (value === '') {
      fireEvent.change(presetRevisionField(), { target: { value: '5' } })
      fireEvent.change(presetRevisionField(), { target: { value } })
    } else fireEvent.change(presetRevisionField(), { target: { value } })
    await act(async () => { pending.resolve(intakeAt(value === '7' ? 4 : 3, value === '7' ? 9 : 7)) })
    expect(presetRevisionField().value).toBe(value)
    view.unmount()
  }
})

it('G1C-6 a version move ignores the old read', async () => {
  const old = deferred(), next = deferred()
  const readIntake = vi.fn((id, version) => version === 3 ? old.promise : next.promise)
  const view = openPresetRead({ readIntake })
  await act(async () => {})
  view.rerender(<SolarToolForm {...view.props} drawingVersion={4} />)
  await act(async () => { old.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('')
  expect(presetRun().title).toBe("Reading this drawing's design revision.")
  await act(async () => { next.resolve(intakeAt(4, 9)) })
  expect(presetRevisionField().value).toBe('9')
  expect(readIntake.mock.calls).toEqual([['d1', 3], ['d1', 4]])
})

it('G1C-7 a drawing move ignores the old drawing at the same version', async () => {
  const old = deferred(), next = deferred()
  const readIntake = vi.fn((id) => id === 'a' ? old.promise : next.promise)
  const view = openPresetRead({ readIntake, drawingId: 'a' })
  await act(async () => {})
  view.rerender(<SolarToolForm {...view.props} drawingId="b" />)
  await act(async () => { old.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('')
  await act(async () => { next.resolve(intakeAt(3, 9)) })
  expect(presetRevisionField().value).toBe('9')
  expect(readIntake.mock.calls).toEqual([['a', 3], ['b', 3]])
})

it('G1C-8 a new version withdraws an automatic revision even when its read fails', async () => {
  const first = deferred(), next = deferred()
  const readIntake = vi.fn((id, version) => version === 3 ? first.promise : next.promise)
  const view = openPresetRead({ readIntake })
  await act(async () => { first.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('7')
  view.rerender(<SolarToolForm {...view.props} drawingVersion={4} />)
  expect(presetRevisionField().value).toBe('')
  expect(presetRun().title).toBe("Reading this drawing's design revision.")
  await act(async () => { next.resolve(Promise.reject(new Error('offline'))) })
  expect(presetRevisionField().value).toBe('')
  expect(presetRun().title).toBe('')
})

it('G1C-9 a typed revision survives a drawing and version move', async () => {
  const first = deferred(), next = deferred()
  const readIntake = vi.fn((id) => id === 'd1' ? first.promise : next.promise)
  const view = openPresetRead({ readIntake })
  await act(async () => { first.resolve(intakeAt(3, 7)) })
  fireEvent.change(presetRevisionField(), { target: { value: '5' } })
  view.rerender(<SolarToolForm {...view.props} drawingId="d2" drawingVersion={4} />)
  expect(presetRevisionField().value).toBe('5')
  await act(async () => { next.resolve(intakeAt(4, 9)) })
  expect(presetRevisionField().value).toBe('5')
})

it('G1C-10 refuses invalid revisions and preserves the declared maximum', async () => {
  for (const rev of [-1, 1.5, '7', null, Number.MAX_SAFE_INTEGER + 1, 2147483648, 2147483647]) {
    const pending = deferred()
    const view = openPresetRead({ readIntake: () => pending.promise })
    presetName()
    await act(async () => { pending.resolve(intakeAt(3, rev)) })
    expect(presetRevisionField().value).toBe(rev === 2147483647 ? '2147483647' : '')
    expect(presetRun().title).toBe('')
    if (rev === 2147483647) {
      fireEvent.click(presetRun())
      expect(view.props.onSubmit.mock.calls[0][1].expected_rev).toBe(2147483647)
    }
    view.unmount()
  }
})

it('G1C-11 pending reads block button and keyboard submission without marking fields attempted', async () => {
  const pending = deferred()
  const view = openPresetRead({ readIntake: () => pending.promise })
  presetName()
  fireEvent.change(presetRevisionField(), { target: { value: '5' } })
  expect(presetRun().disabled).toBe(true)
  fireEvent.submit(view.container.querySelector('form'))
  expect(view.props.onSubmit).not.toHaveBeenCalled()
  expect(view.props.onClose).not.toHaveBeenCalled()
  fireEvent.click(screen.getByLabelText('Supply the settings'))
  const mppt = screen.getByLabelText('MPPT count')
  fireEvent.change(mppt, { target: { value: '2.5' } })
  expect(presetRun().disabled).toBe(true)
  expect(presetRun().title).toBe("Reading this drawing's design revision.")
  fireEvent.submit(view.container.querySelector('form'))
  expect(mppt.hasAttribute('aria-invalid')).toBe(false)
  expect(view.props.onSubmit).not.toHaveBeenCalled()
  expect(view.props.onClose).not.toHaveBeenCalled()
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
})

it('G1C-12 an explicit preset revision wins over the read', async () => {
  const pending = deferred()
  openPresetRead({ presetRevision: 4, readIntake: () => pending.promise })
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('4')
})

it('G1C-15 List and generic tools keep the generic form without automatic preset props', () => {
  const listTool = { ...presetTool, name: 'solar-design-presets-list', capabilities: ['drawing.read'],
    params: listDeclaration.record.params,
    solar: { ...presetTool.solar, name: 'solar-design-presets-list', entitlement: 'run_read', order: 14 } }
  for (const generic of [listTool, tool, conversionTool]) {
    const view = render(<SolarToolForm tool={generic} presetRevision={4} onSubmit={vi.fn()} onClose={vi.fn()} />)
    expect(screen.queryByTestId('solar-preset-form')).toBeNull()
    expect(screen.queryByLabelText('Graph revision')).toBeNull()
    expect(view.container.querySelector('form')).toBeNull()
    expect(presetRun().title).toBe('')
    view.unmount()
  }
})

it('G1C-16 the preset form refuses an automatic revision below its declared minimum', () => {
  const params = structuredClone(presetTool.params)
  params.properties.expected_rev.minimum = 5
  render(<SolarPresetForm tool={{ ...presetTool, params }} onSubmit={vi.fn()} onClose={vi.fn()}
    autoRevision={{ key: 'k', value: 3 }} />)
  expect(presetRevisionField().value).toBe('')
})

it('G1C-17 the preset form directly refuses a fractional automatic revision', () => {
  render(<SolarPresetForm tool={presetTool} onSubmit={vi.fn()} onClose={vi.fn()}
    autoRevision={{ key: 'k', value: 1.5 }} />)
  expect(presetRevisionField().value).toBe('')
})

it('G1C-18 a changed automatic key clears the old revision even without an intervening null', () => {
  const props = { tool: presetTool, onSubmit: vi.fn(), onClose: vi.fn() }
  const view = render(<SolarPresetForm {...props} autoRevision={{ key: 'a', value: 7 }} />)
  expect(presetRevisionField().value).toBe('7')
  view.rerender(<SolarPresetForm {...props} autoRevision={{ key: 'b', value: 2147483648 }} />)
  expect(presetRevisionField().value).toBe('')
})

it('G1C-19 a keyboard submit during reading does not lock a later valid submit', async () => {
  const pending = deferred()
  const view = openPresetRead({ readIntake: () => pending.promise })
  fireEvent.submit(view.container.querySelector('form'))
  expect(view.props.onSubmit).not.toHaveBeenCalled()
  expect(view.props.onClose).not.toHaveBeenCalled()
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('7')
  presetName()
  expect(presetRun().disabled).toBe(false)
  fireEvent.submit(view.container.querySelector('form'))
  expect(view.props.onSubmit).toHaveBeenCalledTimes(1)
  expect(view.props.onSubmit).toHaveBeenCalledWith(presetTool, { expected_rev: 7, subcommand: 'Create', name: 'Alpha' })
  expect(view.props.onClose).toHaveBeenCalledTimes(1)
})

it('G1C-23 a valid keyboard submit during reading does not lock a later submit', async () => {
  const pending = deferred()
  const view = openPresetRead({ readIntake: () => pending.promise })
  presetName()
  fireEvent.change(presetRevisionField(), { target: { value: '5' } })
  fireEvent.submit(view.container.querySelector('form'))
  expect(view.props.onSubmit).not.toHaveBeenCalled()
  expect(view.props.onClose).not.toHaveBeenCalled()
  await act(async () => { pending.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('5')
  expect(presetRun().disabled).toBe(false)
  fireEvent.submit(view.container.querySelector('form'))
  expect(view.props.onSubmit).toHaveBeenCalledTimes(1)
  expect(view.props.onSubmit).toHaveBeenCalledWith(presetTool, { expected_rev: 5, subcommand: 'Create', name: 'Alpha' })
  expect(view.props.onClose).toHaveBeenCalledTimes(1)
})

it('G1C-21 an old read resolving after the new read cannot replace its revision or reading state', async () => {
  const oldRead = deferred()
  const newRead = deferred()
  const readIntake = vi.fn((id, version) => version === 3 ? oldRead.promise : newRead.promise)
  const view = openPresetRead({ readIntake })
  await act(async () => {})
  view.rerender(<SolarToolForm {...view.props} drawingVersion={4} />)
  await act(async () => {})
  expect(readIntake.mock.calls).toEqual([['d1', 3], ['d1', 4]])
  expect(presetRun().title).toBe("Reading this drawing's design revision.")
  await act(async () => { newRead.resolve(intakeAt(4, 9)) })
  expect(presetRevisionField().value).toBe('9')
  presetName()
  expect(presetRun().disabled).toBe(false)
  await act(async () => { oldRead.resolve(intakeAt(3, 7)) })
  expect(presetRevisionField().value).toBe('9')
  expect(presetRun().title).toBe('')
  expect(presetRun().disabled).toBe(false)
})

it('G1C-22 a generic tool without a revision never reads and its form ignores automatic preset props', async () => {
  const generic = { ...tool, params: { type: 'object', properties: { label: { type: 'string', default: 'ordinary' } } } }
  const props = { tool: generic, onSubmit: vi.fn(), onClose: vi.fn() }
  const readIntake = vi.fn(async () => intakeAt(3, 7))
  const publicView = render(<SolarToolForm {...props} readIntake={readIntake} drawingId="d1" drawingVersion={3} />)
  await act(async () => {})
  expect(readIntake).not.toHaveBeenCalled()
  publicView.unmount()
  // The router returns the private generic component; obtain its type without changing production exports.
  const GenericSolarToolForm = SolarToolForm(props).type
  const first = render(<GenericSolarToolForm {...props} />)
  const html = first.container.innerHTML
  first.unmount()
  const second = render(<GenericSolarToolForm {...props} autoRevision={{ key: 'wrong', value: 99 }} />)
  expect(second.container.innerHTML).toBe(html)
  expect([...second.container.querySelectorAll('input, select, textarea')].some((field) => field.value === '99')).toBe(false)
})
