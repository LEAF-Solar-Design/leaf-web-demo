import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import * as api from '../api.js'
import ProjectSolarWorkspace from './ProjectSolarWorkspace.jsx'
import X07ProjectProfileReview from './X07ProjectProfileReview.jsx'
import { createSolarProjectClient } from './solarProjectClient.js'

const ids = [1, 2, 3, 4].map(value => `${String(value).repeat(8)}-${String(value).repeat(4)}-${String(value).repeat(4)}-${String(value).repeat(4)}-${String(value).repeat(12)}`)
const binding = { kind: 'project', organization_id: ids[0], project_id: ids[1], drawing_id: ids[2], input_version_id: ids[3] }
const roles = { module: { block: 'Panel', layer: 'PV' } }
const row = { block: 'Panel', layer: 'PV', count: 2, insertion_bounds: [0, 0, 2, 2] }
const discovery = { schema: 'leaf.drawing-role-discovery.v1', module: { first_choice: row, candidates: [row], competing: false }, roof: [], obstruction: [] }
const identity = { customer_key: ids[0], project_key: ids[1], drawing_key: ids[2], input_version_id: ids[3] }
const path = `/api/projects/${ids[1]}/drawings/${ids[2]}/versions/${ids[3]}/role-profile`
const copy = value => JSON.parse(JSON.stringify(value))
const response = (data, status = 200) => ({ ok: status === 200, status, json: async () => copy(data) })
const clients = []
const isCurrent = () => true

afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.dispose()) })

function fixture() {
  const state = { profile: null, pin: null, refusal: false }
  const fetchImpl = vi.fn(async (url, init) => {
    if (url.includes('/context?')) return response({ context: { ...binding, head_version_id: ids[3], is_head: true },
      intake: {}, role_discovery: discovery })
    if (url.includes('/role-profile/history') && state.history) return state.history(url, init)
    expect(url).toBe(`https://fixture.test${path}`)
    if (init.method === 'POST') {
      if (state.refusal) return response({ error: { reason_code: 'X07_PROFILE_CONFLICT' } }, 409)
      const selection = JSON.parse(init.body)
      expect(selection).toEqual({ block: 'Panel', layer: 'PV', design_window: [0, 0, 2, 2], expected_version: 0 })
      state.profile = { customer_key: ids[0], profile_id: 'profile-one', version: 1, roles: copy(roles) }
      state.pin = { ...copy(state.profile), project_key: ids[1] }
      return response({ ...identity, ok: true, applied_profile: state.pin })
    }
    if (init.method === 'PUT') {
      const edit = JSON.parse(init.body)
      expect(edit.expected_version).toBe(state.profile.version)
      state.profile = { ...state.profile, version: state.profile.version + 1, roles: edit.roles }
      return response({ ...identity, profile: state.profile })
    }
    return response({ ...identity, expected_version: state.profile?.version ?? 0,
      profile: state.profile, application_record: state.pin })
  })
  const client = createSolarProjectClient({ fetchImpl, apiBase: 'https://fixture.test',
    authHeaders: () => ({ Authorization: 'Bearer synthetic' }) })
  client.setScope(binding, 'authority-one')
  clients.push(client)
  return { state, fetchImpl, client, props: { solarBinding: binding, projectClient: client,
    authorityKey: 'authority-one', isCurrent, projectCatalog: null } }
}

