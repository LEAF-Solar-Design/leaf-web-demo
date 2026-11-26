import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { useLayoutEffect } from 'react'
import X07CustomerProfileEditor from './X07CustomerProfileEditor.jsx'
afterEach(cleanup)
const record = (customer, version = 1) => ({ customer_key: customer, profile_id: 'profile-' + customer,
  version, roles: { module: { block: 'Panel', layer: 'MODULES' } } })
it('does not mount a retry before an actual load refusal', async () => {
  let initialRetry, refuse
  const load = vi.fn(() => new Promise((_resolve, reject) => { refuse = reject }))
  function MountProbe() {
    useLayoutEffect(() => { initialRetry = screen.queryByRole('button', { name: 'Retry loading profile' }) }, [])
    return <X07CustomerProfileEditor customerKey="a" drawingKey="a:v1" loadProfile={load} />
  }
  render(<MountProbe />)
  expect(initialRetry).toBeNull()
  expect(screen.queryByRole('button', { name: 'Retry loading profile' })).toBeNull()
  await waitFor(() => expect(load).toHaveBeenCalledTimes(1))
  await act(async () => refuse(new Error('PRIVATE_LOAD')))
  await screen.findByRole('alert')
  const retry = screen.getByRole('button', { name: 'Retry loading profile' })
  expect(retry.isConnected).toBe(true)
  expect(document.activeElement).toBe(retry)
  expect(load).toHaveBeenCalledTimes(1)
  expect(document.body.textContent).not.toContain('PRIVATE_LOAD')
})
it('keeps edits on a stale save and displays the older project pin', async () => {
  const load = vi.fn().mockResolvedValueOnce(record('a')).mockResolvedValueOnce(record('a', 2))
  const save = vi.fn().mockRejectedValueOnce({ code: 'STALE_VERSION' })
    .mockImplementationOnce(async (_customer, roles) => ({ ...record('a', 3), roles }))
  render(<X07CustomerProfileEditor customerKey="a" loadProfile={load} saveProfile={save} applicationRecord={record('a')} />)
  fireEvent.change(await screen.findByLabelText('Module layer'), { target: { value: 'EDITED' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByRole('alert')
  expect(screen.getByLabelText('Module layer')).toHaveValue('EDITED')
  fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))
  await screen.findByText('Profile version: 2')
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Profile saved.')
  expect(save.mock.calls.map(call => call[2].expected_version)).toEqual([1, 2])
  expect(screen.getByText('Project is pinned to version 1.')).toBeTruthy()
})
it('clears the previous customer and never displays its pinned record', async () => {
  const view = render(<X07CustomerProfileEditor customerKey="a" loadProfile={async () => record('a')} />)
  await screen.findByText('Profile id: profile-a')
  view.rerender(<X07CustomerProfileEditor customerKey="b" loadProfile={async () => record('b')} applicationRecord={record('a')} />)
  await screen.findByText('Profile id: profile-b')
  expect(screen.queryByText('Profile id: profile-a')).toBeNull()
  expect(screen.queryByText(/Project is pinned/)).toBeNull()
})

it.each(['roof', 'obstruction'])('reviews a peer deletion of %s while keeping the other draft', async role => {
  const loaded = { ...record('a'), roles: { ...record('a').roles, [role]: { layer: 'ORIGINAL' } } }
  let current = loaded
  const load = vi.fn(async () => current)
  const save = vi.fn()
    .mockRejectedValueOnce({ code: 'STALE_VERSION' })
    .mockImplementationOnce(async (_customer, roles) => ({ ...current, version: 4, roles }))
  render(<X07CustomerProfileEditor customerKey="a" loadProfile={load} saveProfile={save}
    applicationRecord={loaded} pinnedRoles={loaded.roles} />)

  fireEvent.change(await screen.findByLabelText('Module layer'), { target: { value: 'KEEP-DRAFT' } })
  const roleInput = screen.getByLabelText(new RegExp(`^${role} layer$`, 'i'))
  fireEvent.change(roleInput, { target: { value: 'LOCAL-DRAFT' } })
  current = { ...loaded, version: 2, roles: { ...loaded.roles, [role]: { layer: 'PEER-EDIT' } } }
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByRole('alert')

  current = { ...record('a', 3), roles: { ...record('a').roles } }
  fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))
  await screen.findByText('Profile version: 3')
  const pinDiff = within(screen.getByRole('region', { name: 'Pinned versus current roles' }))
  const deletion = within(pinDiff.getByRole('region', { name: `Removed ${role}` }))
  expect(deletion.getByText('"ORIGINAL"')).toBeTruthy()
  expect(roleInput).toHaveValue('LOCAL-DRAFT')
  expect(screen.getByLabelText('Module layer')).toHaveValue('KEEP-DRAFT')
  expect(save).toHaveBeenCalledTimes(1)

  fireEvent.click(screen.getByRole('button', { name: `Restore loaded ${role} value` }))
  expect(roleInput.isConnected).toBe(false)
  expect(screen.getByLabelText('Module layer')).toHaveValue('KEEP-DRAFT')
  expect(save).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Profile version: 4')
  expect(save.mock.calls[1][1]).toEqual({ module: { block: 'Panel', layer: 'KEEP-DRAFT' } })
  expect(save.mock.calls.map(call => call[2].expected_version)).toEqual([1, 3])
  expect(screen.getByText('Project is pinned to version 1.')).toBeTruthy()
})

