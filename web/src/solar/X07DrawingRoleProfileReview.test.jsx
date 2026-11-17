import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import Review from './X07DrawingRoleProfileReview.jsx'

afterEach(cleanup)
const roles = { module: { block: 'Panel', layer: 'MODULES' } }
const pin = { customer_key: 'customer-a', project_key: 'project-a', profile_id: 'profile-a', version: 1, roles }
const ack = (drawing = 'drawing-a') => ({ ok: true, drawing_key: drawing, applied_profile: structuredClone(pin) })
const row = { block: 'Panel', layer: 'MODULES', count: 3, insertion_bounds: [0, 0, 2, 2] }
const reply = { ok: true, data: { role_discovery: { schema: 'leaf.drawing-role-discovery.v1',
  module: { first_choice: row, candidates: [row], competing: false }, roof: [], obstruction: [] } } }
const client = () => ({ readContext: vi.fn().mockResolvedValue(reply) })
const defaults = () => ({ customerKey: 'customer-a', drawingKey: 'drawing-a', authorityKey: 'authority-a',
  solarBinding: { project_id: 'project-a' }, projectClient: client(), isCurrent: () => true,
  onConfirm: vi.fn().mockResolvedValue(ack()), loadProfile: vi.fn().mockResolvedValue(structuredClone(pin)), saveProfile: vi.fn() })
const submit = async () => fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }

it('refuses missing, malformed and foreign scoped pins without opening or disclosing an editor', async () => {
  const invalid = [null, undefined, [], {}, { ...pin, customer_key: 'PRIVATE_CUSTOMER' },
    { ...pin, project_key: 'PRIVATE_PROJECT' }, { ...pin, profile_id: '' },
    ...[0, -1, 1.5, '1', true, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1].map(version => ({ ...pin, version })),
    { ...pin, drawing_key: 'PRIVATE_DRAWING' }, { ...pin, roles: {} }]
  for (const candidate of invalid) {
    const props = defaults()
    props.onConfirm.mockResolvedValue({ ok: true, drawing_key: 'drawing-a', applied_profile: candidate })
    const view = render(<Review {...props} />)
    await submit()
    expect(await screen.findByRole('alert')).toHaveTextContent('Roles could not be saved. Try again.')
    expect(screen.queryByRole('region', { name: 'Customer role profile' })).toBeNull()
    expect(props.loadProfile).not.toHaveBeenCalled()
    expect(props.saveProfile).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toContain('PRIVATE_')
    props.onConfirm.mockResolvedValue(ack())
    await submit()
    expect(await screen.findByRole('textbox', { name: 'Module layer' })).toHaveValue('MODULES')
    view.unmount()
  }
})

it('requires the drawing result and binding to agree, and accepts exact positive safe boundaries', async () => {
  for (const result of [{ ok: true, applied_profile: pin }, ack('drawing-b'), { ...ack(), drawing_key: '' }]) {
    const props = defaults(); props.onConfirm.mockResolvedValue(result)
    const view = render(<Review {...props} />); await submit()
    await screen.findByRole('alert'); expect(props.loadProfile).not.toHaveBeenCalled(); view.unmount()
  }
  const mismatched = defaults(); mismatched.solarBinding.drawing_id = 'drawing-b'
  const view = render(<Review {...mismatched} />); await submit(); await screen.findByRole('alert')
  expect(mismatched.loadProfile).not.toHaveBeenCalled(); view.unmount()
  for (const version of [1, Number.MAX_SAFE_INTEGER]) {
    const props = defaults(), result = ack(); result.applied_profile.version = version
    props.onConfirm.mockResolvedValue(result); props.loadProfile.mockResolvedValue(result.applied_profile)
    const current = render(<Review {...props} />); await submit()
    expect(await screen.findByText(`Profile version: ${version}`)).toBeTruthy(); current.unmount()
  }
})