it('normal Solar workspace confirms once, edits a new version and reloads the old project pin without catalog', async () => {
  const h = fixture()
  const view = render(<ProjectSolarWorkspace {...h.props} />)
  const confirm = await screen.findByRole('button', { name: 'Confirm roles' })
  fireEvent.click(confirm)
  const block = await screen.findByLabelText('Module block')
  expect(block.value).toBe('Panel')
  const oldPin = copy(h.state.pin)
  fireEvent.change(block, { target: { value: 'Edited' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await waitFor(() => expect(h.state.profile.version).toBe(2))
  await waitFor(() => expect(screen.getByLabelText('Module block').value).toBe('Edited'))
  expect(h.state.pin).toEqual(oldPin)
  expect(h.fetchImpl.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
  view.unmount()
  render(<ProjectSolarWorkspace {...h.props} />)
  await screen.findByText('Saved drawing roles loaded.')
  expect((await screen.findByLabelText('Module block')).value).toBe('Edited')
  expect(h.state.pin).toEqual(oldPin)
  expect(screen.queryByRole('button', { name: 'Confirm roles' })).toBeNull()
  for (const [, init] of h.fetchImpl.mock.calls) {
    expect(init.headers).toMatchObject({ Authorization: 'Bearer synthetic', 'X-Org-Id': ids[0], 'X-Project-Id': ids[1] })
    expect(init.cache).toBe('no-store')
  }
})

it('a refused confirmation retains the choice and allows only explicit retry', async () => {
  const h = fixture(); h.state.refusal = true
  render(<X07ProjectProfileReview {...h.props} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  await screen.findByText('Roles could not be saved. Try again.')
  expect(screen.getByRole('radio').checked).toBe(true)
  expect(screen.queryByRole('region', { name: 'Customer role profile' })).toBeNull()
  expect(h.fetchImpl.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
  h.state.refusal = false
  fireEvent.click(screen.getByRole('button', { name: 'Confirm roles' }))
  await screen.findByLabelText('Module block')
  expect(h.fetchImpl.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(2)
})

it('rejects a foreign profile response before exposing its map', async () => {
  const h = fixture()
  h.fetchImpl.mockResolvedValue(response({ ...identity, customer_key: ids[1], expected_version: 1,
    application_record: { customer_key: ids[1], roles: { module: { block: 'private sentinel', layer: 'private' } } } }))
  render(<X07ProjectProfileReview {...h.props} />)
  await screen.findByText('Customer role profile could not be read.')
  expect(screen.queryByText(/private sentinel/)).toBeNull()
  expect(screen.queryByRole('button', { name: 'Confirm roles' })).toBeNull()
})

it('retires an old client read on same-scope authority replacement', async () => {
  const old = fixture(), fresh = fixture()
  let settle
  old.fetchImpl.mockImplementationOnce(() => new Promise(resolve => { settle = resolve }))
  const view = render(<X07ProjectProfileReview {...old.props} />)
  await waitFor(() => expect(old.fetchImpl).toHaveBeenCalledTimes(1))
  const signal = old.fetchImpl.mock.calls[0][1].signal
  view.rerender(<X07ProjectProfileReview {...fresh.props} />)
  expect(signal.aborted).toBe(true)
  settle(response({ ...identity, expected_version: 1, application_record: { customer_key: ids[0],
    project_key: ids[1], profile_id: 'retired', version: 1, roles: copy(roles) } }))
  await screen.findByRole('button', { name: 'Confirm roles' })
  expect(screen.queryByText('Saved drawing roles loaded.')).toBeNull()
})

it.each([0, 3])('opens the acknowledged editor from profile version %i without waiting for another read', async initialVersion => {
  const h = fixture()
  const base = h.fetchImpl.getMockImplementation()
  const saved = { customer_key: ids[0], profile_id: 'profile-one', version: initialVersion || 1, roles }
  let confirmed = false
  h.fetchImpl.mockImplementation(async (url, init) => {
    if (url.endsWith(path) && init.method === 'POST') {
      expect(JSON.parse(init.body).expected_version).toBe(initialVersion)
      confirmed = true
      return response({ ...identity, ok: true, applied_profile: { ...saved, project_key: ids[1] }, profile: saved })
    }
    if (url.endsWith(path) && init.method === 'GET') {
      if (confirmed) return new Promise(() => {})
      return response({ ...identity, expected_version: initialVersion, profile: initialVersion ? saved : null, application_record: null })
    }
    return base(url, init)
  })
  render(<ProjectSolarWorkspace {...h.props} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  await screen.findByText('Roles confirmed.')
  const layer = await screen.findByLabelText('Module layer')
  expect(layer.value).toBe('PV')
  fireEvent.change(layer, { target: { value: 'Edited layer' } })
  expect(layer.value).toBe('Edited layer')
  expect(screen.getByRole('button', { name: 'Save profile' }).disabled).toBe(false)
  expect(h.fetchImpl.mock.calls.filter(([url, init]) => url.endsWith(path) && init.method === 'GET')).toHaveLength(1)
})

it('ignores a retired confirmation when the project changes before acknowledgment', async () => {
  const old = fixture(), fresh = fixture()
  const base = old.fetchImpl.getMockImplementation()
  let finish
  old.fetchImpl.mockImplementation((url, init) => init.method !== 'POST' ? base(url, init) : new Promise(resolve => { finish = resolve }))
  const view = render(<X07ProjectProfileReview {...old.props} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Confirm roles' }))
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  const other = { ...binding, project_id: ids[3] }
  fresh.client.setScope(other, 'next')
  const active = { customer_key: ids[0], profile_id: 'new-profile', version: 2, roles: { module: { block: 'Active', layer: 'New' } } }
  fresh.fetchImpl.mockImplementation(async () => response({ ...identity, project_key: other.project_id,
    expected_version: 2, profile: active, application_record: { ...active, project_key: other.project_id } }))
  view.rerender(<X07ProjectProfileReview {...fresh.props} authorityKey="next" solarBinding={other} />)
  await screen.findByDisplayValue('Active')
  const retired = { ...active, profile_id: 'old-profile', roles, version: 1 }
  await act(async () => finish(response({ ...identity, ok: true, profile: retired,
    applied_profile: { ...retired, project_key: ids[1] } })))
  expect(screen.getByLabelText('Module layer').value).toBe('New')
  expect(old.fetchImpl.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(1)
})


function setupHistory() {
  const h = fixture()
  h.state.profile = { customer_key: ids[0], profile_id: 'profile-one', version: 2, roles: copy(roles) }
  h.state.pin = { ...copy(h.state.profile), version: 1, project_key: ids[1] }
  return h
}

it.each([
  ['Deck East', 'Deck West', 'outline', undefined, undefined],
  ['Roof Lower', 'Roof Upper', 'outline', undefined, undefined],
  ['Roof East', 'Roof West', 'insert', 'Level 1', 'Level 2'],
])('reviews %s and %s before saving a replacement roof role', async (first, second, kind, firstBlock, secondBlock) => {
  const h = setupHistory()
  h.state.profile.roles.roof = { layer: 'Roof Middle' }
  h.state.pin.roles.roof = { layer: 'Roof Middle' }
  const originalPin = copy(h.state.pin)
  const firstRole = firstBlock ? { layer: first, block: firstBlock } : { layer: first }
  const secondRole = secondBlock ? { layer: second, block: secondBlock } : { layer: second }
  const candidates = [
    { handle: 'E1', role: firstRole, kind, bounds: kind === 'outline' ? [0, 0, 8, 8] : [0, 0, 0, 0] },
    { handle: 'E2', role: secondRole, kind, bounds: kind === 'outline' ? [12, 0, 20, 8] : [12, 0, 12, 0] },
  ]
  const base = h.fetchImpl.getMockImplementation()
  h.fetchImpl.mockImplementation((url, init) => url.endsWith(path) && init.method === 'GET'
    ? Promise.resolve(response({ ...identity, expected_version: h.state.profile.version,
      profile: h.state.profile, application_record: h.state.pin,
      roof_inheritance_candidates: h.state.profile.version === 2 ? candidates : [] }))
    : base(url, init))
  const writes = () => h.fetchImpl.mock.calls.filter(([, init]) => init.method !== 'GET')
  const firstLabel = firstBlock ? `${first} · ${firstBlock}` : first
  const secondLabel = secondBlock ? `${second} · ${secondBlock}` : second

  render(<ProjectSolarWorkspace {...h.props} />)
  await screen.findByRole('group', { name: 'Roof role candidates' })
  expect(screen.getByText(`${firstLabel} · E1`)).toBeTruthy()
  expect(screen.getByText(`${secondLabel} · E2`)).toBeTruthy()
  expect(screen.getByText(kind === 'outline' ? 'Outline bounds: 12, 0, 20, 8' : 'Insertion point: 12, 0')).toBeTruthy()
  expect(screen.getByLabelText('Roof layer').value).toBe('Roof Middle')
  expect(writes()).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: `Use ${firstLabel} in draft` }))
  expect(screen.getByLabelText('Roof layer').value).toBe(first)
  fireEvent.click(screen.getByRole('button', { name: `Use ${secondLabel} in draft` }))
  expect(screen.getByLabelText('Roof layer').value).toBe(second)
  expect(screen.queryByLabelText('Roof block')?.value).toBe(secondBlock)
  expect(screen.getByLabelText('Module block').value).toBe('Panel')
  expect(writes()).toHaveLength(0)

  fireEvent.click(screen.getByRole('button', { name: 'Save profile' }))
  await waitFor(() => expect(screen.queryByRole('group', { name: 'Roof role candidates' })).toBeNull())
  expect(h.state.profile.version).toBe(3)
  expect(h.state.pin).toEqual(originalPin)
  expect(writes()).toHaveLength(1)
  expect(writes()[0][1].method).toBe('PUT')
  expect(JSON.parse(writes()[0][1].body)).toEqual({ expected_version: 2, roles: { ...roles, roof: secondRole } })
})

it.each([
  ['DraftPanel', 'PV', ['A1[0,0]'], ['B1[0,0]']],
  ['Panel', 'Alternative', ['A2[0,0]'], []],
  ['UnusedPanel', 'PV', [], []],
])('shows only the selected copy after Undo to %s on %s', async (block, layer, first, second) => {
  const h = setupHistory()
  const base = h.fetchImpl.getMockImplementation()
  const outlines = [{ handle: 'E1', layer: 'Roof' }, { handle: 'E2', layer: 'Roof' }]
  h.fetchImpl.mockImplementation((url, init) => url.endsWith(path) && init.method === 'GET'
    ? Promise.resolve(response({ ...identity, expected_version: h.state.profile.version,
      profile: h.state.profile, application_record: h.state.pin, outline_choices: outlines }))
    : base(url, init))
  const preview = vi.spyOn(api, 'previewRoofPlacementChanges')
  const report = paths => ({ input_version_id: ids[3], previous_input_version_id: ids[3],
    windows: [{ added_paths: paths }] })
  let finishSecond
  preview.mockResolvedValueOnce(report(first))
    .mockImplementationOnce(() => new Promise(resolve => { finishSecond = resolve }))
    .mockResolvedValueOnce(report(first))
    .mockResolvedValueOnce(report(second))

  try {
    render(<ProjectSolarWorkspace {...h.props} />)
    fireEvent.change(await screen.findByLabelText('Module block'), { target: { value: block } })
    fireEvent.change(screen.getByLabelText('Module layer'), { target: { value: layer } })
    fireEvent.click(screen.getByRole('button', { name: 'Restore loaded module value' }))
    expect(preview).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Undo last restore' }))
    const picker = screen.getByLabelText('Design copy outline')
    expect(picker.value).toBe('E1')
    if (first.length) await screen.findByText(first[0])
    else await screen.findByText('No panels returned in this copy.')

    fireEvent.change(picker, { target: { value: 'E2' } })
    if (first.length) expect(screen.queryByText(first[0])).toBeNull()
    await waitFor(() => expect(finishSecond).toBeTypeOf('function'))
    fireEvent.change(picker, { target: { value: 'E1' } })
    expect(preview.mock.calls[1][3].signal.aborted).toBe(true)
    await act(async () => finishSecond(report(['DEAD[0,0]'])))
    expect(screen.queryByText('DEAD[0,0]')).toBeNull()
    fireEvent.change(picker, { target: { value: 'E2' } })
    if (second.length) await screen.findByText(second[0])
    else await screen.findByText('No panels returned in this copy.')
    if (first.length) expect(screen.queryByText(first[0])).toBeNull()

    expect(screen.getByLabelText('Module block').value).toBe(block)
    expect(screen.getByLabelText('Module layer').value).toBe(layer)
    expect(h.fetchImpl.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true)
    expect(preview.mock.calls.map(([org, revision, request]) => [org, revision, request])).toEqual(
      ['E1', 'E2', 'E1', 'E2'].map(handle => [ids[0], binding, { previous_input_version_id: ids[3],
        previous_role: roles.module, block, layer, windows: [{ within: { outline_handle: handle } }] }]),
    )
    fireEvent.change(screen.getByLabelText('Module block'), { target: { value: 'New edit' } })
    expect(screen.queryByLabelText('Design copy outline')).toBeNull()
  } finally {
    preview.mockRestore()
  }
})

function historyResponse(version, cursor, changes, scope = identity) {
  return response({ ...scope, profile_id: 'profile-one', through_version: 2, next_cursor: cursor,
    rows: [{ version, created_at: '2026-11-25T00:00:00Z', changes }] })
}

it('shows exact role changes on demand and loads older versions without writing', async () => {
  const h = setupHistory()
  h.state.history = vi.fn(async url => new URL(url).searchParams.has('cursor')
    ? historyResponse(1, null, [])
    : historyResponse(2, 'before=2', [{ role: 'module', field: 'block', operation: 'changed',
      old: 'Panel', new: 'Edited' }]))
  render(<X07ProjectProfileReview {...h.props} />)
  const open = await screen.findByRole('button', { name: 'View profile history' })
  expect(h.state.history).not.toHaveBeenCalled()
  fireEvent.click(open)
  await screen.findByText('module.block changed from Panel to Edited')
  fireEvent.click(screen.getByRole('button', { name: 'Load older revisions' }))
  await screen.findByText('Version 1')
  expect(screen.getByText('Version 2')).toBeTruthy()
  expect(new URL(h.state.history.mock.calls[1][0]).searchParams.get('cursor')).toBe('before=2')
  expect(h.fetchImpl.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true)
  expect(h.state.pin.version).toBe(1)
})

it('rejects another customer history and permits a fresh read', async () => {
  const h = setupHistory()
  const forbidden = [{ role: 'roof', field: 'layer', operation: 'added', old: null, new: 'foreign layer' }]
  h.state.history = vi.fn()
    .mockResolvedValueOnce(historyResponse(2, 'two', forbidden, { ...identity, customer_key: ids[1] }))
    .mockResolvedValueOnce(historyResponse(2, 'two', []))
  render(<X07ProjectProfileReview {...h.props} />)
  fireEvent.click(await screen.findByRole('button', { name: 'View profile history' }))
  await screen.findByText('Profile history could not be read.')
  expect(screen.queryByText(/foreign layer/)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Retry profile history' }))
  await screen.findByText('Version 2')
  expect(screen.queryByText('Profile history could not be read.')).toBeNull()
})

it('retires a history reply when the project authority changes', async () => {
  const h = setupHistory()
  let resolve, signal
  h.state.history = vi.fn((_url, init) => {
    signal = init.signal
    return new Promise(settle => { resolve = settle })
  })
  const view = render(<X07ProjectProfileReview {...h.props} />)
  fireEvent.click(await screen.findByRole('button', { name: 'View profile history' }))
  await waitFor(() => expect(h.state.history).toHaveBeenCalledOnce())
  h.client.setScope(binding, 'authority-two')
  view.rerender(<X07ProjectProfileReview {...h.props} authorityKey="authority-two" />)
  await screen.findByRole('button', { name: 'View profile history' })
  expect(signal.aborted).toBe(true)
  await act(async () => resolve(historyResponse(2, 'two', [])))
  expect(screen.queryByText('Version 2')).toBeNull()
})