it.each([
  { name: 'a restored roof and edited obstruction',
    peer: { roof: { layer: 'RESTORED' }, obstruction: { layer: 'NEW-OBSTRUCTION' } } },
  { name: 'a deleted roof and edited obstruction',
    peer: { obstruction: { layer: 'NEW-OBSTRUCTION' } } },
  { name: 'another field on the edited module role',
    peer: { module: { block: 'Peer block', layer: 'MODULES' }, roof: { layer: 'RESTORED' } } },
])('keeps the local module edit alongside $name', async ({ peer }) => {
  const initial = { ...record('a'), roles: { ...record('a').roles,
    roof: { layer: 'ORIGINAL-ROOF' }, obstruction: { layer: 'ORIGINAL-OBSTRUCTION' } } }
  const deleted = { ...initial, version: 2, roles: { module: initial.roles.module, obstruction: initial.roles.obstruction } }
  const latest = { ...initial, version: 4, roles: { module: initial.roles.module, ...peer } }
  const other = { ...record('b'), roles: { ...record('b').roles, roof: { layer: 'OTHER' } } }
  const load = vi.fn().mockResolvedValueOnce(initial).mockResolvedValueOnce(deleted)
    .mockResolvedValueOnce(latest).mockResolvedValueOnce(other)
  const save = vi.fn().mockRejectedValueOnce({ code: 'STALE_VERSION' })
    .mockRejectedValueOnce({ code: 'STALE_VERSION' })
    .mockImplementationOnce(async (_customer, roles) => ({ ...latest, version: 5, roles }))
  const view = render(<X07CustomerProfileEditor customerKey="a" drawingKey="a:v1" loadProfile={load}
    saveProfile={save} applicationRecord={initial} pinnedRoles={initial.roles} />)
  fireEvent.change(await screen.findByLabelText('Module layer'), { target: { value: 'MY-DRAFT' } })
  const saveDraft = () => fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  const reload = () => fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))

  saveDraft()
  await screen.findByRole('alert')
  reload()
  await screen.findByText('Profile version: 2')
  const pinDiff = within(screen.getByRole('region', { name: 'Pinned versus current roles' }))
  expect(pinDiff.getByRole('region', { name: 'Removed roof' })).toBeTruthy()
  expect(screen.queryByLabelText('Roof layer')).toBeNull()
  expect(screen.getByLabelText('Module layer')).toHaveValue('MY-DRAFT')

  saveDraft()
  await screen.findByRole('alert')
  reload()
  await screen.findByText('Profile version: 4')
  expect(screen.getByLabelText('Module layer')).toHaveValue('MY-DRAFT')
  expect(screen.getByLabelText('Module block')).toHaveValue(latest.roles.module.block)
  for (const role of ['roof', 'obstruction']) {
    const input = screen.queryByLabelText(new RegExp(`^${role} layer$`, 'i'))
    if (latest.roles[role]) expect(input).toHaveValue(latest.roles[role].layer)
    else expect(input).toBeNull()
  }
  expect(save).toHaveBeenCalledTimes(2)

  saveDraft()
  await screen.findByText('Profile version: 5')
  expect(save.mock.calls.map(([customer, , options]) => [customer, options.expected_version]))
    .toEqual([['a', 1], ['a', 2], ['a', 4]])
  expect(save.mock.calls[2][1]).toEqual({ ...latest.roles, module: { ...latest.roles.module, layer: 'MY-DRAFT' } })
  expect(screen.getByText('Project is pinned to version 1.')).toBeTruthy()

  view.rerender(<X07CustomerProfileEditor customerKey="b" drawingKey="b:v8" loadProfile={load}
    saveProfile={save} applicationRecord={other} pinnedRoles={other.roles} />)
  await screen.findByText('Profile id: profile-b')
  expect(screen.getByLabelText('Roof layer')).toHaveValue('OTHER')
  expect(screen.getByLabelText('Module layer')).toHaveValue('MODULES')
  expect(save).toHaveBeenCalledTimes(3)
})