it('retires deferred confirmations across drawing A-B-A with an unchanged binding', async () => {
  const props = defaults(), pending = deferred()
  props.onConfirm.mockReturnValueOnce(pending.promise).mockResolvedValue(ack())
  const view = render(<Review {...props} />); await submit()
  const oldSignal = props.onConfirm.mock.calls[0][1].signal
  view.rerender(<Review {...props} drawingKey="drawing-b" />)
  view.rerender(<Review {...props} drawingKey="drawing-a" />)
  expect(oldSignal.aborted).toBe(true)
  await act(async () => pending.resolve(ack()))
  expect(props.loadProfile).not.toHaveBeenCalled(); expect(props.saveProfile).not.toHaveBeenCalled()
  expect(screen.queryByText('Roles confirmed.')).toBeNull()
  await submit(); expect(await screen.findByRole('textbox', { name: 'Module layer' })).toBeTruthy()
  expect(props.onConfirm).toHaveBeenCalledTimes(2)
})

it('retires deferred editor loads and save acknowledgments across drawing A-B-A', async () => {
  const props = defaults(), load = deferred(), save = deferred()
  props.loadProfile.mockReturnValueOnce(load.promise).mockResolvedValue(structuredClone(pin))
  const view = render(<Review {...props} />); await submit()
  await screen.findByText('Reading or saving profile.')
  const loadSignal = props.loadProfile.mock.calls[0][1].signal
  view.rerender(<Review {...props} drawingKey="drawing-b" />); view.rerender(<Review {...props} />)
  await act(async () => load.resolve({ ...pin, roles: { module: { block: 'RETIRED_PROFILE', layer: 'OLD' } } }))
  expect(loadSignal.aborted).toBe(true); expect(document.body.textContent).not.toContain('RETIRED_PROFILE')
  expect(screen.queryByRole('textbox')).toBeNull(); expect(props.saveProfile).not.toHaveBeenCalled()
  await submit(); await screen.findByRole('textbox', { name: 'Module layer' })
  props.saveProfile.mockReturnValueOnce(save.promise)
  fireEvent.change(screen.getByRole('textbox', { name: 'Module layer' }), { target: { value: 'NEXT' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await act(async () => {})
  const saveSignal = props.saveProfile.mock.calls[0][2].signal
  view.rerender(<Review {...props} drawingKey="drawing-b" />); view.rerender(<Review {...props} />)
  await act(async () => save.resolve({ ...pin, version: 2 }))
  expect(saveSignal.aborted).toBe(true); expect(screen.queryByText('Profile saved.')).toBeNull()
  expect(screen.queryByRole('textbox')).toBeNull(); expect(props.saveProfile).toHaveBeenCalledTimes(1)
})

it('snapshots pinned roles instead of following mutable callbacks or later profile versions', async () => {
  const props = defaults(), result = ack()
  props.onConfirm.mockResolvedValue(result)
  props.saveProfile.mockImplementation(async (customer, next) => ({ ...pin, roles: next, version: 2 }))
  render(<Review {...props} />); await submit(); await screen.findByRole('textbox', { name: 'Module layer' })
  result.applied_profile.roles.module.layer = 'MUTATED_RECEIPT'
  fireEvent.change(screen.getByRole('textbox', { name: 'Module layer' }), { target: { value: 'UPDATED' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Profile version: 2')
  const summary = within(screen.getByRole('region', { name: 'Pinned drawing roles' }))
  expect(summary.getByText('Panel on MODULES')).toBeTruthy()
  expect(summary.getByText('Pinned profile version: 1')).toBeTruthy()
  expect(screen.getByText('Project is pinned to version 1.')).toBeTruthy()
  expect(screen.getByRole('region', { name: 'Pinned drawing roles' })).not.toHaveTextContent('UPDATED')
  expect(screen.getByRole('region', { name: 'Pinned drawing roles' })).not.toHaveTextContent('MUTATED_RECEIPT')
})
