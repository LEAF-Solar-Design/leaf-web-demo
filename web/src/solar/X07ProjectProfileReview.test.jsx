import React from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