it('restores changed, removed and draft-only roles independently without an implicit save', async () => {
  const loaded = { ...record('a'), roles: { ...record('a').roles, roof: { layer: 'ROOF' } } }
  const save = vi.fn().mockImplementation(async (_customer, roles) => ({ ...loaded, version: 2, roles }))
  render(<X07CustomerProfileEditor customerKey="a" drawingKey="a:v1" loadProfile={async () => loaded} saveProfile={save} />)
  fireEvent.change(await screen.findByLabelText('Module layer'), { target: { value: 'EDITED' } })
  fireEvent.click(screen.getByRole('button', { name: 'Remove roof role' }))
  fireEvent.click(screen.getByRole('button', { name: 'Add obstruction role' }))
  fireEvent.change(screen.getByLabelText('Obstruction layer'), { target: { value: 'NEW' } })
  fireEvent.click(screen.getByRole('button', { name: 'Restore loaded roof value' }))
  expect(screen.getByLabelText('Roof layer')).toHaveValue('ROOF')
  expect(document.activeElement).toBe(screen.getByLabelText('Roof layer'))
  expect(screen.getByLabelText('Module layer')).toHaveValue('EDITED')
  expect(screen.getByLabelText('Obstruction layer')).toHaveValue('NEW')
  fireEvent.click(screen.getByRole('button', { name: 'Restore loaded obstruction value' }))
  expect(screen.queryByLabelText('Obstruction layer')).toBeNull()
  expect(document.activeElement).toBe(screen.getByLabelText('Module layer'))
  expect(save).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Profile version: 2')
  expect(save.mock.calls[0][1]).toEqual({ module: { block: 'Panel', layer: 'EDITED' }, roof: { layer: 'ROOF' } })
})

it('keeps a selective restore made during a pending save after its earlier acknowledgment', async () => {
  let finish
  const save = vi.fn((_customer, roles) => new Promise(resolve => { finish = () => resolve({ ...record('a', 2), roles }) }))
  render(<X07CustomerProfileEditor customerKey="a" drawingKey="a:v1" loadProfile={async () => record('a')} saveProfile={save} />)
  fireEvent.change(await screen.findByLabelText('Module layer'), { target: { value: 'SUBMITTED' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await act(async () => {})
  fireEvent.click(screen.getByRole('button', { name: 'Restore loaded module value' }))
  expect(screen.getByLabelText('Module layer')).toHaveValue('MODULES')
  expect(document.activeElement).toBe(screen.getByLabelText('Module layer'))
  await act(async () => finish())
  expect(await screen.findByText('Profile saved. Newer edits are unsaved.')).toBeTruthy()
  expect(screen.getByLabelText('Module layer')).toHaveValue('MODULES')
  expect(screen.getByRole('region', { name: 'Draft changes from loaded version 2' })).toHaveTextContent('1 changed.')
  expect(save).toHaveBeenCalledTimes(1)
})

it('undoes only the last restore with exact removed and draft-only roles and no writes', async () => {
  const save = vi.fn(), loaded = { ...record('a'), roles: { ...record('a').roles, roof: { layer: 'R\u00f3F' } } }
  render(<X07CustomerProfileEditor customerKey="a" drawingKey="a:v1" loadProfile={async () => loaded} saveProfile={save} />)
  fireEvent.change(await screen.findByLabelText('Module layer'), { target: { value: 'Mo\u0301D' } })
  fireEvent.click(screen.getByRole('button', { name: 'Remove roof role' }))
  fireEvent.click(screen.getByRole('button', { name: 'Add obstruction role' }))
  fireEvent.change(screen.getByLabelText('Obstruction layer'), { target: { value: 'DRAFT\u00e9' } })
  fireEvent.click(screen.getByRole('button', { name: 'Restore loaded roof value' }))
  fireEvent.click(screen.getByRole('button', { name: 'Restore loaded obstruction value' }))
  expect(screen.getAllByRole('button', { name: 'Undo last restore' })).toHaveLength(1)
  fireEvent.click(screen.getByRole('button', { name: 'Undo last restore' }))
  expect(screen.getByLabelText('Obstruction layer')).toHaveValue('DRAFT\u00e9')
  expect(document.activeElement).toBe(screen.getByLabelText('Obstruction layer'))
  expect(screen.getByLabelText('Roof layer')).toHaveValue('R\u00f3F')
  expect(screen.getByLabelText('Module layer')).toHaveValue('Mo\u0301D')
  expect(screen.queryByRole('button', { name: 'Undo last restore' })).toBeNull()
  expect(save).not.toHaveBeenCalled()
})

it('expires undo on edits, reset and explicit version reload', async () => {
  const save = vi.fn().mockRejectedValue({ code: 'STALE_VERSION' }), load = vi.fn().mockResolvedValueOnce(record('a')).mockResolvedValue(record('a', 2))
  render(<X07CustomerProfileEditor customerKey="a" drawingKey="a:v1" loadProfile={load} saveProfile={save} />)
  const layer = await screen.findByLabelText('Module layer')
  const restore = () => fireEvent.click(screen.getByRole('button', { name: 'Restore loaded module value' }))
  fireEvent.change(layer, { target: { value: 'ONE' } }); restore()
  fireEvent.change(layer, { target: { value: 'TWO' } })
  expect(screen.queryByRole('button', { name: 'Undo last restore' })).toBeNull()
  restore(); fireEvent.click(screen.getByRole('button', { name: 'Reset draft' }))
  expect(screen.queryByRole('button', { name: 'Undo last restore' })).toBeNull()
  fireEvent.change(layer, { target: { value: 'THREE' } }); restore()
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' })); await screen.findByRole('alert')
  expect(screen.getByRole('button', { name: 'Undo last restore' })).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))
  await screen.findByText('Profile version: 2')
  expect(screen.queryByRole('button', { name: 'Undo last restore' })).toBeNull()
  expect(save).toHaveBeenCalledTimes(1)
})

it('authors tracker type and multiplier, restores the legacy draft and reopens the saved version', async () => {
  const original = record('a')
  const save = vi.fn(async (_customer, roles) => ({ ...original, roles, version: 2 }))
  const renderEditor = profile => render(<X07CustomerProfileEditor customerKey="a"
    loadProfile={async () => profile} saveProfile={save} />)
  const editor = renderEditor(original)
  fireEvent.click(await screen.findByRole('button', { name: 'Add tracker role' }))
  for (const [label, value] of [['Tracker block', 'T'], ['Tracker layer', 'Posts'],
    ['Tracker type', 'west-2'], ['Modules per instance', '24']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
  fireEvent.click(screen.getByRole('button', { name: 'Restore loaded trackers value' }))
  expect(screen.queryByLabelText('Tracker type')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Undo last restore' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Profile saved.')
  const rows = [{ block: 'T', layer: 'Posts', tracker_type: 'west-2', modules_per_instance: 24 }]
  expect(save.mock.calls[0][1]).toEqual({ ...original.roles, trackers: rows })
  editor.unmount()
  renderEditor(await save.mock.results[0].value)
  expect(await screen.findByLabelText('Tracker type')).toHaveValue('west-2')
  expect(screen.getByLabelText('Modules per instance')).toHaveValue(24)
})

it.each([
  { name: 'a peer tracker edit', peerEdit: true, removeLocal: false },
  { name: 'a local tracker removal', peerEdit: false, removeLocal: true },
  { name: 'a conflicting local removal and peer edit', peerEdit: true, removeLocal: true },
])('reloads a stale profile with $name', async ({ peerEdit, removeLocal }) => {
  const tracker = { block: 'T', layer: 'Posts', tracker_type: 'west-2', modules_per_instance: 24 }
  const original = record('a')
  const initial = { ...original, roles: { ...original.roles, trackers: [tracker] } }
  const latest = { ...initial, version: 2, roles: { ...initial.roles,
    obstruction: { layer: 'PEER-OBSTRUCTION' },
    trackers: [peerEdit ? { ...tracker, modules_per_instance: 36 } : tracker] } }
  const load = vi.fn().mockResolvedValueOnce(initial).mockResolvedValue(latest)
  const save = vi.fn().mockRejectedValueOnce({ code: 'STALE_VERSION' })
    .mockImplementation(async (_customer, roles) => ({ ...latest, roles, version: 3 }))
  render(<X07CustomerProfileEditor customerKey="a" loadProfile={load} saveProfile={save}
    applicationRecord={initial} pinnedRoles={initial.roles} />)
  fireEvent.change(await screen.findByLabelText('Module layer'), { target: { value: 'MY-DRAFT' } })
  if (removeLocal) fireEvent.click(screen.getByRole('button', { name: 'Remove tracker 1' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  const reload = () => fireEvent.click(screen.getByRole('button', { name: 'Reload version, keep edits' }))
  await screen.findByRole('alert')
  reload()

  if (peerEdit && removeLocal) {
    await screen.findByText(/Tracker roles changed while you edited them/)
    expect(screen.getByText('Profile version: 1')).toBeTruthy()
    expect(screen.queryByLabelText('Tracker type')).toBeNull()
    const retry = screen.queryByRole('button', { name: /save profile/i })
    if (retry) expect(retry).toBeDisabled()
    expect(save).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: 'Restore loaded trackers value' }))
    reload()
  }

  await screen.findByText('Profile version: 2')
  expect(screen.getByLabelText('Obstruction layer')).toHaveValue('PEER-OBSTRUCTION')
  expect(screen.getByLabelText('Module layer')).toHaveValue('MY-DRAFT')
  const { trackers, ...latestRoles } = latest.roles
  const expected = { ...latestRoles, module: { ...latestRoles.module, layer: 'MY-DRAFT' } }
  if (peerEdit || !removeLocal) expected.trackers = trackers
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await screen.findByText('Profile version: 3')
  expect(save.mock.calls[1][1]).toEqual(expected)
  expect(save.mock.calls.map(call => call[2].expected_version)).toEqual([1, 2])
  expect(screen.getByText('Project is pinned to version 1.')).toBeTruthy()
})
